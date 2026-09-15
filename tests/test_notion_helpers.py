"""Payload builders, where Notion's own limits bite."""

from alliegent.integrations import notion as n


def test_long_text_is_split_under_the_per_object_limit():
    """A single text object over 2000 characters is a 400 from Notion; lesson
    material is routinely several times that."""
    payload = n.rich_text("x" * 4500)["rich_text"]
    assert [len(p["text"]["content"]) for p in payload] == [2000, 2000, 500]


def test_text_beyond_the_property_ceiling_is_dropped_not_fatal():
    payload = n.rich_text("x" * (2000 * 120))["rich_text"]
    assert len(payload) == 100


def test_short_text_stays_one_object():
    assert len(n.rich_text("hello")["rich_text"]) == 1


def test_empty_text_is_still_a_valid_property():
    assert n.rich_text("")["rich_text"] == [{"type": "text", "text": {"content": ""}}]
