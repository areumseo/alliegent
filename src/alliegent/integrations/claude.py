"""Turn a day's collected articles into the Discord news digest.

The articles are gathered from publication feeds (see `news_feeds`) rather
than found by the model. Search cost between 89k and 437k input tokens per
run, varied without warning, and some mornings never finished; a feed list
costs about 1.2k input tokens and the article's own publication date decides
what counts as yesterday.

So the model's job here is judgement and language, not retrieval: pick the
items that matter out of a supplied list, and write them up in English and
Korean. It cannot invent a link, because it never goes looking for one.

The model returns the finished Discord message rather than JSON. Structured
outputs would add a parse step whose failure at 09:00 means no digest at all,
so the formatting lives in the prompt and nothing here can fail to parse.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Sequence
from datetime import date

from .news_feeds import Entry

log = logging.getLogger(__name__)

ITEM_START = re.compile(r"^\*\*\d+\.", re.MULTILINE)
TOPICS_LINE = re.compile(r"^TOPICS:\s*(.+)$", re.MULTILINE | re.IGNORECASE)

MODEL = "claude-sonnet-5"
# Ten items at two or three sentences each, in two languages, plus the model's
# own thinking — 8000 truncated the list mid-item.
MAX_TOKENS = 16000
# A ceiling on the whole call, searches included. Nothing downstream waits on
# this -- a missing digest is a non-event -- so it is better to give up than to
# keep a paid request alive indefinitely.
TIMEOUT_SECONDS = 15 * 60

SYSTEM = """\
You produce a daily AI news digest for one reader, delivered to Discord.

You are given every AI article published on {day} by a handful of technology
publications. Work only from that list. Do not add stories from memory, and do
not alter a headline or a URL -- both are quoted from the article itself.

Select the {count} most significant items. Significance means: it changes what
someone building with AI can do, how much it costs, or what the competitive
landscape looks like. Model and product launches, major research results,
funding and acquisitions that shift the field, and consequential policy or
regulation all qualify. Rank by how much the item would change a practitioner's
plans — not by how much coverage it got. Skip opinion pieces, listicles,
speculation with no new facts, and stories where the only news is that someone
commented on an earlier story. Never include the same story twice from two
outlets.

Your entire reply is the digest itself. No preamble, no narration of how you
chose, no closing note about coverage or quota. Start at "TOPICS:" and stop
after the last KO line.

Output format — plain text, exactly this shape:

TOPICS: <3-6 short labels for what the day was about, separated by " · ">

**1. [<the article's own English headline>](<the article's url>)**
EN: <one or two sentences: what happened and why it matters>
KO: <the same, in Korean>

Rules:
- The topics line is read first and decides whether the rest gets read, so
  each label is a subject, not a category: "OpenAI teen safety", "Nvidia
  8GW datacentre", not "AI news" or "industry". Two or three words each.
- Copy the headline and URL exactly as given. Never invent or edit either.
  The headline goes inside the brackets and the URL inside the parentheses,
  as shown -- the reader sees a link, not a bare address.
- Two sentences at most, and one is often enough. This is read on a phone,
  and a screenful per item is what made the previous digest hard to scan.
  Say what happened and what it changes; leave the detail to the article.
- Work only from the supplied summaries. Where one is too thin, say what the
  article says and stop -- inventing specifics would be worse.
- Each summary is one line. Do not use line breaks inside a summary.
- The Korean line must read as Korean written by a person, not as a
  transliteration. Keep established English technical terms in English.
- Write the Korean in 격식체 — end sentences with -습니다 / -입니다. Never use
  the plain 해라체 (-했다, -이다) that news articles use.
- No markdown headers, no bullet characters, no numbered-list syntax beyond
  the bold number shown above.
- If fewer than {count} items clear the significance bar, return fewer.
  Padding the list is worse than a short list.\
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
    topics = ""
    found = TOPICS_LINE.search(text)
    if found:
        topics = _topics(found.group(1))

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

    # The EN:/KO: labels are scaffolding for the parsing above, not something
    # the reader needs; the flags say the same thing in one character. The two
    # summaries sit on consecutive lines now that they are a sentence or two
    # each -- the blank line between them was there to break up a paragraph
    # that no longer exists, and on a phone it doubled the height of the list.
    rendered: list[str] = []
    for line in lines:
        if line.startswith("EN:"):
            rendered.append(f"🇺🇸 {line[3:].lstrip()}")
        elif line.startswith("KO:"):
            rendered.append(f"🇰🇷 {line[3:].lstrip()}")
        else:
            rendered.append(line)

    body = "\n".join(rendered).strip()
    return f"{topics}\n\n{body}" if (topics and body) else body


def _topics(raw: str) -> str:
    """Render the day's subjects as chips above the items.

    The line exists to be read on its own: it answers "is there anything for
    me today" without scrolling five items, which is what the digest was
    failing at. Backticks are stripped rather than escaped -- a chip is one
    short label, and a stray backtick would swallow the rest of the line.
    """
    labels = [
        part.strip().strip("-—·,").replace("`", "")
        for part in re.split(r"[·|]|\s{2,}", raw)
    ]
    labels = [label for label in labels if label][:6]
    if not labels:
        return ""
    return "🏷️ " + "  ".join(f"`{label}`" for label in labels)


async def write_digest(
    api_key: str, day: date, entries: Sequence[Entry], count: int = 5
) -> str:
    """Write the digest for `day` from articles already collected.

    Raises NewsUnavailable rather than returning a partial digest: the caller
    stays quiet, which is the right outcome for a message nobody is waiting on.
    """
    from anthropic import AsyncAnthropic

    if not entries:
        raise NewsUnavailable(f"no articles published {day.isoformat()}")

    # No retries. A retry rewrites the whole digest at full price for a message
    # nobody is waiting on; failing once and staying quiet is cheaper.
    client = AsyncAnthropic(api_key=api_key, max_retries=0)
    article_list = "\n".join(entry.as_prompt_line() for entry in entries)
    try:
        # Streamed: the digest itself is thousands of output tokens, and a
        # non-streaming request that size sat on one connection until it hit
        # the SDK's ten-minute timeout -- which the SDK then retried twice,
        # billing one digest three times and delivering none of them.
        async with asyncio.timeout(TIMEOUT_SECONDS), client.messages.stream(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=SYSTEM.format(count=count, day=day.isoformat()),
            # Choosing five stories from a list and writing them up is a
            # judgement and language task, not a reasoning one.
            output_config={"effort": "low"},
            # No `fallbacks` here: Sonnet 5 rejects the parameter outright
            # (400), so a refusal just means no digest today -- which the
            # stop_reason check below turns into silence rather than a crash.
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Articles published {day.isoformat()} "
                        f"({len(entries)} total):\n\n{article_list}\n\n"
                        f"Write the digest."
                    ),
                }
            ],
        ) as stream:
            response = await stream.get_final_message()
    except TimeoutError as exc:
        raise NewsUnavailable(f"timed out after {TIMEOUT_SECONDS}s") from exc
    except Exception as exc:  # network, auth, rate limit
        raise NewsUnavailable(f"{type(exc).__name__}: {exc}") from exc
    finally:
        await client.close()

    log.info(
        "news digest: %s in, %s out",
        response.usage.input_tokens,
        response.usage.output_tokens,
    )

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
