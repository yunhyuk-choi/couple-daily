"""본문 강조 서식(굵게·형광펜·2색) 회귀 테스트.

**왜 이 파일이 있나.** 강조는 한 번 사라진 적이 있다. `0e862f2`가 넣은 강조 토큰
지시(`**굵게**` `==형광펜==` `*핑크*` `` `파랑` ``)를, 프롬프트를 gf-blog 쪽으로
통째로 교체한 `5a45ead`가 **"볼드·굵게·형광펜·색 강조를 쓰지 마라"**로 덮어썼다.
변환기(`_emph_html`·북마클릿 `emphNodes`)는 멀쩡히 살아 있었는데 모델이 토큰을
내보내지 않으니 결과물에서 강조가 통째로 증발했다 — 조용한 회귀였다.

그래서 여기서 **세 겹**을 전부 못질한다. 한 겹만 깨져도 같은 증발이 다시 난다:

  (1) 프롬프트가 강조를 **지시**하는가            — 빠지면 모델이 토큰을 안 낸다
  (2) 서버 변환기가 토큰을 **살려서 HTML로** 내는가 — 빠지면 파싱에서 탈락한다
  (3) 미리보기·발행본·네이티브 발행이 **같은 서식**인가 — 한쪽만 고치면 화면과
      실제 글이 달라진다

색은 **분홍(#e64980)·파랑(#1c7ed6) 둘뿐**이다(사용자 지시 "2색정도"). 형광펜은
글자색이 아니라 배경(#ffec99)이라 별도다. 이 세 값은 2026-08 `0e862f2` 팔레트
그대로이고, 네이버 쪽(북마클릿 `nodeStyle`)과 **같은 값이어야 한다**.
"""
import os
import re
import shutil
import subprocess
import sys

import pytest

import ai
import app as app_module

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BOOKMARKLET = os.path.join(_ROOT, "tools", "naver-bookmarklet.js")

PINK = "#e64980"      # 색 1 — '무엇'(상호·메뉴)
BLUE = "#1c7ed6"      # 색 2 — '어디·언제·얼마'(위치·시간·가격)
MARK = "#ffec99"      # 형광펜 배경(글자색이 아니다)


# --------------------------------------------------------------------------- #
# (1) 프롬프트가 강조를 지시한다
# --------------------------------------------------------------------------- #
def test_prompt_instructs_the_four_emphasis_tokens():
    """⛔ 회귀의 진원지. 이 지시가 빠지면 모델이 토큰을 아예 안 낸다."""
    body = ai.load_prompt("blog-review")
    assert body
    # 네 가지 표시가 전부 설명돼 있다.
    assert "**15분**" in body                 # 굵게
    assert "==형광펜==" in body               # 형광펜
    assert "*문래갈매기*" in body             # 분홍 글자
    assert "`6층 안쪽`" in body               # 파랑 글자
    # 금지문이 되살아나면(= 프롬프트를 다시 교체하면) 여기서 깨진다.
    assert "볼드·굵게·형광펜·색 강조를 쓰지 마라" not in body


def test_prompt_caps_emphasis_without_mechanical_quota():
    """'문단마다 1개' 같은 고정 규칙은 사람 글로 안 보인다 — 상한만 둔다."""
    body = ai.load_prompt("blog-review")
    assert "의미상 중요한 곳에만 써라" in body
    assert "기계적으로 박으면" in body
    assert "10곳 안팎" in body                 # 상한은 있다
    assert "상한이지 할당량이 아니다" in body   # 할당량은 아니다


def test_prompt_keeps_two_colors_and_sibling_discipline():
    """색은 2색까지. gf-blog 에서 가져온 나머지 규율(표·이모지 금지)은 그대로."""
    body = ai.load_prompt("blog-review")
    assert "색은 분홍·파랑 둘뿐이다" in body
    # 자매 레포 규율 중 **표·이모지 금지는 유지**한다(강조만 되살렸다).
    assert "표를 쓰지 마라" in body
    assert "이모지" in body
    # 소제목·해시태그는 시스템이 꾸민다 — 거기엔 강조를 쓰지 않는다.
    assert "소제목(`heading`)·해시태그에는 쓰지 마라" in body


