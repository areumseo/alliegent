"""Inferring a new item's category from how the same activity was filed before.

History-based rather than a model call: "Karrot" meaning Work is a fact about
this person's past entries, not something to reason about.
"""

from __future__ import annotations

from datetime import date

from alliegent.agenda import AgendaService, normalise_title
from alliegent.config import Config

from .conftest import FakeNotionClient, make_page

DS = "ds_agenda-db"
TODAY = date(2026, 8, 10)


def service(pages=None):
    client = FakeNotionClient({DS: pages or []})
    return client, AgendaService(client, Config(), "agenda-db")


# -- title normalisation ---------------------------------------------------


def test_the_clock_time_is_dropped():
    """Titles carry their time, so the raw string never repeats."""
    assert normalise_title("Karrot 6PM") == normalise_title("Karrot 11AM")
    assert normalise_title("Ballet 7:10PM") == normalise_title("Ballet 8PM")


def test_a_suffix_still_distinguishes_two_activities():
    """"BC English 11AM — Prep" is preparation, not the lesson."""
    assert normalise_title("BC English 11AM — Prep") != normalise_title("BC English 8PM")
    assert normalise_title("BC English 11AM — Prep") == normalise_title(
        "BC English 8:30PM — Prep"
    )


def test_case_and_punctuation_do_not_matter():
    assert normalise_title("Claude Code - Agent") == normalise_title("claude code agent")


def test_korean_titles_survive_normalisation():
    assert normalise_title("가짜연구소 지원") == "가짜연구소 지원"


def test_a_title_that_is_only_a_time_normalises_to_nothing():
    assert normalise_title("7:10PM") == ""


# -- inference -------------------------------------------------------------


async def test_the_category_comes_from_the_same_activity_before():
    _, svc = service(
        [make_page("p1", "Karrot 5PM", day="2026-08-01", category="Work")]
    )
    assert await svc.guess_category("Karrot 6:45PM", TODAY) == "Work"


async def test_an_unseen_activity_gets_no_category():
    """Better an empty Category than a confident wrong one, which stays
    invisible until it skews a filter."""
    _, svc = service([make_page("p1", "Ballet", day="2026-08-01", category="Exercise")])
    assert await svc.guess_category("Dentist", TODAY) is None


async def test_the_most_common_past_category_wins():
    _, svc = service(
        [
            make_page("p1", "Karrot 5PM", day="2026-08-01", category="Work"),
            make_page("p2", "Karrot 6PM", day="2026-08-02", category="Work"),
            make_page("p3", "Karrot 7PM", day="2026-08-03", category="Personal"),
        ]
    )
    assert await svc.guess_category("Karrot 11AM", TODAY) == "Work"


async def test_past_items_with_no_category_are_ignored():
    _, svc = service(
        [
            make_page("p1", "Ballet 7PM", day="2026-08-01", category=None),
            make_page("p2", "Ballet 8PM", day="2026-08-02", category="Exercise"),
        ]
    )
    assert await svc.guess_category("Ballet 7:10PM", TODAY) == "Exercise"


async def test_nothing_in_history_at_all():
    _, svc = service()
    assert await svc.guess_category("Anything", TODAY) is None


# -- add_item integration --------------------------------------------------


async def test_add_applies_the_inferred_category():
    client, svc = service(
        [make_page("p1", "Karrot 5PM", day="2026-08-01", category="Work")]
    )
    await svc.add_item("Karrot 9PM", TODAY, infer_category=True)
    _, props = client.created[0]
    assert props["Category"] == {"select": {"name": "Work"}}


async def test_an_explicit_category_is_never_overridden():
    client, svc = service(
        [make_page("p1", "Karrot 5PM", day="2026-08-01", category="Work")]
    )
    await svc.add_item("Karrot 9PM", TODAY, category="Personal", infer_category=True)
    _, props = client.created[0]
    assert props["Category"] == {"select": {"name": "Personal"}}


async def test_inference_is_opt_in():
    """The scaffolding job passes categories through from its template and
    must not have them second-guessed."""
    client, svc = service(
        [make_page("p1", "Karrot 5PM", day="2026-08-01", category="Work")]
    )
    await svc.add_item("Karrot 9PM", TODAY)
    _, props = client.created[0]
    assert "Category" not in props


async def test_no_category_property_means_no_lookup():
    """A workspace without a Category column shouldn't pay for the query."""
    from alliegent.config import Config as C

    config = C()
    config.agenda.props.category = ""
    client = FakeNotionClient({DS: []})
    svc = AgendaService(client, config, "agenda-db")
    assert await svc.guess_category("Karrot", TODAY) is None
