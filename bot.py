import os
import sqlite3
import asyncio
from dataclasses import dataclass
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
RIOT_API_KEY = os.getenv("RIOT_API_KEY", "")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
LEADERBOARD_CHANNEL_ID = int(os.getenv("LEADERBOARD_CHANNEL_ID", "0"))
UPDATE_MINUTES = int(os.getenv("UPDATE_MINUTES", "10"))
PLATFORM = os.getenv("PLATFORM", "EUW1").lower()      # e.g. euw1, eun1, na1
REGIONAL = os.getenv("REGIONAL", "EUROPE").lower()    # europe, americas, asia, sea
DB_PATH = os.getenv("DB_PATH", "lol_elo_bot.sqlite3")

TIERS = {
    "IRON": 0, "BRONZE": 400, "SILVER": 800, "GOLD": 1200,
    "PLATINUM": 1600, "EMERALD": 2000, "DIAMOND": 2400,
    "MASTER": 2800, "GRANDMASTER": 2800, "CHALLENGER": 2800,
}
DIVISIONS = {"IV": 0, "III": 100, "II": 200, "I": 300}


@dataclass
class RankedEntry:
    tier: str
    rank: str
    lp: int
    wins: int
    losses: int

    @property
    def absolute_lp(self) -> int:
        # Master+ has no divisions; Riot returns rank often as I.
        if self.tier in {"MASTER", "GRANDMASTER", "CHALLENGER"}:
            return TIERS[self.tier] + self.lp
        return TIERS[self.tier] + DIVISIONS.get(self.rank, 0) + self.lp

    @property
    def label(self) -> str:
        if self.tier in {"MASTER", "GRANDMASTER", "CHALLENGER"}:
            return f"{self.tier.title()} {self.lp} LP"
        return f"{self.tier.title()} {self.rank} {self.lp} LP"


class RiotClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session: Optional[aiohttp.ClientSession] = None

    async def start(self):
        self.session = aiohttp.ClientSession(headers={"X-Riot-Token": self.api_key})

    async def close(self):
        if self.session:
            await self.session.close()

    async def _get(self, url: str) -> dict | list:
        assert self.session is not None
        async with self.session.get(url) as resp:
            if resp.status == 404:
                raise ValueError("Riot Account nicht gefunden.")
            if resp.status == 403:
                raise ValueError("Riot API Key ungültig oder abgelaufen.")
            if resp.status == 429:
                raise ValueError("Riot Rate Limit erreicht. Später erneut versuchen.")
            if resp.status >= 400:
                text = await resp.text()
                raise ValueError(f"Riot API Fehler {resp.status}: {text[:200]}")
            return await resp.json()

    async def get_puuid(self, game_name: str, tag_line: str) -> str:
        url = f"https://{REGIONAL}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
        data = await self._get(url)
        return data["puuid"]

    async def get_soloq_entry_by_puuid(self, puuid: str) -> Optional[RankedEntry]:
        url = f"https://{PLATFORM}.api.riotgames.com/lol/league/v4/entries/by-puuid/{puuid}"
        entries = await self._get(url)
        for entry in entries:
            if entry.get("queueType") == "RANKED_SOLO_5x5":
                return RankedEntry(
                    tier=entry["tier"],
                    rank=entry.get("rank", "I"),
                    lp=int(entry["leaguePoints"]),
                    wins=int(entry.get("wins", 0)),
                    losses=int(entry.get("losses", 0)),
                )
        return None

    async def get_current_soloq(self, game_name: str, tag_line: str) -> Optional[RankedEntry]:
        puuid = await self.get_puuid(game_name, tag_line)
        return await self.get_soloq_entry_by_puuid(puuid)


class Database:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS players (
                discord_id INTEGER PRIMARY KEY,
                game_name TEXT NOT NULL,
                tag_line TEXT NOT NULL,
                display_name TEXT NOT NULL,
                peak_lp INTEGER NOT NULL,
                peak_label TEXT NOT NULL,
                current_lp INTEGER NOT NULL,
                current_label TEXT NOT NULL,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        self.conn.commit()

    def upsert_player(self, discord_id: int, game_name: str, tag_line: str, display_name: str, entry: RankedEntry):
        self.conn.execute("""
            INSERT INTO players(discord_id, game_name, tag_line, display_name, peak_lp, peak_label, current_lp, current_label)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(discord_id) DO UPDATE SET
                game_name=excluded.game_name,
                tag_line=excluded.tag_line,
                display_name=excluded.display_name,
                peak_lp=MAX(players.peak_lp, excluded.current_lp),
                peak_label=CASE WHEN excluded.current_lp > players.peak_lp THEN excluded.current_label ELSE players.peak_label END,
                current_lp=excluded.current_lp,
                current_label=excluded.current_label,
                updated_at=CURRENT_TIMESTAMP
        """, (discord_id, game_name, tag_line, display_name, entry.absolute_lp, entry.label, entry.absolute_lp, entry.label))
        self.conn.commit()

    def update_player_rank(self, discord_id: int, entry: RankedEntry):
        row = self.conn.execute("SELECT peak_lp FROM players WHERE discord_id=?", (discord_id,)).fetchone()
        if not row:
            return
        peak_lp = max(int(row["peak_lp"]), entry.absolute_lp)
        peak_label = entry.label if entry.absolute_lp >= int(row["peak_lp"]) else self.conn.execute(
            "SELECT peak_label FROM players WHERE discord_id=?", (discord_id,)
        ).fetchone()["peak_label"]
        self.conn.execute("""
            UPDATE players
            SET current_lp=?, current_label=?, peak_lp=?, peak_label=?, updated_at=CURRENT_TIMESTAMP
            WHERE discord_id=?
        """, (entry.absolute_lp, entry.label, peak_lp, peak_label, discord_id))
        self.conn.commit()

    def remove_player(self, discord_id: int):
        self.conn.execute("DELETE FROM players WHERE discord_id=?", (discord_id,))
        self.conn.commit()

    def players(self):
        return self.conn.execute("SELECT * FROM players ORDER BY (current_lp - peak_lp) DESC, current_lp DESC").fetchall()

    def get_setting(self, key: str) -> Optional[str]:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str):
        self.conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (key, value))
        self.conn.commit()


