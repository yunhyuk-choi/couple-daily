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


# --- 파일로 분리한 프롬프트 (prompts/*.md) --------------------------------
# 긴 생성 프롬프트는 '튜닝 대상'이라 코드에 박지 않는다 — 글 품질을 고치는 사람이
# 파이썬을 안 건드리고 마크다운만 고칠 수 있어야 한다. 파일 맨 위의 문서용 머리말
# (제목 + 인용 블록)은 모델에 보낼 내용이 아니므로 **첫 '---' 줄까지 잘라낸다.**
_PROMPT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")
_PROMPT_CACHE = {}


def load_prompt(name: str) -> str:
    """``prompts/<name>.md``의 프롬프트 본문을 읽어 돌려준다(프로세스 내 캐시).

    파일 맨 위 머리말(사람용 설명)은 첫 ``---`` 구분선까지 버린다. 파일이 없거나
    읽기에 실패하면 ``""``를 돌려준다 — 호출부가 그걸 보고 우아하게 실패한다
    (프롬프트가 없다고 앱이 죽으면 안 된다).
    """
    cached = _PROMPT_CACHE.get(name)
    if cached is not None:
        return cached
    path = os.path.join(_PROMPT_DIR, f"{name}.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
    except OSError as e:
        print(f"[ai] prompt file missing: {path} ({e})", file=sys.stderr)
        _PROMPT_CACHE[name] = ""
        return ""
    body = raw.replace("\r\n", "\n")
    lines = body.split("\n")
    for i, ln in enumerate(lines):
        if ln.strip() == "---":           # 첫 구분선 뒤부터가 실제 프롬프트
            body = "\n".join(lines[i + 1:])
            break
    body = body.strip()
    _PROMPT_CACHE[name] = body
    return body


def _claude_argv(allow_web: bool = False) -> list:
    """``claude -p`` argv를 만든다(권한 플래그 포함). 단위 테스트로 argv만 검증 가능.

    권한 정책: 이 앱의 claude 콜은 전부 '사용자가 미리 전권을 승인한' 자동화 동작이다
    (사용자 자신의 사진·임시파일을 읽어 캡션/크롭을 만든다). 비대화형 ``-p`` 모드에선
    권한 프롬프트에 답할 수 없어, 필요한 도구를 반드시 ``--allowedTools``로 미리
    허용해야 한다. 그래서 매 호출에 읽기 계열(Read/Glob/Grep)을 항상 allow-list한다
    — 비전 프롬프트가 임시 이미지 파일을 Read하도록 시키는데, 허용 안 하면
    "권한이 없어서 열람이 거부됐어요"로 거부된다.

    ``--dangerously-skip-permissions``/``--permission-mode bypassPermissions``는 쓰지
    않는다: Render는 root로 도는데 Claude Code가 root에서 그 플래그들을 거부해 모든
    claude 콜이 깨진다. ``--allowedTools``는 root에서도 동작하고 필요한 권한만 준다.
    ``allow_web=True``면 WebSearch/WebFetch도 추가로 허용한다(팝업 웹검색 경로).
    """
    tools = ["Read", "Glob", "Grep"]
    if allow_web:
        tools += ["WebSearch", "WebFetch"]
    return ["claude", "-p", "--allowedTools", *tools]


def _run_claude(prompt: str, timeout: int = CLAUDE_TIMEOUT,
                allow_web: bool = False) -> str:
    """Run `claude -p`, feeding the prompt via stdin. Returns raw stdout text.

    읽기 계열 도구(Read/Glob/Grep)는 매 호출에 미리 허용된다(``_claude_argv`` 참고) —
    비전 프롬프트가 임시 이미지를 Read할 때 비대화형 모드에서 거부되지 않게 한다.
    ``allow_web=True``면 WebSearch/WebFetch까지 허용해 라이브 웹 검색을 쓴다(팝업
    페처 전용). 요청 경로의 데일리 질문 콜에도 안전하다(도구를 안 쓰면 그만 —
    여기에 세마포어·블로킹을 추가하지 않는다).

    Raises RuntimeError on non-zero exit / timeout / missing binary.
    """
    argv = _claude_argv(allow_web)
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
        return fallback_daily_question(recent_pairs)


def fallback_daily_question(recent_pairs):
    """claude 를 **돌리지 않고** 고르는 '오늘의 질문' — ``(text, 'fallback')``.

    ``generate_daily_question`` 의 실패 폴백과 **같은 선택 규칙**(히스토리 길이로
    로테이션)을 쓰는 단일 원천. 호출부는 둘이다:
      * 위 generate_daily_question 의 예외 폴백(claude 가 실패했을 때)
      * ``app.get_or_create_today_question`` 이 claude 슬롯을 못 잡았을 때 —
        그 경우 백그라운드가 나중에 개인화 질문으로 올려준다(기능 유지).
    """
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


def suggest_crop(image_bytes, subject_hint, target_aspect, ext=".jpg",
                 image_url=None):
    """사진에서 '이 단락이 말하는 대상'이 담긴 가장 중요한 영역의 정규화 바운딩박스.

    돌려주는 박스는 **0~1 정규화 좌표**다 — 해상도와 무관하다. 그래서 claude 에게
    보여 주는 그림은 **원본일 필요가 전혀 없다**: 작은 다운스케일본으로 '어디를
    남길지'만 정하면, 실제 크롭은 나중에 **원본 픽셀에서** 이뤄진다(브라우저 Canvas).
    호출부는 Graph 렌디션(수백 KB)을 넘긴다 — 예전엔 3MB 원본을 넘겼다.

    이미지를 claude 에게 보여 주는 길은 둘이다:

      * ``image_url`` — 만료 있는 **서명 URL**. ``claude -p --allowedTools
        WebFetch Read`` 가 그 URL 을 받아 자기 쪽에 내려받고 Read 로 픽셀을 본다.
        우리 파이썬 프로세스는 바이트를 **한 번도 안 만진다.**
        ⚠️ 실측(2026-10-07): 된다. 다만 WebFetch 왕복이 붙어 **한 장에 30~33초**가
        더 든다(사진 N장이면 N배). 그리고 큰 이미지(10MB JPEG)에서는 WebFetch 가
        픽셀 대신 텍스트로 변환해 **실패**했다 — 작은 렌디션에서만 믿을 수 있다.
      * ``image_bytes`` — 임시파일에 떨구고 ``Read`` 로 보여 준다(기본). 렌디션이면
        디스크·메모리 모두 수백 KB다.

    ``subject_hint``는 해당 섹션의 소제목+본문 일부(무엇에 관한 단락인지)다.
    어떤 실패에도 ``None``을 돌려준다(절대 raise 안 함).

    동시성: 호출부(백그라운드 워커)가 캡션과 '같은' _CAPTION_SEM으로 직렬화한다 —
    여기서는 세마포어를 잡지 않는다(_run_claude에도 추가하지 않는다).
    ``target_aspect``는 프롬프트 참고용 힌트로만 넘긴다(강제 비율은 결정론적
    ``compute_crop_rect``가 처리).
    """
    if not image_bytes and not image_url:
        return None
    tmp_path = None
    try:
        if image_url:
            where = f"다음 URL 의 이미지를 WebFetch 로 받아서 실제로 보고: {image_url}"
            tools_web = True
        else:
            suffix = ext if ext in _CROP_IMAGE_EXTS else ".jpg"
            fd, tmp_path = tempfile.mkstemp(prefix="cd_crop_", suffix=suffix)
            with os.fdopen(fd, "wb") as fh:
                fh.write(image_bytes)
            where = f"다음 경로의 이미지 파일을 읽어서 실제로 보고: {tmp_path}"
            tools_web = False

        hint = (subject_hint or "").strip()[:400] or "(설명 없음)"
        try:
            aspect_txt = f"{float(target_aspect):.3g}"
        except (TypeError, ValueError):
            aspect_txt = "1.333"
        prompt = (
            f"{where} 답해줘.\n\n"
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
        raw = _run_claude(prompt, timeout=CAPTION_TIMEOUT, allow_web=tools_web)
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


def _valid_crop(crop):
    """[x,y,w,h] 정규화 크롭이 유효(4개 float·0..1·경계 안·양수)하면 리스트로, 아니면
    None. 수동/구스키마 변환에서 넘어온 크롭 보존용(순수/오프라인)."""
    if not isinstance(crop, (list, tuple)) or len(crop) != 4:
        return None
    try:
        x, y, w, h = (float(v) for v in crop)
    except (TypeError, ValueError):
        return None
    if not all(0.0 <= v <= 1.0 for v in (x, y, w, h)):
        return None
    if w <= 0 or h <= 0 or x + w > 1.0001 or y + h > 1.0001:
        return None
    return [x, y, w, h]


def _normalize_block(b, n_photos, _s):
    """자유형 블록 하나를 타입별로 검증·정규화한다. 못 쓰면 None(호출부가 드롭).

    v2에서 AI가 만들 수 있는 블록은 **para/heading/quote(text) · image(photo_index[,crop])
    넷뿐**이다. info(요약표)·faq(고정 Q&A)는 gf-blog 규율이 금지해 폐기했고, ratings는
    사용자가 매긴 총점이라 ``_normalize_review``가 직접 붙인다 — 그래서 여기서 받지
    않는다(알 수 없는 타입 → None → 드롭). 옛 후기에 남아 있는 세 타입은 생성이 아니라
    **표시** 경로(app._ensure_blocks·_review_copy_html/_text)가 계속 다룬다.
    """
    if not isinstance(b, dict):
        return None
    t = _s(b.get("type")).lower()
    if t in ("para", "heading", "quote"):
        text = _s(b.get("text"))
        return {"type": t, "text": text} if text else None
    if t == "image":
        pi = b.get("photo_index")
        try:
            pi = int(pi)
        except (TypeError, ValueError):
            return None
        if pi < 0 or pi >= n_photos:
            return None
        blk = {"type": "image", "photo_index": pi}
        crop = _valid_crop(b.get("crop"))
        if crop:  # 수동/구스키마 변환에서 온 크롭을 보존
            blk["crop"] = crop
        return blk
    return None


def _normalize_review(data, photos, overall_score=0):
    """claude가 준 자유형 블록 dict를 렌더 가능한 일관 스키마로 정규화(순수/오프라인).

    v2 스키마::

        {title, title_target, title_candidates[], title_pick_reason,
         blocks:[{type,...}], hashtags:[..]}

    규칙:
      * 모든 문자열 .strip(). title 비면 후보 첫 개로 폴백, 그래도 없으면 None.
      * title_candidates: {title, structure, reason} 최대 3개. 최종 title이 후보에
        없으면 맨 앞에 끼워 넣는다(상세 화면이 '지금 쓰는 제목'도 고를 수 있게).
      * blocks: 타입별 검증(_normalize_block — para/heading/image/quote만 통과).
        최대 60개.
      * summary: 비어 있는 게 기본이다(v2 계약 — answer-first 고정 폐기). 값이 있으면
        맨 앞 para로 끼워 넣어 하류(렌더·복사본)는 blocks만 보면 되게 한다.
      * ratings: AI 값은 버리고 **사용자 총점 하나**로 항상 끝에 붙인다(지어낸
        항목별 별점 방지).
      * hashtags: 최대 10개, 앞에 # 보장.
      * title이 없거나 서술 콘텐츠(para/quote)가 전혀 없으면 None.
    """
    if not isinstance(data, dict):
        return None

    n_photos = len(photos or [])

    def _s(v):
        return v.strip() if isinstance(v, str) else ("" if v is None else str(v).strip())

    # --- 제목: 타깃 한 문장 → 후보 3개 → 선정 + 이유 ---------------------
    title = _s(data.get("title"))
    title_target = _s(data.get("title_target"))
    title_pick_reason = _s(data.get("title_pick_reason"))
    candidates = []
    raw_cands = data.get("title_candidates")
    if isinstance(raw_cands, list):
        seen = set()
        for c in raw_cands:
            if isinstance(c, str):
                c = {"title": c}
            if not isinstance(c, dict):
                continue
            ct = _s(c.get("title"))
            if not ct or ct in seen:
                continue
            seen.add(ct)
            candidates.append(
                {
                    "title": ct,
                    "structure": _s(c.get("structure")),
                    "reason": _s(c.get("reason")),
                }
            )
            if len(candidates) >= 3:
                break
    if not title and candidates:
        title = candidates[0]["title"]
    if title and not any(c["title"] == title for c in candidates):
        candidates.insert(0, {"title": title, "structure": "", "reason": ""})
        candidates = candidates[:4]

    blocks = []
    raw_blocks = data.get("blocks")
    if isinstance(raw_blocks, list):
        for b in raw_blocks:
            nb = _normalize_block(b, n_photos, _s)
            if nb:
                blocks.append(nb)
            if len(blocks) >= 60:
                break

    # summary는 '정보형 글'에서만 채워지는 선택 필드다(기본 빈 문자열). 있으면 맨 앞
    # para로 흡수해, 렌더·복사본·export가 전부 blocks 하나만 보면 되게 한다.
    summary = _s(data.get("summary"))
    if summary:
        blocks.insert(0, {"type": "para", "text": summary})

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

    if not title:
        return None
    has_text = any(b["type"] in ("para", "quote") for b in blocks)
    if not has_text:
        return None

    # 별점은 **사용자가 매긴 총점 하나**만. AI가 항목별 점수를 내면 그건 날조다
    # (폼에 항목별 입력이 없다). 표로도 그리지 않는다 — 렌더러가 평문 한 줄로 낸다.
    blocks.append(
        {
            "type": "ratings",
            "items": [
                {"aspect": "전체 만족도", "score": _clamp_score10(overall_score, 0)}
            ],
        }
    )

    out = {"title": title, "blocks": blocks, "hashtags": hashtags}
    if candidates:
        out["title_candidates"] = candidates
    if title_target:
        out["title_target"] = title_target
    if title_pick_reason:
        out["title_pick_reason"] = title_pick_reason
    return out


# 후기 '재료' 칸 — DB 컬럼 ↔ 프롬프트에 보일 라벨. 이 **순서가 곧 글의 흐름**이다
# (방문 계기 → 찾아가는 길 → 웨이팅 → 주문·가격 → 하이라이트 → 아쉬운 점 → 조사 정보).
# 레퍼런스 맛집 글 5편을 역산해 뽑은 정보 조각들이고, 전부 선택 입력이다 —
# 모르면 비워 두는 편이 지어내는 것보다 낫다(prompts/blog-review.md 참고).
REVIEW_DETAIL_FIELDS = (
    ("visited_when", "언제 갔는지(요일·시간대)"),
    ("visit_reason", "왜 갔는지(방문 계기)"),
    ("access_note", "찾아가는 길·자리"),
    ("waiting", "웨이팅·입장"),
    ("order_items", "주문한 것과 가격"),
    ("highlight", "가장 기억에 남은 것"),
    ("downside", "아쉬운 점"),
    ("researched", "검색해서 확인한 공개 정보"),
)

# '검색해서 확인한' 칸만 어미가 다르다(~것으로 안내돼 있어요). 재료 블록에서 한 번 더
# 못박아, 모델이 공개 정보를 직접 겪은 것처럼 쓰지 않게 한다.
_RESEARCHED_NOTE = " ← 이건 직접 겪은 게 아니라 검색으로 확인한 공개 정보다"


def build_review_input_block(topic, location, prose, overall_score, details=None):
    """후기 '재료' 블록 문자열 — 값이 있는 칸만 넣는다(빈 칸은 '모른다'는 뜻).

    ``write_review``와 키워드 조사(``suggest_keyword_candidates`` /
    ``select_keywords``)가 **같은 재료**를 보도록 한 곳에서 만든다. 순수 함수.
    """
    topic = (topic or "").strip()
    location = (location or "").strip()
    prose = (prose or "").strip()
    details = details if isinstance(details, dict) else {}
    lines = [f"주제 / 장소: {topic}"]
    if location:
        lines.append(f"위치: {location}")
    for key, label in REVIEW_DETAIL_FIELDS:
        val = (details.get(key) or "").strip()
        if not val:
            continue
        note = _RESEARCHED_NOTE if key == "researched" else ""
        lines.append(f"{label}{note}: {val}")
    lines.append(
        f"사용자가 매긴 총점: {_clamp_score10(overall_score, 0)}/10 "
        "(본문에 숫자로 쓰지는 마라)"
    )
    lines.append(
        "자유 서술(사용자가 직접 쓴 말 — 이 말투와 표현을 최대한 살려라):\n"
        + (prose or "(없음)")
    )
    return "\n".join(lines)


# --- 키워드 조사(노선 2 Step 3) — claude를 두 번 쓴다 -----------------------
# 1) 후보 생성: 후기 재료만 보고 '후기로 직접 답할 수 있는' 롱테일 후보 3~5개.
# 2) 선정:     네이버 블로그 검색·트렌드로 모은 증거를 보고 메인 1 + 보조 2~4.
# 그 사이의 네트워크 조사는 ``keyword_research``가 ``naver_api``로 한다(여긴 AI만).
# 어떤 실패에도 None을 돌려준다 — 조사는 **있으면 좋은 것**이지 생성의 전제가 아니다.
def suggest_keyword_candidates(input_block):
    """재료 블록 → 롱테일 후보 리스트. 실패하면 None(조사를 건너뛴다).

    반환: ``[{"keyword","variants":[..],"question","answerable"}, ...]`` 최대 5개.
    첫 후보는 프롬프트상 시작 검색어(`지역+상호`)다 — 비교 기준선으로 남긴다.
    """
    template = load_prompt("keyword-candidates")
    if not template or not (input_block or "").strip():
        return None
    prompt = template.replace("{{INPUT_BLOCK}}", input_block)
    try:
        data = _extract_json(_run_claude(prompt))
    except Exception as e:  # noqa: BLE001 — 조사는 best-effort
        print(f"[ai] keyword candidates failed: {e}", file=sys.stderr)
        return None
    return normalize_candidates(data)


def normalize_candidates(data):
    """후보 응답을 정규화(순수/오프라인). 못 쓰면 None."""
    if not isinstance(data, dict):
        return None
    out = []
    seen = set()
    for raw in data.get("candidates") or []:
        if isinstance(raw, str):
            raw = {"keyword": raw}
        if not isinstance(raw, dict):
            continue
        kw = (raw.get("keyword") or "").strip()
        if not kw or kw in seen or len(kw) > 60:
            continue
        seen.add(kw)
        variants = []
        for v in raw.get("variants") or []:
            v = str(v).strip()
            if v and v not in variants and len(v) <= 60:
                variants.append(v)
            if len(variants) >= 3:
                break
        if kw not in variants:
            variants.insert(0, kw)
        out.append(
            {
                "keyword": kw,
                "variants": variants[:3],
                "question": (raw.get("question") or "").strip()[:300],
                "answerable": (raw.get("answerable") or "").strip()[:300],
            }
        )
        if len(out) >= 5:
            break
    return out or None


def select_keywords(input_block, evidence_block, candidates):
    """조사 증거 → 메인 1 + 보조 2~4 + 선정·제외 이유. 실패하면 None.

    ``candidates``는 1단계 산출(메인 이름 검증용). 모델이 조사하지 않은 말을 메인으로
    내면 첫 후보로 되돌린다 — 조사 없이 고르는 건 규율 위반이다.
    """
    template = load_prompt("keyword-select")
    if not template or not candidates:
        return None
    prompt = (
        template
        .replace("{{INPUT_BLOCK}}", input_block or "")
        .replace("{{EVIDENCE_BLOCK}}", evidence_block or "(조사 결과 없음)")
    )
    try:
        data = _extract_json(_run_claude(prompt))
    except Exception as e:  # noqa: BLE001
        print(f"[ai] keyword selection failed: {e}", file=sys.stderr)
        return None
    return normalize_selection(data, candidates)


def normalize_selection(data, candidates):
    """선정 응답을 화면·프롬프트가 믿을 수 있는 모양으로 정규화(순수/오프라인).

    규칙:
      * ``main``은 반드시 **조사한 후보 중 하나**. 아니면 첫 후보로 되돌린다.
      * ``supporting``은 메인을 뺀 것 중 최대 4개(없으면 빈 리스트).
      * 설명은 strip + 길이 상한. **지어낸 경쟁 점수 같은 숫자 필드는 받지 않는다**
        (받을 자리를 안 만들면 모델이 만들어 낼 수도 없다).
    """
    if not isinstance(data, dict) or not candidates:
        return None
    names = [c["keyword"] for c in candidates]

    def _s(v, limit=600):
        return (v.strip() if isinstance(v, str) else "")[:limit]

    main = _s(data.get("main"), 60)
    if main not in names:
        main = names[0]
    supporting = []
    for v in data.get("supporting") or []:
        v = _s(v, 60)
        if v and v != main and v not in supporting:
            supporting.append(v)
        if len(supporting) >= 4:
            break
    excluded = []
    for raw in data.get("excluded") or []:
        if isinstance(raw, str):
            raw = {"keyword": raw}
        if not isinstance(raw, dict):
            continue
        kw = _s(raw.get("keyword"), 60)
        if not kw or kw == main:
            continue
        excluded.append({"keyword": kw, "reason": _s(raw.get("reason"), 300)})
        if len(excluded) >= 5:
            break
    return {
        "main": main,
        "main_reason": _s(data.get("main_reason")),
        "supporting": supporting,
        "supporting_reason": _s(data.get("supporting_reason")),
        "excluded": excluded,
        "competition": _s(data.get("competition")),
        "limits": _s(data.get("limits")),
        "angle": _s(data.get("angle")),
    }


# 키워드 조사가 없을 때 blog-review 프롬프트에 들어가는 블록. '조사를 했는데 결과가
# 없다'가 아니라 **'조사를 안 했다'**로 읽히게 쓴다 — 모델이 없는 조사를 지어내
# 인용하지 않도록.
NO_KEYWORD_BLOCK = (
    "(키워드 조사를 하지 않았다 — 네이버 API HUB 키가 없거나 조사에 실패했다.)\n"
    "검색어 조사 결과가 없으니 **검색량·경쟁·노출에 대한 언급을 아예 하지 마라.**\n"
    "재료에 있는 것만 가지고, 위 [제목] 규칙대로 지역·업종·상호가 보이는 제목과\n"
    "자연스러운 방문 흐름의 본문을 써라."
)


def format_keyword_block(research):
    """``keyword_research`` 산출 dict → blog-review 프롬프트에 넣을 텍스트 블록.

    조사가 없거나 선정에 실패했으면 ``NO_KEYWORD_BLOCK``. 순수 함수라 테스트가 쉽다.
    """
    if not isinstance(research, dict):
        return NO_KEYWORD_BLOCK
    sel = research.get("selection")
    if not isinstance(sel, dict) or not sel.get("main"):
        return NO_KEYWORD_BLOCK
    lines = [f"메인 키워드: {sel['main']}"]
    if sel.get("supporting"):
        lines.append("보조 키워드: " + ", ".join(sel["supporting"]))
    if sel.get("angle"):
        lines.append(f"이 키워드로 들어온 독자가 읽어야 할 것: {sel['angle']}")
    if sel.get("main_reason"):
        lines.append(f"선정 이유: {sel['main_reason']}")
    if sel.get("limits"):
        lines.append(f"조사의 한계: {sel['limits']}")
    return "\n".join(lines)


def write_review(topic, location, prose, overall_score, photos, details=None,
                 research=None):
    """후기 재료를 네이버 블로그 글(구조화 JSON)로 만든다 — 프롬프트는 파일 분리.

    ``photos``: ``{"index": i, "caption": <그 사진의 AI 캡션>, "tags": [..]}``의
    순서 리스트(호출부가 순서대로 넘긴다). ``details``: 선택 입력 칸 dict
    (``REVIEW_DETAIL_FIELDS``의 키들 — 없거나 빈 값은 프롬프트에서 통째로 빠진다).
    ``research``: ``keyword_research.research()`` 산출(선택). **없어도 그대로 생성한다**
    — 키워드 조사는 글의 방향을 잡아 주는 보조일 뿐 생성의 전제가 아니다.

    claude를 '한 번'만 호출하고 strict JSON을 파싱해 ``_normalize_review``로 정규화한
    dict를 돌려주거나, 어떤 실패에도 ``None``을 돌려준다(절대 raise 안 함). 느린
    서브프로세스라 호출부가 반드시 백그라운드에서 돌린다.

    v2 기준: 글의 내용 규율은 gf-blog 프롬프트가 정본이다. 표·볼드 금지, 글자 수·
    키워드 반복·소제목 개수 고정 금지, 경험/조사 어미 구분, 요약표·FAQ 고정 섹션
    폐기. 본문 규율 전체는 ``prompts/blog-review.md``에 있고 여기선 재료만 채운다.
    """
    topic = (topic or "").strip()
    if not topic:
        return None
    prose = (prose or "").strip()
    location = (location or "").strip()
    overall_score = _clamp_score10(overall_score, 0)
    photos = photos or []
    details = details or {}

    template = load_prompt("blog-review")
    if not template:
        print("[ai] blog-review prompt unavailable — skip", file=sys.stderr)
        return None

    # --- 재료 블록: 값이 있는 칸만 넣는다(빈 칸은 '모른다'는 뜻이라 아예 안 보인다).
    #     키워드 조사와 **같은 재료**를 보도록 빌더를 공유한다. ---
    input_block = build_review_input_block(
        topic, location, prose, overall_score, details
    )

    # --- 사진 재료 — 각 사진의 index(0-based)·캡션·태그를 그대로 준다. 캡션이 없으면
    #     '(설명 없음)'으로 표시하고, 모델이 지어내지 않도록 규칙에서 못박는다. ---
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
            f"- 사진은 0번부터 {len(photos) - 1}번까지 {len(photos)}장이다. 넣고 싶은 "
            "자리에 image 블록을 두고 photo_index에 그 자리에 가장 잘 맞는 사진 번호"
            "(0-based)를 넣어라. 없는 번호는 절대 쓰지 마라. 모든 사진을 다 넣을 "
            "필요는 없다."
        )
    else:
        photo_block = "(첨부된 사진 없음)"
        photo_rule = "- 첨부된 사진이 없으니 image 블록은 넣지 마라."

    prompt = (
        template
        .replace("{{INPUT_BLOCK}}", input_block)
        .replace("{{PHOTO_BLOCK}}", photo_block)
        .replace("{{PHOTO_RULE}}", photo_rule)
        .replace("{{PHOTO_COUNT}}", str(len(photos)))
        # 키워드 조사 결과(없으면 '조사 안 했다' 블록 — 모델이 없는 조사를 인용하지
        # 않게 한다). 키가 없어도 생성은 그대로 돈다.
        .replace("{{KEYWORD_BLOCK}}", format_keyword_block(research))
    )

    try:
        raw = _run_claude(prompt)
        data = _extract_json(raw)
        result = _normalize_review(data, photos, overall_score)
        if not result:
            raise ValueError("empty review")
        return result
    except Exception as e:  # noqa: BLE001 — degrade gracefully, never raise
        print(f"[ai] blog review generation failed: {e}", file=sys.stderr)
        return None


# --- 썸네일 카피(노선 2 Step 4) ---------------------------------------------
# 글이 **완성된 뒤** 한 번 더 claude를 쓴다. 그래서 초안 생성 경로에 끼지 않는다 —
# 사용자가 상세 화면에서 요청할 때만 돈다(초안 생성 시간을 1초도 늘리지 않는다).
# 그림은 AI가 그리지 않는다. 카피만 쓰고, 렌더는 브라우저가 template.css 수치로 한다
# (gf-blog [썸네일 카피 생성] 7·8번 — AI 이미지 생성·유사 글꼴 금지).
_THUMB_BODY_CHARS = 2600


def suggest_thumbnail_copy(input_block, title, body_text, research=None,
                           budget=None):
    """완성된 글 → 썸네일 카피 후보 3개 + 선정. 실패하면 ``None``(기능만 꺼진다).

    ``budget``은 ``thumbnail.copy_budget()`` 산출(상자별 대략의 글자 예산)이고
    프롬프트의 길이 안내로만 들어간다 — **넘침의 판정은 브라우저 실측**이다.
    반환은 ``thumbnail.normalize_copy()``가 정규화한 dict.
    """
    import thumbnail  # 지연 import — thumbnail은 ai를 쓰지 않는다(순환 없음)

    template = load_prompt("thumbnail-copy")
    if not template:
        print("[ai] thumbnail-copy prompt unavailable — skip", file=sys.stderr)
        return None
    budget = budget or {}
    prompt = (
        template
        .replace("{{INPUT_BLOCK}}", (input_block or "").strip() or "(없음)")
        .replace("{{TITLE}}", (title or "").strip() or "(제목 없음)")
        .replace("{{POST_BODY}}", (body_text or "").strip()[:_THUMB_BODY_CHARS]
                 or "(본문 없음)")
        .replace("{{KEYWORD_BLOCK}}", format_keyword_block(research))
        .replace("{{MAIN_BUDGET}}", str(budget.get("main_line", 9)))
        .replace("{{SUB_BUDGET}}", str(budget.get("sub", 12)))
        .replace("{{BADGE_BUDGET}}", str(budget.get("badge", 7)))
    )
    try:
        data = _extract_json(_run_claude(prompt))
    except Exception as e:  # noqa: BLE001 — 썸네일은 글 생성의 전제가 아니다
        print(f"[ai] thumbnail copy failed: {e}", file=sys.stderr)
        return None
    return thumbnail.normalize_copy(data)
