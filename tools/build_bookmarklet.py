"""유저스크립트 → **북마클릿** 생성 (데스크톱용).

    python tools/build_bookmarklet.py            # 다시 만든다
    python tools/build_bookmarklet.py --check    # 최신인지 확인만 (낡았으면 종료코드 1)
    python tools/build_bookmarklet.py --stdout   # 파일 대신 표준출력

## 왜 생성인가 — 복제본은 반드시 갈라진다

이 레포에는 2026-10-13 까지 `tools/naver-bookmarklet.js` 라는 **손으로 유지하는 1,400줄
복제본**이 있었다. 유저스크립트가 크롭·v2 자동경로·URL 페이로드를 받도록 자라는 동안
복제본은 뒤처졌고, 앱이 사진 바이트 대신 **URL + 크롭 좌표**를 보내기 시작한 순간
북마클릿 경로에서는 **사진이 조용히 비어 나갔다.** 그래서 그 파일을 지웠는데, 그때
데스크톱에서 쓸 길까지 같이 사라진 것이 이 모듈이 생긴 이유다.

**원천은 `tools/naver-uploader.user.js` 하나다.** 북마클릿은 그 소스의 다른 *형태*이지
다른 *사본*이 아니다. 두 파일이 갈라질 자리가 구조적으로 없다 — `--check`(그리고 같은
함수를 보는 `tests/test_bookmarklet_build.py`)가 어긋나면 막는다.

## 왜 "줄바꿈을 지운 한 줄"이 아닌가 — ASI 함정

북마클릿은 한 줄이어야 하지만 JS 를 **진짜로** 한 줄로 이어 붙이면 자동 세미콜론 삽입
(ASI)에 기대던 자리가 조용히 깨진다. 그래서 줄바꿈을 지우지 않고 **`%0A` 로 퍼센트
인코딩**한다 — 브라우저가 `javascript:` URL 을 실행 전에 디코드하므로 줄 구조가 원본
그대로 살아난다.

## 변환은 **줄 전체가 `//` 주석인 줄과 빈 줄을 지우고 들여쓰기를 깎는 것**이 전부다

줄 **머리**가 `//` 인 줄은 JS 문자열 리터럴 안쪽일 수 없다(JS 문자열은 줄을 넘지 못한다).
그래서 이 삭제는 증명 없이 안전하다. 코드 줄 뒤에 붙은 짧은 주석은 **그대로 둔다** —
줄바꿈이 보존되니 해롭지 않고, 그걸 지우려는 순간 정규식 리터럴(`/\\/+$/`)을 주석으로
오인하는 사고가 시작된다.

⚠️ **들여쓰기를 깎으므로 여러 줄에 걸친 템플릿 리터럴(`` ` ``)이 들어오면 문자열 내용이
바뀐다.** 지금 원천에 템플릿 리터럴은 없다(백틱은 정규식 `EMPH_RE` 안과 `'`'` 문자
비교에만 나오며 둘 다 한 줄짜리다). 자매 레포는 "백틱이 하나라도 있으면 중단"으로 막는데
여기서는 그 백틱들이 **강조 기능의 본체**라 쓸 수 없다. 대신 테스트가 두 겹으로 막는다:
`node --check` 로 생성물의 **구문**을, `emphNodes` 를 실제로 돌려 **동작**을 확인한다.
새로 여러 줄 템플릿 리터럴을 쓰고 싶으면 **문자열 이어붙이기(`+`)로 써라.**

## 퍼센트 인코딩 범위

`%`·`#`·공백·`"`·`<`·`>`·`\\`·줄바꿈·비ASCII 를 전부 인코딩한다. `#` 은 안 하면 조각
(fragment)으로 잘려 나가고(코드에 `#fff` 가 있다), `%` 는 안 하면 이중 디코드로 깨진다.
비ASCII(한글)는 브라우저마다 저장 방식이 달라 우리가 먼저 UTF-8 로 못 박는다.

## 줄끝

원천은 윈도우 체크아웃에서 CRLF 로 내려온다(`core.autocrlf=true`). 읽을 때 LF 로
정규화하고 비교도 정규화해서 한다 — 안 그러면 리눅스/윈도우에서 `--check` 결과가
갈린다.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import quote

TOOLS = Path(__file__).resolve().parent
ROOT = TOOLS.parent

# 스크립트로 직접 돌면 sys.path[0] 이 `tools/` 라 루트 모듈을 못 찾는다. 버전을 읽는
# 정규식은 **앱과 공용**이어야 하므로(둘이 따로 적으면 반드시 어긋난다) 루트를 얹고
# `userscripts` 에서 가져온다.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import userscripts  # noqa: E402  (위 sys.path 조정 뒤에 와야 한다)

SOURCE = TOOLS / "naver-uploader.user.js"
BOOKMARKLET = TOOLS / "naver-uploader.bookmarklet.txt"

META_OPEN = "// ==UserScript=="
META_CLOSE = "// ==/UserScript=="

# 본문 버전 리터럴의 꼬리 주석 표식 — 다른 `VERSION` 과 구분하는 열쇠.
VERSION_TAG = "cd-up-version"

# 퍼센트 인코딩에서 **살려 둘** ASCII. 여기 없는 글자는 전부 `%XX` 가 된다.
# (영숫자는 `quote` 가 언제나 살린다 — 여기 적지 않아도 된다.)
URL_SAFE = "!$&'()*+,-./:;=?@_~[]{}|^"

_VERSION_LITERAL_RE = re.compile(
    r"(var\s+VERSION\s*=\s*)(['\"][^'\"]*['\"])(\s*;\s*/\*\s*" + re.escape(VERSION_TAG) + r"\s*\*/)"
)


def normalize(text: str) -> str:
    """줄끝을 LF 로 못 박는다 (윈도우 체크아웃 CRLF 대비)."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def parse_version(src: str) -> str:
    """유저스크립트 메타의 `@version` — 버전의 **단일 원천**.

    읽는 정규식은 `userscripts` 가 들고 있다(앱의 `expected_version()` 과 **같은**
    한 줄을 봐야 한다). 여기서는 못 찾았을 때 생성을 멈추는 것만 자기 말로 한다.
    """
    v = userscripts.parse_version(src)
    if not v:
        raise SystemExit(f"`@version` 줄을 찾지 못했다: {SOURCE}")
    return v


