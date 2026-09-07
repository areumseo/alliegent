"""Discord bot: slash commands plus the notifier the scheduled jobs push to."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Coroutine
from datetime import date, datetime, time, timedelta
from typing import Any

import discord
from discord import app_commands

from .. import reports
from ..agenda import AgendaService, ProjectService
from ..chat import ChatAgent, strip_mentions
from ..config import Config, Secrets
from ..jobs import Jobs
from .calendar import make_source

log = logging.getLogger(__name__)


class AlliegentBot(discord.Client):
    def __init__(
        self,
        *,
        config: Config,
        agenda: AgendaService,
        projects: ProjectService | None,
        secrets: Secrets,
        guild_id: int = 0,
        enable_chat: bool = True,
    ) -> None:
        # message_content is a privileged intent, needed to read what someone
        # says when they mention the bot. It has to be enabled in the Discord
        # developer portal too; without it messages arrive with empty content
        # and the bot looks like it is ignoring you.
        chat_on = enable_chat and bool(secrets.anthropic_api_key)
        intents = discord.Intents.default()
        intents.message_content = chat_on
        super().__init__(intents=intents)
        self.config = config
        self.agenda = agenda
        self.projects = projects
        self.secrets = secrets
        self.guild_id = guild_id
        self.tree = app_commands.CommandTree(self)
        self._tasks: set[asyncio.Task[None]] = set()
        self.chat = (
            ChatAgent(secrets.anthropic_api_key, agenda, config, secrets)
            if chat_on
            else None
        )
        self.jobs = Jobs(
            agenda,
            projects,
            config,
            self.notify,
            anthropic_api_key=secrets.anthropic_api_key,
            calendar_source=make_source(secrets),
            secrets=secrets,
        )
        _register(self)

    async def setup_hook(self) -> None:
        # Guild-scoped commands appear instantly; global ones take up to an hour.
        if self.guild_id:
            guild = discord.Object(id=self.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash commands synced to guild %s", self.guild_id)
        else:
            await self.tree.sync()
            log.info("Slash commands synced globally (may take up to 1 hour)")

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)

    async def on_message(self, message: discord.Message) -> None:
        """Answer when mentioned. Only when mentioned: reacting to everything
        would talk over conversations and bill for the privilege."""
        if self.chat is None or self.user is None:
            return
        if message.author.bot or not self.user.mentioned_in(message):
            return
        # @everyone / @here mention the bot too, and are not addressed to it.
        if message.mention_everyone:
            return

        text = strip_mentions(message.content, self.user.id)
        if not text:
            return

        async with message.channel.typing():
            try:
                reply = await self.chat.respond(message.channel.id, text)
            except Exception as exc:
                log.exception("chat failed")
                reply = f"⚠️ Something went wrong: {type(exc).__name__}"
        for part in reports.chunk(reply):
            await message.reply(part, mention_author=False)

    async def notify(self, message: str, kind: str = "agenda") -> None:
        """Push a scheduled message to the channel configured for `kind`."""
        try:
            channel_id = self.secrets.channel_for(kind)
        except RuntimeError as exc:
            log.error("%s", exc)
            return

        channel = self.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id)
            except discord.HTTPException as exc:
                log.error("Cannot reach channel %s (%s): %s", channel_id, kind, exc)
                return
        if not isinstance(channel, discord.abc.Messageable):
            log.error("Channel %s is not messageable", channel_id)
            return
        for part in reports.chunk(message):
            # Link previews would undo the point of a compact digest: five
            # articles means five cards, each taller than the entry itself.
            await channel.send(part, suppress_embeds=True)

    def today(self) -> date:
        return datetime.now(self.config.tz).date()

    def spawn(self, coro: Coroutine[Any, Any, None]) -> None:
        """Run a slow command in the background.

        The task is held in a set until it finishes: asyncio only keeps a weak
        reference, so a task nothing holds can be garbage-collected mid-flight
        and simply never complete.
        """
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)


async def _reply(interaction: discord.Interaction, message: str) -> None:
    """Send a possibly-long message as one response plus followups."""
    parts = reports.chunk(message)
    for part in parts:
        await interaction.followup.send(part, suppress_embeds=True)


async def _deliver(
    bot: AlliegentBot, interaction: discord.Interaction, message: str, kind: str
) -> None:
    """Post to the channel this kind belongs to, wherever it was invoked from.

    A command and its scheduled twin should land in the same place, or the
    archive ends up split across whichever channel someone happened to be in.
    Invoked from that channel already, it just replies inline — posting there
    and acknowledging here would duplicate it.
    """
    try:
        target = bot.secrets.channel_for(kind)
    except RuntimeError:
        await _reply(interaction, message)
        return

    if interaction.channel_id == target:
        await _reply(interaction, message)
        return

    await bot.notify(message, kind)
    channel = bot.get_channel(target)
    name = f"#{channel.name}" if isinstance(channel, discord.TextChannel) else "its channel"
    await interaction.followup.send(f"📨 Posted to {name}.")


CLEAR_TIME = {"none", "clear", "off", "없음", "-"}


def parse_time(text: str | None) -> time | None:
    """Accept '14:00', '2pm', '2:30 PM', '11AM', or a word meaning no time.

    Returns None both for "nothing given" and for "clear it": in a day ordered
    by time those are the same thing -- the item goes to the end -- so the
    caller does not have to tell them apart.
    """
    if not text:
        return None
    value = text.strip().strip(".!,").strip().casefold().replace(" ", "")
    if value in CLEAR_TIME:
        return None
    match = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?(am|pm)?", value)
    if not match:
        raise ValueError(
            f"Could not read a time from {text!r}. Try 14:00, 2pm, or 9:30am."
        )
    hour, minute, meridiem = int(match.group(1)), int(match.group(2) or 0), match.group(3)
    if meridiem:
        if not 1 <= hour <= 12:
            raise ValueError(f"{text!r} is not a valid 12-hour time.")
        hour = hour % 12 + (12 if meridiem == "pm" else 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"{text!r} is not a valid time.")
    return time(hour, minute)


# Times people write into the task itself: "Cafe shift 11AM", "Dance class 7:10PM".
TITLE_TIME = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap]m)\b", re.IGNORECASE)


def time_in_title(title: str) -> time | None:
    """The clock time written into a title, if there is one.

    Items get typed as "Cafe shift 11AM" out of habit, and that reading is already
    the time the item happens at -- taking it means the day sorts correctly
    without anyone learning a new argument.
    """
    match = TITLE_TIME.search(title)
    if not match:
        return None
    try:
        return parse_time(
            f"{match.group(1)}:{match.group(2) or '00'}{match.group(3).lower()}"
        )
    except ValueError:
        return None


async def _add_to_calendar(
    bot: AlliegentBot, summary: str, day: date, clock: time | None
) -> str:
    """Mirror an agenda item into the calendar, reporting either way.

    The Notion row is already written by the time this runs, so a calendar
    failure must not read as the task having failed -- it is one line saying
    the calendar half did not happen, not an error for the whole command.
    """
    from .calendar import DEFAULT_EVENT_MINUTES, CalendarWriteError, create_event

    if clock is None:
        # No hour to put it at, so it becomes an all-day entry rather than an
        # arbitrary one; an all-day event in iCal ends the following midnight.
        start: date | datetime = day
        end: date | datetime = day + timedelta(days=1)
        shown = "all day"
    else:
        start = datetime.combine(day, clock, tzinfo=bot.config.tz)
        end = start + timedelta(minutes=DEFAULT_EVENT_MINUTES)
        shown = f"{reports.fmt_time(clock)}–{reports.fmt_time(end.time())}"

    try:
        where = await create_event(bot.secrets, summary, start, end)
    except CalendarWriteError as exc:
        return f"⚠️ Not added to the calendar: {exc}"
    except Exception as exc:
        log.exception("calendar write failed")
        return f"⚠️ Not added to the calendar: {type(exc).__name__}"
    return f"📅 Calendar — {where}, {shown}"


def parse_day(text: str | None, today: date) -> date:
    """Accept 'today', 'tomorrow', 'MM-DD', 'YYYY-MM-DD', or the Korean
    equivalents, which are shorter to type on a Korean keyboard."""
    if not text:
        return today
    # Trailing punctuation and capitalisation both come free from phone
    # keyboards; "Tomorrow." should not be a parse error.
    value = text.strip().strip(".!,").strip()
    relative = {
        "오늘": 0,
        "내일": 1,
        "모레": 2,
        "내일모레": 2,
        "today": 0,
        "tod": 0,
        "tomorrow": 1,
        "tmr": 1,
        "tmrw": 1,
        "day after tomorrow": 2,
    }
    key = value.casefold()
    if key in relative:
        return today + timedelta(days=relative[key])
    # Year-less input is assumed to mean the current year, so it is filled in
    # before parsing rather than after (strptime defaults to 1900 otherwise).
    for fmt, candidate in (
        ("%Y-%m-%d", value),
        ("%Y-%m-%d", f"{today.year}-{value}"),
        ("%Y-%m/%d", f"{today.year}-{value}"),
    ):
        try:
            return datetime.strptime(candidate, fmt).date()
        except ValueError:
            continue
    raise ValueError(
        f"Couldn't read that date: {text!r} — try today, tomorrow, 2026-08-15, or 08-15"
    )


def parse_numbers(text: str) -> list[int]:
    """Parse "3", "3,5", "3 5", or "3, 5" into [3, 5].

    Duplicates are dropped and the order the user typed is kept, so the reply
    reads back in the order they asked for.
    """
    seen: list[int] = []
    for chunk in text.replace(",", " ").split():
        try:
            value = int(chunk)
        except ValueError as exc:
            raise ValueError(f"Not a number: {chunk!r}") from exc
        if value not in seen:
            seen.append(value)
    if not seen:
        raise ValueError("Give at least one number.")
    return seen


OVERDUE_WORDS = {"overdue", "od", "late", "밀린", "지난"}


def wants_overdue(when: str | None) -> bool:
    """Whether `when` names the backlog rather than a day.

    Kept separate from parse_day: the overdue list is not a date, and making
    it resolve to one would put every item on the same wrong day.
    """
    if not when:
        return False
    return when.strip().strip(".!,").casefold() in OVERDUE_WORDS


async def _resolve(
    bot: AlliegentBot, interaction: discord.Interaction, numbers: str, when: str | None
) -> tuple[list, date | None] | None:
    """Map typed numbers onto items, against a single snapshot.

    One fetch for the whole command: resolving each number separately would
    let an earlier completion or deletion shift the list under the later ones,
    so `/delete 3,5` would remove item 3 and then whatever slid into 5.

    `when` is a day, or "overdue" for the backlog. Overdue items span days, so
    the day comes back as None and callers that need one read it off each
    item -- which is the item's real date either way.
    """
    try:
        wanted = parse_numbers(numbers)
    except ValueError as exc:
        await interaction.followup.send(f"⚠️ {exc}")
        return None

    if wants_overdue(when):
        day = None
        items = await bot.agenda.overdue(bot.today())
        empty = "⚠️ Nothing overdue."
        where = "The overdue list"
    else:
        try:
            day = parse_day(when, bot.today())
        except ValueError as exc:
            await interaction.followup.send(f"⚠️ {exc}")
            return None
        items = await bot.agenda.items_on(day)
        empty = f"⚠️ Nothing scheduled on {reports.fmt_date(day)}."
        where = reports.fmt_date(day)

    if not items:
        await interaction.followup.send(empty)
        return None

    bad = [n for n in wanted if not 1 <= n <= len(items)]
    if bad:
        listed = ", ".join(str(n) for n in bad)
        await interaction.followup.send(
            f"⚠️ Out of range: {listed}. {where} has {len(items)} item(s)."
        )
        return None
    return [items[n - 1] for n in wanted], day


def _register(bot: AlliegentBot) -> None:
    tree = bot.tree

    @tree.command(name="today", description="Show today's agenda")
    async def today_cmd(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        day = bot.today()
        items = await bot.agenda.items_on(day)
        await _deliver(bot, interaction, reports.today_list(day, items), "agenda")

    @tree.command(name="tomorrow", description="Show tomorrow's agenda")
    async def tomorrow_cmd(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        day = bot.today() + timedelta(days=1)
        items = await bot.agenda.items_on(day)
        await _deliver(
            bot,
            interaction,
            reports.day_list(day, items, today=bot.today()),
            "agenda",
        )

    @tree.command(name="status", description="Progress for a day and its week")
    @app_commands.describe(when="Which day (defaults to today)")
    async def status_cmd(interaction: discord.Interaction, when: str | None = None) -> None:
        await interaction.response.defer()
        today = bot.today()
        try:
            day = parse_day(when, today)
        except ValueError as exc:
            await interaction.followup.send(f"⚠️ {exc}")
            return
        monday = day - timedelta(days=day.weekday())
        todays = await bot.agenda.items_on(day)
        overdue = await bot.agenda.overdue(day)
        week = await bot.agenda.items_between(monday, monday + timedelta(days=6))
        await _deliver(
            bot,
            interaction,
            reports.status(day, todays, overdue, week, today=today),
            "agenda",
        )

    @tree.command(name="add", description="Add an item to the agenda")
    @app_commands.describe(
        task="What to add",
        # This is the only place a user finds out Korean words work, so they
        # belong here and not just in the README.
        when="오늘 / 내일 / 모레 / today / tomorrow / 2026-08-15 / 08-15 (default: today)",
        at="Time of day, e.g. 14:00 or 2pm. Without one it goes to the end of the day",
        cal="Also put it in the calendar. Defaults to on for items with a time",
    )
    async def add_cmd(
        interaction: discord.Interaction,
        task: str,
        when: str | None = None,
        at: str | None = None,
        cal: bool | None = None,
    ) -> None:
        await interaction.response.defer()
        try:
            day = parse_day(when, bot.today())
            # A time typed into the task itself ("Cafe shift 11AM") is the same
            # information, so it counts -- otherwise the habit of writing it
            # there would quietly leave the day unordered.
            clock = parse_time(at) or time_in_title(task)
        except ValueError as exc:
            await interaction.followup.send(f"⚠️ {exc}")
            return

        item = await bot.agenda.add_item(task, day, at=clock, infer_category=True)

        filed = f" · {item.category}" if item.category else ""
        when_text = reports.fmt_date(day)
        if clock:
            when_text += f" {reports.fmt_time(clock)}"
        lines = [f"✅ Added — **{item.title}** ({when_text}{filed})"]

        # An item with a time is something that happens at an hour, which is
        # what a calendar is for; a bare task is not. `cal` overrides both
        # ways, since some timed items are still just tasks.
        if cal if cal is not None else clock is not None:
            lines.append(await _add_to_calendar(bot, item.title, day, clock))
        await interaction.followup.send("\n".join(lines))

    @tree.command(name="done", description="Mark items done by their listed number")
    @app_commands.describe(
        numbers="Number(s) from the list, e.g. 3 or 3,5",
        when="Which list: a day like tomorrow or 08-15, or `overdue` (defaults to today)",
    )
    async def done_cmd(
        interaction: discord.Interaction, numbers: str, when: str | None = None
    ) -> None:
        await interaction.response.defer()
        resolved = await _resolve(bot, interaction, numbers, when)
        if resolved is None:
            return
        chosen, day = resolved

        already = [i.title for i in chosen if i.done]
        marked = []
        for item in chosen:
            if item.done:
                continue
            await bot.agenda.set_done(item.id)
            marked.append(item.title)

        # The date goes in the confirmation: numbers are per-day, so naming the
        # day is what makes a wrong one obvious straight away.
        lines = []
        if marked:
            titles = ", ".join(f"**{t}**" for t in marked)
            lines.append(f"✅ Done — {titles} ({reports.fmt_date(day)})")
        if already:
            lines.append("Already done — " + ", ".join(already))
        await interaction.followup.send("\n".join(lines))

    @tree.command(
        name="delete", description="Move items to Notion's trash by their listed number"
    )
    @app_commands.describe(
        numbers="Number(s) from the list, e.g. 3 or 3,5",
        when="Which list: a day like tomorrow or 08-15, or `overdue` (defaults to today)",
    )
    async def delete_cmd(
        interaction: discord.Interaction, numbers: str, when: str | None = None
    ) -> None:
        await interaction.response.defer()
        resolved = await _resolve(bot, interaction, numbers, when)
        if resolved is None:
            return
        chosen, day = resolved

        for item in chosen:
            await bot.agenda.trash(item.id)
        titles = ", ".join(f"**{i.title}**" for i in chosen)
        await interaction.followup.send(
            f"🗑️ Moved to trash — {titles} ({reports.fmt_date(day)}, "
            "recoverable in Notion)"
        )

    @tree.command(name="move", description="Move items to another day by their listed number")
    @app_commands.describe(
        numbers="Number(s) from the list, e.g. 3 or 3,5",
        to="Where to move them, e.g. tomorrow or 08-20",
        frm="Which day they're on now (defaults to today)",
    )
    @app_commands.rename(frm="from")
    async def move_cmd(
        interaction: discord.Interaction,
        numbers: str,
        to: str,
        frm: str | None = None,
    ) -> None:
        await interaction.response.defer()
        resolved = await _resolve(bot, interaction, numbers, frm)
        if resolved is None:
            return
        chosen, source = resolved

        try:
            target = parse_day(to, bot.today())
        except ValueError as exc:
            await interaction.followup.send(f"⚠️ {exc}")
            return
        if target == source:
            await interaction.followup.send(
                f"⚠️ They're already on {reports.fmt_date(target)}."
            )
            return

        for item in chosen:
            await bot.agenda.reschedule(item.id, target, item.at)
        titles = ", ".join(f"**{i.title}**" for i in chosen)
        # Both dates: moving is the one write whose result is invisible on the
        # day you ran it from, so the message has to say where things went.
        await interaction.followup.send(
            f"📅 Moved {titles} — {reports.fmt_date(source)} → "
            f"{reports.fmt_date(target)}"
        )

    @tree.command(name="time", description="Set or clear an item's time of day")
    @app_commands.describe(
        numbers="Which items, by their listed number (3 or 3,5)",
        at="14:00, 2pm, 9:30am, or 'none' to clear it and send it to the end",
        when="Which day, or `overdue` (defaults to today)",
    )
    async def time_cmd(
        interaction: discord.Interaction,
        numbers: str,
        at: str,
        when: str | None = None,
    ) -> None:
        await interaction.response.defer()
        resolved = await _resolve(bot, interaction, numbers, when)
        if resolved is None:
            return
        items, day = resolved

        try:
            clock = parse_time(at)
        except ValueError as exc:
            await interaction.followup.send(f"⚠️ {exc}")
            return

        for item in items:
            # The item's own date, not the day argument: an overdue item keeps
            # the day it was scheduled for, and for a day list they are equal.
            await bot.agenda.set_time(item.id, item.day or day or bot.today(), clock)

        titles = ", ".join(f"**{item.title}**" for item in items)
        moved_to = reports.fmt_time(clock) if clock else "no time (end of day)"
        on = reports.fmt_date(day) if day else "their own days"
        await interaction.followup.send(f"🕘 {titles} — {moved_to} on {on}")

    @tree.command(name="overdue", description="Show overdue, unfinished items")
    async def overdue_cmd(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        items = await bot.agenda.overdue(bot.today())
        await _deliver(bot, interaction, reports.overdue_list(items), "agenda")

    @tree.command(name="projects", description="Show active projects")
    async def projects_cmd(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if bot.projects is None:
            await interaction.followup.send(
                "⚠️ No projects database configured (NOTION_PROJECTS_DB_ID)."
            )
            return
        projects = reports.project_list(await bot.projects.active())
        await _deliver(bot, interaction, projects, "projects")

    @tree.command(name="brief", description="Run the daily brief now")
    async def brief_cmd(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await _deliver(bot, interaction, await bot.jobs.build_daily_brief(), "agenda")

    @tree.command(name="news", description="Fetch today's AI news digest now")
    async def news_cmd(interaction: discord.Interaction) -> None:
        # Searching the web and writing ten summaries takes minutes, not
        # seconds. Waiting on it would leave the invoker watching a spinner
        # with no idea whether anything is happening, so acknowledge now and
        # let the digest arrive in its channel when it's ready.
        await interaction.response.send_message(
            "🔎 Searching — the digest will land in the news channel shortly.",
            ephemeral=True,
        )

        async def run() -> None:
            digest = await bot.jobs.build_ai_news()
            if digest is None:
                await interaction.followup.send(
                    "⚠️ Couldn't fetch the news digest — check the logs.",
                    ephemeral=True,
                )
                return
            await bot.notify(digest, "news")

        bot.spawn(run())

    @tree.error
    async def on_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        log.exception("Command failed", exc_info=error)
        message = f"⚠️ Something went wrong.\n```{error}```"
        if interaction.response.is_done():
            await interaction.followup.send(message)
        else:
            await interaction.response.send_message(message, ephemeral=True)
