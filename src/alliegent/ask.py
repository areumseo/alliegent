"""Ask alliegent what it thinks, from any channel.

The chat agent behind a mention manages the agenda: look things up, add,
complete, delete. This is the other half -- an opinion. "Should I cancel
ChatGPT?", "is the Stockpulse work slowing down?", "can I afford to move more
into the funds this month?" A view is only worth having if it is made from the
person's own numbers, so the command reads the channel it is typed in and hands
the model the same reports the bot would post there: costs and project status in
#build, the snapshot and the plan in #assets, sales in #karrot, the day in
anywhere else.

It sees those reports and nothing else, and says so when they do not answer the
question. It has no tools and writes nothing, which is the point of keeping it
apart from the chat agent: asking for an opinion can never change a row.

The reports go to the Anthropic API with the question, as the agenda does when
the bot is mentioned. Amounts included, in #build and #assets.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import date

from . import assets as assets_module

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-5"
MAX_TOKENS = 1500
# The model call gives up after this, so a stuck request answers with an apology
# instead of leaving "thinking…" up for fifteen minutes.
ANSWER_TIMEOUT = 60.0
# Enough for two reports and a plan; past it a report is padding the model
# would have to read past, and the bill grows with it.
CONTEXT_LIMIT = 14000

SYSTEM = """\
You are alliegent, talking over chat with the one person whose agenda, projects,
assets and tool costs you look after. They have asked what you think. Today is
{today} ({weekday}). They asked in the {where} channel, so the figures below are
that area's reports -- the only data you have. Do not invent a figure that is
not in them, and say so plainly when they do not answer the question.

Talk the way a thoughtful colleague who knows their situation would: plain,
natural sentences in a few short paragraphs. Start with what you think, said
the way you would say it out loud, then why, from their numbers. If something
could change your mind, say it in a sentence rather than as a labelled part. No
headings and no labels such as "Verdict", "Reasoning" or "Conclusion", and no
bullet lists unless you are listing figures. Be warm without flattering: it is
fine to say "I'd" and "I think", that a number looks fine, or that you are not
sure. When you think they are wrong, say so kindly and plainly. On money, give
your reasoning, not guarantees, and skip boilerplate disclaimers.

Stay under about 200 words unless asked for more. Reply in the language the
question is written in -- Korean for a Korean question, English for an English
one -- and keep names and item titles exactly as they appear in the reports,
whatever language they are in.

Reports from the {where} channel:

{context}\
"""


# Dedicated channels only, in the order they are read: the review channel falls
# back to the agenda one, so agenda is checked first. A server that runs on one
# shared channel matches none of these and gets the day.
KINDS = ("build", "assets", "karrot", "english", "news", "agenda", "review")


def channel_kind(secrets, *channel_ids: int | None) -> str | None:
    """Which area a channel belongs to, from the ids it could be -- the channel
    itself, or the forum a post sits in."""
    wanted = {i for i in channel_ids if i}
    for kind in KINDS:
        own = {getattr(secrets, f"discord_{kind}_channel_id", 0)}
        if kind == "build":
            own.add(secrets.discord_projects_channel_id)  # its name before #build
        if (own - {0}) & wanted:
            return kind
    return None


# How long the reports get before the answer goes ahead without the ones still
# running, and how long a set of reports is reused. A follow-up question in the
# same channel a minute later wants the same reports, not another tour of Notion
# and GitHub.
CONTEXT_TIMEOUT = 25.0
CACHE_SECONDS = 300.0
_cache: dict[str, tuple[float, str]] = {}


async def _safe(label: str, awaitable) -> str | None:
    """One report, or nothing: a failing source costs the answer that report,
    not the answer."""
    started = time.monotonic()
    try:
        return await awaitable
    except Exception:
        log.exception("ask: could not read %s", label)
        return None
    finally:
        # Per report, so a slow ask can be traced to the one that made it slow.
        log.info("ask: read %s in %.1fs", label, time.monotonic() - started)


def _sources(jobs, kind: str | None) -> list[tuple[str, object]]:
    """The reports an area is given, as (label, awaitable)."""
    if kind == "build":
        return [("project status", jobs.build_project_week()), ("costs", jobs.build_costs())]
    if kind == "assets":
        return [("assets", _assets(jobs)), ("plan", _plan(jobs))]
    if kind == "karrot":
        return [("karrot sales", jobs.build_karrot_report())]
    if kind in ("news", "english"):
        return []  # nothing of theirs to read here; the model is told there is none
    return [("the day", jobs.build_daily_brief())]


async def gather_context(
    jobs,
    kind: str | None,
    *,
    timeout: float = CONTEXT_TIMEOUT,
    clock=time.monotonic,
) -> str:
    """The reports for a channel, as the bot would post them there.

    Read together, since they are independent and one after another they add
    up. Whatever is still running when `timeout` passes is dropped and named, so
    the model knows what it does not have -- a slow report costs the answer that
    report, never the answer. A complete set is kept for a few minutes.
    """
    key = kind or ""
    hit = _cache.get(key)
    if hit and clock() - hit[0] < CACHE_SECONDS:
        return hit[1]

    sources = _sources(jobs, kind)
    tasks = [(label, asyncio.ensure_future(_safe(label, coro))) for label, coro in sources]
    if tasks:
        _, pending = await asyncio.wait([t for _, t in tasks], timeout=timeout)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    parts: list[str] = []
    missing: list[str] = []
    for label, task in tasks:
        text = None if task.cancelled() else task.result()
        if text:
            parts.append(text)
        else:
            missing.append(label)

    text = "\n\n".join(parts)[:CONTEXT_LIMIT]
    if missing:
        text += ("\n\n" if text else "") + "(Not available just now: " + ", ".join(missing) + ")"
    elif not text:
        text = "(no reports for this channel)"
    if tasks and not missing:
        _cache[key] = (clock(), text)
    return text


async def _assets(jobs) -> str | None:
    if jobs.assets is None:
        return None
    history = await jobs.assets.history()
    if not history:
        return None
    previous = history[-2] if len(history) > 1 else None
    return assets_module.snapshot_message(history[-1], previous)


async def _plan(jobs) -> str | None:
    if jobs.plans is None or jobs.assets is None:
        return None
    from .plans import current_standing

    return await current_standing(jobs.plans, jobs.assets, jobs.config)


async def answer(
    question: str,
    context: str,
    *,
    api_key: str,
    today: date,
    kind: str | None,
    client=None,
) -> str:
    """The model's view. `client` is injectable so the call can be tested."""
    from anthropic import AsyncAnthropic

    own = client is None
    client = client or AsyncAnthropic(api_key=api_key, timeout=ANSWER_TIMEOUT, max_retries=1)
    try:
        response = await client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM.format(
                today=today.isoformat(),
                weekday=today.strftime("%A"),
                where=f"#{kind}" if kind else "this",
                context=context,
            ),
            # Low, as the chat agent has it. The figures are already in the prompt, so
            # what is left is saying what they mean, and a long think before it is
            # most of what made the answer take a minute.
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": question}],
        )
    finally:
        if own:
            await client.close()
    if response.stop_reason == "refusal":
        return "⚠️ I can't help with that one."
    reply = "\n".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()
    return reply or "…"
