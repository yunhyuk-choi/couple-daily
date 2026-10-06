# 데이트 후기 → 네이버 블로그 포스트 자동생성 — 스펙 (durable blueprint)

> 이 문서는 세션이 끊겨도 다음 세션이 이어받을 수 있게 합의된 스펙을 박제한 것이다.
> 코드와 함께 커밋한다. **P1(완료)** = 폼 + 모델 + 목록/상세. **P2(완료)** = AI 생성 + 인앱 웹뷰 미리보기 + 네이버 복사텍스트.
> **v2(완료 — 노선 2 Step 1)** = 입력 폼 재설계 · 프롬프트 파일 분리·교체 · 출력 스키마 정리 · 제목 후보 3개.
> 계획서는 [blog-review-v2-plan.md](blog-review-v2-plan.md), 무엇이 왜 바뀌었는지는 아래 **§10**.

## 1. 기능 개요

커플이 다녀온 곳을 **주제/장소 · 위치 · 재료 칸 8개(전부 선택) · 자유 서술 · 반쪽별 별점 ·
추억 사진(순서)** 으로 입력하면, 그걸 재료로 **네이버 블로그에 그대로 올릴 수 있는 포스트**를
AI가 자동 생성해 주는 기능이다.

**내용 규율의 정본은 gf-blog 프롬프트(`C:\temp\gf-blog\naver-blog-prompt.md`)다** — 조회수·일일
방문자가 더 높은 쪽, 즉 실측으로 검증된 글쓰기다. 숫자 근거는 같은 작성자 블로그 32편 분석
(`naver-ent-autopost/docs/REFERENCE-STYLE.md`). 우리가 지키는 것은 **배선**(폼·워커·상태머신·
네이버 업로더)이고, 글쓰기는 그쪽을 따른다.

- AI 메커니즘은 앱 전체 제약에 따라 **`claude` CLI 백엔드 서브프로세스**다 (Anthropic API/SDK 금지 — `ai.py`).
- AI는 **요청 경로에서 동기로 돌지 않는다** — 백그라운드 스레드에서만 (앱 절대 제약).

## 2. 플로우

```
[작성 폼]  주제/장소 · 위치 · 재료 칸 8개(선택) · 자유 서술 · 별점(0~10 드래그) · 사진 선택+순서
   │  저장 (P1: status='draft')
   ▼
[키워드 조사] (Step 3, 선택)  롱테일 후보 → 네이버 블로그 검색·트렌드 → 메인/보조 선정
   │                   키가 없으면 통째로 건너뛴다(죽지 않는다) → research_json
   ▼
[백그라운드 AI]  (P2)  claude로 포스트 생성 → ai_json 저장, status 'pending'→'ready'/'failed'
   │                   프롬프트는 prompts/blog-review.md (v2 — 코드에 안 박는다)
   ▼
[상세]  제목 후보 3개 선택 + 인앱 웹뷰 미리보기(이미지 인라인) + 편집 가능한 네이버 복사본
```

- P1은 여기서 저장까지만 한다. 상세에는 "AI 초안은 다음 단계에서 생성돼요" placeholder 카드를 보인다.
- P2가 그 placeholder를 미리보기 + 복사텍스트로 교체한다.

## 3. 데이터 모델 — `BlogReview` (table `blog_reviews`)

