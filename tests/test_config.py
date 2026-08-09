from __future__ import annotations

import pytest

from alliegent.config import Config, Secrets, load_config


def test_channel_routing_prefers_the_specific_channel():
    secrets = Secrets(
        discord_channel_id=1,
        discord_agenda_channel_id=2,
        discord_projects_channel_id=3,
        discord_review_channel_id=4,
    )
    assert secrets.channel_for("agenda") == 2
    assert secrets.channel_for("projects") == 3
    assert secrets.channel_for("review") == 4


def test_unset_channels_fall_back_to_the_default():
    secrets = Secrets(discord_channel_id=1)
    assert secrets.channel_for("agenda") == 1
    assert secrets.channel_for("projects") == 1


def test_news_has_its_own_channel():
    secrets = Secrets(discord_channel_id=1, discord_news_channel_id=9)
    assert secrets.channel_for("news") == 9


def test_news_falls_back_to_the_default_channel():
    assert Secrets(discord_channel_id=1).channel_for("news") == 1


def test_review_falls_back_to_the_agenda_channel_not_the_default():
    secrets = Secrets(discord_channel_id=1, discord_agenda_channel_id=2)
    assert secrets.channel_for("review") == 2


def test_channel_lookup_fails_loudly_when_nothing_is_configured():
    with pytest.raises(RuntimeError, match="DISCORD_PROJECTS_CHANNEL_ID"):
        Secrets().channel_for("projects")


def test_blank_ids_mean_unconfigured_not_a_crash():
    """A freshly copied .env is all `KEY=` placeholders; that must load."""
    assert Secrets(discord_channel_id="").discord_channel_id == 0


def test_trailing_comments_are_stripped_from_ids():
    """python-dotenv leaves `KEY=123  # note` as '123  # note'."""
    secrets = Secrets(discord_channel_id="123  # main channel")
    assert secrets.discord_channel_id == 123


def test_a_comment_only_value_is_treated_as_unset():
    assert Secrets(discord_review_channel_id="# optional").discord_review_channel_id == 0


def test_require_names_every_missing_setting():
    with pytest.raises(RuntimeError) as exc:
        Secrets().require("notion_token", "discord_bot_token")
    assert "NOTION_TOKEN" in str(exc.value)
    assert "DISCORD_BOT_TOKEN" in str(exc.value)


def test_require_passes_when_set():
    Secrets(notion_token="x").require("notion_token")


def test_shipped_toml_parses_and_matches_model_defaults():
    """The committed alliegent.toml is the file users edit; if it drifts from
    the model it silently stops configuring anything."""
    from alliegent.config import DEFAULT_TOML

    config = load_config(DEFAULT_TOML)
    assert config.timezone == "Asia/Seoul"
    assert config.agenda.props.title == Config().agenda.props.title
    assert config.projects.props.next_action == Config().projects.props.next_action
    assert set(config.agenda.status_values) == {"todo", "doing", "done"}


def test_missing_toml_falls_back_to_defaults(tmp_path):
    assert load_config(tmp_path / "nope.toml").timezone == "Asia/Seoul"
