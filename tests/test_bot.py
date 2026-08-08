"""Bot construction smoke tests.

discord.py validates slash-command names and option names at registration
time, against Discord's own rules. Building the bot here means a bad command
name fails in CI rather than on first deploy.
"""

from __future__ import annotations

from alliegent.agenda import AgendaService, ProjectService
from alliegent.config import Config
from alliegent.integrations.discord_bot import AlliegentBot

from .conftest import FakeNotionClient

EXPECTED = {"오늘", "추가", "완료", "밀린것", "프로젝트", "브리핑"}


def make_bot() -> AlliegentBot:
    client = FakeNotionClient()
    config = Config()
    return AlliegentBot(
        config=config,
        agenda=AgendaService(client, config, "agenda-db"),
        projects=ProjectService(client, config, "proj-db"),
        channel_id=123,
        guild_id=0,
    )


def test_korean_command_names_pass_discord_validation():
    bot = make_bot()
    assert {cmd.name for cmd in bot.tree.get_commands()} == EXPECTED


def test_every_command_has_a_description():
    # Discord rejects commands with an empty description.
    for cmd in make_bot().tree.get_commands():
        assert cmd.description
        assert len(cmd.description) <= 100


def test_renamed_options_are_valid():
    bot = make_bot()
    add = next(c for c in bot.tree.get_commands() if c.name == "추가")
    assert {p.display_name for p in add.parameters} == {"할일", "날짜"}


def test_bot_shares_its_notifier_with_the_jobs():
    bot = make_bot()
    assert bot.jobs.notify == bot.notify
