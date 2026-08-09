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

EXPECTED = {
    "today",
    "tomorrow",
    "status",
    "add",
    "done",
    "overdue",
    "projects",
    "brief",
    "news",
}


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
    """One exception: /add's `when` lists the Korean date words it accepts.
    That description is the only place a user learns they work, so the values
    have to appear there — spelling them out is not the same as writing the
    interface in Korean."""
    for cmd in make_bot().tree.get_commands():
        assert cmd.description.isascii(), cmd.description
        for param in cmd.parameters:
            if (cmd.name, param.display_name) == ("add", "when"):
                continue
            assert param.description.isascii(), param.description


def test_add_advertises_the_korean_date_words():
    add = next(c for c in make_bot().tree.get_commands() if c.name == "add")
    when = next(p for p in add.parameters if p.display_name == "when")
    for word in ("오늘", "내일", "모레"):
        assert word in when.description


def test_option_descriptions_fit_discords_limit():
    for cmd in make_bot().tree.get_commands():
        for param in cmd.parameters:
            assert len(param.description) <= 100, (cmd.name, param.display_name)


def test_bot_shares_its_notifier_with_the_jobs():
    bot = make_bot()
    assert bot.jobs.notify == bot.notify


# -- command → channel routing ---------------------------------------------
# A command and its scheduled twin must land in the same channel, or the
# archive splits across whichever channel someone happened to be in.

COMMAND_CHANNELS = {
    "today": "agenda",
    "tomorrow": "agenda",
    "status": "agenda",
    "overdue": "agenda",
    "brief": "agenda",
    "projects": "projects",
    "news": "news",
}

# Short write confirmations answer in place: routing a one-line "Added — X"
# would turn every write into two messages.
INLINE_COMMANDS = {"add", "done"}


def test_every_command_either_routes_or_is_deliberately_inline():
    names = {cmd.name for cmd in make_bot().tree.get_commands()}
    assert names == set(COMMAND_CHANNELS) | INLINE_COMMANDS


def test_routed_kinds_all_resolve_to_a_channel():
    secrets = Secrets(discord_channel_id=1)
    for kind in set(COMMAND_CHANNELS.values()):
        assert secrets.channel_for(kind) > 0