| 컬럼 | 타입 | 설명 |
|---|---|---|
| `id` | PK Integer | |
| `couple_id` | FK couples, not-null, index | 커플 스코프 |
| `created_by` | FK users, not-null | 작성자 |
| `topic` | String(200), not-null | 주제/장소명 |
| `location` | String(300), nullable | 위치 |
| `prose` | Text, not-null | **자유 서술 원문** (v2: '3~4줄' 가이드 폐기 — 길수록 좋다) |
| `visited_when` | String(100), nullable | (v2) 언제 갔는지 — 요일·시간대 |
| `visit_reason` | String(300), nullable | (v2) 왜 갔는지 — 방문 계기 |
| `access_note` | String(300), nullable | (v2) 찾아가는 길·층·역·주차·좌석 |
| `waiting` | String(200), nullable | (v2) 웨이팅·입장 — 대기 분·앞 팀 수·줄서기 방식 |
| `order_items` | Text, nullable | (v2) 주문한 것과 가격·총액 |
| `highlight` | String(300), nullable | (v2) 가장 기억에 남은 것 하나 |
| `downside` | String(300), nullable | (v2) 아쉬운 점 |
| `researched` | Text, nullable | (v2) **검색해서 확인한** 공개 정보(어미가 달라진다) |
| `overall_score` | Integer, not-null | 0~10 (반쪽별 드래그 합계) |
| `photo_ids` | Text, nullable | JSON list[int] — 선택한 Photo id, **순서 유지** |
| `ai_json` | Text, nullable | P2 결과(구조화 JSON) |
| `edited_text` | Text, nullable | P2 사용자가 편집한 복사텍스트 |
| `research_json` | Text, nullable | (Step 3) 키워드 조사 메모 JSON — 후보·근거·선정 이유·한계 |
| `status` | String(16), not-null, default `'draft'` | 'draft'=생성됨·AI 미실행. P2에서 'pending'/'ready'/'failed' |
| `created_at` / `updated_at` | DateTime | |

- `@property photos_ordered` → `photo_ids` 순서대로 이 커플의 Photo 행을 돌려준다 (JSON 디코드 → 조회 → 순서 유지 → 없는 id 스킵).
- `@property details` → 채워진 v2 재료 칸만 `{필드: 값}`으로. `ai.write_review(details=...)` 입력.
- `@property research` → `research_json` 디코드(없거나 깨졌으면 None). 조사 메모 카드 입력.
- `BlogReview.DETAIL_FIELDS` = 재료 칸 이름 튜플(단일 원천). **순서가 곧 글의 흐름 순서**다.
- 테이블 자체는 `db.create_all()`가 만들지만, **v2 컬럼 8개는 운영 DB에 이미 있는 테이블에
  붙여야 하므로** `models.run_startup_migrations()` §7이 없는 컬럼만 `ALTER TABLE ... ADD
  COLUMN`한다(Postgres·SQLite 공통, 멱등). 전부 nullable이라 **기존 행은 NULL로 남고**
  화면·재생성 모두 정상 동작한다(값이 없으면 프롬프트에서 그 줄이 빠질 뿐).

## 4. AI 출력 JSON 형태 (`ai_json`에 저장 — v2)

`ai.write_review(topic, location, prose, overall_score, photos, details=None) -> dict|None`이
`claude`를 **한 번** 호출해 만든다(`_normalize_review`로 정규화). `photos` 인자는
`{"index": i, "caption": <그 사진 AI 캡션>, "tags": [..]}`의 **순서 리스트**,
`details`는 §3의 재료 칸 dict(빈 칸은 안 들어간다).

```json
{
  "title": "문래역 저녁 고기집 문래갈매기, 직원이 구워주는 갈매기살 2인분 후기",
  "title_target": "문래에서 주말 저녁에 고기 먹을 곳을 찾는 커플",
  "title_candidates": [
    { "title": "...", "structure": "쉼표 2절", "reason": "..." }
  ],
  "title_pick_reason": "왜 이걸 골랐는지 1~2문장",
  "blocks": [
    { "type": "para",    "text": "본문 단락" },
    { "type": "heading", "text": "검색어이면서 답인 소제목" },
    { "type": "image",   "photo_index": 0, "crop": [x,y,w,h] },
    { "type": "quote",   "text": "한 줄 매듭(선택)" },
    { "type": "ratings", "items": [{ "aspect": "전체 만족도", "score": 8 }] }
  ],
  "hashtags": ["#문래갈매기", "#문래맛집"]
}
```

- **블록은 네 종류뿐이다** — `para` / `heading` / `image` / `quote`. AI는 이 넷만 낸다.
  `ratings` 하나는 **시스템이** 사용자 총점으로 끝에 붙인다.
- **`info`(요약표)·`faq`(고정 Q&A)는 v2에서 폐기** — gf-blog가 요약표·장단점표·추천대상표와
  고정 섹션을 금지한다. 정보는 *그 정보가 필요한 문단에서* 말한다.
  ⚠️ **렌더러(`_review_copy_html`·`_review_copy_text`)는 두 타입을 계속 그린다** — 운영 DB에
  남아 있는 옛 `ai_json` 때문이다. 생성만 막고 표시는 유지한다.
