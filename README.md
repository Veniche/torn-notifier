# Torn travel notifier

DMs you on Discord a few seconds before your flight lands abroad in
Torn. Polls the Torn API on an interval, then schedules one precise
alert per trip instead of polling tightly near landing time. Return
flights to Torn don't trigger an alert.

## 1. Create the Discord bot

1. https://discord.com/developers/applications → **New Application**.
2. **Bot** tab → **Add Bot** → copy the token → put it in `.env` as `DISCORD_BOT_TOKEN`.
3. No privileged intents needed — this bot only sends DMs, it doesn't read messages.
4. **OAuth2 → URL Generator** → scope `bot`, no permissions required → open the
   generated URL and invite it to your private server (a bot must share a
   server with you before it can DM you).

## 2. Get your Discord user ID

User Settings → **Advanced** → enable **Developer Mode** → right-click your own
name anywhere → **Copy User ID** → put it in `.env` as `DISCORD_USER_ID`.

## 3. Get a Torn API key

torn.com → **Settings → API** → create a new key with **Minimal Access**
(enough for the `travel` selection) → put it in `.env` as `TORN_API_KEY`.
If the bot logs an access-level error, check the key's level on that page.

## 4. Run it

Create a `.env` file in this folder:

```
DISCORD_BOT_TOKEN=your-bot-token
DISCORD_USER_ID=your-user-id
TORN_API_KEY=your-api-key
# optional
# ALERT_LEAD_SECONDS=20
# POLL_INTERVAL_SECONDS=60
```

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python torn_travel_notifier.py
```

Start a trip in-game and confirm the DM arrives near landing before
moving to step 5.

## 5. Keep it running (systemd)

```bash
# edit torn-notifier.service first — replace every USERNAME with your
# actual user, and make sure the paths match where you cloned this repo
sudo cp torn-notifier.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now torn-notifier
sudo systemctl status torn-notifier   # confirm it's active
journalctl -u torn-notifier -f        # tail logs
```

## Tuning

- `ALERT_LEAD_SECONDS` — how many seconds before landing the DM fires (default 20).
- `POLL_INTERVAL_SECONDS` — how often it checks travel status (default 60).
  Lower this (e.g. to 15–20) if you take short domestic trips, since a
  60s poll can miss detecting a trip that's already almost over.

Set these in `.env`, then restart the service
(`sudo systemctl restart torn-notifier`).

Keep `.env` out of git; it's listed in `.gitignore`.
