# Torn travel notifier

DMs you on Discord a few seconds before your flight lands in Torn,
both abroad and on the way back home, and when your drug cooldown ends.
Polls the Torn API on an interval, then schedules one precise alert per
trip or cooldown instead of polling tightly near the deadline.

When you're about to land back in Torn, it also sends a **foreign stock
report** (best items to buy abroad) for your next trip, and you can pull one any time with the
`/travel` slash command in the bot's DMs. `/restock` shows stock and
estimated restock times for the country you're in (or any country).

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
→ put it in `.env` as `TORN_API_KEY`. The bot reads the `travel` and
`cooldowns` selections; if it logs an access-level error, raise the key to
**Limited Access**. Don't use a Full Access key — the bot never needs it.

## 4. Run it

Create a `.env` file in this folder:

```
DISCORD_BOT_TOKEN=your-bot-token
DISCORD_USER_ID=your-user-id
TORN_API_KEY=your-api-key
# optional
# ALERT_LEAD_SECONDS=30
# TRAVEL_CAPACITY=5
# TRAVEL_BUDGET=500000
# TE_TRADER=SomeTrader
# TE_API_KEY=your-tornexchange-api-key
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

- `ALERT_LEAD_SECONDS` — how many seconds before landing the DM fires (default 30).
  The drug alert fires when the cooldown reaches 0.
- `POLL_INTERVAL_SECONDS` — how often it checks for a new trip (default 60).
  This only affects how soon a trip is detected, not alert accuracy: the
  alert is timed from Torn's arrival timestamp. The default is fine even
  for the shortest flights.

- `TRAVEL_CAPACITY` — how many items you can carry per trip (default 5).
  Set this to your real capacity; profit numbers scale with it.
- `TRAVEL_BUDGET` — the most cash you carry abroad (default: no cap). Items
  costing more than budget ÷ capacity are counted only as far as the budget
  covers, so expensive items drop down the ranking.
- `TE_TRADER` + `TE_API_KEY` — value items at that
  [TornExchange](https://tornexchange.com) trader's buy prices instead of
  Torn's market value. The TE key is on your TornExchange profile. Items the
  trader doesn't buy fall back to market value and are marked 🏪.
- `STOCK_POLL_SECONDS` — how often stock snapshots are taken (default 300).

Set these in `.env`, then restart the service
(`sudo systemctl restart torn-notifier`).

Keep `.env` out of git; it's listed in `.gitignore`.

## Stock report

Grouped by trip length (short: Mexico, Cayman, Canada · medium: Hawaii,
UK, Argentina, Switzerland · long: Japan, China, UAE, South Africa) and
sorted by profit per hour, top 8 per group. It covers every foreign item
that's profitable and fits your budget; plushies are marked 🧸 and flowers
🌸. Out-of-stock items that would have made the list are named at the
bottom of each group. For each item it shows:

- **Stock now → predicted at landing.** Stock comes from
  [YATA](https://yata.yt)'s public travel export, which players' scripts
  keep updated. The bot snapshots it every 5 minutes and uses the last 90
  minutes to estimate how fast each item sells, then projects that over
  your flight. Until it has ~20 minutes of history it shows "no trend yet".
  Restocks aren't predicted: an item that's out now is listed as out.
- **Profit per trip:** (sell price − shop cost) × the items you can
  actually buy (your capacity, capped by your budget and by predicted stock).
- **Profit per hour:** profit per trip ÷ round-trip flight time.
- **💸 Cash check:** if your cash on hand can't cover the full load, the
  item shows how much more to bring. Only cash on hand counts, since you
  can't reach your vault or bank abroad. Needs a key with access to the
  `money` selection; otherwise the footer shows "cash unknown".

Flight times start from Torn's standard table for your travel method and
switch to your real flight times once the bot has seen you fly there.
Snapshots and flight times are kept in `state.json` so restarts don't lose
them. Market value is an average of recent sales, so real sale prices can
be a bit lower. Plushies and flowers sell easily; for less-traded items
(e.g. Raw Ivory, Tiger Bone Powder) check the item market before buying a
full load, because selling 18 at market value may take a while.

## Restock watch (`/restock`)

For the country you're in or flying to (or one you pick), lists the most
profitable items with stock now and either when they'll sell out, or —
if they're out — when they should restock. Restock times are learned from
the bot's own snapshots: each time an item sells out and comes back, the
bot records how long it stayed empty and how much came back, and predicts
from the median of the last 7 days. It needs to see at least one full
cycle per item first, so expect "no restock history yet" for the first day
or so after setting it up. The main report's sold-out list shows the same
restock estimate when there is one.
