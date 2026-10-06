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
| POST | `/reviews/<rid>/thumbnail` | `review_thumbnail_generate` | (Step 4) 썸네일 카피 (재)생성 — 백그라운드 |
| POST | `/reviews/<rid>/thumbnail/state` | `review_thumbnail_state` | (Step 4) 고른 후보·줄인 문구·쓸 사진·**렌더 측정값** 저장(서버가 넘침 재판정) |
| POST | `/reviews/<rid>/gif/state` | `review_gif_state` | (Step 5) 고른 영상·구간·프리셋 + **브라우저가 보고한 결과/진단** 저장(서버가 용량 재판정) |
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
- ~~**Step 4 — 썸네일 생성.**~~ → **§12에서 완료.** Chromium을 서버에 올리지 않고
  사용자의 브라우저가 `template.css` 수치 그대로 렌더한다.


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

---

## 12. Step 4 — 썸네일 (노선 2 Step 4)

### 12.1 선행 판단 — 서버에 Chromium 을 올리지 않는다

gf-blog 는 **Playwright + Chrome headless** 로 썸네일을 렌더한다
(`{맛집명}/thumbnail/render.py`). 우리는 Render 무료티어(512MB · 0.1 CPU)이고,
사용자가 **Chromium 을 서버에 올리는 것을 명시적으로 배제**했다. 그래서 세 경로를
실측하고 골랐다.

| 경로 | 실측 | 판정 |
|---|---|---|
| (a) 앱의 기존 미리보기 재사용 | 기존 미리보기는 **네이버 본문 HTML** 을 그리는 것이라 1080×1350 썸네일을 내지 않는다 | 산출물로는 ✗ — 다만 **그 미리보기를 그리는 주체(사용자의 브라우저)** 가 (c)의 실행 장소다 |
| (b) Pillow 서버 렌더 | Pillow 는 설치돼 있지만 **한글을 못 그린다** — `python:3.12-slim` 에 폰트가 없고(Dockerfile 이 `fonts-*` 를 설치하지 않는다) Pillow 기본 폰트는 한글을 전부 같은 `.notdef` 로 찍는다(서로 다른 한글 문자열의 래스터가 **바이트 단위로 동일**함을 확인). 쓰려면 Pretendard 3종(≈3.5MB)을 레포·이미지에 싣고, CSS 레이아웃(반행간·flex 정렬·그라데이션)을 Pillow 로 **다시 구현**해야 한다 — 검증된 수치에서 소리 없이 어긋날 자리가 생긴다. 합성 자체는 1080×1350 에 0.13초(로컬) | ✗ |
| **(c) 브라우저 렌더 (채택)** | DOM 이 `design/thumbnail-template.css` **선언 그대로** 1080×1350 을 잡고 → 진짜 브라우저가 레이아웃 → `scrollWidth<=clientWidth` 로 넘침 판정 → Canvas 2D 가 같은 수치로 래스터. **서버 CPU 0, 서버 메모리 0, 폰트 문제 없음**, 그리고 넘침 검증이 gf-blog 의 assert 와 **같은 식 그대로** 남는다 | ✓ |

(c)는 "AI 이미지 생성 금지"도 그대로 지킨다 — **배경은 후기에 올린 실제 사진**이고
그 위에 타이포만 얹는 **합성**이다. 외부 이미지 생성 모델을 부르지 않는다.

### 12.2 디자인 수치 — `design/thumbnail-template.css` 가 정본

gf-blog `썸네일/template.css`(Figma 내보내기, 선택자 없는 선언 블록)를 **그대로** 들여왔다.
`thumbnail.py` 가 gf-blog `render.py` 와 **같은 알고리즘**(`position:` 등장 위치로 쪼개고
배지는 `display: flex;`·`width: 270px;` 에서 한 번 더 가른다)으로 파싱한다 —
**선택자만 붙이고 값은 보존한다.** 숫자를 코드에 다시 적지 않으므로 어긋날 수 없다.

