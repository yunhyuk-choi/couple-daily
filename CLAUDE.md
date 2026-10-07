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
- ⛔ **그리고 `pending` 행의 복구 경로를 반드시 갖춘다.** 데몬 스레드는 프로세스가 죽으면
  같이 죽지만 DB 행은 `pending` 그대로 남는다 — 아무도 되돌리지 않으면 **영원히 '작성중'** 이다
  (2026-10-07 실사고: 메모리 한도 초과 → Render 자동 재시작 → 블로그 초안이 하루 종일 고착).
  새 백그라운드 생성기를 만들 때 세 가지를 같이 만든다:
  1. **판정** — `_bg_job_verdict(lock, keys, key, updated_at, stuck_after)` 가 단일 원천이다
     (`running`/`orphaned`/`overdue`). 인프로세스 가드가 비어 있으면 스레드가 죽은 것이다.
  2. **재개** — 그 상태를 보는 화면(또는 폴링 엔드포인트)에서 `resume_*_if_orphaned` 로 재스폰.
     ⚠️ **직전 산출물을 절대 지우지 않는다** — 되돌리는 건 `updated_at` 뿐이다.
  3. **탈출구** — 템플릿이 '무슨 일이 났는지' 말하고 사람이 직접 다시 돌릴 버튼을 준다.
     무한 `meta refresh` 만 도는 상태를 남기지 않는다.
  상한은 그 작업의 **최악의 정상 소요**로 잡는다(후기 초안은 claude 콜이 여러 번이라
  `_STUCK_REVIEW`=25분, 단발 콜짜리는 `_STUCK_GENERATING`=5분). 회귀 테스트는
  `tests/test_stuck_recovery.py`.

## ⛔ 512MB 규율 — 실측으로 밝힌 것들 (2026-10-07 OOM)

Render 무료티어는 **512MB 한 칸**에 파이썬 워커 + `claude -p` 가 같이 산다. 실측:

| 무엇 | 실측 |
|---|---|
| 파이썬 워커(부팅 직후, 실서버 프로세스) | **95 MB** |
| `claude -p` **한 개** | **약 400 MB** (앱과 같은 argv·stdin. 넉넉한 머신 기준 상한 — 컨테이너에선 V8 힙이 작게 잡혀 더 낮지만, **둘이면 512MB 를 넘는다**는 결론은 같다) |

### 1. ⛔ claude 는 **절대 둘이 동시에 뜨지 않는다** (가장 큰 범인)

`_CAPTION_SEM`(기본 1)은 '캡션용'이 아니라 **이 앱의 모든 claude 호출이 지나야 하는 단
하나의 문**이다. 사고 직전까지 이 문을 **안 지나는 호출이 셋** 있었다 — 월간 회고
(`/answer` 마다 트리거)·판결(버튼)·오늘의 질문(요청 경로). 신규 기능(키워드 조사→썸네일
→GIF)이 들어오며 후기 초안이 문을 쥐는 시간이 ~20초 → **170초+** 로 늘자, 그 위에 셋 중
하나가 겹칠 확률이 같이 뛰었다. **새 claude 호출을 추가할 때는 반드시 이 문을 지나게 할 것.**

요청 경로(`get_or_create_today_question`)만 특별 취급한다: 무한 대기는 gunicorn
`--timeout 180` 에 걸리므로 `_QUESTION_SLOT_WAIT`(15s)만 기다리고, 못 잡으면 claude 를
돌리지 않고 폴백 질문으로 즉시 응답한 뒤 **백그라운드가 개인화 질문으로 올려준다**
(`upgrade_daily_question`, 아무도 답하지 않았을 때만). 기능을 깎지 않으려는 장치다.

### 2. 바이트 캐시는 **RAM 이 아니라 디스크**에 둔다 (`bytecache.DiskByteCache`)

개수 상한은 메모리를 전혀 묶지 못한다 — 엔트리 크기가 5배 넘게 들쭉날쭉하기 때문이다.
그렇다고 개수를 줄이면 기능이 깎인다. 그래서 **담는 양을 줄이는 대신 자리를 옮겼다**:

