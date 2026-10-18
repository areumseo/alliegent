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
from contextlib import closing, contextmanager
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
        # Deleted and cleared text is overwritten in the file, not just
        # unlinked from it: forgetting should not leave it in free pages.
        db.execute("PRAGMA secure_delete = ON")
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

    def last_logged(self, kind: str) -> date | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT MAX(date) AS day FROM prompt_log WHERE kind = ?", (kind,)
            ).fetchone()
        return date.fromisoformat(row["day"]) if row["day"] else None

    def daily_prompts(self, start: date, end: date) -> list[tuple[date, bool]]:
        """Each day the question was asked from start to end, and whether answered."""
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT date, MAX(answered) AS answered FROM prompt_log
                WHERE kind = 'daily' AND date BETWEEN ? AND ?
                GROUP BY date ORDER BY date
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        return [(date.fromisoformat(r["date"]), bool(r["answered"])) for r in rows]

    def mark_answered(self, kind: str) -> None:
        """The latest prompt of a kind was taken up -- /talk after an offer."""
        with self._connect() as db:
            db.execute(
                """
                UPDATE prompt_log SET answered = 1 WHERE id =
                  (SELECT MAX(id) FROM prompt_log WHERE kind = ?)
                """,
                (kind,),
            )

    def clear_raw(self, before: date) -> int:
        """Drop the long entries of days before `before`; scores and notes stay."""
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE checkin SET raw_text = NULL WHERE date < ? AND raw_text IS NOT NULL",
                (before.isoformat(),),
            )
        return cursor.rowcount

    def forget(self, day: date | None) -> int:
        """Delete one day's check-in, or everything when `day` is None."""
        with self._connect() as db:
            if day is None:
                count = db.execute("DELETE FROM checkin").rowcount
                db.execute("DELETE FROM prompt_log")
            else:
                count = db.execute(
                    "DELETE FROM checkin WHERE date = ?", (day.isoformat(),)
                ).rowcount
        if day is None:
            with closing(sqlite3.connect(self.path)) as db:
                db.execute("VACUUM")
        return count

    def snapshot(self) -> bytes:
        """The whole database as bytes, consistent even mid-write."""
        with (
            closing(sqlite3.connect(self.path)) as source,
            closing(sqlite3.connect(":memory:")) as copy,
        ):
            source.backup(copy)
            return copy.serialize()

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


# -- the week ------------------------------------------------------------------

# Words too common in a mood note to say anything when they repeat.
_STOPWORDS = {
    "the",
    "and",
    "but",
    "was",
    "for",
    "with",
    "too",
    "very",
    "just",
    "today",
    "really",
    "bit",
    "lot",
    "day",
    "got",
    "not",
    "had",
    "have",
    "feel",
    "felt",
    "오늘",
    "너무",
    "그냥",
    "진짜",
    "조금",
    "많이",
    "정말",
    "하루",
    "그래도",
    "약간",
    "계속",
    "다시",
    "그리고",
    "근데",
    "했다",
    "했어",
    "같아",
    "있었다",
}
# Particles that turn one Korean word into several. Stripped longest first,
# and only when what is left still has two characters.
_PARTICLES = sorted(
    [
        "에서",
        "에게",
        "으로",
        "이랑",
        "하고",
        "까지",
        "부터",
        "보다",
        "은",
        "는",
        "이",
        "가",
        "을",
        "를",
        "에",
        "도",
        "로",
        "와",
        "과",
        "랑",
        "의",
    ],
    key=len,
    reverse=True,
)


def _words(text: str) -> set[str]:
    out = set()
    for word in re.findall(r"[\w']+", text.casefold()):
        for particle in _PARTICLES:
            if word.endswith(particle) and len(word) - len(particle) >= 2:
                word = word[: -len(particle)]
                break
        if len(word) >= 2 and not word.isdigit() and word not in _STOPWORDS:
            out.add(word)
    return out


def repeated_words(
    entries: list[Checkin], *, least: int = 2, limit: int = 5
) -> list[tuple[str, int]]:
    """Words in two or more days' notes, with how many days.

    Read from notes, summaries and keywords only. Long entries are not read:
    what this finds is posted back to the channel, and an entry is never quoted.
    """
    counts: dict[str, int] = {}
    for entry in entries:
        text = " ".join(t for t in (entry.note, entry.summary, entry.keywords) if t)
        for word in _words(text):
            counts[word] = counts.get(word, 0) + 1
    ranked = sorted(
        ((w, n) for w, n in counts.items() if n >= least), key=lambda pair: (-pair[1], pair[0])
    )
    return ranked[:limit]


