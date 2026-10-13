"""브라우저에서 도는 우리 스크립트의 **버전 단일 원천** — 그리고 앱이 그걸 아는 길.

## 왜 이 모듈이 있나

네이버 업로더는 **유저스크립트**(또는 그걸로 만든 북마클릿)다. 그 사본은 *등록하는
순간의 코드 전체*이고 **스스로 갱신되지 않는다.** 우리가 앱과 스크립트를 같이 고쳐도
브라우저에 박힌 쪽은 어제 것일 수 있다 — 그리고 지금까지 **앱은 그 사실을 전혀 몰랐다.**
스크립트가 앱에 거는 유일한 호출(``GET /api/naver-export/pending``)이 버전을 안 실었기
때문에, 사람이 오버레이 제목의 ``v1.1.0`` 을 **눈으로** 맞춰야 했다. "조용히 뒤처진
스크립트"는 그 다음에 오는 모든 이상한 증상의 뿌리다(자매 레포 ``naver-ent-autopost``
의 ``app/userscripts.py`` 가 같은 사고 두 건 끝에 먼저 만든 장치다 — 사상을 가져왔다).

## 단일 원천은 ``tools/naver-uploader.user.js`` 의 ``@version`` 한 줄이다

거기서 셋이 갈라져 나온다::

    tools/naver-uploader.user.js   // @version 1.2.0
        ├─ 런타임 리터럴  var VERSION = '1.2.0';  /* cd-up-version */   (빌더가 심는다)
        ├─ 북마클릿 생성물 tools/naver-uploader.bookmarklet.txt         (빌더가 만든다)
        └─ 앱이 기대하는 값  expected_version()                          (이 모듈)

``tools/build_bookmarklet.py`` 가 **이 모듈의 정규식을 가져다 쓴다** — 버전을 읽는
코드를 두 군데 적으면 반드시 어긋난다. ``--check`` 와 ``tests/test_bookmarklet_build.py``
가 생성물 쪽을, ``tests/test_uploader_version.py`` 가 앱 쪽을 본다.

## 읽기 실패는 조용한 degrade다

파일을 못 읽으면 ``expected_version()`` 이 ``""`` 를 돌려주고 경고는 **나가지 않는다.**
기대값을 모르는 채로 "네 버전이 틀렸다"고 말하는 것은 거짓말이기 때문이다.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "tools" / "naver-uploader.user.js"

#: 설치 안내 문서 — 경고 문구가 사람을 보낼 곳.
INSTALL_DOC = "tools/NAVER-UPLOADER-INSTALL.md"

#: 버전 필드를 아예 안 보내는 옛 사본을 부르는 이름. 빈 문자열로 두면 화면이
#: "버전 (없음)" 같은 말을 하게 되고, 그건 사람에게 아무것도 알려 주지 않는다.
LEGACY_VERSION = "버전을 안 보내는 옛 사본"

#: 브라우저가 보낸 버전 문자열의 상한 — 저장·표시 모두 이 길이로 자른다.
MAX_VERSION_LEN = 40

_VERSION_META_RE = re.compile(r"^//\s*@version\s+(\S+)\s*$", re.MULTILINE)


def parse_version(src: str) -> str:
    """유저스크립트 원문 → ``@version`` 값. 못 찾으면 ``""``.

    ``tools/build_bookmarklet.py`` 가 **이 함수를 import 해서** 쓴다(그쪽은 못 찾으면
    생성을 중단한다 — 호출부마다 실패를 자기 말로 다룬다).
    """
    m = _VERSION_META_RE.search(src or "")
    return m.group(1).strip() if m else ""


def normalize_sent(raw) -> str:
    """브라우저가 보낸 ``v`` 파라미터를 믿을 수 있는 짧은 문자열로.

    공개 엔드포인트가 받는 값이라 **길이와 모양을 여기서 못 박는다** — 이 값은 DB 에
    저장되고 화면에 그려진다. 버전처럼 생기지 않은 것은 통째로 버린다(= 안 보낸 것).
    """
    s = (raw or "").strip()[:MAX_VERSION_LEN]
    return s if re.fullmatch(r"[0-9A-Za-z._+-]{1,%d}" % MAX_VERSION_LEN, s) else ""


@lru_cache(maxsize=1)
def expected_version() -> str:
    """앱이 **기대하는** 업로더 버전. 원천 파일에서 읽는다 — 두 군데 적지 않는다.

    읽지 못하면 ``""`` — 그 경우 경고는 나가지 않는다(모르면 말하지 않는다).
    """
    try:
        return parse_version(SOURCE.read_bytes().decode("utf-8"))
    except OSError:
        return ""


def version_warning(sent) -> str:
    """브라우저가 보낸 버전 vs 앱이 기대하는 버전. 같거나 모르면 ``""``.

    **안 보낸 것도 경고다** — 버전 필드 자체가 나중에 생겼으므로, 안 보낸다는 건
    그 전 사본이라는 뜻이다. 다만 말투가 다르다: 틀렸다가 아니라 *모르겠다* 다.
    """
    want = expected_version()
    if not want:
        return ""          # 기대값을 모른다 — 남의 버전을 판정할 자격이 없다
    got = normalize_sent(sent)
    if got == want:
        return ""
    if not got:
        return (
            f"네이버 업로더가 자기 버전을 안 보내 — 어떤 사본인지 모르겠어"
            f" (앱이 기대하는 건 v{want}). 북마클릿·유저스크립트는 스스로"
            f" 갱신되지 못해(등록한 순간의 코드 전체야). 최신으로 다시 등록해줘"
            f" — {INSTALL_DOC}"
        )
    return (
        f"네이버 업로더가 옛 버전이야 — 설치된 건 v{got}, 앱이 기대하는 건 v{want}."
        f" 북마클릿·유저스크립트는 스스로 갱신되지 못해(등록한 순간의 코드 전체야)."
        f" 최신으로 다시 등록해줘 — {INSTALL_DOC}"
    )