| 캐시 | 전(RAM 상주) | 후 | 용량 |
|---|---|---|---|
| `app._blog_proc_cache` (가공완료 JPEG) | 최악 **656MB**(200개 × hq 3.28MB) | **0** (디스크 128MB) | std ~217장 / hq ~39장 |
| `onedrive._bytes_cache` (원본) | **197MB**(64개 × 3.03MB) | **0** (디스크 96MB) | 원본 ~31장 |
| `onedrive._thumb_cache` (썸네일) | 0.83MB(192개) | 그대로 | 엔트리 균일·작음 — 무죄 |

Render 의 ephemeral 저장소는 tmpfs 가 아니라 **disk** 다(캡션 임시 이미지도 이미 파일로
쓴다). 히트 비용은 OneDrive 왕복 + 재인코딩(수 초) → 로컬 파일 read(수 ms)로 **더 싸졌다.**
디스크를 못 쓰는 환경에서는 조용히 '항상 미스'로 동작한다.

### 3. 이미지는 **필요한 만큼만** 디코드한다

| 무엇 | 전 | 후 | 비고 |
|---|---|---|---|
| `_image_display_size` (후기 생성 중 사진마다) | **90.9MB** | **0.0MB** | 크기 두 개 알려고 전체 디코드하던 것 → `size` + EXIF 태그 274. 방향 8종에서 같은 값 확인 |
| `_blog_img_process` hq(원본·q95) | 143MB | **50MB** | 전체 사본 2개 제거 — 출력 **바이트 동일** |
| `_blog_img_process` std(1280·q92) | 112MB | **66MB** | 〃 |
| `_heic_to_jpeg` | 100MB | **85MB** | 〃 |

**Pillow 는 `exif_transpose(img)` 와 `convert("RGB")` 가 각각 전체 사본을 만든다**(12MP 한
장의 RGB 버퍼가 ~36MB). 반드시 `_exif_upright()` / `_as_rgb()` 를 쓴다.

### 4. 무죄 확인 (실서버 프로세스 RSS 로 지상검증)

| 경로 | 실측 |
|---|---|
| `/videos/upload` 50MB (GIF 기능) | Δpeak **+7.0MB** — werkzeug 스풀 → 3.2MiB 청크 PUT |
| `/videos/<id>/stream` 50MB 중계 | Δpeak **+6.7MB** — 제너레이터, 소켓에 흘릴 뿐 |
| `keyword_research.research` (claude 제외) | Δpeak **+0.1MB**, 저장 JSON 6.9KB |
| 신규 기능 전체를 돌린 뒤 서버 RSS | 95MB → **97MB** (상주 증가 없음) |

### 5. ⛔ 서버는 **이미지 픽셀을 만지지 않는다** (3차 — 구조로 푼다)

2차까지는 "같은 일을 더 적은 메모리로" 였다. 3차는 질문을 바꿨다 — **서버가 그 일을
해야 하나?** 열쇠는 하나다: **크롭 좌표는 0~1 정규화라 해상도와 무관하다.**
그러니 '어디를 자를지'(판단)와 '실제로 자르기'(픽셀)를 떼어놓을 수 있다.

```
판단  : claude 가 **작은 렌디션**(수백 KB)만 보고 [x,y,w,h] 를 낸다   ← 해상도 무관
표시  : 브라우저가 그 좌표로 **CSS 크롭**해 보여 준다                 ← 디코딩 0
발행  : 브라우저가 **원본**을 받아 Canvas 로 자른다                   ← 화질은 원본 그대로
```

**이미지 소스는 용도별로 갈린다 — 원본을 아무 데나 부르지 않는다:**

| 용도 | 소스 | 서버 비용 |
|---|---|---|
| 갤러리 그리드 | `onedrive.get_thumbnail` (Graph medium, 실측 4.4KB) | 중계 |
| 상세·크롭 UI 미리보기 | `/blog-img/<id>?v=r1280` (Graph 렌디션) | **중계만** |
| claude 비전의 크롭 판단 | Graph 렌디션 c1600 (757KB) | 중계 |
| 발행용 크롭 산출 | `?v=orig` → **브라우저 Canvas** | **0 (스트리밍)** |

`Graph 렌디션`(`c{W}x{H}`)은 Microsoft 가 **서버에서** 비율을 지켜 줄여 준다. 상한이
문서에 없어서 `onedrive.get_rendition` 이 **돌아온 가로세로를 보고 판정**한다 — 비율이
원본과 다르거나 요구 해상도에 못 미치면 **쓰지 않고 원본으로 폴백**한다. 실제 한도는
`tools/probe_graph_renditions.py` 로 운영에서 한 번 재면 된다.

