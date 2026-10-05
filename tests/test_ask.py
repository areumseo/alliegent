"""Asking for an opinion: which channel's reports the model is given, what it
is told, and that a failing source cannot cost the answer. No model is called;
the client is a stand-in. Amounts are invented."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from alliegent import ask
from alliegent.config import Secrets

TODAY = date(2026, 10, 5)


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


async def test_a_failing_report_costs_the_answer_that_report_only():
    assert await ask.gather_context(Jobs(costs=True), "build") == "[week]"


async def test_when_every_report_fails_the_model_is_told_there_are_none():
    got = await ask.gather_context(Jobs(week=True, costs=True), "build")
    assert "no reports" in got


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


async def test_it_is_told_to_give_a_view_not_to_invent_figures_and_to_reply_in_english():
    client = Client()
    await asked(client)
    system = " ".join(client.calls[0]["system"].split())   # as one line
    assert "Give a view first" in system
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
