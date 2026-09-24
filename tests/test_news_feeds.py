"""Reading publication feeds.

These are the digest's only source of stories now, so the parsing has to
survive both feed formats and the ways a feed goes wrong: a publication that
stops publishing, one that returns something that isn't a feed at all, and the
timezone question of what "yesterday" means.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import httpx
import pytest
from httpx import ConnectError

from alliegent.integrations.news_feeds import (
    Entry,
    check,
    entries_for,
    parse_feed,
    recent_window,
    stale_feeds,
)

SEOUL = ZoneInfo("Asia/Seoul")

RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel>
  <item>
    <title>A model shipped</title>
    <link>https://example.com/model</link>
    <pubDate>Thu, 20 Aug 2026 13:00:35 +0000</pubDate>
    <description>&lt;p&gt;It has &lt;b&gt;features&lt;/b&gt;.&lt;/p&gt;</description>
  </item>
  <item>
    <title>Older news</title>
    <link>https://example.com/old</link>
    <pubDate>Tue, 18 Aug 2026 09:00:00 +0000</pubDate>
    <description>Yesterday's yesterday.</description>
  </item>
</channel></rss>
"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>An Atom story</title>
    <link href="https://verge.example/story"/>
    <published>2026-08-20T21:50:22+00:00</published>
    <summary>What happened.</summary>
  </entry>
</feed>
"""

# A feed that is alive and simply has nothing in it, which is not the same as
# a feed that has gone.
EMPTY_RSS = """<?xml version="1.0"?>
<rss version="2.0"><channel><title>Quiet</title></channel></rss>
"""


def test_rss_entries_are_read():
    entries = parse_feed(RSS, source="example.com")
    assert [e.title for e in entries] == ["A model shipped", "Older news"]
    assert entries[0].url == "https://example.com/model"


def test_html_is_stripped_from_the_summary():
    """Feed descriptions carry markup, and it would land in the prompt."""
    entries = parse_feed(RSS, source="example.com")
    assert entries[0].summary == "It has features ."


def test_atom_entries_are_read():
    """The Verge publishes Atom, where the link is an attribute, not text."""
    entries = parse_feed(ATOM, source="verge.example")
    assert entries[0].url == "https://verge.example/story"
    assert entries[0].published.year == 2026


def test_a_page_that_is_not_a_feed_yields_nothing():
    """A publication that starts serving an HTML error page shouldn't raise."""
    assert parse_feed("<html><body>Not a feed</body></html>", source="x.example") == []
    assert parse_feed("", source="x.example") == []


def test_entries_without_a_date_are_dropped():
    """An undated entry can't be placed in a day, and the day is the point."""
    xml = RSS.replace("<pubDate>Thu, 20 Aug 2026 13:00:35 +0000</pubDate>", "")
    assert [e.title for e in parse_feed(xml, source="x")] == ["Older news"]


def entry(url: str, published: datetime, source: str = "example.com") -> Entry:
    return Entry("T", url, source, published, "s")


async def test_only_the_requested_day_is_kept(monkeypatch):
    feed = {"https://a.example/feed": RSS}
    await _assert_titles(monkeypatch, feed, date(2026, 8, 20), ["A model shipped"])
    await _assert_titles(monkeypatch, feed, date(2026, 8, 18), ["Older news"])


async def test_the_day_is_the_readers_day_not_utc(monkeypatch):
    """22:00 UTC on the 20th is already the 21st in Seoul. Comparing UTC dates
    would file that story under the wrong day and drop it from the digest."""
    late = RSS.replace(
        "Thu, 20 Aug 2026 13:00:35 +0000", "Thu, 20 Aug 2026 22:00:00 +0000"
    )
    await _assert_titles(
        monkeypatch, {"https://a.example/feed": late}, date(2026, 8, 21), ["A model shipped"]
    )


