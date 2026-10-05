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

import logging
from datetime import date

from . import assets as assets_module

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-5"
MAX_TOKENS = 1500
# Enough for two reports and a plan; past it a report is padding the model
# would have to read past, and the bill grows with it.
CONTEXT_LIMIT = 14000

SYSTEM = """\
You are alliegent, giving your honest opinion to the one person whose agenda,
projects, assets and tool costs you look after. Today is {today} ({weekday}).
They asked in the {where} channel, so the figures below are that area's
reports -- the only data you have. Do not invent a figure that is not in them,
and say so plainly when they do not answer the question.

Give a view first: a recommendation or a judgement, in a sentence. Then the
reasons, from their numbers, in a few lines. Say what would change your mind if
that is useful. Disagree when you think they are wrong; do not flatter, and do
not hedge a view into nothing. On money, give reasoning, not guarantees, and
skip boilerplate disclaimers.

This is chat: stay under about 200 words unless asked for more. Always reply in
English, even when the question is in Korean -- keep Korean names and titles
verbatim.

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


async def _safe(label: str, awaitable) -> str | None:
    """One report, or nothing: a failing source costs the answer that report,
    not the answer."""
    try:
        return await awaitable
    except Exception:
        log.exception("ask: could not read %s", label)
        return None


async def gather_context(jobs, kind: str | None) -> str:
    """The reports for a channel, as the bot would post them there."""
    parts: list[str | None] = []
    if kind == "build":
        parts.append(await _safe("project week", jobs.build_project_week()))
        parts.append(await _safe("costs", jobs.build_costs()))
    elif kind == "assets":
        parts.append(await _safe("assets", _assets(jobs)))
        parts.append(await _safe("plan", _plan(jobs)))
    elif kind == "karrot":
        parts.append(await _safe("karrot", jobs.build_karrot_report()))
    elif kind in ("news", "english"):
        pass  # nothing of theirs to read here; the model is told there is none
    else:
        parts.append(await _safe("the day", jobs.build_daily_brief()))
    text = "\n\n".join(p for p in parts if p)
    return text[:CONTEXT_LIMIT] or "(no reports for this channel)"


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
    client = client or AsyncAnthropic(api_key=api_key)
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
            output_config={"effort": "medium"},
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
