from __future__ import annotations

from datetime import date

from alliegent.agenda import AgendaService
from alliegent.chat import TOOLS, ChatAgent, strip_mentions
from alliegent.config import Config

from .conftest import FakeNotionClient, make_page

DS = "ds_agenda-db"
TODAY = date(2026, 8, 9)


def build(pages=None):
    client = FakeNotionClient({DS: pages or []})
    config = Config()
    agent = ChatAgent("sk-test", AgendaService(client, config, "agenda-db"), config)
    agent._today = lambda: TODAY  # pinned so tests don't drift with the calendar
    return agent, client


# -- mention stripping -----------------------------------------------------


def test_mention_is_removed_so_the_model_sees_the_request():
    assert strip_mentions("<@123> 내일 뭐 있지?", 123) == "내일 뭐 있지?"


def test_nickname_mention_form_is_removed_too():
    assert strip_mentions("<@!123> hello", 123) == "hello"


def test_a_bare_mention_leaves_nothing_to_answer():
    assert strip_mentions("<@123>", 123) == ""


def test_other_peoples_mentions_survive():
    assert strip_mentions("<@123> ask <@456> about it", 123) == "ask <@456> about it"


# -- tools -----------------------------------------------------------------


async def test_list_agenda_reports_status_per_item():
    agent, _ = build(
        [
            make_page("p1", "보고서", day="2026-08-09", status="Done"),
            make_page("p2", "회의 준비", day="2026-08-09", status="Not started"),
        ]
    )
    out = await agent._run_tool("list_agenda", {"day": "2026-08-09"})
    assert "보고서 [done]" in out
    assert "회의 준비 [not done]" in out


async def test_list_agenda_says_so_when_the_day_is_empty():
    agent, _ = build()
    assert "No items" in await agent._run_tool("list_agenda", {"day": "2026-08-09"})


async def test_add_item_writes_and_confirms():
    agent, client = build()
    out = await agent._run_tool("add_item", {"title": "장보기", "day": "2026-08-10"})
    assert client.created
    assert "장보기" in out and "2026-08-10" in out


async def test_complete_item_marks_the_matching_row():
    agent, client = build([make_page("p1", "Diary", day="2026-08-09", status="Not started")])
    out = await agent._run_tool(
        "complete_item", {"title": "Diary", "day": "2026-08-09"}
    )
    assert client.updated
    assert "Diary" in out


async def test_complete_item_lists_candidates_when_the_title_is_wrong():
    """The model should be able to recover, so the miss explains itself."""
    agent, client = build([make_page("p1", "Diary", day="2026-08-09")])
    out = await agent._run_tool(
        "complete_item", {"title": "다이어리", "day": "2026-08-09"}
    )
    assert client.updated == []
    assert "'Diary'" in out


async def test_completing_something_already_done_is_not_an_error():
    agent, client = build([make_page("p1", "Diary", day="2026-08-09", status="Done")])
    out = await agent._run_tool("complete_item", {"title": "Diary", "day": "2026-08-09"})
    assert client.updated == []
    assert "already done" in out


async def test_a_bad_date_comes_back_as_text_not_an_exception():
    """Tool failures are returned to Claude so it can ask, rather than
    crashing the whole reply."""
    agent, _ = build()
    out = await agent._run_tool("list_agenda", {"day": "tomorrow"})
    assert "failed" in out.lower()


async def test_unknown_tool_is_reported_rather_than_raising():
    agent, _ = build()
    assert "Unknown tool" in await agent._run_tool("delete_everything", {})


# -- tool definitions ------------------------------------------------------


async def test_every_declared_tool_is_implemented():
    """A declared tool with no branch comes back as "Unknown tool" at runtime,
    which the model can do nothing useful with."""
    agent, _ = build()
    for tool in TOOLS:
        out = await agent._run_tool(tool["name"], {})
        assert "Unknown tool" not in out, tool["name"]


def test_tools_declare_their_required_arguments():
    by_name = {t["name"]: t for t in TOOLS}
    for name in ("add_item", "complete_item", "delete_item"):
        assert by_name[name]["input_schema"]["required"] == ["title", "day"]


def test_nothing_can_change_a_date():
    """Rescheduling exists on the service but is deliberately not exposed:
    silently moving an item is hard to notice and hard to undo, unlike a
    trashed row that Notion keeps."""
    assert not any("resched" in t["name"] or "move" in t["name"] for t in TOOLS)


# -- delete ----------------------------------------------------------------


async def test_delete_trashes_the_matching_row():
    agent, client = build([make_page("p1", "장보기", day="2026-08-09")])
    out = await agent._run_tool("delete_item", {"title": "장보기", "day": "2026-08-09"})
    assert client.trashed == ["p1"]
    assert "trash" in out.lower()


async def test_delete_leaves_the_row_alone_when_the_title_is_wrong():
    agent, client = build([make_page("p1", "장보기", day="2026-08-09")])
    await agent._run_tool("delete_item", {"title": "쇼핑", "day": "2026-08-09"})
    assert client.trashed == []


async def test_delete_and_complete_are_not_interchangeable():
    """Same matching logic, different outcomes — a mix-up would either lose a
    row or fail to record work as done."""
    agent, client = build([make_page("p1", "Diary", day="2026-08-09")])
    await agent._run_tool("complete_item", {"title": "Diary", "day": "2026-08-09"})
    assert client.updated and client.trashed == []
