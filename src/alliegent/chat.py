"""Conversational access to the agenda, for when the bot is mentioned.

The slash commands cover the same ground, but only if you remember which one
you want and how its arguments go. This is the same capability reached by
asking — "내일 뭐 있지?", "장보기 내일 추가해줘" — with Claude choosing the
tool.

A manual tool loop rather than the SDK's tool runner: the tools are async and
closed over one bot's AgendaService, which the runner's decorator-based
registration doesn't fit, and this path runs on every mention.
"""

from __future__ import annotations

import logging
from datetime import date

from .agenda import AgendaService
from .config import Config

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-5"
MAX_TOKENS = 2000
MAX_TOOL_ROUNDS = 6
# Roughly six exchanges. Enough for "내일 뭐 있지?" → "두 번째 거 지워줘" to
# resolve, without growing the prompt (and the bill) without bound.
HISTORY_LIMIT = 12

SYSTEM = """\
You are alliegent, a personal assistant in a Discord server. You manage one
person's Notion agenda and answer questions about it.

Today is {today} ({weekday}), timezone Asia/Seoul.

Always reply in English, even when the person writes to you in Korean — they
read English comfortably and have asked for it. Keep any Korean they used
verbatim when you quote an item's title back to them: the titles live in
Notion in whatever language they were written, and translating one would stop
it matching the row it names.

Keep replies short — this is chat, not a report. One or two sentences for a
confirmation. When listing agenda items, list them plainly, one per line, with
no preamble.

Use the tools rather than guessing. If you are asked what is on a day, look it
up; never answer from memory or assumption.

Before adding, completing, or deleting anything, be sure you have the right
item and the right date — ask if it is ambiguous. After a write, say plainly
what you did. If a request would change several items at once, confirm first.

Deleting moves the item to Notion's trash, where it can be restored; say that
when you delete something. Never delete when the person asked to complete
something, or the reverse.

You cannot change an item's date; say so if asked, and suggest doing it in
Notion.\
"""

TOOLS = [
    {
        "name": "list_agenda",
        "description": (
            "List the agenda items on one date, with their status. Use this for "
            "any question about what is scheduled on a given day."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "day": {
                    "type": "string",
                    "description": "The date as YYYY-MM-DD.",
                }
            },
            "required": ["day"],
        },
    },
    {
        "name": "list_overdue",
        "description": (
            "List unfinished items dated before today. Use this when asked what "
            "is late, pending, or piling up."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "add_item",
        "description": "Add a new item to the agenda on a given date.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "What to add."},
                "day": {"type": "string", "description": "The date as YYYY-MM-DD."},
            },
            "required": ["title", "day"],
        },
    },
    {
        "name": "delete_item",
        "description": (
            "Move one item to Notion's trash. Recoverable there, not a permanent "
            "delete. Call list_agenda first and pass the title exactly as it "
            "appears."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "The item's exact title."},
                "day": {"type": "string", "description": "The date as YYYY-MM-DD."},
            },
            "required": ["title", "day"],
        },
    },
    {
        "name": "complete_item",
        "description": (
            "Mark one item done. Call list_agenda first and pass the title "
            "exactly as it appears there."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "The item's exact title."},
                "day": {"type": "string", "description": "The date as YYYY-MM-DD."},
            },
            "required": ["title", "day"],
        },
    },
]


class ChatAgent:
    def __init__(self, api_key: str, agenda: AgendaService, config: Config) -> None:
        self._api_key = api_key
        self._agenda = agenda
        self._config = config
        self._history: dict[int, list[dict]] = {}

    def _today(self) -> date:
        from datetime import datetime

        return datetime.now(self._config.tz).date()

    async def _run_tool(self, name: str, args: dict) -> str:
        """Execute one tool. Errors come back as text so Claude can recover."""
        try:
            if name == "list_agenda":
                day = date.fromisoformat(args["day"])
                items = await self._agenda.items_on(day)
                if not items:
                    return f"No items on {day.isoformat()}."
                return "\n".join(
                    f"- {i.title} [{'done' if i.done else 'not done'}]" for i in items
                )

            if name == "list_overdue":
                items = await self._agenda.overdue(self._today())
                if not items:
                    return "Nothing overdue."
                return "\n".join(
                    f"- {i.title} ({i.day.isoformat() if i.day else 'no date'})"
                    for i in items
                )

            if name == "add_item":
                day = date.fromisoformat(args["day"])
                item = await self._agenda.add_item(args["title"], day)
                return f"Added '{item.title}' on {day.isoformat()}."

            if name in ("complete_item", "delete_item"):
                day = date.fromisoformat(args["day"])
                items = await self._agenda.items_on(day)
                match = next(
                    (i for i in items if i.title.strip() == args["title"].strip()), None
                )
                if match is None:
                    titles = ", ".join(f"'{i.title}'" for i in items) or "none"
                    return (
                        f"No item titled '{args['title']}' on {day.isoformat()}. "
                        f"Items that day: {titles}."
                    )
                if name == "delete_item":
                    await self._agenda.trash(match.id)
                    return f"Moved '{match.title}' to the trash in Notion."
                if match.done:
                    return f"'{match.title}' was already done."
                await self._agenda.set_done(match.id)
                return f"Marked '{match.title}' done."
        except Exception as exc:
            log.exception("chat tool %s failed", name)
            return f"That failed: {type(exc).__name__}: {exc}"

        return f"Unknown tool: {name}"

    async def respond(self, channel_id: int, text: str) -> str:
        from anthropic import AsyncAnthropic

        today = self._today()
        history = self._history.setdefault(channel_id, [])
        history.append({"role": "user", "content": text})

        client = AsyncAnthropic(api_key=self._api_key)
        try:
            for _ in range(MAX_TOOL_ROUNDS):
                response = await client.messages.create(
                    model=MODEL,
                    max_tokens=MAX_TOKENS,
                    system=SYSTEM.format(
                        today=today.isoformat(), weekday=today.strftime("%A")
                    ),
                    # Chat is latency-sensitive and the tasks are small.
                    output_config={"effort": "low"},
                    tools=TOOLS,
                    messages=history,
                )

                if response.stop_reason == "refusal":
                    history.pop()
                    return "⚠️ I can't help with that one."

                history.append({"role": "assistant", "content": response.content})

                if response.stop_reason != "tool_use":
                    break

                results = []
                for block in response.content:
                    if block.type == "tool_use":
                        output = await self._run_tool(block.name, dict(block.input))
                        results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": output,
                            }
                        )
                history.append({"role": "user", "content": results})
            else:
                log.warning("chat hit the tool-round limit in channel %s", channel_id)
        finally:
            await client.close()
            # Trim from the front, but never leave a tool_result as the first
            # message — the API rejects a conversation that opens with one.
            del history[: max(0, len(history) - HISTORY_LIMIT)]
            while history and not isinstance(history[0].get("content"), str):
                history.pop(0)

        reply = "\n".join(
            block.text
            for block in response.content
            if getattr(block, "type", None) == "text"
        ).strip()
        return reply or "…"

    def forget(self, channel_id: int) -> None:
        self._history.pop(channel_id, None)


def strip_mentions(text: str, bot_id: int) -> str:
    """Remove the bot's own mention so the model sees only the request."""
    for form in (f"<@{bot_id}>", f"<@!{bot_id}>"):
        text = text.replace(form, " ")
    return " ".join(text.split())