- **`summary`는 선택이고 기본은 빈 문자열이다** (v1의 answer-first 고정 폐기). 값이 있으면
  정규화가 **맨 앞 para로 흡수**하므로, 하류(렌더·복사본·export)는 `blocks`만 보면 된다.
- **`ratings`는 사용자 입력 별점이고 표로 만들지 않는다.** AI가 낸 항목별 별점은 폼에 대응
  입력이 없어 날조이므로 정규화가 **버린다**. 렌더는 평문 한 줄(`총점 ★★★★☆ (8/10)`).
- `blocks[].photo_index` = `photos` 순서 리스트의 0-based 인덱스. 범위 밖이면 블록을 버린다.
- `crop`은 AI가 아니라 `_attach_section_crops`(비전)가 심고, 사용자가 상세에서 조정한다.
- **정규화 규칙**: 모든 문자열 `.strip()`; score 0..10 클램프; `blocks`≤60·`hashtags`≤10·
  `title_candidates`≤3(최종 title이 후보에 없으면 맨 앞에 끼워 넣어 ≤4); 해시태그 `#` 자동
  부착; `title`이 없거나 서술 콘텐츠(para/quote)가 하나도 없으면 `None`(→ 워커가 실패 처리).

### 4.1 제목 후보 3개 (v2)

AI가 **타깃 한 문장 → 후보 3개(서로 다른 구조) → 기준 비교 → 선정 + 이유**를 낸다.
상세 화면이 라디오로 보여주고, `POST /reviews/<rid>/title` (`index`=0-based)로 바꿔 고른다.
그 라우트는 `ai_json.title`만 갈아끼우고 **`edited_text`를 비운다**(안 그러면 화면 제목과
초안이 어긋난다 — 편집본이 있으면 프런트가 confirm을 띄운다). 후보 목록은 보존된다.

실측 패턴(2026년 23편, `REFERENCE-STYLE.md`): **쉼표 2절이 65%** —
`[검색으로 도달하는 말], [이 글만 답하는 것]`. 길이 중앙 33자, 물음표·느낌표 **0편**,
과장어 0건, 상호는 21/23편에 들어가지만 **상호로 시작하지 않는다**(지역·업종이 먼저).

## 5. 글쓰기 규율 — `prompts/blog-review.md` (단일 원천)

프롬프트는 **코드에 박지 않는다.** `ai.load_prompt("blog-review")`가 파일을 읽어
`{{INPUT_BLOCK}}` / `{{PHOTO_BLOCK}}` / `{{PHOTO_RULE}}`만 치환한다(파일 맨 위의 사람용
머리말은 첫 `---`까지 잘라낸다). 글 품질 튜닝 = 그 마크다운만 고치기.

정본은 gf-blog 프롬프트이고, 그 파일이 인코딩하는 핵심은 이렇다:

- **표 금지 · 볼드 금지 · 형광펜/색 강조 금지 · 본문 이모지 금지.** 요약표·장단점표·
  추천대상표도 금지. (그래서 v1의 인라인 강조 토큰 `*핑크*` / `` `파랑` `` / `==형광펜==` /
  `**굵게**`를 프롬프트에서 걷어냈다. 파서는 옛 후기용으로 남아 있다.)
- **글자 수·키워드 반복 횟수·소제목 개수·감정 표현 횟수를 고정하지 않는다.**
  (v1의 `적정 분량 900~1400자`를 **삭제했다** — 실측 630~3,350자로 5.3배 차이다.)
- **경험 / 조사 / 추정을 어미로 구분한다.** 직접 겪은 것 `~했어요` `~더라고요`,
  검색해서 확인한 공개 정보 `~것으로 안내돼 있어요`, 추정은 **아예 쓰지 않는다**.
  맛집은 경험이 기본이고, 조사 정보는 폼의 `researched` 칸에 적힌 것만 쓴다.
- **No-fabrication**: 재료에 없는 가격·영업시간·메뉴·직원 발언·동행 반응을 지어내지 않는다.
  방문 당시 결제액과 현재 공개 메뉴 가격을 섞지 않는다.
