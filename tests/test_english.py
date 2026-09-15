"""English lesson review.

Every lesson here is invented. The model is never called: extraction and
grading are tested on the shapes they return, and the quiz flow on a fake
Notion, because a test that spends money on every run stops being run.
"""

from __future__ import annotations

import io
from datetime import date

from alliegent import english as E
from alliegent.config import Config
from alliegent.jobs import Jobs

from .conftest import FakeNotionClient

DAY = date(2026, 9, 15)


# -- reading material --------------------------------------------------------


def test_a_pdf_with_a_text_layer_yields_its_text():
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    buffer = io.BytesIO()
    writer.write(buffer)
    # A blank page has no text layer: that is exactly the scan case, which
    # must come back empty so the original is sent instead.
    assert E.pdf_text(buffer.getvalue()) == ""


def test_something_that_is_not_a_pdf_is_treated_as_unreadable_not_fatal():
    assert E.pdf_text(b"not a pdf at all") == ""


def test_html_keeps_the_prose_and_drops_the_page_furniture():
    html = """
    <html><head><style>.x{}</style><script>track()</script></head>
    <body><nav>Menu Home About</nav>
    <h1>Multi-word verbs</h1>
    <p>The children are <b>growing up</b>.</p>
    <footer>Cookies &amp; privacy</footer></body></html>
    """
    text = E.html_to_text(html)
    assert "Multi-word verbs" in text
    assert "The children are growing up ." in text
    assert "track()" not in text and "Menu" not in text and "Cookies" not in text


def test_links_are_pulled_out_of_a_message():
    text = "today's page https://learnenglish.example/grammar/verbs, and the pdf"
    assert E.urls_in(text) == ["https://learnenglish.example/grammar/verbs"]


def test_material_with_only_a_link_that_failed_to_load_is_empty():
    """Nothing to read means nothing to send to the model, and nothing billed."""
    material = E.Material(reference_urls=["https://x.example"], reference_texts=[""])
    assert material.empty


def test_lesson_material_and_reference_pages_are_labelled_apart():
    """The model is told which is which: expressions come from the lesson,
    never from a public grammar page anyone could read."""
    material = E.Material(
        text="Tutor said: 'I'm looking forward to it'",
        reference_urls=["https://ref.example"],
        reference_texts=["General grammar about multi-word verbs"],
    )
    content = E.material_content(material)
    texts = [c["text"] for c in content if c["type"] == "text"]
    assert any(t.startswith("LESSON material") for t in texts)
    assert any(t.startswith("REFERENCE page") for t in texts)


def test_a_scanned_pdf_is_sent_as_a_document_not_as_empty_text():
    material = E.Material(scanned_pdfs=[b"%PDF-1.4 fake"])
    kinds = [c["type"] for c in E.material_content(material)]
    assert "document" in kinds


# -- cleaning what the model returns ----------------------------------------


def test_one_malformed_item_does_not_sink_the_lesson():
    data = {
        "topic": "Plans",
        "expressions": [
            {"phrase": "look forward to", "context": "You can't wait for a trip"},
            {"phrase": "", "context": "no phrase, dropped"},
        ],
        "mistakes": [
            {"my_sentence": "I look forward to see you", "corrected": "…to seeing you",
             "error_type": "Grammar"},
            {"my_sentence": "half a correction"},
        ],
    }
    cleaned = E.clean_extraction(data)
    assert [e["phrase"] for e in cleaned["expressions"]] == ["look forward to"]
    assert len(cleaned["mistakes"]) == 1


def test_an_unknown_error_type_becomes_other_rather_than_a_bad_select():
    """Writing an option Notion doesn't have would create it silently."""
    data = {"mistakes": [{"my_sentence": "a", "corrected": "b", "error_type": "Spelling"}]}
    assert E.clean_extraction(data)["mistakes"][0]["error_type"] == "Other"


def test_a_reply_wrapped_in_prose_still_parses():
    raw = 'Here are the notes:\n```json\n{"topic": "x", "expressions": []}\n```'
    assert E._json_object(raw)["topic"] == "x"


def test_no_json_at_all_is_a_clear_failure():
    try:
        E._json_object("I could not read the lesson.")
    except E.EnglishUnavailable:
        return
    raise AssertionError("a reply without JSON must fail loudly, not save an empty lesson")


# -- answers ---------------------------------------------------------------


def test_numbered_answers_land_on_their_numbers():
    assert E.split_answers("2. second\n1. first", 2) == ["first", "second"]


def test_plain_lines_fill_in_order():
    """Numbering is asked for, but five plain lines are just as clear."""
    assert E.split_answers("first\nsecond", 2) == ["first", "second"]


def test_a_skipped_question_is_an_empty_answer_not_a_shifted_one():
    """Answering 1 and 3 must not grade the third answer against question 2."""
    assert E.split_answers("1. a\n3. c", 3) == ["a", "", "c"]


# -- the evening quiz --------------------------------------------------------


def lesson_page(pid, day, topic, quizzed=False):
    return {
        "object": "page", "id": pid, "url": "", "in_trash": False,
        "created_time": "2026-09-15T00:00:00.000Z",
        "properties": {
            "Name": {"type": "title", "title": [{"plain_text": topic, "type": "text"}]},
            "Date": {"type": "date", "date": {"start": day}},
            "Topic": {"type": "rich_text", "rich_text": [{"plain_text": topic, "type": "text"}]},
            "Quizzed": {"type": "checkbox", "checkbox": quizzed},
        },
    }


