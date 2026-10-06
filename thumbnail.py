"""썸네일 — Figma 내보내기 CSS(값 보존) 파싱 + 카피 정규화 + 렌더 검증 (노선 2 Step 4).

**정본은 gf-blog다.** 디자인 수치는 `design/thumbnail-template.css`(그쪽
`썸네일/template.css`를 그대로 가져온 파일)이고, 이 모듈은 그 파일을 *파싱할 뿐*
숫자를 다시 적지 않는다 — gf-blog `{맛집명}/thumbnail/render.py`가 쓰던
``position:`` 기준 쪼개기를 그대로 옮겼다(선택자만 붙이고 **값은 보존**).

### 렌더는 어디서 도는가 — 서버가 아니라 **보는 사람의 브라우저**다

gf-blog는 Playwright + Chrome headless로 렌더했다. 우리는 Render 무료티어
(512MB · 0.1 CPU)라 Chromium을 못 올린다(사용자 명시 배제). 대신 **이미 있는
인앱 미리보기와 같은 자리** — 사용자의 브라우저 — 에서 렌더한다:

  * DOM 미리보기가 이 CSS 값 그대로 1080×1350 박스를 만든다 → **진짜 브라우저가
    레이아웃을 계산한다.** 넘침 검증은 gf-blog와 **같은 식**(``scrollWidth <=
    clientWidth``)을 그 DOM에서 그대로 친다.
  * 래스터화는 Canvas 2D가 같은 수치로 그린다(외부 라이브러리 없음).
  * 폰트는 ``document.fonts.check()``로 확인하고, 실패하면 **렌더를 거부한다**
    (다른 폰트로 조용히 대체하면 검증된 디자인이 아니게 된다).

서버는 그래서 픽셀을 만들지 않는다. 서버가 하는 일은 (1) 이 CSS에서 스펙을 뽑아
클라이언트에 **단일 원천**으로 넘기고, (2) 클라이언트가 보고한 측정값을
**다시 계산해** 넘침 여부를 판정·기록하는 것이다(클라이언트의 ``ok`` 주장을 믿지
않는다). 판정 결과는 gf-blog의 ``render-check.json``에 해당한다.

### 규율 (gf-blog에서 그대로 가져온 것)

* **카피가 넘치면 폰트를 줄이지 않는다 — 문구를 줄인다.** 그래서 이 모듈 어디에도
  "자동 축소" 경로가 없다. 넘치면 ``ok=False``이고 내려받기가 막힌다.
* AI 이미지 생성 금지. 사진은 사용자가 올린 실제 사진만 쓴다.
* 카피 형식: ``~~한 000`` / ``~~할 때 가기 좋은 000``. 후보 3개 → 1개 선정.
"""
import json
import os
import re

# Figma 내보내기 원본(gf-blog 썸네일/template.css 사본). **이 파일이 수치의 정본**이다.
TEMPLATE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "design", "thumbnail-template.css"
)

# 카피 네 칸. 화면·프롬프트·정규화가 이 순서를 공유한다.
COPY_FIELDS = ("main_line1", "main_line2", "sub", "badge")

# 한 칸의 하드 상한(문자). **이건 넘침 판정이 아니다** — 터무니없는 입력만 막는 안전장치다.
# 진짜 판정은 브라우저가 실제로 그려 보고 재는 것(evaluate_render_check)이다. 글자폭은
# 글자마다 달라서(한글 1em · 공백 0.3em · 라틴 0.5em) 글자 수로 가를 수 없다.
MAX_COPY_CHARS = 40

# 프롬프트에 적어 줄 **대략의 글자 예산** — 상자 폭 ÷ 폰트 크기(한글 1글자 ≈ 1em).
# 어디까지나 안내이고, 넘치면 재는 쪽이 이긴다.
_HANGUL_EM = 1.0


class TemplateError(RuntimeError):
    """템플릿 CSS를 읽거나 쪼갤 수 없다 — 썸네일 기능을 끈다(앱은 죽지 않는다)."""