**실측 (3.03MB · 3024×4032, RSS 봉우리 / tracemalloc 파이썬 할당 봉우리):**

| 경로 | 전 | 후 |
|---|---|---|
| `/blog-img` std (크롭→1280·q92) | **90.7MB** / 6.10MB | **10.2MB** / 2.03MB |
| 발행 소스 (전: `hq=1` 서버 크롭 q95 / 후: `v=orig` 중계) | **78.0MB** / 16.14MB | **3.3MB** / 6.10MB · 디코드 0 |
| `/memories/<id>/image` (첫 청크까지) | 3.2MB / 6.18MB · 첫조각 **3103KB** | 0.3MB / **0.19MB** · 첫조각 **64KB** |
| `/memories/upload` 41.5MB 사진 | 44.4MB / **42.1MB** | 4.3MB / **6.5MB** (크기 무관) |
| 크롭용 치수 1장 | 0.1MB / 6.07MB (원본 보유) | 0.1MB / **0.01MB** (메타만) |
| claude 에게 보여 주는 바이트 | **3103KB** | **757KB** |

**체감 성능도 같이 좋아졌다** (사진 5장짜리 후기, 첫 화면에 필요한 전부):

| 화면 | 전 | 후 |
|---|---|---|
| `/reviews/<id>` HTML+이미지 | **1325 ms** · OneDrive 원본 10회 | **511 ms** · 원본 **0**회 (렌디션 10) |
| `/memories` 첫 화면 | 썸네일 + **근처 원본 전부 선제 다운로드**(5장 = 15.2MB) | 썸네일만. 원본은 **열려는 신호**(hover/pointerdown)에만 |

**⚠️ 바뀐 것 하나 — std 미리보기 픽셀.** 입력이 원본→렌디션으로 바뀌어 리샘플이 한 번
늘었다. **출력 해상도는 완전히 동일**하지만(`_thumbnail_size` 로 맞춘다) 픽셀은 완전히
같지 않다: 사진형 이미지 **PSNR 45.6 dB**(채널 평균차 0.76/255 — 육안 동일), 순수 노이즈
**31.0 dB**(최악). 이 바이트는 **최종 발행본이 아니다**(발행본은 브라우저가 원본에서
자른다 — 오히려 서버 재인코딩 한 세대가 사라져 좋아졌다). 받아들이지 않겠다면
`BLOG_STD_SOURCE=original` 로 바이트까지 동일한 옛 동작으로 돌아간다(봉우리는 90.7MB).

**브라우저 Canvas 크롭은 지상검증했다**(Chrome 154, 로컬 하네스): 1200×1600 사분면 테스트
이미지를 `[0.25,0.30,0.50,0.375]` 로 잘라 **정확히 600×600**(Pillow 기준 박스와 동일),
모서리 색 오차 ±1(JPEG), 크롭이 '전체 프레임'이면 **원본 Blob 을 그대로 통과**(재인코딩 0회).

**`claude -p` 에 URL 을 주는 길도 실측했다**(`CROP_VISION_SOURCE=url`). `--allowedTools
WebFetch Read` 면 **된다** — WebFetch 가 JPEG 를 받아 두고 Read 가 픽셀을 본다(320×240 ·
1600×1200 · 4000×3000 모두 정확히 묘사). 다만 **사진 한 장에 30~33초**가 더 들고(5장이면
+2분 반), 10MB JPEG 한 건은 WebFetch 가 텍스트로 변환해 **실패**했다. 아낄 메모리는
렌디션 한 장치(수백 KB)뿐이라 **기본은 `file`**(렌디션을 임시파일로)이다. URL 을 쓸 때는
`_blog_img_sig` 의 **만료 5분 서명 URL**(렌디션을 가리킨다)을 쓴다 — 새 메커니즘 금지.

### 6. ⛔ OneDrive URL 로 **302 리다이렉트하지 않는다** (재서 뒤집힌 결론)

"서버가 바이트를 들지 말고 브라우저를 OneDrive 로 보내면 되잖아"는 메모리 목표는
달성하지만 **클라이언트 캐시 목표를 정면으로 깨뜨린다**:

1. Graph pre-auth URL 은 **발급할 때마다 다른 URL**(쿼리에 1회용 토큰)이다. 브라우저
   캐시 키는 최종 URL이라 **매번 미스**다. 고정 URL(`/memories/<id>/image`) + `ETag` +
   `Cache-Control` 이면 재방문이 **304** 다.