| 영역 | 좌표·크기 | 타이포 |
|---|---|---|
| 캔버스 | 1080×1350 | — |
| 그라데이션 | 전면 | `linear-gradient(180deg, rgba(217,217,217,0) 0%, rgba(17,17,17,0.7) 81.73%)` |
| 메인(2줄) | (90, 935) 900×300 | Pretendard 700 · 95px / 113px · `#FFFFFF` |
| 서브(1줄) | (90, 1181) 578×54 | Pretendard 500 · 45px / 54px · `#FFFFFF` |
| 배지 | (90, 846) 307×70 · padding 10 · radius 999 | 배경 `#D7FFFC` |
| 배지 라벨 | 270×43 (배지 안 flex 중앙) | Pretendard 600 · 36px / 43px · `#000000` |

`tests/test_thumbnail.py` 가 이 표의 숫자를 **하나하나** 못박는다. 박스 기준은 gf-blog
`render.py` 와 같은 `box-sizing: border-box` 다.

### 12.3 배경 — 후기에 올린 **첫 사진**

- 기본 배경 = `review.photos_ordered[0]`(사용자가 고른 순서의 첫 사진). 화면에서 다른
  사진으로 바꿀 수 있고 선택은 `thumbnail_json.photo_index` 에 남는다.
- 바이트는 **앱이 이미 쓰는 경로** 그대로다 — OneDrive 뒤의 서명 URL `/blog-img/<id>`
  에 `&hq=1`(원본 해상도. 1280px 다운스케일본을 1080 캔버스에 늘리면 흐려진다).
  **새 업로드 경로를 만들지 않는다.**
- 사진은 `object-fit: cover`(중앙) 로 캔버스를 채운다.
- **가독성 처리 = 위 그라데이션**이다. 사진 위에 아래로 갈수록 짙어지는 어두운 층이
  깔려 흰 글자가 읽힌다(메인이 시작되는 y=935 에서 오버레이 불투명도 ≈0.59).
  템플릿에 없는 그림자를 새로 더하지 않는다 — 검증된 디자인은 이 조합이다.
- **사진이 없는 후기**는 그라데이션 끝 색(`rgb(17,17,17)`) 단색을 배경으로 쓴다.
  없는 그림을 지어내지 않는다.

### 12.4 넘침 검증 — 재서 판정하고, 폰트가 아니라 문구를 줄인다

gf-blog `render.py` 의 두 assert 를 **같은 식으로** 유지한다:

1. `document.fonts.check('<weight> <size>px Pretendard')` — 폰트가 안 떴으면 **렌더를
   거부한다.** 다른 폰트로 그리면 검증된 디자인이 아니다.
2. 메인 · 서브 · 배지 라벨 세 상자에서 `scrollWidth <= clientWidth`.
   미리보기는 `transform: scale()` 로만 줄이므로(레이아웃 크기 불변) 이 값은 **1080 기준
   실측값**이다.

그리고 **판정은 서버가 다시 한다**(`thumbnail.evaluate_render_check`). 브라우저가 보낸
`ok` 주장은 읽지 않고 상자별 측정값만 받아 재계산한다 — 클레임이 아니라 측정이 근거다.
결과는 `thumbnail_json.render_check` 에 좌표·폰트·넘침과 함께 남는다(gf-blog
`render-check.json` 대응).

⛔ **넘치면 글자 크기를 줄이지 않는다.** 내려받기 버튼이 잠기고, 사람이 문구를 줄이거나
더 짧은 후보를 고른다. 그래서 `static/thumbnail.js` 에도 `thumbnail.py` 스키마에도
**글자 크기를 바꾸는 길이 없다**(회귀 테스트가 그 부재를 검증한다).

### 12.5 카피 — claude 가 쓰고, **초안 생성 경로에 끼지 않는다**

```
초안 생성(기존)  : 키워드 조사 → 본문 생성 → 제목 후보      ← 여기에 아무것도 안 더했다
썸네일(신규)     : 사람이 상세 화면에서 요청 → 카피 후보 3개 → 선정 → 브라우저 렌더
```

썸네일 카피는 **글이 완성되고 제목이 확정된 뒤**에 뽑는 것이라 생성 파이프라인에 넣을
이유가 없다. 이미 Render 0.1 CPU 에서 ~170초인 초안 생성에 **claude 호출을 하나도 더
얹지 않는다**(회귀 테스트가 `generate_review` 경로에서 썸네일이 안 불린다는 것을 못박는다).
요청 시에만 `generate_thumbnail_copy` 워커가 `_CAPTION_SEM` 안에서 한 번 돈다.

