"""Reading the channels for /ask: which are read, that #mood never is, and that
one channel failing costs that channel only. Discord is a stand-in."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import discord
import pytest

from alliegent import ask, channel_history
from alliegent.config import Secrets

KST = timezone(timedelta(hours=9))
NOW = datetime(2026, 11, 21, 15, 0, tzinfo=KST)


@pytest.fixture(autouse=True)
def fresh_cache():
    channel_history._cache.clear()


def msg(text, minutes_ago=10, author="Ally"):
    return SimpleNamespace(
        content=text,
        created_at=(NOW - timedelta(minutes=minutes_ago)).astimezone(UTC),
        author=SimpleNamespace(display_name=author),
    )


class Channel:
    def __init__(self, cid, name, messages=(), parent_id=None, fail=False):
        self.id, self.name, self.parent_id = cid, name, parent_id
        self.messages = list(messages)  # newest first, as Discord gives them
        self.fail = fail
        self.reads = 0

    def history(self, limit, after, oldest_first):
        self.reads += 1
        if self.fail:
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no access")

        async def gen():
            for m in self.messages[:limit]:
                if m.created_at > after:
                    yield m

        return gen()


class Bot:
    def __init__(self, *channels):
        self.channels = {c.id: c for c in channels}

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def fetch_channel(self, cid):
        raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "gone")


def test_mood_is_never_among_the_channels_read():
    s = Secrets(discord_agenda_channel_id=1, discord_build_channel_id=2,
                discord_mood_channel_id=9)
    assert 9 not in [cid for _, cid in channel_history.searchable_channels(s)]


def test_mood_is_left_out_even_when_it_shares_an_id_with_another_setting():
    s = Secrets(discord_channel_id=9, discord_mood_channel_id=9)
    assert channel_history.searchable_channels(s) == []


def test_a_shared_default_channel_is_read_once():
    s = Secrets(discord_channel_id=1, discord_build_channel_id=2)
    ids = [cid for _, cid in channel_history.searchable_channels(s)]
    assert sorted(ids) == [1, 2]


async def test_messages_come_headed_by_channel_oldest_first():
    s = Secrets(discord_agenda_channel_id=1, discord_build_channel_id=2)
    bot = Bot(
        Channel(1, "agenda", [msg("second", 5), msg("first", 50)]),
        Channel(2, "build", [msg("pricing stays at 9900", 30)]),
    )
    text = await channel_history.gather(bot, s, now=NOW)
    assert "## #agenda" in text and "## #build" in text
    assert text.index("first") < text.index("second")
    assert "Ally: pricing stays at 9900" in text


async def test_old_messages_are_not_read():
    s = Secrets(discord_agenda_channel_id=1)
    bot = Bot(Channel(1, "agenda", [msg("recent", 10), msg("ancient", 60 * 24 * 30)]))
    text = await channel_history.gather(bot, s, now=NOW)
    assert "recent" in text and "ancient" not in text


async def test_a_thread_under_mood_is_not_read():
    s = Secrets(discord_agenda_channel_id=1, discord_mood_channel_id=9)
    sneaky = Channel(1, "agenda", [msg("private")], parent_id=9)
    text = await channel_history.gather(Bot(sneaky), s, now=NOW)
    assert "private" not in text and sneaky.reads == 0


async def test_an_unreadable_channel_is_named_and_the_rest_still_read():
    s = Secrets(discord_agenda_channel_id=1, discord_build_channel_id=2)
    bot = Bot(Channel(1, "agenda", [msg("hello")]), Channel(2, "build", fail=True))
    text = await channel_history.gather(bot, s, now=NOW)
    assert "hello" in text
    assert "Could not read: #build" in text


async def test_a_busy_channel_keeps_its_newest_messages():
    s = Secrets(discord_agenda_channel_id=1)
    many = [msg(f"line {i:03d} " + "x" * 200, i) for i in range(150)]
    text = await channel_history.gather(Bot(Channel(1, "agenda", many)), s, now=NOW)
    assert "line 000" in text and "line 149" not in text
    assert len(text) <= channel_history.TOTAL_CHARS + 100


async def test_a_complete_read_is_reused_for_a_few_minutes():
    s = Secrets(discord_agenda_channel_id=1)
    ch = Channel(1, "agenda", [msg("hello")])
    clock = iter([0.0, 0.0, 60.0]).__next__
    await channel_history.gather(Bot(ch), s, now=NOW, clock=clock)
    await channel_history.gather(Bot(ch), s, now=NOW, clock=clock)
    assert ch.reads == 1


# -- what the model is told ------------------------------------------------------


class Client:
    def __init__(self):
        self.kwargs = None
        self.messages = self

    async def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(stop_reason="end_turn",
                               content=[SimpleNamespace(type="text", text="ok")])


async def test_the_messages_go_in_the_system_prompt_with_where_they_came_from():
    client = Client()
    await ask.answer("뭐라고 했지?", "(reports)", api_key="k", today=NOW.date(),
                     kind=None, history="## #build\n[11-21 14:30] Ally: 9900", client=client)
    system = client.kwargs["system"]
    assert "[11-21 14:30] Ally: 9900" in system
    assert "which channel" in system


async def test_without_messages_the_prompt_is_as_before():
    client = Client()
    await ask.answer("q", "(reports)", api_key="k", today=NOW.date(), kind=None, client=client)
    assert "Recent messages" not in client.kwargs["system"]
