# alliegent

노션 Weekly Agenda 관리와 개인 프로젝트 추적을 자동화하고, 결과를 디스코드로 알려주는 개인 비서 에이전트.

봇과 스케줄러가 한 프로세스에서 함께 돌아갑니다. Fly.io에 상시 배포하는 걸 기준으로 만들어졌지만, 로컬에서도 똑같이 실행됩니다.

## 하는 일

| 기능 | 기본 시각 (Asia/Seoul) | 설명 |
| --- | --- | --- |
| 아침 데일리 브리핑 | 매일 08:00 | 오늘 할 일 + 밀린 항목 + 진행 중인 프로젝트를 한 장으로 요약 |
| 미완료 항목 알림 | 매일 21:00 | 오늘 끝내지 못한 항목과 기한 지난 항목을 알림 |
| 주간 항목 생성 | 월 06:00 | 다가오는 날짜에 비어 있는 Agenda 행을 미리 생성 |
| 정체 프로젝트 알림 | 수 10:00 | N일 동안 관련 활동이 없는 프로젝트를 짚어줌 |
| 주간 회고 요약 | 일 21:00 | 한 주 완료/미완료를 집계해 회고 초안 생성 |

디스코드 슬래시 명령어:

- `/오늘` — 오늘 아젠다 보기
- `/추가 <할일> [날짜]` — 아젠다 항목 추가 (날짜 생략 시 오늘)
- `/완료 <번호>` — `/오늘` 목록의 번호로 완료 처리
- `/밀린것` — 기한 지난 미완료 항목
- `/프로젝트` — 진행 중인 프로젝트와 다음 할 일
- `/브리핑` — 데일리 브리핑 즉시 실행

## 설정

### 1. 노션 통합 만들기

1. <https://www.notion.so/my-integrations> → **New integration** → 이름 `alliegent`
2. **Internal Integration Secret** 복사 (`ntn_`으로 시작)
3. Weekly Agenda DB와 프로젝트 DB를 각각 열고, 우측 상단 `···` → **Connections** → `alliegent` 추가
   - 이걸 안 하면 API가 404를 냅니다. 토큰이 있어도 공유되지 않은 DB는 안 보입니다.
4. 각 DB의 URL에서 32자리 hex ID를 복사
   - `notion.so/myworkspace/<여기가_DB_ID>?v=...`

### 2. 디스코드 앱 만들기

1. <https://discord.com/developers/applications> → **New Application**
2. **Bot** → **Reset Token** → 토큰 복사
3. **Installation** → Guild Install에 `bot`, `applications.commands` 스코프 추가 → 생성된 URL로 내 서버에 초대
4. 디스코드 앱 설정에서 **개발자 모드**를 켜고, 알림 받을 채널 우클릭 → **채널 ID 복사**

### 3. 로컬 실행

```bash
uv sync
```

```bash
cp .env.example .env
```

`.env`를 채운 뒤, 실제 노션 스키마를 확인합니다.

```bash
uv run python scripts/inspect_notion.py
```

출력된 TOML 블록을 `alliegent.toml`의 `[agenda.props]` / `[projects.props]`에 반영하세요. 이 매핑이 맞아야 나머지가 동작합니다.

각 잡을 디스코드에 보내지 않고 터미널에서 미리 확인할 수 있습니다. 매핑이 맞는지 여기서 먼저 검증하세요.

```bash
uv run python -m alliegent.cli brief
```

`scaffold`만 노션에 쓰기 때문에, 기본은 미리보기이고 `--commit`을 붙여야 실제로 생성됩니다.

```bash
uv run python -m alliegent.cli scaffold --commit
```

사용 가능한 잡: `brief`, `incomplete`, `scaffold`, `stale`, `review`

봇 실행:

```bash
uv run alliegent
```

### 4. Fly.io 배포

```bash
fly launch --no-deploy --copy-config
```

비밀값은 repo가 아니라 fly에 저장합니다.

```bash
fly secrets set NOTION_TOKEN=... NOTION_AGENDA_DB_ID=... NOTION_PROJECTS_DB_ID=... DISCORD_BOT_TOKEN=... DISCORD_CHANNEL_ID=...
```

```bash
fly deploy
```

## 설정 파일

- `.env` — 비밀값만. **커밋 금지** (이 repo는 public입니다).
- `alliegent.toml` — 스케줄 시각, 노션 속성 이름 매핑, 정체 판단 기준일. 커밋해도 안전합니다.

## 개발

```bash
uv run pytest
```

```bash
uv run ruff check .
```
