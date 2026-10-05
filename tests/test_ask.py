"""Asking for an opinion: which channel's reports the model is given, what it
is told, and that a failing source cannot cost the answer. No model is called;
the client is a stand-in. Amounts are invented."""

from __future__ import annotations

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from alliegent import ask
from alliegent.config import Secrets

TODAY = date(2026, 10, 5)


@pytest.fixture(autouse=True)
def fresh_cache():
    """Reports are kept for a few minutes; one test's must not answer another's."""
    ask._cache.clear()


# -- which area a channel is -------------------------------------------------------


def secrets(**ids):
    return Secrets(discord_channel_id=1, **ids)


def test_a_channel_is_the_area_it_was_set_up_for():
    s = secrets(discord_build_channel_id=10, discord_assets_channel_id=20,
                discord_karrot_channel_id=30)
    assert ask.channel_kind(s, 10) == "build"
    assert ask.channel_kind(s, 20) == "assets"
    assert ask.channel_kind(s, 30) == "karrot"


def test_a_post_in_a_forum_is_that_forums_area():
    s = secrets(discord_build_channel_id=10)
    assert ask.channel_kind(s, 777, 10) == "build"   # the post, then its forum


def test_the_older_projects_channel_still_means_build():
    assert ask.channel_kind(secrets(discord_projects_channel_id=40), 40) == "build"


def test_an_unrecognised_channel_is_no_area():
    assert ask.channel_kind(secrets(discord_build_channel_id=10), 99) is None
    assert ask.channel_kind(secrets(), 1) is None   # a server on one shared channel


def test_review_sharing_the_agenda_channel_reads_as_agenda():
    s = secrets(discord_agenda_channel_id=50)
    assert ask.channel_kind(s, 50) == "agenda"


# -- what the model is given -----------------------------------------------------------


class Jobs:
    """Reports that say which one they are, so a test can see who was read."""

    def __init__(self, **broken):
        self.broken = set(broken)
        self.assets = None
        self.plans = None
        self.config = None

    async def _report(self, name):
        if name in self.broken:
            raise RuntimeError(name)
        return f"[{name}]"

    async def build_project_week(self):
        return await self._report("week")

    async def build_costs(self):
        return await self._report("costs")

    async def build_karrot_report(self):
        return await self._report("karrot")

    async def build_daily_brief(self):
        return await self._report("day")


async def test_build_is_given_the_projects_and_the_costs():
    assert await ask.gather_context(Jobs(), "build") == "[week]\n\n[costs]"


async def test_karrot_is_given_its_report():
    assert await ask.gather_context(Jobs(), "karrot") == "[karrot]"


async def test_the_agenda_and_unknown_channels_are_given_the_day():
    assert await ask.gather_context(Jobs(), "agenda") == "[day]"
    assert await ask.gather_context(Jobs(), None) == "[day]"


async def test_news_and_english_have_nothing_of_theirs_to_read():
    assert "no reports" in await ask.gather_context(Jobs(), "news")


async def test_a_failing_report_costs_the_answer_that_report_only_and_is_named():
    got = await ask.gather_context(Jobs(costs=True), "build")
    assert got == "[week]\n\n(Not available just now: costs)"


async def test_when_every_report_fails_the_model_is_told_so():
    got = await ask.gather_context(Jobs(week=True, costs=True), "build")
    assert got == "(Not available just now: project status, costs)"


async def test_the_context_is_capped():
    class Long(Jobs):
        async def build_daily_brief(self):
            return "x" * (ask.CONTEXT_LIMIT * 2)

    assert len(await ask.gather_context(Long(), "agenda")) == ask.CONTEXT_LIMIT


async def test_assets_are_given_the_snapshot_and_the_plan(monkeypatch):
    from alliegent import plans as plans_module
    from alliegent.assets import Snapshot

    class Assets:
        async def history(self):
            return [Snapshot("a", date(2026, 9, 21), {"Savings": 10}),
                    Snapshot("b", date(2026, 9, 28), {"Savings": 12})]

    async def standing(plans, assets, config):
        return "[plan]"

    monkeypatch.setattr(plans_module, "current_standing", standing)
    jobs = Jobs()
    jobs.assets, jobs.plans = Assets(), object()
    text = await ask.gather_context(jobs, "assets")
    assert "Savings" in text and text.endswith("[plan]")