def stamp_version(src: str) -> str:
    """본문 버전 리터럴을 `@version` 에서 다시 심는다(자기 정규화).

    생성물에서는 메타 블록이 **떨어져 나가므로**(`strip_metadata_block`) 런타임이
    자기 버전을 알려면 본문에도 리터럴이 있어야 한다. 사람이 고치는 곳은 언제나
    `@version` 한 줄이고, 어긋나면 `--check` 와 빌드 테스트가 막는다.
    """
    want = parse_version(src)
    out, hits = _VERSION_LITERAL_RE.subn(
        lambda m: m.group(1) + "'" + want + "'" + m.group(3), src
    )
    if hits != 1:
        raise SystemExit(
            f"본문의 버전 리터럴(`var VERSION = '…';  /* {VERSION_TAG} */`)이 "
            f"{hits} 개다 — 정확히 하나여야 한다: {SOURCE}"
        )
    return out


def strip_metadata_block(src: str) -> str:
    """`// ==UserScript== … // ==/UserScript==` 를 떼어 낸다.

    매니저 없이 실행하는 형태에서는 의미가 없는 블록이고, 남겨 두면 "이것도 설치하는
    것인가" 하는 오해를 만든다.
    """
    lines = src.split("\n")
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == META_OPEN)
        end = next(i for i, ln in enumerate(lines) if ln.strip() == META_CLOSE)
    except StopIteration:
        raise SystemExit(f"유저스크립트 헤더를 찾지 못했다: {SOURCE}")
    return "\n".join(lines[:start] + lines[end + 1:]).lstrip("\n")


