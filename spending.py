"""
Spending: what you'll need to pay over the coming days.

Rent and upkeep come from your properties through the Torn API, so they
never need updating. Everything else (Xanax, other bills) is a manual
entry kept in spending.json: an amount that's either cash ("4m") or items
("5 xanax", priced at the current lowest listing), how often it repeats,
and optionally when it's next due.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from datetime import datetime, timezone
from typing import Callable

import aiohttp

from market import money, parse_amount

log = logging.getLogger("torncierge")

USER_PROPERTIES_URL = "https://api.torn.com/v2/user/properties"
USER_BASIC_URL = "https://api.torn.com/v2/user/basic"
PROPERTIES_CACHE_SECONDS = 30 * 60
DAY = 86400

# Horizon when you rent nothing.
DEFAULT_HORIZON_DAYS = 30


def parse_every(text: str) -> int:
    """'once' -> 0, 'daily' -> 1, 'weekly' -> 7, '50d' / '50' -> 50."""
    t = text.strip().lower()
    named = {"once": 0, "one-off": 0, "daily": 1, "weekly": 7, "monthly": 30}
    if t in named:
        return named[t]
    m = re.fullmatch(r"(\d+)\s*d?", t)
    if not m:
        raise ValueError(text)
    return int(m.group(1))


def parse_due(text: str) -> int:
    """'today' -> 0, 'tomorrow' -> 1, '3d' / '3' -> 3 (days from now)."""
    t = text.strip().lower()
    if t in ("today", "now"):
        return 0
    if t == "tomorrow":
        return 1
    m = re.fullmatch(r"(?:in\s*)?(\d+)\s*d?", t)
    if not m:
        raise ValueError(text)
    return int(m.group(1))


def every_text(days: int) -> str:
    return {0: "once", 1: "daily", 7: "weekly"}.get(days, f"every {days}d")


class Spending:
    def __init__(self, path: str, api_key: str,
                 item_price: Callable[[str], tuple[int, str] | None]) -> None:
        """`item_price(name)` -> (price per unit you'd pay, item's proper name), or None."""
        self.path = path
        self.api_key = api_key
        self.item_price = item_price
        # name -> {"amount": "5 xanax", "every": 7, "due": epoch or None}
        self.entries: dict[str, dict] = {}
        # Rent alerts already sent, as "property_id:lease_end_date:days_left".
        self.alerts_sent: list[str] = []
        self.player_id: int | None = None
        self.properties: list[dict] = []
        self.properties_fetched = 0.0
        self._load()

    # --- persistence -----------------------------------------------------

    def _load(self) -> None:
        try:
            with open(self.path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except (OSError, ValueError) as exc:
            log.error("Couldn't read %s: %s", self.path, exc)
            return
        self.entries = data.get("entries", {})
        self.alerts_sent = data.get("alerts_sent", [])

    def _save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"entries": self.entries, "alerts_sent": self.alerts_sent[-50:]}, f, indent=1)
        os.replace(tmp, self.path)

    # --- entries ---------------------------------------------------------

    def unit_cost(self, amount: str) -> tuple[int, str]:
        """(cost of one occurrence, description). Raises ValueError if unreadable."""
        try:
            return parse_amount(amount), money(parse_amount(amount))
        except ValueError:
            pass
        m = re.fullmatch(r"\s*(\d+)\s*x?\s+(.+?)\s*", amount, re.IGNORECASE)
        if not m:
            raise ValueError(amount)
        qty = int(m.group(1))
        priced = self.item_price(m.group(2))
        if priced is None:
            raise ValueError(f"unknown item {m.group(2)!r}")
        price, name = priced
        return qty * price, f"{qty}x {name} ({money(qty * price)})"

    def add(self, name: str, amount: str, every: str, due: str | None) -> str:
        self.unit_cost(amount)  # validate now, not at report time
        every_days = parse_every(every)
        due_ts = None if due is None else int(time.time()) + parse_due(due) * DAY
        self.entries[name] = {"amount": amount, "every": every_days, "due": due_ts}
        self._save()
        return name

    def item_names(self) -> list[str]:
        """Item names used in entries, so their prices can be refreshed first."""
        names = []
        for e in self.entries.values():
            m = re.fullmatch(r"\s*(\d+)\s*x?\s+(.+?)\s*", e["amount"], re.IGNORECASE)
            if m:
                try:
                    parse_amount(e["amount"])
                except ValueError:
                    names.append(m.group(2))
        return names

    def remove(self, name: str) -> bool:
        if self.entries.pop(name, None) is None:
            return False
        self._save()
        return True

    # --- API (rent and upkeep) ------------------------------------------

    async def refresh(self, session: aiohttp.ClientSession, force: bool = False) -> None:
        if not force and time.time() - self.properties_fetched < PROPERTIES_CACHE_SECONDS:
            return
        params = {"key": self.api_key}
        if self.player_id is None:
            async with session.get(USER_BASIC_URL, params=params, timeout=20) as resp:
                self.player_id = (await resp.json())["profile"]["id"]
        async with session.get(USER_PROPERTIES_URL, params=params, timeout=20) as resp:
            data = await resp.json()
        if "error" in data:
            raise ValueError(data["error"])
        self.properties = data["properties"]
        self.properties_fetched = time.time()

    def leases(self) -> list[dict]:
        """Properties you rent (not ones you own and rent out)."""
        return [p for p in self.properties
                if p.get("status") == "rented" and (p.get("rented_by") or {}).get("id") == self.player_id]

    def upkeep_per_day(self) -> int:
        total = 0
        for p in self.properties:
            renter = (p.get("rented_by") or {}).get("id")
            if p.get("status") == "rented" and renter != self.player_id:
                continue  # yours, rented out: the tenant pays its upkeep
            total += p["upkeep"]["property"] + p["upkeep"]["staff"]
        return total

    def default_horizon(self) -> int:
        """Days until your next rent is due, or DEFAULT_HORIZON_DAYS if you rent nothing."""
        days = [p["rental_period_remaining"] for p in self.leases() if p.get("rental_period_remaining") is not None]
        return min(days) if days else DEFAULT_HORIZON_DAYS

    # --- totals ----------------------------------------------------------

    def lines(self, horizon_days: int) -> list[dict]:
        """Every cost in the next `horizon_days`: {name, total, detail, auto}."""
        now = time.time()
        out = []
        for p in self.leases():
            remaining = p.get("rental_period_remaining")
            if remaining is not None and remaining <= horizon_days:
                # Assume the renewal costs what this lease did.
                out.append({"name": f"{p['property']['name']} rent", "total": p["cost"], "auto": True,
                            "detail": f"renews in {remaining}d (last lease {money(p['cost'])}/"
                                      f"{p['rental_period']}d)"})
        upkeep = self.upkeep_per_day()
        if upkeep:
            out.append({"name": "Upkeep", "total": upkeep * horizon_days, "auto": True,
                        "detail": f"{money(upkeep)}/day × {horizon_days}d"})
        end = now + horizon_days * DAY
        for name, e in self.entries.items():
            try:
                unit, desc = self.unit_cost(e["amount"])
            except ValueError as exc:
                out.append({"name": name, "total": 0, "auto": False, "detail": f"⚠️ can't price: {exc}"})
                continue
            every, due = e["every"], e["due"]
            if every == 0:  # one-off: counts if it's still ahead (or has no date)
                if due is not None and due < now - DAY:
                    out.append({"name": name, "total": 0, "auto": False, "detail": f"{desc} · past due — remove it?"})
                    continue
                count = 1 if due is None or due <= end else 0
                when = "" if due is None else f" · due in {max(math.ceil((due - now) / DAY), 0)}d"
            elif due is None:  # recurring, no date: spread evenly
                count = horizon_days / every
                when = ""
            else:  # recurring from a date: roll past dates forward, count those in range
                period = every * DAY
                while due < now - DAY:
                    due += period
                count = 0 if due > end else math.floor((end - due) / period) + 1
                when = f" · next in {max(math.ceil((due - now) / DAY), 0)}d"
            times = f"{round(count, 1):g}×" if every else ""
            outside = " · after this window" if count == 0 else ""
            out.append({"name": name, "total": int(unit * count), "auto": False,
                        "detail": f"{times}{desc} {every_text(every)}{when}{outside}"})
        return out

    def total(self, horizon_days: int) -> int:
        return sum(line["total"] for line in self.lines(horizon_days))

    def embed(self, horizon_days: int, horizon_source: str) -> dict:
        lines = self.lines(horizon_days)
        rows = [f"{'🔄' if l['auto'] else '✏️'} **{l['name']}** — {money(l['total'])} · {l['detail']}"
                for l in lines]
        total = sum(l["total"] for l in lines)
        rows.append(f"\n**Total: {money(total)}** · ≈ {money(total / max(horizon_days, 1))}/day")
        return {"title": f"🧾 Spending — next {horizon_days} days ({horizon_source})",
                "description": "\n".join(rows),
                "footer": {"text": "🔄 from Torn API · ✏️ your entries (/spend-add, /spend-remove) · "
                                   "items priced at lowest listing"}}

    # --- rent alerts -----------------------------------------------------

    def due_rent_alerts(self, alert_days: list[int]) -> list[str]:
        """Messages for leases whose days-left just hit a configured value (once each)."""
        messages = []
        today = datetime.now(timezone.utc).date()
        for p in self.leases():
            remaining = p.get("rental_period_remaining")
            if remaining is None or remaining not in alert_days:
                continue
            lease_end = today.toordinal() + remaining  # stable for the whole lease
            key = f"{p['id']}:{lease_end}:{remaining}"
            if key in self.alerts_sent:
                continue
            self.alerts_sent.append(key)
            messages.append(f"🏝️ {p['property']['name']} lease: {remaining} day{'s' if remaining != 1 else ''} "
                            f"left (Torn's count) — renewing costs ≈ {money(p['cost'])}. "
                            f"Renew on the property page.")
        if messages:
            self._save()
        return messages