# -- the model call -----------------------------------------------------------------------


class Client:
    def __init__(self, reply="Do it.", stop="end_turn"):
        self.calls = []
        self._reply, self._stop = reply, stop
        self.closed = False
        self.messages = SimpleNamespace(create=self._create)

    async def _create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(
            stop_reason=self._stop,
            content=[SimpleNamespace(type="text", text=self._reply)],
        )

    async def close(self):
        self.closed = True


async def asked(client, kind="build", question="Should I cancel it?"):
    return await ask.answer(
        question, "[costs]", api_key="k", today=TODAY, kind=kind, client=client
    )


async def test_the_question_goes_as_asked_and_the_reports_go_in_the_system_prompt():
    client = Client()
    assert await asked(client) == "Do it."
    [call] = client.calls
    assert call["messages"] == [{"role": "user", "content": "Should I cancel it?"}]
    assert "[costs]" in call["system"]
    assert "#build" in call["system"]
    assert "Monday" in call["system"]


async def test_the_model_has_no_tools_so_asking_can_never_change_a_row():
    client = Client()
    await asked(client)
    assert "tools" not in client.calls[0]


async def test_it_is_told_to_say_what_it_thinks_not_to_invent_figures_and_to_reply_in_english():
    client = Client()
    await asked(client)
    system = " ".join(client.calls[0]["system"].split())   # as one line
    assert "Start with what you think" in system
    assert "Do not invent a figure" in system
    assert "Always reply in English" in system


async def test_a_refusal_is_said_plainly():
    assert "can't help" in await asked(Client(stop="refusal"))


async def test_an_empty_reply_is_not_an_empty_message():
    assert await asked(Client(reply="")) == "…"


async def test_an_unrecognised_channel_is_not_named_in_the_prompt():
    client = Client()
    await asked(client, kind=None)
    assert "#None" not in client.calls[0]["system"]


async def test_it_is_told_to_talk_like_a_colleague_not_to_write_a_verdict_memo():
    """The first version came back as Verdict / Reasoning / Conclusion headings."""
    client = Client()
    await asked(client)
    system = " ".join(client.calls[0]["system"].split())
    assert "thoughtful colleague" in system
    assert 'No headings and no labels such as "Verdict"' in system


# -- slow reports and the cache ---------------------------------------------------------


class Slow(Jobs):
    """Costs never come back in time."""

    async def build_costs(self):
        await asyncio.sleep(10)
        return "[costs]"


async def test_a_slow_report_is_dropped_and_named_so_the_answer_still_goes_ahead():
    got = await ask.gather_context(Slow(), "build", timeout=0.05)
    assert got == "[week]\n\n(Not available just now: costs)"


async def test_the_reports_are_read_together_not_one_after_another():
    class Waits(Jobs):
        async def build_project_week(self):
            await asyncio.sleep(0.2)
            return "[week]"

        async def build_costs(self):
            await asyncio.sleep(0.2)
            return "[costs]"

    started = asyncio.get_running_loop().time()
    assert await ask.gather_context(Waits(), "build") == "[week]\n\n[costs]"
    assert asyncio.get_running_loop().time() - started < 0.35   # not 0.4


async def test_a_complete_set_is_reused_for_a_few_minutes():
    class Counting(Jobs):
        reads = 0

        async def build_daily_brief(self):
            Counting.reads += 1
            return "[day]"

    now = [1000.0]
    jobs = Counting()
    await ask.gather_context(jobs, "agenda", clock=lambda: now[0])
    now[0] += ask.CACHE_SECONDS - 1
    await ask.gather_context(jobs, "agenda", clock=lambda: now[0])
    assert Counting.reads == 1
    now[0] += 2   # past the window
    await ask.gather_context(jobs, "agenda", clock=lambda: now[0])
    assert Counting.reads == 2


async def test_each_area_keeps_its_own_reports():
    jobs = Jobs()
    assert await ask.gather_context(jobs, "karrot") == "[karrot]"
    assert await ask.gather_context(jobs, "agenda") == "[day]"


async def test_an_incomplete_set_is_not_kept_so_the_next_ask_tries_again():
    got = await ask.gather_context(Jobs(costs=True), "build")
    assert "Not available" in got
    assert await ask.gather_context(Jobs(), "build") == "[week]\n\n[costs]"