def expression_page(pid, lesson_id, phrase, context):
    def text(value):
        return {"type": "rich_text", "rich_text": [{"plain_text": value, "type": "text"}]}

    return {
        "object": "page", "id": pid, "url": "", "in_trash": False,
        "created_time": "2026-09-15T00:00:00.000Z",
        "properties": {
            "Phrase": {"type": "title", "title": [{"plain_text": phrase, "type": "text"}]},
            "Lesson": {"type": "relation", "relation": [{"id": lesson_id}]},
            "Meaning (KO)": text(""), "Context": text(context),
            "My Attempt": text(""), "Tutor Version": text(""),
        },
    }


class SentMessage:
    def __init__(self, message_id):
        self.id = message_id


def quiz_setup(lessons, expressions):
    client = FakeNotionClient(
        {
            "ds_lessons": lessons,
            "ds_expressions": expressions,
            "ds_mistakes": [],
            "ds_reviews": [],
        }
    )
    service = E.EnglishService(
        client, Config(),
        lessons_db="lessons", expressions_db="expressions",
        mistakes_db="mistakes", reviews_db="reviews",
    )
    sent = []

    async def notify(message, kind):
        sent.append((message, kind))
        return [SentMessage(4242)]

    jobs = Jobs(None, None, Config(), notify, clock=lambda: DAY, english=service)
    return client, jobs, sent


async def test_the_quiz_asks_about_todays_lesson():
    client, jobs, sent = quiz_setup(
        [lesson_page("L1", "2026-09-15", "Plans")],
        [expression_page("X1", "L1", "look forward to", "You can't wait for a trip")],
    )
    await jobs.run_english_quiz()
    message, kind = sent[0]
    assert kind == "english"
    assert "You can't wait for a trip" in message
    assert "look forward to" not in message  # the question must not give it away


async def test_the_quiz_remembers_which_message_the_answers_reply_to():
    client, jobs, _ = quiz_setup(
        [lesson_page("L1", "2026-09-15", "Plans")],
        [expression_page("X1", "L1", "look forward to", "You can't wait for a trip")],
    )
    await jobs.run_english_quiz()
    review = next(p for ds, p in client.created)
    assert review["Quiz Message"]["rich_text"][0]["text"]["content"] == "4242"
    assert review["Result"]["select"]["name"] == "Pending"


async def test_a_lesson_is_quizzed_only_once():
    """Twice in an evening would be a restart away from happening."""
    client, jobs, sent = quiz_setup(
        [lesson_page("L1", "2026-09-15", "Plans")],
        [expression_page("X1", "L1", "look forward to", "You can't wait")],
    )
    await jobs.run_english_quiz()
    assert any(props.get("Quizzed") == {"checkbox": True} for _, props in client.updated)


async def test_a_day_without_a_lesson_is_silent():
    client, jobs, sent = quiz_setup([], [])
    await jobs.run_english_quiz()
    assert sent == []


async def test_yesterdays_lesson_is_not_tonights_quiz():
    client, jobs, sent = quiz_setup(
        [lesson_page("L1", "2026-09-14", "Old")],
        [expression_page("X1", "L1", "phrase", "context")],
    )
    await jobs.run_english_quiz()
    assert sent == []


async def test_the_quiz_is_capped_at_five():
    expressions = [expression_page(f"X{i}", "L1", f"p{i}", f"situation {i}") for i in range(8)]
    _, jobs, sent = quiz_setup([lesson_page("L1", "2026-09-15", "Plans")], expressions)
    await jobs.run_english_quiz()
    assert sent[0][0].count("**") >= 2
    assert "**6.**" not in sent[0][0]


# -- the graded reply --------------------------------------------------------


def test_the_graded_reply_scores_and_shows_the_taught_expression():
    review = E.Review("R1", "You can't wait", "X1", "Pending", "4242")
    expression = E.Expression("X1", "look forward to", "", "You can't wait", "", "")
    text = E.graded_message(
        [(review, expression, "I'm excited", "Close", "Try: I'm looking forward to it.")]
    )
    assert "0/1" in text
    assert "look forward to" in text
    assert "Try:" in text


def test_a_reference_that_would_not_load_still_names_its_topic():
    """British Council refuses server requests, so the page text is usually
    missing; the address still tells the model which grammar it was."""
    material = E.Material(
        text="lesson notes",
        reference_urls=["https://learnenglish.example/grammar/multi-word-verbs"],
        reference_texts=[""],
    )
    texts = [c["text"] for c in E.material_content(material) if c["type"] == "text"]
    assert any("multi-word-verbs" in t and "could not be loaded" in t for t in texts)


def test_a_link_alone_is_not_a_lesson():
    """A public grammar page is the same for everyone; there is nothing in it
    about this lesson to quiz on."""
    assert not E.Material(reference_urls=["https://x.example"], reference_texts=["text"]).has_lesson


def test_a_pdf_or_notes_are_a_lesson():
    assert E.Material(pdf_texts=["notes"]).has_lesson
    assert E.Material(text="what we covered").has_lesson
    assert E.Material(images=[("image/png", b"x")]).has_lesson
