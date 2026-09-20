# alliegent

A personal agent that keeps a Notion Weekly Agenda up to date, digests the day's AI news, and reports to Discord. Ask it in plain language or drive it with slash commands.

The Discord bot and the job scheduler share a single asyncio loop, so the whole thing runs as one process on one small machine.

## What it does

| Job | Default time (Asia/Seoul) | Description |
| --- | --- | --- |
| Daily brief | 08:00 daily | Today's items, anything overdue, and active projects, in one message |
| AI news digest | 09:00 daily | Five of yesterday's AI stories, read from publication feeds — a topics line, then linked headlines with a short summary in English and Korean |
| Incomplete alert | 14:00 and 19:30 daily | Today's unfinished items and anything past its date. Two runs: one while the day can still change, one to close it out |
| Weekly planning | Sat 10:00 | Prompts you to plan the coming week, showing what's in it, which days are empty, and what's carrying over |
| Week scaffolding | *off* | Copies last week's `Recurring` items onto the coming week. Disabled until something actually repeats |
| Stale project nudge | Wed 10:00 | Projects with no linked agenda activity for N days. Off until a projects database exists |
| English quiz | 20:30 daily | Five questions on today's lesson, answered by replying. Silent on days without a lesson |
| Bonus rollover | 1 Apr and 1 Oct, 09:00 | Moves the half-yearly bonus from Expected into Savings once it has been paid |
| ESPP rollover | Day after each purchase date, 09:00 | Moves ESPP contributions into Vested once they have become shares |
| Asset prompt | Mon 09:00 | Asks for this week's balances, carrying last week's figures to edit |
| Karrot candidates | Mon 09:00 | What is decided on but not yet listed. Silent when there is nothing waiting |
| Karrot sales | Sat 20:00 | The week's sales, with the month, the year and the running total. Silent when nothing sold and nothing is owed |
| Weekly review | Sun 21:00 | Completion stats for the past week as a review draft |

The evening alert stays silent when there is nothing pending. A daily "all clear" ping trains you to ignore the channel, so only the morning brief is unconditional — and it distinguishes a day you finished from a day with nothing on it, rather than reporting both as empty.

### Slash commands

| Command | Description |
| --- | --- |
| `/today` | Show today's agenda |
| `/tomorrow` | Show tomorrow's agenda, numbered |
| `/status [when]` | Completion for a day and the week containing it, plus what's left on it. `when` defaults to today |
| `/add <task> [when] [at] [cal]` | Add an item, optionally at a time. Its Category is inferred from history. `when` accepts `오늘` / `내일` / `모레`, `today` / `tomorrow` / `tmr` (any capitalisation), `2026-08-15`, `08-15`, or `08/15`; defaults to today. An item with a time also lands in the calendar — `cal` forces that on or off |
| `/done <numbers> [when]` | Complete items by their listed number — one or several (`3` or `3,5`). `when` picks the day, or `overdue` for the backlog; defaults to today |
| `/delete <numbers> [when]` | Move items to Notion's trash by number — recoverable there. Takes `when` the same way, `overdue` included |
| `/move <numbers> <to> [from]` | Move items to another day. `from` defaults to today |
| `/time <numbers> <at> [when]` | Set an item's time — `14:00`, `2pm`, `9:30am` — which is what moves it in the day. `none` clears it and sends it to the end |
| `/overdue` | Overdue, unfinished items — numbered, so they can be cleared |
| `/projects` | Active projects and their next actions |
| `/brief` | Run the daily brief now |
| `/assets show` · `/assets trend` | Latest snapshot with what changed, and the recent history |
| `/karrot …` | Second-hand listings: `list`, `add`, `sold`, `sent`, `paid`, `sales`, `summary` |
| `/news` | Build the AI news digest now — acknowledges immediately and posts to the news channel when ready (under a minute) |

Everything the bot writes in Discord is English, so nothing needs an input-method switch — except English quiz situations, for the reason given below. Data keeps the language it arrived in — Karrot item names are Korean because they are quoted from a Korean marketplace. Korean date words are still accepted as `when` values.

Every message that lists today's unfinished work numbers it the way `/today` does — counting completed rows too, so the numbers are gaps rather than 1,2,3. That is deliberate: `/done` and `/delete` resolve a number against the full day, so renumbering the unfinished subset would make "2" mean a different row depending on which message you read it in.

`overdue` is a list in its own right: `/done 2 overdue`, `/delete 1,3 overdue`. Type it straight through — Discord only moves to the next option when you press Tab, so the day usually lands in the numbers field, and the commands read it back out from there rather than answering "Not a number". Delayed items span days, so no day argument reaches them, and a backlog that can only be looked at is a backlog that stays.

