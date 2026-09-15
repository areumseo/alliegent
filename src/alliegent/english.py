"""English lesson review: collect what a lesson taught, then quiz on it that evening.

The source of truth for a lesson is what the tutor sent afterwards -- a PDF of
the lesson, often with a reference page linked. Those two do different jobs and
are kept apart: the PDF is *this* lesson (what I said, what was corrected), the
link is general grammar anyone could read. The quiz is built from the first and
only explained with the second; a quiz built from a public grammar page would
be a grammar exercise, not a review of the lesson.

Everything is stored in Notion, like the rest of the bot, rather than in a
local database. At two lessons a week the volume is tiny, and the lessons stay
browsable and correctable in the place the user already works.

Cost is kept low on purpose. PDF text is extracted on the server and only the
text is sent to the model -- sending the PDF itself has every page processed as
an image as well, several times the tokens for the same words. The original is
sent only when a PDF has no text layer (a scan). Quiz prompts come from the
extracted context, so asking costs nothing; only grading calls the model.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import date

import httpx

from .config import Config
from .integrations import notion as n
from .integrations.notion import NotionClient

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-5"
TIMEOUT_SECONDS = 5 * 60
QUIZ_SIZE = 5

ERROR_TYPES = (
    "Grammar",
    "Vocabulary",
    "Collocation",
    "Preposition",
    "Tense",
    "Word order",
    "Pronunciation",
    "Other",
)
RESULTS = ("Correct", "Close", "Missed")

URL_PATTERN = re.compile(r"https?://\S+")


class EnglishUnavailable(RuntimeError):
    """The model call could not produce a usable answer."""


# -- what a lesson was made of ---------------------------------------------


@dataclass
class Material:
    """Everything posted for one lesson, before the model has read it."""

    text: str = ""
    pdf_texts: list[str] = field(default_factory=list)
    # PDFs with no text layer, sent to the model whole as a last resort.
    scanned_pdfs: list[bytes] = field(default_factory=list)
    images: list[tuple[str, bytes]] = field(default_factory=list)  # (media type, data)
    reference_urls: list[str] = field(default_factory=list)
    reference_texts: list[str] = field(default_factory=list)

    @property
    def has_lesson(self) -> bool:
        """Whether anything here is the lesson itself, not background."""
        return bool(
            self.text.strip()
            or any(text.strip() for text in self.pdf_texts)
            or self.scanned_pdfs
            or self.images
        )

    @property
    def empty(self) -> bool:
        # A reference that failed to load leaves an empty string behind; counting
        # that as material would send the model a request with nothing in it,
        # and bill for it.
        return not (
            self.text.strip()
            or any(text.strip() for text in self.pdf_texts)
            or self.scanned_pdfs
            or self.images
            or any(text.strip() for text in self.reference_texts)
        )


def pdf_text(data: bytes) -> str:
    """The text layer of a PDF, or "" when it has none.

    Empty is the signal to fall back to sending the file itself: a scan has no
    text to extract, and guessing at it here would only lose information.
    """
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception:
        log.exception("could not read PDF text")
        return ""
    text = "\n\n".join(p.strip() for p in pages if p.strip())
    # A handful of stray characters is what a scan's metadata yields, not a
    # usable text layer.
    return text if len(text) > 40 else ""


_TAG = re.compile(r"<[^>]+>")
_BLOCK_END = re.compile(r"</(p|div|li|h[1-6]|tr|br)\s*>", re.IGNORECASE)


def html_to_text(html: str) -> str:
    """Readable text from a page, crude on purpose.

    The reference pages are prose, examples and tables; keeping block breaks
    is enough for the model to follow them, and a full HTML parser would be a
    dependency for no better result.
    """
    html = re.sub(r"(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>", " ", html)
    html = _BLOCK_END.sub("\n", html)
    text = _TAG.sub(" ", html)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&#39;", "'"), ("&quot;", '"')):
        text = text.replace(entity, char)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


async def fetch_reference(url: str, *, limit: int = 20_000) -> str:
    """A reference page's text, or "" when it cannot be read.

    Background only, so a failure here costs the explanation, not the lesson.
    """
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
            response = await client.get(url, headers={"User-Agent": "alliegent/1.0"})
            response.raise_for_status()
    except Exception as exc:
        log.warning("reference page unavailable: %s (%s)", url, type(exc).__name__)
        return ""
    return html_to_text(response.text)[:limit]


# -- stored shapes -----------------------------------------------------------


@dataclass(frozen=True)
class Expression:
    id: str
    phrase: str
    meaning_ko: str
    context: str
    my_attempt: str
    tutor_version: str


@dataclass(frozen=True)
class Review:
    id: str
    prompt: str
    expression_id: str | None
    result: str | None
    quiz_message: str


@dataclass(frozen=True)
class Lesson:
    id: str
    day: date | None
    topic: str
    quizzed: bool


# -- the model ---------------------------------------------------------------


EXTRACT_SYSTEM = """\
You turn the material from one English conversation lesson into structured
notes for the learner's review. The learner is Korean.