async def test_the_same_story_from_two_feeds_appears_once(monkeypatch):
    feeds = {
        "https://a.example/feed": RSS,
        # Same article, reached with a tracking parameter.
        "https://b.example/feed": RSS.replace(
            "https://example.com/model", "https://example.com/model?utm_source=b"
        ),
    }
    entries = await _collect(monkeypatch, feeds, date(2026, 8, 20))
    assert len(entries) == 1


async def test_one_dead_feed_does_not_cost_the_others(monkeypatch):
    """A publication being down should cost the digest that publication."""
    feeds = {"https://a.example/feed": RSS, "https://dead.example/feed": None}
    entries = await _collect(monkeypatch, feeds, date(2026, 8, 20))
    assert [e.title for e in entries] == ["A model shipped"]


def test_stale_feeds_names_the_silent_ones():
    entries = [entry("https://x", datetime.now(SEOUL), source="a.example")]
    quiet = stale_feeds(entries, feeds=("https://a.example/f", "https://b.example/f"))
    assert quiet == ["https://b.example/f"]


def test_the_window_is_the_day_that_just_finished():
    assert recent_window(date(2026, 8, 21)) == date(2026, 8, 20)


# -- the health check ------------------------------------------------------
# A silent publication reads the same in the morning log however it went
# quiet. Telling a moved feed from a blocked one from a slow news day is the
# whole point of checking on demand.


async def test_check_reports_a_working_feed_with_its_newest_story(monkeypatch):
    health = await _check(monkeypatch, {"https://a.example/feed": RSS})
    assert health[0].ok
    assert health[0].entries == 2
    assert health[0].newest == datetime(2026, 8, 20, 13, 0, 35, tzinfo=UTC)


async def test_check_names_the_status_a_refusing_feed_returned(monkeypatch):
    """403 and 404 need different answers — one is the user agent, the other
    is a URL that has moved — so the number itself has to survive."""
    health = await _check(monkeypatch, {"https://a.example/feed": 403})
    assert not health[0].ok
    assert health[0].detail == "HTTP 403"


async def test_check_names_the_error_when_the_host_never_answers(monkeypatch):
    health = await _check(monkeypatch, {"https://a.example/feed": ConnectError})
    assert health[0].detail == "ConnectError"


async def test_check_tells_an_empty_feed_from_a_broken_one(monkeypatch):
    """A feed that parses but carries nothing is still a feed, and saying so
    is what stops a quiet publication being mistaken for a dead one."""
    health = await _check(monkeypatch, {"https://a.example/feed": EMPTY_RSS})
    assert health[0].detail == "no entries"


# -- helpers ---------------------------------------------------------------


async def _collect(monkeypatch, feeds: dict[str, str | None], day: date):
    def handler(request: httpx.Request) -> httpx.Response:
        body = feeds[str(request.url)]
        if body is None:
            return httpx.Response(503)
        return httpx.Response(200, text=body)

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    return await entries_for(day, SEOUL, feeds=tuple(feeds))


async def _check(monkeypatch, feeds: dict[str, object]):
    """Run `check` against canned responses.

    A value is the feed body, an HTTP status to return instead, or an
    exception class to raise — the three ways a publication goes quiet.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        outcome = feeds[str(request.url)]
        if isinstance(outcome, type) and issubclass(outcome, Exception):
            raise outcome("no route to host")
        if isinstance(outcome, int):
            return httpx.Response(outcome)
        return httpx.Response(200, text=outcome)

    transport = httpx.MockTransport(handler)
    real = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    return await check(feeds=tuple(feeds))


async def _assert_titles(monkeypatch, feeds, day, expected):
    entries = await _collect(monkeypatch, feeds, day)
    assert [e.title for e in entries] == expected


@pytest.mark.parametrize("raw", ["not a date", ""])
def test_unparseable_dates_do_not_raise(raw):
    xml = RSS.replace("Thu, 20 Aug 2026 13:00:35 +0000", raw)
    parse_feed(xml, source="x")  # must not raise
