"""
Torn stock market: where to park money, and dividend-ready alerts.

Torn stock prices only move a few percent a month, so the useful signals
are dividends ("benefit blocks": hold enough shares and a stock pays cash
or items on a schedule) and how much a stock swings, for money you may
need to pull out at short notice. Prices aren't predicted.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Callable

import aiohttp

log = logging.getLogger("torn-notifier")

TORN_STOCKS_URL = "https://api.torn.com/v2/torn/stocks"
TORN_STOCK_URL = "https://api.torn.com/v2/torn/{stock_id}/stocks"
USER_STOCKS_URL = "https://api.torn.com/v2/user/stocks"
TORN_STATS_URL = "https://api.torn.com/torn/"

MARKET_CACHE_SECONDS = 10 * 60
# Monthly ranges need one call per stock; refresh a few per pass.
DETAILS_PER_REFRESH = 6
DETAILS_CACHE_SECONDS = 6 * 3600
POINTS_CACHE_SECONDS = 3600

TOP_BLOCKS = 8
TOP_STABLE = 5


def parse_amount(text: str) -> int:
    """'38m', '500k', '1.2bn', '$38,000,000' -> dollars."""
    t = text.strip().lower().replace(",", "").replace("$", "").replace(" ", "")
    for suffix, mult in (("bn", 1_000_000_000), ("b", 1_000_000_000), ("m", 1_000_000), ("k", 1_000)):
        if t.endswith(suffix):
            return int(float(t[: -len(suffix)]) * mult)
    return int(float(t))


def money(n: float) -> str:
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 1_000_000_000:
        return f"{sign}${n / 1_000_000_000:.2f}bn"
    if n >= 1_000_000:
        return f"{sign}${n / 1_000_000:.1f}m"
    if n >= 1_000:
        return f"{sign}${n / 1_000:.0f}k"
    return f"{sign}${n:.0f}"


class StockMarket:
    def __init__(self, api_key: str, item_value: Callable[[str], int | None]) -> None:
        """`item_value(name)` returns what one unit of an item nets you, or None."""
        self.api_key = api_key
        self.item_value = item_value
        self.market: dict[int, dict] = {}  # stock id -> TornStock
        self.market_fetched = 0.0
        self.details: dict[int, list] = {}  # stock id -> [performance, fetched_at]
        self.holdings: list[dict] = []  # UserStock entries
        self.point_price: float | None = None
        self.points_fetched = 0.0
        # Stocks whose ready dividend has already been DM'd; cleared once collected.
        self.notified: set[int] = set()

    async def _get(self, session: aiohttp.ClientSession, url: str, **params) -> dict:
        async with session.get(url, params={**params, "key": self.api_key}, timeout=20) as resp:
            data = await resp.json()
        if "error" in data:
            raise ValueError(data["error"])
        return data

    async def refresh(self, session: aiohttp.ClientSession, details: bool = True) -> list[dict]:
        """Update market and holdings; returns holdings whose dividend just became ready."""
        if time.time() - self.market_fetched > MARKET_CACHE_SECONDS:
            data = await self._get(session, TORN_STOCKS_URL)
            self.market = {s["id"]: s for s in data["stocks"]}
            self.market_fetched = time.time()

        if time.time() - self.points_fetched > POINTS_CACHE_SECONDS:
            try:
                data = await self._get(session, TORN_STATS_URL, selections="stats")
                self.point_price = data["stats"]["points_averagecost"]
                self.points_fetched = time.time()
            except Exception as exc:
                log.error("Points price fetch failed: %s", exc)

        self.holdings = (await self._get(session, USER_STOCKS_URL))["stocks"]

        if details:
            stale = sorted(self.market, key=lambda i: self.details.get(i, [None, 0])[1])
            for stock_id in stale[:DETAILS_PER_REFRESH]:
                if time.time() - self.details.get(stock_id, [None, 0])[1] < DETAILS_CACHE_SECONDS:
                    break
                try:
                    data = await self._get(session, TORN_STOCK_URL.format(stock_id=stock_id))
                    self.details[stock_id] = [data["stocks"]["chart"]["performance"], time.time()]
                except Exception as exc:
                    log.error("Stock %s details failed: %s", stock_id, exc)
                    break

        newly_ready = []
        for h in self.holdings:
            if h["bonus"]["available"]:
                if h["id"] not in self.notified:
                    self.notified.add(h["id"])
                    newly_ready.append(h)
            else:
                self.notified.discard(h["id"])
        return newly_ready

    # --- valuation -------------------------------------------------------

    def payout_value(self, description: str) -> int | None:
        """What one dividend nets you in cash terms, if it's cash, items or points."""
        text = description.strip()
        m = re.fullmatch(r"\$([\d,]+)", text)
        if m:
            return int(m.group(1).replace(",", ""))
        m = re.fullmatch(r"([\d,]+) points?", text)
        if m and self.point_price:
            return int(int(m.group(1).replace(",", "")) * self.point_price)
        m = re.fullmatch(r"(\d+)x (.+)", text)
        if m:
            unit = self.item_value(m.group(2))
            if unit:
                return int(m.group(1)) * unit
        return None  # energy, nerve, happiness, perks: not cash

    def block(self, stock_id: int) -> dict | None:
        """First benefit block of an active (dividend-paying) stock, valued."""
        s = self.market[stock_id]
        bonus = s["bonus"]
        if bonus["passive"]:
            return None
        value = self.payout_value(bonus["description"])
        cost = bonus["requirement"] * s["market"]["price"]
        yearly = value * 365 / bonus["frequency"] if value else None
        return {"cost": cost, "value": value, "yearly": yearly,
                "yield": yearly / cost if yearly else None}

    def month_range(self, stock_id: int) -> float | None:
        """(high − low) / price over the last month, as a fraction."""
        detail = self.details.get(stock_id)
        if not detail:
            return None
        month = detail[0]["last_month"]
        return (month["high"] - month["low"]) / self.market[stock_id]["market"]["price"]

    def year_change(self, stock_id: int) -> float | None:
        detail = self.details.get(stock_id)
        return detail[0]["last_year"]["change_percentage"] / 100 if detail else None

    def liquid(self, cash: int | None) -> float:
        """Cash on hand plus shares you could sell without breaking a block."""
        total = cash or 0
        for h in self.holdings:
            s = self.market[h["id"]]
            locked = s["bonus"]["requirement"] if h["bonus"]["increment"] > 0 and not s["bonus"]["passive"] else 0
            total += max(h["shares"] - locked, 0) * s["market"]["price"]
        return total

    # --- report ----------------------------------------------------------

    def _swing(self, stock_id: int) -> str:
        r = self.month_range(stock_id)
        return "swing not checked yet" if r is None else f"{r:.1%} monthly swing"

    def report_embeds(self, cash: int | None, reserve: int, reserve_label: str = "") -> list[dict]:
        if not self.market:
            return [{"title": "Stocks", "description": "No stock data yet — try again in a minute."}]
        total = sum(h["shares"] * self.market[h["id"]]["market"]["price"] for h in self.holdings)

        lines = []
        for h in sorted(self.holdings, key=lambda h: -h["shares"] * self.market[h["id"]]["market"]["price"]):
            s = self.market[h["id"]]
            value = h["shares"] * s["market"]["price"]
            bonus = h["bonus"]
            b = self.block(h["id"])
            if s["bonus"]["passive"]:
                status = f"perk: {s['bonus']['description']} (no dividend)"
            elif bonus["increment"] > 0:
                if bonus["available"]:
                    status = f"✅ **dividend ready** — {s['bonus']['description']}"
                else:
                    days = max(bonus["frequency"] - bonus["progress"], 0)
                    status = f"{bonus['increment']} block(s) · next {s['bonus']['description']} in {days}d"
            else:
                need = max(s["bonus"]["requirement"] - h["shares"], 0)
                status = (f"no block · {need:,} more shares "
                          f"({money(need * s['market']['price'])}) for {s['bonus']['description']}"
                          f"/{s['bonus']['frequency']}d")
                if b and b["yield"]:
                    status += f" ({b['yield']:.0%}/yr)"
            lines.append(f"**{s['acronym']}** · {h['shares']:,} shares · {money(value)} · "
                         f"{self._swing(h['id'])}\n{status}")
        cash_text = "" if cash is None else f" · {money(cash)} cash on hand"
        holdings = {"title": f"📈 Your stocks — {money(total)}{cash_text}",
                    "description": "\n".join(lines) or "You don't hold any stocks."}

        liquid = self.liquid(cash)
        free = liquid - reserve
        label = f" ({reserve_label})" if reserve_label else ""
        lines = [f"Keeping **{money(reserve)}** ready{label}" if reserve or reserve_label
                 else "No reserve set — add `reserve:` (e.g. `reserve:38m`) to keep cash aside",
                 f"Liquid now (cash + shares outside blocks): {money(liquid)} → "
                 + (f"**{money(free)} free** for dividend blocks" if free >= 0
                    else f"⚠️ **{money(-free)} short** of your reserve")]
        reserve = {"title": "🏝️ Cash to keep ready", "description": "\n".join(lines)}

        owned = {h["id"]: h for h in self.holdings}
        blocks = []
        for stock_id, s in self.market.items():
            b = self.block(stock_id)
            if not b or not b["yield"] or owned.get(stock_id, {}).get("bonus", {}).get("increment"):
                continue
            blocks.append((b["yield"], stock_id, b))
        blocks.sort(reverse=True)
        affordable = [x for x in blocks if x[2]["cost"] <= free][:TOP_BLOCKS]
        lines = []
        for yld, stock_id, b in affordable:
            s = self.market[stock_id]
            lines.append(f"**{s['acronym']}** — {s['bonus']['description']} every {s['bonus']['frequency']}d · "
                         f"block {money(b['cost'])} · **{yld:.0%}/yr** ({money(b['yearly'])}/yr) · "
                         f"{self._swing(stock_id)}")
        nearest = min((x for x in blocks if x[2]["cost"] > free), key=lambda x: x[2]["cost"], default=None)
        if nearest:
            yld, stock_id, b = nearest
            s = self.market[stock_id]
            lines.append(f"Next within reach: **{s['acronym']}** — block {money(b['cost'])} "
                         f"({money(b['cost'] - max(free, 0))} more free money needed) · {yld:.0%}/yr")
        blocks_embed = {"title": "💰 Dividend blocks you can afford after your reserve",
                        "description": "\n".join(lines) or "No cash/item dividend blocks fit yet."}

        stable = sorted((self.month_range(i), i) for i in self.market if self.month_range(i) is not None)
        lines = []
        for r, stock_id in stable[:TOP_STABLE]:
            s = self.market[stock_id]
            yc = self.year_change(stock_id)
            lines.append(f"**{s['acronym']}** — {r:.1%} monthly swing · {yc:+.1%} this year")
        checked = len(stable)
        stable_embed = {"title": "🛡️ Most stable (for money you may need soon)",
                        "description": "\n".join(lines) or "Swings not checked yet — try again in a few minutes.",
                        "footer": {"text": (f"Swings checked for {checked}/{len(self.market)} stocks · "
                                            "yields are first block only, items at best sale price · "
                                            "prices aren't predicted")}}
        return [holdings, reserve, blocks_embed, stable_embed]
