"""Discord bot: slash commands plus the notifier the scheduled jobs push to."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import discord
from discord import app_commands

from .. import reports
from ..agenda import AgendaService, ProjectService
from ..config import Config, Secrets
from ..jobs import Jobs

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
    ) -> None:
        # Slash commands need no privileged intents; message content is never read.
        super().__init__(intents=discord.Intents.default())
        self.config = config
        self.agenda = agenda
        self.projects = projects
        self.secrets = secrets
        self.guild_id = guild_id
        self.tree = app_commands.CommandTree(self)
        self.jobs = Jobs(agenda, projects, config, self.notify)
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


async def _reply(interaction: discord.Interaction, message: str) -> None:
    """Send a possibly-long message as one response plus followups."""
    parts = reports.chunk(message)
    await interaction.followup.send(parts[0])
    for part in parts[1:]:
        await interaction.followup.send(part)


def parse_day(text: str | None, today: date) -> date:
    """Accept 'today', 'tomorrow', 'MM-DD', 'YYYY-MM-DD', or the Korean
    equivalents, which are shorter to type on a Korean keyboard."""
    if not text:
        return today
    value = text.strip()
    relative = {"오늘": 0, "내일": 1, "모레": 2, "today": 0, "tomorrow": 1}
    if value in relative:
        return today + timedelta(days=relative[value])
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


def _register(bot: AlliegentBot) -> None:
    tree = bot.tree

    @tree.command(name="today", description="Show today's agenda")
    async def today_cmd(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        day = bot.today()
        items = await bot.agenda.items_on(day)
        await _reply(interaction, reports.today_list(day, items))

    @tree.command(name="add", description="Add an item to the agenda")
    @app_commands.describe(
        task="What to add",
        when="today / tomorrow / 2026-08-15 / 08-15 (defaults to today)",
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
        item = await bot.agenda.add_item(task, day)
        await interaction.followup.send(
            f"✅ Added — **{item.title}** ({reports.fmt_date(day)})"
        )

    @tree.command(name="done", description="Mark an item done by its number in /today")
    @app_commands.describe(number="The number shown in /today")
    async def done_cmd(interaction: discord.Interaction, number: int) -> None:
        await interaction.response.defer()
        day = bot.today()
        # Re-fetch in the same order /today uses, so the numbers still line up
        # without keeping hidden state between commands.
        items = await bot.agenda.items_on(day)
        if not 1 <= number <= len(items):
            hint = (
                f"⚠️ Pick a number between 1 and {len(items)}."
                if items
                else "⚠️ Nothing scheduled today."
            )
            await interaction.followup.send(hint)
            return
        item = items[number - 1]
        await bot.agenda.set_done(item.id)
        await interaction.followup.send(f"✅ Done — **{item.title}**")

    @tree.command(name="overdue", description="Show overdue, unfinished items")
    async def overdue_cmd(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        items = await bot.agenda.overdue(bot.today())
        await _reply(interaction, reports.overdue_list(items))

    @tree.command(name="projects", description="Show active projects")
    async def projects_cmd(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if bot.projects is None:
            await interaction.followup.send(
                "⚠️ No projects database configured (NOTION_PROJECTS_DB_ID)."
            )
            return
        await _reply(interaction, reports.project_list(await bot.projects.active()))

    @tree.command(name="brief", description="Run the daily brief now")
    async def brief_cmd(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await _reply(interaction, await bot.jobs.build_daily_brief())

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
