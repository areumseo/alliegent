# alliegent

A personal agent that keeps a Notion Weekly Agenda up to date, digests the day's AI news, and reports to Discord. Ask it in plain language or drive it with slash commands.

The Discord bot and the job scheduler share a single asyncio loop, so the whole thing runs as one process on one small machine.

## What it does

| Job | Default time (Asia/Seoul) | Description |
| --- | --- | --- |
| Daily brief | 08:00 daily | Today's items, anything overdue, and active projects, in one message |
| AI news digest | 09:00 daily | Up to 10 AI stories from the last 24h — English headline and link, three-sentence summary in English and Korean |
| Incomplete alert | 21:00 daily | Today's unfinished items and anything past its date |
| Weekly planning | Sat 10:00 | Prompts you to plan the coming week, showing what's in it, which days are empty, and what's carrying over |
| Week scaffolding | *off* | Copies last week's `Recurring` items onto the coming week. Disabled until something actually repeats |
| Stale project nudge | Wed 10:00 | Projects with no linked agenda activity for N days. Off until a projects database exists |
| Weekly review | Sun 21:00 | Completion stats for the past week as a review draft |

The evening alert stays silent when there is nothing pending. A daily "all clear" ping trains you to ignore the channel, so only the morning brief is unconditional — and it distinguishes a day you finished from a day with nothing on it, rather than reporting both as empty.

### Slash commands

| Command | Description |
| --- | --- |
| `/today` | Show today's agenda |
| `/tomorrow` | Show tomorrow's agenda (unnumbered — `/done` applies to today) |
| `/status` | Today's and this week's completion, plus what's left today |
| `/add <task> [when]` | Add an item; its Category is inferred from history. `when` accepts `오늘` / `내일` / `모레`, `today` / `tomorrow` / `tmr` (any capitalisation), `2026-08-15`, `08-15`, or `08/15`; defaults to today |
| `/done <numbers>` | Complete items by their number in `/today` — one or several (`3` or `3,5`) |
| `/delete <numbers>` | Move items to Notion's trash by number — recoverable there |
| `/overdue` | Overdue, unfinished items |
| `/projects` | Active projects and their next actions |
| `/brief` | Run the daily brief now |
| `/news` | Fetch the AI news digest now — acknowledges immediately and posts to the news channel when ready (2–3 min) |

Everything the bot shows in Discord is English, so nothing needs an input-method switch. Korean date words are still accepted as `when` values.

Every message that lists today's unfinished work numbers it the way `/today` does — counting completed rows too, so the numbers are gaps rather than 1,2,3. That is deliberate: `/done` and `/delete` resolve a number against the full day, so renumbering the unfinished subset would make "2" mean a different row depending on which message you read it in.

`/done` and `/delete` take several numbers at once (`3,5` or `3 5`) and resolve them against a single snapshot of the day. Running them one at a time would not be equivalent: completing or trashing item 3 shortens the list, so the item that was 5 becomes 4 and the next command would hit the wrong row.

**Commands post to the same channel their scheduled equivalent uses**, wherever you invoke them from — agenda commands to the agenda channel, `/projects` to the projects channel, `/news` to the news channel. Run one from somewhere else and you get a one-line "Posted to #channel" instead, so the archive never splits across whichever channel you happened to be in. Run it from the destination channel and it just answers in place.

