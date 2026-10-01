"""
Foreign stock report: every item abroad you can afford a load of.

Stock comes from YATA's public travel export (crowd-sourced, refreshed by
players' scripts every few minutes). YATA only gives the current quantity,
so we keep our own snapshots to estimate how fast each item sells out,
predict what will be left when you land, and learn how long items stay
empty before restocking. Each item is valued at the best way to sell it:
a TornExchange trader's buy price, or the item market (lowest listing minus
your undercut, minus Torn's sales fee).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass

import aiohttp

log = logging.getLogger("torn-notifier")

YATA_URL = "https://yata.yt/api/v1/travel/export/"
TORN_ITEMS_URL = "https://api.torn.com/torn/"
TORN_USER_URL = "https://api.torn.com/user/"
TE_PRICES_URL = "https://tornexchange.com/api/prices/{trader}"
TORN_LISTINGS_URL = "https://api.torn.com/v2/market/{item_id}/itemmarket"
USER_AGENT = "torn-notifier (personal bot; github.com/Veniche/torn-notifier)"

TYPE_ICONS = {"Plushie": "🧸 ", "Flower": "🌸 "}

# Items shown per trip-length group; keeps the report under Discord's
# 6,000-character limit per message.
TOP_PER_GROUP = 8

# How long to keep snapshots, and how much of them the sell-rate uses.
HISTORY_SECONDS = 3 * 3600
RATE_WINDOW_SECONDS = 90 * 60
# Need at least this much history before trusting a sell-rate.
MIN_RATE_SPAN_SECONDS = 20 * 60

ITEMS_CACHE_SECONDS = 3600
# TE caches trader price lists for 5 minutes server-side.
TE_CACHE_SECONDS = 30 * 60

# Lowest item-market listings are one API call per item, so they're
# refreshed on rotation: this many per stock refresh, each kept this long.
LISTINGS_PER_REFRESH = 20
LISTING_CACHE_SECONDS = 30 * 60

# Restock cycles (sold out -> restocked) kept for estimating restock delays.
CYCLE_HISTORY_SECONDS = 7 * 24 * 3600

# Items listed per country in /restock.
TOP_RESTOCK = 10

# YATA country code -> (name, flag, distance group)
COUNTRIES = {
    "mex": ("Mexico", "🇲🇽", "short"),
    "cay": ("Cayman Islands", "🇰🇾", "short"),
    "can": ("Canada", "🇨🇦", "short"),
    "haw": ("Hawaii", "🌺", "medium"),
    "uni": ("United Kingdom", "🇬🇧", "medium"),
    "arg": ("Argentina", "🇦🇷", "medium"),
    "swi": ("Switzerland", "🇨🇭", "medium"),
    "jap": ("Japan", "🇯🇵", "long"),
    "chi": ("China", "🇨🇳", "long"),
    "uae": ("UAE", "🇦🇪", "long"),
    "sou": ("South Africa", "🇿🇦", "long"),
}
CODE_BY_NAME = {name: code for code, (name, _, _) in COUNTRIES.items()}

GROUPS = [("short", "✈️ Short trips"), ("medium", "✈️ Medium trips"), ("long", "✈️ Long trips")]

# One-way flight minutes by travel method. Used until we've seen a real
# trip to that country with that method (perks can shave a few minutes).
BASE_MINUTES = {
    "Standard": {"mex": 26, "cay": 35, "can": 41, "haw": 134, "uni": 159, "arg": 167,
                 "swi": 175, "jap": 225, "chi": 242, "uae": 271, "sou": 297},
    "Airstrip": {"mex": 18, "cay": 25, "can": 29, "haw": 94, "uni": 111, "arg": 117,
                 "swi": 123, "jap": 158, "chi": 169, "uae": 190, "sou": 208},
    "Private": {"mex": 13, "cay": 18, "can": 20, "haw": 67, "uni": 80, "arg": 83,
                "swi": 88, "jap": 113, "chi": 121, "uae": 135, "sou": 149},
    "Business": {"mex": 8, "cay": 11, "can": 12, "haw": 40, "uni": 48, "arg": 50,
                 "swi": 53, "jap": 68, "chi": 72, "uae": 81, "sou": 89},
}


@dataclass
class Row:
    country: str
    item: str
    now: int
    at_landing: int | None  # None = not enough history to predict
    buy: int
    per_trip: int
    per_hour: int
    load_cost: int  # cost of buying `buy` units
    want: int  # units you'd buy with full stock (capacity, capped by budget)
    potential_hour: int  # profit/hr if `want` units were in stock
    key: str  # "country:item_id"
    sell_via: str  # where to sell for the best net price


def duration(seconds: float) -> str:
    minutes = max(int(seconds // 60), 0)
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def money(n: float) -> str:
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 1_000_000:
        return f"{sign}${n / 1_000_000:.1f}m"
    if n >= 1_000:
        return f"{sign}${n / 1_000:.0f}k"
    return f"{sign}${n:.0f}"


class StockTracker:
    def __init__(self, state_path: str, api_key: str, capacity: int, budget: int | None,
                 te_key: str | None = None, te_traders: list[str] | None = None,
                 market_undercut: int = 0, market_fee: float = 0.05) -> None:
        self.state_path = state_path
        self.api_key = api_key
        self.capacity = capacity
        self.budget = budget
        self.te_key = te_key
        self.te_traders = te_traders or []
        # trader -> {item_id: buy price}; a trader is missing until fetched
        self.te_prices: dict[str, dict[int, int]] = {}
        self.te_fetched = 0.0
        self.market_undercut = market_undercut
        self.market_fee = market_fee
        # item_id -> [lowest item-market listing, fetched_at]
        self.listings: dict[int, list[int]] = {}
        # "country:item_id" -> [[restocked_at, seconds_empty, restock_qty], ...]
        self.cycles: dict[str, list[list[int]]] = {}
        # "country:item_id" -> when it was seen selling out (still empty)
        self.empty_since: dict[str, int] = {}
        # Latest travel selection, to know where you are for /restock.
        self.travel: dict = {}
        # "country:item_id" -> [[yata_update_ts, quantity], ...]
        self.history: dict[str, list[list[int]]] = {}
        # "Method:country" -> observed one-way seconds
        self.flight_seconds: dict[str, int] = {}
        self.method = "Standard"
        # Cash on hand at the last refresh; None if the key can't read it.
        self.cash: int | None = None
        self.latest: dict = {}
        self.items: dict[int, dict] = {}
        self.items_fetched = 0.0
        self._load()

    # --- persistence -----------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self.state_path) as f:
                state = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.error("Couldn't read %s, starting fresh: %s", self.state_path, exc)
            return
        self.history = state.get("history", {})
        self.flight_seconds = state.get("flight_seconds", {})
        self.method = state.get("method", self.method)
        self.cycles = state.get("cycles", {})
        self.empty_since = state.get("empty_since", {})

    def _save(self) -> None:
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"history": self.history, "flight_seconds": self.flight_seconds,
                       "method": self.method, "cycles": self.cycles,
                       "empty_since": self.empty_since}, f)
        os.replace(tmp, self.state_path)

    # --- inputs ----------------------------------------------------------

    def note_travel(self, travel: dict) -> None:
        """Learn travel method and real flight times from the travel selection."""
        self.travel = travel
        method = travel.get("method")
        if method in BASE_MINUTES and method != self.method:
            self.method = method
            self._save()
        code = CODE_BY_NAME.get(travel.get("destination", ""))
        departed, arrival = travel.get("departed"), travel.get("timestamp")
        if code and method and departed and arrival and arrival > departed:
            key = f"{method}:{code}"
            if self.flight_seconds.get(key) != arrival - departed:
                self.flight_seconds[key] = arrival - departed
                self._save()

    async def refresh(self, session: aiohttp.ClientSession, listings: bool = False) -> None:
        """Pull YATA stock (and item values when stale) and record a snapshot.

        `listings` also rotates item-market listing checks; only the
        background loop asks for that, so commands stay quick.
        """
        headers = {"User-Agent": USER_AGENT}
        async with session.get(YATA_URL, headers=headers, timeout=20) as resp:
            data = await resp.json(content_type=None)
        self.latest = data.get("stocks", {})

        now = int(time.time())
        cutoff = now - HISTORY_SECONDS
        for code, country in self.latest.items():
            ts = country.get("update", 0)
            for item in country.get("stocks", []):
                key = f"{code}:{item['id']}"
                points = self.history.setdefault(key, [])
                if not points or points[-1][0] != ts:
                    if points:
                        self._note_cycle(key, points[-1], [ts, item["quantity"]])
                    points.append([ts, item["quantity"]])
        for key in list(self.history):
            self.history[key] = [p for p in self.history[key] if p[0] >= cutoff]
            if not self.history[key]:
                del self.history[key]
        for key in list(self.cycles):
            self.cycles[key] = [c for c in self.cycles[key] if c[0] >= now - CYCLE_HISTORY_SECONDS]
            if not self.cycles[key]:
                del self.cycles[key]
        self._save()

        # Only cash on hand counts: vault and bank money can't be reached abroad.
        # A failure here shouldn't block the rest of the refresh.
        try:
            params = {"selections": "money", "key": self.api_key}
            async with session.get(TORN_USER_URL, params=params, timeout=15) as resp:
                self.cash = (await resp.json()).get("money_onhand")
        except Exception as exc:
            log.error("Cash check failed: %s", exc)
            self.cash = None

        if self.te_key and self.te_traders and time.time() - self.te_fetched > TE_CACHE_SECONDS:
            for trader in self.te_traders:
                try:
                    await self._fetch_te_prices(session, trader)
                except Exception as exc:  # keep using the last list, or the market
                    log.error("TornExchange prices for %s failed: %s", trader, exc)
            self.te_fetched = time.time()

        if time.time() - self.items_fetched > ITEMS_CACHE_SECONDS:
            params = {"selections": "items", "key": self.api_key}
            async with session.get(TORN_ITEMS_URL, params=params, timeout=30) as resp:
                items = (await resp.json()).get("items", {})
            if items:
                self.items = {int(k): v for k, v in items.items() if v.get("market_value")}
                self.items_fetched = time.time()

        if listings:
            await self._refresh_listings(session)

    async def _refresh_listings(self, session: aiohttp.ClientSession) -> None:
        """Fetch lowest listings for the stalest items you could buy abroad."""
        now = time.time()
        # Only items that could make a report: affordable and profitable by
        # some method. Value each by what a full load would earn.
        load_value: dict[int, int] = {}
        for country in self.latest.values():
            for item in country.get("stocks", []):
                info = self.items.get(item["id"])
                if not info or not item["cost"]:
                    continue
                want = self.capacity if self.budget is None else min(self.capacity, self.budget // item["cost"])
                best = max([info["market_value"]] + [p[item["id"]] for p in self.te_prices.values()
                                                     if item["id"] in p])
                if want and best > item["cost"]:
                    load_value[item["id"]] = max(load_value.get(item["id"], 0), want * (best - item["cost"]))
        # Stalest first, and among equally stale, the most lucrative.
        stale = sorted((self.listings.get(i, [0, 0])[1], -v, i) for i, v in load_value.items()
                       if now - self.listings.get(i, [0, 0])[1] > LISTING_CACHE_SECONDS)
        for _, _, item_id in stale[:LISTINGS_PER_REFRESH]:
            try:
                await self.fetch_listing(session, item_id)
            except Exception as exc:
                log.error("Listing fetch for item %s failed: %s", item_id, exc)
                break  # likely rate-limited or offline; try again next refresh

    async def fetch_listing(self, session: aiohttp.ClientSession, item_id: int) -> None:
        url = TORN_LISTINGS_URL.format(item_id=item_id)
        params = {"limit": 1, "key": self.api_key}
        async with session.get(url, params=params, timeout=15) as resp:
            data = await resp.json()
        if "error" in data:
            raise ValueError(data["error"])
        listings = (data.get("itemmarket") or {}).get("listings") or []
        if listings:
            self.listings[item_id] = [int(listings[0]["price"]), int(time.time())]

    async def _fetch_te_prices(self, session: aiohttp.ClientSession, trader: str) -> None:
        url = TE_PRICES_URL.format(trader=trader)
        params = {"key": self.te_key}
        headers = {"User-Agent": USER_AGENT}
        async with session.get(url, params=params, headers=headers, timeout=20) as resp:
            data = await resp.json(content_type=None)
        items = (data.get("data") or {}).get("items")
        if data.get("status") != "success" or not items:
            raise ValueError(f"unexpected response: {str(data)[:200]}")
        self.te_prices[trader] = {int(i["item_id"]): int(i["price"]) for i in items if i.get("price")}
        log.info("Loaded %d buy prices from TE trader %s", len(self.te_prices[trader]), trader)

    def _note_cycle(self, key: str, prev: list[int], cur: list[int]) -> None:
        """Record sell-outs and restocks between two consecutive snapshots."""
        # The change happened somewhere between the two snapshots.
        mid = (prev[0] + cur[0]) // 2
        if prev[1] > 0 and cur[1] == 0:
            self.empty_since[key] = mid
        elif prev[1] == 0 and cur[1] > 0:
            started = self.empty_since.pop(key, None)
            if started is not None:
                self.cycles.setdefault(key, []).append([mid, mid - started, cur[1]])

    # --- prediction ------------------------------------------------------

    def sale_options(self, item_id: int) -> list[tuple[int, str, str]]:
        """Every way to sell one unit, best first: (net price, where, how it's worked out)."""
        options = [(prices[item_id], trader, "TornExchange buy price, no fee")
                   for trader, prices in self.te_prices.items() if item_id in prices]
        listing = self.listings.get(item_id)
        if listing:
            base, basis = listing[0], f"lowest listing ${listing[0]:,}"
        else:  # not fetched yet; Torn's average sale price is the best guess
            base, basis = self.items[item_id]["market_value"], "market value (listing not checked yet)"
        net = int((base - self.market_undercut) * (1 - self.market_fee))
        options.append((net, "item market",
                        f"{basis} − ${self.market_undercut:,} undercut − {self.market_fee:.0%} fee"))
        return sorted(options, reverse=True)

    def best_sale(self, item_id: int) -> tuple[int, str]:
        net, where, _ = self.sale_options(item_id)[0]
        return net, where

    def restock_estimate(self, key: str) -> tuple[int | None, int, int | None]:
        """(seconds until restock or None, cycles seen, typical restock qty)."""
        cycles = self.cycles.get(key, [])
        if not cycles:
            return None, 0, None
        delays = sorted(c[1] for c in cycles)
        typical_delay = delays[len(delays) // 2]
        typical_qty = sorted(c[2] for c in cycles)[len(cycles) // 2]
        started = self.empty_since.get(key)
        if started is None:
            return None, len(cycles), typical_qty
        return started + typical_delay - int(time.time()), len(cycles), typical_qty

    def one_way_seconds(self, code: str) -> int:
        observed = self.flight_seconds.get(f"{self.method}:{code}")
        return observed or BASE_MINUTES[self.method][code] * 60

    def sell_rate(self, key: str) -> float | None:
        """Units sold per second over the recent window, ignoring restocks."""
        points = self.history.get(key, [])
        cutoff = int(time.time()) - RATE_WINDOW_SECONDS
        points = [p for p in points if p[0] >= cutoff]
        if len(points) < 2 or points[-1][0] - points[0][0] < MIN_RATE_SPAN_SECONDS:
            return None
        sold = sum(max(a[1] - b[1], 0) for a, b in zip(points, points[1:]))
        return sold / (points[-1][0] - points[0][0])

    def rows(self) -> dict[str, list[Row]]:
        groups: dict[str, list[Row]] = {g: [] for g, _ in GROUPS}
        for code, country in self.latest.items():
            if code not in COUNTRIES:
                continue
            name, flag, group = COUNTRIES[code]
            flight = self.one_way_seconds(code)
            # Wherever you are, you'd land abroad roughly one flight from now.
            elapsed = int(time.time()) - country.get("update", int(time.time()))
            for item in country.get("stocks", []):
                info = self.items.get(item["id"])
                if not info:
                    continue
                price, sell_via = self.best_sale(item["id"])
                margin = price - item["cost"]
                want = self.capacity
                if self.budget is not None and item["cost"] > 0:
                    want = min(want, self.budget // item["cost"])
                if margin <= 0 or want == 0:
                    continue
                rate = self.sell_rate(f"{code}:{item['id']}")
                at_landing = None
                if rate is not None:
                    at_landing = max(int(item["quantity"] - rate * (elapsed + flight)), 0)
                expected = item["quantity"] if at_landing is None else at_landing
                buy = min(want, expected)
                round_trip_hours = 2 * flight / 3600
                per_trip = buy * margin
                groups[group].append(Row(
                    f"{flag} {name}", TYPE_ICONS.get(info["type"], "") + item["name"],
                    item["quantity"], at_landing, buy, per_trip, int(per_trip / round_trip_hours),
                    buy * item["cost"], want, int(want * margin / round_trip_hours),
                    f"{code}:{item['id']}", sell_via,
                ))
        for rows in groups.values():
            rows.sort(key=lambda r: r.per_hour, reverse=True)
        return groups

    def report_embeds(self) -> list[dict]:
        if not self.latest or not self.items:
            return [{"title": "Travel stock", "description": "No stock data yet — try again in a minute."}]
        oldest = min(c.get("update", 0) for c in self.latest.values())
        age_min = max(int((time.time() - oldest) / 60), 0)
        groups = self.rows()
        embeds = []
        for group, title in GROUPS:
            rows = groups[group]
            shown = [r for r in rows if r.buy > 0][:TOP_PER_GROUP]
            lines = []
            for r in shown:
                if r.at_landing is None:
                    status = f"{r.now:,} now · no trend yet"
                elif r.at_landing < r.want:
                    status = f"⚠️ {r.now:,} now → ~{r.at_landing:,} at landing"
                else:
                    status = f"✅ {r.now:,} now → ~{r.at_landing:,} at landing"
                line = (f"**{r.item}** · {r.country} · {self._via(r.sell_via)}\n{status} · "
                        f"{money(r.per_trip)}/trip · **{money(r.per_hour)}/hr**")
                if self.cash is not None and self.cash < r.load_cost:
                    line += f"\n💸 costs {money(r.load_cost)} — bring {money(r.load_cost - self.cash)} more cash"
                lines.append(line)
            # Out-of-stock items worth knowing about: ones that would have made
            # the list if they were in stock.
            cutoff = shown[-1].per_hour if len(shown) == TOP_PER_GROUP else 0
            gone = sorted((r for r in rows if r.buy == 0 and r.potential_hour > cutoff),
                          key=lambda r: r.potential_hour, reverse=True)[:5]
            if gone:
                names = ", ".join(f"{r.item} ({r.country.split(' ', 1)[1]}{self._restock_hint(r)})"
                                  for r in gone)
                lines.append(f"❌ Out / sold out by landing: {names}")
            embeds.append({"title": title, "description": "\n".join(lines) or "Nothing profitable."})
        embeds[-1]["footer"] = {"text": self._footer(age_min)}
        return embeds

    def _restock_hint(self, r: Row) -> str:
        if r.now > 0:
            return ""
        eta, _, _ = self.restock_estimate(r.key)
        if eta is None:
            return ""
        return ", restock due now" if eta <= 0 else f", restock ~{duration(eta)}"

    @staticmethod
    def _via(where: str) -> str:
        return "🏪 market" if where == "item market" else f"🤝 {where}"

    def _footer(self, age_min: int) -> str:
        cash = "cash unknown" if self.cash is None else f"{money(self.cash)} on hand"
        traders = len(self.te_prices)
        prices = (f"net of {self.market_fee:.0%} market fee"
                  + (f" vs {traders} trader{'s' if traders != 1 else ''}" if traders else ""))
        return (f"{self.method} · {self.capacity} items/trip · "
                f"{'no budget cap' if self.budget is None else money(self.budget) + ' budget'} · "
                f"{cash} · {prices} · stock data up to {age_min} min old (YATA)")

    def location_code(self) -> str | None:
        """Country you're in or flying to, from the last travel poll."""
        return CODE_BY_NAME.get(self.travel.get("destination", ""))

    def restock_embed(self, code: str) -> dict:
        name, flag, _ = COUNTRIES[code]
        country = self.latest.get(code)
        if not country or not self.items:
            return {"title": f"{flag} {name}", "description": "No stock data yet — try again in a minute."}
        rows = [r for r in self.rows()[COUNTRIES[code][2]] if r.key.startswith(code + ":")]
        # Rank by what a full load would earn, so empty items still show.
        rows.sort(key=lambda r: r.potential_hour, reverse=True)
        lines = []
        for r in rows[:TOP_RESTOCK]:
            eta, seen, qty = self.restock_estimate(r.key)
            if r.now > 0:
                rate = self.sell_rate(r.key)
                if rate:
                    status = f"✅ {r.now:,} now · sells out in ~{duration(r.now / rate)}"
                else:
                    status = f"✅ {r.now:,} now · no trend yet"
            elif eta is None:
                status = "❌ out · no restock history yet" if seen == 0 else \
                    f"❌ out · sold out before tracking started ({seen} cycles seen)"
            elif eta <= 0:
                status = f"❌ out · restock due any minute (overdue {duration(-eta)})"
            else:
                status = f"❌ out · restock in ~{duration(eta)}"
            if r.now == 0 and qty:
                status += f" · usually +{qty:,} · {seen} cycle{'s' if seen != 1 else ''}"
            lines.append(f"**{r.item}** · {money(r.potential_hour)}/hr at full load · "
                         f"{self._via(r.sell_via)}\n{status}")
        age_min = max(int((time.time() - country.get("update", 0)) / 60), 0)
        return {"title": f"{flag} {name} — restock watch",
                "description": "\n".join(lines) or "Nothing profitable here.",
                "footer": {"text": self._footer(age_min)}}

    def item_names(self) -> dict[str, int]:
        return {info["name"]: item_id for item_id, info in self.items.items()}

    def sell_embed(self, item_id: int, qty: int) -> dict:
        name = self.items[item_id]["name"]
        options = self.sale_options(item_id)
        best = options[0][0]
        lines = []
        for rank, (net, where, how) in enumerate(options):
            medal = "🥇" if rank == 0 else "▫️"
            gap = "" if rank == 0 else f" (−${(best - net) * qty:,} vs best)"
            lines.append(f"{medal} **{self._via(where)}** — ${net:,} each · "
                         f"**${net * qty:,}** for {qty}{gap}\n{how}")
        if not self.te_prices:
            lines.append("_No TornExchange traders configured — only the item market is compared._")
        return {"title": f"Where to sell {name}", "description": "\n".join(lines)}
