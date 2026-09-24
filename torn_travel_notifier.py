"""
Torn travel notifier — Discord DM a few seconds before you land.

Polls Torn's API for your travel status. Once a trip is detected, it
schedules a single precise alert timed to fire shortly before arrival,
rather than polling every few seconds (which would burn API calls and
risk look like scripted hammering).
"""

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
ALERT_LEAD_SECONDS = int(os.getenv("ALERT_LEAD_SECONDS", "20"))

TORN_API_URL = "https://api.torn.com/user/"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("torn-notifier")

intents = discord.Intents.default()
client = discord.Client(intents=intents)

# Arrival timestamp we've already scheduled an alert for, so a repeat
# poll of the same trip doesn't schedule a second alert.
scheduled_arrival: int | None = None


async def fetch_travel(session: aiohttp.ClientSession) -> dict:
    params = {"selections": "travel", "key": TORN_API_KEY}
    async with session.get(TORN_API_URL, params=params, timeout=15) as resp:
        return await resp.json()


async def send_alert(destination: str) -> None:
    user = await client.fetch_user(DISCORD_USER_ID)
    await user.send(f"🛬 Landing in ~{ALERT_LEAD_SECONDS}s — {destination}")
    log.info("Alert sent for %s", destination)


async def schedule_alert(destination: str, delay: int) -> None:
    await asyncio.sleep(delay)
    await send_alert(destination)


async def poll_loop() -> None:
    global scheduled_arrival
    await client.wait_until_ready()

    async with aiohttp.ClientSession() as session:
        while not client.is_closed():
            try:
                data = await fetch_travel(session)

                if "error" in data:
                    code = data["error"].get("code")
                    if code == 16:
                        log.error(
                            "API key doesn't have access to the travel "
                            "selection — regenerate it with at least "
                            "Minimal Access on torn.com/preferences.php#tab=api"
                        )
                    else:
                        log.error("Torn API error: %s", data["error"])
                else:
                    travel = data.get("travel", {})
                    destination = travel.get("destination")
                    time_left = travel.get("time_left", 0)
                    is_traveling = bool(destination) and destination != "Torn" and time_left > 0

                    if is_traveling:
                        arrival = int(time.time()) + time_left
                        if arrival != scheduled_arrival:
                            scheduled_arrival = arrival
                            delay = max(time_left - ALERT_LEAD_SECONDS, 0)
                            log.info(
                                "Trip to %s detected — landing in %ss, "
                                "alert scheduled in %ss",
                                destination, time_left, delay,
                            )
                            asyncio.create_task(schedule_alert(destination, delay))
                    else:
                        scheduled_arrival = None

            except Exception as exc:  # keep the loop alive across transient errors
                log.error("Poll failed: %s", exc)

            await asyncio.sleep(POLL_INTERVAL_SECONDS)


@client.event
async def on_ready() -> None:
    log.info("Logged in as %s", client.user)
    client.loop.create_task(poll_loop())


if __name__ == "__main__":
    client.run(DISCORD_BOT_TOKEN)
