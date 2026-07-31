# 우리의 하루 (couple-daily)

둘만을 위한 아주 작은 웹앱. 매일 하나의 **오늘의 질문**에 각자 답하고, 둘 다 답하면
서로의 답이 공개돼. 월말엔 한 달의 답변을 돌아보는 **관계 인사이트**를 볼 수 있어.

앱 이름(기본값 "우리의 하루")은 환경변수나 인앱 설정에서 바꿀 수 있고, PWA라서 홈 화면에
설치할 수 있어.

## 핵심 정체성 (중요)

이 앱의 AI는 **`claude` CLI를 백엔드에서 서브프로세스로 실행**하는 방식이야
(Anthropic API/SDK가 **아님**). 오늘의 질문 생성과 월간 정성 인사이트는 모두
`claude -p`(프롬프트는 stdin으로 주입)를 호출하고, JSON만 출력하도록 지시한 뒤 파싱해.
이게 이 프로젝트의 존재 이유이자 지켜야 할 제약이야.

## 기능 (Feature 1)

1. **인증 + 커플 연결 (수동 승인)** — 이메일/비밀번호 가입(werkzeug 해시), 서버 세션.
   첫 사용자가 커플 공간을 만들면 관리자가 되고 초대 코드를 받아. 두 번째 사용자가 그
   코드로 가입하면 `pending` 상태가 되고, 관리자가 "수락"하면 커플로 연결돼. 승인+연결된
   사용자만 앱을 쓸 수 있어.
2. **오늘의 질문** — 커플당 하루 한 개. 지난 답변을 프롬프트에 넣어 개인화한 질문을
   claude가 생성하고 그날 고정돼. 둘 다 답하기 전엔 서로의 답이 안 보이고, 둘 다 답하면
   공개돼. 지난 질문/답 히스토리도 볼 수 있어.
3. **월간 관계 인사이트 (하이브리드)** — 정량(코드로 DB 집계: 참여일수, 최고 연속기록,
   먼저 답하는 사람, 평균 답 길이 추세)과 정성(claude가 실제 답변을 읽고 주제/분위기/가장
   달랐던 질문/한 줄 요약). **가짜 사랑 점수·궁합 %는 없어.** 근거 있는 부드러운 관찰만.
4. **PWA** — `manifest.json`(동적, APP_NAME 반영) + 서비스 워커(오프라인 셸/설치 가능).
   iOS는 Safari 공유 → "홈 화면에 추가"로 설치.
5. **예쁜 이모지** — Microsoft Fluent Emoji(MIT) SVG를 `static/emoji/`에 큐레이션해서
   `<img>`로 사용. 앱 아이콘(192/512 png)도 Fluent "Two hearts"에서 생성.
   출처/라이선스: [`static/emoji/LICENSE.md`](static/emoji/LICENSE.md).

## 아키텍처 / 파일 맵

| 파일 | 역할 |
|---|---|
| `app.py` | Flask 앱 팩토리 · 라우팅 · 세션 · 인증 · 커플 승인 · PWA 라우트 |
| `models.py` | SQLAlchemy 모델 (Couple, User, DailyQuestion, Answer, Setting) |
| `ai.py` | **`claude` CLI 래퍼** — 질문/정성 인사이트 생성 (stdin 프롬프트, JSON 파싱) |
| `insights.py` | 정량 지표 계산 (DB 집계, 점수화 없음) |
| `templates/` | Jinja 템플릿 (base + 각 화면) |
| `static/style.css` | 따뜻한 핑크 테마 |
| `static/sw.js` | 서비스 워커 (오프라인 셸) |
| `static/emoji/*.svg` | Fluent Emoji (MIT) |
| `static/icons/*.png` | PWA 아이콘 |
| `Dockerfile` · `fly.toml` · `.github/workflows/deploy.yml` | 배포 |

### 주요 엔드포인트