Every message that shows the backlog numbers it identically — the brief, the evening alert, the weekly plan, and `/overdue` — because those numbers get typed into a command that recounts the list itself. The brief stops at ten and points at `/overdue` for the rest; `/overdue` never truncates, since a number the list doesn't show is a number nothing can resolve.

Numbers are per-day, and `/done` and `/delete` take the day as an argument (`/done 2 tomorrow`), so any day's list can be numbered and acted on. Their confirmations name the date, which is what makes a wrong day obvious immediately. A list for a day other than today repeats the argument you'd need.

`/done` and `/delete` take several numbers at once (`3,5` or `3 5`) and resolve them against a single snapshot of the day. Running them one at a time would not be equivalent: completing or trashing item 3 shortens the list, so the item that was 5 becomes 4 and the next command would hit the wrong row.

**Commands post to the same channel their scheduled equivalent uses**, wherever you invoke them from — agenda commands to the agenda channel, `/projects` to the projects channel, `/news` to the news channel. Run one from somewhere else and you get a one-line "Posted to #channel" instead, so the archive never splits across whichever channel you happened to be in. Run it from the destination channel and it just answers in place.

`/add`, `/done`, `/delete`, `/move`, and `/time` are the exception: they answer where you typed them, since routing a one-line confirmation would turn every write into two messages.

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
| `DISCORD_KARROT_CHANNEL_ID` | Karrot listings report |
| `DISCORD_ASSETS_CHANNEL_ID` | Weekly asset prompt |
| `DISCORD_ENGLISH_CHANNEL_ID` | English lesson material in, quizzes and grading out |
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

### 4. Run it on the Mac mini

The bot runs from a checkout at `~/work/alliegent` on a Mac mini reachable over Tailscale, under launchd. Install the jobs for whoever is logged in:

```bash
./scripts/install_service.sh
```

The plists in `scripts/` are templates: launchd expands neither `~` nor `$HOME`, so a committed plist would have to hardcode one person's home directory — and name them, in a public repo. `install_service.sh` fills `__HOME__` in at install time.

To restart the bot:

```bash
launchctl kickstart -k gui/$(id -u)/com.alliegent.bot
```

Logs are in `~/Library/Logs/alliegent/` — `stderr.log` for the bot, `update.log` for the updater.

**Staying current.** The code is edited on the laptop and runs on the mini, so the two drift apart with every push. `scripts/self_update.sh` runs every 15 minutes: it fetches `main`, runs the test suite, and restarts the bot only if the tests pass. A commit that fails is reset back to the previous one, so a bad push leaves the running version serving rather than taking the bot down, and the next good push recovers on its own without anyone logging in.

It skips entirely when the working tree on the mini is dirty. Editing directly on the server and auto-pulling do conflict, and of the two failures, losing work in progress is the one that can't be undone by rerunning.

### 5. Deploy to Fly.io

Fly is no longer where this runs — the mini replaced it, and the machines were destroyed on 2026-08-19. The app and its secrets still exist, so `fly deploy` would bring it back; these notes are kept for that case.

**Only ever run one instance.** Two copies of the bot both hold a gateway connection and both run the scheduler, so every scheduled message is posted twice and slash commands race: whichever loses the race logs `404 (10062) Unknown interaction` followed by `400 (40060) Interaction has already been acknowledged`. That pair in the log means a second instance is running somewhere, not a bug in the command.

This is exactly how the mini migration went wrong, twice. `fly scale count 0` was run, but machines were present and `started` afterwards, and Fly kept serving alongside the mini until the duplicate posts gave it away. Destroying them didn't hold either: `.github/workflows/fly-deploy.yml` deployed on every push to `main`, so the next commit rebuilt the machines within a minute — including the commit that documented them as destroyed.

That workflow is now `workflow_dispatch` only. With the mini pulling `main` itself, a push is already a deploy, and a second automatic one lands a second bot. `fly status` showing no machines is the check — not that a scale or destroy command was issued at some point.


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

Items added with a time are mirrored into `ICLOUD_WRITE_CALENDAR` as 30-minute events. That default is the distinction between the two tools: something happening at an hour belongs in a calendar, a chore belongs on a list, and a calendar filled with chores stops showing what the day is actually committed to. `cal: true` overrides it for an untimed item (which becomes an all-day entry), `cal: false` keeps a timed one out.

Thirty minutes because an agenda item carries a start and nothing else. A wrong end is easy to drag in the calendar; a missing event is not.

