"""Bot construction smoke tests.

discord.py validates slash-command names and option names at registration
time, against Discord's own rules. Building the bot here means a bad command
name fails in CI rather than on first deploy.
"""
# The whole Discord-facing surface is English: names, descriptions, replies.

from __future__ import annotations

import pytest

from alliegent.agenda import AgendaService, ProjectService
from alliegent.config import Config, Secrets
from alliegent.integrations.discord_bot import (
    AlliegentBot,
    means_clear,
    parse_numbers,
    resolve_project,
)

from .conftest import FakeNotionClient

EXPECTED = {
    "today",
    "tomorrow",
    "status",
    "add",
    "done",
    "delete",
    "change",
    "overdue",
    "projects",
    "brief",
    "news",
    "karrot",
    "assets",
}


def leaf_commands(bot: AlliegentBot):
    """Every command that actually takes arguments, groups expanded.

    /karrot is a group, and a group has subcommands rather than parameters —
    walking the tree without expanding it silently skips everything inside.
    """
    out = []
    for command in bot.tree.get_commands():
        children = getattr(command, "commands", None)
        if children:
            out.extend(children)
        else:
            out.append(command)
    return out


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


# Two option descriptions name Korean values, because the values themselves
# are Korean and the description is the only place a user finds them: the date
# words /add accepts, and the categories the Karrot database uses. Listing a
# value is not the same as writing the interface in Korean — every command and
# option description is English, including Karrot's, whose *replies* are not.
KOREAN_VALUE_OPTIONS = {("add", "when"), ("change", "day")}


def test_command_names_pass_discord_validation():
    bot = make_bot()
    assert {cmd.name for cmd in bot.tree.get_commands()} == EXPECTED


def test_command_names_need_no_input_method_switch():
    """Typing a slash command should never require switching to a Korean IME."""
    for cmd in leaf_commands(make_bot()):
        assert cmd.name.isascii(), cmd.name
        for param in cmd.parameters:
            assert param.display_name.isascii(), param.display_name


def test_every_command_has_a_description():
    # Discord rejects commands with an empty description.
    for cmd in leaf_commands(make_bot()):
        assert cmd.description
        assert len(cmd.description) <= 100


def test_add_command_options():
    bot = make_bot()
    add = next(c for c in leaf_commands(bot) if c.qualified_name == "add")
    assert {p.display_name for p in add.parameters} == {
        "task",
        "when",
        "at",
        "category",
        "project",
        "cal",
    }


def test_descriptions_are_english_too():
    """Every command reads as English in Discord's own UI, Karrot included.

    The channel's replies are Korean on purpose, but the command picker is
    where you choose a command before any reply exists — and a picker that
    switches language mid-list is just harder to scan.
    """
    for cmd in leaf_commands(make_bot()):
        assert cmd.description.isascii(), (cmd.qualified_name, cmd.description)
        for param in cmd.parameters:
            if (cmd.qualified_name, param.display_name) in KOREAN_VALUE_OPTIONS:
                continue
            assert param.description.isascii(), (cmd.qualified_name, param.description)


def test_korean_values_are_still_advertised_where_they_are_the_values():
    """The exception has to keep earning itself: if these stop listing the
    Korean words, nobody learns they can be typed."""
    # Qualified, because /add and /karrot add share a name.
    commands = {c.qualified_name: c for c in leaf_commands(make_bot())}
    for qualified, option_name, word in (("add", "when", "내일"), ("change", "day", "내일")):
        option = next(
            p for p in commands[qualified].parameters if p.display_name == option_name
        )
        assert word in option.description, qualified


def test_add_advertises_the_korean_date_words():
    add = next(c for c in leaf_commands(make_bot()) if c.qualified_name == "add")
    when = next(p for p in add.parameters if p.display_name == "when")
    for word in ("오늘", "내일", "모레"):
        assert word in when.description


def test_option_descriptions_fit_discords_limit():
    for cmd in leaf_commands(make_bot()):
        for param in cmd.parameters:
            assert len(param.description) <= 100, (cmd.name, param.display_name)


# -- number parsing --------------------------------------------------------
# /done and /delete take several numbers at once and resolve them against one
# snapshot. Running them one at a time would let an earlier change shift the
# list under the later numbers.