`GET /`(대시보드/오늘의 질문) · `GET,POST /signup` · `GET,POST /login` · `POST /logout` ·
`POST /approve/<user_id>` · `POST /answer` · `GET /history` · `GET /insight` ·
`GET,POST /settings` · `GET /manifest.json` · `GET /sw.js` · `GET /offline` · `GET /healthz`

## 로컬 실행

```bash
pip install -r requirements.txt
python app.py            # http://127.0.0.1:5000  (SQLite 폴백, debug 리로더)
```

- `DATABASE_URL`이 없으면 `instance/couple_daily.db`(SQLite)로 자동 폴백해서 Postgres 없이도 돌아가.
- 오늘의 질문/인사이트 생성이 동작하려면 이 머신에 `claude` CLI가 로그인되어 있어야 해.
  (CLI가 없거나 실패하면 질문은 안전한 기본 질문으로 폴백하고, 인사이트 정성 파트는 생략돼.)

### 환경변수

| 변수 | 용도 | 기본값 |
|---|---|---|
| `SECRET_KEY` | 세션 서명 키 | `dev-insecure-change-me` (prod에선 반드시 설정) |
| `DATABASE_URL` | Postgres 연결 (`postgres://`도 자동 정규화) | 없으면 SQLite |
| `APP_NAME` | 최초 시드용 앱 이름 (이후엔 설정 화면 값 우선) | `우리의 하루` |
| `FLASK_ENV` | `production`이면 보안 쿠키 on | (unset) |
| `CLAUDE_CODE_OAUTH_TOKEN` | prod에서 CLI 인증 토큰 | — |

## 배포 (Fly.io) — 사용자가 직접 해야 하는 단계

> 이 리포는 배포 설정만 제공하고 **실제 배포는 하지 않았어**(계정 접근 없음).

1. **Fly CLI 설치 & 로그인**: `flyctl auth login`
2. **앱 생성**: `fly apps create <원하는-앱이름>` → `fly.toml`의 `app =` 값을 그 이름으로 수정.
3. **Postgres 준비**: `fly postgres create` 후 앱에 attach (`fly postgres attach <db>`) → `DATABASE_URL` 시크릿이 자동 주입돼. (외부 Postgres면 직접 `fly secrets set DATABASE_URL=...`)
4. **Claude CLI 토큰 생성**: 로컬에서 `claude setup-token` 실행 → 장기 토큰 발급.
5. **Fly 시크릿 설정**:
   ```bash
   fly secrets set SECRET_KEY="$(python -c 'import secrets;print(secrets.token_hex(32))')"
   fly secrets set CLAUDE_CODE_OAUTH_TOKEN="<claude setup-token 값>"
   # DATABASE_URL은 postgres attach로 자동 설정됨 (아니면 직접 set)
   ```
6. **GitHub 자동 배포**: Fly 배포 토큰 발급 `fly tokens create deploy` → GitHub 리포
   **Settings → Secrets and variables → Actions**에 `FLY_API_TOKEN` 이름으로 추가.
   이후 `main`에 push하면 `.github/workflows/deploy.yml`이 롤링(무중단) 배포해.
7. 최초 수동 배포로 확인: `fly deploy`.

> ⚠️ prod 컨테이너에서 `claude` CLI가 `CLAUDE_CODE_OAUTH_TOKEN`으로 인증되는지 실환경 확인은
> 계정이 있어야 가능해 (여기선 미검증). 토큰 환경변수명이 CLI 버전에 따라 다를 수 있으니
> `claude setup-token` 안내 문구를 따라줘.

## 로드맵

- **F1 (완료)** — 인증/커플 연결 · 오늘의 질문 · 월간 인사이트 · PWA · Fluent Emoji.
- **F2** — 자연어 추억 검색 (지난 답변을 자연어로 검색).
- **F3** — AI 데이트/기념일 비서 (기념일 리마인드 + 데이트 아이디어 추천).

## 라이선스 / 출처

- 이모지: Microsoft Fluent Emoji, MIT — [`static/emoji/LICENSE.md`](static/emoji/LICENSE.md).
