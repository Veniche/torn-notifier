# Torn travel notifier

DMs you on Discord a few seconds before your flight lands in Torn,
both abroad and on the way back home, and when your drug cooldown ends.
Polls the Torn API on an interval, then schedules one precise alert per
trip or cooldown instead of polling tightly near the deadline.

When you're about to land back in Torn, it also sends a **foreign stock
report** (best items to buy abroad) for your next trip. Slash commands in
the bot's DMs give you the same report and more on demand — see
[Commands](#commands).

## Commands

| Command | Alias | What it does |
|---|---|---|
| `/travel` | `/t` | Foreign stock report: best items to buy abroad, grouped by trip length, with stock predicted at landing, profit per trip and per hour, and where to sell each. |
| `/travel-restock [country]` | `/trs` | Stock, sell-out and restock estimates for the country you're in or flying to, or the `country` you pick. |
| `/sell` | — | Every item in the `/travel` report, grouped by where it sells best (🤝 each trader / 🏪 item market), with the net price per unit and how far ahead of the next-best method it is. |
| `/sell item:<name> [qty]` | — | One item in detail: every way to sell it, for `qty` units (default: your travel capacity). Checks its live lowest listing. Item names autocomplete. |

Discord has no real aliases, so each alias is its own entry in the `/`
menu. Commands only answer the user in `DISCORD_USER_ID`; anyone else gets
"This bot is private." After commands are added or renamed, restart Discord
(Ctrl+R / Cmd+R on desktop, fully close the app on mobile) to see them —
until then, typing the name just sends a plain message the bot ignores.

### Automatic DMs

| When | Message |
|---|---|
| `ALERT_LEAD_SECONDS` before any landing | 🛬 Landing in ~30s — *destination* |
| Right after the alert for a landing in Torn | The `/travel` stock report |
| Drug cooldown reaches 0 | 💊 Drug cooldown is over |

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

torn.com → **Settings → API** → create a new key with **Limited Access**
→ put it in `.env` as `TORN_API_KEY`. The bot only reads data: `travel`,
`cooldowns` and `money` (for the cash check), plus item details and
item-market listings. A Minimal key covers travel and cooldowns, but the
cash check then shows "cash unknown". Don't use a Full Access key — the bot
never needs it.

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
# TE_TRADERS=SomeTrader,AnotherTrader
# TE_API_KEY=your-tornexchange-api-key
# ITEM_MARKET_UNDERCUT=10
# ITEM_MARKET_FEE=5
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
- `TE_TRADERS` + `TE_API_KEY` — [TornExchange](https://tornexchange.com)
  traders to compare (comma-separated names; the TE key is on your
  TornExchange profile). Each item is valued at whichever pays most: a
  trader's buy price (🤝) or the item market (🏪).
- `ITEM_MARKET_UNDERCUT` — dollars below the lowest listing you list at
  (default 0).
- `ITEM_MARKET_FEE` — Torn's item market sales fee in percent (default 5,
  in effect since June 2025; lower it if a company special reduces it).
  Item market value = (lowest listing − undercut) × (1 − fee).
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
  Items that are out now are listed as out, with a restock estimate when
  the bot has one (see `/travel-restock`).
- **Where to sell:** 🤝 *trader* or 🏪 market, whichever nets more (see
  `/sell`).
- **Profit per trip:** (best net sale price − shop cost) × the items you can
  actually buy (your capacity, capped by your budget and by predicted stock).
- **Profit per hour:** profit per trip ÷ round-trip flight time.
- **💸 Cash check:** if your cash on hand can't cover the full load, the
  item shows how much more to bring. Only cash on hand counts, since you
  can't reach your vault or bank abroad. Needs a key with access to the
  `money` selection; otherwise the footer shows "cash unknown".

Flight times start from Torn's standard table for your travel method and
switch to your real flight times once the bot has seen you fly there.
Snapshots, flight times, restock cycles and checked listing prices are
kept in `state.json` so restarts don't lose them. Plushies and flowers sell easily; for less-traded items (e.g. Raw
Ivory, Tiger Bone Powder) check the item market before buying a full load
if you plan to list rather than sell to a trader.

## Restock watch (`/travel-restock`)

For the country you're in or flying to (or one you pick), lists the most
profitable items with stock now and either when they'll sell out, or —
if they're out — when they should restock. Restock times are learned from
the bot's own snapshots: each time an item sells out and comes back, the
bot records how long it stayed empty and how much came back, and predicts
from the median of the last 7 days. It needs to see at least one full
cycle per item first, so expect "no restock history yet" for the first day
or so after setting it up. The main report's sold-out list shows the same
restock estimate when there is one.

## Where to sell (`/sell`)

With no item, lists everything in the `/travel` report grouped by best
sale method, per unit (quantity only multiplies, so it doesn't change the
winner). A `*` means that item's market price is still Torn's average
because its lowest listing hasn't been checked yet. With `item`, it ranks
every way to sell that item — each configured trader's buy price and
the item market after your undercut and the sales fee — with the total
for a full load (or `qty`). It checks the live lowest listing for that
item. In the background, the bot also checks lowest listings for the
items most likely to be in your reports, 20 at a time on each stock
refresh (each kept 30 minutes), so the travel report's 🤝/🏪 choice uses
real listings rather than Torn's average price.
