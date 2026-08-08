"""Discord bot: slash commands plus the notifier the scheduled jobs push to."""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

import discord
from discord import app_commands

from .. import reports
from ..agenda import AgendaService, ProjectService
from ..config import Config
from ..jobs import Jobs

log = logging.getLogger(__name__)


class AlliegentBot(discord.Client):
    def __init__(
        self,
        *,
        config: Config,
        agenda: AgendaService,
        projects: ProjectService | None,
        channel_id: int,
        guild_id: int = 0,
    ) -> None:
        # Slash commands need no privileged intents; message content is never read.
        super().__init__(intents=discord.Intents.default())
        self.config = config
        self.agenda = agenda
        self.projects = projects
        self.channel_id = channel_id
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

    async def notify(self, message: str) -> None:
        """Push a scheduled message to the configured channel."""
        channel = self.get_channel(self.channel_id)
        if channel is None:
            try:
                channel = await self.fetch_channel(self.channel_id)
            except discord.HTTPException as exc:
                log.error("Cannot reach channel %s: %s", self.channel_id, exc)
                return
        if not isinstance(channel, discord.abc.Messageable):
            log.error("Channel %s is not messageable", self.channel_id)
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
    """Accept '오늘', '내일', '모레', 'MM-DD', or 'YYYY-MM-DD'."""
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
    raise ValueError(f"날짜를 이해하지 못했습니다: {text!r} (예: 오늘, 내일, 2026-08-15, 08-15)")


def _register(bot: AlliegentBot) -> None:
    tree = bot.tree

    @tree.command(name="오늘", description="오늘 아젠다를 보여줍니다")
    async def today_cmd(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        day = bot.today()
        items = await bot.agenda.items_on(day)
        await _reply(interaction, reports.today_list(day, items))

    @tree.command(name="추가", description="아젠다에 할 일을 추가합니다")
    @app_commands.rename(task="할일", when="날짜")
    @app_commands.describe(
        task="추가할 할 일",
        when="오늘 / 내일 / 2026-08-15 / 08-15 (생략하면 오늘)",
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
            f"✅ 추가했습니다 — **{item.title}** ({reports.fmt_date(day)})"
        )

    @tree.command(name="완료", description="오늘 목록의 번호로 완료 처리합니다")
    @app_commands.rename(number="번호")
    @app_commands.describe(number="`/오늘` 목록에 표시된 번호")
    async def done_cmd(interaction: discord.Interaction, number: int) -> None:
        await interaction.response.defer()
        day = bot.today()
        # Re-fetch in the same order /오늘 uses, so the numbers still line up
        # without keeping hidden state between commands.
        items = await bot.agenda.items_on(day)
        if not 1 <= number <= len(items):
            hint = (
                f"⚠️ 1~{len(items)} 사이의 번호를 입력해주세요."
                if items
                else "⚠️ 오늘 항목이 없습니다."
            )
            await interaction.followup.send(hint)
            return
        item = items[number - 1]
        await bot.agenda.set_done(item.id)
        await interaction.followup.send(f"✅ 완료 — **{item.title}**")

    @tree.command(name="밀린것", description="기한이 지난 미완료 항목을 보여줍니다")
    async def overdue_cmd(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        items = await bot.agenda.overdue(bot.today())
        if not items:
            await interaction.followup.send("🎉 밀린 항목이 없습니다.")
            return
        lines = [f"**밀린 항목 ({len(items)}건)**"]
        for item in items:
            when = reports.fmt_date(item.day) if item.day else "날짜 없음"
            lines.append(f"⚠️ {item.title} — {when}")
        await _reply(interaction, "\n".join(lines))

    @tree.command(name="프로젝트", description="진행 중인 프로젝트를 보여줍니다")
    async def projects_cmd(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        if bot.projects is None:
            await interaction.followup.send(
                "⚠️ 프로젝트 DB가 설정되지 않았습니다 (NOTION_PROJECTS_DB_ID)."
            )
            return
        await _reply(interaction, reports.project_list(await bot.projects.active()))

    @tree.command(name="브리핑", description="데일리 브리핑을 지금 실행합니다")
    async def brief_cmd(interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        await _reply(interaction, await bot.jobs.build_daily_brief())

    @tree.error
    async def on_error(
        interaction: discord.Interaction, error: app_commands.AppCommandError
    ) -> None:
        log.exception("Command failed", exc_info=error)
        message = f"⚠️ 처리 중 오류가 발생했습니다.\n```{error}```"
        if interaction.response.is_done():
            await interaction.followup.send(message)
        else:
            await interaction.response.send_message(message, ephemeral=True)