def average(entries: list[Checkin]) -> float | None:
    scores = [e.score for e in entries if e.score is not None]
    return sum(scores) / len(scores) if scores else None


def week_report(
    end: date,
    entries: list[Checkin],
    *,
    before: list[Checkin] = (),
    agenda: dict[date, tuple[int, int]] | None = None,
) -> str | None:
    """Seven days to `end`: each day's score, what got done, and what repeats.

    Facts only. It says what the scores were and what came up more than once;
    it does not say what they mean. `agenda` is each day's (done, planned),
    when the agenda could be read. None when the week has no check-ins.
    """
    from . import reports

    if not any(e.score is not None or e.note for e in entries):
        return None
    start = end - timedelta(days=6)
    by_day = {e.day: e for e in entries}
    rows = []
    for offset in range(7):
        day = start + timedelta(days=offset)
        entry = by_day.get(day)
        score = entry.score if entry and entry.score is not None else None
        done = ""
        if agenda is not None:
            finished, planned = agenda.get(day, (0, 0))
            done = f"{finished}/{planned}" if planned else "-"
        rows.append(
            (
                reports.fmt_date(day),
                str(score) if score is not None else "-",
                "#" * score if score else "",
                done,
            )
        )
    lines = [f"🌙 **Mood — {reports.fmt_date(start)} to {reports.fmt_date(end)}**", ""]
    lines += reports._table(("Day", "Score", "", "Done"), rows, flex=2, right=(1, 3), optional=(3,))

    mean = average(entries)
    scored = sum(1 for e in entries if e.score is not None)
    if mean is not None:
        line = f"Average {mean:.1f} over {scored} day{'s' * (scored != 1)}"
        previous = average(list(before))
        if previous is not None:
            line += f" (the week before: {previous:.1f})"
        lines.append(line + ".")

    if agenda is not None:
        low = [d for d in by_day if (by_day[d].score or 3) <= 2 and agenda.get(d, (0, 0))[1]]
        high = [d for d in by_day if (by_day[d].score or 0) >= 4 and agenda.get(d, (0, 0))[1]]
        if low and high:

            def rate(days: list[date]) -> int:
                done = sum(agenda[d][0] for d in days)
                planned = sum(agenda[d][1] for d in days)
                return round(done / planned * 100)

            lines.append(
                f"Agenda done: {rate(low)}% on days scored 1-2, {rate(high)}% on days scored 4-5."
            )

    words = repeated_words(entries)
    if words:
        lines.append("Came up more than once: " + ", ".join(f"{w} ({n})" for w, n in words) + ".")
    return "\n".join(lines)


# -- /talk ---------------------------------------------------------------------

TALK_DAYS = 14
# Notes are cut to this in /talk, so fourteen of them fit one Discord message
# and the summary copies as a single block.
TALK_NOTE_MAX = 100


def _short(day: date) -> str:
    return f"{day.month}/{day.day}"


def low_runs(
    entries: list[Checkin], *, at_most: int = 2, length: int = 3
) -> list[tuple[date, date]]:
    """Stretches of `length` or more consecutive days all scored `at_most` or lower.

    A day with no score breaks a stretch: it is not known to be low.
    """
    runs: list[tuple[date, date]] = []
    start = last = None

    def close() -> None:
        if start is not None and (last - start).days + 1 >= length:
            runs.append((start, last))

    for entry in sorted(entries, key=lambda e: e.day):
        if entry.score is None or entry.score > at_most:
            close()
            start = last = None
        elif last is not None and entry.day == last + timedelta(days=1):
            last = entry.day
        else:
            close()
            start = last = entry.day
    close()
    return runs


