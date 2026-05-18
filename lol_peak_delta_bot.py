import os
import re
import sqlite3
from dataclasses import dataclass
from typing import Optional, Tuple

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
RIOT_API_KEY = os.getenv("RIOT_API_KEY")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
LEADERBOARD_CHANNEL_ID = int(os.getenv("LEADERBOARD_CHANNEL_ID", "0"))

PLATFORM = os.getenv("RIOT_PLATFORM", "EUW1").upper()
REGIONAL = os.getenv("RIOT_REGIONAL", "EUROPE").lower()
DB_PATH = os.getenv("DB_PATH", "elo_bot.db")

TIER_BASE = {
    "IRON": 0, "BRONZE": 400, "SILVER": 800, "GOLD": 1200,
    "PLATINUM": 1600, "EMERALD": 2000, "DIAMOND": 2400,
    "MASTER": 2800, "GRANDMASTER": 3200, "CHALLENGER": 3600,
}
DIVISION_VALUE = {"IV": 0, "III": 100, "II": 200, "I": 300}


@dataclass
class RankedEntry:
    tier: str
    rank: str
    lp: int
    wins: int = 0
    losses: int = 0

    @property
    def label(self) -> str:
        if self.tier in {"MASTER", "GRANDMASTER", "CHALLENGER"}:
            return f"{self.tier.title()} {self.lp} LP"
        return f"{self.tier.title()} {self.rank} {self.lp} LP"

    @property
    def score(self) -> int:
        return TIER_BASE.get(self.tier.upper(), 0) + DIVISION_VALUE.get(self.rank.upper(), 0) + self.lp


def parse_riot_id(value: str) -> Tuple[str, str]:
    value = value.strip()
    if "#" not in value:
        raise ValueError("Bitte Riot ID im Format `Name#TAG` eingeben, z. B. `Arexor#EUW`.")
    game_name, tag_line = value.split("#", 1)
    game_name, tag_line = game_name.strip(), tag_line.strip()
    if not game_name or not tag_line:
        raise ValueError("Bitte Riot ID im Format `Name#TAG` eingeben, z. B. `Arexor#EUW`.")
    return game_name, tag_line


def parse_peak_elo(value: str) -> int:
    raw = value.strip().upper().replace("LP", "").replace("_", " ")
    raw = re.sub(r"\s+", " ", raw)

    aliases = {
        "I": "IRON", "B": "BRONZE", "S": "SILVER", "G": "GOLD",
        "P": "PLATINUM", "E": "EMERALD", "D": "DIAMOND",
        "M": "MASTER", "GM": "GRANDMASTER", "C": "CHALLENGER",
    }

    compact = re.match(r"^(GM|[IBSGPEDMC])\s*([1-4IVX]*)?\s*(\d+)?$", raw)
    if compact:
        tier = aliases[compact.group(1)]
        div_raw = compact.group(2) or "I"
        lp = int(compact.group(3) or 0)
    else:
        parts = raw.split()
        if not parts:
            raise ValueError("Peak Elo fehlt.")
        tier = parts[0]
        if tier not in TIER_BASE:
            raise ValueError("Unbekannte Peak Elo. Beispiel: `Diamond 2 50` oder `Master 120`.")

        div_raw = "I"
        lp = 0
        if tier in {"MASTER", "GRANDMASTER", "CHALLENGER"}:
            if len(parts) >= 2:
                lp = int(parts[1])
        else:
            if len(parts) < 2:
                raise ValueError("Bitte Division angeben. Beispiel: `Diamond 2 50`.")
            div_raw = parts[1]
            if len(parts) >= 3:
                lp = int(parts[2])

    roman_map = {"1": "I", "2": "II", "3": "III", "4": "IV", "I": "I", "II": "II", "III": "III", "IV": "IV"}
    div = roman_map.get(div_raw, div_raw)

    if tier not in TIER_BASE:
        raise ValueError("Unbekannter Rang.")
    if tier not in {"MASTER", "GRANDMASTER", "CHALLENGER"} and div not in DIVISION_VALUE:
        raise ValueError("Unbekannte Division. Nutze 1-4 oder I-IV.")
    if lp < 0:
        raise ValueError("LP darf nicht negativ sein.")

    return TIER_BASE[tier] + DIVISION_VALUE.get(div, 0) + lp


def score_to_label(score: int) -> str:
    best_tier = "IRON"
    for tier, base in TIER_BASE.items():
        if score >= base:
            best_tier = tier

    base = TIER_BASE[best_tier]
    remain = score - base

    if best_tier in {"MASTER", "GRANDMASTER", "CHALLENGER"}:
        return f"{best_tier.title()} {remain} LP"

    division = "IV"
    for div, val in DIVISION_VALUE.items():
        if remain >= val:
            division = div
    lp = remain - DIVISION_VALUE[division]
    return f"{best_tier.title()} {division} {lp} LP"