# --------------------------------------------------------------------------- #
# 1. Figma 내보내기 CSS 쪼개기 — gf-blog render.py와 **같은 알고리즘**
# --------------------------------------------------------------------------- #
def split_blocks(css):
    """선택자 없는 Figma 내보내기를 5개 선언 블록으로 쪼갠다.

    gf-blog ``render.py``가 하던 그대로다: 주석을 걷고 ``position:`` 등장 위치로
    자른 뒤, 배지(auto layout)는 ``display: flex;``와 라벨 시작(``width: 270px;``)에서
    한 번 더 가른다. **선언 문자열은 손대지 않는다**(값 보존).

    반환: ``{"canvas","gradient","main","sub","badge","badge_label"}`` → 선언 문자열.
    """
    clean = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    pos = list(re.finditer(r"position:\s*(?:relative|absolute);", clean))
    if len(pos) < 5:
        raise TemplateError(
            f"template css: position 블록이 5개여야 하는데 {len(pos)}개다"
        )
    canvas = clean[pos[0].start(): pos[1].start()]
    gradient = clean[pos[1].start(): pos[2].start()]
    main = clean[pos[2].start(): pos[3].start()]
    sub_and_badge = clean[pos[3].start(): pos[4].start()]
    if "display: flex;" not in sub_and_badge:
        raise TemplateError("template css: 배지 auto layout(display: flex)을 못 찾았다")
    sub, badge_flex = sub_and_badge.split("display: flex;", 1)
    rest = clean[pos[4].start():]
    label_at = rest.find("width: 270px;")
    if label_at == -1:
        raise TemplateError("template css: 배지 라벨 시작(width: 270px)을 못 찾았다")
    badge = "display: flex;" + badge_flex + rest[:label_at]
    label = rest[label_at:]
    # 캔버스의 Figma 체커보드 배경(url(Checker.png))은 디자인이 아니라 플레이스홀더다.
    canvas = re.sub(r"background:\s*url\([^)]*\);", "", canvas)
    return {
        "canvas": canvas,
        "gradient": gradient,
        "main": main,
        "sub": sub,
        "badge": badge,
        "badge_label": label,
    }


def declarations(block):
    """선언 블록 문자열 → ``{속성: 값}`` dict (순서·값 그대로)."""
    out = {}
    for stmt in block.split(";"):
        stmt = stmt.strip()
        if not stmt or ":" not in stmt:
            continue
        prop, _, val = stmt.partition(":")
        out[prop.strip()] = val.strip()
    return out


def _px(decls, prop, default=None):
    raw = decls.get(prop)
    if raw is None:
        if default is None:
            raise TemplateError(f"template css: '{prop}' 선언이 없다")
        return default
    m = re.match(r"^(-?\d+(?:\.\d+)?)px$", raw.strip())
    if not m:
        if default is None:
            raise TemplateError(f"template css: '{prop}: {raw}'를 px로 못 읽었다")
        return default
    v = float(m.group(1))
    return int(v) if v.is_integer() else v


def parse_gradient(value):
    """``linear-gradient(180deg, rgba(..) 0%, rgba(..) 81.73%)`` → 정지점 리스트.

    반환: ``{"css": <원문>, "angle": 180, "stops": [{"rgba": [r,g,b,a], "at": 0.0}, ..]}``.
    값은 **원문 그대로** 보존하고, 캔버스가 쓸 수 있게 숫자로도 같이 낸다.
    """
    raw = (value or "").strip()
    m = re.match(r"^linear-gradient\((.*)\)$", raw, flags=re.S)
    if not m:
        raise TemplateError(f"template css: 그라데이션을 못 읽었다 — {raw[:60]!r}")
    body = m.group(1)
    angle_m = re.match(r"\s*(-?\d+(?:\.\d+)?)deg\s*,", body)
    angle = float(angle_m.group(1)) if angle_m else 180.0
    stops = []
    for cm in re.finditer(
        r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)\s*(?:,\s*([\d.]+)\s*)?\)"
        r"\s*([\d.]+)%",
        body,
    ):
        r, g, b, a, at = cm.groups()
        stops.append(
            {
                "rgba": [float(r), float(g), float(b), float(a) if a is not None else 1.0],
                "at": float(at) / 100.0,
            }
        )
    if len(stops) < 2:
        raise TemplateError("template css: 그라데이션 정지점이 2개 미만이다")
    return {"css": raw, "angle": angle, "stops": stops}


