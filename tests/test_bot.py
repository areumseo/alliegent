"""Bot construction smoke tests.

discord.py validates slash-command names and option names at registration
time, against Discord's own rules. Building the bot here means a bad command
name fails in CI rather than on first deploy.
"""
# The whole Discord-facing surface is English: names, descriptions, replies.

from __future__ import annotations

from alliegent.agenda import AgendaService, ProjectService
from alliegent.config import Config, Secrets
from alliegent.integrations.discord_bot import AlliegentBot

from .conftest import FakeNotionClient

EXPECTED = {"today", "add", "done", "overdue", "projects", "brief"}


def make_bot() -> AlliegentBot:
    client = FakeNotionClient()
    config = Config()
    return AlliegentBot(
        config=config,
        agenda=AgendaService(client, config, "agenda-db"),
        projects=ProjectService(client, config, "proj-db"),
        secrets=Secrets(discord_channel_id=123),
        guild_id=0,
    )


def test_command_names_pass_discord_validation():
    bot = make_bot()
    assert {cmd.name for cmd in bot.tree.get_commands()} == EXPECTED


def test_command_names_need_no_input_method_switch():
    """Typing a slash command should never require switching to a Korean IME."""
    for cmd in make_bot().tree.get_commands():
        assert cmd.name.isascii(), cmd.name
        for param in cmd.parameters:
            assert param.display_name.isascii(), param.display_name


def test_every_command_has_a_description():
    # Discord rejects commands with an empty description.
    for cmd in make_bot().tree.get_commands():
        assert cmd.description
        assert len(cmd.description) <= 100


def test_add_command_options():
    bot = make_bot()
    add = next(c for c in bot.tree.get_commands() if c.name == "add")
    assert {p.display_name for p in add.parameters} == {"task", "when"}


def test_descriptions_are_english_too():
    for cmd in make_bot().tree.get_commands():
        assert cmd.description.isascii(), cmd.description
        for param in cmd.parameters:
            assert param.description.isascii(), param.description


def test_bot_shares_its_notifier_with_the_jobs():
    bot = make_bot()
    assert bot.jobs.notify == bot.notify
