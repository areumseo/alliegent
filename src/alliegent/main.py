"""Entrypoint: one process running the Discord bot and the job scheduler."""

from __future__ import annotations

import asyncio
import logging

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
    for kind in ("agenda", "projects", "review"):
        secrets.channel_for(kind)

    client = NotionClient(secrets.notion_token)
    agenda = AgendaService(client, config, secrets.notion_agenda_db_id)
    projects = (
        ProjectService(client, config, secrets.notion_projects_db_id)
        if secrets.notion_projects_db_id
        else None
    )
    if projects is None:
        log.warning("NOTION_PROJECTS_DB_ID not set — project features are disabled")

    bot = AlliegentBot(
        config=config,
        agenda=agenda,
        projects=projects,
        secrets=secrets,
        guild_id=secrets.discord_guild_id,
    )
    scheduler = build_scheduler(bot.jobs, config)

    try:
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