# --------------------------------------------------------------------------- #
# (2) 서버 변환기가 토큰을 살려 낸다
# --------------------------------------------------------------------------- #
def test_emph_html_maps_every_token():
    html = app_module._emph_html(
        "먼저 온 팀이 3팀, **15분** 기다렸어요. "
        "==양념으로 시키세요== *문래갈매기* 는 `7번 출구` 3분."
    )
    assert "<b>15분</b>" in html
    assert f'background-color:{MARK}' in html and "양념으로 시키세요" in html
    assert f'color:{PINK}' in html and "문래갈매기" in html
    assert f'color:{BLUE}' in html and "7번 출구" in html


def test_emph_html_uses_exactly_two_font_colors():
    """사용자 지시: '2색정도'. 색이 늘어나면 깨진다."""
    src = app_module._emph_html("*가* `나` **다** ==라==")
    colors = set(re.findall(r"(?<!background-)color:(#[0-9a-fA-F]{6})", src))
    assert colors == {PINK, BLUE}, colors


def test_emph_html_escapes_before_emphasizing():
    """강조를 되살리면서 주입 구멍을 열지 않는다."""
    html = app_module._emph_html('**<script>alert(1)</script>**')
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_strip_emph_leaves_plain_text():
    """text/plain 폴백에는 토큰이 남지 않는다."""
    assert app_module._strip_emph("**15분** ==꼭== *상호* `위치`") == "15분 꼭 상호 위치"


# --------------------------------------------------------------------------- #
# (3) 미리보기 · 발행본 · 네이티브 발행이 같은 서식
# --------------------------------------------------------------------------- #
_AI_WITH_EMPHASIS = {
    "title": "문래갈매기 웨이팅 감안해도 다시 갈 집",
    "blocks": [
        {"type": "para",
         "text": "먼저 온 팀이 3팀 있었고 실제로는 **15분** 정도 기다렸어요.\n"
                 "*문래갈매기* 는 `문래역 7번 출구` 에서 3분 거리예요."},
        {"type": "heading", "text": "갈매기살은 양념으로"},
        {"type": "para", "text": "==양념으로 꼭 시키세요== 소금구이와 결이 달랐어요."},
        {"type": "quote", "text": "**다시 생각나는** 집이에요."},
    ],
    "hashtags": ["#문래갈매기"],
}


def _ready(make_review):
    import json
    return make_review(
        ai_json=json.dumps(_AI_WITH_EMPHASIS, ensure_ascii=False), status="ready"
    )


def test_copy_html_carries_every_emphasis_through_the_builder(flask_app, make_review):
    """⛔ 생성 경로를 타고 나온 **발행본**에 네 서식이 전부 살아 있어야 한다."""
    html = app_module._review_copy_html(_ready(make_review))
    assert html
    assert "<b>15분</b>" in html
    assert f"color:{PINK}" in html and "문래갈매기" in html
    assert f"color:{BLUE}" in html and "문래역 7번 출구" in html
    assert f"background-color:{MARK}" in html and "양념으로 꼭 시키세요" in html
    assert "<b>다시 생각나는</b>" in html          # quote 블록도 통과한다
    # 토큰 원문이 그대로 새어 나가면 안 된다(변환이 안 된 것).
    assert "**" not in html and "==" not in html


def test_headings_stay_plain(flask_app, make_review):
    """소제목 장식은 시스템 몫 — 거기에 강조가 끼면 두 번 꾸며진다."""
    html = app_module._review_copy_html(_ready(make_review))
    assert "<h3" in html and "갈매기살은 양념으로" in html
    open_tag = html.index("<h3")
    inner = html[html.index(">", open_tag) + 1:html.index("</h3>")]
    assert inner.strip() == "갈매기살은 양념으로"   # 태그 하나 없는 평문


def test_title_stays_plain_in_both_paths(flask_app, make_review):
    """제목은 평문이다 — 북마클릿 `documentTitle` 은 emphNodes 를 안 거치므로,
    HTML 쪽만 서식을 입히면 화면 제목과 실제 글 제목이 갈라진다.
    """
    import json
    payload = dict(_AI_WITH_EMPHASIS, title="**문래갈매기** 다녀온 `후기`")
    review = make_review(
        ai_json=json.dumps(payload, ensure_ascii=False), status="ready"
    )
    html = app_module._review_copy_html(review)
    h2 = html[html.index("<h2"):html.index("</h2>")]
    inner = h2[h2.index(">") + 1:]
    assert inner == "문래갈매기 다녀온 후기"     # 토큰도 태그도 없다


