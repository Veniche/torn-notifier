"""
Foreign stock report for plushies and flowers.

Stock comes from YATA's public travel export (crowd-sourced, refreshed by
players' scripts every few minutes). YATA only gives the current quantity,
so we keep our own snapshots to estimate how fast each item sells out and
predict what will be left when you land.
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
USER_AGENT = "torn-notifier (personal bot; github.com/Veniche/torn-notifier)"

ITEM_TYPES = {"Plushie", "Flower"}

# How long to keep snapshots, and how much of them the sell-rate uses.
HISTORY_SECONDS = 3 * 3600
RATE_WINDOW_SECONDS = 90 * 60
# Need at least this much history before trusting a sell-rate.
MIN_RATE_SPAN_SECONDS = 20 * 60

ITEMS_CACHE_SECONDS = 3600

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


def money(n: float) -> str:
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 1_000_000:
        return f"{sign}${n / 1_000_000:.1f}m"
    if n >= 1_000:
        return f"{sign}${n / 1_000:.0f}k"
    return f"{sign}${n:.0f}"


class StockTracker:
    def __init__(self, state_path: str, api_key: str, capacity: int) -> None:
        self.state_path = state_path
        self.api_key = api_key
        self.capacity = capacity
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

    def _save(self) -> None:
        tmp = self.state_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"history": self.history, "flight_seconds": self.flight_seconds,
                       "method": self.method}, f)
        os.replace(tmp, self.state_path)

    # --- inputs ----------------------------------------------------------

    def note_travel(self, travel: dict) -> None:
        """Learn travel method and real flight times from the travel selection."""
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

    async def refresh(self, session: aiohttp.ClientSession) -> None:
        """Pull YATA stock (and item values when stale) and record a snapshot."""
        headers = {"User-Agent": USER_AGENT}
        async with session.get(YATA_URL, headers=headers, timeout=20) as resp:
            data = await resp.json(content_type=None)
        self.latest = data.get("stocks", {})

        cutoff = int(time.time()) - HISTORY_SECONDS
        for code, country in self.latest.items():
            ts = country.get("update", 0)
            for item in country.get("stocks", []):
                points = self.history.setdefault(f"{code}:{item['id']}", [])
                if not points or points[-1][0] != ts:
                    points.append([ts, item["quantity"]])
        for key in list(self.history):
            self.history[key] = [p for p in self.history[key] if p[0] >= cutoff]
            if not self.history[key]:
                del self.history[key]
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

        if time.time() - self.items_fetched > ITEMS_CACHE_SECONDS:
            params = {"selections": "items", "key": self.api_key}
            async with session.get(TORN_ITEMS_URL, params=params, timeout=30) as resp:
                items = (await resp.json()).get("items", {})
            if items:
                self.items = {int(k): v for k, v in items.items() if v.get("type") in ITEM_TYPES}
                self.items_fetched = time.time()

    # --- prediction ------------------------------------------------------

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
                margin = info["market_value"] - item["cost"]
                if margin <= 0:
                    continue
                rate = self.sell_rate(f"{code}:{item['id']}")
                at_landing = None
                if rate is not None:
                    at_landing = max(int(item["quantity"] - rate * (elapsed + flight)), 0)
                expected = item["quantity"] if at_landing is None else at_landing
                buy = min(self.capacity, expected)
                per_trip = buy * margin
                per_hour = int(per_trip / (2 * flight / 3600))
                groups[group].append(Row(f"{flag} {name}", item["name"], item["quantity"],
                                         at_landing, buy, per_trip, per_hour, buy * item["cost"]))
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
            lines, gone = [], []
            for r in groups[group]:
                if r.now == 0 or r.at_landing == 0:
                    gone.append(f"{r.item} ({r.country.split(' ', 1)[1]})")
                    continue
                if r.at_landing is None:
                    status = f"{r.now:,} now · no trend yet"
                elif r.at_landing < self.capacity:
                    status = f"⚠️ {r.now:,} now → ~{r.at_landing:,} at landing"
                else:
                    status = f"✅ {r.now:,} now → ~{r.at_landing:,} at landing"
                line = (f"**{r.item}** · {r.country}\n{status} · "
                        f"{money(r.per_trip)}/trip · **{money(r.per_hour)}/hr**")
                if self.cash is not None and self.cash < r.load_cost:
                    line += f"\n💸 costs {money(r.load_cost)} — bring {money(r.load_cost - self.cash)} more cash"
                lines.append(line)
            if gone:
                lines.append(f"❌ Out / sold out by landing: {', '.join(gone)}")
            embeds.append({"title": title, "description": "\n".join(lines) or "Nothing profitable."})
        cash = "cash unknown" if self.cash is None else f"{money(self.cash)} on hand"
        embeds[-1]["footer"] = {
            "text": f"{self.method} · {self.capacity} items/trip · {cash} · "
                    f"stock data up to {age_min} min old (YATA)"
        }
        return embeds