2. 302 는 Graph 왕복을 **늘린다**(URL 받기 + CDN 받기 = 2회 vs 중계 1회).
3. 그 URL 은 **자격증명이다**(토큰만으로 그 파일이 열린다). 영상 경로가 이미 같은 판단을
   내려 뒀다(`onedrive.probe_direct_cors`: "반환값에 URL을 절대 담지 않는다").

그래서 **고정 URL + 제너레이터 중계 + 강한 클라이언트 캐시**다. 메모리는 302 와 똑같이
평평하고(한 번에 64KB) 캐시는 살아난다.

### 7. 아직 안 고친 것

- **미리보기와 클립보드 HTML 이 같은 문자열이다.** 그래서 상세 화면이 '서버가 자른'
  std 이미지를 받는다. 둘을 갈라 미리보기를 **렌디션+CSS 크롭**으로 바꾸면 서버 디코드가
  **완전히 0** 이 되고 §5 의 화질 각주도 사라진다. 안 한 이유는 어려워서가 아니라,
  `copy_html` 이 **사람이 편집해 저장하는 원본**(`edited_text`)이라 편집·저장 흐름까지
  함께 손봐야 하고 그건 별도 변경으로 다루는 게 맞아서다.
- 48MP(8000×6000) 원본은 여전히 `BLOG_STD_SOURCE=original` 에서만 문제다 — 기본(렌디션)
  경로는 원본 해상도와 **무관**해졌다.

회귀 테스트: `tests/test_image_pipeline.py`(구조 계약) · `tests/test_claude_serialization.py`
· `tests/test_image_memory.py` · `tests/test_stuck_recovery.py`.

## 아키텍처

- **`app.py`** — Flask 앱 팩토리(`create_app`)·라우팅·세션·인증·커플 승인·PWA 라우트.
  월간 인사이트 백그라운드 생성(`regenerate_monthly_report`/`_kick_monthly_report`)도 여기.
  개발은 `python app.py`(SQLite·debug), 프로덕션은 `gunicorn app:app`.
- **`models.py`** — SQLAlchemy: `Couple`·`User`·`DailyQuestion`·`Answer`·`Setting`·
  `MonthlyReport`(캐시된 월간 정성 인사이트) 등.
- **`ai.py`** — `claude` CLI 래퍼. `generate_daily_question`, `generate_monthly_qualitative`,
  `write_review`(블로그 후기), `suggest_keyword_candidates`/`select_keywords`(키워드 조사),
  `suggest_thumbnail_copy`(썸네일 카피).
  긴 생성 프롬프트는 `ai.load_prompt(<이름>)`로 **파일에서** 읽는다.
- **`bytecache.py`** — 디스크에 사는 TTL+바이트예산 캐시(`DiskByteCache`). 사진 원본
  (`onedrive`)·가공완료 JPEG(`app`) 두 캐시의 **단일 원천**이다. 512MB 안에서 '담는 양'과
  '죽지 않기'를 떼어 놓으려고 만들었다 — 근거와 실측은 그 모듈 머리말이 정본.
- **`naver_api.py`** — NAVER API HUB(블로그 검색 · 검색어 트렌드) 얇은 클라이언트. 자격증명은
  **인자로만** 받고 로그·예외에 남기지 않는다. 지표 오독 금지(검색 결과 수 != 검색량,
  상대지수 != 절대 검색 횟수, 빈 트렌드 = 정량 확인 실패)는 모듈 머리말이 정본.
- **`keyword_research.py`** — 후기 재료 → 롱테일 후보 → 네이버 조사 → 메인/보조 선정.
  **키가 없으면 건너뛴다**(`status: "skipped"`) — 생성은 그대로 돈다. 자세한 건
  `docs/blog-review-spec.md` §11.
- **`thumbnail.py`** — 썸네일. Figma 내보내기 CSS(`design/thumbnail-template.css`)를
  파싱해 **수치를 보존한 스펙**을 내고, 브라우저가 보고한 측정값으로 **넘침을 다시
  판정**한다. ⛔ **서버는 픽셀을 만들지 않는다** — Render 무료티어에 Chromium 을 안 올리고
  사용자의 브라우저가 DOM+Canvas 로 그린다. 넘치면 폰트가 아니라 **문구를 줄인다**.
  자세한 건 `docs/blog-review-spec.md` §12.