The material has two kinds of source, and they are not equal:
- LESSON material (the tutor's PDF or notes, screenshots, pasted text) is what
  happened in this lesson. Take expressions and corrections only from here.
- REFERENCE pages are general grammar explanations. Use them only to write a
  clearer meaning or context. Never take expressions or corrections from them.

Reply with one JSON object and nothing else:

{
  "topic": "short topic of the lesson, in English",
  "expressions": [
    {
      "phrase": "the expression worth keeping, as used in the lesson",
      "meaning_ko": "what it means, in natural Korean",
      "context": "one sentence describing a situation where it fits, worded so
                  it can be asked as a question without giving the phrase away",
      "my_attempt": "what the learner said instead, if the material shows it, else empty",
      "tutor_version": "how the tutor said it, if the material shows it, else empty"
    }
  ],
  "mistakes": [
    {
      "my_sentence": "the learner's sentence as they said it",
      "corrected": "the corrected sentence",
      "error_type": "one of: Grammar, Vocabulary, Collocation, Preposition,
                     Tense, Word order, Pronunciation, Other"
    }
  ]
}

Rules:
- Include only what the lesson material actually contains. An empty list is
  correct when there is nothing; inventing examples is not.
- Up to 10 expressions, the most useful first.
- The context must not contain the phrase itself or an obvious form of it.
"""

GRADE_SYSTEM = """\
You grade a Korean learner's answers to an English expression quiz. For each
item you get the situation that was asked, the expression the tutor taught,
and the learner's answer.

Judge whether the answer would work in that situation, not whether it matches
the taught expression word for word. A different natural phrasing is Correct.
Understandable but unnatural or slightly wrong is Close. Wrong, unclear or
empty is Missed.

Reply with one JSON object and nothing else:

{"grades": [{"result": "Correct|Close|Missed", "feedback": "..."}]}

The feedback is one or two short sentences in English: what worked, what to
change, and the taught expression whenever the answer differed from it.

Return exactly one grade per item, in the order given.
"""


def _json_object(raw: str) -> dict:
    """The JSON object in a reply, tolerating a stray fence or sentence around it."""
    start, end = raw.find("{"), raw.rfind("}")
    if start == -1 or end <= start:
        raise EnglishUnavailable("the model returned no JSON")
    try:
        return json.loads(raw[start : end + 1])
    except json.JSONDecodeError as exc:
        raise EnglishUnavailable(f"the model returned malformed JSON: {exc}") from exc


async def _ask(api_key: str, system: str, content: list[dict], max_tokens: int) -> str:
    from anthropic import AsyncAnthropic

    # Same pattern as the news digest: streamed, no retries, a hard ceiling.
    # A retry repeats the whole call at full price for a message nobody is
    # waiting on.
    client = AsyncAnthropic(api_key=api_key, max_retries=0)
    try:
        async with asyncio.timeout(TIMEOUT_SECONDS), client.messages.stream(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": content}],
        ) as stream:
            response = await stream.get_final_message()
    except TimeoutError as exc:
        raise EnglishUnavailable(f"timed out after {TIMEOUT_SECONDS}s") from exc
    except Exception as exc:
        raise EnglishUnavailable(f"{type(exc).__name__}: {exc}") from exc
    finally:
        await client.close()

    if response.stop_reason == "refusal":
        raise EnglishUnavailable("the model declined the request")
    log.info(
        "english model call: %s in, %s out",
        response.usage.input_tokens,
        response.usage.output_tokens,
    )
    return "\n".join(b.text for b in response.content if getattr(b, "type", None) == "text")


def material_content(material: Material) -> list[dict]:
    """The request content, with each source labelled by what it is."""
    content: list[dict] = []
    lesson_text = "\n\n".join(t for t in [material.text.strip(), *material.pdf_texts] if t)
    if lesson_text:
        content.append({"type": "text", "text": f"LESSON material:\n\n{lesson_text}"})
    for data in material.scanned_pdfs:
        content.append({"type": "text", "text": "LESSON material (scanned PDF):"})
        content.append(
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.standard_b64encode(data).decode("ascii"),
                },
            }
        )
    for media_type, data in material.images:
        content.append({"type": "text", "text": "LESSON material (screenshot):"})
        content.append(
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": base64.standard_b64encode(data).decode("ascii"),
                },
            }
        )
    for page_url, text in zip(material.reference_urls, material.reference_texts, strict=False):
        if text:
            content.append({"type": "text", "text": f"REFERENCE page ({page_url}):\n\n{text}"})
        else:
            # British Council's pages refuse requests from servers, so the text
            # is usually unavailable. The address still names the grammar topic,
            # and standard grammar is something the model already knows.
            content.append(
                {
                    "type": "text",
                    "text": f"REFERENCE page ({page_url}): could not be loaded. Its "
                    "address names the grammar topic; use your own knowledge of it.",
                }
            )
    content.append({"type": "text", "text": "Write the notes."})
    return content


async def extract(api_key: str, material: Material) -> dict:
    raw = await _ask(api_key, EXTRACT_SYSTEM, material_content(material), 4000)
    return clean_extraction(_json_object(raw))


def clean_extraction(data: dict) -> dict:
    """Keep only well-formed entries, so one bad item cannot sink a lesson."""
    expressions = []
    for item in data.get("expressions") or []:
        phrase = str(item.get("phrase") or "").strip()
        if not phrase:
            continue
        expressions.append(
            {
                "phrase": phrase,
                "meaning_ko": str(item.get("meaning_ko") or "").strip(),
                "context": str(item.get("context") or "").strip(),
                "my_attempt": str(item.get("my_attempt") or "").strip(),
                "tutor_version": str(item.get("tutor_version") or "").strip(),
            }
        )
    mistakes = []
    for item in data.get("mistakes") or []:
        mine = str(item.get("my_sentence") or "").strip()
        fixed = str(item.get("corrected") or "").strip()
        if not (mine and fixed):
            continue
        kind = str(item.get("error_type") or "Other").strip()
        mistakes.append(
            {
                "my_sentence": mine,
                "corrected": fixed,
                "error_type": kind if kind in ERROR_TYPES else "Other",
            }
        )
    return {
        "topic": str(data.get("topic") or "").strip() or "Lesson",
        "expressions": expressions[:10],
        "mistakes": mistakes,
    }


async def grade(
    api_key: str, items: list[tuple[str, str, str]]
) -> list[tuple[str, str]]:
    """(situation, taught expression, answer) -> (result, feedback), in order."""
    lines = [
        f"{i}. Situation: {situation}\n   Taught: {taught}\n   Answer: {answer or '(no answer)'}"
        for i, (situation, taught, answer) in enumerate(items, start=1)
    ]
    raw = await _ask(api_key, GRADE_SYSTEM, [{"type": "text", "text": "\n\n".join(lines)}], 2000)
    grades = _json_object(raw).get("grades") or []
    out = []
    for i in range(len(items)):
        entry = grades[i] if i < len(grades) else {}
        result = str(entry.get("result") or "Missed").strip()
        out.append(
            (result if result in RESULTS else "Missed", str(entry.get("feedback") or "").strip())
        )
    return out


def split_answers(text: str, count: int) -> list[str]:
    """Numbered answers out of one message; unnumbered lines fill in order.

    "1. ... 2. ..." is what the quiz asks for, but a reply written as five
    plain lines is just as clear, and refusing it would be pedantic.
    """
    numbered: dict[int, str] = {}
    loose: list[str] = []
    for line in (line.strip() for line in text.splitlines()):
        if not line:
            continue
        match = re.match(r"^(\d+)[.):\-]\s*(.*)$", line)
        if match and 1 <= int(match.group(1)) <= count:
            numbered[int(match.group(1))] = match.group(2).strip()
        else:
            loose.append(line)
    answers = []
    for i in range(1, count + 1):
        if i in numbered:
            answers.append(numbered[i])
        elif loose:
            answers.append(loose.pop(0))
        else:
            answers.append("")
    return answers


# -- storage -----------------------------------------------------------------


class EnglishService:
    def __init__(
        self,
        client: NotionClient,
        config: Config,
        *,
        lessons_db: str,
        expressions_db: str,
        mistakes_db: str,
        reviews_db: str,
    ) -> None:
        self._client = client
        self._cfg = config
        self._dbs = {
            "lessons": lessons_db,
            "expressions": expressions_db,
            "mistakes": mistakes_db,
            "reviews": reviews_db,
        }
        self._ds: dict[str, str] = {}

    async def _source(self, name: str) -> str:
        if name not in self._ds:
            self._ds[name] = await self._client.resolve_data_source(self._dbs[name])
        return self._ds[name]

    async def save_lesson(
        self, day: date, notes: dict, raw_text: str, source_url: str | None
    ) -> tuple[str, int, int]:
        """Write a lesson and everything extracted from it. Returns the lesson id
        and how many expressions and mistakes were stored."""
        lesson = await self._client.create_page(
            await self._source("lessons"),
            {
                "Name": n.title(notes["topic"]),
                "Date": n.date_prop(day),
                "Topic": n.rich_text(notes["topic"]),
                "Source URL": n.url(source_url),
                "Raw Text": n.rich_text(raw_text),
                "Quizzed": n.checkbox(False),
            },
        )
        lesson_id = lesson["id"]

        expressions_ds = await self._source("expressions")
        for item in notes["expressions"]:
            await self._client.create_page(
                expressions_ds,
                {
                    "Phrase": n.title(item["phrase"]),
                    "Lesson": n.relation([lesson_id]),
                    "Meaning (KO)": n.rich_text(item["meaning_ko"]),
                    "Context": n.rich_text(item["context"]),
                    "My Attempt": n.rich_text(item["my_attempt"]),
                    "Tutor Version": n.rich_text(item["tutor_version"]),
                },
            )

        mistakes_ds = await self._source("mistakes")
        for item in notes["mistakes"]:
            await self._client.create_page(
                mistakes_ds,
                {
                    "My Sentence": n.title(item["my_sentence"]),
                    "Lesson": n.relation([lesson_id]),
                    "Corrected": n.rich_text(item["corrected"]),
                    "Error Type": n.select(item["error_type"]),
                },
            )
        return lesson_id, len(notes["expressions"]), len(notes["mistakes"])

    async def attach_reference(self, lesson_id: str, url_value: str) -> None:
        await self._client.update_page(lesson_id, {"Source URL": n.url(url_value)})

    async def lessons_on(self, day: date) -> list[Lesson]:
        ds = await self._source("lessons")
        out = []
        async for page in self._client.query(ds):
            if n.read_date(page, "Date") == day:
                out.append(
                    Lesson(
                        id=page["id"],
                        day=day,
                        topic=n.read_text(page, "Topic") or n.read_title(page, "Name"),
                        quizzed=n.read_checkbox(page, "Quizzed"),
                    )
                )
        return out

    async def expressions_for(self, lesson_ids: set[str]) -> list[Expression]:
        ds = await self._source("expressions")
        out = []
        async for page in self._client.query(ds):
            if not lesson_ids.intersection(n.read_relation_ids(page, "Lesson")):
                continue
            out.append(
                Expression(
                    id=page["id"],
                    phrase=n.read_title(page, "Phrase"),
                    meaning_ko=n.read_text(page, "Meaning (KO)"),
                    context=n.read_text(page, "Context"),
                    my_attempt=n.read_text(page, "My Attempt"),
                    tutor_version=n.read_text(page, "Tutor Version"),
                )
            )
        return out

    async def mark_quizzed(self, lesson_ids: list[str]) -> None:
        for lesson_id in lesson_ids:
            await self._client.update_page(lesson_id, {"Quizzed": n.checkbox(True)})

    async def record_questions(
        self, day: date, questions: list[Expression], quiz_message: str
    ) -> None:
        ds = await self._source("reviews")
        for expression in questions:
            await self._client.create_page(
                ds,
                {
                    "Prompt": n.title(expression.context or expression.phrase),
                    "Expression": n.relation([expression.id]),
                    "Asked At": n.date_prop(day),
                    "Result": n.select("Pending"),
                    "Quiz Message": n.rich_text(quiz_message),
                },
            )

    async def pending_for(self, quiz_message: str) -> list[Review]:
        """The unanswered questions of one quiz, in the order they were asked."""
        ds = await self._source("reviews")
        rows = []
        async for page in self._client.query(ds):
            if n.read_text(page, "Quiz Message") != quiz_message:
                continue
            related = n.read_relation_ids(page, "Expression")
            rows.append(
                (
                    n.read_created(page),
                    Review(
                        id=page["id"],
                        prompt=n.read_title(page, "Prompt"),
                        expression_id=related[0] if related else None,
                        result=n.read_select(page, "Result"),
                        quiz_message=quiz_message,
                    ),
                )
            )
        rows.sort(key=lambda pair: pair[0])
        return [review for _, review in rows if review.result == "Pending"]

    async def save_grade(self, review_id: str, answer: str, result: str, feedback: str) -> None:
        await self._client.update_page(
            review_id,
            {
                "My Answer": n.rich_text(answer),
                "Result": n.select(result),
                "Feedback": n.rich_text(feedback),
            },
        )

    async def expression(self, expression_id: str) -> Expression | None:
        ds = await self._source("expressions")
        async for page in self._client.query(ds):
            if page["id"] == expression_id:
                return Expression(
                    id=page["id"],
                    phrase=n.read_title(page, "Phrase"),
                    meaning_ko=n.read_text(page, "Meaning (KO)"),
                    context=n.read_text(page, "Context"),
                    my_attempt=n.read_text(page, "My Attempt"),
                    tutor_version=n.read_text(page, "Tutor Version"),
                )
        return None


# -- messages ----------------------------------------------------------------


def saved_message(topic: str, expressions: int, mistakes: int, sources: list[str]) -> str:
    return "\n".join(
        [
            f"🇬🇧 **Saved — {topic}**",
            f"{expressions} expression(s) · {mistakes} correction(s)",
            f"_from: {', '.join(sources)}_",
            "_Quiz this evening._" if expressions else "_Nothing to quiz on from this one._",
        ]
    )


def quiz_message(questions: list[Expression], topics: list[str]) -> str:
    out = [f"🇬🇧 **Evening quiz — {' · '.join(topics)}**", "", "What would you say?", ""]
    for i, expression in enumerate(questions, start=1):
        out.append(f"**{i}.** {expression.context or expression.meaning_ko}")
    out += ["", "_Reply to this message with your answers, one per line._"]
    return "\n".join(out)


MARKS = {"Correct": "✅", "Close": "🟡", "Missed": "❌"}


def graded_message(rows: list[tuple[Review, Expression | None, str, str, str]]) -> str:
    """rows: (review, expression, answer, result, feedback)."""
    score = sum(1 for *_, result, _ in rows if result == "Correct")
    out = [f"🇬🇧 **Quiz — {score}/{len(rows)}**", ""]
    for i, (_, expression, answer, result, feedback) in enumerate(rows, start=1):
        out.append(f"{MARKS.get(result, '•')} **{i}.** {answer or '(no answer)'}")
        if expression and expression.phrase:
            out.append(f"   Lesson: *{expression.phrase}*")
        if feedback:
            out.append(f"   {feedback}")
    return "\n".join(out)


def urls_in(text: str) -> list[str]:
    return [u.rstrip(").,>") for u in URL_PATTERN.findall(text)]
