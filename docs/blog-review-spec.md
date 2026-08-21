# 데이트 후기 → 네이버 블로그 포스트 자동생성 — 스펙 (durable blueprint)

> 이 문서는 세션이 끊겨도 다음 세션이 이어받을 수 있게 합의된 스펙을 박제한 것이다.
> 코드와 함께 커밋한다. **P1(현재)** = 폼 + 모델 + 목록/상세. **P2(다음)** = AI 생성 + 인앱 웹뷰 미리보기 + 네이버 복사텍스트.

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

## 4. P2 AI 출력 JSON 형태 (`ai_json`에 저장)

```json
{
  "title": "포스트 제목 (검색 의도를 담은)",
  "summary": "답변 우선(answer-first) 요약 — 첫 ~200자 안에 핵심 결론",
  "info_block": "장소·위치·비용·소요시간 등 스캔 가능한 정보 블록(구조화 텍스트)",
  "sections": [
    { "heading": "검색 의도 기반 소제목", "text": "본문 단락", "photo_index": 0 }
  ],
  "ratings": [
    { "aspect": "분위기", "score": 8 }
  ],
  "faq": [
    { "q": "자주 묻는 질문", "a": "답변" }
  ],
  "hashtags": ["#데이트", "#장소명"]
}
```

- `sections[].photo_index` = `photo_ids` 순서 리스트에 대한 0-based 인덱스 (그 단락에 붙일 사진).

## 5. SEO / AEO 규칙 (P2 프롬프트가 지켜야 할 것)

- **Answer-first summary**: 첫 ~200자 안에 핵심 결론/요약을 먼저 준다.
- **Intent-based headings**: 소제목은 검색 의도(“가볼만해?”, “주차 되나?” 등) 기반.
- **Scannable info block**: 장소·위치·비용·시간을 훑기 쉬운 블록으로.
- **FAQ**: 자주 묻는 질문 섹션 (AEO — 음성/AI 답변 최적화).
- **Explicit ratings**: 측면별 점수를 명시(분위기/가성비/재방문의사 등).
- **First-hand tone**: 실제 다녀온 1인칭 경험 톤.
- **Hashtags**: 네이버 블로그 해시태그.

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
| GET,POST | `/reviews/new` | `review_new` | 작성 폼 / 저장(status='draft', created_by=me) |
| GET | `/reviews/<rid>` | `review_detail` | 저장된 후기 표시 + placeholder 카드 |
| GET,POST | `/reviews/<rid>/edit` | `review_edit` | 주제/위치/산문/별점/사진 편집 |
| POST | `/reviews/<rid>/delete` | `review_delete` | 삭제 → 목록 |

- cross-couple rid → 404.

## 8. 내비게이션

`base.html`의 데이트 부모 children에 세 번째 child 추가: `{'label': '후기', 'endpoint': 'reviews'}`.
서브탭: 둘러보기 | 찜 | 후기.

## 9. 단계 구분

- **P1 (현재)**: 폼 + 모델 + 목록 + 상세. **AI 없음.** 단 AI가 P2에서 쓸 모든 것을 저장한다.
- **P2 (다음)**: 백그라운드 AI 생성 + 인앱 웹뷰 미리보기(`/memories/<id>/image` 인라인) + 편집 가능한 네이버 복사텍스트(`[사진 N]` 마커).