def strip_comment_lines(src: str) -> str:
    """줄 전체가 `//` 주석인 줄과 빈 줄을 지우고 들여쓰기를 깎는다 (머리말 참조)."""
    return "\n".join(s for s in (ln.strip() for ln in src.split("\n")) if s and not s.startswith("//"))


def bookmarklet(src: str) -> str:
    """즐겨찾기 URL 한 줄. 줄바꿈은 지우지 않고 `%0A` 로 간다 (머리말의 ASI 설명)."""
    code = strip_comment_lines(strip_metadata_block(src))
    return "javascript:" + quote(code + "\nvoid 0;", safe=URL_SAFE, encoding="utf-8") + "\n"


def build(src: str) -> dict[Path, str]:
    """원천 문자열 → {파일 경로: 내용}. 파일을 쓰지 않는다(테스트가 그대로 쓴다).

    ⭐ **원천 자신도 산출물이다** — 본문 버전 리터럴이 `@version` 과 어긋나면 여기서
    바로잡는다. 그래야 사람이 고칠 곳이 `@version` 한 줄로 남는다.
    """
    src = stamp_version(normalize(src))
    return {SOURCE: src, BOOKMARKLET: bookmarklet(src)}


def read_source() -> str:
    if not SOURCE.exists():
        raise SystemExit(f"원천이 없다: {SOURCE}")
    return normalize(SOURCE.read_bytes().decode("utf-8"))


def build_all() -> dict[Path, str]:
    """전부 생성한다 — `--check` 와 테스트가 **같은 이 함수**를 본다."""
    return build(read_source())


def is_stale(made: dict[Path, str] | None = None) -> list[Path]:
    """생성물 중 원천과 어긋난 파일들 (비어 있으면 일치)."""
    made = build_all() if made is None else made
    return [
        p for p, text in made.items()
        if not p.exists() or normalize(p.read_bytes().decode("utf-8")) != text
    ]


def _write(path: Path, text: str) -> bool:
    """내용이 같으면 쓰지 않는다(수정 시각을 흔들지 않는다). 쓴 경우 True.

    UTF-8(BOM 없음) · LF — `newline=""` 가 없으면 윈도우에서 CRLF 가 섞인다.
    """
    old = normalize(path.read_bytes().decode("utf-8")) if path.exists() else None
    if old == text:
        return False
    path.write_text(text, encoding="utf-8", newline="")
    return True


def main(argv: list[str] | None = None) -> int:
    # 윈도우 콘솔이 cp949 라 한글·em dash 한 글자에 통째로 죽는다.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

    p = argparse.ArgumentParser(description="네이버 업로더 유저스크립트 → 북마클릿 생성")
    p.add_argument("--check", action="store_true",
                   help="파일을 고치지 않고 최신인지만 확인한다 (낡았으면 종료코드 1)")
    p.add_argument("--stdout", action="store_true", help="파일 대신 표준출력으로 찍는다")
    args = p.parse_args(argv)

    made = build_all()

    if args.stdout:
        sys.stdout.write(made[BOOKMARKLET])
        return 0

    if args.check:
        stale = is_stale(made)
        if stale:
            for path in stale:
                print(f"낡음: {path.relative_to(ROOT)}")
            print("→ `python tools/build_bookmarklet.py` 로 다시 생성하라.")
            return 1
        print(f"생성물이 원천과 일치한다 (v{parse_version(made[SOURCE])} / 파일 {len(made)}개).")
        return 0

    for path, text in made.items():
        changed = _write(path, text)
        print(f"{'갱신' if changed else '그대로'}: {path.relative_to(ROOT)} ({len(text):,}자)")
    print("북마클릿은 .txt **전체**를 복사해 즐겨찾기 편집 창의 **URL 칸**에 넣는다 "
          "(주소창이 아니다 — 크롬이 `javascript:` 를 지운다). tools/NAVER-UPLOADER-INSTALL.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
