"""
Torn notifier — Discord DM a few seconds before you land, and when your
drug cooldown ends.

Polls Torn's API for your travel status and cooldowns. Once a trip or
cooldown is detected, it schedules a single precise alert, rather than
polling every few seconds (which would burn API calls and risk looking
like scripted hammering).
"""

from __future__ import annotations

import asyncio
import logging
import os
import time

import aiohttp
import discord
from dotenv import load_dotenv

load_dotenv()

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_USER_ID = int(os.environ["DISCORD_USER_ID"])
TORN_API_KEY = os.environ["TORN_API_KEY"]

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
ALERT_LEAD_SECONDS = int(os.getenv("ALERT_LEAD_SECONDS", "30"))

# Cooldowns only come back as "seconds remaining", so the end time we
# derive jitters between polls (rounding, cached responses). Only treat
# it as a new cooldown if the end time moves by more than this.
COOLDOWN_TOLERANCE_SECONDS = 60

TORN_API_URL = "https://api.torn.com/user/"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("torn-notifier")

intents = discord.Intents.default()
client = discord.Client(intents=intents)

# Arrival timestamp we've already scheduled an alert for, so a repeat
# poll of the same trip doesn't schedule a second alert.
scheduled_arrival: int | None = None
alert_task: asyncio.Task | None = None

# Same idea for the drug cooldown's end time.
scheduled_drug_end: int | None = None
drug_task: asyncio.Task | None = None

poll_task: asyncio.Task | None = None


async def fetch_status(session: aiohttp.ClientSession) -> dict:
    params = {"selections": "travel,cooldowns", "key": TORN_API_KEY}
    async with session.get(TORN_API_URL, params=params, timeout=15) as resp:
        return await resp.json()


async def send_dm(text: str) -> None:
    user = await client.fetch_user(DISCORD_USER_ID)
    await user.send(text)
    log.info("Alert sent: %s", text)


async def send_later(text: str, delay: int) -> None:
    await asyncio.sleep(delay)
    await send_dm(text)


def handle_travel(travel: dict) -> None:
    global scheduled_arrival, alert_task

    destination = travel.get("destination")
    time_left = travel.get("time_left", 0)
    if not (destination and time_left > 0):
        scheduled_arrival = None
        return

    # Prefer Torn's fixed arrival timestamp: recomputing it from
    # time_left drifts by a second between polls and would schedule
    # duplicate alerts for the same trip.
    arrival = travel.get("timestamp") or int(time.time()) + time_left
    if arrival == scheduled_arrival:
        return

    if alert_task and not alert_task.done():
        alert_task.cancel()
    scheduled_arrival = arrival
    # Time from the fixed arrival, not time_left: a cached API response
    # can report a stale time_left.
    delay = max(arrival - int(time.time()) - ALERT_LEAD_SECONDS, 0)
    log.info(
        "Trip to %s detected — landing in %ss, alert scheduled in %ss",
        destination, time_left, delay,
    )
    alert_task = asyncio.create_task(
        send_later(f"🛬 Landing in ~{ALERT_LEAD_SECONDS}s — {destination}", delay)
    )


def handle_drug_cooldown(remaining: int) -> None:
    global scheduled_drug_end, drug_task

    if remaining <= 0:
        scheduled_drug_end = None
        return

    end = int(time.time()) + remaining
    if scheduled_drug_end is not None and abs(end - scheduled_drug_end) <= COOLDOWN_TOLERANCE_SECONDS:
        return

    if drug_task and not drug_task.done():
        drug_task.cancel()
    scheduled_drug_end = end
    log.info("Drug cooldown detected — ends in %ss", remaining)
    drug_task = asyncio.create_task(
        send_later("💊 Drug cooldown is over — you can take another", remaining)
    )


async def poll_loop() -> None:
    await client.wait_until_ready()

    async with aiohttp.ClientSession() as session:
        while not client.is_closed():
            try:
                data = await fetch_status(session)

                if "error" in data:
                    code = data["error"].get("code")
                    if code == 16:
                        log.error(
                            "API key doesn't have access to the travel/cooldowns "
                            "selections — check its access level on "
                            "torn.com/preferences.php#tab=api"
                        )
                    else:
                        log.error("Torn API error: %s", data["error"])
                else:
                    handle_travel(data.get("travel", {}))
                    handle_drug_cooldown(data.get("cooldowns", {}).get("drug", 0))

            except Exception as exc:  # keep the loop alive across transient errors
                log.error("Poll failed: %s", exc)

            await asyncio.sleep(POLL_INTERVAL_SECONDS)


@client.event
async def on_ready() -> None:
    global poll_task
    log.info("Logged in as %s", client.user)
    # on_ready fires again after reconnects; only ever run one poll loop.
    if poll_task is None or poll_task.done():
        poll_task = asyncio.create_task(poll_loop())


if __name__ == "__main__":
    # log_handler=None: discord.py's logs go through basicConfig above
    # instead of a second handler that would print every line twice.
    client.run(DISCORD_BOT_TOKEN, log_handler=None)
