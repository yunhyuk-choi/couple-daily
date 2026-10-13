"""북마클릿은 **유저스크립트에서 생성된다** — 그 계약의 회귀 테스트.

**왜 이 파일이 있나.** 데스크톱용 북마클릿은 한때 `tools/naver-bookmarklet.js` 라는
**손으로 유지하는 1,400줄 복제본**이었다. 유저스크립트가 크롭·v2 자동경로·URL 페이로드를
받도록 자라는 동안 복제본은 뒤처졌고, 앱이 사진 바이트 대신 **URL + 크롭 좌표**를 보내기
시작한 순간 북마클릿 경로에서는 **사진이 조용히 비어 나갔다.** 설치 문서는 "로직은
동일하다"고 *약속으로* 적어 두고 있었다 — 약속은 어긋난다.

그래서 지금 북마클릿은 사본이 아니라 **같은 소스의 다른 형태**다
(`tools/build_bookmarklet.py`). 이 파일은 그 구조가 조용히 풀리지 않도록 네 겹을 못질한다:

  (1) 커밋된 생성물이 원천과 **일치**하는가        — 어긋나면 그게 곧 옛 복제본이다
  (2) 버전이 `@version` **한 줄**에서만 오는가     — 두 군데 적으면 갈라진다
  (3) 새 페이로드(URL+크롭)를 북마클릿이 **받는가** — 옛 북마클릿이 죽은 바로 그 이유
  (4) 강조 네 가지가 북마클릿에서 **실제로 나오는가** — 코드에 있는 것과 나오는 것은 다르다

(4)는 `tests/test_review_emphasis.py` 가 유저스크립트 쪽에서 지키는 것과 **같은 팔레트**를
생성물 쪽에서 한 번 더 확인한다. 생성이라 자동으로 따라와야 정상이고, 안 따라오면
생성 변환(주석 제거·들여쓰기 깎기)이 뭔가를 망가뜨렸다는 뜻이다.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from urllib.parse import unquote

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_spec = importlib.util.spec_from_file_location(
    "build_bookmarklet", os.path.join(_ROOT, "tools", "build_bookmarklet.py")
)
bb = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bb)

PINK = "#e64980"      # 색 1 — '무엇'(상호·메뉴)
BLUE = "#1c7ed6"      # 색 2 — '어디·언제·얼마'
MARK = "#ffec99"      # 형광펜 배경

_NODE = shutil.which("node")


def _decoded():
    """커밋된 `.txt` 를 `javascript:` URL 로 보고 디코드한 **실행될 코드 그대로**."""
    text = bb.BOOKMARKLET.read_bytes().decode("utf-8").strip()
    assert text.startswith("javascript:"), "북마클릿은 `javascript:` 로 시작해야 한다"
    return unquote(text[len("javascript:"):], encoding="utf-8")


# --------------------------------------------------------------------------- #
# (1) 커밋된 생성물이 원천과 일치한다 — 복제본으로 돌아가지 않는다
# --------------------------------------------------------------------------- #
def test_committed_outputs_match_the_source():
    """⛔ 이 테스트가 깨졌다 = 북마클릿이 다시 '손으로 유지하는 사본'이 됐다.

    고치는 법은 하나다: `python tools/build_bookmarklet.py`.
    """
    stale = bb.is_stale()
    assert not stale, (
        "생성물이 원천과 어긋났다: "
        + ", ".join(os.path.relpath(p, _ROOT) for p in stale)
        + " — `python tools/build_bookmarklet.py` 로 다시 생성하라."
    )


def test_check_mode_agrees_with_the_test():
    """CLI `--check` 와 이 테스트가 **같은 함수**를 본다(두 판정이 갈라지지 않는다)."""
    assert bb.main(["--check"]) == 0


def test_bookmarklet_is_one_encoded_line():
    """즐겨찾기 URL 칸에 들어가는 **한 줄**이어야 한다 (공백·`#`·줄바꿈이 날것이면 깨진다)."""
    raw = bb.BOOKMARKLET.read_bytes().decode("utf-8")
    assert raw.count("\n") == 1 and raw.endswith("\n")   # 꼬리 개행 하나뿐
    line = raw.strip()
    for bad in (" ", "\t", "#", '"', "<", ">", "\\"):
        assert bad not in line, f"인코딩 안 된 {bad!r} 가 남았다"
    assert line.isascii(), "한글이 날것으로 남았다 — 브라우저마다 저장 방식이 다르다"


def test_decoded_bookmarklet_is_the_source_minus_comments():
    """디코드하면 **원천의 코드 그대로**다 — 사본이 아니라 형태만 다르다는 증거."""
    src = bb.read_source()
    want = bb.strip_comment_lines(bb.strip_metadata_block(bb.stamp_version(src))) + "\nvoid 0;"
    assert _decoded() == want
    # 유저스크립트 메타 블록은 떨어져 나간다(매니저 없이 도는 형태라 의미가 없다).
    assert "==UserScript==" not in _decoded()


# --------------------------------------------------------------------------- #
# (2) 버전은 `@version` 한 줄에서만 온다
# --------------------------------------------------------------------------- #
def test_version_comes_from_one_line_only():
    src = bb.read_source()
    version = bb.parse_version(src)
    assert version != "1.0.0", "내용이 바뀌었는데 `@version` 을 안 올렸다"
    assert f"var VERSION = '{version}';" in src          # 본문 리터럴이 따라왔다
    assert f"var VERSION = '{version}';" in _decoded()   # 북마클릿에도 같은 값이 박혔다


def test_stamp_version_fixes_a_drifted_literal():
    """본문 리터럴을 손으로 틀리게 적어도 빌더가 `@version` 으로 되돌린다."""
    src = bb.read_source()
    version = bb.parse_version(src)
    broken = src.replace(f"var VERSION = '{version}';", "var VERSION = '0.0.0';", 1)
    assert "var VERSION = '0.0.0';" in broken            # 정말 망가뜨렸다
    assert f"var VERSION = '{version}';" in bb.stamp_version(broken)


# --------------------------------------------------------------------------- #
# (3) 새 페이로드(URL + 크롭 좌표)를 북마클릿이 받는다
# --------------------------------------------------------------------------- #
def test_bookmarklet_accepts_the_url_plus_crop_payload():
    """⛔ 옛 북마클릿이 죽은 바로 그 지점.

    앱의 📤 는 사진 **바이트**가 아니라 `{src, url, crop}` 을 싣는다(모바일 클립보드가
    바이트를 못 나른다). 받는 쪽이 그 URL 에서 받아 **여기서** 잘라야 한다.
    """
    code = _decoded()
    assert "function resolveImageBlobs" in code
    assert "im.url" in code and "im.crop" in code        # 새 모양
    assert "im.dataUri" in code                          # 옛 모양 폴백도 산다
    assert "function cropToBlob" in code                 # 받은 뒤 여기서 굽는다
    assert "fetch(im.url" in code


def test_bookmarklet_uploads_to_naver_not_external_links():
    """발행된 글의 사진은 **네이버 내부 이미지**여야 한다 — 외부 링크는 글이 지워진다."""
    code = _decoded()
    assert "upphoto" in code
    assert "internalResource" in code


# --------------------------------------------------------------------------- #
# (4) 강조 네 가지가 북마클릿에서 나온다
# --------------------------------------------------------------------------- #
def test_bookmarklet_source_maps_the_four_emphasis_tokens():
    """팔레트가 서버(`tests/test_review_emphasis.py`)와 **같은 값**이어야 한다."""
    code = _decoded()
    assert "function emphNodes" in code
    assert "EMPH_RE" in code
    assert "bold: true" in code                          # **굵게**
    assert f"backgroundColor: '{MARK}'" in code          # ==형광펜==
    assert f"fontColor: '{PINK}'" in code                # *분홍*
    assert f"fontColor: '{BLUE}'" in code                # `파랑`
    # para 블록이 emphNodes 를 통과해 조립된다(그냥 tn() 으로 넣으면 강조가 죽는다).
    assert "para(emphNodes(ln), 'center')" in code


@pytest.mark.skipif(not _NODE, reason="node 없음 — 소스 계약 테스트가 대신 지킨다")
def test_decoded_bookmarklet_is_valid_javascript(tmp_path):
    """⛔ ASI 함정 감시. 줄 구조가 깨지면 여기서 잡힌다 (`%0A` 보존의 근거)."""
    js = tmp_path / "bookmarklet.js"
    js.write_text(_decoded(), encoding="utf-8", newline="")
    kw = {}
    if sys.platform == "win32":          # 콘솔 창을 띄우지 않는다
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    r = subprocess.run([_NODE, "--check", str(js)], capture_output=True,
                       text=True, encoding="utf-8", **kw)
    assert r.returncode == 0, r.stderr


@pytest.mark.skipif(not _NODE, reason="node 없음 — 소스 계약 테스트가 대신 지킨다")
def test_bookmarklet_emphnodes_actually_styles_nodes(tmp_path):
    """⭐ **코드에 있다**와 **실제로 나온다**는 다르다 — 북마클릿의 `emphNodes` 를 돌린다."""
    code = _decoded()
    start = code.index("function tn(value, style) {")
    end = code.index("function quotationComp(")
    harness = tmp_path / "emph.js"
    harness.write_text(
        "var __n = 0; function sid() { return 'id' + (++__n); }\n"
        + code[start:end]
        + "\nconsole.log(JSON.stringify(emphNodes(process.argv[2])));\n",
        encoding="utf-8", newline="",
    )
    kw = {}
    if sys.platform == "win32":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    out = subprocess.run(
        [_NODE, str(harness), "먼저 **15분** ==꼭 양념== *문래갈매기* `7번 출구` 끝"],
        capture_output=True, text=True, encoding="utf-8", check=True, **kw,
    ).stdout
    nodes = json.loads(out)
    styled = {n["value"]: n.get("style") for n in nodes if n.get("style")}
    assert styled["15분"] == {"@ctype": "nodeStyle", "bold": True}
    assert styled["꼭 양념"]["backgroundColor"] == MARK
    assert styled["문래갈매기"]["fontColor"] == PINK
    assert styled["7번 출구"]["fontColor"] == BLUE
    # 강조 밖 글자는 스타일이 없다(전부 색칠되면 그것도 회귀다).
    assert any(n.get("style") is None for n in nodes)