- **방문 흐름**: 방문 계기 → 위치·입구 → 내부·자리 → 메뉴·주문·가격 → 경험 → 짧은 총평.
  도입은 한 단락 100자 안팎(주문·맛 평가·결론을 미리 풀지 않는다).
- **소제목 = 검색어이면서 동시에 답** (`문래갈매기 웨이팅은 15분 정도였어요`). 400~600자마다
  하나 정도이고 개수는 고정하지 않는다. 마지막 소제목이 총평 자리.
- **한 단락 = 2~3문장 = 70~110자.** 한 문장을 한 줄씩 기계적으로 끊지 않는다.
- **숫자를 그 숫자가 필요한 문장에 박는다** (대기 분·앞 팀 수·인분·가격·총액).
- **과장·확언 금지** — `무조건` `역대급` `실패 없는`은 코호트 23편에 0건. 아쉬운 점은 솔직히.
- **레퍼런스는 구성·호흡만 참고**하고 문장·감탄사·경험·직원 발언을 복제하지 않는다.
- **작성 후 확인 체크리스트 18항목**(맛집용으로 재작성)이 프롬프트 끝에 있다.
- few-shot은 **최신 7편 코호트(2026-09-13~10-03)에서만** 뽑았다. 2024 개발기록 9편과
  '한 문장 = 한 줄' 16편은 어투가 달라 쓰지 않는다(섞으면 문체가 오염된다).

## 5.1 네이버 복사본 포맷

표시·복사 경로는 둘이다. **HTML**(`_review_copy_html` — 실제 미리보기·클립보드 본문)과
**플레인 텍스트**(`_review_copy_text` — text/plain 폴백). 둘 다 `_ensure_blocks`로 옛/새
스키마를 `blocks` 하나로 통일한 뒤 순서대로 평문화한다. `edited_text`가 있으면 그것이 먼저다.

v2에서 바뀐 것:

- **`<table>`을 쓰지 않는다.** 별점은 `총점 ★★★★☆ (8/10)` 평문 한 줄(옛 후기의 항목별
  별점도 같은 평문 줄로).
- **`<b>`를 쓰지 않는다** — 시스템 헤더(`방문 날짜 : 2026년 10월`)·info·FAQ 전부 평문.
- `quote` 블록이 없을 때 붙는 마무리 문구에서 `한 번 가보시길 추천드려요`를 뺐다.
  대신 레퍼런스가 실제로 쓰는 맺음(`운영시간과 메뉴는 변동될 수 있으니 방문 전 최신 정보를
  확인해 주세요.`)으로 바꿨다.
- 플레인 텍스트에서 `⭐ 별점` 머리글을 없앴다(총점 한 줄이라 머리글이 군더더기).
- 이미지는 네이버에 붙여넣을 수 없으므로 플레인 텍스트에선 `[사진 N] — 캡션` 마커.
  (HTML 경로는 공개 서명 URL로 실제 이미지를 넣는다 — §5.2 이후 그대로.)

## 5.2 백그라운드 워커 · 라우트 (P2)

- `generate_review(app, review_id)` — 데몬 스레드, review_id 가드로 중복 차단, 자체
  `app_context`, `_CAPTION_SEM` 안에서 **(1) 키워드 조사(선택) → (2) `ai.write_review`**를
  직렬화해 돈다(512MB: 동시에 claude 하나). 조사 결과는 성패와 무관하게 `research_json`에
  남는다 — 사람이 '왜 조사가 안 붙었는지'를 화면에서 봐야 하기 때문이다.
  성공 시 `ai_json`+`status='ready'`, 실패 시 `'failed'`(단 직전 성공 초안 있으면 유지).
  커밋 rollback 가드, 절대 raise 안 함. **claude는 오직 이 워커에서만.**
- 라우트: `review_new` POST → `status='pending'` + 스폰 → 상세로. `review_edit` POST →
  입력 변경이므로 pending + `edited_text=None` + 재생성 스폰. `POST .../regenerate`
  (`review_regenerate`) → pending + edited_text 비우고 재생성. `POST .../save-text`
  (`review_save_text`) → 편집 복사본을 `edited_text`에 저장(fetch면 JSON, 아니면 리다이렉트).
