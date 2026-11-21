"""What has been said in the server's channels, for /ask.

The reports /ask already reads are rebuilt from Notion and GitHub; they know
the numbers but not the conversation. This reads the channels themselves --
the recent messages in each configured channel, and the recent posts in a
forum -- so a question like "what did I decide about the Stockpulse pricing?"
can be answered from what was actually written, wherever it was written.

#mood is never read. It was built on the rule that what is written there stays
on this machine and never goes to an outside model, and /ask sends what it
reads to the Anthropic API. The channel is left out by id, a thread whose
parent is #mood is left out too, and if #mood happens to share an id with
another setting it is still left out.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta

log = logging.getLogger(__name__)

# How far back, and how much. Two weeks covers "what did we say last week"; a
# channel past these limits loses its oldest messages first.
DAYS = 14
PER_CHANNEL = 150
# Characters per channel, and for everything together. Korean runs close to a
# token a character, so the total stays near 30k tokens of input.
CHANNEL_CHARS = 10000
TOTAL_CHARS = 36000
# Recent posts read from a forum, besides the channels themselves.
FORUM_POSTS = 8
TIMEOUT = 20.0
CACHE_SECONDS = 300.0
_cache: dict[str, tuple[float, str]] = {}

# The channels read, in this order. #mood is not among them, and never will be.
KINDS = ("agenda", "build", "assets", "karrot", "news", "english", "review")


def searchable_channels(secrets) -> list[tuple[str, int]]:
    """(area, channel id) for every configured channel but #mood, each id once.

    Areas that fall back to the shared default channel all name it; it is read
    once, under the first area that uses it.
    """
    mood = secrets.discord_mood_channel_id
    seen: set[int] = set()
    out: list[tuple[str, int]] = []
    for kind in KINDS:
        try:
            channel_id = secrets.channel_for(kind)
        except RuntimeError:
            continue
        if not channel_id or channel_id == mood or channel_id in seen:
            continue
        seen.add(channel_id)
        out.append((kind, channel_id))
    return out


def format_message(message, tz) -> str | None:
    """One line: when, who, what. Nothing for a message with no text."""
    text = (getattr(message, "content", "") or "").strip()
    if not text:
        return None
    when = message.created_at.astimezone(tz).strftime("%m-%d %H:%M")
    author = getattr(message.author, "display_name", None) or str(message.author)
    return f"[{when}] {author}: {text}"


async def _lines(channel, after: datetime, tz) -> list[str]:
    """A channel's recent messages, oldest first."""
    lines = []
    async for message in channel.history(limit=PER_CHANNEL, after=after, oldest_first=False):
        line = format_message(message, tz)
        if line:
            lines.append(line)
    lines.reverse()
    return lines


def _clip(lines: list[str], limit: int) -> str:
    """The newest lines that fit in `limit` characters, in order."""
    kept: list[str] = []
    size = 0
    for line in reversed(lines):
        size += len(line) + 1
        if size > limit:
            break
        kept.append(line)
    kept.reverse()
    return "\n".join(kept)


async def _read_forum(forum, after: datetime, tz, mood_id: int, limit: int) -> str:
    """A forum's recent posts, each with its own recent messages."""
    threads = [t for t in getattr(forum, "threads", []) if getattr(t, "parent_id", None) != mood_id]
    threads.sort(key=lambda t: getattr(t, "last_message_id", 0) or 0, reverse=True)
    parts = []
    share = max(limit // max(min(len(threads), FORUM_POSTS), 1), 500)
    for thread in threads[:FORUM_POSTS]:
        lines = await _lines(thread, after, tz)
        if lines:
            parts.append(f"Post \"{thread.name}\":\n{_clip(lines, share)}")
    return "\n\n".join(parts)


async def _read_one(bot, kind, channel_id, after, tz, mood_id, limit):
    """(heading, text) for one channel, or (heading, None) when it can't be read."""
    import discord

    channel = bot.get_channel(channel_id)
    try:
        if channel is None:
            channel = await bot.fetch_channel(channel_id)
        name = getattr(channel, "name", kind)
        heading = f"#{name}" if name == kind else f"#{name} ({kind})"
        if getattr(channel, "parent_id", None) == mood_id:
            return heading, ""
        if isinstance(channel, discord.ForumChannel):
            return heading, await _read_forum(channel, after, tz, mood_id, limit)
        return heading, _clip(await _lines(channel, after, tz), limit)
    except discord.HTTPException as exc:
        log.warning("ask: cannot read channel %s (%s): %s", channel_id, kind, exc)
        return f"#{kind}", None


async def gather(
    bot, secrets, *, now: datetime, timeout: float = TIMEOUT, clock=time.monotonic
) -> str:
    """Recent messages from every channel but #mood, headed by channel.

    Channels are read together; one that can't be read, or is still reading
    when `timeout` passes, is named as unavailable rather than costing the
    answer. A complete read is kept for a few minutes.
    """
    hit = _cache.get("all")
    if hit and clock() - hit[0] < CACHE_SECONDS:
        return hit[1]

    mood_id = secrets.discord_mood_channel_id
    after = now - timedelta(days=DAYS)
    tz = now.tzinfo
    channels = searchable_channels(secrets)
    # Shared out, so the last channels are not the ones cut when all are busy.
    limit = min(CHANNEL_CHARS, TOTAL_CHARS // max(len(channels), 1))
    tasks = [
        (kind, asyncio.ensure_future(_read_one(bot, kind, cid, after, tz, mood_id, limit)))
        for kind, cid in channels
    ]
    if tasks:
        _, pending = await asyncio.wait([t for _, t in tasks], timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    parts: list[str] = []
    missing: list[str] = []
    for kind, task in tasks:
        if task.cancelled() or task.exception() is not None:
            missing.append(f"#{kind}")
            continue
        heading, text = task.result()
        if text is None:
            missing.append(heading)
        elif text:
            parts.append(f"## {heading}\n{text}")

    text = "\n\n".join(parts)[:TOTAL_CHARS]
    if missing:
        text += ("\n\n" if text else "") + "(Could not read: " + ", ".join(missing) + ")"
    elif not text:
        text = "(no recent messages)"
    if tasks and not missing:
        _cache["all"] = (clock(), text)
    return text
