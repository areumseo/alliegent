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
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime
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
    cleaned = text.strip().replace("️", "")
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

    def _connect(self) -> sqlite3.Connection:
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        return db

    def upsert(
        self,
        day: date,
        score: int,
        note: str | None = None,
        *,
        now: datetime | None = None,
    ) -> Checkin:
        """Record the day's score, replacing whatever the day had.

        A second check-in on the same day is a correction, so it overwrites.
        The note is replaced only when one is given: re-scoring a day should not
        quietly throw away what was written about it.
        """
        if not 1 <= score <= 5:
            raise ValueError("A score is 1 to 5.")
        if note is not None:
            note = note.strip()[:NOTE_MAX] or None
        stamp = (now or datetime.now()).isoformat(timespec="seconds")
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO checkin (date, score, note, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(date) DO UPDATE SET
                  score = excluded.score,
                  note = COALESCE(excluded.note, checkin.note)
                """,
                (day.isoformat(), score, note, stamp),
            )
        found = self.get(day)
        assert found is not None
        return found

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


def confirmation(entry: Checkin) -> str:
    """The one line a check-in gets back. The note is not echoed: it was just
    written, and the channel should not become a second copy of it."""
    day = entry.day.strftime("%a %d %b")
    noted = " Note saved." if entry.note else ""
    return f"✅ {day}: {entry.score}/5.{noted}"