- 상세(`review_detail`): `pending`이면 "초안 쓰는 중" + `pending`일 때만 `<meta refresh 4s>`
  (case_detail 판결중 패턴). `ready`면 **웹뷰 미리보기**(제목·요약·info·섹션+인라인 전체
  이미지·별점·FAQ·해시태그) + **편집 가능한 네이버 복사본**(복사/저장/다시 생성). `failed`면
  재시도. 전부 커플 스코프, cross-couple 404.

## 6. 작성 폼 UI 상세

### 반쪽별 별점 (0~10 드래그 위젯)
- **별 5개**, 각 별 = 2점 → 홀수 값은 별의 **반쪽**만 채운다.
- 의존성 없는 구현 (SVG 또는 두 겹 별 글리프 + clip/gradient half-fill).
- 입력 지원: **터치 드래그 + 마우스 드래그 + 탭(pointer events)** + **키보드(←/→ ±1, Home/End)** a11y.
- 숫자 표시 ("8 / 10"). 값은 hidden input `name="overall_score"`.
- 0~10 clamp (양끝 넘어가면 clamp). 핑크 별.

### 사진 선택 + 순서
- 추억 앨범 사진을 썸네일(`/memories/<id>/thumb`, shimmer skeleton)로 선택 그리드에 로드.
- 탭 = 선택 토글(체크 오버레이). 선택된 사진은 아래 **순서 스트립**에 나타난다.
- 순서 재정렬: pointer 기반 드래그 + **좌/우 이동 버튼 폴백**(드래그가 까다로우면 버튼으로 항상 가능).
- 최종 순서 id 리스트 → hidden input `name="photo_ids"` (JSON array), 모든 변경마다 동기화.
- 선택 개수 표시. (P1은 기존 앨범에서 선택으로 충분 — 여기서 새 업로드 없음.)

### 재료 칸 8개 (v2) — `조금 더 구체적으로`
- **전부 선택 입력**이고, 폼에 `모르면 그냥 비워 둬 — 빈칸이 AI가 지어낸 문장보다 나아`를
  **문구로 적어 둔다.** 짧은 느낌점만으로 1,500자 글을 만들면 AI가 지어내는데, 그게 정본
  프롬프트가 가장 강하게 금지하는 바로 그것이다.
- 각 칸에는 레퍼런스에서 실제로 나온 모양의 placeholder를 넣는다
  (`예: 앞에 3팀, 약 15분 기다림`, `예: 신라칼낙새 17,000원 · 감자전 12,000원, 둘이 총 46,000원`).
- 서버에서도 길이를 자른다(`_parse_review_details`). 빈 값은 **NULL로 저장**한다.
- `researched` 칸 아래에만 추가 안내: *직접 겪은 게 아니라 검색으로 안 것만* — 글에서
  어미가 달라지기 때문이다.
- 어떤 칸을 왜 넣었는지(원문 근거)는 §10 표.

### 나머지 필드
- 주제/장소 text (필수), 위치 text (선택).
- **자유 서술 textarea (필수)** — v2에서 `rows=4` → `rows=10`, 라벨도 `느낀점·특징(3~4줄)` →
  `자유 서술(길수록 좋아)`. placeholder가 무엇을 적을지 네 줄로 안내한다. 사람 말투의
  원천이라 길게 받는다.
- Submit "등록 → AI 초안 생성".
- 유효성 오류 시 입력값(재료 칸 포함) + 선택 사진 유지하며 재렌더.

## 7. 라우트 (모두 `@active_couple_required`, 커플 스코프)

| 메서드 | 경로 | endpoint | 역할 |
|---|---|---|---|
| GET | `/reviews` | `reviews` | 이 커플 후기 목록(최신순). 카드: 주제·위치·별점·작성일·상태힌트. 빈 상태. + 새 후기 FAB |
| GET,POST | `/reviews/new` | `review_new` | 작성 폼 / 저장(P2: status='pending' + 초안 생성 스폰) |
| GET | `/reviews/<rid>` | `review_detail` | 후기 표시 + (P2) 웹뷰 미리보기·네이버 복사본 |
| GET,POST | `/reviews/<rid>/edit` | `review_edit` | 주제/위치/재료 칸/자유 서술/별점/사진 편집(pending 재생성) |
| POST | `/reviews/<rid>/title` | `review_pick_title` | (v2) 제목 후보 중 선택 → `ai_json.title` 교체 + `edited_text` 비움 |
| POST | `/reviews/<rid>/regenerate` | `review_regenerate` | (P2) 초안 다시 생성(edited_text 비움) |
| POST | `/reviews/<rid>/save-text` | `review_save_text` | (P2) 편집한 네이버 복사본 저장 |
| POST | `/reviews/<rid>/delete` | `review_delete` | 삭제 → 목록 |