- **`static/thumbnail.js`** — 그 브라우저 렌더러(DOM 미리보기 · `scrollWidth<=clientWidth`
  넘침 assert · `document.fonts.check` · Canvas 1080×1350 래스터).
- **`gifmaker.py`** — 동영상 → GIF. 프리셋(가로폭·fps)·한도를 **단일 원천**으로 내고,
  브라우저가 보고한 결과로 **용량을 다시 판정**한다. ⛔ **서버는 픽셀을 만들지 않는다** —
  Render 무료티어(512MB·0.1 CPU, `python:3.12-slim`)에 ffmpeg 가 없고 올리지도 않는다.
  넘치면 화질을 몰래 깎지 않고 **내려받기를 막고 길이를 줄이라고 말한다**.
  영상 원본은 사진과 같은 OneDrive 폴더에 보관하되 **`videos` 테이블**로 분리한다
  (vision 캡셔너·EXIF·블로그 서명 URL 경로에 50MB 영상이 들어가지 않게).
  자세한 건 `docs/blog-review-spec.md` §13.
- **`static/gif.js`** — 그 브라우저 변환기. **네이티브 우선**(`<video>` + Canvas),
  실패가 확인되면 **그때 비로소** WASM 디코더(ffmpeg 코어 ESM, 약 32MB)를 lazy 로드한다.
  실패에는 전부 이름이 있다(`gifmaker.DIAG_CODES`) — **조용한 빈 프레임 금지**.
- **`static/imagecrop.js`** — **발행용 크롭을 브라우저가 굽는다**(서버는 픽셀을 안 만진다).
  `v=orig` 로 받은 **원본**을 Canvas 로 잘라 JPEG(q0.95)로 낸다 — 다운스케일본에서
  자르는 것보다 화질이 좋다. ⛔ **표시용 크롭에 쓰지 말 것**(그건 CSS 가 한다);
  Canvas 인코딩은 **발행 버튼 1회**뿐이고, 12MP 캔버스가 48MB라 **한 장씩 순차**로
  돌리며 진행률을 보고한다. 네이버 유저스크립트에는 CSP 때문에 같은 함수의 사본이
  있다(`tools/naver-uploader.user.js` — 고칠 땐 양쪽 같이).
- **`tools/probe_graph_renditions.py`** — Graph 커스텀 렌디션(`c{W}x{H}`)의 **실제 한도**를
  재는 도구. 문서에 상한이 없어서 운영에서 한 번 재서 안다(출력에 URL 을 담지 않는다).
- **`static/vendor/gifenc.esm.js`** — GIF 인코더 사본(MIT). *주 경로*라 CDN 에 매달지
  않는다(테스트가 sha256 으로 무결성을 본다). 손으로 고치지 말고 원본 URL 에서 다시 받는다.
- **`prompts/`** — 파일로 분리한 생성 프롬프트(`blog-review.md`·`keyword-candidates.md`·
  `keyword-select.md`·`thumbnail-copy.md`). **글 품질 튜닝은 여기서** 한다
  — 파이썬을 안 건드린다. 파일 맨 위 사람용 머리말은 첫 `---`까지 잘려 나가고, `{{...}}`
  자리만 런타임에 치환된다.
- **`tests/`** — pytest(네트워크·`claude` 없이 돈다 — 임시 SQLite + `_run_claude` 가짜).
  `pip install -r requirements-dev.txt` 후 `python -m pytest tests -q`.
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
| `GET,POST /settings` | 앱 이름(Setting)·내 표시이름 수정 · 네이버 업로더 키 재발급 · **네이버 API HUB 키(사용자별) 저장/연결확인/삭제** — 값은 다시 렌더하지 않고 설정됨/미설정만 |
| `POST /videos/upload` | (Step 5) 동영상 1개를 **청크 스트림**으로 OneDrive 에 보관 — 바이트를 통째로 메모리에 올리지 않는다 |
| `GET /videos/<id>/stream` | (Step 5) **Range(206) 중계.** `<video>` seek 의 전제이자 same-origin 이라 Canvas taint 없음 |
| `GET /videos/<id>/source` | (Step 5) 바이트 경로 (a)OneDrive 직접 / (b)앱 프록시 를 **실측(ACAO·206)으로** 가르고 행에 캐시 |
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
