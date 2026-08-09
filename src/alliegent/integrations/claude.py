"""Daily AI news digest, via Claude with the server-side web search tool.

Why search rather than RSS: a feed list has to be curated, deduplicated, and
ranked by hand, and a dead feed fails silently. Letting the model search means
one API call covers finding, selecting, summarising, and translating — and
there is no feed list to rot.

The model returns the finished Discord message rather than JSON. Structured
outputs are incompatible with citations, which the web search tool produces,
and a parse failure at 09:00 would mean no digest at all — so the formatting
lives in the prompt and nothing here can fail to parse.
"""

from __future__ import annotations

import logging
import re
from datetime import date

log = logging.getLogger(__name__)

ITEM_START = re.compile(r"^\*\*\d+\.", re.MULTILINE)

MODEL = "claude-opus-5"
# Ten items at two or three sentences each, in two languages, plus the model's
# own thinking — 8000 truncated the list mid-item.
MAX_TOKENS = 16000

SYSTEM = """\
You produce a daily AI news digest for one reader, delivered to Discord.

Search the web for the most significant AI news from the last 24 hours. Prefer
English-language sources — they publish earlier and in more depth than Korean
coverage of the same stories.

Link to reporting, not to changelogs. A vendor's release-notes page, a docs
page, or an aggregator listing is not a story: if the news is a product
release, find an article about it, and only fall back to the vendor's own
announcement post if no coverage exists. A title like "Release Notes" or
"Latest Updates" means you picked the wrong page — search again.

Select the {count} most significant items. Significance means: it changes what
someone building with AI can do, how much it costs, or what the competitive
landscape looks like. Model and product launches, major research results,
funding and acquisitions that shift the field, and consequential policy or
regulation all qualify. Rank by how much the item would change a practitioner's
plans — not by how much coverage it got. Skip opinion pieces, listicles,
speculation with no new facts, and stories where the only news is that someone
commented on an earlier story. Never include the same story twice from two
outlets.

Your entire reply is the digest itself. No preamble, no narration of your
searching, no closing note about coverage or quota. Start at "**1." and stop
after the last KO line.

Output format — plain text, exactly this shape for each item, separated by a
blank line:

**1. <the article's own English headline>**
<url>
EN: <three sentences: what happened, the specifics that matter (numbers,
    names, dates, what shipped), and what changes as a result>
KO: <the same content in Korean — not a shorter version>

Rules:
- Use the article's real headline and real URL. Never invent either.
- Write enough that the reader does not need to open the link to know what
  happened. Include the concrete details — who, how much, when, what exactly
  shipped — rather than gesturing at them. A summary that could describe any
  story in its category is too vague to be worth reading.
- Each summary is one paragraph on one line. Do not use line breaks inside a
  summary; the EN and KO lines each stay a single line.
- The Korean line must read as Korean written by a person, not as a
  transliteration. Keep established English technical terms in English.
- Write the Korean in 격식체 — end sentences with -습니다 / -입니다. Never use
  the plain 해라체 (-했다, -이다) that news articles use.
- No markdown headers, no bullet characters, no numbered-list syntax beyond the
  bold number shown above.
- If you find fewer than {count} items that clear the significance bar, return
  fewer. Padding the list is worse than a short list.\
"""


class NewsUnavailable(RuntimeError):
    """The digest could not be produced; the caller should stay quiet."""


def _clean(text: str) -> str:
    """Keep only the numbered items, and put each field back on one line.

    The prompt asks for no preamble and no closing note, but instruction
    adherence isn't a guarantee and a stray "I'll search for..." at the top of
    every digest is exactly the kind of thing that survives for months. So the
    boundaries are enforced here rather than hoped for: everything before the
    first item and after the last KO line is dropped.

    Search results also arrive with citation line breaks mid-sentence, which
    splits an EN or KO line across several lines and breaks the layout — those
    are collapsed back.
    """
    match = ITEM_START.search(text)
    if match:
        text = text[match.start() :]

    lines: list[str] = []
    # A summary line stays "open" so a citation break folds back into it
    # instead of splitting the sentence across lines. EN and KO differ on
    # blank lines: an EN line is always followed by its KO line, so a blank is
    # safely a citation artefact. After KO, a blank is the boundary between
    # items — and the point where any trailing commentary starts — so KO only
    # absorbs lines directly beneath it.
    open_field = ""
    for raw in text.split("\n"):
        line = raw.rstrip()
        if line.startswith(("**", "EN:", "KO:", "http")):
            open_field = line[:2] if line.startswith(("EN", "KO")) else ""
            lines.append(line)
            continue

        if open_field == "EN":
            if line.strip():
                lines[-1] = f"{lines[-1].rstrip()} {line.strip()}"
            continue
        if open_field == "KO" and line.strip():
            lines[-1] = f"{lines[-1].rstrip()} {line.strip()}"
            continue

        open_field = ""
        lines.append(line)

    # Punctuation that ended up orphaned by a fold ("... this year ; the news").
    lines = [re.sub(r"\s+([.,;:!?])", r"\1", line) for line in lines]

    # Punctuation that ended up orphaned by a fold ("... this year ; the news").
    lines = [re.sub(r"\s+([.,;:!?])", r"\1", line) for line in lines]

    # Drop trailing commentary: nothing after the final KO line belongs here.
    while lines and not lines[-1].startswith("KO:"):
        lines.pop()

    # The labels are scaffolding, not content: they make the boundaries above
    # unambiguous, and a reader can tell English from Korean at a glance.
    lines = [
        line[3:].lstrip() if line.startswith(("EN:", "KO:")) else line for line in lines
    ]
    return "\n".join(lines).strip()


async def fetch_ai_news(api_key: str, today: date, count: int = 10) -> str:
    """Return the digest body, or raise NewsUnavailable."""
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=api_key)
    try:
        response = await client.beta.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM.format(count=count),
            # Routine daily summarisation; the selection judgement matters more
            # than reasoning depth, and this runs every morning.
            output_config={"effort": "medium"},
            # Finding ten stories worth reporting takes more than a handful of
            # searches; too low a cap silently yields a short digest.
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 20}],
            # Opus 5's safety classifiers can decline a request outright; this
            # re-runs it on the recommended fallback instead of returning nothing.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Today is {today.isoformat()}. Give me today's AI news digest."
                    ),
                }
            ],
        )
    except Exception as exc:  # network, auth, rate limit
        raise NewsUnavailable(f"{type(exc).__name__}: {exc}") from exc
    finally:
        await client.close()

    # Check stop_reason before reading content — a refusal returns HTTP 200
    # with empty or partial content, so indexing content[0] would break here.
    if response.stop_reason == "refusal":
        raise NewsUnavailable("Claude declined the request")

    raw = "\n".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    )
    text = _clean(raw)
    if not text:
        raise NewsUnavailable("no items in response")
    return text
