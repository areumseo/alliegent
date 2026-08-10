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


def test_fmt_date_shows_weekday_and_day():
    assert reports.fmt_date(TODAY) == "Sat 8/8"


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


def test_day_list_can_omit_numbers():
    """Tomorrow's list is unnumbered — /done resolves against today."""
    text = reports.day_list(TODAY, [item("내일 할 것")], numbered=False)
    assert "내일 할 것" in text
    assert "`1.`" not in text


def test_day_list_reports_an_empty_day():
    assert "nothing scheduled" in reports.day_list(TODAY, [], numbered=False)


# -- status ----------------------------------------------------------------


def test_status_counts_today_and_the_week():
    todays = [item("a", done=True), item("b"), item("c")]
    week = [item("a", done=True), item("b"), item("c"), item("d", done=True)]
    text = reports.status(TODAY, todays, [], week)
    assert "Today — 1 of 3 done (33%)" in text
    assert "This week — 2 of 4 done (50%)" in text


def test_status_numbers_match_the_today_list():
    """The numbers must index the full day, not the pending subset — /done
    resolves them the way /today prints them."""
    todays = [item("done one", done=True), item("still open")]
    text = reports.status(TODAY, todays, [], todays)
    assert "`2.` still open" in text
    assert "`1.` still open" not in text


def test_status_celebrates_a_finished_day():
    todays = [item("a", done=True)]
    text = reports.status(TODAY, todays, [], todays)
    assert "Nothing left today" in text


def test_status_omits_overdue_when_there_is_none():
    text = reports.status(TODAY, [item("a")], [], [item("a")])
    assert "Overdue" not in text


def test_status_shows_overdue_count():
    text = reports.status(TODAY, [], [item("x"), item("y")], [])
    assert "Overdue — 2" in text


def test_status_handles_a_completely_empty_day():
    text = reports.status(TODAY, [], [], [])
    assert "nothing scheduled" in text
    assert "Nothing left today" not in text  # nothing was scheduled to finish


# -- numbering -------------------------------------------------------------
# Every message that numbers today's unfinished items has to number them the
# way /today prints them, because that is what /done and /delete resolve
# against. Numbering the pending subset from 1 makes "2" mean a different row
# depending on which message you happened to read it in.

MIXED = [item("first", done=True), item("second"), item("third", done=True), item("fourth")]


def test_the_brief_numbers_against_the_whole_day():
    text = reports.daily_brief(TODAY, MIXED, [], [])
    assert "`2.` second" in text
    assert "`4.` fourth" in text
    assert "`1.` second" not in text


def test_the_evening_alert_numbers_against_the_whole_day():
    text = reports.incomplete_alert(TODAY, MIXED, [])
    assert text is not None
    assert "`2.` second" in text
    assert "`4.` fourth" in text


def test_status_numbers_the_same_way():
    text = reports.status(TODAY, MIXED, [], MIXED)
    assert "`2.` second" in text
    assert "`4.` fourth" in text


def test_all_three_agree_on_the_numbers():
    """The real requirement: read a number anywhere, /done it safely."""
    brief = reports.daily_brief(TODAY, MIXED, [], [])
    alert = reports.incomplete_alert(TODAY, MIXED, [])
    stat = reports.status(TODAY, MIXED, [], MIXED)
    for line in reports.pending_lines(MIXED):
        assert line in brief and line in alert and line in stat


def test_daily_brief_handles_an_empty_day():
    text = reports.daily_brief(TODAY, [], [], [])
    assert "nothing scheduled" in text


def test_daily_brief_distinguishes_a_finished_day_from_an_empty_one():
    """Both leave nothing to list; telling someone who cleared the day that
    nothing was scheduled reads as the bot not having noticed."""
    text = reports.daily_brief(TODAY, [item("a", done=True), item("b", done=True)], [], [])
    assert "all 2 done" in text
    assert "nothing scheduled" not in text


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
    assert "Tue 8/11" in text and "Thu 8/13" in text
    assert text.count("Ballet 7:10PM") == 2


