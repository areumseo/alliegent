"""Run a single job from the terminal, without waiting for its scheduled time.

Prints to stdout by default, so the Notion property mapping in alliegent.toml
can be checked before letting the scheduler loose:

    uv run python -m alliegent.cli brief
    uv run python -m alliegent.cli scaffold          # preview only
    uv run python -m alliegent.cli scaffold --commit # actually create rows
    uv run python -m alliegent.cli feeds             # what each news feed returns

Add --send to post the result to the Discord channel the job would normally
use. That verifies the token, the channel IDs, and the bot's permissions in
one shot, instead of waiting until 08:00 to find out something is wrong.
"""

from __future__ import annotations

import argparse
import asyncio
import logging

from .agenda import AgendaService, ProjectService
from .config import get_config, get_secrets
from .integrations.notion import NotionClient, NotionError
from .jobs import Jobs

JOBS = ("brief", "news", "incomplete", "planning", "scaffold", "stale", "review")

# Diagnostics rather than jobs: they report on the setup instead of producing
# a message, so they have nothing to post and no channel to post it to.
CHECKS = ("feeds",)

# Which channel each job posts to, mirroring jobs.py.
JOB_CHANNEL = {
    "brief": "agenda",
    "news": "news",
    "incomplete": "agenda",
    "planning": "agenda",
    "scaffold": "agenda",
    "stale": "projects",
    "review": "review",
}


async def send_to_discord(secrets, kind: str, message: str) -> None:
    """Log in, post to the channel for `kind`, log out.

    A one-shot connection rather than the long-lived bot: this is a check,
    not a service.
    """
    import discord

    from . import reports

    secrets.require("discord_bot_token")
    channel_id = secrets.channel_for(kind)

    client = discord.Client(intents=discord.Intents.default())
    failure: Exception | None = None

    @client.event
    async def on_ready() -> None:
        nonlocal failure
        try:
            channel = client.get_channel(channel_id) or await client.fetch_channel(
                channel_id
            )
            for part in reports.chunk(message):
                await channel.send(part)
            print(f"\nSent to Discord → #{getattr(channel, 'name', channel_id)}")
        except Exception as exc:  # surfaced after the client shuts down
            failure = exc
        finally:
            await client.close()

    await client.start(secrets.discord_bot_token)
    if failure is not None:
        raise failure


async def check_feeds(config) -> int:
    """Report what each news feed is actually returning right now.

    The morning log can only say a publication was silent, which reads the
    same whether its feed moved, started refusing this user agent, or just had
    a quiet day. This is the answer to "is VentureBeat dead or was yesterday
    slow?", available at the moment the question comes up.
    """
    from .integrations.news_feeds import FEEDS, check

    print(f"Checking {len(FEEDS)} feeds\n")
    broken = []
    for health in sorted(await check(), key=lambda h: h.source):
        newest = (
            health.newest.astimezone(config.tz).strftime("%Y-%m-%d %H:%M")
            if health.newest
            else "-"
        )
        mark = "ok " if health.ok else "!! "
        print(f"{mark}{health.source:<26}{health.detail:<18}{health.entries:>3}  {newest}")
        if not health.ok:
            broken.append(health)

    if broken:
        print(
            f"\n{len(broken)} feed(s) returned nothing usable. Find the current URL "
            "or drop the publication, in integrations/news_feeds.py — a feed left "
            "in the list stays silent instead of failing."
        )
    return 1 if broken else 0


async def _run(name: str, commit: bool, send: bool) -> int:
    config = get_config()
    # Before the secrets: a check needs the network, not a Notion token, and
    # refusing to run one because .env is incomplete helps nobody.
    if name == "feeds":
        return await check_feeds(config)

    secrets = get_secrets()
    secrets.require("notion_token", "notion_agenda_db_id")

    async def printer(message: str, kind: str = "") -> None:  # pragma: no cover
        print(message)

    client = NotionClient(secrets.notion_token)
    agenda = AgendaService(client, config, secrets.notion_agenda_db_id)
    projects = (
        ProjectService(client, config, secrets.notion_projects_db_id)
        if secrets.notion_projects_db_id
        else None
    )
    from .integrations.calendar import make_source

    jobs = Jobs(
        agenda,
        projects,
        config,
        printer,
        anthropic_api_key=secrets.anthropic_api_key,
        calendar_source=make_source(secrets),
    )

    try:
        if name == "brief":
            message = await jobs.build_daily_brief()
        elif name == "incomplete":
            message = await jobs.build_incomplete_alert()
        elif name == "news":
            message = await jobs.build_ai_news()
        elif name == "planning":
            message = await jobs.build_weekly_planning()
        elif name == "stale":
            message = await jobs.build_stale_projects()
        elif name == "review":
            message = await jobs.build_weekly_review()
        elif name == "scaffold":
            message = await jobs.week_scaffold(commit=commit)
            if not commit:
                print("(preview only — pass --commit to actually create rows)\n")
        else:  # pragma: no cover - argparse restricts choices
            raise ValueError(name)
    except NotionError as exc:
        print(f"\nNotion error: {exc}")
        print("Run scripts/inspect_notion.py and fix the mappings in alliegent.toml.")
        return 1
    except RuntimeError as exc:
        print(f"\n{exc}")
        return 1
    finally:
        await client.aclose()

    print(message if message is not None else "(nothing to report)")

    if send:
        if message is None:
            print("\nNothing to report, so nothing was sent.")
            return 0
        try:
            await send_to_discord(secrets, JOB_CHANNEL[name], message)
        except RuntimeError as exc:
            print(f"\n{exc}")
            return 1
        except Exception as exc:
            print(f"\nDiscord send failed: {type(exc).__name__}: {exc}")
            print("Check that the bot can view and post in that channel.")
            return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="alliegent.cli", description=__doc__)
    parser.add_argument("job", choices=(*JOBS, *CHECKS))
    parser.add_argument(
        "--commit",
        action="store_true",
        help="for the scaffold job, actually create the rows in Notion",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="post the result to the Discord channel this job uses",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    logging.getLogger("discord").setLevel(logging.WARNING)
    # discord.py warns about missing voice support on every connect; this bot
    # only posts text, so the warning is pure noise.
    logging.getLogger("discord.client").setLevel(logging.ERROR)
    return asyncio.run(_run(args.job, args.commit, args.send))


if __name__ == "__main__":
    raise SystemExit(main())