규율 정본은 gf-blog `naver-blog-prompt.md` **[썸네일 카피 생성] 1~6·8번**이고
`prompts/thumbnail-copy.md` 에 옮겼다: 후기에 없는 것 금지 / `~~한 000`·`~~할 때 가기
좋은 000` 형식 / 궁금증 유도만으로 끝내지 않기 / **후보 3개 중 1개 선정 + 이유** /
과장·판매 문구 금지 / 메인·서브·배지가 서로 다른 것을 말하기. 7·9번(렌더링 방법·소재
폴더 저장)은 앱이 하므로 프롬프트에 넣지 않는다.

상자별 **대략의 글자 예산**(메인 9 / 서브 12 / 배지 7 — 상자 폭 ÷ 폰트 크기)이
프롬프트에 안내로 들어가고, 후보 하나는 **일부러 더 짧게** 만들게 한다(넘쳤을 때 바로
고를 안전 후보). 그 숫자는 판정이 아니다 — 판정은 언제나 브라우저 실측이다.

### 12.6 데이터·엔드포인트

| | |
|---|---|
| 컬럼 | `blog_reviews.thumbnail_json` (TEXT, nullable — `run_startup_migrations()` 가 ADD COLUMN) |
| 내용 | `candidates[3]` · `picked` · `pick_reason` · `copy`(사람이 줄인 최종 문구) · `photo_index` · `render_check` · `status` |
| `POST /reviews/<rid>/thumbnail` | 카피 (재)생성 — 백그라운드 스폰. 초안이 `ready` 일 때만 |
| `POST /reviews/<rid>/thumbnail/state` | 고른 후보 · 줄인 문구 · 쓸 사진 · **측정값** 저장(서버가 넘침 재판정) |

⚠️ 템플릿에서 `thumb.copy` 로 쓰면 **Jinja 가 dict 의 `copy()` 메서드**를 먼저 집어
(truthy) 편집칸이 전부 빈 채로 렌더된다. 반드시 `thumb['copy']` 로 읽는다.

### 12.7 테스트

`tests/test_thumbnail.py` — 네트워크·`claude` 없이 돈다. 템플릿 수치 보존 · 카피 정규화 ·
넘침 재판정(클라이언트 주장 무시) · **초안 생성 경로 격리** · 라우트 커플 스코프 ·
화면 렌더 · 렌더러에 폰트 축소 코드가 없음을 검증한다.

## 13. Step 5 — 동영상 → GIF (노선 2 Step 5)

네이버 맛집 글에 넣을 **움직이는 한 컷**을 만든다. gf-blog 에는 이 기능이 없다
(MOV 에서 '대표 프레임' 정지 이미지를 뽑는 언급만 있고 ffmpeg 도 GIF 코드도 없다) —
**가져올 자산 없이 새로 만든 것**이다. 썸네일(§12)과 같은 철학으로 간다.

### 13.1 선행 판단 — 서버에 ffmpeg 를 올리지 않는다

| | |
|---|---|
| 프로덕션 | Render 무료티어 **512MB · 0.1 CPU** |
| 베이스 이미지 | `python:3.12-slim` — **ffmpeg 없음** |
| 설치하면 | 이미지가 수백 MB 불고, 0.1 CPU 에서 480p 3초 인코딩도 요청 타임아웃을 넘는다 |

→ **변환은 브라우저가 한다.** 썸네일이 Chromium 대신 사용자의 브라우저에 DOM+Canvas
렌더를 맡긴 것과 **같은 판단**이고, 브라우저에는 이미 OS 하드웨어 디코더가 있으니
공짜로 쓰는 게 맞다. 서버가 하는 일은 세 가지뿐이다:

1. 프리셋·한도를 **단일 원천**으로 내려보낸다 (`gifmaker.spec()`).
2. 원본 바이트를 **Range(206)로 흘린다** (`/videos/<id>/stream`).
3. 브라우저가 보고한 결과를 **다시 판정한다** (`gifmaker.evaluate_result`).