def test_screen_html_shows_the_same_emphasis_as_publish(flask_app, make_review):
    """미리보기와 발행본이 **같은 서식**이어야 한다(이미지 자리만 다르다)."""
    publish = app_module._review_copy_html(_ready(make_review))
    screen = app_module._review_screen_html(publish)
    for needle in ("<b>15분</b>", f"color:{PINK}", f"color:{BLUE}",
                   f"background-color:{MARK}", "<b>다시 생각나는</b>"):
        assert needle in screen, needle
    # 화면본이 바꾸는 건 <img> 뿐이다 — 강조 마크업 개수가 같아야 한다.
    assert screen.count("<b>") == publish.count("<b>")
    assert screen.count("<span style=") == publish.count("<span style=")


def test_copy_text_drops_tokens_but_keeps_words(flask_app, make_review):
    text = app_module._review_copy_text(_ready(make_review))
    assert "15분" in text and "양념으로 꼭 시키세요" in text
    assert "**" not in text and "==" not in text


def test_export_doc_keeps_raw_tokens_for_native_publish(flask_app, make_review):
    """네이티브 발행(setDocumentData)은 **원문 토큰**을 받아 nodeStyle 로 바꾼다.

    여기서 토큰이 깎이면 북마클릿의 `emphNodes` 가 볼 게 없어져 발행본만
    밋밋해진다(화면은 멀쩡해서 더 늦게 들킨다).
    """
    doc_data, _images = app_module._build_export_payload(_ready(make_review))
    assert doc_data
    joined = "\n".join(
        b.get("text", "") for b in doc_data["blocks"] if b.get("type") in
        ("para", "quote")
    )
    assert "**15분**" in joined
    assert "==양념으로 꼭 시키세요==" in joined
    assert "*문래갈매기*" in joined
    assert "`문래역 7번 출구`" in joined


# --------------------------------------------------------------------------- #
# (3-b) 네이버 네이티브 경로 — 북마클릿이 토큰을 SmartEditor nodeStyle 로 바꾼다
# --------------------------------------------------------------------------- #
def test_bookmarklet_source_maps_tokens_to_nodestyle():
    """node 없이도 도는 소스 계약 — 팔레트가 서버와 **같은 값**이어야 한다."""
    with open(_BOOKMARKLET, encoding="utf-8") as f:
        src = f.read()
    assert "function emphNodes" in src
    assert "EMPH_RE" in src
    assert "bold: true" in src
    assert f"fontColor: '{PINK}'" in src
    assert f"fontColor: '{BLUE}'" in src
    assert f"backgroundColor: '{MARK}'" in src
    # para 블록이 emphNodes 를 통과해 조립된다(그냥 tn() 으로 넣으면 강조가 죽는다).
    assert "para(emphNodes(ln), 'center')" in src


_NODE = shutil.which("node")


@pytest.mark.skipif(not _NODE, reason="node 없음 — 소스 계약 테스트가 대신 지킨다")
def test_bookmarklet_emphnodes_runs_and_styles_nodes(tmp_path):
    """북마클릿의 실제 `emphNodes` 를 node 로 돌려 nodeStyle 을 확인한다."""
    with open(_BOOKMARKLET, encoding="utf-8") as f:
        src = f.read()
    start = src.index("  function tn(value, style) {")
    end = src.index("  function quotationComp(")
    harness = tmp_path / "emph.js"
    harness.write_text(
        "var __n = 0; function sid() { return 'id' + (++__n); }\n"
        + src[start:end]
        + "\nconsole.log(JSON.stringify(emphNodes(process.argv[2])));\n",
        encoding="utf-8",
    )
    kw = {}
    if sys.platform == "win32":          # 콘솔 창을 띄우지 않는다
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    out = subprocess.run(
        [_NODE, str(harness), "먼저 **15분** ==꼭 양념== *문래갈매기* `7번 출구` 끝"],
        capture_output=True, text=True, encoding="utf-8", check=True, **kw,
    ).stdout
    import json as _json
    nodes = _json.loads(out)
    styled = {n["value"]: n.get("style") for n in nodes if n.get("style")}
    assert styled["15분"] == {"@ctype": "nodeStyle", "bold": True}
    assert styled["꼭 양념"]["backgroundColor"] == MARK
    assert styled["문래갈매기"]["fontColor"] == PINK
    assert styled["7번 출구"]["fontColor"] == BLUE
    # 강조 밖 글자는 스타일이 없다(전부 색칠되면 그것도 회귀다).
    assert any(n.get("style") is None for n in nodes)