def test_a_single_number_still_works():
    assert parse_numbers("3") == [3]


@pytest.mark.parametrize("text", ["3,5", "3 5", "3, 5", " 3 ,5 "])
def test_separators_people_actually_type(text):
    assert parse_numbers(text) == [3, 5]


def test_typed_order_is_preserved():
    """The reply reads back in the order they asked for."""
    assert parse_numbers("5,3") == [5, 3]


def test_duplicates_collapse():
    assert parse_numbers("3,3,5") == [3, 5]


@pytest.mark.parametrize("text", ["", "   ", ",", " , "])
def test_nothing_to_act_on_is_rejected(text):
    with pytest.raises(ValueError):
        parse_numbers(text)


def test_non_numbers_are_rejected_by_name():
    with pytest.raises(ValueError, match="three"):
        parse_numbers("3,three")


def test_chat_is_off_without_an_api_key():
    """No key means no message_content intent either — requesting a privileged
    intent the app doesn't need is what makes a bot fail to connect."""
    bot = make_bot()
    assert bot.chat is None
    assert bot.intents.message_content is False


def test_chat_and_its_intent_turn_on_together():
    client = FakeNotionClient()
    config = Config()
    bot = AlliegentBot(
        config=config,
        agenda=AgendaService(client, config, "agenda-db"),
        projects=None,
        secrets=Secrets(discord_channel_id=123, anthropic_api_key="sk-test"),
    )
    assert bot.chat is not None
    assert bot.intents.message_content is True


def test_chat_can_be_disabled_even_with_a_key():
    """The fallback when the portal intent isn't enabled: keep the scheduled
    jobs running rather than refusing to start."""
    client = FakeNotionClient()
    config = Config()
    bot = AlliegentBot(
        config=config,
        agenda=AgendaService(client, config, "agenda-db"),
        projects=None,
        secrets=Secrets(discord_channel_id=123, anthropic_api_key="sk-test"),
        enable_chat=False,
    )
    assert bot.chat is None
    assert bot.intents.message_content is False


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
    "karrot": "karrot",
    "assets": "assets",
}

# Short write confirmations answer in place: routing a one-line "Added — X"
# would turn every write into two messages.
INLINE_COMMANDS = {"add", "done", "delete", "change"}


def test_every_command_either_routes_or_is_deliberately_inline():
    names = {cmd.name for cmd in make_bot().tree.get_commands()}
    assert names == set(COMMAND_CHANNELS) | INLINE_COMMANDS


def test_routed_kinds_all_resolve_to_a_channel():
    secrets = Secrets(discord_channel_id=1)
    for kind in set(COMMAND_CHANNELS.values()):
        assert secrets.channel_for(kind) > 0


# -- /add and the calendar -------------------------------------------------


def _should_mirror(cal: bool | None, has_time: bool) -> bool:
    """The rule /add applies, written out so it can be checked directly."""
    return cal if cal is not None else has_time


def test_a_timed_item_goes_to_the_calendar_by_default():
    """Something happening at an hour is what a calendar is for."""
    assert _should_mirror(None, True)


def test_a_bare_task_stays_out_of_the_calendar():
    """The reason this is not always-on: most agenda rows are chores, and a
    calendar full of them stops showing what the day is committed to."""
    assert not _should_mirror(None, False)


def test_cal_false_keeps_a_timed_item_out():
    assert not _should_mirror(False, True)


def test_cal_true_puts_an_untimed_item_in():
    assert _should_mirror(True, False)


# -- acting on the backlog -------------------------------------------------


def test_overdue_is_recognised_as_a_list_to_act_on():
    """`/delete 2 overdue` has to mean the backlog. Delayed items span days,
    so there is otherwise no day argument that reaches them."""
    from alliegent.integrations.discord_bot import wants_overdue

    for word in ("overdue", "Overdue", "od", "late", "밀린", "overdue."):
        assert wants_overdue(word), word


def test_a_day_is_not_mistaken_for_the_backlog():
    from alliegent.integrations.discord_bot import wants_overdue

    for word in (None, "", "today", "tomorrow", "2026-08-15", "내일"):
        assert not wants_overdue(word), word


# -- typing the whole command into one field -------------------------------


