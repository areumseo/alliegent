# alliegent

A personal agent that keeps a Notion Weekly Agenda up to date, tracks personal projects, and reports to Discord.

The Discord bot and the job scheduler share a single asyncio loop, so the whole thing runs as one process on one small machine.

## What it does

| Job | Default time (Asia/Seoul) | Description |
| --- | --- | --- |
| Daily brief | 08:00 daily | Today's items, anything overdue, and active projects, in one message |
| Incomplete alert | 21:00 daily | Today's unfinished items and anything past its date |
| Weekly planning | Sat 10:00 | Prompts you to plan the coming week, showing what's in it, which days are empty, and what's carrying over |
| Week scaffolding | *off* | Copies last week's `Recurring` items onto the coming week. Disabled until something actually repeats |
| Stale project nudge | Wed 10:00 | Projects with no linked agenda activity for N days |
| Weekly review | Sun 21:00 | Completion stats for the past week as a review draft |

The evening alert stays silent when there is nothing pending. A daily "all clear" ping trains you to ignore the channel, so only the morning brief is unconditional.

### Slash commands

| Command | Description |
| --- | --- |
| `/오늘` | Show today's agenda |
| `/추가 <할일> [날짜]` | Add an item; date accepts `오늘`, `내일`, `모레`, `2026-08-15`, or `08-15` |
| `/완료 <번호>` | Complete an item by its number in `/오늘` |
| `/밀린것` | Overdue, unfinished items |
| `/프로젝트` | Active projects and their next actions |
| `/브리핑` | Run the daily brief now |

Commands work in any channel the bot can see, regardless of the notification routing below.

## Setup

### 1. Notion token

1. Go to <https://www.notion.so/developers/tokens> → **New token** → name it `alliegent` → select the **Notion API** capability → **Create token**
   - The older `notion.so/my-integrations` URL now 404s. If the developer portal is unavailable to you, the equivalent lives in Settings → **Connections** → *Develop or manage integrations*, which only workspace owners can see. On Business and Enterprise plans an owner must first enable token creation there.
2. Copy the token (starts with `ntn_`)
3. Grant it access to your Weekly Agenda and Projects databases — either by selecting them while creating the token, or afterwards from each database's `···` → **Connections**
   - Without this the API returns 404. A valid token does not grant access to databases it hasn't been given.
4. Copy each database's 32-character hex ID from its URL: `notion.so/myworkspace/<DATABASE_ID>?v=...`

### 2. Discord app

1. Go to <https://discord.com/developers/applications> → **New Application**
2. **Bot** → **Reset Token** → copy the token
3. **Installation** → add the `bot` and `applications.commands` scopes → open the generated URL to invite it to your server
4. Enable **Developer Mode** (Settings → Advanced), then right-click your server icon → **Copy Server ID**, and each channel → **Copy Channel ID**

Each job posts to the channel matching its kind:

| Variable | Receives |
| --- | --- |
| `DISCORD_AGENDA_CHANNEL_ID` | Daily brief, incomplete alert, week scaffolding |
| `DISCORD_PROJECTS_CHANNEL_ID` | Stale project nudges |
| `DISCORD_REVIEW_CHANNEL_ID` | Weekly review (falls back to the agenda channel) |
| `DISCORD_CHANNEL_ID` | Fallback for anything left blank |

### 3. Run locally

```bash
uv sync
```

```bash
cp .env.example .env
```

Fill in `.env`, then dump your real Notion schema:

```bash
uv run python scripts/inspect_notion.py
```

It prints every property with its type, plus a ready-to-paste TOML block. Copy that into the `[agenda.props]` and `[projects.props]` tables in `alliegent.toml`. **The mappings shipped in that file are guesses** — nothing works until they match your actual property names.

Preview any job in the terminal without posting to Discord:

```bash
uv run python -m alliegent.cli brief
```

Jobs: `brief`, `incomplete`, `planning`, `scaffold`, `stale`, `review`.

`scaffold` is the only job that writes to Notion, so it previews by default and needs `--commit` to actually create rows:

```bash
uv run python -m alliegent.cli scaffold --commit
```

Start the bot:

```bash
uv run alliegent
```

### 4. Deploy to Fly.io

```bash
fly launch --no-deploy --copy-config
```

Secrets go to Fly, never into the repo:

```bash
fly secrets set NOTION_TOKEN=... NOTION_AGENDA_DB_ID=... NOTION_PROJECTS_DB_ID=... DISCORD_BOT_TOKEN=... DISCORD_GUILD_ID=... DISCORD_AGENDA_CHANNEL_ID=... DISCORD_PROJECTS_CHANNEL_ID=...
```

```bash
fly deploy
```

The machine must not auto-stop. A Discord gateway connection is long-lived and takes no inbound HTTP, so `fly.toml` deliberately has no `[http_service]` — if the machine suspends, the bot goes offline and scheduled jobs never fire.

## The agenda database

The Weekly Agenda is a Notion database, one row per item:

| Property | Type | Purpose |
| --- | --- | --- |
| `Name` | title | The item |
| `Date` | date | Which day it belongs to |
| `Status` | status | `Not started` / `In progress` / `Done` |
| `Recurring` | checkbox | Repeats weekly — the scaffolding job uses these as its template |
| `Category` | select | Lesson / Work / Exercise / Study / Contact / Personal / Admin |
| `Note` | text | Optional |

A database rather than a hand-built page means past weeks accumulate instead of being overwritten, which is what makes the weekly review possible at all. It also means there is no week to "set up" — a date view draws the days on its own.

`Recurring` is unused for now, since nothing in the agenda repeats on a fixed weekly slot. If that changes, tick it on the rows that come back every week and give `week_scaffold_time` a value in `alliegent.toml`; the job then recreates them on the same weekday without retyping. Scaffolded rows stay ticked, so the following week works from them in turn, and re-running never duplicates because it matches on title and date.

Any job can be switched off the same way: blank its time in `alliegent.toml`.

## Configuration

- `.env` — secrets only. Never committed; this repo is public.
- `alliegent.toml` — schedule times, Notion property name mappings, staleness threshold. Safe to commit.

## Notes on the Notion API

Pinned to version `2026-03-11`. Two things in recent versions break older examples:

- Since `2025-09-03` a database no longer holds a schema. It contains one or more **data sources**, and queries, schema reads, and page creation all target a `data_source_id` — not the database ID from the URL. The client resolves this for you.
- Since `2026-03-11` the trash flag is `in_trash`, not `archived`.

Status is read from the schema rather than assumed, so a checkbox, a select, or a status property all work for marking things done.

## Development

```bash
uv run pytest
```

```bash
uv run ruff check .
```
