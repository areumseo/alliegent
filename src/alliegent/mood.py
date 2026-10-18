"""#mood: a daily check-in kept in a SQLite file on this machine and nowhere else.

The agent records, notices and summarises. It is not a counsellor and does not
interpret: what it says back is the score and the date, never a reading of them.

Nothing here goes to Notion, and nothing written by the person is logged. The
database file is created owner-only (0600) in an owner-only directory, since
it is the one store this bot keeps that is about how someone is, rather than
what they are doing.
"""

from __future__ import annotations

import os
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS checkin (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL UNIQUE,
  score INTEGER CHECK (score BETWEEN 1 AND 5),
  note TEXT,
  summary TEXT,
  keywords TEXT,
  raw_text TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS prompt_log (
  id INTEGER PRIMARY KEY,
  date TEXT NOT NULL,
  kind TEXT NOT NULL,
  answered INTEGER DEFAULT 0
);
"""

# A one-line note, as the design has it. Long enough for a sentence, short
# enough that it stays a note and is safe to quote back in a summary.
NOTE_MAX = 200

# Faces read as a score, so a check-in can be one tap. Ordered worst to best;
# anything not here is not a score. One code point each, so a string of them
# splits into faces.
_FACES = {
    1: "😭😢😞😫😩",
    2: "😕🙁😔😟😣",
    3: "😐😶🙂😌",
    4: "😊😀😃😄",
    5: "😁🤩🥳😆",
}
EMOJI_SCORES = {face: score for score, faces in _FACES.items() for face in faces}


@dataclass(frozen=True)
class Checkin:
    day: date
    score: int | None
    note: str | None
    summary: str | None
    keywords: str | None


def parse_score(text: str) -> int | None:
    """A 1-5 score from a digit or a face, or None if the text is not one.

    Only the whole input counts: "3" and "😊" are scores, "3 hours of sleep"
    is not. Variation selectors are dropped first, since some keyboards send
    a face with one attached and it should read the same.
    """
    cleaned = text.strip().replace("\ufe0f", "")
    if cleaned in {"1", "2", "3", "4", "5"}:
        return int(cleaned)
    return EMOJI_SCORES.get(cleaned)


class MoodStore:
    """The check-in table and its prompt log, in one local SQLite file."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self._prepare()

    def _prepare(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not self.path.exists():
            # Created owner-only from the start, rather than created and then
            # narrowed: there is no moment at which it is readable by others.
            fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        os.chmod(self.path, 0o600)
        with self._connect() as db:
            db.executescript(SCHEMA)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def upsert(
        self,
        day: date,
        score: int,
        note: str | None = None,
        *,
        now: datetime | None = None,
    ) -> Checkin:
        """Record the day's score, keeping a note already written if none is given."""
        return self.record(day, score=score, note=note, now=now)

    def record(
        self,
        day: date,
        *,
        score: int | None = None,
        note: str | None = None,
        raw_text: str | None = None,
        now: datetime | None = None,
    ) -> Checkin:
        """Write what was given for the day, leaving the rest as it was.

        A second answer on the same day is a correction, so what it carries
        overwrites. What it does not carry is kept: a score sent after a long
        message scores that message rather than erasing it.
        """
        if score is not None and not 1 <= score <= 5:
            raise ValueError("A score is 1 to 5.")
        if note is not None:
            note = note.strip()[:NOTE_MAX] or None
        if raw_text is not None:
            raw_text = raw_text.strip() or None
        stamp = (now or datetime.now()).isoformat(timespec="seconds")
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO checkin (date, score, note, raw_text, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                  score = COALESCE(excluded.score, checkin.score),
                  note = COALESCE(excluded.note, checkin.note),
                  raw_text = COALESCE(excluded.raw_text, checkin.raw_text)
                """,
                (day.isoformat(), score, note, raw_text, stamp),
            )
            db.execute(
                "UPDATE prompt_log SET answered = 1 WHERE date = ? AND kind = 'daily'",
                (day.isoformat(),),
            )
        found = self.get(day)
        assert found is not None
        return found

    def log_prompt(self, day: date, kind: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO prompt_log (date, kind, answered) VALUES (?, ?, ?)",
                (day.isoformat(), kind, int(self.get(day) is not None and kind == "daily")),
            )

    def prompted(self, day: date, kind: str) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM prompt_log WHERE date = ? AND kind = ?",
                (day.isoformat(), kind),
            ).fetchone()
        return row is not None

    def has_raw(self, day: date) -> bool:
        with self._connect() as db:
            row = db.execute(
                "SELECT 1 FROM checkin WHERE date = ? AND raw_text IS NOT NULL",
                (day.isoformat(),),
            ).fetchone()
        return row is not None

    def get(self, day: date) -> Checkin | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT date, score, note, summary, keywords FROM checkin WHERE date = ?",
                (day.isoformat(),),
            ).fetchone()
        return _checkin(row) if row else None

    def between(self, start: date, end: date) -> list[Checkin]:
        """Check-ins from start to end inclusive, oldest first."""
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT date, score, note, summary, keywords FROM checkin
                WHERE date BETWEEN ? AND ? ORDER BY date
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        return [_checkin(row) for row in rows]


def _checkin(row: sqlite3.Row) -> Checkin:
    return Checkin(
        day=date.fromisoformat(row["date"]),
        score=row["score"],
        note=row["note"],
        summary=row["summary"],
        keywords=row["keywords"],
    )


def confirmation(entry: Checkin, *, wrote: bool = False) -> str:
    """The one line a check-in gets back. What was written is not echoed: it
    was just sent, and the channel should not become a second copy of it.

    With no score yet, the line says how to add one rather than asking: one
    line and no follow-up question, as the check-in promises.
    """
    day = entry.day.strftime("%a %d %b")
    saved = " Note saved." if wrote or entry.note else ""
    if entry.score is None:
        return f"📝 {day} saved. Send 1-5 or a face to score it."
    return f"✅ {day}: {entry.score}/5.{saved}"


# A reply before this hour belongs to the evening before: an answer to the
# 21:00 question sent at half past midnight is about the day that just ended.
DAY_STARTS_AT = 4

PROMPT = "How was your energy today? 1-5 or a face."


def checkin_day(now: datetime) -> date:
    """The day a check-in made at `now` is about."""
    if now.hour < DAY_STARTS_AT:
        return now.date() - timedelta(days=1)
    return now.date()


@dataclass(frozen=True)
class Reply:
    score: int | None = None
    note: str | None = None
    raw_text: str | None = None


_LEADING = re.compile(r"^([^\s,.:;/-]+)[\s,.:;/-]+(.+)$", re.DOTALL)


def parse_reply(text: str) -> Reply:
    """Split a #mood message into a score, a one-line note, or a long entry.

    A score alone, or a score followed by words ("2 slept badly"), scores
    the day. One short line is a note. Anything longer, or on more than one
    line, is kept whole as the day's entry and stays on this machine: it is
    never quoted back, summarised by a model, or sent anywhere.
    """
    text = text.strip()
    if not text:
        return Reply()
    score = parse_score(text)
    if score is not None:
        return Reply(score=score)
    match = _LEADING.match(text)
    if match and (score := parse_score(match.group(1))) is not None:
        text = match.group(2).strip()
    if "\n" not in text and len(text) <= NOTE_MAX:
        return Reply(score=score, note=text)
    return Reply(score=score, raw_text=text)


class VoiceUnavailable(RuntimeError):
    """Transcription cannot run here: not Apple Silicon, or no ffmpeg."""


def transcribe(path: Path, model: str) -> str:
    """A voice memo's words, transcribed on this machine with mlx-whisper.

    Blocking and slow (seconds per minute of audio), so it is called from a
    worker thread. The first call downloads the model from Hugging Face; the
    audio itself never leaves the machine.
    """
    try:
        import mlx_whisper  # type: ignore[import-not-found]
    except ImportError as exc:
        raise VoiceUnavailable("voice memos need Apple Silicon (mlx-whisper)") from exc
    try:
        result = mlx_whisper.transcribe(str(path), path_or_hf_repo=model, language="ko")
    except FileNotFoundError as exc:
        # mlx-whisper decodes audio with the ffmpeg binary.
        raise VoiceUnavailable("ffmpeg is not installed (brew install ffmpeg)") from exc
    return str(result.get("text", "")).strip()
