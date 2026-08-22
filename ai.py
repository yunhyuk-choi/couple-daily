"""AI layer — the app's ONLY AI mechanism is the `claude` CLI (subprocess).

This is a hard project constraint: we call `claude -p` on the backend, NOT the
Anthropic API/SDK. For structured results we instruct Claude to emit strict JSON
and parse it defensively (stripping any prose / code fences).

Windows gotcha (learned the hard way): every subprocess that reads `claude`
output MUST use text=True, encoding='utf-8', errors='replace' or Korean/emoji
output blows up with a cp949 UnicodeDecodeError.

Security: the prompt is fed to `claude -p` via STDIN (never as a shell arg and
never with shell=True), so user answer text is treated purely as data. The CLI
is invoked as a plain argv list.
"""
import json
import os
import re
import subprocess
import sys
import tempfile

CLAUDE_TIMEOUT = 120  # seconds; `claude -p` is an agent and can be slow
# Vision (reading an image off disk) is markedly slower than a text prompt on
# the 0.1-CPU free tier — give it generous headroom so it isn't killed mid-read.
CAPTION_TIMEOUT = 180
# 팝업 수집은 웹 검색(WebSearch/WebFetch)을 돌리므로 훨씬 느리다(~120s+).
POPUP_TIMEOUT = 300

# Gentle fallbacks used only if the CLI is unavailable / errors out.
FALLBACK_QUESTIONS = [
    "오늘 하루 중 가장 마음이 따뜻해졌던 순간은 언제였어?",
    "요즘 서로에게 가장 고마웠던 일은 뭐야?",
    "우리가 함께 가장 크게 웃었던 최근 순간을 떠올려볼래?",
    "오늘 너를 가장 힘들게 한 건 뭐였어? 내가 어떻게 도와주면 좋을까?",
    "우리가 다음에 꼭 같이 해보고 싶은 소소한 일 하나는?",
]