def format_delta(delta: int) -> str:
    return f"+{delta} LP" if delta > 0 else f"{delta} LP"


def build_leaderboard(rows) -> str:
    if not rows:
        return "**LoL Peak Leaderboard**\nNoch keine Spieler registriert. Nutze `/register`."

    lines = ["**LoL Peak Leaderboard**", "`Name | Delta from peak elo`", ""]
    for idx, row in enumerate(rows, start=1):
        delta = int(row["current_lp"]) - int(row["peak_lp"])
        lines.append(
            f"`{idx:>2}.` **{row['display_name']}** | `{format_delta(delta)}` "
            f"({row['current_label']} / Peak: {row['peak_label']})"
        )
    return "\n".join(lines)


intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
riot = RiotClient(RIOT_API_KEY)
db = Database(DB_PATH)


async def refresh_all_players() -> int:
    count = 0
    for row in db.players():
        try:
            entry = await riot.get_current_soloq(row["game_name"], row["tag_line"])
            if entry:
                db.update_player_rank(int(row["discord_id"]), entry)
                count += 1
            await asyncio.sleep(1.2)  # simple protection vs. rate limits
        except Exception as exc:
            print(f"Refresh failed for {row['display_name']}: {exc}")
    return count


async def post_or_edit_leaderboard() -> None:
    channel = bot.get_channel(LEADERBOARD_CHANNEL_ID)
    if not isinstance(channel, discord.TextChannel):
        return
    content = build_leaderboard(db.players())
    message_id = db.get_setting("leaderboard_message_id")
    if message_id:
        try:
            msg = await channel.fetch_message(int(message_id))
            await msg.edit(content=content)
            return
        except discord.NotFound:
            pass
    msg = await channel.send(content)
    db.set_setting("leaderboard_message_id", str(msg.id))


@tasks.loop(minutes=UPDATE_MINUTES)
async def periodic_update():
    await refresh_all_players()
    await post_or_edit_leaderboard()


@bot.event
async def on_ready():
    await riot.start()
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.copy_global_to(guild=guild)
        await bot.tree.sync(guild=guild)
    else:
        await bot.tree.sync()
    periodic_update.start()
    print(f"Logged in as {bot.user}")


@bot.event
async def close():
    await riot.close()
    await super(type(bot), bot).close()


@bot.tree.command(description="Registriert deine Riot ID privat für das Peak-Elo-Leaderboard.")
@app_commands.describe(game_name="Riot ID Name, z.B. Faker", tag_line="Riot ID Tag, z.B. EUW", display_name="Name im Leaderboard, optional")
async def register(interaction: discord.Interaction, game_name: str, tag_line: str, display_name: Optional[str] = None):
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        entry = await riot.get_current_soloq(game_name.strip(), tag_line.strip())
        if entry is None:
            await interaction.followup.send("Ich finde für diesen Account keine SoloQ-Rank-Daten.", ephemeral=True)
            return
        name = display_name.strip() if display_name else interaction.user.display_name
        db.upsert_player(interaction.user.id, game_name.strip(), tag_line.strip(), name, entry)
        await post_or_edit_leaderboard()
        await interaction.followup.send(f"Registriert: **{name}** mit aktuellem Peak **{entry.label}**.", ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(f"Fehler: {exc}", ephemeral=True)


@bot.tree.command(description="Entfernt dich aus dem Leaderboard.")
async def unregister(interaction: discord.Interaction):
    db.remove_player(interaction.user.id)
    await post_or_edit_leaderboard()
    await interaction.response.send_message("Du wurdest aus dem Leaderboard entfernt.", ephemeral=True)


@bot.tree.command(description="Zeigt das Leaderboard.")
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.send_message(build_leaderboard(db.players()))


@bot.tree.command(description="Aktualisiert Elo-Daten und Leaderboard manuell.")
async def refresh(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True, thinking=True)
    count = await refresh_all_players()
    await post_or_edit_leaderboard()
    await interaction.followup.send(f"Aktualisiert: {count} Spieler.", ephemeral=True)


if not DISCORD_TOKEN or not RIOT_API_KEY:
    raise RuntimeError("Bitte DISCORD_TOKEN und RIOT_API_KEY in .env setzen.")

bot.run(DISCORD_TOKEN)
