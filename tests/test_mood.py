from __future__ import annotations

import stat
from datetime import date

import pytest

from alliegent.config import Config, Secrets
from alliegent.mood import NOTE_MAX, MoodStore, confirmation, parse_score

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
