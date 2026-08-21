# 데이트 후기 → 네이버 블로그 포스트 자동생성 — 스펙 (durable blueprint)

> 이 문서는 세션이 끊겨도 다음 세션이 이어받을 수 있게 합의된 스펙을 박제한 것이다.
> 코드와 함께 커밋한다. **P1(완료)** = 폼 + 모델 + 목록/상세. **P2(완료)** = AI 생성 + 인앱 웹뷰 미리보기 + 네이버 복사텍스트.

## 1. 기능 개요

커플이 데이트를 다녀온 뒤, **주제/장소 · 위치 · 느낀점(산문) · 반쪽별 별점 · 추억 사진(순서)** 을 입력하면,
그 원문을 재료로 **네이버 블로그에 그대로 올릴 수 있는 SEO/AEO 최적화 포스트**를 AI가 자동 생성해 주는 기능이다.

- AI 메커니즘은 앱 전체 제약에 따라 **`claude` CLI 백엔드 서브프로세스**다 (Anthropic API/SDK 금지 — `ai.py`).
- AI는 **요청 경로에서 동기로 돌지 않는다** — 백그라운드 스레드에서만 (앱 절대 제약).

## 2. 플로우

```
[작성 폼]  주제/장소 · 위치 · 반쪽별 별점(0~10 드래그) · 추억 사진 선택+순서 · 느낀점(산문)
   │  저장 (P1: status='draft')
   ▼
[백그라운드 AI]  (P2)  claude로 SEO/AEO 포스트 생성 → ai_json 저장, status 'pending'→'ready'/'failed'
   │
   ▼
[상세]  인앱 웹뷰 미리보기(이미지 인라인) + 편집 가능한 네이버 복사텍스트([사진 N] 마커)
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
| `prose` | Text, not-null | 느낀점·특징 3~4줄 원문 |
| `overall_score` | Integer, not-null | 0~10 (반쪽별 드래그 합계) |
| `photo_ids` | Text, nullable | JSON list[int] — 선택한 Photo id, **순서 유지** |
| `ai_json` | Text, nullable | P2 결과(구조화 JSON) |
| `edited_text` | Text, nullable | P2 사용자가 편집한 복사텍스트 |
| `status` | String(16), not-null, default `'draft'` | 'draft'=생성됨·AI 미실행. P2에서 'pending'/'ready'/'failed' |
| `created_at` / `updated_at` | DateTime | |

- `@property photos_ordered` → `photo_ids` 순서대로 이 커플의 Photo 행을 돌려준다 (JSON 디코드 → 조회 → 순서 유지 → 없는 id 스킵).
- Brand-new 테이블 — `db.create_all()`가 만든다 (ALTER 마이그레이션 불필요).

## 4. P2 AI 출력 JSON 형태 (`ai_json`에 저장 — 최종형)

`ai.write_review(topic, location, prose, overall_score, photos) -> dict|None`이
`claude`를 **한 번** 호출해 만든다(`_normalize_review`로 정규화). `photos` 인자는
`{"index": i, "caption": <그 사진 AI 캡션>, "tags": [..]}`의 **순서 리스트**.

```json
{
  "title": "검색 의도를 담은 포스트 제목",
  "summary": "answer-first 요약 — 한줄평 + 총점 결론을 맨 앞에",
  "info_block": [ { "label": "위치", "value": "..." } ],
  "sections": [ { "heading": "검색어형 소제목", "text": "본문 단락", "photo_index": 0 } ],
  "ratings": [ { "aspect": "분위기", "score": 8 } ],
  "faq": [ { "q": "자주 묻는 질문", "a": "답변" } ],
  "hashtags": ["#장소명", "#데이트"]
}
```

- `info_block`은 **구조화 리스트**(P1 스펙의 문자열에서 확정) — 훑기 쉬운 label:value.
- `sections[].photo_index` = `photos` 순서 리스트의 0-based 인덱스(그 단락에 붙일 사진).
- **정규화 규칙**: 모든 문자열 `.strip()`; 모든 score 0..10 클램프; `photo_index`는
  `0..len(photos)-1` 범위일 때만 유지(아니면 null); `ratings`≤6·`faq`≤4·`hashtags`≤10;
  빈 항목 제거; 해시태그는 `#` 없으면 자동 부착. `title`·`summary`·`sections`(≥1) 없으면
  `None`(→ 워커가 실패 처리). `overall_score`는 AI가 아니라 **사용자 값**을 표시에 그대로 쓴다.