def talk_summary(end: date, entries: list[Checkin]) -> str:
    """Fourteen days as one block of plain text, to paste into a conversation.

    Scores, repeated words, notes and summaries, and what changed sharply.
    No long entry, no transcript: those never leave this machine, and this
    is written to be carried somewhere else.
    """
    start = end - timedelta(days=TALK_DAYS - 1)
    entries = [e for e in entries if start <= e.day <= end]
    by_day = {e.day: e for e in entries}
    scored = [e for e in entries if e.score is not None]
    lines = [
        f"Mood check-ins, {_short(start)} to {_short(end)} "
        f"({TALK_DAYS} days, {len(scored)} scored 1-5, 5 is best)",
    ]
    days = [start + timedelta(days=n) for n in range(TALK_DAYS)]
    marks = []
    for day in days:
        entry = by_day.get(day)
        marks.append(f"{_short(day)} {entry.score if entry and entry.score else '-'}")
    lines.append("Scores: " + ", ".join(marks))

    first = average([e for e in scored if e.day < start + timedelta(days=7)])
    second = average([e for e in scored if e.day >= start + timedelta(days=7)])
    if first is not None and second is not None:
        lines.append(f"Average: first week {first:.1f}, second week {second:.1f}")
    elif scored:
        lines.append(f"Average: {average(scored):.1f}")

    changes = []
    previous = None
    for entry in scored:
        if previous is not None and abs(entry.score - previous.score) >= 2:
            changes.append(
                f"{_short(previous.day)} {previous.score} -> {_short(entry.day)} {entry.score}"
            )
        previous = entry
    if changes:
        lines.append("Changes of 2 or more: " + "; ".join(changes))
    for first_day, last_day in low_runs(entries):
        lines.append(f"Scored 2 or lower every day {_short(first_day)} to {_short(last_day)}")
    missing = sum(1 for day in days if day not in by_day)
    if missing:
        lines.append(f"No check-in on {missing} of {TALK_DAYS} days")

    words = repeated_words(entries)
    if words:
        lines.append("Came up more than once: " + ", ".join(f"{w} ({n})" for w, n in words))

    written = [e for e in reversed(entries) if e.note or e.summary]
    if written:
        lines.append("Notes, newest first:")
        for entry in written:
            text = " / ".join(t for t in (entry.summary, entry.note) if t)
            if len(text) > TALK_NOTE_MAX:
                text = text[: TALK_NOTE_MAX - 1] + "…"
            score = f" ({entry.score})" if entry.score else ""
            lines.append(f"- {_short(entry.day)}{score}: {text}")

    # A note with a fence in it would end the block early.
    body = "\n".join(lines).replace("```", "'''")
    return f"```\n{body}\n```"


# -- noticing ------------------------------------------------------------------
# Simple rules, kept as constants so they can be tuned once there is data.

LOW_SCORE = 2  # a day scored this or lower is a low day
LOW_RUN_DAYS = 3  # A: this many low days in a row
SILENT_DAYS = 3  # B: this many daily questions in a row left unanswered
RECENT_DAYS = 7  # C: the last week's average...
BASELINE_DAYS = 28  # ...against the four weeks before it...
DROP = 1.0  # ...lower by this much or more
RECENT_MIN, BASELINE_MIN = 3, 7  # C needs this many scores on each side
OFFER_COOLDOWN_DAYS = 7  # an offer is not repeated within a week
SAFETY_RUN_DAYS = 7  # low this long: point to people, not to /talk

OFFER = (
    "Seems like things have been hard lately. Want to talk? `/talk` makes a summary to start from."
)

# The one place #mood speaks Korean: the line it points to is a Korean
# service, and this is not a moment to be reading a second language.
SAFETY_NOW = (
    "지금 많이 힘든 것 같아. 이건 혼자, 또는 나랑만 버틸 일이 아니야. "
    "가까운 사람에게 연락하거나, 자살예방상담전화 **109**(24시간)에 전화해 줘. "
    "당장 위험하면 **112**나 **119**."
)
SAFETY_LOW = (
    "일주일 넘게 낮은 점수가 이어지고 있어. 가까운 사람이나 전문가와 이야기해 보면 좋겠어. "
    "많이 힘들 땐 자살예방상담전화 **109**(24시간)."
)

# Phrases that mean someone may be in danger. Matched with spaces removed, so
# "죽고 싶다" and "죽고싶다" both count. Kept narrow on purpose: "끝내고 싶다"
# is usually about work, and a false alarm here teaches the reader to skip it.
_CRISIS = [
    "죽고싶",
    "자살",
    "자해",
    "사라지고싶",
    "살기싫",
    "살고싶지않",
    "죽어버리",
    "killmyself",
    "suicid",
    "selfharm",
    "wanttodie",
    "endmylife",
    "hurtmyself",
]


