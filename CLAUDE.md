# CLAUDE.md — couple-daily

> Claude Code 세션이 이 리포에서 작업할 때 읽는 온보딩 문서. 사람용 개요는 [README.md](README.md).
> 여기서는 *다른 Claude가 빠르게 맥락을 잡고 실수를 피하도록* 아키텍처·제약·함정을 기록한다.

## 이 프로젝트가 하는 일

연인 **두 사람만**을 위한 웹앱. 매일 하나의 "오늘의 질문"에 각자 답하고, 둘 다 답하면
서로의 답이 공개된다. 월말엔 정량+정성 하이브리드 "관계 인사이트"를 본다. PWA로 홈 화면에
설치 가능. (이 리포는 원래 `claude-web-wrapper` 템플릿에서 시작해 용도 변경된 것이다 —
래퍼 전용 엔드포인트(`/start-login`·`/complete-login`·에이전트 실행형 `/ask`)와 프론트는
모두 제거됐다.)

## ⛔ 절대 제약 — AI 메커니즘은 `claude` CLI다

이 앱의 모든 AI 기능은 **백엔드에서 `claude` CLI를 서브프로세스로 실행**해서 동작한다.
**Anthropic API/SDK를 쓰지 않는다.** 이게 이 프로젝트의 존재 이유다.

- 호출 형태: `claude -p` (프롬프트는 **stdin**으로 주입 — 인자 길이 제한 회피 + 셸에 안 노출).
- 구조적 결과가 필요하면 프롬프트에서 "JSON만 출력"을 지시하고, 출력에서 코드펜스/프로즈를
  방어적으로 벗겨낸 뒤 파싱한다 (`ai._extract_json`).
- `claude -p`는 느릴 수 있는 에이전트다(수 초). 타임아웃 120s, 실패 시 우아하게 폴백:
  질문은 `FALLBACK_QUESTIONS`, 인사이트 정성 파트는 생략.

## ⛔ 규칙 — AI(claude CLI)·에이전트 동작은 절대 요청(HTTP) 경로에서 동기 실행 금지

`claude` CLI(subprocess)는 느리고(무료 0.1 CPU에서 10~30초) 메모리(약 265MB)를 많이 쓴다.
페이지 렌더/요청 핸들러에서 **동기로 호출하면 워커 타임아웃·OOM·502로 터진다** (실측된 크래시 모드).

- **기본 패턴(캐시 + 백그라운드 재생성):** AI 생성은 **백그라운드 스레드**에서 돌리고 결과를
  **DB 테이블에 저장**한다. 요청 핸들러는 **DB 캐시본만 읽어 즉시 렌더**한다. 관련 이벤트(예: 새
  답변)가 발생하면 재생성을 트리거한다. **레퍼런스 구현 = `MonthlyReport` / `regenerate_monthly_report`**
  (`/insight`는 캐시본만 읽고 claude를 요청 경로에서 절대 호출하지 않는다).
- **즉시성이 필요한 경우(대화형 등):** 요청은 **로딩/플레이스홀더 상태로 즉시 응답**하고, AI는
  백그라운드에서 돌린 뒤 완료되면 **폴링(자동 새로고침/fetch) 또는 SSE로 결과를 나중에 전달**한다.
  HTTP 응답을 AI 호출로 **절대 블로킹하지 않는다**.
- **AI 아닌 값싼 계산(개수·통계 등)은 요청에서 실시간**으로 해도 된다(예: `compute_monthly_stats`).
  지연시켜야 하는 건 **에이전트/AI 작업만**이다.
- 백그라운드 스레드는 **자체 app context + 새 DB 세션**(요청 세션을 스레드 간 공유 금지),
  **예외 catch + 로깅**(스레드 밖으로 예외 전파 금지), **중복 동시 생성 가드**(DB status + in-process
  set)를 반드시 갖춘다.

## 아키텍처

- **`app.py`** — Flask 앱 팩토리(`create_app`)·라우팅·세션·인증·커플 승인·PWA 라우트.
  월간 인사이트 백그라운드 생성(`regenerate_monthly_report`/`_kick_monthly_report`)도 여기.
  개발은 `python app.py`(SQLite·debug), 프로덕션은 `gunicorn app:app`.
- **`models.py`** — SQLAlchemy: `Couple`·`User`·`DailyQuestion`·`Answer`·`Setting`·
  `MonthlyReport`(캐시된 월간 정성 인사이트) 등.
- **`ai.py`** — `claude` CLI 래퍼. `generate_daily_question`, `generate_monthly_qualitative`.
- **`insights.py`** — 정량 지표 (DB 집계). **점수화·궁합% 금지**, 정직한 카운트만.
- **`templates/`** — `base.html` + 화면별. **파일명은 정상**(옛 래퍼의 `intex.html` 오타는 제거됨).
- **`static/`** — `style.css`(핑크 테마)·`sw.js`(서비스 워커)·`emoji/`(Fluent, MIT)·`icons/`(PWA).