회귀 테스트가 이 판단을 못박는다 — `gifmaker.py`·`app.py`·`onedrive.py` 어디에도
ffmpeg 를 실행하는 코드가 없고 `Dockerfile`·`requirements.txt` 도 설치하지 않는다.

### 13.2 영상 바이트를 브라우저에 어떻게 넘기나 — (a) 직접 vs (b) 프록시

| | (a) OneDrive 직접 | (b) 앱 프록시 |
|---|---|---|
| 비용 | 서버 대역폭 0 (Microsoft CDN) | 서버가 중계 |
| Canvas 픽셀 읽기 | **CDN 이 `Access-Control-Allow-Origin` 을 줘야 한다.** 없으면 캔버스가 오염돼 프레임 추출이 *아예* 막힌다 | same-origin 이라 taint 없음 |
| seek | 다운로드 URL 이 206 을 줘야 한다 | 우리가 Range 를 중계한다 |
| 자격증명 | 다운로드 URL은 쿼리에 토큰이 박힌 **자격증명**이다 — DOM 에 흘러간다 | 노출 없음 |

**주장하지 않고 잰다.** `/videos/<id>/source` 가 `onedrive.probe_direct_cors` 로 실제
다운로드 URL 에 `Origin` 을 붙여 1바이트를 받아 보고, **ACAO 가 있고 206 도 되면 (a)**,
아니면 **(b)** 를 고른다. 결과는 `videos.cors_probe` 에 남아 영상당 한 번만 잰다.
ACAO 가 없으면 **다운로드 URL 을 내보내지 않는다** — 쓰지도 못할 자격증명을 흘릴
이유가 없다. 이 리포에 이미 기록된 실측("personal-OneDrive pre-auth 다운로드 URL 은
거의 즉시 만료된다" — `onedrive.py` `_bytes_cache` 주석)도 (b) 쪽을 가리킨다.

⚠️ 이 앱의 이미지 프록시에는 **Range 처리가 없었다**(206 응답 0건). 영상은 seek 이
필수라 그대로 쓸 수 없어 `/videos/<id>/stream` 을 새로 열었다. Range 헤더는 **그대로
OneDrive 로 전달**하고 돌아온 206 을 중계한다 — 서버가 구간을 직접 자르지 않으므로
파싱 버그로 엉뚱한 바이트를 줄 일이 없고, 전체를 받아 쪼갤 필요도 없다. 응답은
제너레이터라 **한 번에 256KB만** 메모리에 있다.

### 13.3 보관 — 사진과 같은 OneDrive, 다른 테이블

영상은 버리지 않고 OneDrive `couple-daily` 폴더에 보관한다(사용자 결정). 다만
`photos` 가 아니라 **`videos` 테이블**이다 — 사진 경로에는 자동 캡션(vision)·EXIF·
블로그 서명 URL·썸네일 프록시가 줄줄이 달려 있고, 0.1 CPU 워커에 50MB 영상을 먹이는
길을 아예 만들지 않는 게 요점이다. 브랜드-뉴 테이블이라 `db.create_all()` 이 만든다
(ALTER 마이그레이션은 `blog_reviews.gif_json` 하나뿐).

업로드는 **청크 스트림**이다(`onedrive.upload_stream` — Graph 업로드 세션, 3.2MiB
청크). 사진처럼 `file.read()` 로 통째로 읽지 않으므로 50MB 영상을 올려도 워커 메모리는
평평하다.

### 13.4 디코딩은 2단 — 네이티브 우선, 실패하면 그때 WASM

브라우저는 자체 코덱이 아니라 **OS 코덱을 빌려 쓴다.** 아이폰 MOV(HEVC) 기준:

| 기기 | 코덱 출처 | 타는 경로 |
|---|---|---|
| 아이폰 Safari | iOS 네이티브 HEVC | ① 네이티브 |
| 맥북 | macOS 시스템 HEVC | ① 네이티브 |
| 갤럭시 (Android Chrome) | 플랫폼 하드웨어 디코더 | ① 네이티브 |
| 윈도우 데스크톱 | **HEVC Video Extensions 가 깔려 있어야 한다** | 있으면 ①, 없으면 ② |

즉 **구멍은 윈도우 한 경우뿐**이다. 그래서 ②(ffmpeg.wasm 코어, 약 32MB)는 기본 경로가
아니라 **폴백**이고, ①이 실패한 것이 *확인된 뒤에만* 로드한다(lazy). 되는 기기에
32MB 다운로드와 느린 디코딩을 물리는 건 손해다. 위 표는 분기 설명이고, **실제 판정은
`static/gif.js` 의 진단 코드가 그 기기에서 직접 한다.**

* ① 네이티브 — `<video>` seek → Canvas `drawImage` → `getImageData`.
* ② 폴백 — `@ffmpeg/core` **ESM 빌드를 우리 모듈 워커에서 직접 import** 해 rawvideo
  RGBA 프레임을 뽑는다. 감싸개(`@ffmpeg/ffmpeg`)는 쓰지 않는다(§13.7 실측 참조).

**GIF 인코더는 두 경로가 공유한다** — 그래야 결과가 같다. 인코더(`gifenc`)는 *주 경로*라
CDN 이 아니라 `static/vendor/` 에 사본으로 둔다(테스트가 sha256 으로 무결성을 본다).
폴백 코어 32MB 는 담을 수 없어 CDN 이고, **못 받으면 조용히 깨지는 대신 기능을 막고
말한다**(`loader-failed`) — 썸네일이 Pretendard 를 못 받으면 렌더를 거부한 것과 같다.

### 13.5 조용한 실패를 만들지 않는다

이 기능의 최악의 실패 모드는 **에러 없이 빈 프레임**이다. 그래서 모든 실패에 이름이
있고(`gifmaker.DIAG_CODES`) 화면이 그 이름으로 말하며, 상태에 저장된다:

| 코드 | 화면이 하는 말 |
|---|---|
| `native-ok` | 이 기기가 바로 디코딩해 — 빠른 경로 |
| `wasm-ok` | 네이티브로 안 돼서 브라우저 안 디코더로 만들어. 처음 한 번 약 32MB, 더 느려 |
| `decode-failed` | 이 기기에서는 이 영상을 디코딩할 수 없어 |
| `tainted` | 픽셀을 읽을 수 없어 (CORS) |
| `blank-frames` | 디코더가 에러 없이 빈 화면만 내고 있어 |
| `loader-failed` | 폴백 디코더를 내려받지 못했어 — GIF 만들기를 막았어 |
| `no-video` | 고를 영상이 없어 |

판정은 **재서** 한다: 메타데이터 로드 성공 여부 · `video.error.code` · `getImageData`
가 던지는가 · 뽑은 프레임이 전부 단색인가(`isUniform`). 32MB 다운로드와 느린 디코딩은
진행률 없이는 멈춘 것처럼 보이므로 진행 바와 단계별 문구를 함께 낸다.

### 13.6 용량 가드 — 넘치면 **막는다. 몰래 깎지 않는다**

GIF 는 프레임마다 256색이라 480p 3초가 수 MB까지 간다. 썸네일의 넘침 검증과 **같은
철학**이다:

* 한도 `MAX_GIF_BYTES = 8MB`. 넘으면 **내려받기를 막고** "길이를 줄여줘"라고 말한다.
* **자동으로 색·fps·크기를 낮추는 경로가 코드에 없다.** 줄이는 건 사람이고, 프리셋을
  낮추는 것도 사람이 고른다.
* 만들기 전에는 *예상* 용량을 보여 주되(화면·서버가 같은 계수를 쓴다), **판정은 언제나
  실측**이다 — 인코딩이 끝난 실제 바이트 수로 가른다.
* 클라이언트가 보낸 `ok` 주장은 읽지 않는다. 서버가 `bytes <= MAX_GIF_BYTES` 를 다시
  계산한다(`gifmaker.evaluate_result`).

길이 상한은 `MAX_DURATION = 6초`다. 폴백 디코딩이 느려도 견딜 만한 범위와 정합한다.

### 13.7 실측으로 바로잡은 것 (되밟지 말 것)

| 증상 | 원인 | 조치 |
|---|---|---|
| `Cannot find module 'blob:…'` | `@ffmpeg/ffmpeg` UMD 는 워커를 webpack 청크로 띄운다. 크로스 오리진 Worker 를 피해 blob 으로 넘기면 `classWorkerURL` 이 **모듈** 워커를 만드는데, 그 안의 코드는 `importScripts` 를 쓴다(모듈 워커엔 없다) | 감싸개를 버리고 **코어 ESM 을 우리 모듈 워커에서 직접 import** |
| 20프레임 GIF 가 212KB → **4.6MB** | `gifenc` 는 타입드 배열의 `byteOffset/length` 를 보지 않고 **밑의 ArrayBuffer 전체**를 읽는다. WASM 경로 프레임은 한 덩어리 raw 버퍼의 *뷰* 라 프레임마다 영상 전체가 들어갔다 | 두 경로가 만나는 `encode()` 에서 자기 버퍼를 가진 사본으로 정규화 |
| 12장이어야 할 것이 **4장** | `-ss` 를 `-i` 앞에 두면 빠른(키프레임) 탐색이고, 컨테이너 duration 이 실제보다 짧게 적힌 파일에서 그 길이에 맞춰 잘린다 | `-ss` 를 `-i` **뒤**로(정확 탐색). 몇 초 클립이라 비용이 작다 |
| 백그라운드 탭에서 "굽는 중… 20/20" 에 영원히 멈춤 | 크롬이 숨은 탭의 `setTimeout` 을 1초→1분으로 조인다 | 프레임 사이 양보를 **MessageChannel** 로 |
| 네이티브 136 vs 폴백 134 | 우리는 짝수로 내렸고 ffmpeg `scale=W:-2` 는 가장 가까운 짝수로 반올림한다 | 양쪽 다 **가장 가까운 짝수** (`gifmaker.target_height`·`targetSize`) |
| 네이티브가 메타데이터조차 못 읽으면 폴백도 못 돌았다 | 목표 크기를 `<video>` 해상도에서만 구했다 | 모르면 `scale=W:-2` 가 높이를 정하고 **로그에서 읽는다** — 폴백이 꼭 필요한 기기에서만 죽던 구멍 |

### 13.8 데이터·엔드포인트

| | |
|---|---|
| 테이블 | `videos` (brand-new — `db.create_all()`). OneDrive item id + 이름 · 바이트 수 · MIME · `cors_probe`(실측 기록) |
| 컬럼 | `blog_reviews.gif_json` (TEXT, nullable — `run_startup_migrations()` 가 ADD COLUMN) |
| 내용 | `settings`(video_id·start·duration·width·fps) · `result`(bytes·width·height·frames·fps·duration·path·ok·over_by) · `diag` · `status` |
| `POST /videos/upload` | 영상 1개를 **청크 스트림**으로 OneDrive 에 올리고 행 생성 (AJAX) |
| `GET /videos/<id>/stream` | **Range(206)** 중계. same-origin 이라 Canvas taint 없음 |
| `GET /videos/<id>/source` | 바이트 경로 (a)/(b) 판정 — 실측 후 행에 캐시 |
| `POST /videos/<id>/delete` | OneDrive 에서 삭제 + 행 삭제 |
| `POST /reviews/<rid>/gif/state` | 고른 설정 · **브라우저가 보고한 결과/진단** 저장(서버가 용량 재판정) |

설정이 바뀌면 직전 결과는 버린다 — 옛 용량으로 "통과"라고 말하지 않게
(썸네일에서 문구가 바뀌면 `render_check` 를 비운 것과 같은 결).

**네이버 발행 연계는 이번 범위가 아니다.** GIF 는 사용자 기기로 내려받는 데까지다
(썸네일과 동일). 구조는 막지 않았다 — 만든 GIF 는 Blob 이고, 나중에 `/blog-img` 처럼
업로드·서명 URL 경로를 붙이면 된다.

### 13.9 테스트

`tests/test_gif.py` — 네트워크·`claude` 없이 돈다. 서버 ffmpeg 부재 · 벤더 인코더
무결성(sha256) · 네이티브 우선 + lazy 폴백 · 프리셋 강제 · 용량 가드(클라이언트 주장
무시) · 진단 코드 화이트리스트 · 업로드 스트리밍(바이트 통째 읽기 금지) · Range 중계와
소켓 회수 · 커플 스코프 · (a)/(b) 실측 분기와 **서명 URL 비노출** · **초안 생성 경로
격리**를 검증한다.