def mentions_crisis(text: str) -> bool:
    squashed = re.sub(r"[\s\-']", "", text.casefold())
    return any(phrase in squashed for phrase in _CRISIS)


def _since(day: date, today: date, days: int) -> bool:
    return day is not None and (today - day).days < days


def due_notice(store: MoodStore, today: date, *, silence: bool = False) -> tuple[str, str] | None:
    """(kind, message) when a rule fires and its cooldown allows, else None.

    A long low stretch points to people first; shorter signals offer /talk.
    `silence` adds rule B, which only makes sense before the day's question.
    Each kind is sent at most once a week, and an unanswered one is not
    followed up: answering is optional, and a nudge would be pressure.
    """
    window = store.between(today - timedelta(days=RECENT_DAYS + BASELINE_DAYS), today)
    runs = [run for run in low_runs(window, at_most=LOW_SCORE, length=1) if run[1] == today]
    current = (runs[0][1] - runs[0][0]).days + 1 if runs else 0

    if current >= SAFETY_RUN_DAYS:
        if not _since(store.last_logged("safety"), today, OFFER_COOLDOWN_DAYS):
            return "safety", SAFETY_LOW
        return None
    if _since(store.last_logged("talk_offer"), today, OFFER_COOLDOWN_DAYS):
        return None

    fired = current >= LOW_RUN_DAYS
    if not fired and silence:
        asked = store.daily_prompts(today - timedelta(days=SILENT_DAYS), today - timedelta(days=1))
        fired = len(asked) == SILENT_DAYS and not any(answered for _, answered in asked)
    if not fired:
        recent = [e for e in window if e.day > today - timedelta(days=RECENT_DAYS)]
        baseline = [e for e in window if e.day <= today - timedelta(days=RECENT_DAYS)]
        now, then = average(recent), average(baseline)
        enough = (
            sum(e.score is not None for e in recent) >= RECENT_MIN
            and sum(e.score is not None for e in baseline) >= BASELINE_MIN
        )
        fired = enough and now is not None and then is not None and then - now >= DROP
    return ("talk_offer", OFFER) if fired else None


# -- retention and backup ------------------------------------------------------

BACKUP_PREFIX = "mood-"
BACKUP_SUFFIX = ".db.enc"


class BackupSkipped(RuntimeError):
    """The backup folder is not there -- usually the disk is not plugged in."""


def new_backup_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode()


def backup(store: MoodStore, folder: Path, key: str, day: date, *, keep: int) -> Path:
    """Write an encrypted copy of the database into `folder`, keeping `keep`.

    The folder must already exist: it is on an external disk, and creating
    it when the disk is missing would put the backup on the internal one
    under /Volumes, which is exactly where it should not be.
    """
    from cryptography.fernet import Fernet

    if not folder.is_dir():
        raise BackupSkipped(f"{folder} is not there")
    target = folder / f"{BACKUP_PREFIX}{day.isoformat()}{BACKUP_SUFFIX}"
    sealed = Fernet(key.encode()).encrypt(store.snapshot())
    partial = target.with_suffix(".partial")
    fd = os.open(partial, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(sealed)
    partial.replace(target)
    for old in sorted(folder.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}"))[:-keep]:
        old.unlink()
    return target


def restore(source: Path, key: str, out: Path) -> None:
    """Decrypt a backup into a database file at `out`, owner-only."""
    from cryptography.fernet import Fernet

    data = Fernet(key.encode()).decrypt(source.read_bytes())
    if out.exists():
        raise FileExistsError(f"{out} exists; restore into a new path")
    fd = os.open(out, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)


def _main() -> None:
    """`python -m alliegent.mood newkey` or `... restore BACKUP OUT`.

    The key is read from MOOD_BACKUP_KEY, as the bot reads it.
    """
    import argparse

    from .config import get_secrets

    parser = argparse.ArgumentParser(prog="python -m alliegent.mood")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("newkey", help="print a new backup key")
    restoring = commands.add_parser("restore", help="decrypt a backup")
    restoring.add_argument("backup", type=Path)
    restoring.add_argument("out", type=Path)
    args = parser.parse_args()
    if args.command == "newkey":
        print(new_backup_key())
        return
    key = get_secrets().mood_backup_key
    if not key:
        raise SystemExit("MOOD_BACKUP_KEY is not set.")
    restore(args.backup, key, args.out)
    print(f"Restored to {args.out}")


if __name__ == "__main__":
    _main()