### 엔드포인트

| 경로 | 하는 일 |
|---|---|
| `GET /` | 로그인/상태에 따라 로그인·대기·승인·오늘의 질문 대시보드로 분기 |
| `GET,POST /signup` | 초대코드 없으면 커플 생성+관리자, 있으면 pending 가입 |
| `GET,POST /login` | 세션 로그인 (IP당 최소 rate limit) |
| `POST /logout` | 세션 클리어 |
| `POST /approve/<user_id>` | 관리자가 pending 파트너 수락 → approved |
| `POST /answer` | 오늘 질문에 답변(있으면 수정). 둘 다 답해야 공개 |
| `GET /history` | 지난 질문+양쪽 답 (공개된 것만) |
| `GET /insight` | `?year=&month=` 월간 정량(실시간)+정성(DB 캐시본). claude는 요청 경로에서 호출 안 함; 없으면 백그라운드 생성 트리거 후 플레이스홀더 |
| `GET,POST /settings` | 앱 이름(Setting)·내 표시이름 수정 |
| `GET /manifest.json` | 동적 매니페스트(APP_NAME 반영) |
| `GET /sw.js` | 서비스 워커 (루트 스코프) |
| `GET /healthz` | 헬스체크 |

### 데이터 흐름 요점

- **오늘의 질문**: `get_or_create_today_question()`가 (couple, 오늘) 로 조회 → 없으면 최근 8개
  질문+답을 프롬프트에 넣어 생성·저장. `UniqueConstraint(couple_id, q_date)`로 동시 생성 방어.
- **공개 규칙**: `DailyQuestion.both_answered`(답 2개)일 때만 서로의 답이 보인다.
- **접근 제어**: `active_couple_required` = 로그인 + `status=='approved'` + `couple_id` 있음.
- **월간 인사이트**: 정량 지표는 `compute_monthly_stats`로 요청마다 실시간 계산. 정성 파트는
  `MonthlyReport`(couple_id, year, month 유니크)에 캐시하고 `regenerate_monthly_report`가
  백그라운드 스레드에서 갱신. `/answer` 커밋 후 현재 달 리포트를 재생성 트리거, `/insight`는
  캐시 `ready`면 즉시 렌더·아니면 생성 트리거 후 플레이스홀더(생성 중엔 자동 새로고침). 위
  "AI는 요청 경로에서 동기 실행 금지" 규칙의 레퍼런스 구현.

## ⚠️ 반드시 알아야 할 함정 (실측)

1. **인코딩 (Windows)** — `subprocess`가 기본 cp949로 `claude` 출력을 디코딩하면 한글/이모지에서
   `UnicodeDecodeError`. **모든 CLI 실행은 `text=True, encoding='utf-8', errors='replace'`.**
   (`ai._run_claude`가 이미 그렇게 함.)
2. **CLI 커맨드명** — 비대화형은 `claude -p`. `claude code`/`claude login`은 **없다**.
   prod 인증은 `claude setup-token`으로 만든 장기 토큰(`CLAUDE_CODE_OAUTH_TOKEN` 시크릿).
3. **`claude -p`는 stdin 프롬프트를 읽는다** — 긴 월간 인사이트 프롬프트를 인자로 주면 Windows
   인자 길이 한계에 걸릴 수 있어 stdin으로 준다. (실측으로 `printf ... | claude -p` 동작 확인.)
4. **JSON 파싱 방어** — 모델이 가끔 코드펜스/설명을 붙인다. `_extract_json`이 펜스 제거 →
   전체 파싱 → 첫 `{...}` 스팬 파싱 순으로 폴백.

## 🔐 보안

- 옛 래퍼의 **에이전트 실행형 `/ask`는 완전히 제거**됐다. CLI가 실행되는 곳은 서버 측
  질문/인사이트 생성뿐이고, 프롬프트는 **앱이 통제**한다. 사용자 답변 텍스트는 stdin
  프롬프트 안의 **데이터**로만 들어가며, `shell=True`·인자 삽입은 쓰지 않는다.
- 세션 시크릿은 `SECRET_KEY` 환경변수. prod(`FLASK_ENV=production` 또는 `DATABASE_URL` 존재)에선
  `SESSION_COOKIE_SECURE` on. 비밀번호는 werkzeug 해시. 로그인은 IP당 최소 rate limit.

## 로드맵

- **F1 (완료)** — 인증/커플 연결(수동 승인) · 오늘의 질문(개인화+공개규칙+히스토리) ·
  월간 인사이트(정량+정성, 점수화 없음) · PWA · Fluent Emoji.
- **F2** — 자연어 추억 검색 (지난 답변을 자연어로 검색; claude로 관련 답변 추림).
- **F3** — AI 데이트/기념일 비서 (기념일 리마인드 + 데이트 아이디어 추천).

## 개발

```bash
pip install -r requirements.txt
python app.py     # http://127.0.0.1:5000 (SQLite 폴백)
```