## 5. 네이버-AI 최적화 규칙 (P2 프롬프트가 인코딩하는 것 — 핵심)

타깃 = **네이버 통합검색 / AI 브리핑(하이퍼클로바X 기반)**. 생성 글은 네이버 AI가
**인용**하고 네이버의 **실제 경험 필터**를 통과하도록 구성한다.

- **진짜 경험 후기 톤**: 네이버 AI는 광고성·AI 티 나는 일반론을 적극 하위 노출시킨다.
  모든 서술을 사용자 `prose` + 실제 사진 `caption`에 근거해 구체적·1인칭·다녀온 느낌으로.
- **No-fabrication**: 사용자가 주지 않은 사실(특히 **가격·영업시간·정확한 메뉴**)은 지어내지
  않는다 — 모르면 빼거나 "방문 시 확인". 과장된 마케팅 톤·상투어 금지.
- **Answer-first**: `summary`가 한줄평 + 총점 결론을 맨 앞에.
- **검색어형 소제목**: 사람들이 검색하는 말투(예: "○○동 △△카페 위치·가는 길",
  "분위기·인테리어", "메뉴·가격", "데이트 코스로 어때?", "총평").
- **스캔 가능한 info_block**: 위치/가격대/이런 점 좋아요 등 label:value, 아는 것만.
- **FAQ 2~3개**: 질문형이 AI 인용에 강함 — 현실적 데이트 후기 질문.
- **명시 별점**: 총점 + 맥락형 항목별 3~5개(분위기/맛·메뉴/가성비/데이트적합도/재방문의향 등).
- **적정 분량**: sections 본문 합계 대략 900~1400자, 각 사진 한 줄 설명(캡션 활용),
  검색어형 해시태그.

## 5.1 네이버 복사텍스트 포맷 (`_review_copy_text(review) -> str`)

이미지는 네이버에 붙여넣을 수 없으므로 사진 자리에 `[사진 N]` 마커(사진 순서대로
1부터)를 넣은 **플레인 텍스트**를 만든다. `edited_text`가 있으면 그것을, 없으면 이
함수 출력을 편집 textarea에 시드한다. 형태:

```text
<title>

<summary>
⭐ 총점 ★★★★☆ (8/10)

▶ 핵심 정보
· <label>: <value>

<section heading>
<section text>
[사진 1] — <그 사진 캡션>

⭐ 별점
· <aspect> ★★★★☆ (8/10)

❓ 자주 묻는 질문
Q. ...
A. ...

<#hashtag #hashtag ...>
```

- 별 문자열(`_star_bar`): 꽉 찬 별=2점, 홀수는 반쪽 `⯪`, 나머지 `☆`(5칸). 옆에 `(N/10)` 병기.
- 사진 마커는 그 섹션의 `photo_index`가 가리키는 순서 위치에 놓는다(캡션 없으면 마커만).

## 5.2 백그라운드 워커 · 라우트 (P2)

- `generate_review(app, review_id)` — 데몬 스레드, review_id 가드로 중복 차단, 자체
  `app_context`, `_CAPTION_SEM`으로 `ai.write_review` 직렬화(512MB: 동시에 claude 하나).
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

### 나머지 필드
- 주제/장소 text (필수), 위치 text (선택), 느낀점·특징 textarea (필수, ~3-4줄).
- Submit "저장" (P2에서 "등록 → AI 초안 생성"으로 바뀐다).
- 유효성 오류 시 입력값 + 선택 사진 유지하며 재렌더.

## 7. 라우트 (모두 `@active_couple_required`, 커플 스코프)

| 메서드 | 경로 | endpoint | 역할 |
|---|---|---|---|
| GET | `/reviews` | `reviews` | 이 커플 후기 목록(최신순). 카드: 주제·위치·별점·작성일·상태힌트. 빈 상태. + 새 후기 FAB |
| GET,POST | `/reviews/new` | `review_new` | 작성 폼 / 저장(P2: status='pending' + 초안 생성 스폰) |
| GET | `/reviews/<rid>` | `review_detail` | 후기 표시 + (P2) 웹뷰 미리보기·네이버 복사본 |
| GET,POST | `/reviews/<rid>/edit` | `review_edit` | 주제/위치/산문/별점/사진 편집(P2: pending 재생성) |
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