The Notion row is written first, so a calendar failure reports itself on its own line rather than failing the whole command — the task is in the agenda either way.

**A calendar that can't be read says so in the brief.** The block is simply absent otherwise, which is indistinguishable from a day with no events — that hid an expired app password for a week. An authorisation failure is named specifically, because it will not clear up on its own: only a new app-specific password from appleid.apple.com fixes it.


## The AI news digest

At 09:00 the digest covers **yesterday** — at nine in the morning the day's own stories have barely been filed, and the day that just finished is the one worth reading about.

The digest is built to be scanned, not read through. A `🏷️` line of the day's subjects comes first, so the question "is there anything for me today" is answered without scrolling; each headline is itself the link, which removes a line per item; and summaries are a sentence or two rather than three. Link previews are suppressed on send — five articles would otherwise drag in five cards, each taller than the entry above it.

Stories come from the feeds listed in `news_feeds.py` (TechCrunch, The Verge, Ars Technica, VentureBeat, MIT Technology Review). The model picks five and writes them up in English and Korean; it does not go looking for news itself.

**It used to search, and that is worth knowing about before changing it back.** Letting the model search read well, but every search result stayed in its context and was re-read on each following step, so the input grew with roughly the square of the search count. One digest measured anywhere from 89k to 437k input tokens with no way to predict which, and on 2026-08-21 the call ran past its fifteen-minute ceiling and delivered nothing at all. Below about seven searches the model would rather stop than guess, and it started citing "top AI news today" roundups instead of articles.

Reading feeds instead fixes those by construction rather than by tuning:

| | Search | Feeds |
| --- | --- | --- |
| Input tokens | 89k–437k, unpredictable | ~1.2k |
| Time | 5–15+ min, sometimes never | under a minute |
| "Yesterday" | an instruction to honour | a filter on the feed's own date |
| Links | sometimes roundup pages | always the article |

The cost is a list to maintain. A feed that moves or dies goes quiet rather than failing, so any feed contributing nothing is named in a `no articles from:` warning — worth reading if the digest starts looking thin.

Delivery is Discord, plus Gmail if `AI_NEWS_EMAIL_TO` is set.

## English lesson review

Post what a lesson left behind into `#english-review` — the PDF, screenshots, pasted notes, a reference link — and it becomes four linked Notion databases: the lesson, the expressions worth keeping, the corrections, and every quiz question asked. That evening a five-question quiz goes out; reply to it with your answers and each is graded against what the tutor taught. This is the one channel the bot reads without being mentioned, because receiving material is its whole job.

**The PDF is the lesson; a reference link is background.** They are sent to the model labelled apart, and expressions and corrections come only from the lesson material. A public grammar page is the same for everyone, so a quiz built from it would be a grammar exercise, not a review of what you said. A link posted on its own is attached to today's lesson instead of creating a second one — the PDF and its link usually arrive as two messages.

**British Council's pages refuse requests from servers** — HTTP/2 streams reset, HTTP/1.1 never answers, from both the laptop and the server. When the page cannot be read, its address is passed instead: it names the grammar topic, and standard grammar is something the model already knows.

Cost is kept down deliberately. PDF text is extracted on the server with `pypdf` and only the text is sent; sending the file makes the model process every page as an image as well, several times the tokens for the same words. The original goes only when a PDF has no text layer. Quiz questions come from the stored context, so asking costs nothing — only extracting a lesson and grading call the model.

**Quiz situations are the one place the bot writes Korean.** Tried in English they leaked the answer through the words that set the scene — "how would you say you're *willing* to do that" defines *be up for* rather than asking for it. A Korean situation makes you produce the English instead of recognising it. The heading, the instruction and the feedback around it stay English.

Answers are read as a **reply to the quiz message**, which is how they are told apart from new lesson material posted while a quiz is open. Numbered lines land on their numbers, plain lines fill in order, and a skipped number stays empty rather than shifting every later answer onto the wrong question.

Spaced repetition, the pre-lesson review and the monthly mistake report are the second phase; the Reviews database records every result from the start so they will have history to work from.

## Assets

A weekly snapshot, typed in by hand. Korean banks have no personal API worth building on and scraping survives neither the certificates nor the one-time codes, so the division of labour is the other way round from everything else here: a person writes seven numbers into Notion once a week, and the bot does the part a person will not — compare, weigh, and show the trend.

