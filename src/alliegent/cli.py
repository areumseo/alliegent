"""Run a single job from the terminal, without waiting for its scheduled time.

Prints to stdout by default, so the Notion property mapping in alliegent.toml
can be checked before letting the scheduler loose:

    uv run python -m alliegent.cli brief
    uv run python -m alliegent.cli scaffold          # preview only
    uv run python -m alliegent.cli scaffold --commit # actually create rows

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

JOBS = ("brief", "incomplete", "planning", "scaffold", "stale", "review")

# Which channel each job posts to, mirroring jobs.py.
JOB_CHANNEL = {
    "brief": "agenda",
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
            print(f"\n디스코드 전송 완료 → #{getattr(channel, 'name', channel_id)}")
        except Exception as exc:  # surfaced after the client shuts down
            failure = exc
        finally:
            await client.close()

    await client.start(secrets.discord_bot_token)
    if failure is not None:
        raise failure


async def _run(name: str, commit: bool, send: bool) -> int:
    config = get_config()
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
    jobs = Jobs(agenda, projects, config, printer)

    try:
        if name == "brief":
            message = await jobs.build_daily_brief()
        elif name == "incomplete":
            message = await jobs.build_incomplete_alert()
        elif name == "planning":
            message = await jobs.build_weekly_planning()
        elif name == "stale":
            message = await jobs.build_stale_projects()
        elif name == "review":
            message = await jobs.build_weekly_review()
        elif name == "scaffold":
            message = await jobs.week_scaffold(commit=commit)
            if not commit:
                print("(미리보기 — 실제로 생성하려면 --commit)\n")
        else:  # pragma: no cover - argparse restricts choices
            raise ValueError(name)
    except NotionError as exc:
        print(f"\nNotion 오류: {exc}")
        print("scripts/inspect_notion.py 로 스키마를 확인하고 alliegent.toml을 맞춰주세요.")
        return 1
    except RuntimeError as exc:
        print(f"\n{exc}")
        return 1
    finally:
        await client.aclose()

    print(message if message is not None else "(알릴 내용 없음)")

    if send:
        if message is None:
            print("\n알릴 내용이 없어 전송하지 않았습니다.")
            return 0
        try:
            await send_to_discord(secrets, JOB_CHANNEL[name], message)
        except RuntimeError as exc:
            print(f"\n{exc}")
            return 1
        except Exception as exc:
            print(f"\n디스코드 전송 실패: {type(exc).__name__}: {exc}")
            print("봇이 해당 채널에 접근·쓰기 권한이 있는지 확인해주세요.")
            return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="alliegent.cli", description=__doc__)
    parser.add_argument("job", choices=JOBS)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="scaffold 잡에서 실제로 노션에 행을 생성합니다",
    )
    parser.add_argument(
        "--send",
        action="store_true",
        help="결과를 해당 잡의 디스코드 채널로 실제 전송합니다",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    logging.getLogger("discord").setLevel(logging.WARNING)
    return asyncio.run(_run(args.job, args.commit, args.send))


if __name__ == "__main__":
    raise SystemExit(main())