def test_a_day_typed_after_the_numbers_is_read_as_the_day():
    """Discord fills one option at a time and only advances on Tab, so
    `/done 1 overdue` typed straight through puts both words in `numbers` —
    and the reply was "Not a number: 'overdue'", which reads like the command
    is broken rather than like a typing rule was missed."""
    from alliegent.integrations.discord_bot import split_numbers_and_when

    assert split_numbers_and_when("1 overdue") == ("1", "overdue")
    assert split_numbers_and_when("1,3 tomorrow") == ("1 3", "tomorrow")
    assert split_numbers_and_when("2 08-15") == ("2", "08-15")


def test_numbers_alone_leave_no_day_behind():
    from alliegent.integrations.discord_bot import split_numbers_and_when

    for text in ("1", "1,3", "1 3", "  2  "):
        numbers, when = split_numbers_and_when(text)
        assert when == "", text
        assert numbers


def test_everything_after_the_first_word_belongs_to_the_day():
    """Korean date words are two syllables and no digits, and a date can carry
    a space; splitting on the first non-number keeps both intact."""
    from alliegent.integrations.discord_bot import split_numbers_and_when

    assert split_numbers_and_when("3 5 내일") == ("3 5", "내일")
    assert split_numbers_and_when("1 day after tomorrow") == (
        "1",
        "day after tomorrow",
    )


# -- starting up with features switched off ---------------------------------


def test_a_feature_without_its_database_does_not_require_its_channel():
    """Deploying a feature switched off must not stop the rest from starting.
    Requiring the English channel before it existed did exactly that."""
    from alliegent.main import enabled_routes

    secrets = Secrets(discord_channel_id=0, discord_agenda_channel_id=1)
    routes = enabled_routes(secrets)
    assert "english" not in routes and "karrot" not in routes and "assets" not in routes


def test_a_configured_feature_does_require_its_channel():
    from alliegent.main import enabled_routes

    secrets = Secrets(notion_english_lessons_db_id="db")
    assert "english" in enabled_routes(secrets)


def test_the_core_routes_are_always_required():
    from alliegent.main import enabled_routes

    assert {"agenda", "review", "news"} <= set(enabled_routes(Secrets()))


def test_buying_on_karrot_has_its_own_subcommand():
    """Not a third choice on /karrot spent: that one records what selling
    cost, and a purchase filed there would come off Net."""
    names = {c.qualified_name for c in leaf_commands(make_bot())}
    assert "karrot bought" in names and "karrot spent" in names


# -- projects --------------------------------------------------------------


async def test_naming_a_project_without_a_projects_database_is_refused():
    """The same refusal /projects gives, rather than an AttributeError in the
    log and a silent failure in the channel."""
    client = FakeNotionClient()
    config = Config()
    bot = AlliegentBot(
        config=config,
        agenda=AgendaService(client, config, "agenda-db"),
        projects=None,
        secrets=Secrets(discord_channel_id=123),
    )
    with pytest.raises(ValueError, match="NOTION_PROJECTS_DB_ID"):
        await resolve_project(bot, "뭐든지")


def test_the_words_that_clear_a_value_are_the_same_everywhere():
    """`at` has taken 'none' to mean "take it off" since /time; `project`
    reuses the vocabulary rather than inventing a second one."""
    assert means_clear("none") and means_clear("없음") and means_clear("-")
    assert not means_clear("블로그")


# -- /change ---------------------------------------------------------------
# The one command for editing an item: the day and the time it replaced
# /move and /time with, plus the category and the title, which had no command
# at all and meant opening Notion.


def test_change_covers_every_editable_field():
    change = next(c for c in leaf_commands(make_bot()) if c.qualified_name == "change")
    assert {p.display_name for p in change.parameters} == {
        "numbers",
        "day",
        "at",
        "category",
        "project",
        "name",
        "from",
    }


def test_change_asks_for_nothing_required_but_the_numbers():
    """Every field is optional on its own; the command checks that at least
    one was given, so `/change 3` alone is refused with a message rather than
    by Discord's own validation."""
    change = next(c for c in leaf_commands(make_bot()) if c.qualified_name == "change")
    required = {p.display_name for p in change.parameters if p.required}
    assert required == {"numbers"}
