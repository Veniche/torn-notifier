# Torn travel notifier

DMs you on Discord a few seconds before you land in Torn. Polls the
Torn API on an interval, then schedules one precise alert per trip
instead of polling tightly near landing time.

## 1. Create the Discord bot

1. https://discord.com/developers/applications → **New Application**.
2. **Bot** tab → **Add Bot** → copy the token → put it in `.env` as `DISCORD_BOT_TOKEN`.
3. No privileged intents needed — this bot only sends DMs, it doesn't read messages.
4. **OAuth2 → URL Generator** → scope `bot`, no permissions required → open the
   generated URL and invite it to your private server (a bot must share a
   server with you before it can DM you).

## 2. Get your Discord user ID

Settings → **Advanced** → enable **Developer Mode** → right-click your own
name anywhere → **Copy User ID** → put it in `.env` as `DISCORD_USER_ID`.

## 3. Get a Torn API key

torn.com → **Settings → API** → create a new key with **Minimal Access**
(that should cover the `travel` selection). If the bot logs an "access
level" error on startup, bump the key to **Limited Access** instead —
put whichever key works in `.env` as `TORN_API_KEY`.

## 4. Run it

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then fill in your values
python torn_travel_notifier.py
```

Start a trip in-game and confirm the DM arrives near landing before
moving to step 5.

## 5. Keep it running (systemd)

```bash
# edit torn-notifier.service first — replace USERNAME with your actual user
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

## Pushing to GitHub

This wasn't pushed for you — no GitHub connection is set up on this
side, and pushing needs your own auth. From this folder:

```bash
git init
git add .
git commit -m "Initial commit: Torn travel notifier"
gh repo create torn-notifier --private --source=. --push
# or, without gh CLI: create the repo on github.com first, then
# git remote add origin <url> && git branch -M main && git push -u origin main
```

`.env` is already git-ignored — double check it's not staged before
your first commit.
