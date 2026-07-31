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
import re
import subprocess
import sys

CLAUDE_TIMEOUT = 120  # seconds; `claude -p` is an agent and can be slow
# Vision (reading an image off disk) is markedly slower than a text prompt on
# the 0.1-CPU free tier — give it generous headroom so it isn't killed mid-read.
CAPTION_TIMEOUT = 180

# Gentle fallbacks used only if the CLI is unavailable / errors out.
FALLBACK_QUESTIONS = [
    "오늘 하루 중 가장 마음이 따뜻해졌던 순간은 언제였어?",
    "요즘 서로에게 가장 고마웠던 일은 뭐야?",
    "우리가 함께 가장 크게 웃었던 최근 순간을 떠올려볼래?",
    "오늘 너를 가장 힘들게 한 건 뭐였어? 내가 어떻게 도와주면 좋을까?",
    "우리가 다음에 꼭 같이 해보고 싶은 소소한 일 하나는?",
]


def _run_claude(prompt: str, timeout: int = CLAUDE_TIMEOUT) -> str:
    """Run `claude -p`, feeding the prompt via stdin. Returns raw stdout text.

    Raises RuntimeError on non-zero exit / timeout / missing binary.
    """
    try:
        proc = subprocess.run(
            ["claude", "-p"],
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
def generate_daily_question(recent_pairs):
    """Generate one warm daily question, personalized from recent answers.

    recent_pairs: list of dicts {question, answers: [text, ...]} (most recent
    first), used to steer the new question. Returns (text, source) where source
    is 'ai' or 'fallback'.
    """
    history_lines = []
    for i, p in enumerate(recent_pairs[:8], 1):
        ans = " / ".join(a for a in p.get("answers", []) if a)
        history_lines.append(f"{i}. Q: {p['question']}\n   A: {ans or '(답변 없음)'}")
    history_block = "\n".join(history_lines) if history_lines else "(아직 지난 답변이 없음)"

    prompt = (
        "너는 연인 두 사람을 위한 '오늘의 질문'을 만드는 다정한 도우미야.\n"
        "두 사람이 서로를 더 알아가고 대화를 나눌 수 있는, 따뜻하고 구체적인 질문을 "
        "딱 하나 한국어로 만들어줘.\n"
        "규칙:\n"
        "- 반말체의 다정한 말투.\n"
        "- 너무 무겁거나 사생활을 캐묻는 질문은 피하고, 답하기 편안한 질문.\n"
        "- 아래 지난 대화가 있으면 그 흐름/관심사를 자연스럽게 이어가되, 똑같은 질문은 반복하지 마.\n"
        "- 한 문장, 물음표로 끝나게.\n\n"
        f"[최근 지난 질문과 답변]\n{history_block}\n\n"
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


def judge_fight(name_a, name_b, situation, statements):
    """Judge a couple's fight with `claude`, returning a normalized verdict dict.

    ``statements`` is a list of ``{"name": <display_name>, "text": <진술>}`` with
    1 or 2 entries (a partner who left no statement is simply absent). Builds a
    warm-but-fair Korean prompt, runs `claude -p`, parses strict JSON, and returns
    a normalized dict — or ``None`` on ANY failure (never raises), exactly like
    ``generate_monthly_qualitative``. Because it runs the slow agent subprocess,
    the caller MUST invoke it from a background thread, never on the request path.

    Returns on success::

        {"fault": [{"name": name_a, "percent": int}, {"name": name_b, "percent": int}],
         "summary": str, "reason": str, "resolution": str, "note": str}
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
        f"너는 연인 두 사람({name_a}, {name_b})의 다툼을 판결하는, 다정하지만 공정한 "
        "'AI 판사'야. 재미있고 따뜻하게, 그러나 어느 한쪽으로 치우치지 않고 균형 있고 "
        "솔직하게 판결해.\n\n"
        "아래의 상황 설명과, 있는 만큼의 각자 진술만을 근거로 판단해. 지어내지 마.\n\n"
        f"[상황 설명]\n{(situation or '').strip()}\n\n"
        f"[각자 진술]\n{stmt_block}\n\n"
        "판결 규칙:\n"
        f"- 잘못 비율(fault)은 {name_a}와 {name_b} 두 사람 것을 합쳐 정확히 100이 되게, "
        "근거 있게 배분해.\n"
        + (
            "- 지금은 한 사람의 진술만 있어. 그 한계를 감안해서 신중하게 판단하고, "
            "note에 '한쪽 진술만 있어 판단에 한계가 있다'는 취지를 부드럽게 한 문장으로 "
            "적어줘.\n"
            if only_one
            else "- 두 사람의 진술을 모두 고려해서 공평하게 판단해.\n"
        )
        + "- 금지: 모욕, 인신공격, 한쪽 편만 들도록 부추기는 말, 과격하거나 비난하는 말투.\n"
        "- 목표는 관계 회복이야. 구체적이고 실천 가능한 화해 방법을 제시해.\n\n"
        "출력은 반드시 아래 JSON 객체 하나만, 다른 텍스트/설명/코드펜스 없이:\n"
        "{\n"
        f'  "fault": [{{"name": "{name_a}", "percent": <정수>}}, '
        f'{{"name": "{name_b}", "percent": <정수>}}],\n'
        '  "summary": "<한 줄 판결 요지>",\n'
        '  "reason": "<양측을 고려한 판결 이유, 2~4문장>",\n'
        '  "resolution": "<두 사람을 위한 구체적 화해/해결 제안, 2~3문장>",\n'
        '  "note": "<한쪽만 진술 등 한계가 있으면 한 문장, 없으면 빈 문자열>"\n'
        "}"
    )
    try:
        raw = _run_claude(prompt)
        data = _extract_json(raw)
        fault = _normalize_fault(data.get("fault"), name_a, name_b)
        if not fault:
            raise ValueError("no usable fault data")
        return {
            "fault": fault,
            "summary": (data.get("summary") or "").strip(),
            "reason": (data.get("reason") or "").strip(),
            "resolution": (data.get("resolution") or "").strip(),
            "note": (data.get("note") or "").strip(),
        }
    except Exception as e:  # noqa: BLE001 — degrade gracefully, never raise
        print(f"[ai] fight judgment failed: {e}", file=sys.stderr)
        return None
