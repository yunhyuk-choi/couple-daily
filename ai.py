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
        "- 정확히 한 문장, 물음표로 끝나고, 어느 쪽이 읽어도 자연스러운 다정한 반말체.\n\n"
        "지난 대화는 오직 '중복 회피'용 참고 자료야(내용 인용 금지):\n"
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
