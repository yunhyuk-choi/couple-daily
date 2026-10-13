"""유저스크립트 버전 — **앱이 설치본이 몇 버전인지 안다.**

지키려는 계약 넷:

1. 기대값의 단일 원천은 ``tools/naver-uploader.user.js`` 의 ``@version`` 한 줄이다
   (빌더도 앱도 **같은 함수**로 읽는다 — 두 군데 적으면 반드시 어긋난다).
2. 스크립트가 그 호출에 자기 버전을 **실어 보낸다**.
3. 다르면 앱이 경고한다 — 응답(``version_warning``)으로도, **앱 화면**으로도.
   화면이 필요한 이유: 뒤처진 사본은 응답의 새 필드를 띄울 줄도 모른다.
4. 버전이 안 와도 **깨지지 않는다** — "어떤 사본인지 모르겠다"로 안내할 뿐,
   페이로드 전달은 예전과 똑같다.
"""
import json

import app as app_module
import userscripts
from models import db

UP = userscripts.SOURCE
WANT = userscripts.expected_version()

_AI = {
    "title": "문래갈매기 다녀왔어요",
    "blocks": [{"type": "para", "text": "맛있었어요."}],
    "hashtags": ["#문래갈매기"],
}


# --------------------------------------------------------------------------- #
# 1. 단일 원천
# --------------------------------------------------------------------------- #
def test_expected_version_comes_from_the_userscript_itself():
    src = UP.read_bytes().decode("utf-8")
    assert WANT, "원천에서 @version 을 못 읽으면 경고할 자격이 없다"
    assert f"@version      {WANT}" in src or f"@version {WANT}" in src


def test_the_builder_reads_the_version_through_the_same_function():
    """빌더가 자기 정규식을 또 들고 있으면 언젠가 갈라진다."""
    import sys
    sys.path.insert(0, str(userscripts.ROOT / "tools"))
    try:
        import build_bookmarklet
    finally:
        sys.path.pop(0)
    src = UP.read_bytes().decode("utf-8")
    assert build_bookmarklet.parse_version(src) == WANT
    # 빌더 모듈에 **또 하나의** @version 정규식이 있으면 안 된다.
    builder_src = (userscripts.ROOT / "tools" / "build_bookmarklet.py").read_text(
        encoding="utf-8"
    )
    assert "@version" not in builder_src.replace("`@version`", "")


def test_the_running_script_literal_matches_the_meta():
    """북마클릿에는 메타 블록이 없다 — 런타임이 자기 버전을 알려면 본문에도 있어야."""
    src = UP.read_bytes().decode("utf-8")
    assert f"var VERSION = '{WANT}';" in src


def test_the_script_sends_its_version_on_the_only_call_it_makes():
    src = UP.read_bytes().decode("utf-8")
    assert "/api/naver-export/pending?k=" in src
    assert "'&v=' + encodeURIComponent(VERSION)" in src


# --------------------------------------------------------------------------- #
# 2. 경고 문장
# --------------------------------------------------------------------------- #
def test_a_matching_version_says_nothing():
    assert userscripts.version_warning(WANT) == ""


def test_an_old_version_is_named_in_the_warning():
    warn = userscripts.version_warning("0.0.1")
    assert "0.0.1" in warn and WANT in warn
    assert userscripts.INSTALL_DOC in warn


def test_a_missing_version_says_it_does_not_know_rather_than_breaking():
    for sent in (None, "", "   "):
        warn = userscripts.version_warning(sent)
        assert warn, "안 보낸 것도 '옛 사본'이라는 신호다"
        assert "모르겠어" in warn
        assert WANT in warn


def test_junk_versions_are_treated_as_not_sent():
    """공개 엔드포인트가 받는 값이라 DB·화면에 가기 전에 모양을 못 박는다."""
    assert userscripts.normalize_sent("<script>x</script>") == ""
    assert userscripts.normalize_sent("1.2.0") == "1.2.0"
    assert userscripts.normalize_sent("가" * 80) == ""
    assert len(userscripts.normalize_sent("1" * 200)) <= userscripts.MAX_VERSION_LEN


def test_no_warning_when_we_cannot_read_the_expected_version(monkeypatch):
    """기대값을 모르면 **남의 버전을 판정하지 않는다**(모르면 말하지 않는다)."""
    monkeypatch.setattr(userscripts, "expected_version", lambda: "")
    assert userscripts.version_warning("0.0.1") == ""


# --------------------------------------------------------------------------- #
# 3. 엔드포인트 — 모든 응답 경로에 실린다
# --------------------------------------------------------------------------- #
def test_pending_carries_the_warning_even_when_there_is_nothing_pending(
    client, couple_user, flask_app
):
    key = app_module._ensure_export_key(couple_user)
    db.session.commit()
    body = client.get(f"/api/naver-export/pending?k={key}&v=0.0.1").get_json()
    assert body["pending"] is False
    assert WANT in body["version_warning"]


