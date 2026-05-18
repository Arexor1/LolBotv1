# LoL Elo Peak Discord Bot

Features:
- `/register` nimmt Riot ID Eingaben als private Slash-Command-Optionen entgegen (ephemeral Antwort).
- Speichert aktuellen SoloQ-Elo als Peak beim Registrieren.
- Trackt Current LP vs. Peak LP.
- Postet/editiert ein Leaderboard im Format: `Name | Delta from peak elo`.

## Setup
1. Python 3.11+ installieren.
2. Discord Bot erstellen, Token in `.env` eintragen.
3. Riot API Key vom Riot Developer Portal in `.env` eintragen.
4. Bot in deinen Server einladen mit Scopes: `bot`, `applications.commands`; Permissions: `Send Messages`, `Read Message History`.
5. Installieren:

```bash
pip install -r requirements.txt
cp .env.example .env
python bot.py
```

## Commands
- `/register game_name:<Name> tag_line:<Tag> display_name:<Name optional>`
- `/unregister`
- `/leaderboard`
- `/refresh`

Hinweis: Normale Riot Development Keys laufen ab und haben Rate Limits. Für dauerhaft öffentlichen Betrieb brauchst du einen Production Key.