def test_weekly_review_computes_completion_rate():
    text = reports.weekly_review(
        date(2026, 8, 2),
        TODAY,
        [item("a", done=True), item("b", done=True), item("c"), item("d")],
    )
    assert "Done 2 of 4" in text
    assert "50%" in text


def test_weekly_review_groups_by_day():
    """A flat list of eighteen ticks says only that the week happened; the
    question a review answers is what each day held."""
    text = reports.weekly_review(
        date(2026, 8, 3),
        date(2026, 8, 9),
        [
            item("Karrot 5PM", done=True, day=date(2026, 8, 3)),
            item("Ballet", done=True, day=date(2026, 8, 4)),
            item("Diary", day=date(2026, 8, 4)),
        ],
    )
    assert "**Mon 8/3**" in text
    assert "**Tue 8/4**" in text
    assert text.index("Karrot 5PM") < text.index("Ballet")


def test_a_day_with_unfinished_items_shows_its_own_count():
    text = reports.weekly_review(
        date(2026, 8, 3),
        date(2026, 8, 9),
        [
            item("a", done=True, day=date(2026, 8, 4)),
            item("b", day=date(2026, 8, 4)),
        ],
    )
    assert "**Tue 8/4**  (1/2)" in text


def test_a_fully_finished_day_carries_no_count():
    """The tick marks already say it; a (3/3) beside them is noise."""
    text = reports.weekly_review(
        date(2026, 8, 3),
        date(2026, 8, 9),
        [item("a", done=True, day=date(2026, 8, 4))],
    )
    assert "**Tue 8/4**\n" in text
    assert "(1/1)" not in text


def test_days_with_nothing_on_them_are_skipped():
    text = reports.weekly_review(
        date(2026, 8, 3), date(2026, 8, 9), [item("a", day=date(2026, 8, 4))]
    )
    assert "8/5" not in text


def test_nothing_is_truncated():
    """A review that hides a third of the week defeats itself; long messages
    are chunked at send time instead."""
    items = [item(f"task {n}", done=True, day=date(2026, 8, 3)) for n in range(20)]
    text = reports.weekly_review(date(2026, 8, 3), date(2026, 8, 9), items)
    assert "more" not in text
    for n in range(20):
        assert f"task {n}" in text


def test_undated_items_are_kept_at_the_end():
    text = reports.weekly_review(
        date(2026, 8, 3),
        date(2026, 8, 9),
        [item("floating", day=None), item("dated", done=True, day=date(2026, 8, 4))],
    )
    assert "**No date**" in text
    assert text.index("dated") < text.index("floating")


def test_weekly_review_handles_an_empty_week():
    text = reports.weekly_review(date(2026, 8, 2), TODAY, [])
    assert "0%" in text


def test_weekly_planning_lists_empty_days():
    text = reports.weekly_planning(
        date(2026, 8, 10),
        [item("Ballet", day=date(2026, 8, 11))],
        [],
    )
    assert "1 item(s) scheduled" in text
    assert "Tue 8/11" not in text.split("Empty days")[1]


def test_weekly_planning_prompts_when_the_week_is_empty():
    text = reports.weekly_planning(date(2026, 8, 10), [], [])
    assert "Nothing scheduled for next week yet." in text


def test_weekly_planning_surfaces_overdue_carryover():
    text = reports.weekly_planning(
        date(2026, 8, 10), [], [item("밀린 것", day=date(2026, 8, 3))]
    )
    assert "Carrying over" in text and "밀린 것" in text


def test_stale_projects_is_silent_when_none():
    assert reports.stale_projects([]) is None


def test_stale_projects_distinguishes_no_activity_from_old_activity():
    text = reports.stale_projects(
        [
            (Project("a", "옛날 것", None, "", ""), date(2026, 7, 1)),
            (Project("b", "기록 없음", None, "", ""), None),
        ]
    )
    assert "last activity Wed 7/1" in text
    assert "no linked activity" in text


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
    with pytest.raises(ValueError, match="Couldn't read that date"):
        parse_day("다음주 언젠가", TODAY)
