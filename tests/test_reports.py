from __future__ import annotations

from datetime import date

import pytest

from alliegent import reports
from alliegent.agenda import AgendaItem, Project
from alliegent.integrations.discord_bot import parse_day

TODAY = date(2026, 8, 8)  # Saturday


def item(title, *, done=False, day=TODAY):
    return AgendaItem(
        id=title, title=title, day=day, status=None, done=done, url="", project_ids=()
    )


def test_fmt_date_uses_korean_weekday():
    assert reports.fmt_date(TODAY) == "8/8(토)"


def test_daily_brief_lists_pending_and_overdue():
    text = reports.daily_brief(
        TODAY,
        [item("오늘 할 일"), item("이미 한 것", done=True)],
        [item("밀린 것", day=date(2026, 8, 1))],
        [Project("p", "프로젝트", "In progress", "다음 단계", "")],
    )
    assert "오늘 할 일" in text
    assert "이미 한 것" not in text  # completed items aren't repeated back
    assert "밀린 것" in text
    assert "다음 단계" in text


def test_no_empty_checkbox_anywhere():
    """An unchecked box on every line of an all-unfinished list is pure noise."""
    brief = reports.daily_brief(TODAY, [item("남은 것")], [], [])
    today = reports.today_list(TODAY, [item("한 것", done=True), item("남은 것")])
    review = reports.weekly_review(
        date(2026, 8, 2), TODAY, [item("한 것", done=True), item("남은 것")]
    )
    planning = reports.weekly_planning(date(2026, 8, 10), [item("예정")], [])
    for text in (brief, today, review, planning):
        assert "⬜" not in text


def test_done_items_stay_marked_where_they_mix_with_pending():
    text = reports.today_list(TODAY, [item("한 것", done=True), item("남은 것")])
    assert "✅ 한 것" in text
    assert "✅ 남은 것" not in text


def test_daily_brief_handles_an_empty_day():
    text = reports.daily_brief(TODAY, [], [], [])
    assert "등록된 항목이 없습니다" in text


def test_incomplete_alert_is_silent_when_nothing_is_pending():
    assert reports.incomplete_alert(TODAY, [item("끝남", done=True)], []) is None


def test_incomplete_alert_reports_pending_work():
    text = reports.incomplete_alert(TODAY, [item("남은 것")], [])
    assert text is not None and "남은 것" in text


def test_week_scaffold_is_silent_when_nothing_was_created():
    assert reports.week_scaffold(date(2026, 8, 10), []) is None


def test_week_scaffold_groups_created_items_by_day():
    text = reports.week_scaffold(
        date(2026, 8, 10),
        [
            ("Ballet 7:10PM", date(2026, 8, 11), "Exercise"),
            ("Butfit 12:30PM", date(2026, 8, 13), "Exercise"),
            ("Ballet 7:10PM", date(2026, 8, 13), "Exercise"),
        ],
    )
    assert text is not None
    assert "8/11(화)" in text and "8/13(목)" in text
    assert text.count("Ballet 7:10PM") == 2


def test_weekly_review_computes_completion_rate():
    text = reports.weekly_review(
        date(2026, 8, 2),
        TODAY,
        [item("a", done=True), item("b", done=True), item("c"), item("d")],
    )
    assert "완료 2 / 전체 4건" in text
    assert "50%" in text


def test_weekly_review_handles_an_empty_week():
    text = reports.weekly_review(date(2026, 8, 2), TODAY, [])
    assert "0%" in text


def test_weekly_planning_lists_empty_days():
    text = reports.weekly_planning(
        date(2026, 8, 10),
        [item("Ballet", day=date(2026, 8, 11))],
        [],
    )
    assert "등록된 항목 1건" in text
    assert "8/11(화)" not in text.split("비어 있는 날")[1]


def test_weekly_planning_prompts_when_the_week_is_empty():
    text = reports.weekly_planning(date(2026, 8, 10), [], [])
    assert "아직 없습니다" in text


def test_weekly_planning_surfaces_overdue_carryover():
    text = reports.weekly_planning(
        date(2026, 8, 10), [], [item("밀린 것", day=date(2026, 8, 3))]
    )
    assert "넘어온 것" in text and "밀린 것" in text


def test_stale_projects_is_silent_when_none():
    assert reports.stale_projects([]) is None


def test_stale_projects_distinguishes_no_activity_from_old_activity():
    text = reports.stale_projects(
        [
            (Project("a", "옛날 것", None, "", ""), date(2026, 7, 1)),
            (Project("b", "기록 없음", None, "", ""), None),
        ]
    )
    assert "마지막 활동 7/1" in text
    assert "관련 활동 기록 없음" in text


# -- chunking --------------------------------------------------------------


def test_chunk_leaves_short_messages_alone():
    assert reports.chunk("짧은 메시지") == ["짧은 메시지"]


def test_chunk_splits_on_line_boundaries():
    message = "\n".join(f"line {i}" for i in range(500))
    parts = reports.chunk(message, limit=100)
    assert all(len(p) <= 100 for p in parts)
    assert "\n".join(parts) == message


def test_chunk_hard_splits_a_single_overlong_line():
    parts = reports.chunk("x" * 250, limit=100)
    assert [len(p) for p in parts] == [100, 100, 50]


# -- date parsing ----------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (None, date(2026, 8, 8)),
        ("", date(2026, 8, 8)),
        ("오늘", date(2026, 8, 8)),
        ("내일", date(2026, 8, 9)),
        ("모레", date(2026, 8, 10)),
        ("2026-12-25", date(2026, 12, 25)),
        ("12-25", date(2026, 12, 25)),
        ("12/25", date(2026, 12, 25)),
        ("  내일  ", date(2026, 8, 9)),
    ],
)
def test_parse_day(text, expected):
    assert parse_day(text, TODAY) == expected


def test_parse_day_rejects_garbage_with_a_helpful_message():
    with pytest.raises(ValueError, match="날짜를 이해하지 못했습니다"):
        parse_day("다음주 언젠가", TODAY)
