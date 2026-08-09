"""Entrypoint: one process running the Discord bot and the job scheduler."""

from __future__ import annotations

import asyncio
import logging

import discord

from .agenda import AgendaService, ProjectService
from .config import get_config, get_secrets
from .integrations.discord_bot import AlliegentBot
from .integrations.notion import NotionClient
from .scheduler import build_scheduler

log = logging.getLogger(__name__)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    # Text-only bot; the missing-voice-support warning is noise.
    logging.getLogger("discord.client").setLevel(logging.ERROR)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


async def main() -> None:
    configure_logging()
    config = get_config()
    secrets = get_secrets()
    secrets.require("notion_token", "notion_agenda_db_id", "discord_bot_token")
    # Resolve every route up front so a missing channel fails at boot rather
    # than silently swallowing a scheduled message days later.
    for kind in ("agenda", "projects", "review", "news"):
        secrets.channel_for(kind)
    if not secrets.anthropic_api_key:
        log.warning("ANTHROPIC_API_KEY not set — the AI news digest is disabled")

    client = NotionClient(secrets.notion_token)
    agenda = AgendaService(client, config, secrets.notion_agenda_db_id)
    projects = (
        ProjectService(client, config, secrets.notion_projects_db_id)
        if secrets.notion_projects_db_id
        else None
    )
    if projects is None:
        log.warning("NOTION_PROJECTS_DB_ID not set — project features are disabled")

    def make_bot(enable_chat: bool) -> AlliegentBot:
        return AlliegentBot(
            config=config,
            agenda=agenda,
            projects=projects,
            secrets=secrets,
            guild_id=secrets.discord_guild_id,
            enable_chat=enable_chat,
        )

    bot = make_bot(enable_chat=True)
    scheduler = build_scheduler(bot.jobs, config)

    try:
        try:
            async with bot:
                scheduler.start()
                await bot.start(secrets.discord_bot_token)
        except discord.PrivilegedIntentsRequired:
            # Reading mentions needs the Message Content intent, which has to
            # be switched on in the Discord developer portal. Refusing to start
            # over a chat feature would take the scheduled jobs down with it,
            # so drop the feature and carry on.
            log.error(
                "MESSAGE CONTENT INTENT is not enabled for this bot, so replying to "
                "mentions is off. Enable it at discord.com/developers → your app → "
                "Bot → Privileged Gateway Intents, then redeploy. Everything else "
                "is running."
            )
            # The scheduler holds the old bot's notifier, which is now closed —
            # rebuild it, or every scheduled alert would silently go nowhere.
            if scheduler.running:
                scheduler.shutdown(wait=False)
            bot = make_bot(enable_chat=False)
            scheduler = build_scheduler(bot.jobs, config)
            async with bot:
                scheduler.start()
                await bot.start(secrets.discord_bot_token)
    finally:
        if scheduler.running:
            scheduler.shutdown(wait=False)
        await client.aclose()


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("shutting down")


if __name__ == "__main__":
    run()
