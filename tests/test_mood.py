from __future__ import annotations

import sqlite3
import stat
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from alliegent.agenda import AgendaService
from alliegent.config import Config, Secrets
from alliegent.integrations.discord_bot import AlliegentBot
from alliegent.jobs import Jobs
from alliegent.mood import (
    NOTE_MAX,
    OFFER,
    PROMPT,
    SAFETY_LOW,
    SAFETY_NOW,
    TALK_NOTE_MAX,
    Checkin,
    MoodStore,
    Reply,
    VoiceUnavailable,
    checkin_day,
    confirmation,
    due_notice,
    low_runs,
    mentions_crisis,
    parse_reply,
    parse_score,
    talk_summary,
    week_report,
)

from .conftest import FakeNotionClient

DAY = date(2026, 10, 18)


@pytest.fixture
def store(tmp_path):
    return MoodStore(tmp_path / "private" / "mood.db")


def test_file_and_directory_are_owner_only(store):
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700


def test_existing_file_is_narrowed_to_owner_only(tmp_path):
    path = tmp_path / "mood.db"
    path.touch(mode=0o644)
    MoodStore(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_upsert_records_the_day(store):
    entry = store.upsert(DAY, 3, "slept badly")
    assert (entry.day, entry.score, entry.note) == (DAY, 3, "slept badly")
    assert store.get(DAY) == entry


def test_second_checkin_overwrites_the_score(store):
    store.upsert(DAY, 2, "tired")
    store.upsert(DAY, 4)
    entry = store.get(DAY)
    assert entry.score == 4
    # Re-scoring without a note keeps the one already written.
    assert entry.note == "tired"


def test_second_note_replaces_the_first(store):
    store.upsert(DAY, 2, "tired")
    store.upsert(DAY, 2, "better after a walk")
    assert store.get(DAY).note == "better after a walk"


def test_note_is_trimmed_and_capped(store):
    assert store.upsert(DAY, 3, "   ").note is None
    assert len(store.upsert(DAY, 3, "x" * 500).note) == NOTE_MAX


def test_out_of_range_score_is_refused(store):
    with pytest.raises(ValueError):
        store.upsert(DAY, 6)
    assert store.get(DAY) is None


def test_between_is_inclusive_and_ordered(store):
    store.upsert(date(2026, 10, 17), 2)
    store.upsert(date(2026, 10, 15), 4)
    store.upsert(DAY, 3)
    days = [c.day for c in store.between(date(2026, 10, 15), date(2026, 10, 17))]
    assert days == [date(2026, 10, 15), date(2026, 10, 17)]


@pytest.mark.parametrize(
    ("text", "score"),
    [("3", 3), (" 5 ", 5), ("😊", 4), ("🙂️", 3), ("😭", 1)],
)
def test_scores_from_digits_and_faces(text, score):
    assert parse_score(text) == score


@pytest.mark.parametrize("text", ["0", "6", "3 hours of sleep", "", "good", "🍕"])
def test_anything_else_is_not_a_score(text):
    assert parse_score(text) is None


def test_confirmation_does_not_echo_the_note(store):
    line = confirmation(store.upsert(DAY, 3, "something private"))
    assert line == "✅ Sun 18 Oct: 3/5. Note saved."
    assert "private" not in line


def test_mood_channel_never_falls_back():
    secrets = Secrets(discord_channel_id=123)
    with pytest.raises(RuntimeError):
        secrets.channel_for("mood")
    assert Secrets(discord_channel_id=123, discord_mood_channel_id=9).channel_for("mood") == 9


def test_relative_db_path_is_under_the_repo():
    path = Config().mood.resolved_path
    assert path.parts[-2:] == ("data", "mood.db")
    assert path.is_absolute()


# -- step 2: the daily question and replies in #mood ---------------------------

MOOD_CHANNEL = 77


@pytest.mark.parametrize(
    ("text", "reply"),
    [
        ("4", Reply(score=4)),
        ("😞", Reply(score=1)),
        ("2 slept badly", Reply(score=2, note="slept badly")),
        ("3, ok", Reply(score=3, note="ok")),
        ("long day at work", Reply(note="long day at work")),
        ("3 hours of sleep", Reply(score=3, note="hours of sleep")),
        ("first line\nsecond line", Reply(raw_text="first line\nsecond line")),
        ("x" * 300, Reply(raw_text="x" * 300)),
        ("2 " + "y" * 300, Reply(score=2, raw_text="y" * 300)),
        ("   ", Reply()),
    ],
)
def test_parse_reply(text, reply):
    assert parse_reply(text) == reply


@pytest.mark.parametrize(
    ("hour", "day"), [(0, date(2026, 10, 17)), (3, date(2026, 10, 17)), (4, DAY), (21, DAY)]
)
def test_replies_after_midnight_belong_to_the_evening_before(hour, day):
    assert checkin_day(datetime(2026, 10, 18, hour, 30)) == day


def test_a_score_after_a_long_entry_keeps_the_entry(store):
    store.record(DAY, raw_text="a long day\nwith a lot in it")
    entry = store.record(DAY, score=2)
    assert entry.score == 2
    assert store.has_raw(DAY)


def test_answering_marks_the_prompt_answered(store):
    store.log_prompt(DAY, "daily")
    store.record(DAY, score=3)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT answered FROM prompt_log").fetchone()[0] == 1


def _jobs(store):
    sent = []

    async def notify(message, kind):
        sent.append((message, kind))

    client = FakeNotionClient()
    config = Config()
    jobs = Jobs(
        AgendaService(client, config, "agenda-db"),
        None,
        config,
        notify,
        clock=lambda: DAY,
        mood=store,
    )
    return jobs, sent


async def test_daily_question_goes_to_mood_once(store):
    jobs, sent = _jobs(store)
    await jobs.run_mood_checkin()
    await jobs.run_mood_checkin()
    assert sent == [(PROMPT, "mood")]
    assert store.prompted(DAY, "daily")


async def test_no_question_on_a_day_already_checked_in(store):
    jobs, sent = _jobs(store)
    store.upsert(DAY, 4)
    await jobs.run_mood_checkin()
    assert sent == []


async def test_no_question_without_the_feature():
    jobs, sent = _jobs(None)
    await jobs.run_mood_checkin()
    assert sent == []


def _bot(store):
    client = FakeNotionClient()
    config = Config()
    return AlliegentBot(
        config=config,
        agenda=AgendaService(client, config, "agenda-db"),
        projects=None,
        secrets=Secrets(discord_channel_id=1, discord_mood_channel_id=MOOD_CHANNEL),
        mood=store,
    )


def _message(content, attachments=(), channel=MOOD_CHANNEL):
    return SimpleNamespace(
        content=content,
        attachments=list(attachments),
        author=SimpleNamespace(bot=False),
        channel=SimpleNamespace(id=channel),
        reply=AsyncMock(),
        mention_everyone=False,
    )


async def test_mood_channel_needs_message_content_without_an_api_key(store):
    assert _bot(store).intents.message_content


async def test_a_mood_message_is_recorded_and_not_echoed(store, monkeypatch):
    monkeypatch.setattr("alliegent.mood.checkin_day", lambda now: DAY)
    bot = _bot(store)
    message = _message("2 couldn't focus at all")
    await bot.on_message(message)
    entry = store.get(DAY)
    assert (entry.score, entry.note) == (2, "couldn't focus at all")
    line = message.reply.await_args.args[0]
    assert line == "✅ Sun 18 Oct: 2/5. Note saved."
    assert "focus" not in line


async def test_a_long_entry_is_kept_raw_and_asks_for_nothing(store, monkeypatch):
    monkeypatch.setattr("alliegent.mood.checkin_day", lambda now: DAY)
    bot = _bot(store)
    message = _message("a lot happened\nand I am tired")
    await bot.on_message(message)
    assert store.get(DAY).note is None
    assert store.has_raw(DAY)
    assert (
        message.reply.await_args.args[0] == "📝 Sun 18 Oct saved. Send 1-5 or a face to score it."
    )


async def test_messages_elsewhere_are_not_check_ins(store):
    bot = _bot(store)
    await bot.on_message(_message("3", channel=5))
    assert store.get(DAY) is None


class _Typing:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


async def test_a_voice_memo_is_transcribed_into_the_raw_entry(store, monkeypatch):
    monkeypatch.setattr("alliegent.mood.checkin_day", lambda now: DAY)
    monkeypatch.setattr("alliegent.mood.transcribe", lambda path, model: "오늘 좀 지쳤어")
    bot = _bot(store)
    memo = SimpleNamespace(content_type="audio/ogg", filename="voice-message.ogg", save=AsyncMock())
    message = _message("", [memo])
    message.channel.typing = _Typing
    await bot.on_message(message)
    entry = store.get(DAY)
    assert entry.note is None
    assert store.has_raw(DAY)
    assert "지쳤" not in message.reply.await_args.args[0]


async def test_voice_without_mlx_says_so(store, monkeypatch):
    def unavailable(path, model):
        raise VoiceUnavailable("no mlx here")

    monkeypatch.setattr("alliegent.mood.transcribe", unavailable)
    bot = _bot(store)
    memo = SimpleNamespace(content_type="audio/ogg", filename="memo.ogg", save=AsyncMock())
    message = _message("", [memo])
    message.channel.typing = _Typing
    await bot.on_message(message)
    assert store.get(DAY) is None
    assert "Could not transcribe" in message.reply.await_args.args[0]


# -- step 3: the week ------------------------------------------------------------


def _entry(day, score, note=None):
    return Checkin(day=day, score=score, note=note, summary=None, keywords=None)


def test_week_report_lists_every_day_and_the_average():
    end = DAY
    entries = [
        _entry(date(2026, 10, 13), 2, "회사에서 회의가 길었다"),
        _entry(date(2026, 10, 15), 4, "운동 후 개운"),
        _entry(date(2026, 10, 17), 3, "회사 일이 많음, 운동"),
    ]
    agenda = {date(2026, 10, 13): (1, 4), date(2026, 10, 15): (3, 3)}
    text = week_report(end, entries, before=[_entry(date(2026, 10, 8), 4)], agenda=agenda)
    assert "Mon 10/12 to Sun 10/18" in text
    assert "Average 3.0 over 3 days (the week before: 4.0)." in text
    assert "Agenda done: 25% on days scored 1-2, 100% on days scored 4-5." in text
    assert "회사 (2)" in text and "운동 (2)" in text
    # Seven rows, scored or not.
    assert text.count("\n") >= 10
    assert "3 ###" in text


def test_week_report_leaves_raw_entries_out():
    entries = [
        Checkin(day=DAY, score=2, note=None, summary=None, keywords=None),
    ]
    # Raw text is not even a field on a Checkin: the report cannot quote it.
    assert "raw" not in Checkin.__dataclass_fields__
    assert week_report(DAY, entries) is not None


def test_week_report_is_silent_without_check_ins():
    assert week_report(DAY, []) is None


def test_week_report_drops_the_agenda_column_when_unreadable():
    text = week_report(DAY, [_entry(DAY, 3)], agenda=None)
    assert "Done" not in text
    assert "Agenda done" not in text


async def test_mood_week_job_posts_to_mood(store):
    jobs, sent = _jobs(store)
    store.upsert(DAY, 3)
    await jobs.run_mood_week()
    assert len(sent) == 1 and sent[0][1] == "mood"


async def test_mood_week_job_is_silent_on_an_empty_week(store):
    jobs, sent = _jobs(store)
    await jobs.run_mood_week()
    assert sent == []


# -- step 4: /talk ---------------------------------------------------------------


def test_low_runs_need_consecutive_scored_days():
    entries = [
        _entry(date(2026, 10, 10), 2),
        _entry(date(2026, 10, 11), 1),
        _entry(date(2026, 10, 12), 2),
        _entry(date(2026, 10, 13), 2),
        _entry(date(2026, 10, 14), 4),
        _entry(date(2026, 10, 15), 1),
        _entry(date(2026, 10, 16), 1),
        # 10/17 missing: the second stretch is only two days long.
        _entry(date(2026, 10, 18), 1),
    ]
    assert low_runs(entries) == [(date(2026, 10, 10), date(2026, 10, 13))]


def test_talk_summary_is_one_block_without_raw_entries(store):
    store.record(date(2026, 10, 6), score=4, note="좋은 하루")
    store.record(date(2026, 10, 14), score=2, note="회사 스트레스")
    store.record(date(2026, 10, 15), score=2, raw_text="SECRET long entry\nwith lines")
    store.record(DAY, score=1, note="잠을 못 잠 ```")
    text = talk_summary(DAY, store.between(date(2026, 10, 5), DAY))
    assert text.startswith("```\n") and text.endswith("\n```")
    assert text.count("```") == 2
    assert "SECRET" not in text
    assert "10/5 to 10/18" in text
    assert "Changes of 2 or more: 10/6 4 -> 10/14 2" in text
    assert "Scored 2 or lower every day 10/14 to 10/15" not in text  # two days only
    assert "- 10/18 (1): 잠을 못 잠" in text
    assert text.index("10/18 (1)") < text.index("10/14 (2)")


def test_talk_summary_cuts_long_notes():
    text = talk_summary(DAY, [_entry(DAY, 3, "x" * NOTE_MAX)])
    assert "x" * TALK_NOTE_MAX not in text
    assert "x" * (TALK_NOTE_MAX - 1) + "…" in text


# -- step 5: noticing --------------------------------------------------------------


def _days(store, scores, end=DAY):
    """Score consecutive days ending at `end`, oldest first."""
    for offset, score in enumerate(reversed(scores)):
        store.upsert(end - timedelta(days=offset), score)


@pytest.mark.parametrize(
    "text",
    [
        "죽고 싶다",
        "그냥 죽고싶어",
        "자해했어",
        "사라지고 싶어",
        "I want to die",
        "self-harm",
        "suicidal",
    ],
)
def test_crisis_phrases_are_noticed(text):
    assert mentions_crisis(text)


@pytest.mark.parametrize("text", ["일 빨리 끝내고 싶다", "죽을 만큼 피곤", "dead tired", "3"])
def test_ordinary_tiredness_is_not_a_crisis(text):
    assert not mentions_crisis(text)


def test_three_low_days_offer_talk_once_a_week(store):
    _days(store, [4, 2, 1, 2])
    assert due_notice(store, DAY) == ("talk_offer", OFFER)
    store.log_prompt(DAY, "talk_offer")
    _days(store, [2], end=DAY + timedelta(days=1))
    assert due_notice(store, DAY + timedelta(days=1)) is None
    _days(store, [2] * 7, end=DAY + timedelta(days=7))
    # Seven days after the offer the cooldown is over -- and the stretch is
    # now long enough that it points to people rather than to /talk.
    assert due_notice(store, DAY + timedelta(days=7))[0] == "safety"


def test_two_low_days_are_not_enough(store):
    _days(store, [4, 2, 2])
    assert due_notice(store, DAY) is None


def test_a_week_of_low_days_points_to_people(store):
    _days(store, [2, 1, 2, 2, 1, 2, 2])
    assert due_notice(store, DAY) == ("safety", SAFETY_LOW)


def test_three_unanswered_questions_offer_talk_before_the_fourth(store):
    for back in (3, 2, 1):
        store.log_prompt(DAY - timedelta(days=back), "daily")
    assert due_notice(store, DAY) is None
    assert due_notice(store, DAY, silence=True) == ("talk_offer", OFFER)


def test_an_answered_question_breaks_the_silence(store):
    for back in (3, 2, 1):
        store.log_prompt(DAY - timedelta(days=back), "daily")
    store.upsert(DAY - timedelta(days=2), 4)
    assert due_notice(store, DAY, silence=True) is None


def test_a_drop_against_the_month_before_offers_talk(store):
    for back in range(8, 20):
        store.upsert(DAY - timedelta(days=back), 4)
    _days(store, [3, 3, 3])
    assert due_notice(store, DAY) == ("talk_offer", OFFER)


def test_a_drop_needs_enough_days_on_both_sides(store):
    for back in range(8, 12):
        store.upsert(DAY - timedelta(days=back), 5)
    _days(store, [3, 3, 3])
    assert due_notice(store, DAY) is None


async def test_a_crisis_message_gets_the_safety_line(store, monkeypatch):
    monkeypatch.setattr("alliegent.mood.checkin_day", lambda now: DAY)
    bot = _bot(store)
    bot.notify = AsyncMock()
    await bot.on_message(_message("2 오늘은 정말 죽고 싶다"))
    bot.notify.assert_awaited_once_with(SAFETY_NOW, "mood")
    assert "109" in SAFETY_NOW


async def test_a_crisis_in_a_long_entry_is_noticed_too(store, monkeypatch):
    monkeypatch.setattr("alliegent.mood.checkin_day", lambda now: DAY)
    bot = _bot(store)
    bot.notify = AsyncMock()
    await bot.on_message(_message("긴 하루였다\n가끔 사라지고 싶다는 생각이 든다"))
    bot.notify.assert_awaited_once_with(SAFETY_NOW, "mood")


async def test_the_offer_replaces_the_question_after_three_silent_days(store):
    jobs, sent = _jobs(store)
    for back in (3, 2, 1):
        store.log_prompt(DAY - timedelta(days=back), "daily")
    await jobs.run_mood_checkin()
    assert sent == [(OFFER, "mood")]