class Database:
    def __init__(self, path: str):
        self.path = path
        self._init()

    def connect(self):
        return sqlite3.connect(self.path)

    def _init(self):
        with self.connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS players (
                    discord_id INTEGER PRIMARY KEY,
                    display_name TEXT NOT NULL,
                    riot_id TEXT NOT NULL,
                    game_name TEXT NOT NULL,
                    tag_line TEXT NOT NULL,
                    opgg_url TEXT NOT NULL,
                    peak_score INTEGER NOT NULL,
                    current_score INTEGER,
                    current_rank TEXT,
                    delta INTEGER,
                    last_error TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )

    def upsert_player(self, discord_id: int, display_name: str, riot_id: str, game_name: str, tag_line: str, opgg_url: str, peak_score: int, current: Optional[RankedEntry]):
        current_score = current.score if current else None
        current_rank = current.label if current else "Unranked"
        delta = current_score - peak_score if current_score is not None else None

        with self.connect() as db:
            db.execute(
                """
                INSERT INTO players (discord_id, display_name, riot_id, game_name, tag_line, opgg_url, peak_score, current_score, current_rank, delta, last_error, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, CURRENT_TIMESTAMP)
                ON CONFLICT(discord_id) DO UPDATE SET
                    display_name=excluded.display_name,
                    riot_id=excluded.riot_id,
                    game_name=excluded.game_name,
                    tag_line=excluded.tag_line,
                    opgg_url=excluded.opgg_url,
                    peak_score=excluded.peak_score,
                    current_score=excluded.current_score,
                    current_rank=excluded.current_rank,
                    delta=excluded.delta,
                    last_error=NULL,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (discord_id, display_name, riot_id, game_name, tag_line, opgg_url, peak_score, current_score, current_rank, delta),
            )

    def update_current(self, discord_id: int, current: Optional[RankedEntry], error: Optional[str] = None):
        with self.connect() as db:
            if error:
                db.execute("UPDATE players SET last_error=?, updated_at=CURRENT_TIMESTAMP WHERE discord_id=?", (error, discord_id))
                return

            current_score = current.score if current else None
            current_rank = current.label if current else "Unranked"
            db.execute(
                """
                UPDATE players
                SET current_score=?,
                    current_rank=?,
                    delta=CASE WHEN ? IS NULL THEN NULL ELSE ? - peak_score END,
                    last_error=NULL,
                    updated_at=CURRENT_TIMESTAMP
                WHERE discord_id=?
                """,
                (current_score, current_rank, current_score, current_score, discord_id),
            )

    def all_players(self):
        with self.connect() as db:
            db.row_factory = sqlite3.Row
            return db.execute(
                """
                SELECT *
                FROM players
                ORDER BY
                    CASE WHEN delta IS NULL THEN 1 ELSE 0 END,
                    delta DESC,
                    display_name COLLATE NOCASE ASC
                """
            ).fetchall()

    def delete_player(self, discord_id: int):
        with self.connect() as db:
            db.execute("DELETE FROM players WHERE discord_id=?", (discord_id,))


class RiotClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    async def _get(self, url: str):
        headers = {"X-Riot-Token": self.api_key}
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url) as resp:
                text = await resp.text()
                if resp.status != 200:
                    raise RuntimeError(f"Riot API Fehler {resp.status}: {text}")
                return await resp.json()

    async def get_puuid(self, game_name: str, tag_line: str) -> str:
        url = f"https://{REGIONAL}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/{game_name}/{tag_line}"
        data = await self._get(url)
        if "puuid" not in data:
            raise RuntimeError("Riot Account nicht gefunden. Prüfe Name#TAG.")
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


def make_opgg_url(game_name: str, tag_line: str) -> str:
    safe_name = game_name.replace(" ", "%20")
    safe_tag = tag_line.replace(" ", "%20")
    region = PLATFORM.lower()
    if region == "euw1":
        region = "euw"
    elif region == "eun1":
        region = "eune"
    return f"https://www.op.gg/summoners/{region}/{safe_name}-{safe_tag}"


def build_leaderboard_embed(rows) -> discord.Embed:
    embed = discord.Embed(
        title="LoL Peak Delta Leaderboard",
        description="**Name | Delta from peak elo**",
        color=discord.Color.blurple(),
    )

    if not rows:
        embed.add_field(name="Noch keine Spieler", value="Nutze `/register`, um dich einzutragen.", inline=False)
        return embed

    lines = []
    for idx, row in enumerate(rows, start=1):
        if row["delta"] is None:
            delta_text = "Unranked"
        else:
            sign = "+" if row["delta"] >= 0 else ""
            delta_text = f"{sign}{row['delta']} LP"

        line = f"**{idx}. [{row['display_name']}]({row['opgg_url']})** | `{delta_text}`"
        lines.append(line)

    embed.add_field(name="Leaderboard", value="\n".join(lines[:30]), inline=False)
    embed.set_footer(text="Delta = current SoloQ elo minus entered peak elo. Auto-update every 30 min.")
    return embed


intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

db = Database(DB_PATH)
riot = RiotClient(RIOT_API_KEY) if RIOT_API_KEY else None


class RegisterModal(discord.ui.Modal, title="LoL Elo Registrierung"):
    riot_id = discord.ui.TextInput(
        label="Riot ID",
        placeholder="Name#TAG, z. B. Arexor#EUW",
        required=True,
        max_length=80,
    )
    opgg = discord.ui.TextInput(
        label="OP.GG Link oder leer lassen",
        placeholder="https://www.op.gg/summoners/euw/name-tag",
        required=False,
        max_length=200,
    )
    peak_elo = discord.ui.TextInput(
        label="Peak Elo",
        placeholder="z. B. Diamond 2 50 oder Master 120",
        required=True,
        max_length=80,
    )

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if riot is None:
            await interaction.followup.send("RIOT_API_KEY fehlt.", ephemeral=True)
            return

        try:
            game_name, tag_line = parse_riot_id(str(self.riot_id.value))
            peak_score = parse_peak_elo(str(self.peak_elo.value))
            opgg_url = str(self.opgg.value).strip() or make_opgg_url(game_name, tag_line)
            current = await riot.get_current_soloq(game_name, tag_line)

            db.upsert_player(
                discord_id=interaction.user.id,
                display_name=interaction.user.display_name,
                riot_id=f"{game_name}#{tag_line}",
                game_name=game_name,
                tag_line=tag_line,
                opgg_url=opgg_url,
                peak_score=peak_score,
                current=current,
            )

            await interaction.followup.send(
                f"Gespeichert: `{game_name}#{tag_line}` | Peak: `{score_to_label(peak_score)}` | Current: `{current.label if current else 'Unranked'}`",
                ephemeral=True,
            )
            await post_or_update_leaderboard()

        except Exception as e:
            await interaction.followup.send(f"Fehler: {e}", ephemeral=True)


@bot.tree.command(name="register", description="Privates Formular: Riot ID, OP.GG und Peak Elo eintragen")
async def register(interaction: discord.Interaction):
    await interaction.response.send_modal(RegisterModal())


@bot.tree.command(name="leaderboard", description="Zeigt das Peak-Delta Leaderboard")
async def leaderboard(interaction: discord.Interaction):
    rows = db.all_players()
    await interaction.response.send_message(embed=build_leaderboard_embed(rows))


@bot.tree.command(name="update_elo", description="Aktualisiert alle aktuellen SoloQ Elos")
@app_commands.checks.has_permissions(administrator=True)
async def update_elo(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    await update_all_players()
    await post_or_update_leaderboard()
    await interaction.followup.send("Leaderboard aktualisiert.", ephemeral=True)


@bot.tree.command(name="remove_me", description="Löscht dich aus dem Leaderboard")
async def remove_me(interaction: discord.Interaction):
    db.delete_player(interaction.user.id)
    await post_or_update_leaderboard()
    await interaction.response.send_message("Du wurdest aus dem Leaderboard entfernt.", ephemeral=True)


async def update_all_players():
    if riot is None:
        return

    for row in db.all_players():
        try:
            current = await riot.get_current_soloq(row["game_name"], row["tag_line"])
            db.update_current(row["discord_id"], current)
        except Exception as e:
            db.update_current(row["discord_id"], None, error=str(e))


async def post_or_update_leaderboard():
    if not LEADERBOARD_CHANNEL_ID:
        return

    channel = bot.get_channel(LEADERBOARD_CHANNEL_ID)
    if channel is None:
        return

    rows = db.all_players()
    embed = build_leaderboard_embed(rows)

    async for msg in channel.history(limit=20):
        if msg.author == bot.user and msg.embeds and msg.embeds[0].title == "LoL Peak Delta Leaderboard":
            await msg.edit(embed=embed)
            return

    await channel.send(embed=embed)


@tasks.loop(minutes=30)
async def auto_update_loop():
    await update_all_players()
    await post_or_update_leaderboard()


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

    try:
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            print(f"Slash commands synced to guild {GUILD_ID}")
        else:
            await bot.tree.sync()
            print("Slash commands synced globally")
    except Exception as e:
        print(f"Command sync failed: {e}")

    if not auto_update_loop.is_running():
        auto_update_loop.start()

    await post_or_update_leaderboard()
    print("Bot is ready")


if not DISCORD_TOKEN or not RIOT_API_KEY:
    raise RuntimeError("Bitte DISCORD_TOKEN und RIOT_API_KEY in den Host-Variables setzen.")

bot.run(DISCORD_TOKEN)