`/add`, `/done`, and `/delete` are the exception: they answer where you typed them, since routing a one-line confirmation would turn every write into two messages.

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
4. **Bot** → **Privileged Gateway Intents** → enable **MESSAGE CONTENT INTENT**, if you want the bot to answer when mentioned (see [Talking to it](#talking-to-it))
5. Enable **Developer Mode** (Settings → Advanced), then right-click your server icon → **Copy Server ID**, and each channel → **Copy Channel ID**

Inviting the bot to a server does not give it access to private channels — add it to each one individually (**Edit Channel** → **Permissions**), or its messages fail with `50001`.

Each job posts to the channel matching its kind:

| Variable | Receives |
| --- | --- |
| `DISCORD_AGENDA_CHANNEL_ID` | Daily brief, incomplete alert, weekly planning, week scaffolding |
| `DISCORD_PROJECTS_CHANNEL_ID` | Stale project nudges |
| `DISCORD_REVIEW_CHANNEL_ID` | Weekly review (falls back to the agenda channel) |
| `DISCORD_NEWS_CHANNEL_ID` | Daily AI news digest |
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

It prints every property with its type, plus a ready-to-paste TOML block. Copy that into the `[agenda.props]` and `[projects.props]` tables in `alliegent.toml` — nothing works until those names match the properties your databases actually have. The agenda mappings shipped in the file match the schema documented below; the projects ones are untested, since no projects database exists yet.

Preview any job in the terminal without posting to Discord:

```bash
uv run python -m alliegent.cli brief
```

Jobs: `brief`, `news`, `incomplete`, `planning`, `scaffold`, `stale`, `review`.

Add `--send` to actually post the result to the Discord channel that job uses. This checks the bot token, the channel IDs, and the bot's channel permissions in one go, rather than waiting until 08:00 to discover one of them is wrong:

```bash
uv run python -m alliegent.cli brief --send
```

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

Rather than retyping them, pipe the filled-in `.env`:

```bash
grep -E '^[A-Z_]+=.+' .env | fly secrets import
```

```bash
fly deploy && fly status
```

The machine must not auto-stop. A Discord gateway connection is long-lived and takes no inbound HTTP, so `fly.toml` deliberately has no `[http_service]` — if the machine suspends, the bot goes offline and scheduled jobs never fire. `fly launch` re-adds that block every time it runs; remove it again.

**That is also why `fly status` belongs in the deploy command.** With no HTTP service, Fly has no reason to start a stopped machine, so deploying onto one succeeds with no error and leaves the bot down. If the state is `stopped`, start it:

```bash
fly machine start <machine-id>
```

## Talking to it

Mention the bot and it answers, using the same agenda underneath:

```
@alliegent 내일 뭐 있지?
@alliegent 장보기 내일 추가해줘
@alliegent Diary 끝냈어
```

It can read any day, list what's overdue, add items, mark them done, move them to Notion's trash, and create calendar events. Deleting is a trash move, not a hard delete — the row is recoverable in Notion, which is what makes it safe to expose. Changing an item's date is not exposed: a silently moved item is hard to notice and hard to undo, unlike a trashed one you can see in the trash.

Tasks and events go to different places, and it decides which: something happening at a set time ("dentist at 3pm tomorrow") becomes a calendar event, while something to get done ("book a dentist appointment") becomes an agenda item. When a request could be either, it asks.

It answers in English whichever language you ask in, matching the rest of the Discord surface — but item titles are quoted back exactly as they appear in Notion, since a translated title no longer names the row it refers to.

Only mentions trigger it. Replying to everything would talk over conversations and bill for the privilege. Context is kept per channel for about six exchanges, so follow-ups ("then move that one to Wednesday") resolve without repeating yourself.

Runs on Sonnet 5 at low effort — chat is latency-sensitive and each turn is small. Roughly $1–2/month at twenty messages a day.

**This needs the Message Content intent**, which is privileged: discord.com/developers → your app → **Bot** → **Privileged Gateway Intents** → enable **MESSAGE CONTENT INTENT**. Without it, messages arrive with empty content and the bot looks like it's ignoring you. If the app requests the intent without it being enabled, Discord refuses the connection outright — so when that happens the bot logs the fix, drops the chat feature, and starts anyway rather than taking the scheduled jobs down with it.

Chat is skipped entirely when `ANTHROPIC_API_KEY` is unset, and the intent isn't requested at all.

## The calendar

Read over CalDAV with an app-specific password, not an ICS subscription link. An ICS link is unauthenticated — anyone holding it can read that calendar, and an iCloud one is revocable only by unpublishing the calendar — whereas an app password publishes nothing and can be revoked on its own from the Apple ID account page. ICS feeds still work as a fallback (`CALENDAR_ICS_URLS`) and log a warning when used.

Today's events appear at the top of the daily brief, above the to-do list: that part of the day is already committed, and the tasks have to fit around it. All calendars are read and merged into one time-ordered list, with all-day events first; the same event in two calendars prints once. Reminder lists are skipped — they hold no events.

Recurring events are expanded client-side by the same code for both sources, rather than trusting whatever a server chose to expand, so "every Tue and Thu at 19:10" and the weeks that were cancelled behave identically either way.

Writing is opt-in and narrow. `ICLOUD_WRITE_CALENDAR` names the one calendar new events go into and has no default — writing into whichever calendar came back first is not a guess worth making on a real calendar. The bot can create events but not edit or delete them: a created event is easy to spot and remove, while a misread request that alters an existing one is not.

## The AI news digest

Claude searches the web itself (the server-side `web_search` tool) rather than reading a curated RSS list — there is no feed set to maintain, no dedup or ranking code, and no feed that can die quietly. One API call covers finding, selecting, summarising, and translating.

Each story gets its English headline, the link, and a three-sentence summary in English and again in Korean:

```
**1. OpenAI says it slowed Astra model development over security concerns**
https://techcrunch.com/2026/08/07/...
🇺🇸 OpenAI said it deliberately slowed development and release of its next
major model after internal evaluations flagged it as potentially reaching
"critical" capability in cybersecurity. …

🇰🇷 OpenAI가 차기 주력 모델의 개발과 공개를 의도적으로 늦췄다고 밝혔습니다. …
```

The model is asked for `EN:` / `KO:` labels — they make the field boundaries unambiguous for the cleanup below — and they are swapped for flags before sending, with the Korean summary on its own paragraph so the two don't read as one block.

The model returns the finished message rather than JSON. Structured outputs are incompatible with the citations the search tool produces, and a parse failure at 09:00 would mean no digest at all, so the format lives in the prompt (`src/alliegent/integrations/claude.py`). To change how the digest reads — length, tone, what counts as significant — edit the prompt.

What the prompt asks for is not left to trust. `_clean()` drops anything before the first item and after the last summary, so a stray "I'll search for the latest AI news." can't survive at the top of every morning's digest, and folds back the line breaks that citations insert mid-sentence.

This is the only feature that needs `ANTHROPIC_API_KEY`. Leave it unset and the job is skipped with a warning; everything else still runs. Cost is roughly $8/month on Opus 5 at one digest a day, plus web search usage — the input side dominates, since the search results are far larger than the summary.

When the digest can't be produced — no key, rate limit, refusal — nothing is posted. A missing digest is a non-event; a stack trace in the news channel is not.

## The agenda database

The Weekly Agenda is a Notion database, one row per item:

| Property | Type | Purpose |
| --- | --- | --- |
| `Name` | title | The item |
| `Date` | date | Which day it belongs to |
| `Status` | status | `Not started` / `In progress` / `Done` |
| `Recurring` | checkbox | Repeats weekly — the scaffolding job uses these as its template |
| `Category` | select | Lesson / Work / Exercise / Study / Contact / Personal / Admin — filled in automatically on `/add`, see below |
| `Note` | text | Optional |

A database rather than a hand-built page means past weeks accumulate instead of being overwritten, which is what makes the weekly review possible at all. It also means there is no week to "set up" — a date view draws the days on its own.

`Recurring` is unused for now, since nothing in the agenda repeats on a fixed weekly slot. If that changes, tick it on the rows that come back every week and give `week_scaffold_time` a value in `alliegent.toml`; the job then recreates them on the same weekday without retyping. Scaffolded rows stay ticked, so the following week works from them in turn, and re-running never duplicates because it matches on title and date.

`Category` is inferred when you add an item, from how the same activity was filed before. Titles carry their time — "Karrot 6PM", "Karrot 11AM" — so the clock is stripped before comparing and both count as the same activity; a "— Prep" suffix still separates preparation from the lesson itself. The most common past category wins, and an unrecognised activity gets none: an empty Category is obvious, while a confidently wrong one stays invisible until it skews a filter.

This is a history lookup, not a model call. Which categories exist and what belongs in them are facts about these entries, not something to reason about — and it costs one query rather than an API round trip on every add. The lookback is `category_lookback_days` in `alliegent.toml`.

Any job can be switched off the same way: blank its time in `alliegent.toml`.

## Configuration

- `.env` — secrets only. Never committed; this repo is public.
- `alliegent.toml` — schedule times, Notion property name mappings, staleness threshold. Safe to commit.

## Notes on the Notion API

Pinned to version `2026-03-11`. Two things in recent versions break older examples:

- Since `2025-09-03` a database no longer holds a schema. It contains one or more **data sources**, and queries, schema reads, and page creation all target a `data_source_id` — not the database ID from the URL. The client resolves this for you.
- Since `2026-03-11` the trash flag is `in_trash`, not `archived`.

Status is read from the schema rather than assumed, so a checkbox, a select, or a status property all work for marking things done.

## Troubleshooting

**`ModuleNotFoundError: No module named 'alliegent'`** — the editable install intermittently stops being applied to `sys.path`, even though `uv pip list` shows the package installed. Prefix the command instead of reinstalling every time:

```bash
PYTHONPATH=src uv run python -m alliegent.cli brief
```

`uv sync --reinstall-package alliegent` also fixes it, but not durably. Production is unaffected: the Docker image installs a real wheel and `fly.toml` sets `PYTHONPATH` anyway.

**`403 Forbidden (error code: 50001): Missing Access` when sending** — the bot is in the server but cannot see that channel. Inviting the bot to a server does not grant access to private channels. Right-click the channel → **Edit Channel** → **Permissions** → **Add members or roles** → pick the bot → allow **View Channel** and **Send Messages**. The channel ID is not the problem.

**Notion returns 404 for a database that exists** — the token has not been given access to it. Open the database → `···` → **Connections** → add your integration. A valid token alone grants nothing.

**Deploy succeeded but the bot is offline** — check `fly status`. With no `[http_service]`, `fly deploy` will not start a machine that was stopped when the deploy began; it reports success and leaves the new version stopped. `fly machine start <id>`.

**The bot ignores mentions** — either `ANTHROPIC_API_KEY` is unset, or the Message Content intent is off in the developer portal. `fly logs` says which: a missing intent is logged explicitly at startup, and the bot keeps running without the chat feature rather than failing to connect.

## Development

```bash
uv run pytest
```

```bash
uv run ruff check .
```
