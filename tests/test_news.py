"""Cleanup of the model-written digest.

The prompt asks for no preamble and no closing note, but instruction adherence
is not a guarantee — and a stray "I'll search for..." atop every morning's
digest is exactly the kind of thing that survives unnoticed for months. These
cover the enforcement, not the prompt.
"""

from __future__ import annotations

from alliegent.integrations.claude import _clean

# What the model returns: EN:/KO: labels mark the field boundaries, and the
# headline carries its own link.
RAW_ITEM = (
    "**1. [A real headline](https://example.com/a)**\n"
    "EN: What happened.\n"
    "KO: 무슨 일이 있었습니다."
)

# What the reader sees: labels swapped for flags, and the two summaries on
# consecutive lines — the blank line between them doubled the height of a list
# that is read on a phone.
ITEM = (
    "**1. [A real headline](https://example.com/a)**\n"
    "🇺🇸 What happened.\n"
    "🇰🇷 무슨 일이 있었습니다."
)


def test_preamble_is_dropped():
    assert _clean("I'll search for the latest AI news.\n\n" + RAW_ITEM) == ITEM


def test_closing_commentary_is_dropped():
    text = RAW_ITEM + "\n\n(Search quota ran out before I could verify a full ten.)"
    assert _clean(text) == ITEM


def test_both_ends_are_trimmed_at_once():
    text = "Here you go:\n\n" + RAW_ITEM + "\n\nLet me know if you want more!"
    assert _clean(text) == ITEM


def test_citation_line_breaks_are_folded_back():
    """Search results split a sentence mid-line; unfolded, the layout breaks."""
    text = (
        "**1. A real headline**\n"
        "https://example.com/a\n"
        "EN: \nGPT-5.6 becomes the default\n.\n"
        "KO: 기본 모델이 됐다."
    )
    assert "🇺🇸 GPT-5.6 becomes the default." in _clean(text)


def test_blank_lines_inside_a_summary_are_folded_too():
    """The real failure: a citation break that also inserted a blank line, so
    the continuation no longer sat directly under its EN line."""
    text = (
        "**1. A real headline**\n"
        "https://example.com/a\n"
        "EN: First sentence.\n"
        "\n"
        "Second sentence, cut off mid-thought\n"
        "; and the rest of it.\n"
        " Third sentence.\n"
        "KO: 한국어 요약입니다."
    )
    cleaned = _clean(text)
    assert (
        "🇺🇸 First sentence. Second sentence, cut off mid-thought; "
        "and the rest of it. Third sentence." in cleaned
    )
    assert cleaned.endswith("🇰🇷 한국어 요약입니다.")


def test_blank_lines_between_items_are_preserved():
    second = RAW_ITEM.replace("**1.", "**2.").replace("/a", "/b")
    assert "\n\n**2." in _clean(f"{RAW_ITEM}\n\n{second}")


def test_multiple_items_survive_intact():
    second = RAW_ITEM.replace("**1.", "**2.").replace("/a", "/b")
    cleaned = _clean(f"{RAW_ITEM}\n\n{second}")
    assert cleaned.startswith("**1.")
    assert "**2." in cleaned
    assert cleaned.endswith("🇰🇷 무슨 일이 있었습니다.")


def test_the_headline_keeps_its_link_intact():
    """The link is the headline now, so a fold that broke it would leave the
    reader a bare bracket and no way to reach the article."""
    cleaned = _clean(RAW_ITEM)
    assert "[A real headline](https://example.com/a)" in cleaned


def test_the_topics_line_leads_the_digest():
    """It is read first and decides whether the rest gets read at all, which
    is what five full-height entries were failing to allow."""
    cleaned = _clean(
        "TOPICS: OpenAI teen safety · EU AI Act · Nvidia datacentre\n\n" + RAW_ITEM
    )
    assert cleaned.startswith("🏷️ ")
    assert "`OpenAI teen safety`" in cleaned
    assert "`EU AI Act`" in cleaned
    assert cleaned.index("🏷️") < cleaned.index("**1.")


def test_a_digest_without_topics_still_renders():
    """The line is the model's to produce, so its absence must not cost the
    items -- a missing header is a worse outcome than a missing digest is."""
    assert _clean(RAW_ITEM).startswith("**1.")


def test_topics_never_break_out_of_their_chip():
    """A backtick inside a label would swallow the rest of the line."""
    cleaned = _clean("TOPICS: back`tick · plain\n\n" + RAW_ITEM)
    assert "`backtick`" in cleaned


def test_only_a_handful_of_topics_are_kept():
    """A header longer than the list it heads is not a header."""
    labels = " · ".join(f"topic{n}" for n in range(1, 11))
    cleaned = _clean(f"TOPICS: {labels}\n\n" + RAW_ITEM)
    assert cleaned.count("`topic") == 6


def test_response_with_no_items_yields_nothing():
    """Caller treats empty as unavailable and stays quiet."""
    assert _clean("I couldn't find any AI news today.") == ""
