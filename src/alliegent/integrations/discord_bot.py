"""Discord bot: slash commands plus the notifier the scheduled jobs push to."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from datetime import date, datetime, timedelta
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
            await channel.send(part)

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
    await interaction.followup.send(parts[0])
    for part in parts[1:]:
        await interaction.followup.send(part)


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


async def _resolve(
    bot: AlliegentBot, interaction: discord.Interaction, numbers: str, when: str | None
) -> tuple[list, date] | None:
    """Map typed numbers onto one day's items, against a single snapshot.

    One fetch for the whole command: resolving each number separately would
    let an earlier completion or deletion shift the list under the later ones,
    so `/delete 3,5` would remove item 3 and then whatever slid into 5.
    """
    try:
        wanted = parse_numbers(numbers)
    except ValueError as exc:
        await interaction.followup.send(f"⚠️ {exc}")
        return None

    try:
        day = parse_day(when, bot.today())
    except ValueError as exc:
        await interaction.followup.send(f"⚠️ {exc}")
        return None

    items = await bot.agenda.items_on(day)
    if not items:
        await interaction.followup.send(
            f"⚠️ Nothing scheduled on {reports.fmt_date(day)}."
        )
        return None

    bad = [n for n in wanted if not 1 <= n <= len(items)]
    if bad:
        listed = ", ".join(str(n) for n in bad)
        await interaction.followup.send(
            f"⚠️ Out of range: {listed}. {reports.fmt_date(day)} has "
            f"{len(items)} item(s)."
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
    )
    async def add_cmd(
        interaction: discord.Interaction, task: str, when: str | None = None
    ) -> None:
        await interaction.response.defer()
        try:
            day = parse_day(when, bot.today())
        except ValueError as exc:
            await interaction.followup.send(f"⚠️ {exc}")
            return
        item = await bot.agenda.add_item(task, day, infer_category=True)
        filed = f" · {item.category}" if item.category else ""
        await interaction.followup.send(
            f"✅ Added — **{item.title}** ({reports.fmt_date(day)}{filed})"
        )

    @tree.command(name="done", description="Mark items done by their listed number")
    @app_commands.describe(
        numbers="Number(s) from the list, e.g. 3 or 3,5",
        when="Which day's list, e.g. tomorrow or 08-15 (defaults to today)",
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
        when="Which day's list, e.g. tomorrow or 08-15 (defaults to today)",
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
            await bot.agenda.reschedule(item.id, target)
        titles = ", ".join(f"**{i.title}**" for i in chosen)
        # Both dates: moving is the one write whose result is invisible on the
        # day you ran it from, so the message has to say where things went.
        await interaction.followup.send(
            f"📅 Moved {titles} — {reports.fmt_date(source)} → "
            f"{reports.fmt_date(target)}"
        )

    @tree.command(name="reorder", description="Rearrange a day by listed numbers")
    @app_commands.describe(
        order="New order, e.g. 3,1,2. Numbers you leave out keep their relative order",
        when="Which day (defaults to today)",
    )
    async def reorder_cmd(
        interaction: discord.Interaction, order: str, when: str | None = None
    ) -> None:
        await interaction.response.defer()
        resolved = await _resolve(bot, interaction, order, when)
        if resolved is None:
            return
        _, day = resolved

        try:
            sequence = parse_numbers(order)
        except ValueError as exc:
            await interaction.followup.send(f"⚠️ {exc}")
            return

        arranged = await bot.agenda.reorder(day, sequence)
        await _deliver(
            bot, interaction, reports.day_list(day, arranged, today=bot.today()), "agenda"
        )

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
