"""Yesterday's AI stories, collected from a handful of publications' feeds.

This replaces letting the model search for the news. Search read well but was
unpredictable in the two ways that matter for a job that runs unattended every
morning: cost and completion. Every search result stayed in context and was
re-read on each following step, so one digest measured anywhere from 89k to
437k input tokens, and some mornings the call never finished at all -- on
2026-08-21 it hit the fifteen-minute ceiling and delivered nothing.

Feeds fix both by construction. The publication date comes from the feed, so
"yesterday" is a filter rather than an instruction the model has to honour;
every link is a real article rather than a "top AI news today" roundup; and
the input is a bounded list of headlines instead of whatever the searches
happened to drag in.

The cost is that this list has to be maintained: a feed that moves or dies
goes quiet rather than failing loudly, which is what `stale_feeds` is for.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

FEEDS: tuple[str, ...] = (
    "https://techcrunch.com/category/artificial-intelligence/feed/",
    "https://www.theverge.com/rss/ai-artificial-intelligence/index.xml",
    "https://arstechnica.com/ai/feed/",
    "https://venturebeat.com/category/ai/feed/",
    "https://www.technologyreview.com/topic/artificial-intelligence/feed",
)

# Per feed, so one prolific publication cannot crowd out the rest.
MAX_PER_FEED = 15
# Across all feeds. The whole list goes into the prompt, and past this it costs
# more than it adds.
MAX_TOTAL = 45

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


@dataclass(frozen=True)
class Entry:
    title: str
    url: str
    source: str
    published: datetime
    summary: str

    def as_prompt_line(self) -> str:
        text = f"- {self.title} [{self.source}]\n  {self.url}"
        if self.summary:
            text += f"\n  {self.summary}"
        return text


def _text(node: ElementTree.Element | None) -> str:
    if node is None or node.text is None:
        return ""
    return _WS.sub(" ", _TAG.sub(" ", node.text)).strip()


def _parse_date(raw: str) -> datetime | None:
    """Read either RSS (RFC 822) or Atom (ISO 8601) timestamps."""
    raw = raw.strip()
    if not raw:
        return None
    try:
        return parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_feed(xml: str, *, source: str) -> list[Entry]:
    """Pull entries out of an RSS or Atom document.

    Hand-rolled rather than a feed library: the two formats differ in about
    four tag names, and this way a malformed feed is a parse error here rather
    than a dependency to keep current.
    """
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError as exc:
        log.warning("could not parse feed from %s: %s", source, exc)
        return []

    atom = "{http://www.w3.org/2005/Atom}"
    nodes = root.iter("item")
    entries: list[Entry] = []
    for node in list(nodes) or []:
        published = _parse_date(_text(node.find("pubDate")))
        link = _text(node.find("link"))
        title = _text(node.find("title"))
        summary = _text(node.find("description"))
        if title and link and published:
            entries.append(Entry(title, link, source, published, summary[:400]))

    if not entries:  # Atom
        for node in root.iter(f"{atom}entry"):
            published = _parse_date(
                _text(node.find(f"{atom}published")) or _text(node.find(f"{atom}updated"))
            )
            link_node = node.find(f"{atom}link")
            link = link_node.get("href", "") if link_node is not None else ""
            title = _text(node.find(f"{atom}title"))
            summary = _text(node.find(f"{atom}summary")) or _text(
                node.find(f"{atom}content")
            )
            if title and link and published:
                entries.append(Entry(title, link, source, published, summary[:400]))

    return entries[:MAX_PER_FEED]


def _source_name(url: str) -> str:
    host = httpx.URL(url).host
    return host.removeprefix("www.").removeprefix("feeds.")


async def _fetch(client: httpx.AsyncClient, url: str) -> list[Entry]:
    try:
        response = await client.get(url, follow_redirects=True)
        response.raise_for_status()
    except Exception as exc:
        # One unreachable publication should not cost the whole digest.
        log.warning("feed unavailable: %s (%s)", url, type(exc).__name__)
        return []
    return parse_feed(response.text, source=_source_name(url))


async def entries_for(
    day: date, tz: ZoneInfo, *, feeds: tuple[str, ...] = FEEDS
) -> list[Entry]:
    """Every article published on `day`, in the reader's timezone.

    Feeds timestamp in their own zone; comparing the local calendar date is
    what makes "yesterday" mean the day the reader just had rather than a UTC
    window that clips the evening off it.
    """
    headers = {"User-Agent": "alliegent/1.0 (personal news digest)"}
    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        gathered = await asyncio.gather(*(_fetch(client, url) for url in feeds))

    seen: set[str] = set()
    matched: list[Entry] = []
    for entry in sorted(
        (e for batch in gathered for e in batch),
        key=lambda e: e.published,
        reverse=True,
    ):
        if entry.published.astimezone(tz).date() != day:
            continue
        key = entry.url.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        matched.append(entry)

    log.info(
        "feeds: %d articles published %s across %d feeds",
        len(matched),
        day.isoformat(),
        len(feeds),
    )
    return matched[:MAX_TOTAL]


def stale_feeds(entries: list[Entry], *, feeds: tuple[str, ...] = FEEDS) -> list[str]:
    """Feeds that contributed nothing, so a dead one can be noticed.

    A feed that moves or stops publishing simply goes quiet, and the digest
    stays plausible while quietly losing a publication.
    """
    heard = {entry.source for entry in entries}
    return [url for url in feeds if _source_name(url) not in heard]


def recent_window(today: date) -> date:
    """The day a morning digest should cover: the one that just finished."""
    return today - timedelta(days=1)
