#!/usr/bin/env python3
"""Dump the real schema of your Notion databases.

The property mappings in alliegent.toml are guesses until this has been run.
Run it once after creating the integration and sharing the databases with it:

    uv run python scripts/inspect_notion.py

It prints each database's properties with their types and, for select/status
properties, their available option names — then a ready-to-paste TOML block.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from alliegent.config import get_secrets  # noqa: E402
from alliegent.integrations.notion import NotionClient, NotionError  # noqa: E402

# Internal field -> the property types that can plausibly serve it, best first.
GUESS_ORDER = {
    "title": ["title"],
    "date": ["date"],
    "status": ["status", "select", "checkbox"],
    "recurring": ["checkbox"],
    "category": ["select"],
    "project": ["relation"],
    "next_action": ["rich_text"],
    "last_activity": ["date", "last_edited_time"],
}


def describe(name: str, definition: dict) -> str:
    kind = definition.get("type", "?")
    line = f"  {name!r:<28} {kind}"
    options = (definition.get(kind) or {}).get("options") if kind in ("select", "status") else None
    if options:
        line += "  options: " + ", ".join(repr(o["name"]) for o in options)
    return line


def suggest(section: str, schema: dict, fields: list[str]) -> str:
    """Pick the most likely property name for each internal field."""
    lines = [f"[{section}.props]"]
    for field in fields:
        match = ""
        for wanted_type in GUESS_ORDER.get(field, []):
            candidates = [n for n, d in schema.items() if d.get("type") == wanted_type]
            if candidates:
                # Prefer a name that looks like the field, else take the first.
                match = next(
                    (c for c in candidates if field.split("_")[0] in c.casefold()),
                    candidates[0],
                )
                break
        lines.append(f'{field} = "{match}"')
    return "\n".join(lines)


async def inspect(client: NotionClient, label: str, db_id: str, section: str, fields: list[str]):
    print(f"\n=== {label} ({db_id}) ===")
    try:
        ds_id = await client.resolve_data_source(db_id)
    except NotionError as exc:
        print(f"  ERROR: {exc}")
        print("  -> Check the ID, and that the database is shared with your integration")
        print("     (Notion page ... menu -> Connections -> add your integration).")
        return
    print(f"  data_source_id: {ds_id}")
    schema = await client.get_schema(ds_id)
    for name, definition in sorted(schema.items()):
        print(describe(name, definition))
    print("\n  Suggested alliegent.toml block:\n")
    print("  " + suggest(section, schema, fields).replace("\n", "\n  "))


async def main() -> int:
    secrets = get_secrets()
    try:
        secrets.require("notion_token")
    except RuntimeError as exc:
        print(exc)
        return 1

    targets = [
        ("Weekly Agenda", secrets.notion_agenda_db_id, "agenda",
         ["title", "date", "status", "recurring", "category", "project"]),
        ("Projects", secrets.notion_projects_db_id, "projects",
         ["title", "status", "next_action", "last_activity"]),
    ]

    async with NotionClient(secrets.notion_token) as client:
        for label, db_id, section, fields in targets:
            if not db_id:
                print(f"\n=== {label} === (skipped: no ID set in .env)")
                continue
            await inspect(client, label, db_id, section, fields)

    print("\nCopy the suggested blocks into alliegent.toml, fixing anything wrong.")
    print("Set an optional field to \"\" to disable that feature.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