**Locked money is in the total and shown apart from it.** `Pension`, `Deposit` and `Mom` are assets that cannot be drawn on; a single net-worth figure reads as money you could spend. Both numbers appear together every time, which is half the point of the feature. **Expected money is kept out of the total.** `ESPP` still accruing and a `Bonus` not yet paid are real but not held; inside the total, every weekly change would mix money earned with an estimate revised. They get their own section, summed as `EXPECTED`, with `TOTAL+EXP` beneath for the combined figure. Two columns rather than one, because they move differently — ESPP builds up each payday, a bonus is a single estimate — and one column would hide which had changed.

**`Vested` is company stock actually owned** — vested RSUs and ESPP shares once bought. They sit in the same brokerage account as the same shares, so they share a column. ESPP contributions that have not been used to buy anything yet are `Expected`. When an offering period closes the purchase price is set, and the next day the contributions move into `Vested`. The dates are listed in `espp_purchase_dates` rather than recurring, because the plan sets each cycle's dates and they drift. What moves is the amount paid in; the shares are usually worth more, so `Vested` needs updating to their market value once they arrive.

The bonus is paid at the end of September and March, and on the first of the following month it moves into `Savings` on its own. The move is written as that day's row with every other figure carried over, never as an edit to an earlier row: that row recorded what was held on its own date. It is announced in `#assets` with the amounts, since balances are also entered by hand — once the bonus is in `Savings`, adding it again from the bank balance would count it twice.

Monday's prompt carries last week's figures rather than presenting a blank form: editing numbers is faster than recalling them, and a bucket you forgot shows up as one that did not change. Recording twice in the same week corrects that week's row instead of adding a second — otherwise the next comparison would measure a change of zero.

Every amount lives in Notion. Nothing in this repository contains one, and the examples and fixtures are invented.

## Karrot listings

Second-hand sales live in their own Notion database and their own channel, and **this one channel is in Korean**. Every item name and category in that database is Korean already; a listing translated into "Clothing" is one the reader cannot search for. The slash commands keep ASCII names so nothing needs an input-method switch — only what they say is Korean.

The commands describe themselves in English even here — the picker is where you choose a command before any reply exists, and one that switches language mid-list is harder to scan. Only the replies and the morning report are Korean. Statuses follow the order a sale moves through: `Not listed` → `Listed` → `Reserved` → `Sent` → `Sold`.

`Not listed` is a candidate — decided on, not yet put up — and Monday's report is the list of them, because a week is the unit you would act in. `Sent` is posted and waiting on delivery. Both keep a number in `/karrot list`, because both still have something left to do.

**`Listed At` and `Bumped` were removed from the database on 2026-09-13**, and staleness went with them. This is a record of what sold for how much, not of how long a listing sat; nothing else could stand in for those dates, so rather than approximate them the feature is gone. `/karrot bump` went too.

`Status` and `Category` are both English in the database — they are fixed choices, which makes them interface rather than data. Item names stay in the language they were listed in.

`/karrot list` numbers one working set: everything still listed or reserved, plus anything sold but not yet paid for. That is exactly the set with something left to do, which is what makes a single number space workable — a sold and settled item is worth reading but never worth acting on. `/karrot list Sold` is a read-only view and is deliberately unnumbered, so its numbers cannot be confused with the actionable ones.

Three judgements carried over from the script this was ported from, each earned:

- `Sold At` is stamped only on the **first** move into Sold, so changing the status twice does not push the sale date to today.
- Idleness counts from `Bumped` when there is one, else `Listed At` — bumping restarts the clock, which is the point of bumping.
- Items with **no date at all** are reported separately rather than as stale. Nineteen migrated rows have no `Listed At`, and letting them appear as stale every morning is how a daily report becomes one you stop reading. Filling the date in moves them into the normal count automatically.

`/karrot sales` breaks revenue down by week, month and year, and **names the money it could not place**. A sale with no `Sold At` belongs to no period, and 109 of them had none when this was written — reporting "이번 달 ₩0" without saying why would read as a month with no sales rather than a month with no dates. Unpaid sales count as revenue, with the outstanding amount on its own line: the item is gone and the price is settled, so netting it out would hide a sale that happened.

`NOTION_KARROT_DB_ID` switches the whole feature off when blank.

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

`Category` is inferred when you add an item, from how the same activity was filed before. Titles carry their time — "Cafe shift 6PM", "Cafe shift 11AM" — so the clock is stripped before comparing and both count as the same activity; a "— Prep" suffix still separates preparation from the lesson itself. The most common past category wins, and an unrecognised activity gets none: an empty Category is obvious, while a confidently wrong one stays invisible until it skews a filter.

