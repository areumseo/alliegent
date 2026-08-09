"""Run a single job from the terminal and print the result.

This never posts to Discord — it exists to check that the Notion property
mapping in alliegent.toml is right before letting the scheduler loose:

    uv run python -m alliegent.cli brief
    uv run python -m alliegent.cli scaffold          # preview only
    uv run python -m alliegent.cli scaffold --commit # actually create rows
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


async def _run(name: str, commit: bool) -> int:
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
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="alliegent.cli", description=__doc__)
    parser.add_argument("job", choices=JOBS)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="scaffold 잡에서 실제로 노션에 행을 생성합니다",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.WARNING)
    return asyncio.run(_run(args.job, args.commit))


if __name__ == "__main__":
    raise SystemExit(main())
