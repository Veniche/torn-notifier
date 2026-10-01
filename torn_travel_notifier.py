"""
Torn notifier — Discord DM a few seconds before you land, and when your
drug cooldown ends. When you're about to land back in Torn it also sends a
plushie/flower stock report for your next trip (or run /travel any time).

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
from discord import app_commands
from dotenv import load_dotenv

import stock

load_dotenv()

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
DISCORD_USER_ID = int(os.environ["DISCORD_USER_ID"])
TORN_API_KEY = os.environ["TORN_API_KEY"]

POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
ALERT_LEAD_SECONDS = int(os.getenv("ALERT_LEAD_SECONDS", "30"))
TRAVEL_CAPACITY = int(os.getenv("TRAVEL_CAPACITY", "5"))
STOCK_POLL_SECONDS = int(os.getenv("STOCK_POLL_SECONDS", "300"))
STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

# Cooldowns only come back as "seconds remaining", so the end time we
# derive jitters between polls (rounding, cached responses). Only treat
# it as a new cooldown if the end time moves by more than this.
COOLDOWN_TOLERANCE_SECONDS = 60

TORN_API_URL = "https://api.torn.com/user/"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("torn-notifier")

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)
tracker = stock.StockTracker(STATE_PATH, TORN_API_KEY, TRAVEL_CAPACITY)
http: aiohttp.ClientSession | None = None

# Arrival timestamp we've already scheduled an alert for, so a repeat
# poll of the same trip doesn't schedule a second alert.
scheduled_arrival: int | None = None
alert_task: asyncio.Task | None = None

# Same idea for the drug cooldown's end time.
scheduled_drug_end: int | None = None
drug_task: asyncio.Task | None = None

poll_task: asyncio.Task | None = None
stock_task: asyncio.Task | None = None


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


async def fresh_stock_embeds() -> list[discord.Embed]:
    try:
        await tracker.refresh(http)
    except Exception as exc:  # fall back to the last snapshot we have
        log.error("Stock refresh failed: %s", exc)
    return [discord.Embed.from_dict(e) for e in tracker.report_embeds()]


async def land_home_later(text: str, delay: int) -> None:
    """Landing alert for a flight back to Torn, followed by the stock report."""
    await send_later(text, delay)
    user = await client.fetch_user(DISCORD_USER_ID)
    await user.send(embeds=await fresh_stock_embeds())
    log.info("Stock report sent")


def handle_travel(travel: dict) -> None:
    global scheduled_arrival, alert_task

    tracker.note_travel(travel)
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
    text = f"🛬 Landing in ~{ALERT_LEAD_SECONDS}s — {destination}"
    job = land_home_later if destination == "Torn" else send_later
    alert_task = asyncio.create_task(job(text, delay))


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

    while not client.is_closed():
        try:
            data = await fetch_status(http)

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


async def stock_loop() -> None:
    """Snapshot YATA stock regularly so sell-rates are ready when needed."""
    while not client.is_closed():
        try:
            await tracker.refresh(http)
        except Exception as exc:
            log.error("Stock refresh failed: %s", exc)
        await asyncio.sleep(STOCK_POLL_SECONDS)


@tree.command(name="travel", description="Plushie & flower stock abroad, predicted at landing")
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def travel_command(interaction: discord.Interaction) -> None:
    # The repo is public and the bot sits in a server; only answer its owner.
    if interaction.user.id != DISCORD_USER_ID:
        await interaction.response.send_message("This bot is private.", ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    await interaction.followup.send(embeds=await fresh_stock_embeds())


async def setup_hook() -> None:
    global http
    http = aiohttp.ClientSession()
    await tree.sync()

client.setup_hook = setup_hook


@client.event
async def on_ready() -> None:
    global poll_task, stock_task
    log.info("Logged in as %s", client.user)
    # on_ready fires again after reconnects; only ever run one of each loop.
    if poll_task is None or poll_task.done():
        poll_task = asyncio.create_task(poll_loop())
    if stock_task is None or stock_task.done():
        stock_task = asyncio.create_task(stock_loop())


if __name__ == "__main__":
    # log_handler=None: discord.py's logs go through basicConfig above
    # instead of a second handler that would print every line twice.
    client.run(DISCORD_BOT_TOKEN, log_handler=None)