def _text_box(decls, with_position=True):
    box = {
        "width": _px(decls, "width"),
        "height": _px(decls, "height"),
        "font_family": (decls.get("font-family") or "").strip().strip("'\""),
        "font_weight": int(decls.get("font-weight") or 400),
        "font_size": _px(decls, "font-size"),
        "line_height": _px(decls, "line-height"),
        "color": (decls.get("color") or "").strip(),
    }
    if with_position:
        box["left"] = _px(decls, "left")
        box["top"] = _px(decls, "top")
    return box


def build_spec(css):
    """템플릿 CSS 문자열 → 클라이언트가 쓸 스펙 dict (순수 함수).

    **여기서 숫자를 지어내지 않는다** — 전부 CSS에서 읽은 값이다. 브라우저 DOM
    미리보기와 Canvas 래스터화가 *같은* 이 dict를 보기 때문에 둘이 어긋날 수 없다.
    """
    blocks = split_blocks(css)
    d = {k: declarations(v) for k, v in blocks.items()}

    canvas = {"width": _px(d["canvas"], "width"), "height": _px(d["canvas"], "height")}
    gradient = parse_gradient(d["gradient"].get("background"))
    gradient["width"] = _px(d["gradient"], "width")
    gradient["height"] = _px(d["gradient"], "height")
    gradient["left"] = _px(d["gradient"], "left")
    gradient["top"] = _px(d["gradient"], "top")

    main = _text_box(d["main"])
    sub = _text_box(d["sub"])

    badge_radius = d["badge"].get("border-radius", "")
    radius_m = re.match(r"^(\d+(?:\.\d+)?)px$", badge_radius.strip())
    badge = {
        "left": _px(d["badge"], "left"),
        "top": _px(d["badge"], "top"),
        "width": _px(d["badge"], "width"),
        "height": _px(d["badge"], "height"),
        "padding": _px(d["badge"], "padding", 0),
        "background": (d["badge"].get("background") or "").strip(),
        "radius": float(radius_m.group(1)) if radius_m else 0.0,
    }
    label = _text_box(d["badge_label"], with_position=False)

    spec = {
        "canvas": canvas,
        "gradient": gradient,
        "main": main,
        "sub": sub,
        "badge": badge,
        "badge_label": label,
        # 선언 원문도 같이 넘긴다 — DOM 미리보기가 **CSS 그대로** 붙여 쓴다
        # (gf-blog가 선택자만 붙이고 값은 보존한 것과 같은 방식).
        "css": blocks,
    }
    spec["budget"] = copy_budget(spec)
    return spec