- cross-couple rid → 404.

## 8. 내비게이션

`base.html`의 데이트 부모 children에 세 번째 child 추가: `{'label': '후기', 'endpoint': 'reviews'}`.
서브탭: 둘러보기 | 찜 | 후기.

## 9. 단계 구분

- **P1 (완료)**: 폼 + 모델 + 목록 + 상세. **AI 없음.** 단 AI가 P2에서 쓸 모든 것을 저장한다.
- **P2 (완료)**: 백그라운드 AI 생성 + 인앱 웹뷰 미리보기(`/memories/<id>/image` 인라인) + 편집 가능한 네이버 복사텍스트(`[사진 N]` 마커). 상세는 §5.2 참조.
- **v2 (완료 — 노선 2 Step 1)**: 입력 폼 재설계 · 프롬프트 파일 분리·교체 · 출력 스키마 정리 ·
  제목 후보 3개. §10 참조.

## 10. v2에서 바뀐 것 (노선 2 Step 1)

### 10.1 왜

`prose`(느낌점 3~4줄)로 중앙 1,548자 글을 만들면 **AI가 지어낸다.** 조회수가 더 높은 쪽
(gf-blog)의 프롬프트가 가장 강하게 금지하는 것이 바로 그거다. 그래서 *규율을 얹는* 게 아니라
**그쪽 글쓰기를 우리 파이프라인에 태우는** 방향으로 글 만드는 층(폼·프롬프트·스키마)을 교체했다.
배선(워커·상태머신·네이버 업로더·DB)은 그대로 둔다.

### 10.2 입력 폼 재설계 — 각 칸의 원문 근거

근거는 레퍼런스 **A 코호트 맛집 5편**(2026-09-13~10-03)을 역산한 것이다.

| 칸 | 원문 근거(실례) | 몇 편에 |
|---|---|---|
| `visited_when` 언제 | `주말 문래 … 일요일 저녁`, `토요일 오후 3시 반쯤` | 5/5 (도입) |
| `visit_reason` 계기 | `평이 좋다는 이야기를 들어서`, `예전부터 궁금했던` | 5/5 (도입 둘째 문장) |
| `access_note` 가는 길·자리 | `롯데월드몰 6층 식당가 안쪽`, `철공소 골목 안쪽`, `자리는 좌식`, `전용 주차장은 따로 없어요` | 5/5 |
| `waiting` 웨이팅 | `먼저 온 팀이 3팀`, `약 15분 정도 기다린 뒤`, `테이블링 원격 줄서기` | 4/5 (소제목이 되기도) |
| `order_items` 주문·가격 | `갈매기살 2인분과 생맥주`, `신라칼낙새 17,000원 … 총 46,000원`, `인당 16,500원` | 5/5 · 글당 가격 2.8회 |
| `highlight` 기억에 남은 것 | `가장 기억에 남은 조합은 구운 새우젓과 백김치`, `직원분이 직접 구워주셨어요` | 5/5 |
| `downside` 아쉬운 점 | `웨이팅까지 할만큼 인상깊은 맛은 아니었어요`, `남길 확률이 높아요`, `빈자리를 찾기가 어렵더라고요` | 5/5 · `무조건 추천` 0건 |
| `researched` 검색 확인 | `평일 15시부터 … 영업하는 것으로 안내돼 있어`, `매주 월요일은 휴무로 안내돼 있어요` | 4/5 (대개 마지막 단락) |

**넣지 않은 것**(판단해서 쳐냈다): 동행 관계(커플 앱이라 자명), 업종 카테고리(`topic`이 이미
담는다), 지도·주소 블록(§Step 3~4 범위), 키워드 조사 결과(Step 3).

### 10.3 프롬프트