def _run_claude(prompt: str, timeout: int = CLAUDE_TIMEOUT,
                allow_web: bool = False) -> str:
    """Run `claude -p`, feeding the prompt via stdin. Returns raw stdout text.

    ``allow_web=True`` grants the CLI web tools (``--allowedTools WebSearch
    WebFetch``) so the prompt can search the live web. Default False keeps the
    existing behavior UNCHANGED — used ONLY by the popup fetcher, nowhere else.
    (프로덕션의 claude(CLAUDE_CODE_OAUTH_TOKEN)가 WebSearch를 허용해야 팝업이
    실제로 채워진다 — 이 플래그로 동작함을 검증했다.)

    Raises RuntimeError on non-zero exit / timeout / missing binary.
    """
    argv = ["claude", "-p"]
    if allow_web:
        argv += ["--allowedTools", "WebSearch", "WebFetch"]
    try:
        proc = subprocess.run(
            argv,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise RuntimeError("claude CLI not found on PATH") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(f"claude CLI timed out after {timeout}s") from e

    if proc.returncode != 0:
        raise RuntimeError(
            f"claude CLI exited {proc.returncode}: {(proc.stderr or '').strip()[:300]}"
        )
    return (proc.stdout or "").strip()


def _extract_json(raw: str):
    """Best-effort extraction of a JSON object from Claude's output.

    Handles clean JSON, ```json fenced blocks, and JSON embedded in prose.
    """
    if not raw:
        raise ValueError("empty output")
    text = raw.strip()

    # Strip code fences if present.
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fall back to the first {...} span.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError(f"no JSON object found in output: {text[:200]!r}")


# ---------------------------------------------------------------------------
# Daily question generation (personalized from past answers)
# ---------------------------------------------------------------------------
def generate_daily_question(recent_pairs, past_questions=None):
    """Generate one warm daily question, personalized from recent answers.

    recent_pairs: list of dicts {question, answers: [text, ...]} (most recent
    first), used to steer the new question. Returns (text, source) where source
    is 'ai' or 'fallback'.

    past_questions: 이 커플이 지금까지 받은 지난 질문 '텍스트'들의 리스트(최신 먼저,
    ~40개). recent_pairs(개인화용)보다 넓게 모아 '의미 중복 회피'에만 쓴다 — 워딩이
    달라도 같은 뜻이면 새 질문이 겹치지 않게 모델이 직접 판단하도록 프롬프트에 넣는다.
    선택 인자(없으면 무시)라 다른 호출부는 그대로 동작한다.
    """
    history_lines = []
    for i, p in enumerate(recent_pairs[:8], 1):
        ans = " / ".join(a for a in p.get("answers", []) if a)
        history_lines.append(f"{i}. Q: {p['question']}\n   A: {ans or '(답변 없음)'}")
    history_block = "\n".join(history_lines) if history_lines else "(아직 지난 답변이 없음)"

    # 의미 중복 회피용 지난 질문 목록(넓게). 워딩이 달라도 같은 뜻이면 피하도록 나열.
    past_list = [str(t).strip() for t in (past_questions or []) if str(t).strip()]
    past_block = "\n".join(f"- {t}" for t in past_list) if past_list else "(아직 지난 질문이 없음)"

    prompt = (
        "너는 '사귀는 연인 두 사람'에게 매일 하나씩 던져줄 '오늘의 질문'을 만드는 "
        "다정한 도우미야. 지금 하는 일은, 데이트하는 커플에게 물어볼 질문 하나를 "
        "만드는 거야.\n"
        "이 질문은 두 사람에게 '똑같이' 보여져 — 한 사람에게만 하는 말이 아니라 "
        "두 사람 모두에게 함께 건네는 질문이야.\n\n"
        "규칙(아주 중요):\n"
        "- 두 사람 모두에게 함께 건네는 질문이니, 특정 한 사람을 부르는 호칭을 "
        "절대 쓰지 마. 성별·관계 호칭(오빠/누나/언니/형/자기/여보/아기 등)이나 "
        "이름을 쓰지 말고, 누가 더 나이 많은지·성별이 무엇인지도 절대 가정하지 마. "
        "'두 사람'이나 자연스러운 반말 2인칭으로 중립적으로 써.\n"
        "- 아래 지난 대화는 '같은 주제·같은 질문을 반복하지 않기 위해서'만 참고해. "
        "지난 답변에 나온 구체적인 내용·사람·장소·단어를 새 질문에 인용하거나 "
        "언급하지 마(유출 금지). 각 질문은 그 자체로 완결되어야 해.\n"
        "- 분위기: 따뜻하고 호기심을 자아내며 대화를 여는 커플 질문. 깊지만 답하기 "
        "쉬운, 때로는 장난스럽고 때로는 의미 있는 — 사귀는 두 사람이 더 가까워지게 "
        "돕는 질문(잘 알려진 커플/관계 질문 앱들 같은 결). 날마다 결을 바꿔줘 "
        "(어떤 날은 가볍고 재밌게, 어떤 날은 잔잔하게).\n"
        "- 너무 무겁거나 캐묻거나 어색한 질문은 피해.\n"
        "- 정확히 한 문장, 물음표로 끝나고, 어느 쪽이 읽어도 자연스러운 다정한 반말체.\n"
        "- 새 질문은 아래 '지금까지 나온 질문' 어느 것과도 의미가 겹치면 안 된다 — "
        "워딩이 달라도 같은 뜻이면 반드시 다른 주제/각도로 바꿔라. 자연스러운 다음 "
        "질문이 기존과 겹치면 다른 주제를 골라라.\n\n"
        "지난 대화는 오직 '중복 회피'용 참고 자료야(내용 인용 금지):\n"
        f"[최근 지난 질문과 답변]\n{history_block}\n\n"
        "아래는 이 커플에게 지금까지 나온 질문들이야. 새 질문은 이것들과 '의미가 "
        "겹치면' 안 돼(같은 뜻·같은 주제를 워딩만 바꾼 것도 중복이다):\n"
        f"[지금까지 나온 질문]\n{past_block}\n\n"
        '출력은 반드시 JSON 객체 하나만, 다른 텍스트/설명/코드펜스 없이: '
        '{"question": "..."}'
    )
    try:
        raw = _run_claude(prompt)
        data = _extract_json(raw)
        q = (data.get("question") or "").strip()
        if q:
            return q, "ai"
        raise ValueError("empty question field")
    except Exception as e:  # noqa: BLE001 — degrade gracefully
        print(f"[ai] daily question generation failed: {e}", file=sys.stderr)
        # Deterministic-ish fallback: rotate by history length.
        idx = len(recent_pairs) % len(FALLBACK_QUESTIONS)
        return FALLBACK_QUESTIONS[idx], "fallback"


# ---------------------------------------------------------------------------
# Monthly qualitative insight (grounded in the month's actual answers)
# ---------------------------------------------------------------------------
def generate_monthly_qualitative(month_label, name_a, name_b, qa_items):
    """Read a month's Q&A and return grounded, gentle observations.

    qa_items: list of {date, question, a, b} dicts.
    Returns a dict with keys: themes(list[str]), tone(str),
    divergent_question(str), summary(str), fun(str). Returns None on failure.

    Defensive: if there is no partner yet (``name_b`` is falsy) there is no
    two-person conversation to summarize, so return None instead of raising.
    """
    if not qa_items or not name_a or not name_b:
        return None

    lines = []
    for it in qa_items:
        lines.append(
            f"[{it['date']}] Q: {it['question']}\n"
            f"   {name_a}: {it.get('a') or '(무응답)'}\n"
            f"   {name_b}: {it.get('b') or '(무응답)'}"
        )
    body = "\n".join(lines)

    prompt = (
        f"너는 연인 두 사람({name_a}, {name_b})의 한 달치 '오늘의 질문' 답변을 읽고 "
        "따뜻하고 근거 있는 관찰을 정리해주는 도우미야.\n"
        f"대상 기간: {month_label}\n\n"
        "아래 실제 답변만을 근거로 분석해. 지어내지 말고, 실제 답변에 나온 내용만 언급해.\n"
        "절대 하지 말 것: 사랑 점수/궁합 퍼센트 같은 가짜 수치화. 대신 부드럽고 구체적인 관찰.\n\n"
        f"[이번 달 질문과 답변]\n{body}\n\n"
        "다음 JSON 객체 하나만 출력해 (다른 텍스트/코드펜스 없이):\n"
        "{\n"
        '  "themes": ["반복해서 등장한 주제나 키워드 2~4개"],\n'
        '  "tone": "이번 달 답변에서 느껴진 감정적 분위기와 그 변화 (1~2문장)",\n'
        '  "divergent_question": "두 사람의 답이 가장 달랐던 질문과 어떻게 달랐는지 (한 문장)",\n'
        '  "summary": "이번 달을 다정하게 요약하는 한 문장",\n'
        '  "fun": "재미로 보는 가벼운 한마디 (점수 아님)"\n'
        "}"
    )
    try:
        raw = _run_claude(prompt)
        data = _extract_json(raw)
        return {
            "themes": data.get("themes") or [],
            "tone": (data.get("tone") or "").strip(),
            "divergent_question": (data.get("divergent_question") or "").strip(),
            "summary": (data.get("summary") or "").strip(),
            "fun": (data.get("fun") or "").strip(),
        }
    except Exception as e:  # noqa: BLE001
        print(f"[ai] monthly insight generation failed: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Monthly cross-domain report — warm "우리의 N월" recap (Q&A + 사진 + 판결 + 데이트)
# ---------------------------------------------------------------------------
def generate_monthly_report(month_label, name_a, name_b, context):
    """여러 영역(오늘의질문·사진·판결·다녀온 데이트)을 엮은 따뜻한 월간 리포트.

    context: insights.collect_month_context(...)의 반환 dict
      {qa: [...], photos: {count, captions}, cases: {count, resolved, topics},
       dates: {visited, confirmed_count}}.

    STRICT JSON을 유도해 파싱하고 각 필드를 정규화한 dict를 반환한다. 데이터가
    없는 영역의 note는 빈 문자열로 유지한다. 실패 시 None(절대 raise 안 함).
    파트너가 없으면(name_b falsy) 요약할 두 사람 대화가 없으므로 None.
    """
    if not name_a or not name_b:
        return None

    ctx = context or {}
    qa = ctx.get("qa") or []
    photos = ctx.get("photos") or {}
    cases = ctx.get("cases") or {}
    dates = ctx.get("dates") or {}

    # ---- Q&A 블록 ----
    if qa:
        qa_lines = []
        for it in qa:
            qa_lines.append(
                f"[{it.get('date')}] Q: {it.get('question')}\n"
                f"   {name_a}: {it.get('a') or '(무응답)'}\n"
                f"   {name_b}: {it.get('b') or '(무응답)'}"
            )
        qa_block = "\n".join(qa_lines)
    else:
        qa_block = "(이번 달 오늘의 질문 답변 기록 없음)"

    # ---- 사진 블록 ----
    photo_count = photos.get("count") or 0
    captions = photos.get("captions") or []
    if photo_count or captions:
        cap_lines = "\n".join(f"- {c}" for c in captions) or "(캡션 없음)"
        photo_block = f"총 {photo_count}장. 캡션 예시:\n{cap_lines}"
    else:
        photo_block = "(이번 달 추가된 사진 없음)"

    # ---- 판결(사건) 블록 ----
    case_count = cases.get("count") or 0
    resolved = cases.get("resolved") or 0
    topics = cases.get("topics") or []
    if case_count:
        topic_lines = "\n".join(f"- {t}" for t in topics) or "(제목 없음)"
        case_block = (
            f"총 {case_count}건 중 {resolved}건 화해로 종결. 사건 주제:\n{topic_lines}"
        )
    else:
        case_block = "(이번 달 판결(사건) 없음)"

    # ---- 데이트 블록 ----
    visited = dates.get("visited") or []
    confirmed_count = dates.get("confirmed_count") or 0
    if visited:
        visited_lines = "\n".join(f"- {t}" for t in visited)
        date_block = (
            f"이번 달 다녀온 데이트:\n{visited_lines}\n"
            f"(둘 다 찜해 확정된 데이트 후보 누적 {confirmed_count}건)"
        )
    else:
        date_block = (
            f"(이번 달 '다녀왔어'로 표시한 데이트 없음; 둘 다 찜한 확정 후보 "
            f"{confirmed_count}건)"
        )

    prompt = (
        f"너는 연인 두 사람({name_a}, {name_b})의 한 달을 여러 영역의 실제 기록으로 "
        "따뜻하게 돌아봐주는 도우미야.\n"
        f"대상 기간: {month_label}\n\n"
        "아래는 이번 달 실제 데이터야(오늘의 질문 답변·함께 남긴 사진 캡션·화해 "
        "사건·다녀온 데이트). 오직 이 실제 데이터에만 근거해 지어내지 말고, 여러 "
        "영역을 자연스럽게 엮어 '우리의 이번 달' 회고를 만들어줘.\n"
        "절대 하지 말 것: 사랑 점수·궁합 퍼센트 같은 가짜 수치화. 대신 다정하고 "
        "구체적인 관찰. 데이터가 없는 영역의 note는 반드시 빈 문자열(\"\")로 둬.\n\n"
        f"[오늘의 질문 답변]\n{qa_block}\n\n"
        f"[함께 남긴 사진]\n{photo_block}\n\n"
        f"[화해 사건]\n{case_block}\n\n"
        f"[데이트]\n{date_block}\n\n"
        "다음 JSON 객체 하나만 출력해 (다른 텍스트/코드펜스 없이):\n"
        "{\n"
        '  "headline": "우리의 이번 달을 한 문장으로 (다정하게)",\n'
        '  "summary": "여러 영역을 자연스럽게 엮은 2~3문장 요약",\n'
        '  "themes": ["Q&A에서 반복된 주제 2~4개"],\n'
        '  "tone": "이번 달 감정 분위기와 변화 (1~2문장)",\n'
        '  "photo_note": "사진들에서 느껴진 것 (사진 있을 때만, 없으면 \\"\\")",\n'
        '  "date_note": "함께 다녀온 데이트 이야기 (있을 때만, 없으면 \\"\\")",\n'
        '  "harmony_note": "다툼→화해 흐름을 긍정적으로 (사건 있을 때만, 없으면 \\"\\")",\n'
        '  "fun": "재미로 보는 가벼운 한마디 (점수/퍼센트 아님)"\n'
        "}"
    )

    def _s(v):
        return v.strip() if isinstance(v, str) else ("" if v is None else str(v).strip())

    try:
        raw = _run_claude(prompt)
        data = _extract_json(raw)
        themes = data.get("themes")
        if not isinstance(themes, list):
            themes = []
        themes = [str(t).strip() for t in themes if str(t).strip()]
        return {
            "headline": _s(data.get("headline")),
            "summary": _s(data.get("summary")),
            "themes": themes,
            "tone": _s(data.get("tone")),
            "photo_note": _s(data.get("photo_note")),
            "date_note": _s(data.get("date_note")),
            "harmony_note": _s(data.get("harmony_note")),
            "fun": _s(data.get("fun")),
        }
    except Exception as e:  # noqa: BLE001 — degrade gracefully
        print(f"[ai] monthly report generation failed: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Photo captioning via claude vision (reads an image file off disk)
# ---------------------------------------------------------------------------
def caption_image(image_path):
    """Caption a local image file using `claude` vision. Returns a dict or None.

    Passing a file path to ``claude -p`` makes it read + describe the image (the
    CLI's own Read tool ingests the file), so we embed the temp image path in the
    prompt and instruct strict-JSON output. This is a slow agent+vision pass, so
    the caller MUST run it in a background thread — never on the request path.

    Returns ``{"caption": <한국어 한 문장>, "tags": [<한국어 키워드>, ...]}`` on
    success, or ``None`` on any failure/timeout/parse error (never raises).
    """
    if not image_path:
        return None
    prompt = (
        f"다음 경로의 이미지 파일을 읽어서 무엇이 담겨 있는지 보고 답해줘: {image_path}\n\n"
        "너는 연인 두 사람의 추억 사진첩을 정리하는 다정한 도우미야. 이 사진을 보고:\n"
        "- caption: 사진에 실제로 보이는 것을 담은 자연스럽고 다정한 한국어 한 문장.\n"
        "- tags: 사진에 실제로 보이는 사물/장면/색/글자 등을 나타내는 한국어 키워드 2~6개.\n"
        "실제로 보이는 것만 근거로 해. 보이지 않는 걸 지어내지 마.\n\n"
        "출력은 반드시 JSON 객체 하나만, 다른 텍스트/설명/코드펜스 없이:\n"
        '{"caption": "...", "tags": ["...", "..."]}'
    )
    try:
        raw = _run_claude(prompt, timeout=CAPTION_TIMEOUT)
        data = _extract_json(raw)
        caption = (data.get("caption") or "").strip()
        raw_tags = data.get("tags") or []
        if not isinstance(raw_tags, list):
            raw_tags = []
        tags = [str(t).strip() for t in raw_tags if str(t).strip()]
        if not caption:
            raise ValueError("empty caption field")
        return {"caption": caption, "tags": tags}
    except Exception as e:  # noqa: BLE001 — degrade gracefully, never raise
        print(f"[ai] image captioning failed: {e}", file=sys.stderr)
        return None


# suggest_crop이 임시파일로 떨굴 때 허용하는 확장자(claude Read가 여는 포맷).
_CROP_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}


def _normalize_crop_box(data):
    """claude가 준 dict를 정규화 바운딩박스 [x,y,w,h](0..1)로 (순수/오프라인).

    x/y/w/h를 float로 강제·0..1 클램프하고, 박스가 경계를 넘으면 w/h를 줄여 안으로
    집어넣는다. w·h가 0 이하가 되면 못 쓰는 것으로 보고 None. claude 없이 가짜
    dict로 바로 단위 테스트할 수 있게 분리했다.
    """
    if not isinstance(data, dict):
        return None
    try:
        x = float(data.get("x"))
        y = float(data.get("y"))
        w = float(data.get("w"))
        h = float(data.get("h"))
    except (TypeError, ValueError):
        return None
    x = max(0.0, min(1.0, x))
    y = max(0.0, min(1.0, y))
    w = max(0.0, min(1.0, w))
    h = max(0.0, min(1.0, h))
    if x + w > 1.0:
        w = 1.0 - x
    if y + h > 1.0:
        h = 1.0 - y
    if w <= 0.0 or h <= 0.0:
        return None
    return [x, y, w, h]


def suggest_crop(image_bytes, subject_hint, target_aspect, ext=".jpg"):
    """사진에서 '이 단락이 말하는 대상'이 담긴 가장 중요한 영역의 정규화 바운딩박스.

    캡셔너와 '똑같은' 방식(임시파일 + ``claude -p`` 비전)으로 이미지를 claude에
    보여주고, 랜드스케이프 크롭이 전경에 둬야 할 핵심 영역 [x,y,w,h](0..1, 폭/높이
    기준)를 strict JSON으로 받는다. ``subject_hint``는 해당 섹션의 소제목+본문 일부
    (무엇에 관한 단락인지)다. 어떤 실패에도 ``None``을 돌려준다(절대 raise 안 함).

    동시성: 호출부(백그라운드 워커)가 캡션과 '같은' _CAPTION_SEM으로 직렬화한다 —
    여기서는 세마포어를 잡지 않는다(_run_claude에도 추가하지 않는다).
    ``target_aspect``는 프롬프트 참고용 힌트로만 넘긴다(강제 비율은 결정론적
    ``compute_crop_rect``가 처리).
    """
    if not image_bytes:
        return None
    tmp_path = None
    try:
        suffix = ext if ext in _CROP_IMAGE_EXTS else ".jpg"
        fd, tmp_path = tempfile.mkstemp(prefix="cd_crop_", suffix=suffix)
        with os.fdopen(fd, "wb") as fh:
            fh.write(image_bytes)

        hint = (subject_hint or "").strip()[:400] or "(설명 없음)"
        try:
            aspect_txt = f"{float(target_aspect):.3g}"
        except (TypeError, ValueError):
            aspect_txt = "1.333"
        prompt = (
            f"다음 경로의 이미지 파일을 읽어서 실제로 보고 답해줘: {tmp_path}\n\n"
            "너는 블로그 후기 사진을 가로형(landscape)으로 자를 때 '무엇을 반드시 "
            "남길지'를 정하는 도우미야. 이 사진이 실릴 단락은 아래 내용에 관한 거야:\n"
            f"[단락 주제] {hint}\n\n"
            "이 단락이 말하는 '주인공' 하나(그 음식·메뉴·사물·인물 등 바로 그 대상)를 "
            "사진에서 찾아, 그것을 '빠짐없이 딱 감싸는 가장 타이트한' 바운딩박스를 줘.\n"
            "지켜야 할 규칙:\n"
            "- 주인공 전체가 박스 안에 완전히 들어와야 해(끝·가장자리가 잘리면 안 됨). "
            "하지만 사진 전체나 넉넉한 여백을 담지는 마 — 주인공에 딱 맞게.\n"
            "- 주인공이 실제로 있는 위치를 정직하게 반영해. 위쪽에 있으면 y를 작게, "
            "아래에 있으면 y를 크게, 한쪽으로 치우쳐 있으면 x를 그쪽으로. 무조건 "
            "가운데(안전한 중앙 박스)로 두지 마 — 사진마다 위치는 다르다.\n"
            "- 주인공이 여러 개면 이 단락 주제에 가장 맞는 '하나'만 감싸.\n"
            "- 좌표는 0~1 상대값: x·w는 '가로폭' 기준, y·h는 '세로높이' 기준. "
            "x=왼쪽에서 시작, y=위에서 시작, w=폭, h=높이. (x+w, y+h는 1을 넘지 마.)\n"
            f"- 최종 크롭 비율은 대략 {aspect_txt}:1(가로:세로)로 만들 거야(참고).\n\n"
            "출력은 반드시 JSON 객체 하나만, 다른 텍스트/설명/코드펜스 없이:\n"
            '{"x": 0.12, "y": 0.05, "w": 0.55, "h": 0.42}'
        )
        raw = _run_claude(prompt, timeout=CAPTION_TIMEOUT)
        data = _extract_json(raw)
        return _normalize_crop_box(data)
    except Exception as e:  # noqa: BLE001 — degrade gracefully, never raise
        print(f"[ai] suggest_crop failed: {e}", file=sys.stderr)
        return None
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 커플 싸움 AI 판사 (judge a couple's fight from their statements)
# ---------------------------------------------------------------------------
def _normalize_fault(raw_fault, name_a, name_b):
    """Coerce claude's ``fault`` list into a clean two-entry split summing to 100.

    Pure/offline (no claude) so it can be unit-tested directly. Rules:
      * keep the two names EXACTLY as passed in (name_a, name_b), matched by
        name against ``raw_fault`` where possible (else positionally);
      * coerce each percent to int, clamp to 0..100;
      * if the two don't sum to 100, rebalance proportionally (or fall back to
        50/50 when both are 0);
      * return ``None`` when there is no usable fault data at all.
    """
    if not isinstance(raw_fault, list) or not raw_fault:
        return None

    # Map name -> percent from whatever claude returned (defensive on shapes).
    by_name = {}
    ordered = []
    for entry in raw_fault:
        if not isinstance(entry, dict):
            continue
        nm = entry.get("name")
        pct = entry.get("percent")
        try:
            pct = int(round(float(pct)))
        except (TypeError, ValueError):
            pct = None
        ordered.append((nm, pct))
        if isinstance(nm, str):
            by_name[nm.strip()] = pct

    def _pick(target, fallback_idx):
        # Prefer an exact name match; otherwise fall back to position.
        if target in by_name and by_name[target] is not None:
            return by_name[target]
        if fallback_idx < len(ordered):
            return ordered[fallback_idx][1]
        return None

    pa = _pick(name_a, 0)
    pb = _pick(name_b, 1)
    if pa is None and pb is None:
        return None  # nothing usable

    pa = 0 if pa is None else max(0, min(100, pa))
    pb = 0 if pb is None else max(0, min(100, pb))

    total = pa + pb
    if total == 100:
        pass
    elif total <= 0:
        pa, pb = 50, 50  # no signal — split evenly
    else:
        # Rebalance proportionally so the two always sum to exactly 100.
        pa = int(round(pa * 100 / total))
        pb = 100 - pa
    return [
        {"name": name_a, "percent": pa},
        {"name": name_b, "percent": pb},
    ]


# 미션이 하나도 없을 때 쓰는 부드럽고 일반적인 기본 화해 미션 (톤 유지용).
DEFAULT_MISSIONS = [
    "오늘 안에 서로 눈 보고 5초만 꼭 안아주기 🤗",
    "고마웠던 점 하나씩 말해주기",
]

# 판결 텍스트 필드(순서/키 고정) — 정규화·기본값 처리에 함께 쓴다.
_VERDICT_TEXT_FIELDS = (
    "summary", "issue", "facts", "consideration", "judgment",
    "order", "empathy_note",
)


def _normalize_verdict(data, name_a, name_b):
    """claude가 준 원본 dict를 리치·일관 스키마로 정규화한다 (순수/오프라인).

    claude 없이 가짜 dict로 바로 단위 테스트할 수 있게 만들었다. 규칙:
      * ``fault`` 는 ``_normalize_fault`` 재사용.
      * ``winner`` 는 'a'/'b'/'tie' 만 허용, 잘못됐으면 fault 에서 유도
        (잘못 % 가 더 낮은 쪽 = 우리가 손들어주는 쪽 = winner; 같으면 'tie').
      * ``empathy_score`` 는 int 로 강제, 0..100 클램프, 없거나 못 쓰면 60.
      * ``missions`` 는 공백 제거한 비어있지 않은 문자열 리스트, 최대 3개,
        비면 ``DEFAULT_MISSIONS`` 로 폴백.
      * 모든 텍스트 필드는 ``.strip()``, 없으면 "".
      * 쓸 만한 fault 도 없고 텍스트도 전혀 없으면 ``None`` (부분 판결은 살린다).
    """
    if not isinstance(data, dict):
        return None

    fault = _normalize_fault(data.get("fault"), name_a, name_b)

    # 텍스트 필드 정규화.
    texts = {}
    for key in _VERDICT_TEXT_FIELDS:
        v = data.get(key)
        texts[key] = (v or "").strip() if isinstance(v, str) else ""

    # winner: 유효값만 수용, 아니면 fault 에서 유도(잘못 낮은 쪽이 winner).
    winner = data.get("winner")
    if winner not in ("a", "b", "tie"):
        if fault:
            pa, pb = fault[0]["percent"], fault[1]["percent"]
            winner = "a" if pa < pb else ("b" if pb < pa else "tie")
        else:
            winner = "tie"

    # empathy_score: int 강제 → 0..100 클램프 → 기본 60.
    try:
        empathy = int(round(float(data.get("empathy_score"))))
        empathy = max(0, min(100, empathy))
    except (TypeError, ValueError):
        empathy = 60

    # missions: 문자열만, 공백 제거, 빈 것 제거, 최대 3개, 비면 기본 폴백.
    raw_missions = data.get("missions")
    missions = []
    if isinstance(raw_missions, list):
        for m in raw_missions:
            s = str(m).strip() if m is not None else ""
            if s:
                missions.append(s)
            if len(missions) >= 3:
                break
    if not missions:
        missions = list(DEFAULT_MISSIONS)

    # 부분 판결도 보여줄 가치가 있으니, fault·텍스트가 전부 없을 때만 None.
    if not fault and not any(texts.values()):
        return None

    return {
        "winner": winner,
        "fault": fault or [],
        "empathy_score": empathy,
        "missions": missions,
        **texts,
    }


def judge_fight(name_a, name_b, situation, statements):
    """Judge a couple's fight with `claude`, returning a normalized verdict dict.

    ``statements`` is a list of ``{"name": <display_name>, "text": <진술>}`` with
    1 or 2 entries (a partner who left no statement is simply absent). Builds a
    warm, light Korean prompt, runs `claude -p`, parses strict JSON, and returns
    a normalized RICH dict via ``_normalize_verdict`` — or ``None`` on ANY failure
    (never raises), exactly like ``generate_monthly_qualitative``. Because it runs
    the slow agent subprocess, the caller MUST invoke it from a background thread,
    never on the request path.

    Returns on success a dict with keys: winner, fault, summary, issue, facts,
    consideration, judgment, order, empathy_score, empathy_note, missions.
    """
    if not name_a or not name_b or not (situation or "").strip():
        return None
    if not statements:
        return None

    stmt_lines = []
    for s in statements:
        nm = (s.get("name") or "").strip() or "익명"
        txt = (s.get("text") or "").strip() or "(진술 없음)"
        stmt_lines.append(f"- {nm}의 진술: {txt}")
    stmt_block = "\n".join(stmt_lines)
    only_one = len(statements) < 2

    prompt = (
        "[페르소나]\n"
        f"너는 연인 두 사람({name_a}, {name_b})의 다툼을 봐주는, 밝고 다정한 "
        "'커플 화해 판사'야. 무섭거나 권위적인 법정 판사가 아니라, 두 사람을 "
        "아끼는 유쾌한 친구 같은 판사지.\n\n"
        "[말투·태도 규칙 — 아주 중요]\n"
        "- 밝고 다정하게, 유머 한 스푼. 반말체의 따뜻한 말투.\n"
        "- 누구도 상처받지 않게. 잘못을 짚을 때도 귀엽고 부드럽게 돌려서 말해줘.\n"
        "- 항상 관계 회복과 애정을 북돋는 마무리로.\n"
        "- 무겁거나 훈계조·법정 위압감은 절대 금지. 비난·인신공격·한쪽만 편들기 금지.\n"
        "- 이모지는 과하지 않게 한두 개까지만 허용.\n\n"
        "아래의 상황 설명과, 있는 만큼의 각자 진술만을 근거로 판단해. 지어내지 마.\n\n"
        f"[상황 설명]\n{(situation or '').strip()}\n\n"
        f"[각자 진술]\n{stmt_block}\n\n"
        "[판결 규칙]\n"
        f"- 잘못 비율(fault)은 {name_a}와 {name_b} 두 사람 것을 합쳐 정확히 100이 되게, "
        "근거 있게 배분해.\n"
        + (
            "- 지금은 한 사람의 진술만 있어. 그 한계를 부드럽게 감안해서 신중히 판단하고, "
            "그 뉘앙스를 consideration(참작)이나 summary 톤에 자연스럽게 녹여줘.\n"
            if only_one
            else "- 두 사람의 진술을 모두 고려해서 공평하게 판단해.\n"
        )
        + "- summary 는 누가 '아주 조금' 더 잘못인지 두 사람 다 피식 웃게, 기분 상하지 "
        "않게 가볍고 다정하게 한 문장으로.\n"
        "- 목표는 관계 회복이야. 구체적이고 실천 가능하고 귀여운 화해 미션을 제시해.\n\n"
        "출력은 아래 JSON 스키마 하나만, 다른 텍스트/설명/코드펜스 없이 출력해. "
        "각 필드의 톤·길이 지침을 지켜:\n"
        "{\n"
        '  "winner": "a" | "b" | "tie",  '
        f'// a={name_a} 쪽 손을 살짝 더 들어줌, b={name_b}, tie=무승부\n'
        f'  "fault": [{{"name": "{name_a}", "percent": <정수>}}, '
        f'{{"name": "{name_b}", "percent": <정수>}}],  // 합 100\n'
        '  "summary": "<한 줄 판결 요약: 누구 잘못이 조금 더 큰지 기분 상하지 않게 '
        '가볍고 다정하게. 한 문장>",\n'
        '  "issue": "<쟁점: 무엇 때문에 다퉜는지 1~2문장>",\n'
        '  "facts": "<인정되는 사실: 양쪽이 공감할 객관적 사실 1~2문장>",\n'
        '  "consideration": "<참작 사유: 서로 이해해줄 만한 사정 1~2문장>",\n'
        '  "judgment": "<판단: 따뜻하고 위트있는 한 마디로 정리, 2~3문장. 비난조 금지>",\n'
        '  "order": "<주문: 두 사람 모두에게 건네는 회복 지향의 마무리 한마디, 1~2문장>",\n'
        '  "empathy_score": <0~100 정수>,  // 두 사람 의도가 얼마나 잘 통했는지(사랑/공감도). '
        "낮아도 나쁜 게 아니라는 톤\n"
        '  "empathy_note": "<공감 점수 한 줄 설명, 다정하게>",\n'
        '  "missions": ["<화해 미션 2~3개, 구체적·실천가능·귀엽게. 예: 오늘 안에 서로 안아주기>"]\n'
        "}"
    )
    try:
        raw = _run_claude(prompt)
        data = _extract_json(raw)
        verdict = _normalize_verdict(data, name_a, name_b)
        if not verdict:
            raise ValueError("empty verdict")
        return verdict
    except Exception as e:  # noqa: BLE001 — degrade gracefully, never raise
        print(f"[ai] fight judgment failed: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# 데이트 뉴스 커플 맞춤 추천 점수 (배치 채점, 1콜) — P2
# ---------------------------------------------------------------------------
def score_events(profile_text, events):
    """커플 취향 프로필로 후보 행사들을 한 번에(배치 1콜) 채점한다.

    효율이 핵심이다: 행사 하나당 claude 호출을 하지 않는다 — 호출부가 넘긴
    유한한 배치(예: ≤24)를 번호 매긴 목록으로 만들어 프롬프트 '하나'로 채점하고,
    JSON 배열을 돌려받는다. 느린 서브프로세스라 호출부가 반드시 백그라운드에서
    돌려야 한다.

    ``events``: ``{"ref": <event_id>, "title", "category", "place",
    "description"}`` dict의 리스트(호출부가 배치 크기를 제한해 넘긴다).

    반환: 받은 ref에 대해서만 ``{"ref", "score", "reason"}`` 리스트.
      * score → int 강제, 0..100 클램프(없으면 60),
      * reason → .strip()(없으면 ""),
      * ref로 다시 매칭. 출력에 없는 ref는 생략(호출부가 실패 처리/재시도).
    실패 시 ``[]``(또는 부분) 반환, 절대 raise 안 함(다른 ai.py 함수와 동일).
    """
    if not events:
        return []

    # 번호 매긴 후보 목록. ref는 정수 event_id를 그대로 쓴다(모델이 되돌려줌).
    lines = []
    for ev in events:
        ref = ev.get("ref")
        title = (ev.get("title") or "").strip() or "(제목 없음)"
        parts = [f"[ref {ref}] {title}"]
        cat = (ev.get("category") or "").strip()
        place = (ev.get("place") or "").strip()
        desc = (ev.get("description") or "").strip()
        if cat:
            parts.append(f"분류: {cat}")
        if place:
            parts.append(f"장소: {place}")
        if desc:
            parts.append(f"설명: {desc[:200]}")  # 프롬프트 폭주 방지로 잘라 붙임
        lines.append(" / ".join(parts))
    catalog = "\n".join(lines)

    profile = (profile_text or "").strip()
    has_profile = bool(profile)
    profile_block = profile if has_profile else "(아직 이 커플에 대한 정보가 거의 없어)"

    prompt = (
        "[페르소나]\n"
        "너는 연인 두 사람에게 딱 맞는 데이트를 골라주는, 밝고 다정한 '커플 데이트 "
        "큐레이터'야.\n\n"
        "[할 일]\n"
        "아래 '커플 취향 프로필'을 참고해서, 이어지는 '후보 행사' 각각이 이 두 "
        "사람에게 얼마나 좋은 데이트가 될지 0~100점으로 매겨줘. 점수가 높을수록 "
        "이 커플에게 더 잘 맞는다는 뜻이야. 각 행사마다 따뜻하고 짧은 한국어 사유를 "
        "'딱 한 문장'으로 붙여줘.\n\n"
        "[규칙 — 중요]\n"
        "- 말투는 앱 전체와 같은 결로 가볍고 다정하게. 훈계·비난·평가절하 금지.\n"
        "- 점수는 0~100 사이 정수 하나로.\n"
        + (
            "- 프로필에 드러난 두 사람의 관심사·취향·분위기에 잘 맞을수록 높게 줘.\n"
            if has_profile
            else "- 지금은 이 커플에 대한 정보가 거의 없어. 그러니 데이트로서의 "
            "일반적인 매력(접근성·분위기·함께 즐기기 좋은 정도)으로 점수를 매기고, "
            "사유에 '아직 두 사람을 잘 몰라서 일반적인 기준으로 골랐어' 같은 뉘앙스를 "
            "부드럽게 한 번 녹여줘.\n"
        )
        + "- 사유는 각 행사마다 서로 다르게, 그 행사에 맞춰 구체적으로.\n\n"
        f"[커플 취향 프로필]\n{profile_block}\n\n"
        f"[후보 행사]\n{catalog}\n\n"
        "출력은 아래 JSON 객체 하나만, 다른 텍스트/설명/코드펜스 없이. ref는 위에 "
        "주어진 값을 '그대로' 되돌려줘:\n"
        '{"scores": [{"ref": <ref>, "score": <0~100 정수>, "reason": "<한 문장>"}, ...]}'
    )
    try:
        raw = _run_claude(prompt)
        data = _extract_json(raw)
        raw_scores = data.get("scores") if isinstance(data, dict) else None
        if not isinstance(raw_scores, list):
            raise ValueError("no scores array")

        out = []
        seen = set()
        for entry in raw_scores:
            if not isinstance(entry, dict):
                continue
            ref = entry.get("ref")
            if ref is None or ref in seen:
                continue
            # score → int 강제, 0..100 클램프, 없으면 60.
            try:
                sc = int(round(float(entry.get("score"))))
            except (TypeError, ValueError):
                sc = 60
            sc = max(0, min(100, sc))
            reason = entry.get("reason")
            reason = reason.strip() if isinstance(reason, str) else ""
            out.append({"ref": ref, "score": sc, "reason": reason})
            seen.add(ref)
        return out
    except Exception as e:  # noqa: BLE001 — degrade gracefully, never raise
        print(f"[ai] event scoring failed: {e}", file=sys.stderr)
        return []


# ---------------------------------------------------------------------------
# 데이트 뉴스 '추천받기' — 온디맨드 커플 맞춤 데이트 추천 (1콜) — P4
# ---------------------------------------------------------------------------
def recommend_dates(profile_text, candidates):
    """커플 취향 프로필 + 현재 피드 후보로 '한 번'(1콜) 맞춤 데이트를 추천한다.

    페르소나 = 다정한 데이트 큐레이터. 번호(ref) 매긴 후보 목록을 주고, 따뜻한
    한국어 추천 문구(2~3문장, "이런 데이트 어때?" 톤)와 가장 좋은 2~3개 후보를
    각각 한 줄 이유와 함께 고르게 한다. 프로필이 빈약하면 일반적 매력 기준으로
    부드럽게 추천한다. 느린 서브프로세스라 호출부가 반드시 백그라운드에서 돌린다.

    ``candidates``: ``{"ref": <event_id>, "title", "category", "place",
    "description", "score"}`` dict의 리스트(호출부가 상위 ~20개를 넘긴다).

    반환(정규화): ``{"message": <str>, "picks": [{"ref", "why"}, ...]}``.
      * message → .strip(),
      * picks → 후보에 실제 존재하는 ref만 남기고, 최대 3개, why → .strip(),
        중복 ref 제거.
    실패(파싱 실패·빈 message 등) 시 ``None`` 반환, 절대 raise 안 함(다른 ai.py
    함수와 동일).
    """
    if not candidates:
        return None

    # 번호 매긴 후보 목록. ref는 정수 event_id를 그대로 쓴다(모델이 되돌려줌).
    valid_refs = set()
    lines = []
    for c in candidates:
        ref = c.get("ref")
        valid_refs.add(ref)
        title = (c.get("title") or "").strip() or "(제목 없음)"
        parts = [f"[ref {ref}] {title}"]
        cat = (c.get("category") or "").strip()
        place = (c.get("place") or "").strip()
        desc = (c.get("description") or "").strip()
        if cat:
            parts.append(f"분류: {cat}")
        if place:
            parts.append(f"장소: {place}")
        if desc:
            parts.append(f"설명: {desc[:200]}")  # 프롬프트 폭주 방지로 잘라 붙임
        lines.append(" / ".join(parts))
    catalog = "\n".join(lines)

    profile = (profile_text or "").strip()
    has_profile = bool(profile)
    profile_block = profile if has_profile else "(아직 이 커플에 대한 정보가 거의 없어)"

    prompt = (
        "[페르소나]\n"
        "너는 연인 두 사람에게 딱 맞는 데이트를 골라주는, 밝고 다정한 '데이트 "
        "큐레이터'야.\n\n"
        "[할 일]\n"
        "아래 '커플 취향 프로필'과 '후보 행사' 목록을 보고, 이 두 사람에게 오늘 "
        "제안할 데이트를 골라줘. 먼저 따뜻한 추천 문구를 2~3문장으로 쓰고("
        "\"이런 데이트 어때?\" 같은 다정한 톤), 후보 중 가장 잘 어울리는 2~3개를 "
        "골라 각각 한 줄짜리 이유를 붙여줘.\n\n"
        "[규칙 — 중요]\n"
        "- 말투는 앱 전체와 같은 결로 가볍고 다정한 반말체. 훈계·비난 금지.\n"
        "- picks는 2개 이상 3개 이하로. 각 이유는 그 행사에 맞춰 서로 다르게.\n"
        + (
            "- 프로필에 드러난 두 사람의 관심사·취향·분위기에 잘 맞는 걸 골라줘.\n"
            if has_profile
            else "- 지금은 이 커플에 대한 정보가 거의 없어. 그러니 데이트로서의 "
            "일반적인 매력(접근성·분위기·함께 즐기기 좋은 정도)으로 부드럽게 "
            "골라주고, 문구에 '아직 두 사람을 잘 몰라서 일반적인 기준으로 골랐어' "
            "같은 뉘앙스를 한 번 살짝 녹여줘.\n"
        )
        + f"\n[커플 취향 프로필]\n{profile_block}\n\n"
        f"[후보 행사]\n{catalog}\n\n"
        "출력은 아래 JSON 객체 하나만, 다른 텍스트/설명/코드펜스 없이. ref는 위에 "
        "주어진 값을 '그대로' 되돌려줘:\n"
        '{"message": "<추천 문구 2~3문장>", '
        '"picks": [{"ref": <ref>, "why": "<한 줄 이유>"}, ...]}'
    )
    try:
        raw = _run_claude(prompt)
        data = _extract_json(raw)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
        message = (data.get("message") or "").strip()

        raw_picks = data.get("picks")
        if not isinstance(raw_picks, list):
            raw_picks = []
        picks = []
        seen = set()
        for entry in raw_picks:
            if not isinstance(entry, dict):
                continue
            ref = entry.get("ref")
            # 후보에 실제 있는 ref만, 중복 제거, 최대 3개.
            if ref not in valid_refs or ref in seen:
                continue
            why = entry.get("why")
            why = why.strip() if isinstance(why, str) else ""
            picks.append({"ref": ref, "why": why})
            seen.add(ref)
            if len(picks) >= 3:
                break

        if not message:
            raise ValueError("empty message field")
        return {"message": message, "picks": picks}
    except Exception as e:  # noqa: BLE001 — degrade gracefully, never raise
        print(f"[ai] date recommendation failed: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# 데이트 후기 → 네이버 블로그 포스트 (P2) — 네이버 AI 검색 최적화 초안 1콜
# ---------------------------------------------------------------------------
def _clamp_score10(v, default=0):
    """점수를 0~10 정수로 강제·클램프(못 쓰면 default)."""
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return default
    return max(0, min(10, n))


def _normalize_review(data, photos):
    """claude가 준 원본 dict를 렌더 가능한 일관 스키마로 정규화한다 (순수/오프라인).

    claude 없이 가짜 dict로 바로 단위 테스트할 수 있게 분리했다. 규칙:
      * 모든 문자열 .strip().
      * title·summary 비면 최종 None(호출부에서 실패 처리).
      * info_block: {label, value} 리스트, 둘 중 하나라도 비면 제거.
      * sections: {heading, text, photo_index} 리스트, heading·text 모두 비면 제거.
        photo_index는 0..len(photos)-1 범위일 때만 유지(아니면 null).
      * ratings: {aspect, score} 최대 6개, aspect 비면 제거, score 0..10 클램프.
      * faq: {q, a} 최대 4개, q·a 둘 중 하나라도 비면 제거.
      * hashtags: 최대 10개, 공백 제거, 빈 것 제거, 앞에 # 없으면 붙임.
      * 유효 섹션이 하나도 없으면 None.
    """
    if not isinstance(data, dict):
        return None

    n_photos = len(photos or [])

    def _s(v):
        return v.strip() if isinstance(v, str) else ("" if v is None else str(v).strip())

    title = _s(data.get("title"))
    summary = _s(data.get("summary"))

    # info_block
    info = []
    raw_info = data.get("info_block")
    if isinstance(raw_info, list):
        for it in raw_info:
            if not isinstance(it, dict):
                continue
            label = _s(it.get("label"))
            value = _s(it.get("value"))
            if label and value:
                info.append({"label": label, "value": value})

    # sections
    sections = []
    raw_sections = data.get("sections")
    if isinstance(raw_sections, list):
        for it in raw_sections:
            if not isinstance(it, dict):
                continue
            heading = _s(it.get("heading"))
            text = _s(it.get("text"))
            if not heading and not text:
                continue
            pi = it.get("photo_index")
            try:
                pi = int(pi)
            except (TypeError, ValueError):
                pi = None
            if pi is None or pi < 0 or pi >= n_photos:
                pi = None
            sections.append({"heading": heading, "text": text, "photo_index": pi})

    # ratings (최대 6)
    ratings = []
    raw_ratings = data.get("ratings")
    if isinstance(raw_ratings, list):
        for it in raw_ratings:
            if not isinstance(it, dict):
                continue
            aspect = _s(it.get("aspect"))
            if not aspect:
                continue
            ratings.append({"aspect": aspect, "score": _clamp_score10(it.get("score"), 0)})
            if len(ratings) >= 6:
                break

    # faq (최대 4)
    faq = []
    raw_faq = data.get("faq")
    if isinstance(raw_faq, list):
        for it in raw_faq:
            if not isinstance(it, dict):
                continue
            q = _s(it.get("q"))
            a = _s(it.get("a"))
            if not q or not a:
                continue
            faq.append({"q": q, "a": a})
            if len(faq) >= 4:
                break

    # hashtags (최대 10, # 보장)
    hashtags = []
    raw_tags = data.get("hashtags")
    if isinstance(raw_tags, list):
        for t in raw_tags:
            s = _s(t)
            if not s:
                continue
            if not s.startswith("#"):
                s = "#" + s.lstrip("#").replace(" ", "")
            hashtags.append(s)
            if len(hashtags) >= 10:
                break

    if not title or not summary or not sections:
        return None

    return {
        "title": title,
        "summary": summary,
        "info_block": info,
        "sections": sections,
        "ratings": ratings,
        "faq": faq,
        "hashtags": hashtags,
    }


def write_review(topic, location, prose, overall_score, photos):
    """데이트 후기 원문을 네이버 AI 검색 최적화 블로그 초안(구조화 JSON)으로 만든다.

    ``photos``: ``{"index": i, "caption": <그 사진의 AI 캡션>, "tags": [..]}``의
    순서 리스트(호출부가 순서대로 넘긴다). claude를 '한 번'만 호출하고 strict JSON을
    파싱해 ``_normalize_review``로 정규화한 dict를 돌려주거나, 어떤 실패에도 ``None``을
    돌려준다(절대 raise 안 함). 느린 서브프로세스라 호출부가 반드시 백그라운드에서
    돌린다.

    타깃 = 네이버 통합검색/AI 브리핑(하이퍼클로바X). 생성 글은 네이버 AI가 인용하고,
    '진짜 경험 후기' 필터를 통과하도록 구성한다.
    """
    topic = (topic or "").strip()
    if not topic:
        return None
    prose = (prose or "").strip()
    location = (location or "").strip()
    overall_score = _clamp_score10(overall_score, 0)
    photos = photos or []

    # 사진 재료 블록 — 각 사진의 index(0-based)·캡션·태그를 그대로 준다. 캡션이
    # 없으면 '(설명 없음)'로 표시하되, 모델이 지어내지 않도록 명시한다.
    if photos:
        photo_lines = []
        for p in photos:
            idx = p.get("index")
            cap = (p.get("caption") or "").strip() or "(설명 없음)"
            tags = p.get("tags") or []
            tag_str = ", ".join(str(t).strip() for t in tags if str(t).strip())
            line = f"- [사진 {idx}] {cap}"
            if tag_str:
                line += f" (태그: {tag_str})"
            photo_lines.append(line)
        photo_block = "\n".join(photo_lines)
        photo_rule = (
            f"- 사진은 0번부터 {len(photos) - 1}번까지 {len(photos)}장이야. 각 섹션의 "
            "photo_index에는 그 단락과 가장 잘 맞는 사진의 번호(0-based)를 넣고, 붙일 "
            "사진이 없으면 null로 둬. 없는 번호는 절대 쓰지 마. 사진 설명은 위 캡션에만 "
            "근거하고 지어내지 마.\n"
        )
    else:
        photo_block = "(첨부된 사진 없음)"
        photo_rule = "- 첨부된 사진이 없으니 모든 섹션의 photo_index는 null로 둬.\n"

    loc_line = f"위치: {location}\n" if location else "위치: (입력 안 함)\n"

    prompt = (
        "[페르소나]\n"
        "너는 연인이 다녀온 데이트를 '네이버 블로그'에 올릴 진짜 경험 후기 포스트로 "
        "다듬어주는 다정한 도우미야. 실제로 다녀와서 '내돈내산'으로 남기는 감성 후기 "
        "톤이야.\n\n"
        "[가장 중요한 목표]\n"
        "이 글은 '네이버 통합검색·AI 브리핑(하이퍼클로바X 기반)'이 인용하고, 네이버의 "
        "'실제 방문 경험' 필터를 통과하도록 써야 해. 네이버 AI는 광고성·AI 티 나는 "
        "일반론 글을 적극적으로 하위 노출시켜. 그러니 아래 규칙을 반드시 지켜:\n"
        "1) 진짜 다녀온 1인칭 경험 톤으로. 아래 사용자의 '느낀점 원문'과 '사진 캡션'에 "
        "적힌 실제 내용에만 근거해서 구체적·개인적으로 써. 과장된 마케팅 톤·상투적 "
        "미사여구·일반론·AI 광고글 금지.\n"
        "2) 사용자가 주지 않은 사실(특히 정확한 가격·영업시간·메뉴 등)은 절대 지어내지 "
        "마. 모르면 빼거나 '방문 시 확인'이라고 써.\n\n"
        "[말투 — 감성 후기 v2]\n"
        "- 존댓말 기반의 생생한 블로그 말투로 써. '~했어요', '~더라구요', "
        "'~추천드려요' 처럼 다녀온 사람이 도란도란 얘기하듯이. 따뜻하고 친근하게.\n"
        "- 문장을 짧게 끊어. 한 문장 = 한 줄. summary와 각 sections[].text 안에서 "
        "문장이 끝날 때마다 실제 줄바꿈 문자(\\n)로 줄을 나눠. 한 줄에 한 문장만 담아 "
        "(빌더가 각 줄을 가운데 정렬된 한 줄로 렌더해). 한 섹션 text는 대략 3~5줄.\n"
        "- 짧은 줄 끝에는 마침표(.)를 웬만하면 붙이지 마. 진짜 블로거는 짧은 한 줄에 "
        "일일이 마침표를 안 찍어 — 마침표를 줄마다 찍으면 AI 티가 나. 좀 긴 문장이라 "
        "마침표가 읽기 편할 때만 예외적으로 붙여. 물음표(?)·느낌표(!)는 평소대로 써.\n"
        "- 이모지는 아껴서, 감성적인 것만 가끔. 🤍 🩷 ☕️ 📸 🌿 ✨ 정도만 허용. "
        "🔥 😀 👍 💯 💕 같은 흔하고 촌스러운 이모지는 절대 쓰지 마. 소제목마다 붙는 "
        "장식 이모지는 시스템이 알아서 넣으니 넌 넣지 마.\n\n"
        "[강조 마크업 — 색은 쓰지 말고 '표시'만]\n"
        "본문(summary·sections[].text) 안에서 강조하고 싶은 말은 아래 가벼운 기호로 "
        "'감싸기'만 해. 실제 색상 코드·HTML·<span>은 절대 쓰지 마(빌더가 고정 팔레트로 "
        "바꿔줘). 기호는 다음 네 가지만:\n"
        "  *감정·상호·핵심어*  → 별표 하나로 감싸면 핑크 강조.\n"
        "  `가격·주차·시간 같은 팩트`  → 백틱으로 감싸면 파랑 팩트 강조.\n"
        "  ==진짜 인상 깊었던 한마디==  → 등호 두 개로 감싸면 형광펜.\n"
        "  **꼭 굵게**  → 별표 두 개로 감싸면 굵게.\n"
        "규칙: 한 줄에 강조는 최대 1~2개만. 남발하면 AI 티가 나서 역효과야. 기호는 "
        "강조할 '단어/짧은 구'에만 딱 붙여서 감싸(줄 전체를 감싸지 마).\n\n"
        "[구조·AEO 규칙]\n"
        "3) answer-first: summary(요약)는 맨 앞에서 한줄평 + 결론을 먼저 준다. "
        "위 말투대로 짧은 줄들(\\n)로 나눠서.\n"
        "4) 소제목(sections[].heading)은 사람들이 실제로 검색하는 말투로("
        "예: '○○동 △△카페 위치·가는 길', '분위기·인테리어', '메뉴·가격', "
        "'데이트 코스로 어때?', '총평'). heading에는 강조 기호를 쓰지 마.\n"
        "5) info_block은 훑기 쉬운 짧은 label:value만, 아는 것만(예: 분위기·웨이팅·"
        "위치·가격대·이런 점 좋아요). value는 한 줄로 짧게. '방문 날짜'는 시스템이 "
        "실제 날짜로 넣으니 넌 넣지 마.\n"
        "6) FAQ 2~3개(질문형이 AI 인용에 강함) — 실제 데이트 후기에서 궁금할 현실적 질문.\n"
        "7) 별점: 총점 외에 맥락형 항목별 3~5개(예: 분위기/맛·메뉴/가성비/데이트적합도/"
        "재방문의향 중 글 내용에 맞는 것).\n"
        "8) 분량은 sections 본문 합계 대략 900~1400자. 검색어형 해시태그.\n\n"
        "[사용자가 남긴 후기 원문]\n"
        f"주제/장소: {topic}\n"
        f"{loc_line}"
        f"사용자가 매긴 총점: {overall_score}/10\n"
        f"느낀점·특징(원문):\n{prose or '(원문 없음)'}\n\n"
        "[첨부 사진과 그 설명(캡션)]\n"
        f"{photo_block}\n\n"
        "[사진 규칙]\n"
        f"{photo_rule}\n"
        "출력은 아래 JSON 객체 하나만, 다른 텍스트/설명/코드펜스 없이 출력해. "
        "text·summary 값 안의 줄바꿈은 JSON 문자열이므로 \\n 으로 이스케이프해:\n"
        "{\n"
        '  "title": "검색 의도를 담은 포스트 제목",\n'
        '  "summary": "한줄평 + 결론을 맨 앞에 담은 answer-first 요약. 짧은 줄들을 '
        '\\n 으로 나눠서(강조 기호 사용 가능)",\n'
        '  "info_block": [{"label": "분위기", "value": "..."}, '
        '{"label": "웨이팅", "value": "..."}],\n'
        '  "sections": [{"heading": "검색어형 소제목", "text": "짧은 문장들을 \\n으로 '
        '나눈 실제 경험 톤 본문(강조 기호 사용)", "photo_index": 0}],\n'
        '  "ratings": [{"aspect": "분위기", "score": 8}],  // score 0~10 정수, 3~5개\n'
        '  "faq": [{"q": "질문", "a": "답변"}],  // 2~3개\n'
        '  "hashtags": ["#장소명", "#데이트"]\n'
        "}"
    )
    try:
        raw = _run_claude(prompt)
        data = _extract_json(raw)
        result = _normalize_review(data, photos)
        if not result:
            raise ValueError("empty review")
        return result
    except Exception as e:  # noqa: BLE001 — degrade gracefully, never raise
        print(f"[ai] blog review generation failed: {e}", file=sys.stderr)
        return None