def copy_budget(spec):
    """상자별 **대략의** 한글 글자 예산 — 프롬프트 안내용(판정 아님).

    상자 폭 ÷ 폰트 크기. 한글 한 글자가 대략 1em이라는 어림이고, 공백·라틴은 더
    좁아서 실제로는 조금 더 들어간다. **넘침의 판정은 언제나 실측이다.**
    """
    def _n(box):
        return int(box["width"] // (box["font_size"] * _HANGUL_EM))
    return {
        "main_line": _n(spec["main"]),
        "sub": _n(spec["sub"]),
        "badge": _n(spec["badge_label"]),
        "main_lines": max(1, int(spec["main"]["height"] // spec["main"]["line_height"])),
    }


_SPEC_CACHE = {}


def spec():
    """템플릿 CSS를 읽어 스펙을 돌려준다(프로세스 내 캐시). 실패하면 ``None``.

    프롬프트 로더(`ai.load_prompt`)와 같은 태도다 — 파일이 없다고 앱이 죽으면 안 되고,
    호출부가 ``None``을 보고 기능만 끈다.
    """
    cached = _SPEC_CACHE.get("spec")
    if cached is not None:
        return cached
    try:
        with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
            css = f.read()
        built = build_spec(css)
    except (OSError, TemplateError, ValueError) as e:
        print(f"[thumbnail] template spec unavailable: {e}")
        return None
    _SPEC_CACHE["spec"] = built
    return built


# --------------------------------------------------------------------------- #
# 2. 카피 정규화 — 후보 3개 + 선정 이유
# --------------------------------------------------------------------------- #
def _clean_line(v, limit=MAX_COPY_CHARS):
    """한 줄로 눌러 담는다 — 줄바꿈·연속 공백 제거 + 하드 상한."""
    if not isinstance(v, str):
        return ""
    return re.sub(r"\s+", " ", v).strip()[:limit]


def normalize_copy(data):
    """AI 응답 → 화면이 믿을 수 있는 모양으로 (순수/오프라인). 못 쓰면 ``None``.

    규칙:
      * 후보는 **최대 3개**, ``main_line1``·``main_line2``가 둘 다 있어야 후보로 센다
        (두 줄 메인이 이 디자인의 전제다 — 한 줄이면 레이아웃이 비어 보인다).
      * ``picked``는 후보 범위 안으로 강제한다(범위 밖·누락이면 0).
      * **폰트 크기·좌표 같은 디자인 필드는 받지 않는다.** 받을 자리를 안 만들어야
        모델이 "폰트를 줄여서 맞췄다"를 할 수 없다 — 넘치면 문구를 줄이는 게 규율이다.
    """
    if not isinstance(data, dict):
        return None
    cands = []
    for raw in data.get("candidates") or []:
        if not isinstance(raw, dict):
            continue
        c = {f: _clean_line(raw.get(f)) for f in COPY_FIELDS}
        if not (c["main_line1"] and c["main_line2"]):
            continue
        c["reason"] = _clean_line(raw.get("reason"), 300)
        cands.append(c)
        if len(cands) >= 3:
            break
    if not cands:
        return None
    try:
        picked = int(data.get("picked", 0))
    except (TypeError, ValueError):
        picked = 0
    if not (0 <= picked < len(cands)):
        picked = 0
    return {
        "candidates": cands,
        "picked": picked,
        "pick_reason": _clean_line(data.get("pick_reason"), 400),
        # 지금 쓰는 문구 = 선정 후보의 사본. 사람이 화면에서 줄이면 여기만 바뀐다
        # (후보 원문은 '무엇을 줄였나'를 보려고 그대로 남긴다).
        "copy": {f: cands[picked][f] for f in COPY_FIELDS},
    }


def apply_pick(state, picked=None, copy=None, photo_index=None):
    """사람이 화면에서 고르거나 줄인 결과를 상태 dict에 반영(순수 함수).

    ``picked``를 바꾸면 ``copy``는 그 후보로 리셋된다(옛 후보의 편집본이 남아
    화면과 어긋나는 걸 막는다 — 제목 후보 교체가 ``edited_text``를 비우는 것과 같은 결).
    ``copy``가 함께 오면 그 값이 최종이다.
    """
    if not isinstance(state, dict) or not state.get("candidates"):
        return state
    cands = state["candidates"]
    if picked is not None:
        try:
            idx = int(picked)
        except (TypeError, ValueError):
            idx = state.get("picked", 0)
        if 0 <= idx < len(cands) and idx != state.get("picked"):
            state["picked"] = idx
            state["copy"] = {f: cands[idx][f] for f in COPY_FIELDS}
        elif 0 <= idx < len(cands):
            state["picked"] = idx
    if isinstance(copy, dict):
        cur = dict(state.get("copy") or {})
        for f in COPY_FIELDS:
            if f in copy:
                cur[f] = _clean_line(copy.get(f))
        # 메인 두 줄은 비울 수 없다 — 비우면 직전 값을 지킨다.
        for f in ("main_line1", "main_line2"):
            if not cur.get(f):
                cur[f] = (state.get("copy") or {}).get(f, "")
        state["copy"] = cur
        # 문구가 바뀌었으니 직전 렌더 검증은 더 이상 이 문구의 것이 아니다.
        state.pop("render_check", None)
    if photo_index is not None:
        try:
            state["photo_index"] = max(0, int(photo_index))
        except (TypeError, ValueError):
            pass
    return state


# --------------------------------------------------------------------------- #
# 3. 렌더 검증 — gf-blog render-check.json 대응
# --------------------------------------------------------------------------- #
# 넘침을 재는 상자들. gf-blog는 main·sub·badge-label 셋을 assert 했다 — 그대로다.
MEASURED_BOXES = ("main", "sub", "badge_label")


def evaluate_render_check(raw):
    """브라우저가 보낸 측정값을 **서버가 다시 판정**한다. ``None``이면 못 믿을 입력.

    클라이언트가 보낸 ``ok`` 같은 결론은 **읽지 않는다** — 상자별
    ``scrollWidth``/``clientWidth``와 폰트 확인 결과만 받아 여기서 계산한다
    (gf-blog의 ``assert scrollWidth <= clientWidth`` + ``document.fonts.check``와
    같은 판정이고, 믿는 것은 '측정값'이지 '주장'이 아니다).

    반환 예::

        {"ok": False, "fonts_ok": True,
         "boxes": {"main": {"x":90,"y":935,"width":900,"height":300,
                            "scrollWidth":1012,"clientWidth":900,
                            "overflow": True, "font": "700 95px Pretendard"}, ...},
         "overflowing": ["main"]}
    """
    if not isinstance(raw, dict):
        return None
    boxes_in = raw.get("boxes")
    if not isinstance(boxes_in, dict):
        return None
    boxes = {}
    overflowing = []
    for name in MEASURED_BOXES:
        b = boxes_in.get(name)
        if not isinstance(b, dict):
            return None  # 재야 할 상자를 안 쟀다 = 검증이 아니다
        try:
            sw = float(b.get("scrollWidth"))
            cw = float(b.get("clientWidth"))
        except (TypeError, ValueError):
            return None
        if cw <= 0:
            return None
        over = sw > cw + 0.5  # 서브픽셀 반올림 여유
        rec = {
            "scrollWidth": round(sw, 2),
            "clientWidth": round(cw, 2),
            "overflow": over,
            "font": _clean_line(b.get("font"), 120),
        }
        for k in ("x", "y", "width", "height"):
            try:
                rec[k] = round(float(b.get(k)), 2)
            except (TypeError, ValueError):
                pass
        boxes[name] = rec
        if over:
            overflowing.append(name)
    fonts_ok = bool(raw.get("fonts_ok"))
    return {
        "ok": fonts_ok and not overflowing,
        "fonts_ok": fonts_ok,
        "boxes": boxes,
        "overflowing": overflowing,
        "font_checks": [
            _clean_line(s, 120) for s in (raw.get("font_checks") or [])[:6]
            if isinstance(s, str)
        ],
    }


def render_check_json(state):
    """상태의 ``render_check``를 gf-blog ``render-check.json``과 같은 모양의 문자열로.

    사람이 화면에서 '측정값 보기'로 펼쳐 보는 그대로이고, 어디에도 자격증명이 없다.
    """
    rc = (state or {}).get("render_check")
    if not isinstance(rc, dict):
        return ""
    return json.dumps(rc, ensure_ascii=False, indent=2)
