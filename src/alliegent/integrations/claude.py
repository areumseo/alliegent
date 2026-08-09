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
from datetime import date

log = logging.getLogger(__name__)

MODEL = "claude-opus-5"
MAX_TOKENS = 8000

SYSTEM = """\
You produce a daily AI news digest for one reader, delivered to Discord.

Search the web for the most significant AI news from the last 24 hours. Prefer
English-language sources — they publish earlier and in more depth than Korean
coverage of the same stories.

Select the {count} most significant items. Significance means: it changes what
someone building with AI can do, how much it costs, or what the competitive
landscape looks like. Model and product launches, major research results,
funding and acquisitions that shift the field, and consequential policy or
regulation all qualify. Rank by how much the item would change a practitioner's
plans — not by how much coverage it got. Skip opinion pieces, listicles,
speculation with no new facts, and stories where the only news is that someone
commented on an earlier story. Never include the same story twice from two
outlets.

Output format — plain text, no preamble, no closing remarks, exactly this shape
for each item, separated by a blank line:

**1. <the article's own English headline>**
<url>
EN: <one sentence, max 25 words, on what happened and why it matters>
KO: <the same in natural Korean — translate the meaning, not the words>

Rules:
- Use the article's real headline and real URL. Never invent either.
- The Korean line must read as Korean written by a person, not as a
  transliteration. Keep established English technical terms in English.
- No markdown headers, no bullet characters, no numbered-list syntax beyond the
  bold number shown above.
- If you find fewer than {count} items that clear the significance bar, return
  fewer. Padding the list is worse than a short list.\
"""


class NewsUnavailable(RuntimeError):
    """The digest could not be produced; the caller should stay quiet."""


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
            tools=[{"type": "web_search_20260209", "name": "web_search", "max_uses": 8}],
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

    text = "\n".join(
        block.text for block in response.content if getattr(block, "type", None) == "text"
    ).strip()
    if not text:
        raise NewsUnavailable("empty response")
    return text