def test_pending_carries_the_warning_on_the_payload_path(
    client, couple_user, flask_app, make_review
):
    review = make_review(ai_json=json.dumps(_AI, ensure_ascii=False), status="ready")
    key = app_module._ensure_export_key(couple_user)
    db.session.commit()
    app_module._naver_export_put(key, review.id)
    body = client.get(f"/api/naver-export/pending?k={key}&v=0.0.1").get_json()
    assert body["pending"] is True
    assert body["blocks"], "경고를 싣느라 본업(페이로드)이 깨지면 안 된다"
    assert WANT in body["version_warning"]


def test_a_current_script_gets_no_warning_and_the_payload_still_flows(
    client, couple_user, flask_app, make_review
):
    review = make_review(ai_json=json.dumps(_AI, ensure_ascii=False), status="ready")
    key = app_module._ensure_export_key(couple_user)
    db.session.commit()
    app_module._naver_export_put(key, review.id)
    body = client.get(f"/api/naver-export/pending?k={key}&v={WANT}").get_json()
    assert body["pending"] is True
    assert body["version_warning"] == ""


def test_an_old_script_that_sends_no_version_still_gets_its_payload(
    client, couple_user, flask_app, make_review
):
    """⭐ '깨지지 말고 안내하라' — 옛 사본도 하던 일은 그대로 한다."""
    review = make_review(ai_json=json.dumps(_AI, ensure_ascii=False), status="ready")
    key = app_module._ensure_export_key(couple_user)
    db.session.commit()
    app_module._naver_export_put(key, review.id)
    body = client.get(f"/api/naver-export/pending?k={key}").get_json()
    assert body["pending"] is True
    assert body["blocks"]
    assert "모르겠어" in body["version_warning"]


def test_a_bad_key_leaks_nothing_but_still_answers_about_the_version(
    client, flask_app
):
    body = client.get("/api/naver-export/pending?k=nope&v=0.0.1").get_json()
    assert body["pending"] is False
    assert WANT in body["version_warning"]


# --------------------------------------------------------------------------- #
# 4. 앱 화면이 대신 말한다 (뒤처진 사본은 응답의 새 필드를 못 띄운다)
# --------------------------------------------------------------------------- #
def test_the_app_records_what_the_script_said(client, couple_user, flask_app):
    key = app_module._ensure_export_key(couple_user)
    db.session.commit()
    client.get(f"/api/naver-export/pending?k={key}&v=0.0.1")
    db.session.refresh(couple_user)
    assert couple_user.uploader_version == "0.0.1"
    assert couple_user.uploader_seen_at is not None


def test_a_script_that_sends_no_version_is_recorded_as_unknown_not_skipped(
    client, couple_user, flask_app
):
    key = app_module._ensure_export_key(couple_user)
    db.session.commit()
    client.get(f"/api/naver-export/pending?k={key}")
    db.session.refresh(couple_user)
    assert couple_user.uploader_version is None
    assert couple_user.uploader_seen_at is not None, (
        "버전을 안 보낸 것도 '다녀갔다'는 사실이다 — 그래야 화면이 말할 수 있다"
    )
    st = app_module.uploader_status(couple_user)
    assert st and "모르겠어" in st["warning"]
    assert st["installed"] == userscripts.LEGACY_VERSION


def test_silence_is_silence_we_do_not_nag_someone_who_never_installed_it(
    couple_user, flask_app
):
    assert couple_user.uploader_seen_at is None
    assert app_module.uploader_status(couple_user) is None


def test_a_current_script_leaves_the_screens_quiet(client, couple_user, flask_app):
    key = app_module._ensure_export_key(couple_user)
    db.session.commit()
    client.get(f"/api/naver-export/pending?k={key}&v={WANT}")
    db.session.refresh(couple_user)
    assert app_module.uploader_status(couple_user) is None


def test_settings_screen_shows_the_warning(client, couple_user, flask_app):
    key = app_module._ensure_export_key(couple_user)
    db.session.commit()
    client.get(f"/api/naver-export/pending?k={key}&v=0.0.1")
    html = client.get("/settings").get_data(as_text=True)
    assert "0.0.1" in html and WANT in html


def test_review_detail_warns_before_the_export_button(
    client, couple_user, flask_app, make_review
):
    review = make_review(ai_json=json.dumps(_AI, ensure_ascii=False), status="ready")
    key = app_module._ensure_export_key(couple_user)
    db.session.commit()
    client.get(f"/api/naver-export/pending?k={key}&v=0.0.1")
    html = client.get(f"/reviews/{review.id}").get_data(as_text=True)
    assert "0.0.1" in html and WANT in html