`ai.write_review`에 박혀 있던 프롬프트를 **`prompts/blog-review.md`로 들어냈다.** 바뀐 것은
§5 목록 그대로이고, 큰 줄기는 (1) 표·볼드·강조·이모지 금지, (2) `적정 분량 900~1400자` 삭제,
(3) 경험/조사 어미 구분 도입, (4) `info` 요약표·`faq` 고정 섹션 폐기, (5) answer-first 고정
해제, (6) 제목 후보 3개 + 선정 이유, (7) 작성 후 확인 체크리스트 18항목,
(8) few-shot을 최신 7편 코호트로 한정.

### 10.4 하위 호환 (운영 DB)

- 컬럼 8개는 `run_startup_migrations()`가 nullable로 추가한다 — 기존 행은 NULL.
- 옛 `ai_json`(summary/sections/info_block/ratings/faq)은 `_ensure_blocks`가 `blocks`로
  변환하고, 렌더러가 `info`/`faq`/항목별 `ratings`를 **계속 그린다**. 화면이 죽지 않는다.
- 제목 후보가 없는 옛 후기는 상세에서 후보 카드가 **아예 안 그려진다**.
- 회귀 테스트: `tests/test_blog_review_v2.py` (옛/새 스키마 양쪽 렌더 + 표·볼드 부재 검증).

### 10.5 이번 범위가 아닌 것 (자리만 비워 둠)

- **Step 3 — NAVER API HUB 키워드 조사.** 프롬프트는 사용자가 적어 준 `researched` 칸만 쓰고,
  **스스로 웹 검색을 하지 않는다**(`_run_claude(allow_web=False)`).
- **Step 4 — 썸네일 생성.** Render 무료티어(512MB)에 Playwright/Chromium을 올리는 문제가
  선행 판단이다.


---

## 11. Step 3 — NAVER API HUB 키워드 조사 (노선 2 Step 3)

### 11.1 파이프라인에서의 자리

```
리뷰 저장 → [키워드 조사] → 본문 생성 → 제목 후보
              ↑ 신규 (선택 — 키가 없으면 통째로 건너뛴다)
```

`generate_review` 워커가 `_CAPTION_SEM` 안에서 **조사 → 생성** 순으로 돈다. 조사는 claude를
두 번 쓰고(후보 생성 / 증거 보고 선정), 그 사이에 네이버를 친다.

| 단계 | 무엇 | 어디 |
|---|---|---|
| 1 | 후기가 **직접 답할 수 있는** 롱테일 후보 3~5개 | `ai.suggest_keyword_candidates` + `prompts/keyword-candidates.md` |
| 2 | 후보별 블로그 검색(연관순·최신순) + 트렌드 **52주·8주** | `keyword_research.collect_evidence` → `naver_api` |
| 3 | 메인 1 + 보조 2~4 선정 · 선정/제외 이유 · 한계 기록 | `ai.select_keywords` + `prompts/keyword-select.md` |
| 4 | 조사 결과를 본문 프롬프트에 주입 | `ai.format_keyword_block` → `{{KEYWORD_BLOCK}}` |

조사 결과는 `blog_reviews.research_json`에 저장되고 상세 화면이 **조사 메모 카드**로 보여준다.

### 11.2 호출 형태 (gf-blog `{맛집명}/research/collect.py` 실물에서 확인)

```
base    https://naverapihub.apigw.ntruss.com
headers X-NCP-APIGW-API-KEY-ID / X-NCP-APIGW-API-KEY
GET  /search/v1/blog           query, display=20, sort=sim|date, format=json
POST /search-trend/v1/search   {startDate,endDate,timeUnit,keywordGroups[<=5][keywords<=20]}
                               -> results[].data[].ratio (구간별 상대값, 최대 100)
```

### 11.3 지표 오독 금지 (코드 주석·프롬프트·화면 **세 곳 모두**에 박는다)

- **검색 결과 수(`total`) != 검색량.** 그 말로 쓰인 글 편수일 뿐이다.
- **트렌드 상대지수(`ratio`) != 절대 검색 횟수.** 같이 조회한 그룹 안에서 최댓값을 100으로
  둔 상대값이라 그룹이 바뀌면 숫자도 바뀐다.
- **롱테일 트렌드가 빈 배열 = "수요 없음"이 아니라 "정량 확인 실패".** `parse_trend`가
  0이 아니라 `None`을 돌려주는 이유다(0으로 떨어뜨리면 그 자체가 오독이 된다).