This is a history lookup, not a model call. Which categories exist and what belongs in them are facts about these entries, not something to reason about — and it costs one query rather than an API round trip on every add. The lookback is `category_lookback_days` in `alliegent.toml`.

**A day is ordered by the clock**, and `Date` carries the time when an item has one. Items without a time follow the timed ones, in the order they were added — Notion's `created_time` settles that, so adding a task can never renumber the ones already listed. That matters because the numbers printed in one message get typed into another (`/done 3`), and while ordering was left to Notion those numbers shifted whenever anything was added.

Ordering therefore has to happen here rather than in the query. Notion's date sort compares full timestamps, so an item with a time and one without are never tied and no second sort key is ever consulted — a `Date, Order` sort silently degrades to `Date` alone. An earlier `Order` number property was removed for that reason.

The same timestamp comparison makes date *filters* unreliable: an item at 06:30 on the 24th is 21:30 UTC on the 23rd, so a query for the 23rd returns it, while a 06:00 item on the 23rd is missed. Queries therefore ask for a day either side and narrow the range in `items_between`. Nothing about this is visible until items start carrying times, which is exactly when it starts to matter.

Any job can be switched off the same way: blank its time in `alliegent.toml`.

**Status is read as three states, not two.** Notion groups its status options into To-do / In progress / Complete, and each group means something different to whoever reads the list:

| Group | Shown as | Why |
| --- | --- | --- |
| Complete (`Done`, `Canceled`) | `✅` | Needs no more attention. Cancelling is a decision, not an omission — until this was read properly, cancelled items kept appearing in the brief and the overdue list |
| In progress (`In progress`, `On hold`) | `🔸` | Already begun, so it needs finishing rather than starting. "2 of 7 done" reads the same whether two things are half-finished or nothing has been touched |
| To-do (`Not started`, `Ready`) | *nothing* | An unchecked box on every line of a mostly-unstarted list is noise |

`/status` counts the middle group separately for the same reason: `2 of 7 done (29%), 2 in progress`.

Both groups are read from the schema at runtime, and each is found by which group holds a known member (the configured `done` and `doing` values) rather than by name. So renaming `Cancelled` to `Canceled`, adding a `Dropped` status beside it, or renaming a group itself all work with no change here.


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

**`Could not find database with ID …` on every Notion job.** The token in use has no access to that database — which is not always the token you think you set. `.env` keeps the **last** definition of a key, so a second `NOTION_TOKEN=` pasted at the bottom silently replaces a working one further up, and the file still looks correct. A duplicate key is now logged as an error at startup; check the log before the login line.

**A scheduled job that stops arriving.** Jobs stay silent when there is nothing to say, so a broken one looks the same as a quiet one. Failures are now announced in the channel the message belonged to, once per run. That is how a two-day outage went unnoticed in September: the AI news digest kept arriving because it is the one job that never touches Notion.


## Development

```bash
uv run pytest
```

```bash
uv run ruff check .
```

### Releases

The mini follows `main`, so a release does not deploy anything — it names a
state that worked, so there is somewhere to go back to. Cut one from a clean,
pushed `main`:

```bash
scripts/release.sh 1.1.0
```

It runs the tests, shows the commits since the last tag, bumps the version in
`pyproject.toml`, writes a [CHANGELOG](CHANGELOG.md) entry from those commits
and opens it in `$EDITOR` to edit, then tags, pushes and creates the GitHub
release from the entry as edited.

If a release turns out to be broken, put the server back on the last good one.
The updater has to be stopped as well as the code changed, or it would
fast-forward to `main` again within the hour:

```bash
ssh <server> 'bash ~/work/alliegent/scripts/rollback.sh v1.0.0'
```

Once `main` carries the fix, `--resume` returns the server to it and starts
the updater again.

### Secrets and personal data

This repo is public. Enable the hooks once per clone:

```bash
./scripts/install_hooks.sh
```

`scripts/check_secrets.sh` then runs before every commit and refuses one that contains a credential-shaped string, an email address, a phone number, a Discord/Notion id, an `.env`/key file, or — the strongest check, and only possible locally — any value that is actually in your `.env`. Run it with `all` to check the whole tree instead of what is staged. It never prints a matched value; scrollback and CI logs are places a secret would spread to.

Two things worth being explicit about:

- **A leaked API token is not protected by the account's passkey or MFA.** The token *is* the authentication, so account-level protection does nothing here. That is why the check runs before the commit rather than in CI: once pushed, a secret is public immediately and stays in the history after any later fix.
- **Agenda item names are personal data.** A week of them says where someone is and when. Examples and fixtures use invented ones for that reason.

