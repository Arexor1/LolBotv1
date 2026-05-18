import os
import re
from dataclasses import dataclass
from typing import Optional, Tuple

import aiohttp
import discord
import psycopg2
import psycopg2.extras
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
DATABASE_URL = os.getenv("DATABASE_URL") or os.getenv("DATABASE_PRIVATE_URL")

TIER_BASE = {
    "IRON": 0,
    "BRONZE": 400,
    "SILVER": 800,
    "GOLD": 1200,
    "PLATINUM": 1600,
    "EMERALD": 2000,
    "DIAMOND": 2400,
    "MASTER": 2800,
    "GRANDMASTER": 3200,
    "CHALLENGER": 3600,
}

DIVISION_VALUE = {
    "IV": 0,
    "III": 100,
    "II": 200,
    "I": 300,
}


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
    game_name = game_name.strip()
    tag_line = tag_line.strip()
    if not game_name or not tag_line:
        raise ValueError("Bitte Riot ID im Format `Name#TAG` eingeben, z. B. `Arexor#EUW`.")
    return game_name, tag_line


def score_to_label(score: Optional[int]) -> str:
    if score is None:
        return "Unranked"

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
    def __init__(self, database_url: str):
        self.database_url = database_url
        self._init()

    def connect(self):
        return psycopg2.connect(
            self.database_url,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )

    def _init(self):
        with self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    CREATE TABLE IF NOT EXISTS players (
                        discord_id BIGINT PRIMARY KEY,
                        display_name TEXT NOT NULL,
                        riot_id TEXT NOT NULL,
                        game_name TEXT NOT NULL,
                        tag_line TEXT NOT NULL,
                        opgg_url TEXT NOT NULL,
                        peak_score INTEGER,
                        peak_rank TEXT,
                        current_score INTEGER,
                        current_rank TEXT,
                        delta INTEGER,
                        last_error TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cur.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS peak_rank TEXT")

    def upsert_player(
        self,
        discord_id: int,
        display_name: str,
        riot_id: str,
        game_name: str,
        tag_line: str,
        opgg_url: str,
        current: Optional[RankedEntry],
    ):
        current_score = current.score if current else None
        current_rank = current.label if current else "Unranked"
        peak_score = current_score
        peak_rank = current_rank
        delta = 0 if current_score is not None else None

        with self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO players (
                        discord_id, display_name, riot_id, game_name, tag_line, opgg_url,
                        peak_score, peak_rank, current_score, current_rank, delta, last_error, updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, CURRENT_TIMESTAMP)
                    ON CONFLICT(discord_id) DO UPDATE SET
                        display_name=EXCLUDED.display_name,
                        riot_id=EXCLUDED.riot_id,
                        game_name=EXCLUDED.game_name,
                        tag_line=EXCLUDED.tag_line,
                        opgg_url=EXCLUDED.opgg_url,
                        current_score=EXCLUDED.current_score,
                        current_rank=EXCLUDED.current_rank,
                        peak_score=CASE
                            WHEN players.peak_score IS NULL THEN EXCLUDED.current_score
                            WHEN EXCLUDED.current_score IS NULL THEN players.peak_score
                            WHEN EXCLUDED.current_score > players.peak_score THEN EXCLUDED.current_score
                            ELSE players.peak_score
                        END,
                        peak_rank=CASE
                            WHEN players.peak_score IS NULL THEN EXCLUDED.current_rank
                            WHEN EXCLUDED.current_score IS NULL THEN players.peak_rank
                            WHEN EXCLUDED.current_score > players.peak_score THEN EXCLUDED.current_rank
                            ELSE players.peak_rank
                        END,
                        delta=CASE
                            WHEN EXCLUDED.current_score IS NULL THEN NULL
                            ELSE EXCLUDED.current_score - (
                                CASE
                                    WHEN players.peak_score IS NULL THEN EXCLUDED.current_score
                                    WHEN EXCLUDED.current_score > players.peak_score THEN EXCLUDED.current_score
                                    ELSE players.peak_score
                                END
                            )
                        END,
                        last_error=NULL,
                        updated_at=CURRENT_TIMESTAMP
                    """,
                    (
                        discord_id,
                        display_name,
                        riot_id,
                        game_name,
                        tag_line,
                        opgg_url,
                        peak_score,
                        peak_rank,
                        current_score,
                        current_rank,
                        delta,
                    ),
                )

    def update_current(self, discord_id: int, current: Optional[RankedEntry], error: Optional[str] = None):
        with self.connect() as db:
            with db.cursor() as cur:
                if error:
                    cur.execute(
                        "UPDATE players SET last_error=%s, updated_at=CURRENT_TIMESTAMP WHERE discord_id=%s",
                        (error, discord_id),
                    )
                    return

                current_score = current.score if current else None
                current_rank = current.label if current else "Unranked"

                cur.execute(
                    """
                    UPDATE players
                    SET current_score=%s,
                        current_rank=%s,
                        peak_score=CASE
                            WHEN peak_score IS NULL THEN %s
                            WHEN %s IS NULL THEN peak_score
                            WHEN %s > peak_score THEN %s
                            ELSE peak_score
                        END,
                        peak_rank=CASE
                            WHEN peak_score IS NULL THEN %s
                            WHEN %s IS NULL THEN peak_rank
                            WHEN %s > peak_score THEN %s
                            ELSE peak_rank
                        END,
                        delta=CASE
                            WHEN %s IS NULL THEN NULL
                            ELSE %s - (
                                CASE
                                    WHEN peak_score IS NULL THEN %s
                                    WHEN %s > peak_score THEN %s
                                    ELSE peak_score
                                END
                            )
                        END,
                        last_error=NULL,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE discord_id=%s
                    """,
                    (
                        current_score,
                        current_rank,
                        current_score,
                        current_score,
                        current_score,
                        current_score,
                        current_rank,
                        current_score,
                        current_score,
                        current_rank,
                        current_score,
                        current_score,
                        current_score,
                        current_score,
                        current_score,
                        discord_id,
                    ),
                )

    def all_players(self):
        with self.connect() as db:
            with db.cursor() as cur:
                cur.execute(
                    """
                    SELECT *
                    FROM players
                    ORDER BY
                        CASE WHEN delta IS NULL THEN 1 ELSE 0 END,
                        delta DESC,
                        display_name ASC
                    """
                )
                return cur.fetchall()

    def delete_player(self, discord_id: int):
        with self.connect() as db:
            with db.cursor() as cur:
                cur.execute("DELETE FROM players WHERE discord_id=%s", (discord_id,))


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
        url = (
            f"https://{REGIONAL}.api.riotgames.com/riot/account/v1/accounts/by-riot-id/"
            f"{game_name}/{tag_line}"
        )
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
        description="**Name | Delta from peak elo | Peak elo**",
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

        peak_text = row.get("peak_rank") or score_to_label(row.get("peak_score"))
        opgg_url = row["opgg_url"]
        line = f"**{idx}. {row['display_name']}** | `{delta_text}` | Peak: `{peak_text}` | [OP.GG]({opgg_url})"
        lines.append(line)

    embed.add_field(name="Leaderboard", value="\n".join(lines[:30]), inline=False)
    embed.set_footer(text="Peak elo is tracked automatically. Auto-update every 30 min.")
    return embed


intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL fehlt. Füge in Railway eine PostgreSQL Database hinzu.")

db = Database(DATABASE_URL)
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

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        if riot is None:
            await interaction.followup.send("RIOT_API_KEY fehlt.", ephemeral=True)
            return

        try:
            game_name, tag_line = parse_riot_id(str(self.riot_id.value))
            opgg_url = str(self.opgg.value).strip() or make_opgg_url(game_name, tag_line)

            if not opgg_url.startswith("https://"):
                raise ValueError("OP.GG Link muss mit `https://` anfangen.")

            current = await riot.get_current_soloq(game_name, tag_line)

            db.upsert_player(
                discord_id=interaction.user.id,
                display_name=interaction.user.display_name,
                riot_id=f"{game_name}#{tag_line}",
                game_name=game_name,
                tag_line=tag_line,
                opgg_url=opgg_url,
                current=current,
            )

            await interaction.followup.send(
                f"Gespeichert: `{game_name}#{tag_line}` | Current: `{current.label if current else 'Unranked'}` | Peak wird automatisch getrackt.",
                ephemeral=True,
            )
            await post_or_update_leaderboard()

        except Exception as e:
            await interaction.followup.send(f"Fehler: {e}", ephemeral=True)


@bot.tree.command(name="register", description="Privates Formular: Riot ID und OP.GG eintragen")
async def register(interaction: discord.Interaction):
    await interaction.response.send_modal(RegisterModal())


@bot.tree.command(name="leaderboard", description="Zeigt das Peak-Delta Leaderboard")
async def leaderboard(interaction: discord.Interaction):
    rows = db.all_players()
    await interaction.response.send_message(embed=build_leaderboard_embed(rows))


@bot.tree.command(name="updateelo", description="Aktualisiert alle aktuellen SoloQ Elos")
@app_commands.checks.has_permissions(administrator=True)
async def updateelo(interaction: discord.Interaction):
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