- "긴 검색어라 상위 노출이 쉽다"는 주장 금지 — 근거가 없다.
- 넓은 검색어의 상대 관심도가 높다는 이유만으로 `지역+상호`를 메인으로 되돌리지 않는다.
- 선정 스키마에 **숫자 경쟁 점수 필드가 없다** — 받을 자리를 안 만들어 지어낼 수 없게 한다.
- 제목에 **롱테일 전체를 그대로 넣도록 강제하지 않는다**(gf-blog [검색어와 글의 방향] 7번).
  메인 키워드는 *정보 방향*이고, 제목 규칙은 §5의 것이 그대로 이긴다.

### 11.4 자격증명 — 사용자별 · 서버 저장 · 쓰기 전용 UI

- 컬럼: `users.naver_api_key_id` / `users.naver_api_key` (둘 다 nullable,
  `run_startup_migrations()` §1d가 ADD COLUMN). **두 사람의 키가 다를 수 있어** 커플이
  아니라 사용자에 붙인다.
- **localStorage에 두지 않는다** — 평문으로 남고 XSS에 그대로 노출된다. 이 앱은 로그인
  세션이 있으니 서버에 둔다.
- **컬럼 암호화는 하지 않는다.** 근거: 이 레포의 기존 비밀 취급 방식과 같게 맞춘 것이다
  (OneDrive refresh token도 `settings` 테이블 DB 평문이고 앱에 암호화 계층이 없다).
  새로 들어온 더 약한 비밀 하나만 암호화하면 **더 센 비밀이 평문인 채 남아 실효는 없고**,
  `SECRET_KEY` 교체 시 복호 불가라는 운영 위험만 는다. 암호화를 한다면 *모든 비밀에 한 번에*
  적용하는 별도 작업이어야 한다(그때 바꿀 자리는 이 두 컬럼 + `onedrive._SETTING_KEY`).
- 실질적인 노출 차단은 **값을 다시 내보내지 않는 것**으로 한다:
  - 설정 화면은 `설정됨 / 미설정`만 그린다. 라우트가 템플릿에 넘기는 건 **불리언뿐**이다.
  - 입력은 `type="password"` + `autocomplete=off`. 한 칸만 채우면 **그 칸만** 갱신한다.
  - `naver_api.NaverApiError`는 상태코드와 네이버가 준 짧은 메시지만 담는다 — 요청 헤더를
    실어 나르지 않아 스택트레이스에 키가 찍히지 않는다.
  - 조사 메모(`research_json`)·프롬프트·flash 메시지 어디에도 값이 들어가지 않는다
    (회귀 테스트가 이걸 문자열 부재로 검증한다).
- 어느 키를 쓰나: **작성자 키 우선 → 없으면 같은 커플의 파트너 키**. 누구 키로 조사했는지는
  메모에 이름으로 남는다(`key_owner`).
- `연결 확인` 버튼 = `display=1` 블로그 검색 1회(쿼터를 거의 안 쓴다).

### 11.5 키가 없을 때 — **죽지 않는다**

`keyword_research.research()`가 `{"status": "skipped", "reason": "no_credentials"}`를 돌려주고
**claude도 부르지 않는다.** 본문 생성은 지금까지와 똑같이 돌고, 프롬프트에는
`ai.NO_KEYWORD_BLOCK`("조사를 하지 않았다 — 검색량·경쟁·노출 언급 금지")이 들어간다.
상세 화면에는 "설정에서 키를 넣으면 조사가 켜진다" 안내 카드가 뜬다.
조사가 실패했을 때(`status: "failed"`)도 같다 — 생성은 진행되고 화면이 실패를 알린다.

### 11.6 테스트

`tests/test_keyword_research.py` — **네트워크를 타지 않는다.** 실제 응답은
`tests/fixtures/naver_blog_search.json` · `naver_trend_52w.json` · `naver_trend_8w.json`에
저장했고(픽스처에 키가 없다는 것도 테스트한다), 파싱·선정·프롬프트·키 취급·화면을 검증한다.
롱테일 트렌드가 빈 배열인 실제 케이스가 픽스처에 들어 있다.
