"""couple-daily — a tiny daily-question app for exactly two people.

Identity constraint (do not violate): the AI mechanism is the `claude` CLI run
as a backend subprocess (see ai.py), NOT the Anthropic API/SDK. This file wires
Flask + SQLAlchemy, auth, couple linking, the daily question, monthly insight,
settings and PWA plumbing on top of that.

Run locally:   python app.py            (SQLite fallback, debug reloader)
Run in prod:   gunicorn 'app:create_app()'   (Postgres via DATABASE_URL)
"""
import functools
import hmac
import json
import logging
import os
import secrets
import string
import tempfile
import threading
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

import requests

try:  # web push is optional — the app runs fine without VAPID keys configured
    import pywebpush
except ImportError:  # pragma: no cover
    pywebpush = None
from flask import (
    Flask,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    send_from_directory,
    url_for,
)
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

import ai
import events
import insights
import onedrive
from models import (
    Answer,
    Case,
    CaseStatement,
    Comment,
    Couple,
    DailyQuestion,
    EventItem,
    EventScore,
    MonthlyReport,
    Notification,
    Photo,
    PushSubscription,
    Setting,
    User,
    db,
    notify,
    run_startup_migrations,
)

log = logging.getLogger(__name__)

DEFAULT_APP_NAME = os.environ.get("APP_NAME", "우리의 하루")

# ---- Kakao OAuth 2.0 config (read from env; never hardcode secrets) ----
KAKAO_REST_API_KEY = os.environ.get("KAKAO_REST_API_KEY")
KAKAO_CLIENT_SECRET = os.environ.get("KAKAO_CLIENT_SECRET")  # optional
KAKAO_AUTHORIZE_URL = "https://kauth.kakao.com/oauth/authorize"
KAKAO_TOKEN_URL = "https://kauth.kakao.com/oauth/token"
KAKAO_USERINFO_URL = "https://kapi.kakao.com/v2/user/me"


def kakao_enabled() -> bool:
    """Kakao login is only offered when a REST API key is configured."""
    return bool(KAKAO_REST_API_KEY)


# ---- Web Push (VAPID) config (read from env; NEVER hardcode/commit keys) ----
# One VAPID keypair per deployment. The public key is the browser's
# applicationServerKey; the private key signs pushes. Set once on Render and
# keep stable — rotating them invalidates every existing subscription.
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY")
VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY")
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT")  # e.g. mailto:you@example.com

# Shared secret guarding the daily-reminder cron endpoint.
CRON_SECRET = os.environ.get("CRON_SECRET")

# '데이트 뉴스' 새로고침이 동시에 두 번 돌지 않게 하는 최소 가드(비차단).
_events_refresh_lock = threading.Lock()


def push_enabled() -> bool:
    """Web push is only wired up when a full VAPID keypair + subject exist and
    the pywebpush library is importable."""
    return bool(
        pywebpush and VAPID_PUBLIC_KEY and VAPID_PRIVATE_KEY and VAPID_SUBJECT
    )


def send_push(user, title, body, url):
    """Best-effort Web Push fan-out to every device ``user`` has subscribed.

    Layered on top of the in-app Notification (already persisted by the caller).
    Never raises to the caller — a push-service hiccup must not break the main
    action or the in-app row. Dead subscriptions (404/410) are pruned; other
    errors are logged. No-op when push is disabled or there's no recipient.
    """
    if user is None or not push_enabled():
        return
    subs = PushSubscription.query.filter_by(user_id=user.id).all()
    if not subs:
        return
    payload = json.dumps({"title": title, "body": body, "url": url})
    for sub in subs:
        try:
            pywebpush.webpush(
                subscription_info={
                    "endpoint": sub.endpoint,
                    "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
                },
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_SUBJECT},
            )
        except pywebpush.WebPushException as e:  # noqa: BLE001
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status in (404, 410):
                # Subscription is gone (unsubscribed / expired) — prune it.
                try:
                    db.session.delete(sub)
                    db.session.commit()
                except Exception:  # noqa: BLE001
                    db.session.rollback()
                    log.exception("failed pruning dead push subscription %s", sub.id)
            else:
                log.warning("web push failed (status=%s): %s", status, e)
        except Exception:  # noqa: BLE001 — push must never break the caller
            log.exception("unexpected web push error for user %s", user.id)


_INVITE_ALPHABET = string.ascii_uppercase + string.digits  # unambiguous enough

# ---- minimal in-memory login rate limiting (per IP) ----
_LOGIN_ATTEMPTS: dict[str, list[float]] = {}
_RL_WINDOW = 300  # seconds
_RL_MAX = 8       # attempts per window


def _normalize_db_url(url: str) -> str:
    # Heroku/Fly sometimes hand out postgres://; SQLAlchemy 2.x wants postgresql://
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)
    return url


def create_app():
    app = Flask(__name__)

    db_url = os.environ.get("DATABASE_URL")
    if db_url:
        db_url = _normalize_db_url(db_url)
    else:
        # Local dev fallback: SQLite file in instance/ so it runs without Postgres.
        os.makedirs(app.instance_path, exist_ok=True)
        db_url = "sqlite:///" + os.path.join(app.instance_path, "couple_daily.db")

    is_prod = os.environ.get("FLASK_ENV") == "production" or bool(os.environ.get("DATABASE_URL"))

    app.config.update(
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-insecure-change-me"),
        SQLALCHEMY_DATABASE_URI=db_url,
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=is_prod,  # HTTPS-only cookies in prod
        PERMANENT_SESSION_LIFETIME=timedelta(days=90),  # "로그인 유지" lifetime
        # 요청 하나가 큰 사진 1장(최대 50MB)은 넉넉히 통과하되, 그보다 큰(적대적/
        # 실수) 요청은 여기서 막아 작은 Render 워커를 OOM/타임아웃에서 보호한다.
        # 업로드는 사진 단위로 쪼개 보내므로(memories.html) 이 상한이면 충분하다.
        MAX_CONTENT_LENGTH=55 * 1024 * 1024,  # 55 MB (사진 1장 + 멀티파트 여유)
        SQLALCHEMY_ENGINE_OPTIONS={
            "pool_pre_ping": True,   # 유휴 후 죽은 커넥션 자동 감지·재연결 (콜드스타트 500 원인 제거)
            "pool_recycle": 280,     # Neon/프록시가 끊기 전에 선제 재활용 (초)
        },
    )

    db.init_app(app)
    with app.app_context():
        db.create_all()
        # Bring pre-existing tables (live Postgres) up to date for Kakao login.
        run_startup_migrations()
        # Seed the configurable app name once.
        if Setting.get("app_name") is None:
            Setting.set("app_name", DEFAULT_APP_NAME)
            db.session.commit()

    _register_routes(app)
    return app


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    return db.session.get(User, uid)


def _safe_notify(recipient, type, message, link):
    """Create + commit a Notification without ever breaking the caller's action.

    The main action (answer/comment/approval) is already committed by the time
    this runs; a notification failure here is logged and swallowed so it can
    never undo or block that action. Skips silently when there's no recipient.

    This is the single fan-out point the future web-push branch will extend:
    persist the in-app row here, then also enqueue a push to ``recipient``.
    """
    if recipient is None:
        return
    try:
        notify(recipient, type, message, link)
        db.session.commit()
    except Exception:  # noqa: BLE001 — a notification must never break the action
        db.session.rollback()
        log.exception("notify failed (type=%s recipient=%s)", type, getattr(recipient, "id", None))
        return
    # In-app row is persisted; ALSO fan out a Web Push to the same recipient.
    # send_push never raises and is a no-op when push is disabled, so this can
    # neither undo the in-app row nor break the main action.
    send_push(recipient, Setting.get("app_name", DEFAULT_APP_NAME), message, link)


def _gen_invite_code():
    for _ in range(20):
        code = "".join(secrets.choice(_INVITE_ALPHABET) for _ in range(8))
        if not Couple.query.filter_by(invite_code=code).first():
            return code
    raise RuntimeError("could not generate a unique invite code")


def _resolve_couple_for_join(invite_code: str):
    """Shared couple-attach logic for both email signup and Kakao connect.

    ``invite_code`` must already be normalized (stripped/upper-cased); "" means
    "create a new space". Returns ``(couple, is_admin, status, error)`` — on any
    validation failure ``couple`` is None and ``error`` is a Korean message.

    Mirrors the original signup rules exactly so both paths stay in sync:
      * no code  → create a new Couple, become admin, status 'approved'
      * has code → join an existing (non-full) Couple, status 'pending'
    """
    if invite_code:
        couple = Couple.query.filter_by(invite_code=invite_code).first()
        if not couple:
            return None, None, None, "초대 코드가 올바르지 않아."
        if couple.is_full:
            return None, None, None, "이 커플 공간은 이미 두 명이 꽉 찼어."
        return couple, False, "pending", None
    # First user → creates the couple space, becomes admin.
    couple = Couple(invite_code=_gen_invite_code())
    db.session.add(couple)
    db.session.flush()  # get couple.id
    return couple, True, "approved", None


def login_required(view):
    @functools.wraps(view)
    def wrapped(*a, **kw):
        if not current_user():
            return redirect(url_for("login", next=request.path))
        return view(*a, **kw)

    return wrapped


def active_couple_required(view):
    """Only approved + linked users may use the core features."""
    @functools.wraps(view)
    def wrapped(*a, **kw):
        u = current_user()
        if not u:
            return redirect(url_for("login"))
        if not u.is_active_couple:
            return redirect(url_for("index"))
        return view(*a, **kw)

    return wrapped


def _rate_limited(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _LOGIN_ATTEMPTS.get(ip, []) if now - t < _RL_WINDOW]
    _LOGIN_ATTEMPTS[ip] = hits
    return len(hits) >= _RL_MAX


def _record_attempt(ip: str):
    _LOGIN_ATTEMPTS.setdefault(ip, []).append(time.time())


def get_or_create_today_question(couple: Couple) -> DailyQuestion:
    today = date.today()
    q = DailyQuestion.query.filter_by(couple_id=couple.id, q_date=today).first()
    if q:
        return q

    # Personalize from recent history (most recent first, excluding today).
    recent = (
        DailyQuestion.query.filter(
            DailyQuestion.couple_id == couple.id, DailyQuestion.q_date < today
        )
        .order_by(DailyQuestion.q_date.desc())
        .limit(8)
        .all()
    )
    recent_pairs = [
        {"question": r.text, "answers": [a.text for a in r.answers.all()]}
        for r in recent
    ]
    text, source = ai.generate_daily_question(recent_pairs)

    q = DailyQuestion(couple_id=couple.id, q_date=today, text=text, source=source)
    db.session.add(q)
    try:
        db.session.commit()
    except IntegrityError:
        # Partner generated it concurrently — take theirs.
        db.session.rollback()
        q = DailyQuestion.query.filter_by(couple_id=couple.id, q_date=today).first()
    return q


def build_comment_thread(q: DailyQuestion):
    """Return ``[(top_comment, [replies…]), …]`` for a question, oldest-first.

    Comments form an adjacency list (``parent_id``). For a calm, readable UI we
    render exactly two visual levels: each top-level comment, then a *flat* list
    of all its descendants (any depth) at a single reply indent. Returns the
    grouped thread plus the total comment count.
    """
    comments = q.ordered_comments()  # already oldest-first
    by_id = {c.id: c for c in comments}
    tops = []
    replies_of = {}
    for c in comments:
        # Walk up to the top-level ancestor (guards against orphaned parents).
        root = c
        while root.parent_id and root.parent_id in by_id:
            root = by_id[root.parent_id]
        if root.id == c.id:
            tops.append(c)
        else:
            replies_of.setdefault(root.id, []).append(c)
    thread = [(t, replies_of.get(t.id, [])) for t in tops]
    return thread, len(comments)


# --------------------------------------------------------------------------- #
# Monthly report — cached AI qualitative insight, (re)built in the background
# --------------------------------------------------------------------------- #
# The /insight page must render instantly from the cached MonthlyReport row and
# NEVER run the slow `claude -p` subprocess on the request path. Generation is
# offloaded to a daemon thread that owns its own app context + DB session.
#
# Concurrency guards (two layers):
#   * DB-level: the report row's status='generating' + updated_at. A fresh
#     'generating' means a build is in flight; a 'generating' older than
#     _STUCK_GENERATING is treated as stale (a crashed/killed thread) and is
#     retryable so a month can never get stuck showing the placeholder forever.
#   * In-process: a set of (couple_id, year, month) keys currently generating,
#     protected by a lock. This is the fast, exact guard for the single-worker
#     Render free tier (one gunicorn worker) so rapid events don't double-spawn.
_generating_lock = threading.Lock()
_generating: set[tuple[int, int, int]] = set()
_STUCK_GENERATING = timedelta(minutes=5)


def regenerate_monthly_report(app, couple_id, year, month):
    """Background worker: (re)build the cached qualitative report for a month.

    Runs in a daemon thread. Pushes a fresh app context (which yields a NEW
    thread-local scoped DB session — request sessions are never shared across
    threads) and calls the slow `claude -p` pass, then upserts the row:
      * success  → content fields + status='ready' + generated_at.
      * failure/None → status='failed', UNLESS the row already had a good
        report (generated_at set), in which case the prior 'ready' content is
        kept so the user still sees the last good insight.
    Never raises out of the thread; always frees the in-process guard key.
    """
    key = (couple_id, year, month)
    try:
        with app.app_context():
            try:
                couple = db.session.get(Couple, couple_id)
                members = couple.approved_members if couple else []
                if len(members) < 2:
                    return  # solo space — nothing two-person to summarize
                user_a, user_b = members[0], members[1]

                qa_items = insights.collect_month_qa(
                    couple_id, year, month, user_a, user_b
                )
                month_label = f"{year}년 {month}월"
                try:
                    qualitative = ai.generate_monthly_qualitative(
                        month_label, user_a.display_name, user_b.display_name, qa_items
                    )
                except Exception:  # noqa: BLE001 — claude must never crash the thread
                    log.exception(
                        "claude monthly insight raised (couple=%s %s-%s)",
                        couple_id, year, month,
                    )
                    qualitative = None

                report = MonthlyReport.query.filter_by(
                    couple_id=couple_id, year=year, month=month
                ).first()
                if report is None:
                    report = MonthlyReport(
                        couple_id=couple_id, year=year, month=month
                    )
                    db.session.add(report)

                now = datetime.utcnow()
                if qualitative:
                    report.summary = qualitative.get("summary") or ""
                    report.themes = json.dumps(
                        qualitative.get("themes") or [], ensure_ascii=False
                    )
                    report.tone = qualitative.get("tone") or ""
                    report.divergent_question = qualitative.get("divergent_question") or ""
                    report.fun = qualitative.get("fun") or ""
                    report.status = "ready"
                    report.generated_at = now
                elif report.generated_at is not None:
                    # Generation failed but we have prior good content — keep it
                    # visible rather than degrading to a placeholder.
                    report.status = "ready"
                else:
                    report.status = "failed"
                report.updated_at = now

                try:
                    db.session.commit()
                except Exception:  # noqa: BLE001
                    db.session.rollback()
                    log.exception(
                        "commit failed for monthly report (couple=%s %s-%s)",
                        couple_id, year, month,
                    )
            except Exception:  # noqa: BLE001 — belt & suspenders; never escape
                db.session.rollback()
                log.exception(
                    "regenerate_monthly_report failed (couple=%s %s-%s)",
                    couple_id, year, month,
                )
    finally:
        with _generating_lock:
            _generating.discard(key)


def _kick_monthly_report(couple_id, year, month):
    """Atomically claim a month for generation and spawn the background thread.

    Must be called inside an app/request context. No-ops (does NOT spawn) when a
    build is already in flight (in-process key held, or a fresh DB 'generating').
    Marks the row status='generating' (creating it if missing) and commits BEFORE
    spawning, so a concurrent request sees the claim. Content columns are left
    untouched so a prior good report survives the regen window.
    """
    key = (couple_id, year, month)
    now = datetime.utcnow()

    report = MonthlyReport.query.filter_by(
        couple_id=couple_id, year=year, month=month
    ).first()
    # A fresh in-flight DB claim → don't pile on. A stale one (crashed thread) is
    # retryable so the month can't be wedged in 'generating' forever.
    if (
        report is not None
        and report.status == "generating"
        and report.updated_at
        and (now - report.updated_at) < _STUCK_GENERATING
    ):
        return

    with _generating_lock:
        if key in _generating:
            return
        _generating.add(key)

    spawned = False
    try:
        if report is None:
            report = MonthlyReport(
                couple_id=couple_id, year=year, month=month, status="generating"
            )
            db.session.add(report)
        else:
            report.status = "generating"
        report.updated_at = now
        try:
            db.session.commit()
        except IntegrityError:
            # A concurrent request created the row first — take theirs.
            db.session.rollback()
            report = MonthlyReport.query.filter_by(
                couple_id=couple_id, year=year, month=month
            ).first()
            if report is None:
                raise
            report.status = "generating"
            report.updated_at = now
            db.session.commit()

        threading.Thread(
            target=regenerate_monthly_report,
            args=(current_app._get_current_object(), couple_id, year, month),
            daemon=True,
        ).start()
        spawned = True
    except Exception:  # noqa: BLE001
        db.session.rollback()
        log.exception(
            "failed to kick monthly report (couple=%s %s-%s)",
            couple_id, year, month,
        )
    finally:
        if not spawned:
            with _generating_lock:
                _generating.discard(key)


# --------------------------------------------------------------------------- #
# Photo captioning (claude vision) — background, never on the request path
# --------------------------------------------------------------------------- #
# Same discipline as the monthly report: a slow `claude` subprocess (here a
# vision pass) must run off the request thread with its own app context + fresh
# DB session, catch everything, and never escape the thread. Captioning is
# per-photo (each photo is unique), so a simple in-process guard set keyed by
# photo_id is enough to stop a double-spawn for the same photo.
_captioning_lock = threading.Lock()
_captioning: set[int] = set()

# 캡션 동시성 상한 — 기본 1(직렬화). 단일 워커 512MB 무료 티어는 `claude`(Node)
# 프로세스를 한 번에 하나밖에 못 버틴다: N개의 캡션 스레드가 동시에 claude를 띄우면
# OOM 나서 캡션이 실패한다(질문 생성은 한 개라 프로덕션에서 정상 동작). 이 세마포어로
# 여러 스레드가 줄 서서 사진 바이트 로드+vision 호출을 하나씩 순차 처리하게 한다.
_CAPTION_SEM = threading.Semaphore(int(os.environ.get("CAPTION_MAX_CONCURRENCY", "1")))

# Image extensions claude's Read tool recognizes; used to give the temp file the
# right suffix so vision actually ingests it. Falls back to .jpg.
_CAPTION_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}


def _caption_tmp_dir():
    """A scratch dir for vision temp images, INSIDE the process working dir.

    Load-bearing: `claude` sandboxes its file reads to its working directory
    (the subprocess cwd == the app's cwd). A temp image written to the SYSTEM
    temp dir (%TEMP% / /tmp) is outside that sandbox and claude refuses to read
    it — verified. Writing under cwd keeps the image inside the sandbox so vision
    actually ingests it. The dir is git-ignored and files are deleted after use.
    """
    d = os.path.join(os.getcwd(), ".caption-tmp")
    os.makedirs(d, exist_ok=True)
    return d


def caption_photo(app, photo_id):
    """Background worker: caption ONE uploaded photo with `claude` vision.

    Pushes a fresh app context (→ a new thread-local DB session), loads the
    Photo, downloads its bytes from OneDrive to a temp file with the correct
    extension, runs the slow vision pass, and stores the result:
      * success → ``caption`` + ``tags`` (JSON) + ``caption_status='ready'``.
      * failure/timeout/parse-error → ``caption_status='failed'`` (caption null).
    ALWAYS deletes the temp file (finally) and ALWAYS frees the guard key. Never
    raises out of the thread.
    """
    with _captioning_lock:
        if photo_id in _captioning:
            return  # already being captioned — don't pile on
        _captioning.add(photo_id)

    try:
        with app.app_context():
            tmp_path = None
            try:
                photo = db.session.get(Photo, photo_id)
                if photo is None:
                    return  # deleted before we got to it

                item_id = photo.onedrive_item_id
                name_for_ext = photo.filename or photo.original_name or ""

                # 메모리 무거운 구간(사진 바이트 다운로드 + 임시파일 기록 + claude
                # vision)을 세마포어로 직렬화한다: 단일 워커 512MB 티어는 한 번에 사진
                # 하나의 바이트만 메모리에 올리고 claude(Node) 프로세스도 하나만 돌릴 수
                # 있다(동시 실행 시 OOM → 캡션 실패). 다른 캡션 스레드는 여기서 줄 서서
                # 하나씩 순차 처리된다.
                _CAPTION_SEM.acquire()
                try:
                    try:
                        data, _ctype = onedrive.get_photo_content(item_id)
                    except onedrive.OneDriveError:
                        log.exception(
                            "caption_photo: OneDrive fetch failed (photo=%s)", photo_id
                        )
                        data = None

                    if not data:
                        photo.caption_status = "failed"
                        db.session.commit()
                        return

                    ext = os.path.splitext(name_for_ext)[1].lower()
                    if ext not in _CAPTION_IMAGE_EXTS:
                        ext = ".jpg"
                    fd, tmp_path = tempfile.mkstemp(
                        prefix="cd_caption_", suffix=ext, dir=_caption_tmp_dir()
                    )
                    with os.fdopen(fd, "wb") as fh:
                        fh.write(data)

                    result = ai.caption_image(tmp_path)
                finally:
                    _CAPTION_SEM.release()

                # Re-load in case the row changed; commit the outcome.
                photo = db.session.get(Photo, photo_id)
                if photo is None:
                    return
                if result and result.get("caption"):
                    photo.caption = (result["caption"] or "")[:1000]
                    photo.tags = json.dumps(
                        result.get("tags") or [], ensure_ascii=False
                    )
                    photo.caption_status = "ready"
                else:
                    photo.caption_status = "failed"
                db.session.commit()
            except Exception:  # noqa: BLE001 — never let the thread crash
                db.session.rollback()
                log.exception("caption_photo failed (photo=%s)", photo_id)
                # Best-effort: don't leave the row stuck on 'pending' forever.
                try:
                    photo = db.session.get(Photo, photo_id)
                    if photo is not None and photo.caption_status == "pending":
                        photo.caption_status = "failed"
                        db.session.commit()
                except Exception:  # noqa: BLE001
                    db.session.rollback()
            finally:
                if tmp_path and os.path.exists(tmp_path):
                    try:
                        os.remove(tmp_path)
                    except OSError:
                        log.exception(
                            "caption_photo: temp cleanup failed (%s)", tmp_path
                        )
    finally:
        with _captioning_lock:
            _captioning.discard(photo_id)


def _spawn_caption_if_idle(app, photo_id):
    """Spawn a background caption thread for ONE photo unless it's already
    queued/running (checked against the same ``_captioning`` guard that
    ``caption_photo`` uses, so we never pile a second thread on the same photo).
    Best-effort — never raises. Safe now that captioning is serialized via the
    semaphore, so re-spawning many pending photos just queues them one at a time.
    """
    with _captioning_lock:
        if photo_id in _captioning:
            return
    try:
        threading.Thread(
            target=caption_photo, args=(app, photo_id), daemon=True
        ).start()
    except Exception:  # noqa: BLE001 — captioning is best-effort
        log.exception("failed to spawn caption thread for photo %s", photo_id)


# --------------------------------------------------------------------------- #
# 커플 싸움 AI 판사 — background verdict, never on the request path
# --------------------------------------------------------------------------- #
# Same discipline as the monthly report / photo captioner: the slow `claude -p`
# judge pass runs off the request thread with its own app context + fresh DB
# session, catches everything, and never escapes the thread. Per-case in-process
# guard set keyed by case_id stops a double-spawn for the same case.
_judging_lock = threading.Lock()
_judging: set[int] = set()


def judge_case(app, case_id):
    """Background worker: run the AI judge for ONE case, mirroring
    ``regenerate_monthly_report``.

    Pushes a fresh app context (→ a NEW thread-local DB session — request
    sessions are never shared across threads), loads the Case + its statements +
    the two partners' display names, runs the slow judge pass, and upserts the
    outcome:
      * success  → ``verdict_json`` + status='decided' + decided_at.
      * failure/None → status='failed', UNLESS the case already had a good
        verdict (decided_at set), in which case the prior verdict is kept
        ('decided') so the couple still sees the last good judgment.
    Never raises out of the thread; always frees the in-process guard key.
    """
    try:
        with app.app_context():
            try:
                case = db.session.get(Case, case_id)
                if case is None:
                    return  # deleted before we got to it

                couple = db.session.get(Couple, case.couple_id)
                members = couple.approved_members if couple else []
                # Two people (any order) — used only for display names in the
                # prompt. A statement author who left may not be a current member,
                # so resolve names per-statement below, falling back to members.
                name_a = members[0].display_name if len(members) >= 1 else "한 사람"
                name_b = members[1].display_name if len(members) >= 2 else "상대"

                statements = []
                for st in case.statements:
                    author = db.session.get(User, st.user_id)
                    nm = author.display_name if author else "익명"
                    statements.append({"name": nm, "text": st.text})

                try:
                    result = ai.judge_fight(
                        name_a, name_b, case.situation, statements
                    )
                except Exception:  # noqa: BLE001 — claude must never crash the thread
                    log.exception("claude fight judgment raised (case=%s)", case_id)
                    result = None

                # Re-load in case the row changed while claude ran.
                case = db.session.get(Case, case_id)
                if case is None:
                    return
                now = datetime.utcnow()
                if result:
                    case.verdict_json = json.dumps(result, ensure_ascii=False)
                    case.status = "decided"
                    case.decided_at = now
                elif case.decided_at is not None:
                    # Judgment failed but we have a prior good verdict — keep it
                    # visible rather than degrading to a failed state.
                    case.status = "decided"
                else:
                    case.status = "failed"
                case.updated_at = now

                try:
                    db.session.commit()
                except Exception:  # noqa: BLE001
                    db.session.rollback()
                    log.exception("commit failed for case verdict (case=%s)", case_id)
            except Exception:  # noqa: BLE001 — belt & suspenders; never escape
                db.session.rollback()
                log.exception("judge_case failed (case=%s)", case_id)
    finally:
        with _judging_lock:
            _judging.discard(case_id)


# --------------------------------------------------------------------------- #
# 데이트 뉴스 커플 맞춤 추천 점수 (P2) — 취향 프로필(비-AI) + 배치 채점(백그라운드)
# --------------------------------------------------------------------------- #
# 채점은 커플 데이터에서 만든 '취향 프로필'을 근거로 한다. 프로필 조립은 순전히
# 이 커플 자신의 데이터를 뽑아 이어붙이는 값싼 비-AI 작업이고(여기 claude 없음),
# 느린 claude 배치 채점은 아래 백그라운드 워커에서만 돈다.
_PROFILE_MAXLEN = 1400  # 프로필 총 길이 상한(프롬프트 폭주 방지)


def build_couple_taste_profile(couple) -> str:
    """이 커플 자신의 데이터로 짧은 한국어 취향 프로필 텍스트를 만든다(비-AI).

    claude를 부르지 않는다 — 최근 오늘의질문 답변·추억 캡션/태그·(옵션)지난 판사
    사건 상황을 사실 그대로 이어붙여 요약한다. 총 길이를 몇백 자로 제한한다.
    데이터가 사실상 없으면 ""를 반환한다(그럼 채점은 중립 프로필로도 동작한다).
    호출부(백그라운드 워커)가 app_context 안에서 부른다.
    """
    if couple is None:
        return ""
    parts = []

    try:
        # 1) 최근 오늘의질문 답변(양쪽) — 표현된 관심사·기분.
        recent_qs = (
            DailyQuestion.query.filter_by(couple_id=couple.id)
            .order_by(DailyQuestion.q_date.desc())
            .limit(10)
            .all()
        )
        ans_lines = []
        for q in recent_qs:
            for a in q.answers.all():
                t = (a.text or "").strip()
                if t:
                    ans_lines.append(t[:120])
        if ans_lines:
            parts.append("[최근 오늘의질문 답변]\n" + "\n".join(f"- {t}" for t in ans_lines[:16]))
    except Exception:  # noqa: BLE001 — 프로필은 best-effort, 절대 터지지 않게
        db.session.rollback()
        log.debug("taste profile: 답변 수집 실패", exc_info=True)

    try:
        # 2) 최근 추억 사진 캡션 + 태그(ready) — 뭘 찍고 좋아하는지.
        photos = (
            Photo.query.filter_by(couple_id=couple.id, caption_status="ready")
            .order_by(Photo.created_at.desc(), Photo.id.desc())
            .limit(20)
            .all()
        )
        cap_lines = []
        tag_bag = []
        for p in photos:
            c = (p.caption or "").strip()
            if c:
                cap_lines.append(c[:120])
            for tg in p.tags_list:
                tg = str(tg).strip()
                if tg and tg not in tag_bag:
                    tag_bag.append(tg)
        if cap_lines:
            parts.append("[추억 사진 속 모습]\n" + "\n".join(f"- {c}" for c in cap_lines[:12]))
        if tag_bag:
            parts.append("[자주 담는 것들] " + ", ".join(tag_bag[:20]))
    except Exception:  # noqa: BLE001
        db.session.rollback()
        log.debug("taste profile: 사진 수집 실패", exc_info=True)

    try:
        # 3) (옵션) 지난 판사 사건 상황 — 민감할 수 있는 주제(피하기용 참고).
        cases = (
            Case.query.filter_by(couple_id=couple.id)
            .order_by(Case.created_at.desc())
            .limit(5)
            .all()
        )
        sit_lines = []
        for c in cases:
            s = (c.title or c.situation or "").strip()
            if s:
                sit_lines.append(s[:80])
        if sit_lines:
            parts.append(
                "[가끔 부딪히는 지점(자극 피하기 참고)]\n"
                + "\n".join(f"- {s}" for s in sit_lines[:5])
            )
    except Exception:  # noqa: BLE001
        db.session.rollback()
        log.debug("taste profile: 사건 수집 실패", exc_info=True)

    profile = "\n\n".join(parts).strip()
    if len(profile) > _PROFILE_MAXLEN:
        profile = profile[:_PROFILE_MAXLEN].rstrip()
    return profile


# 백그라운드 채점 가드 — 같은 커플에 두 패스가 겹치지 않게 하는 in-process 집합
# (캡션의 _captioning과 같은 패턴). claude 호출은 아래에서 캡션과 '같은'
# _CAPTION_SEM으로 직렬화해 512MB 티어에서 동시에 claude가 둘 이상 뜨지 않게 한다.
_scoring_lock = threading.Lock()
_scoring: set[int] = set()
_SCORE_BATCH_DEFAULT = 24  # 한 패스당 채점 상한(배치 1콜 크기)


def score_events_for_couple(app, couple_id, limit=_SCORE_BATCH_DEFAULT):
    """백그라운드 워커: 이 커플에 아직 점수 없는 진행 중 행사들을 배치로 채점한다.

    caption_photo와 같은 규율: 자기 app_context(→ 새 스레드로컬 세션)를 열고,
    캡션과 공유하는 _CAPTION_SEM으로 claude 배치 콜을 직렬화하고, 모든 예외를
    삼키며 스레드 밖으로 절대 raise 안 하고, finally에서 가드 키를 해제한다.

    선택: 진행 중(end_date NULL 또는 >= 오늘) 행사 중 이 커플에 'ready' 점수가
    없는(행 없거나 status!='ready') 것들을 마감 임박 순으로 최대 ``limit``개.
    없으면 그냥 반환. 선택된 것들에 'pending' 행을 만들어(UI가 "분석 중" 표시)
    두고, 프로필을 한 번 만들고, 배치 페이로드를 꾸려 ai.score_events를 '한 번'
    호출한다. 돌아온 ref → score/reason/status='ready'; 안 돌아온 ref →
    status='failed'(이 패스에서 무한 재시도 방지; 이후 패스가 재시도 가능).
    """
    with _scoring_lock:
        if couple_id in _scoring:
            return  # 이미 이 커플 채점 중 — 겹치지 않게
        _scoring.add(couple_id)

    try:
        with app.app_context():
            try:
                couple = db.session.get(Couple, couple_id)
                if couple is None:
                    return
                today = date.today()

                # 진행 중 행사 중 이 커플에 'ready' 점수가 없는 것 — 마감 임박 순.
                # LEFT JOIN으로 EventScore가 없거나 ready가 아닌 행만 고른다.
                rows = (
                    db.session.query(EventItem, EventScore)
                    .outerjoin(
                        EventScore,
                        db.and_(
                            EventScore.event_id == EventItem.id,
                            EventScore.couple_id == couple_id,
                        ),
                    )
                    .filter(
                        db.or_(
                            EventItem.end_date.is_(None),
                            EventItem.end_date >= today,
                        ),
                        db.or_(
                            EventScore.id.is_(None),
                            EventScore.status != "ready",
                        ),
                    )
                    .order_by(
                        (EventItem.end_date.is_(None)),
                        EventItem.end_date.asc(),
                        EventItem.start_date.asc(),
                    )
                    .limit(int(limit) if limit else _SCORE_BATCH_DEFAULT)
                    .all()
                )
                if not rows:
                    return

                # 선택된 행에 EventScore 행을 'pending'으로 보장(UI "분석 중").
                selected = []  # (event, score_row)
                for ev, sc_row in rows:
                    if sc_row is None:
                        sc_row = EventScore(
                            couple_id=couple_id, event_id=ev.id, status="pending"
                        )
                        db.session.add(sc_row)
                    else:
                        sc_row.status = "pending"
                    selected.append((ev, sc_row))
                try:
                    db.session.commit()
                except IntegrityError:
                    # 동시 패스가 행을 먼저 만들었을 수 있음 — 롤백하고 이번 패스는
                    # 양보(다음 on-view 트리거가 재시도).
                    db.session.rollback()
                    return

                # 프로필 1회 + 배치 페이로드.
                profile_text = build_couple_taste_profile(couple)
                batch = [
                    {
                        "ref": ev.id,
                        "title": ev.title,
                        "category": ev.category,
                        "place": ev.place,
                        "description": ev.description,
                    }
                    for ev, _ in selected
                ]

                # 캡션과 '같은' 세마포어로 claude 배치 콜을 직렬화(512MB: 동시에
                # claude 하나만). claude가 터져도 실패로만 취급.
                _CAPTION_SEM.acquire()
                try:
                    try:
                        results = ai.score_events(profile_text, batch)
                    except Exception:  # noqa: BLE001 — claude가 스레드 죽이지 못하게
                        log.exception(
                            "claude event scoring raised (couple=%s)", couple_id
                        )
                        results = []
                finally:
                    _CAPTION_SEM.release()

                by_ref = {}
                for r in results or []:
                    ref = r.get("ref")
                    if ref is not None:
                        by_ref[ref] = r

                # 결과 반영: 받은 ref → ready, 안 받은 ref → failed.
                now = datetime.utcnow()
                for ev, sc_row in selected:
                    r = by_ref.get(ev.id)
                    if r is not None:
                        sc_row.score = r.get("score")
                        sc_row.reason = (r.get("reason") or "")[:1000]
                        sc_row.status = "ready"
                    else:
                        sc_row.status = "failed"
                    sc_row.updated_at = now
                try:
                    db.session.commit()
                except Exception:  # noqa: BLE001
                    db.session.rollback()
                    log.exception(
                        "commit failed for event scores (couple=%s)", couple_id
                    )
            except Exception:  # noqa: BLE001 — belt & suspenders; 절대 탈출 금지
                db.session.rollback()
                log.exception("score_events_for_couple failed (couple=%s)", couple_id)
    finally:
        with _scoring_lock:
            _scoring.discard(couple_id)


def _spawn_scoring_if_idle(app, couple_id):
    """이 커플 채점 백그라운드 스레드를 스폰(이미 큐/진행 중이면 no-op).

    dates() 뷰가 반복 조회돼도 스레드가 쌓이지 않도록 score_events_for_couple과
    같은 _scoring 가드로 중복을 떨군다. best-effort — 절대 raise 안 함.
    """
    with _scoring_lock:
        if couple_id in _scoring:
            return
    try:
        threading.Thread(
            target=score_events_for_couple, args=(app, couple_id), daemon=True
        ).start()
    except Exception:  # noqa: BLE001 — 채점은 best-effort
        log.exception("failed to spawn scoring thread for couple %s", couple_id)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def _register_routes(app: Flask):
    @app.context_processor
    def inject_globals():
        # DB 장애 내성: 유휴 후 죽은 커넥션 등으로 DB가 잠깐 실패해도 여기서
        # 터지면 *모든* 페이지(에러 페이지 포함)가 함께 무너진다. DB를 만지는
        # 부분은 감싸서 실패 시 안전한 기본값으로 폴백한다(정상 경로는 그대로).
        me = None
        notif_unread = 0
        app_name = DEFAULT_APP_NAME
        try:
            me = current_user()
            # Cheap unread COUNT for the topbar bell dot — only for a logged-in user
            # who's in a couple (the only place the bell is shown).
            if me is not None and me.couple_id:
                notif_unread = (
                    Notification.query.filter_by(user_id=me.id, is_read=False).count()
                )
            app_name = Setting.get("app_name", DEFAULT_APP_NAME)
        except Exception:
            # 오염된 트랜잭션을 비우고 안전한 기본값으로 계속 렌더한다.
            db.session.rollback()
            log.debug("inject_globals: DB 조회 실패 — 안전 기본값으로 폴백", exc_info=True)
            me = None
            notif_unread = 0
            app_name = DEFAULT_APP_NAME
        return {
            "app_name": app_name,
            "me": me,
            "kakao_enabled": kakao_enabled(),
            "push_enabled": push_enabled(),
            "onedrive_enabled": onedrive.onedrive_enabled(),
            "notif_unread": notif_unread,
        }

    @app.template_filter("timeago")
    def _timeago(dt):
        """Short Korean relative time. Comment timestamps are UTC (utcnow)."""
        if not dt:
            return ""
        secs = (datetime.utcnow() - dt).total_seconds()
        if secs < 60:
            return "방금"
        mins = int(secs // 60)
        if mins < 60:
            return f"{mins}분 전"
        hours = int(mins // 60)
        if hours < 24:
            return f"{hours}시간 전"
        days = int(hours // 24)
        if days < 7:
            return f"{days}일 전"
        return dt.strftime("%Y.%m.%d")

    # ---- landing / dashboard ----
    @app.route("/")
    def index():
        u = current_user()
        if not u:
            return redirect(url_for("login"))
        if not u.couple_id:
            # Shouldn't normally happen (signup always attaches a couple), but guard.
            return render_template("no_couple.html")
        if u.status == "pending":
            return render_template("pending.html")

        couple = u.couple
        # Admin waiting for a partner to accept.
        pending_partner = couple.members.filter_by(status="pending").first()
        if u.is_admin and pending_partner:
            return render_template(
                "approve.html", pending=pending_partner, invite_code=couple.invite_code
            )
        # Approved but partner hasn't joined yet.
        if len(couple.approved_members) < 2:
            return render_template("waiting_partner.html", invite_code=couple.invite_code)

        # Full, active couple → today's question.
        q = get_or_create_today_question(couple)
        partner = u.partner
        my_ans = q.answer_by(u.id)
        partner_ans = q.answer_by(partner.id) if partner else None
        revealed = q.both_answered
        thread, comment_count = build_comment_thread(q) if revealed else ([], 0)
        return render_template(
            "dashboard.html",
            question=q,
            my_ans=my_ans,
            partner_ans=partner_ans,
            partner=partner,
            revealed=revealed,
            today=q.q_date,
            thread=thread,
            comment_count=comment_count,
        )

    # ---- auth ----
    @app.route("/signup", methods=["GET", "POST"])
    def signup():
        if current_user():
            return redirect(url_for("index"))
        if request.method == "POST":
            email = (request.form.get("email") or "").strip().lower()
            password = request.form.get("password") or ""
            display_name = (request.form.get("display_name") or "").strip()
            invite_code = (request.form.get("invite_code") or "").strip().upper()

            if not email or not password or not display_name:
                flash("이메일, 비밀번호, 이름을 모두 입력해줘.", "error")
                return render_template("signup.html")
            if len(password) < 8:
                flash("비밀번호는 8자 이상으로 해줘.", "error")
                return render_template("signup.html")
            if User.query.filter_by(email=email).first():
                flash("이미 가입된 이메일이야.", "error")
                return render_template("signup.html")

            couple, is_admin, status, err = _resolve_couple_for_join(invite_code)
            if err:
                flash(err, "error")
                return render_template("signup.html")
            user = User(
                email=email,
                password_hash=generate_password_hash(password),
                display_name=display_name,
                couple_id=couple.id,
                is_admin=is_admin,
                status=status,
            )
            db.session.add(user)
            db.session.commit()
            session.clear()
            session["user_id"] = user.id
            session.permanent = True  # 새 커플은 로그인 상태 유지
            return redirect(url_for("index"))
        return render_template("signup.html")

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if current_user():
            return redirect(url_for("index"))
        if request.method == "POST":
            ip = request.remote_addr or "unknown"
            if _rate_limited(ip):
                flash("로그인 시도가 너무 많아. 잠시 후 다시 시도해줘.", "error")
                return render_template("login.html"), 429
            email = (request.form.get("email") or "").strip().lower()
            password = request.form.get("password") or ""
            user = User.query.filter_by(email=email).first()
            if not user or not check_password_hash(user.password_hash, password):
                _record_attempt(ip)
                flash("이메일 또는 비밀번호가 맞지 않아.", "error")
                return render_template("login.html")
            session.clear()
            session["user_id"] = user.id
            # "로그인 유지" 체크 시 90일 지속 쿠키, 아니면 브라우저 세션 쿠키.
            session.permanent = bool(request.form.get("remember"))
            nxt = request.args.get("next")
            return redirect(nxt or url_for("index"))
        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    def logout():
        session.clear()
        return redirect(url_for("login"))

    # ---- Kakao social login ----
    def _kakao_redirect_uri() -> str:
        # Must exactly match the redirect URI registered in the Kakao console.
        # Force https: on Render we sit behind a TLS proxy, so _external alone
        # can yield http.
        return url_for("kakao_callback", _external=True, _scheme="https")

    def _kakao_start_authorize():
        # Build a fresh CSRF state, stash it, and hand off to Kakao's authorize
        # endpoint. Shared by both the login flow and the link-existing flow so
        # they stay in lockstep on state handling and redirect_uri.
        state = secrets.token_urlsafe(24)
        session["kakao_oauth_state"] = state
        params = {
            "client_id": KAKAO_REST_API_KEY,
            "redirect_uri": _kakao_redirect_uri(),
            "response_type": "code",
            "state": state,
        }
        return redirect(KAKAO_AUTHORIZE_URL + "?" + urlencode(params))

    @app.route("/auth/kakao/login")
    def kakao_login():
        if current_user():
            return redirect(url_for("index"))
        if not kakao_enabled():
            flash("카카오 로그인이 설정되지 않았어.", "error")
            return redirect(url_for("login"))
        # Plain login: make sure we're not carrying a stale link intent.
        session.pop("kakao_link_mode", None)
        return _kakao_start_authorize()

    @app.route("/auth/kakao/link")
    @login_required
    def kakao_link():
        """Link Kakao to the *currently logged-in* account (no new account)."""
        if not kakao_enabled():
            flash("카카오 로그인이 설정되지 않았어.", "error")
            return redirect(url_for("settings"))
        if current_user().kakao_id:
            flash("이미 카카오가 연결된 계정이야.", "error")
            return redirect(url_for("settings"))
        session["kakao_link_mode"] = True
        return _kakao_start_authorize()

    @app.route("/auth/kakao/callback")
    def kakao_callback():
        if not kakao_enabled():
            abort(404)
        # Verify state (CSRF protection). Pop so it can't be replayed.
        expected = session.pop("kakao_oauth_state", None)
        got = request.args.get("state")
        if not expected or not got or not secrets.compare_digest(expected, got):
            flash("카카오 로그인 검증에 실패했어. 다시 시도해줘.", "error")
            return redirect(url_for("login"))
        if request.args.get("error"):
            flash("카카오 로그인이 취소됐어.", "error")
            return redirect(url_for("login"))
        code = request.args.get("code")
        if not code:
            flash("카카오 인증 코드를 받지 못했어.", "error")
            return redirect(url_for("login"))

        # Exchange authorization code for an access token.
        token_data = {
            "grant_type": "authorization_code",
            "client_id": KAKAO_REST_API_KEY,
            "redirect_uri": _kakao_redirect_uri(),
            "code": code,
        }
        if KAKAO_CLIENT_SECRET:
            token_data["client_secret"] = KAKAO_CLIENT_SECRET
        try:
            tr = requests.post(KAKAO_TOKEN_URL, data=token_data, timeout=10)
            tr.raise_for_status()
            access_token = tr.json().get("access_token")
        except requests.RequestException:
            log.exception("kakao token exchange failed")  # never log token body
            access_token = None
        if not access_token:
            flash("카카오 인증에 실패했어. 다시 시도해줘.", "error")
            return redirect(url_for("login"))

        # Fetch the Kakao profile.
        try:
            ur = requests.get(
                KAKAO_USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            ur.raise_for_status()
            profile = ur.json()
        except requests.RequestException:
            log.exception("kakao userinfo fetch failed")
            flash("카카오 프로필을 가져오지 못했어. 다시 시도해줘.", "error")
            return redirect(url_for("login"))

        kakao_id = profile.get("id")
        if kakao_id is None:
            flash("카카오 사용자 정보를 확인하지 못했어.", "error")
            return redirect(url_for("login"))
        kakao_id = str(kakao_id)  # stable identity
        account = profile.get("kakao_account") or {}
        kprofile = account.get("profile") or {}
        props = profile.get("properties") or {}
        nickname = (
            kprofile.get("nickname") or props.get("nickname") or "카카오 사용자"
        )[:60]
        # Optional profile photo. Newer consent exposes it under
        # kakao_account.profile.profile_image_url; older/simple scope puts it in
        # properties.profile_image. Absent is fine — never crash on it.
        profile_image_url = (
            kprofile.get("profile_image_url") or props.get("profile_image") or None
        )
        if profile_image_url:
            profile_image_url = str(profile_image_url)[:512]

        # ---- link-to-existing-account flow ------------------------------- #
        # A logged-in email user chose "connect Kakao" in settings. Attach this
        # Kakao identity to their EXISTING account instead of creating a new one.
        # Pop unconditionally so a stale flag can never leak into a later login.
        if session.pop("kakao_link_mode", False):
            me = current_user()
            if me is not None:
                if me.kakao_id:
                    flash("이미 카카오가 연결된 계정이야.", "error")
                    return redirect(url_for("settings"))
                other = User.query.filter_by(kakao_id=kakao_id).first()
                if other is not None and other.id != me.id:
                    # Never steal/merge — this Kakao already belongs elsewhere.
                    flash("이 카카오 계정은 이미 다른 계정에 연결돼 있어.", "error")
                    return redirect(url_for("settings"))
                me.kakao_id = kakao_id
                if profile_image_url:
                    me.profile_image_url = profile_image_url
                db.session.commit()
                flash(
                    "카카오 계정이 연결됐어! 이제 카카오로도 로그인할 수 있어.", "ok"
                )
                return redirect(url_for("settings"))
            # Link mode but somehow not logged in → fall through to normal login.

        # Existing Kakao user → just log in. Keep their photo current.
        user = User.query.filter_by(kakao_id=kakao_id).first()
        if user:
            if profile_image_url and user.profile_image_url != profile_image_url:
                user.profile_image_url = profile_image_url
                db.session.commit()
            session.clear()
            session["user_id"] = user.id
            session.permanent = True
            return redirect(url_for("index"))

        # New Kakao user → needs to choose/join a couple space next.
        session["pending_kakao_id"] = kakao_id
        session["pending_kakao_nickname"] = nickname
        session["pending_kakao_profile_image"] = profile_image_url
        return redirect(url_for("kakao_connect"))

    @app.route("/auth/kakao/connect", methods=["GET", "POST"])
    def kakao_connect():
        if current_user():
            return redirect(url_for("index"))
        kakao_id = session.get("pending_kakao_id")
        nickname = session.get("pending_kakao_nickname")
        profile_image_url = session.get("pending_kakao_profile_image")
        if not kakao_id:
            # No pending Kakao identity — start over.
            return redirect(url_for("login"))

        if request.method == "POST":
            # Guard against a duplicate that appeared between callback and submit.
            if User.query.filter_by(kakao_id=kakao_id).first():
                session.pop("pending_kakao_id", None)
                session.pop("pending_kakao_nickname", None)
                session.pop("pending_kakao_profile_image", None)
                flash("이미 연결된 카카오 계정이야. 다시 로그인해줘.", "error")
                return redirect(url_for("login"))

            display_name = (request.form.get("display_name") or nickname or "").strip()
            display_name = display_name[:60]
            invite_code = (request.form.get("invite_code") or "").strip().upper()
            if not display_name:
                flash("이름을 입력해줘.", "error")
                return render_template(
                    "kakao_connect.html", nickname=nickname, invite_code=invite_code
                )

            couple, is_admin, status, err = _resolve_couple_for_join(invite_code)
            if err:
                flash(err, "error")
                return render_template(
                    "kakao_connect.html", nickname=nickname, invite_code=invite_code
                )
            user = User(
                kakao_id=kakao_id,
                profile_image_url=profile_image_url,
                display_name=display_name,
                couple_id=couple.id,
                is_admin=is_admin,
                status=status,
            )
            db.session.add(user)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("연결 중 문제가 생겼어. 다시 시도해줘.", "error")
                return redirect(url_for("login"))
            session.pop("pending_kakao_profile_image", None)

            session.clear()
            session["user_id"] = user.id
            session.permanent = True  # 새 커플은 로그인 상태 유지
            return redirect(url_for("index"))

        return render_template(
            "kakao_connect.html", nickname=nickname, invite_code=""
        )

    @app.route("/auth/kakao/unlink", methods=["POST"])
    @login_required
    def kakao_unlink():
        """Detach Kakao from the current account. Refuse if it would lock the
        user out (a Kakao-only account has no email/password to fall back on)."""
        u = current_user()
        if not u.kakao_id:
            return redirect(url_for("settings"))
        if not u.email:
            flash("카카오만 연결된 계정은 연결을 해제할 수 없어.", "error")
            return redirect(url_for("settings"))
        u.kakao_id = None
        db.session.commit()
        flash("카카오 연결을 해제했어.", "ok")
        return redirect(url_for("settings"))

    # ---- couple approval ----
    @app.route("/approve/<int:user_id>", methods=["POST"])
    @login_required
    def approve(user_id):
        u = current_user()
        if not u.is_admin:
            abort(403)
        partner = User.query.get_or_404(user_id)
        if partner.couple_id != u.couple_id or partner.status != "pending":
            abort(400)
        partner.status = "approved"
        db.session.commit()
        # Notify the just-approved partner (never the admin/actor).
        _safe_notify(
            partner,
            "approval",
            "커플 연결이 승인됐어! 🎉",
            url_for("index"),
        )
        flash(f"{partner.display_name}님과 연결됐어! 이제 함께 시작해봐.", "ok")
        return redirect(url_for("index"))

    # ---- daily answer ----
    @app.route("/answer", methods=["POST"])
    @active_couple_required
    def answer():
        u = current_user()
        text = (request.form.get("answer") or "").strip()
        if not text:
            flash("답변을 입력해줘.", "error")
            return redirect(url_for("index"))
        q = get_or_create_today_question(u.couple)
        existing = q.answer_by(u.id)
        is_new = existing is None  # only a first answer notifies (edits stay quiet)
        if existing:
            existing.text = text  # allow editing until reveal is fine; keep simple
        else:
            db.session.add(Answer(question_id=q.id, user_id=u.id, text=text))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            is_new = False
        if is_new:
            # Notify the partner (the recipient) — never the answering user.
            _safe_notify(
                u.partner,
                "answer",
                f"{u.display_name}님이 오늘 답을 남겼어 💌",
                url_for("index"),
            )
        # Freshen this month's cached insight in the BACKGROUND (never blocks the
        # response, never runs claude here). Only meaningful once there's a
        # partner to have a two-person conversation. Guarded against duplicates.
        if u.partner is not None:
            today = date.today()
            _kick_monthly_report(u.couple_id, today.year, today.month)
        return redirect(url_for("index"))

    # ---- comments on a day's revealed answers ----
    @app.route("/question/<int:qid>/comment", methods=["POST"])
    @active_couple_required
    def add_comment(qid):
        u = current_user()
        q = db.session.get(DailyQuestion, qid)
        # Must be this couple's question, and only after both answered (reveal).
        if q is None or q.couple_id != u.couple_id:
            abort(404)
        if not q.both_answered:
            abort(403)

        text = (request.form.get("text") or "").strip()[:1000]
        parent_id_raw = request.form.get("parent_id")
        parent = None
        if parent_id_raw:
            try:
                parent = db.session.get(Comment, int(parent_id_raw))
            except (TypeError, ValueError):
                parent = None
            # A reply's parent must be an existing comment on the SAME question.
            if parent is None or parent.question_id != q.id:
                abort(400)

        if not text:
            flash("댓글을 입력해줘.", "error")
            return _redirect_after_comment(q)

        db.session.add(
            Comment(
                question_id=q.id,
                author_id=u.id,
                parent_id=parent.id if parent else None,
                text=text,
            )
        )
        db.session.commit()
        # Notify the OTHER partner (the one who didn't write this comment).
        # Today's comments live on the dashboard; a past day's live on its
        # detail page (/question/<qid>) — mirror that split in the deep link.
        if q.q_date == date.today():
            link = url_for("index") + f"#c-q{q.id}"
        else:
            link = url_for("question_detail", qid=q.id) + f"#c-q{q.id}"
        _safe_notify(
            u.partner,
            "comment",
            f"{u.display_name}님이 댓글을 남겼어",
            link,
        )
        return _redirect_after_comment(q)

    def _redirect_after_comment(q):
        # Today is answered/commented on the dashboard; a past day returns to
        # its own detail page so the reader stays on the day they're reading.
        if q.q_date == date.today():
            return redirect(url_for("index") + f"#c-q{q.id}")
        return redirect(url_for("question_detail", qid=q.id) + f"#c-q{q.id}")

    # ---- single day detail ----
    @app.route("/question/<int:qid>")
    @active_couple_required
    def question_detail(qid):
        """A single day's question + both answers + comment thread.

        Same reveal rule as the dashboard: both answers (and the comment box)
        appear only once both partners have answered. Pure DB reads — never
        touches claude. 404 for a missing question or one that isn't this
        couple's, so a day can't be read across couples."""
        u = current_user()
        q = db.session.get(DailyQuestion, qid)
        if q is None or q.couple_id != u.couple_id:
            abort(404)
        partner = u.partner
        my_ans = q.answer_by(u.id)
        partner_ans = q.answer_by(partner.id) if partner else None
        revealed = q.both_answered
        thread, comment_count = build_comment_thread(q) if revealed else ([], 0)
        return render_template(
            "question_detail.html",
            question=q,
            my_ans=my_ans,
            partner_ans=partner_ans,
            partner=partner,
            revealed=revealed,
            thread=thread,
            comment_count=comment_count,
        )

    # ---- history ----
    @app.route("/history")
    @active_couple_required
    def history():
        """A clean, clickable list of past days. Each row links to the day's
        detail page (/question/<qid>); the full answers + comment thread live
        there, not inline here. Metadata (reveal state + comment count) is kept
        cheap — a COUNT, never the built thread."""
        u = current_user()
        partner = u.partner
        qs = (
            DailyQuestion.query.filter_by(couple_id=u.couple_id)
            .order_by(DailyQuestion.q_date.desc())
            .all()
        )
        items = []
        for q in qs:
            revealed = q.both_answered
            items.append(
                {
                    "id": q.id,
                    "date": q.q_date,
                    "question": q.text,
                    "revealed": revealed,
                    "answered_mine": q.answer_by(u.id) is not None,
                    "comment_count": q.comments.count() if revealed else 0,
                }
            )
        return render_template("history.html", items=items, partner=partner)

    # ---- 추억 (memories): photos stored in OneDrive ----
    _ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif"}
    _MAX_PHOTO_BYTES = 50 * 1024 * 1024  # 50 MB (대용량 HEIC/JPEG 대응, OneDrive 단순 업로드 250MB 한참 아래)
    _MAX_UPLOAD_BATCH = 20               # 요청당 사진 수 안전 상한 (JS는 이제 1장씩 보냄)
    _EXT_CONTENT_TYPES = {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp",
        ".heic": "image/heic", ".heif": "image/heif",
    }

    def _content_type_for(name):
        ext = os.path.splitext(name or "")[1].lower()
        return _EXT_CONTENT_TYPES.get(ext, "image/jpeg")

    def _attachment_disposition(name):
        """Build a Content-Disposition: attachment header that survives non-ASCII
        (Korean) filenames. Provides an ASCII fallback plus RFC 5987 ``filename*``
        so browsers download with the user's original name."""
        from urllib.parse import quote

        safe = name or "photo.jpg"
        # ASCII fallback: strip quotes/backslashes and drop non-latin1 chars.
        ascii_name = safe.replace('"', "").replace("\\", "")
        ascii_name = ascii_name.encode("ascii", "ignore").decode("ascii") or "photo.jpg"
        return (
            f"attachment; filename=\"{ascii_name}\"; "
            f"filename*=UTF-8''{quote(safe)}"
        )

    @app.route("/memories")
    @active_couple_required
    def memories():
        """Gallery of the couple's photos (newest first) + an upload form.

        When OneDrive isn't configured we do NOT crash — we render a friendly
        "connect OneDrive" note and hide the upload form. Each thumbnail's
        ``<img src>`` points at our own ``/memories/<id>/image`` proxy route (see
        ``memory_image``), NOT at a raw OneDrive URL — personal-OneDrive download
        URLs expire almost immediately and would 401 in the browser. So this
        render does NO network I/O; it's a pure DB read.
        """
        u = current_user()
        if not onedrive.onedrive_enabled():
            return render_template(
                "memories.html", enabled=False, reconnect=False, photos=[]
            )

        rows = (
            Photo.query.filter_by(couple_id=u.couple_id)
            .order_by(Photo.created_at.desc(), Photo.id.desc())
            .all()
        )
        photos = [
            {
                "id": p.id,
                "caption": p.caption,
                "tags": p.tags_list,
                "caption_status": p.caption_status,
                "original_name": p.original_name,
                "uploaded_by": p.uploaded_by,
                "created_at": p.created_at,
            }
            for p in rows
        ]
        # If anything is still being captioned, the template gently auto-reloads
        # so finished captions appear without a manual refresh.
        any_pending = any(p["caption_status"] == "pending" for p in photos)
        any_failed = any(p["caption_status"] == "failed" for p in photos)

        # Self-heal: re-spawn a caption thread for every photo stuck on 'pending'
        # (its original thread may have died/OOM'd/never ran). The _captioning
        # guard drops duplicates, and the semaphore serializes the work, so this
        # is safe even with several stuck photos.
        app_obj = current_app._get_current_object()
        for p in photos:
            if p["caption_status"] == "pending":
                _spawn_caption_if_idle(app_obj, p["id"])

        return render_template(
            "memories.html",
            enabled=True,
            reconnect=onedrive.reconnect_needed(),
            photos=photos,
            any_pending=any_pending,
            any_failed=any_failed,
        )

    @app.route("/memories/search")
    @active_couple_required
    def memories_search():
        """자연어 추억 검색 — 캡션·태그·원본파일명에 대한 즉시 키워드 매칭.

        캡션/태그는 이미 배경 캡셔너가 만든 자연어 AI 설명이라, 그 위를 검색하면
        요청 경로에서 claude를 호출하지 않고도(앱 절대 제약: 동기 AI 금지) 자연어
        검색 경험을 준다 — 순수 DB 읽기다. 커플 범위 밖 사진은 절대 조회하지 않는다.

        매칭: 질의를 공백 토큰화(소문자)하고, 각 토큰이 caption·tags·original_name
        중 하나의 부분문자열이면 해당 토큰이 매칭된 것으로 본다. 결과는 (1) 서로 다른
        매칭 토큰 수 내림차순, (2) 동률이면 created_at 내림차순으로 정렬한다.
        """
        u = current_user()
        q = (request.args.get("q") or "").strip()
        if not onedrive.onedrive_enabled():
            return render_template(
                "memories_search.html", enabled=False, q=q, photos=[]
            )

        tokens = [t for t in q.lower().split() if t]
        photos = []
        if tokens:
            rows = (
                Photo.query.filter_by(couple_id=u.couple_id)
                .order_by(Photo.created_at.desc(), Photo.id.desc())
                .all()
            )
            scored = []
            for p in rows:
                # 검색 대상 텍스트를 소문자로 모아 토큰별 부분문자열 매칭.
                hay = [(p.caption or "").lower(), (p.original_name or "").lower()]
                hay.extend(t.lower() for t in p.tags_list)
                matched = 0
                for tok in tokens:
                    if any(tok in h for h in hay):
                        matched += 1
                if matched:
                    scored.append((matched, p))
            # 매칭 토큰 수 내림차순, 동률은 created_at 내림차순(rows가 이미 최신순이라
            # 안정 정렬로 recency 타이브레이크가 유지된다).
            scored.sort(key=lambda sp: sp[0], reverse=True)
            photos = [
                {
                    "id": p.id,
                    "caption": p.caption,
                    "tags": p.tags_list,
                    "caption_status": p.caption_status,
                    "original_name": p.original_name,
                    "uploaded_by": p.uploaded_by,
                    "created_at": p.created_at,
                }
                for _, p in scored
            ]
        return render_template(
            "memories_search.html",
            enabled=True,
            reconnect=onedrive.reconnect_needed(),
            q=q,
            photos=photos,
        )

    @app.route("/memories/<int:photo_id>/image")
    @active_couple_required
    def memory_image(photo_id):
        """Backend image proxy: stream a photo's bytes from OneDrive.

        The browser never sees a OneDrive URL. We fetch the bytes server-side
        with a FRESH link every time (``get_photo_content`` hits /content, which
        302s to a fresh pre-auth URL) so we can't serve an expired 401 body. A
        brief in-process bytes cache absorbs refetch storms; ``Cache-Control:
        private, max-age=3600`` lets the browser cache each image for an hour.
        404 unless the photo belongs to the requester's couple.
        """
        u = current_user()
        photo = db.session.get(Photo, photo_id)
        if photo is None or photo.couple_id != u.couple_id:
            abort(404)
        try:
            data, ctype = onedrive.get_photo_content_cached(photo.onedrive_item_id)
        except onedrive.OneDriveError:
            log.exception("could not fetch image bytes for photo %s", photo.id)
            abort(502)
        if data is None:
            abort(404)  # gone from OneDrive
        if not ctype or not ctype.startswith("image/"):
            ctype = _content_type_for(photo.filename or photo.original_name)
        resp = app.response_class(data, mimetype=ctype)
        resp.headers["Cache-Control"] = "private, max-age=3600"
        # ?download=1 → force a browser download of the ORIGINAL file bytes with
        # the user's original filename (the lightbox 다운로드 button uses this).
        if request.args.get("download"):
            resp.headers["Content-Disposition"] = _attachment_disposition(
                photo.original_name or photo.filename or f"photo-{photo.id}.jpg"
            )
        return resp

    @app.route("/memories/<int:photo_id>/thumb")
    @active_couple_required
    def memory_thumb(photo_id):
        """Fast gallery thumbnail: stream a SMALL OneDrive-rendered thumbnail.

        Serves ``get_thumbnail`` (a few-KB image) instead of proxying the full
        original, so the grid loads quickly on the free tier. Falls back to the
        full-res ``memory_image`` proxy when a thumbnail isn't available for the
        item yet. Cached for a day in the browser. 404 unless the photo belongs
        to the requester's couple.
        """
        u = current_user()
        photo = db.session.get(Photo, photo_id)
        if photo is None or photo.couple_id != u.couple_id:
            abort(404)
        try:
            data, ctype = onedrive.get_thumbnail(photo.onedrive_item_id, size="medium")
        except onedrive.OneDriveError:
            log.exception("could not fetch thumbnail for photo %s", photo.id)
            data, ctype = None, None
        if data is None:
            # No thumbnail (yet) → fall back to the full-res proxy.
            return redirect(url_for("memory_image", photo_id=photo.id))
        if not ctype or not ctype.startswith("image/"):
            ctype = "image/jpeg"
        resp = app.response_class(data, mimetype=ctype)
        resp.headers["Cache-Control"] = "private, max-age=86400"
        return resp

    def _wants_json():
        """AJAX 업로더 여부 판정: JS는 사진 1장씩 fetch로 보내며 아래 신호 중
        하나를 준다(헤더 X-Requested-With: fetch · ?ajax=1 · Accept: json).
        일반 페이지 전송(폴백)에는 해당하지 않으므로 기존 flash+redirect를 탄다."""
        if request.args.get("ajax") == "1":
            return True
        if request.headers.get("X-Requested-With", "").lower() == "fetch":
            return True
        accept = request.headers.get("Accept", "")
        return "application/json" in accept and "text/html" not in accept

    @app.route("/memories/upload", methods=["POST"])
    @active_couple_required
    def memories_upload():
        u = current_user()
        ajax = _wants_json()
        if not onedrive.onedrive_enabled():
            if ajax:
                return jsonify(ok=False, error="onedrive_disabled",
                               reason="OneDrive 연결이 필요해."), 400
            flash("OneDrive 연결이 필요해.", "error")
            return redirect(url_for("memories"))

        # Bulk-capable: the form posts name="photos" (multiple). Fall back to the
        # legacy single name="photo" so an old cached form still works. The AJAX
        # uploader sends exactly one file per request but reuses the same field.
        files = request.files.getlist("photos") or request.files.getlist("photo")
        files = [f for f in files if f and f.filename]
        if not files:
            if ajax:
                return jsonify(ok=False, error="empty",
                               reason="사진을 선택해줘."), 400
            flash("사진을 선택해줘.", "error")
            return redirect(url_for("memories"))
        if len(files) > _MAX_UPLOAD_BATCH:
            if ajax:
                return jsonify(ok=False, error="too_many",
                               reason=f"한 번에 최대 {_MAX_UPLOAD_BATCH}장까지 올릴 수 있어."), 400
            flash(f"한 번에 최대 {_MAX_UPLOAD_BATCH}장까지 올릴 수 있어.", "error")
            return redirect(url_for("memories"))

        # A manually typed caption only makes sense for a single photo; when a
        # batch is uploaded we ignore it and auto-caption each one instead.
        manual_caption = (request.form.get("caption") or "").strip()[:1000] or None
        if len(files) > 1:
            manual_caption = None

        saved_ids = []          # rows that need background captioning
        saved = 0
        skipped = 0             # invalid / empty / too-big files
        failed = 0              # OneDrive upload errors
        reason = None           # 마지막 스킵/실패 사유 (ajax 리포트용)
        last_name = files[-1].filename if files else None
        for file in files:
            # Validate it's an image by extension AND declared content-type.
            ext = os.path.splitext(file.filename)[1].lower()
            ctype = (file.mimetype or "").lower()
            if ext not in _ALLOWED_IMAGE_EXTS and not ctype.startswith("image/"):
                skipped += 1
                reason = "이미지 파일만 올릴 수 있어."
                continue
            # 원본 바이트를 그대로 읽어 그대로 업로드한다 — 재인코딩/리사이즈 없음.
            data = file.read()
            if not data:
                skipped += 1
                reason = "빈 파일이야."
                continue
            if len(data) > _MAX_PHOTO_BYTES:
                skipped += 1
                reason = "사진 1장은 50MB 이하만 올릴 수 있어."
                continue
            try:
                item_id, stored_name = onedrive.upload_photo(data, file.filename)
            except onedrive.OneDriveError:
                log.exception("OneDrive upload failed for couple %s", u.couple_id)
                failed += 1
                reason = "OneDrive 업로드에 실패했어."
                continue

            # A manual caption is treated as final ('ready'); otherwise the row
            # starts 'pending' and a background thread captions it via claude.
            caption = manual_caption
            photo = Photo(
                couple_id=u.couple_id,
                onedrive_item_id=item_id,
                filename=stored_name[:255],           # real name stored in OneDrive
                original_name=file.filename[:255],    # the user's original filename
                uploaded_by=u.id,
                caption=caption,
                caption_status="ready" if caption else "pending",
            )
            db.session.add(photo)
            # Commit per photo so one later failure can't lose earlier successes.
            db.session.commit()
            saved += 1
            if caption is None:
                saved_ids.append(photo.id)

        # Fire-and-forget auto-captioning for each new photo. The HTTP response
        # returns after the OneDrive PUTs + DB inserts — it NEVER waits on the
        # slow vision pass (CLAUDE.md: no synchronous AI on the request path).
        for pid in saved_ids:
            try:
                threading.Thread(
                    target=caption_photo,
                    args=(current_app._get_current_object(), pid),
                    daemon=True,
                ).start()
            except Exception:  # noqa: BLE001 — captioning is best-effort
                log.exception("failed to spawn caption thread for photo %s", pid)

        # AJAX 업로더(사진 1장씩)에는 JSON으로 결과만 돌려준다 — redirect 없음.
        # 클라이언트가 진행률/실패 파일명을 직접 표시하고, 전부 끝난 뒤 페이지를
        # 새로고침해 새 그리드+flash를 보여준다.
        if ajax:
            return jsonify(
                ok=(saved > 0),
                saved=saved,
                skipped=skipped,
                failed=failed,
                filename=last_name,
                reason=reason,
            )

        # One honest flash summarizing the batch outcome.
        if saved and not (skipped or failed):
            flash(
                f"추억 {saved}장을 저장했어! 💖" if saved > 1 else "추억을 저장했어! 💖",
                "ok",
            )
        elif saved:
            flash(f"{saved}장 저장했어. {skipped + failed}장은 올리지 못했어.", "ok")
        elif failed and onedrive.reconnect_needed():
            flash("OneDrive 재연결이 필요해. 잠시 후 다시 시도해줘.", "error")
        elif failed:
            flash("사진 업로드에 실패했어. 잠시 후 다시 시도해줘.", "error")
        else:
            flash("이미지 파일만 50MB 이하로 올릴 수 있어.", "error")
        return redirect(url_for("memories"))

    @app.route("/memories/recaption", methods=["POST"])
    @active_couple_required
    def memories_recaption():
        """Retry captioning for THIS couple's photos that failed: reset their
        'failed' status back to 'pending', then (guarded) spawn a background
        caption thread for every 'pending' photo. Safe now that captioning is
        serialized — the photos queue up and caption one at a time."""
        u = current_user()
        # 이 커플의 실패한 사진만 다시 'pending'으로 되돌린다(커플 범위 밖은 절대 건드리지 않음).
        failed = Photo.query.filter_by(
            couple_id=u.couple_id, caption_status="failed"
        ).all()
        for p in failed:
            p.caption_status = "pending"
        db.session.commit()

        # 이 커플의 모든 'pending' 사진에 대해 배경 캡셔너를 (중복 방지 가드로) 띄운다.
        pending = Photo.query.filter_by(
            couple_id=u.couple_id, caption_status="pending"
        ).all()
        app_obj = current_app._get_current_object()
        for p in pending:
            _spawn_caption_if_idle(app_obj, p.id)

        flash("안 된 사진들을 다시 분석할게. 잠시 뒤 새로고침해줘.", "ok")
        return redirect(url_for("memories"))

    @app.route("/memories/<int:photo_id>/delete", methods=["POST"])
    @active_couple_required
    def memories_delete(photo_id):
        u = current_user()
        photo = db.session.get(Photo, photo_id)
        # Only this couple's members may delete this couple's photo.
        if photo is None or photo.couple_id != u.couple_id:
            abort(404)
        try:
            onedrive.delete_photo(photo.onedrive_item_id)
        except onedrive.OneDriveError:
            # Remote delete failed — keep the row so we can retry; don't orphan.
            log.exception("OneDrive delete failed for photo %s", photo.id)
            flash("사진 삭제에 실패했어. 잠시 후 다시 시도해줘.", "error")
            return redirect(url_for("memories"))
        db.session.delete(photo)
        db.session.commit()
        flash("사진을 삭제했어.", "ok")
        return redirect(url_for("memories"))

    # ---- 판사 (AI judge for couple fights): 사건 + 진술 + 백그라운드 판결 ----
    def _get_case_or_404(u, case_id):
        """Load a case, 404 unless it belongs to the requester's couple, so a
        case can never be read/acted on across couples."""
        case = db.session.get(Case, case_id)
        if case is None or case.couple_id != u.couple_id:
            abort(404)
        return case

    @app.route("/cases")
    @active_couple_required
    def cases():
        """List the couple's 사건 (newest first). Pure DB read — never claude."""
        u = current_user()
        rows = (
            Case.query.filter_by(couple_id=u.couple_id)
            .order_by(Case.created_at.desc(), Case.id.desc())
            .all()
        )
        items = []
        for c in rows:
            v = c.verdict
            items.append(
                {
                    "id": c.id,
                    "title": c.title,
                    "situation": c.situation,
                    "status": c.status,
                    "created_at": c.created_at,
                    "summary": (v.get("summary") if v else "") or "",
                    "fault": (v.get("fault") if v else None),
                }
            )
        return render_template("cases.html", items=items)

    @app.route("/cases/new", methods=["GET", "POST"])
    @active_couple_required
    def case_new():
        u = current_user()
        if request.method == "POST":
            situation = (request.form.get("situation") or "").strip()
            title = (request.form.get("title") or "").strip()[:120] or None
            statement = (request.form.get("statement") or "").strip()
            if not situation:
                flash("무슨 일이 있었는지 상황을 먼저 적어줘.", "error")
                return render_template("case_new.html")
            case = Case(
                couple_id=u.couple_id,
                created_by=u.id,
                title=title,
                situation=situation,
                status="open",
            )
            db.session.add(case)
            db.session.flush()  # get case.id for the optional statement
            if statement:
                db.session.add(
                    CaseStatement(case_id=case.id, user_id=u.id, text=statement)
                )
            db.session.commit()
            return redirect(url_for("case_detail", case_id=case.id))
        return render_template("case_new.html")

    @app.route("/cases/<int:case_id>")
    @active_couple_required
    def case_detail(case_id):
        """A single 사건: situation + both partners' statements + the verdict.

        Pure DB read — the slow claude judge NEVER runs here; it runs only in the
        background ``judge_case`` worker. 404 for a missing case or one that isn't
        this couple's, so a case can't be read across couples."""
        u = current_user()
        case = _get_case_or_404(u, case_id)
        partner = u.partner
        my_st = CaseStatement.query.filter_by(
            case_id=case.id, user_id=u.id
        ).first()
        partner_st = (
            CaseStatement.query.filter_by(case_id=case.id, user_id=partner.id).first()
            if partner is not None
            else None
        )
        # Render exactly the two couple members' sides (me first). A missing side
        # shows a gentle "아직 진술을 남기지 않았어" placeholder in the template.
        sides = [{"user": u, "statement": my_st, "is_me": True}]
        if partner is not None:
            sides.append({"user": partner, "statement": partner_st, "is_me": False})
        return render_template(
            "case_detail.html",
            case=case,
            verdict=case.verdict,
            sides=sides,
            my_statement=my_st,
            has_statement=len(case.statements) >= 1,
            resolved=(case.status == "resolved"),
        )

    @app.route("/cases/<int:case_id>/statement", methods=["POST"])
    @active_couple_required
    def case_statement(case_id):
        """Upsert MY 진술 for this case (each partner has at most one).

        If a verdict already exists we do NOT auto-clear it — the user re-judges
        manually with the '다시 판결 맡기기' button."""
        u = current_user()
        case = _get_case_or_404(u, case_id)
        if case.status == "resolved":
            flash("이미 종결된 사건이라 진술을 바꿀 수 없어.", "error")
            return redirect(url_for("case_detail", case_id=case.id))
        text = (request.form.get("text") or "").strip()
        if not text:
            flash("진술 내용을 입력해줘.", "error")
            return redirect(url_for("case_detail", case_id=case.id))
        st = CaseStatement.query.filter_by(case_id=case.id, user_id=u.id).first()
        if st:
            st.text = text
            st.updated_at = datetime.utcnow()
        else:
            db.session.add(
                CaseStatement(case_id=case.id, user_id=u.id, text=text)
            )
        try:
            db.session.commit()
        except IntegrityError:
            # A concurrent submit created my statement first — take theirs, update.
            db.session.rollback()
            st = CaseStatement.query.filter_by(
                case_id=case.id, user_id=u.id
            ).first()
            if st:
                st.text = text
                st.updated_at = datetime.utcnow()
                db.session.commit()
        return redirect(url_for("case_detail", case_id=case.id))

    @app.route("/cases/<int:case_id>/judge", methods=["POST"])
    @active_couple_required
    def case_judge(case_id):
        """Kick off the BACKGROUND AI judgment for this case.

        Requires ≥1 statement. Marks the case 'judging', commits so the detail
        page shows the '판결 중' state (+ auto-refresh), then spawns the daemon
        thread. The slow claude pass runs ONLY in ``judge_case`` — never here."""
        u = current_user()
        case = _get_case_or_404(u, case_id)
        if case.status == "resolved":
            flash("이미 화해로 종결된 사건이야. 다시 열면 판결을 새로 맡길 수 있어.", "ok")
            return redirect(url_for("case_detail", case_id=case.id))
        if len(case.statements) < 1:
            flash("먼저 진술을 남겨줘.", "error")
            return redirect(url_for("case_detail", case_id=case.id))

        case.status = "judging"
        case.updated_at = datetime.utcnow()
        db.session.commit()

        # In-process guard so a rapid double-tap can't spawn two judge threads
        # for the same case (single-worker Render free tier). The thread frees
        # the key in its finally.
        spawn = False
        with _judging_lock:
            if case.id not in _judging:
                _judging.add(case.id)
                spawn = True
        if spawn:
            try:
                threading.Thread(
                    target=judge_case,
                    args=(current_app._get_current_object(), case.id),
                    daemon=True,
                ).start()
            except Exception:  # noqa: BLE001 — never break the redirect
                log.exception("failed to spawn judge thread for case %s", case.id)
                with _judging_lock:
                    _judging.discard(case.id)
        return redirect(url_for("case_detail", case_id=case.id))

    @app.route("/cases/<int:case_id>/delete", methods=["POST"])
    @active_couple_required
    def case_delete(case_id):
        """Delete a case (cascade removes its statements). Any couple member may
        delete their couple's case."""
        u = current_user()
        case = _get_case_or_404(u, case_id)
        db.session.delete(case)
        db.session.commit()
        flash("사건을 삭제했어.", "ok")
        return redirect(url_for("cases"))

    @app.route("/cases/<int:case_id>/resolve", methods=["POST"])
    @active_couple_required
    def case_resolve(case_id):
        """화해 완료 — 판결이 난('decided') 사건을 'resolved'로 종결한다.

        새 DB 컬럼/마이그레이션 없이 기존 ``status`` 문자열만 바꾼다. 종결되면
        진술 수정·재판결이 잠긴다(case_statement/case_judge 가드)."""
        u = current_user()
        case = _get_case_or_404(u, case_id)
        if case.status != "decided":
            flash("판결이 난 사건만 화해 완료로 종결할 수 있어.", "error")
            return redirect(url_for("case_detail", case_id=case.id))
        case.status = "resolved"
        case.updated_at = datetime.utcnow()
        db.session.commit()
        flash("화해 완료! 🎉 이 사건은 종결됐어.", "ok")
        return redirect(url_for("case_detail", case_id=case.id))

    @app.route("/cases/<int:case_id>/reopen", methods=["POST"])
    @active_couple_required
    def case_reopen(case_id):
        """실수로 종결한 경우를 위한 안전장치 — 'resolved'를 'decided'로 되돌린다."""
        u = current_user()
        case = _get_case_or_404(u, case_id)
        if case.status == "resolved":
            case.status = "decided"
            case.updated_at = datetime.utcnow()
            db.session.commit()
        return redirect(url_for("case_detail", case_id=case.id))

    # ---- monthly insight ----
    @app.route("/insight")
    @active_couple_required
    def insight():
        u = current_user()
        partner = u.partner
        today = date.today()
        try:
            year = int(request.args.get("year", today.year))
            month = int(request.args.get("month", today.month))
            if not (1 <= month <= 12):
                raise ValueError
        except (TypeError, ValueError):
            year, month = today.year, today.month

        # Solo space: the couple has only one approved member (partner hasn't
        # joined/been approved yet). Never call the stats/qualitative functions
        # with a None partner — show a gentle empty state instead.
        if partner is None:
            return render_template(
                "insight.html",
                no_partner=True,
                stats=None,
                qualitative=None,
                has_data=False,
                year=year,
                month=month,
            )

        # Quantitative stats stay LIVE — a cheap DB query, never cached.
        stats = insights.compute_monthly_stats(u.couple_id, year, month, u, partner)
        # "has data" == at least one answer exists this month (derived from the
        # stats we already computed, so we avoid a second DB pass on the request).
        has_data = bool(
            stats["participation_a"]["days"] or stats["participation_b"]["days"]
        )

        # Qualitative comes ONLY from the cached MonthlyReport — the slow
        # `claude -p` call never runs on this request path.
        #
        # TRUE stale-while-revalidate for the DISPLAY: whether we show content
        # depends on whether the row HAS content (generated_at set), NOT on the
        # status. So while a background refresh runs (status=='generating') the
        # PREVIOUS cached content keeps showing — we never drop a populated
        # report back to the placeholder. The regen path only swaps the content
        # once the new report is ready (it never clears content when it marks
        # the row 'generating').
        report = MonthlyReport.query.filter_by(
            couple_id=u.couple_id, year=year, month=month
        ).first()
        has_content = report is not None and report.generated_at is not None
        qualitative = None
        if has_content:
            qualitative = {
                "summary": report.summary or "",
                "themes": report.themes_list,
                "tone": report.tone or "",
                "divergent_question": report.divergent_question or "",
                "fun": report.fun or "",
            }

        # A background refresh is in flight for this month.
        refreshing = report is not None and report.status == "generating"
        # Placeholder ONLY when there's data but no content has ever been built
        # for this month (first-ever generation). If content exists, we show it.
        pending = has_data and not has_content

        # Trigger a background build when there's data but no cached content yet
        # (missing / failed-with-no-content). When content already exists we do
        # NOT kick from here — answer events keep it fresh — and the in-flight
        # guard in _kick_monthly_report would no-op anyway.
        if has_data and not has_content:
            if report is None or report.status in ("generating", "failed"):
                _kick_monthly_report(u.couple_id, year, month)

        # Auto-reload while anything is being (re)built so the display swaps to
        # the fresh content when the background thread finishes: either building
        # the first report (pending) or refreshing existing content (refreshing).
        auto_refresh = pending or (has_content and refreshing)

        # previous / next month links
        prev_m = (month - 1) or 12
        prev_y = year - 1 if month == 1 else year
        next_m = 1 if month == 12 else month + 1
        next_y = year + 1 if month == 12 else year
        return render_template(
            "insight.html",
            stats=stats,
            qualitative=qualitative,
            has_data=has_data,
            pending=pending,
            refreshing=refreshing,
            auto_refresh=auto_refresh,
            year=year,
            month=month,
            prev=(prev_y, prev_m),
            next=(next_y, next_m),
        )

    # ---- settings ----
    @app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings():
        u = current_user()
        if request.method == "POST":
            new_name = (request.form.get("app_name") or "").strip()
            new_display = (request.form.get("display_name") or "").strip()
            if new_name:
                Setting.set("app_name", new_name[:60])
            if new_display:
                u.display_name = new_display[:60]
            db.session.commit()
            flash("설정을 저장했어.", "ok")
            return redirect(url_for("settings"))
        return render_template(
            "settings.html", current_app_name=Setting.get("app_name", DEFAULT_APP_NAME)
        )

    # ---- in-app notifications ----
    @app.route("/notifications")
    @login_required
    def notifications():
        u = current_user()
        items = (
            Notification.query.filter_by(user_id=u.id)
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .all()
        )
        # Viewing the page marks everything read so the bell dot clears. Keep
        # the just-read ids so this render can still highlight them subtly.
        fresh_ids = {n.id for n in items if not n.is_read}
        if fresh_ids:
            for n in items:
                if n.id in fresh_ids:
                    n.is_read = True
            db.session.commit()
        return render_template("notifications.html", items=items, fresh_ids=fresh_ids)

    # ---- 데이트 뉴스 (서울 문화행사 피드) ----
    @app.route("/dates")
    @active_couple_required
    def dates():
        """진행 중인(만료되지 않은) 행사 목록 — 이 커플 AI 추천 점수순(높은 순).

        end_date가 없거나 오늘 이후인 행만 보인다. 각 행사에 이 커플의 EventScore를
        붙여 {event, score, reason, score_status}로 넘기고, ready 먼저(점수 높은 순)·
        그다음 pending·마지막 failed/없음 순으로 정렬한다. 'ready' 점수가 없는 진행
        중 행사가 있으면 백그라운드 채점 스레드를 스폰한다(요청은 절대 블록 안 함)."""
        u = current_user()
        today = date.today()
        # 진행 중 행사 + 이 커플 EventScore를 LEFT JOIN으로 한 번에.
        rows = (
            db.session.query(EventItem, EventScore)
            .outerjoin(
                EventScore,
                db.and_(
                    EventScore.event_id == EventItem.id,
                    EventScore.couple_id == u.couple_id,
                ),
            )
            .filter(
                db.or_(EventItem.end_date.is_(None), EventItem.end_date >= today)
            )
            .all()
        )

        items = []
        any_pending = False
        for ev, sc in rows:
            status = sc.status if sc is not None else None
            if status == "pending":
                any_pending = True
            items.append(
                {
                    "event": ev,
                    "score": sc.score if sc is not None else None,
                    "reason": sc.reason if sc is not None else None,
                    "score_status": status,
                }
            )

        # 정렬: ready 먼저(점수 높은 순, 동점이면 마감 임박 순) → pending → 나머지.
        # 마감 없는(end_date NULL) 건 날짜 정렬에서 맨 뒤로 가도록 큰 값을 준다.
        _far = date.max

        def _rank(it):
            st = it["score_status"]
            ev = it["event"]
            end = ev.end_date or _far
            if st == "ready":
                # 점수 내림차순 → -score, 동점은 마감 임박 순(end asc), 시작 순.
                return (0, -(it["score"] or 0), end, ev.start_date or _far)
            if st == "pending":
                return (1, 0, end, ev.start_date or _far)
            return (2, 0, end, ev.start_date or _far)

        items.sort(key=_rank)

        # 셀프힐: 'ready' 점수가 없는 진행 중 행사가 있으면 백그라운드 채점을 스폰.
        # (뷰를 반복 조회해도 _scoring 가드로 스레드가 쌓이지 않는다. 요청 블록 X.)
        needs_scoring = any(it["score_status"] != "ready" for it in items)
        if items and needs_scoring:
            _spawn_scoring_if_idle(current_app._get_current_object(), u.couple_id)

        # 채점이 진행/대기 중이면(pending 행이 있거나, 방금 스폰돼 곧 채워질 예정)
        # 완료 점수가 수동 새로고침 없이 뜨도록 템플릿이 부드럽게 자동 새로고침한다.
        scoring_active = bool(items and (any_pending or needs_scoring))

        return render_template(
            "dates.html", items=items, scoring_active=scoring_active
        )

    @app.route("/dates/<int:item_id>")
    @active_couple_required
    def date_detail(item_id):
        u = current_user()
        item = db.session.get(EventItem, item_id)
        if item is None:
            abort(404)
        sc = EventScore.query.filter_by(
            couple_id=u.couple_id, event_id=item.id
        ).first()
        score = sc.score if sc is not None else None
        reason = sc.reason if sc is not None else None
        score_status = sc.status if sc is not None else None
        return render_template(
            "date_detail.html",
            item=item,
            score=score,
            reason=reason,
            score_status=score_status,
        )

    # ---- Web Push subscription management ----
    @app.route("/push/public-key")
    def push_public_key():
        """The VAPID public key the browser needs as applicationServerKey.
        Empty string when push is not configured (front-end hides the toggle)."""
        return app.response_class(
            VAPID_PUBLIC_KEY or "", mimetype="text/plain"
        )

    @app.route("/push/subscribe", methods=["POST"])
    @login_required
    def push_subscribe():
        u = current_user()
        data = request.get_json(silent=True) or {}
        endpoint = data.get("endpoint")
        keys = data.get("keys") or {}
        p256dh = keys.get("p256dh")
        auth = keys.get("auth")
        if not endpoint or not p256dh or not auth:
            return jsonify({"error": "invalid subscription"}), 400
        # Upsert by endpoint: the same browser re-subscribing (or a device that
        # switched accounts) reuses the row instead of duplicating.
        sub = PushSubscription.query.filter_by(endpoint=endpoint).first()
        if sub:
            sub.user_id = u.id
            sub.p256dh = p256dh
            sub.auth = auth
        else:
            db.session.add(
                PushSubscription(
                    user_id=u.id, endpoint=endpoint, p256dh=p256dh, auth=auth
                )
            )
        db.session.commit()
        return jsonify({"ok": True})

    @app.route("/push/unsubscribe", methods=["POST"])
    @login_required
    def push_unsubscribe():
        u = current_user()
        data = request.get_json(silent=True) or {}
        endpoint = data.get("endpoint")
        if endpoint:
            PushSubscription.query.filter_by(
                endpoint=endpoint, user_id=u.id
            ).delete()
        else:
            # No endpoint given → drop all of this user's subscriptions.
            PushSubscription.query.filter_by(user_id=u.id).delete()
        db.session.commit()
        return jsonify({"ok": True})

    # ---- daily reminder cron (called by GitHub Actions on a schedule) ----
    @app.route("/internal/cron/daily-reminder", methods=["POST"])
    def cron_daily_reminder():
        """Nudge every member who still hasn't answered today's question.

        Protected by CRON_SECRET (header ``X-Cron-Secret`` or ``?token=``),
        compared with a constant-time check. Creates an in-app Notification and
        a Web Push for each un-answered member. Safe when a couple has no
        question today (only couples WITH today's question are considered)."""
        provided = request.headers.get("X-Cron-Secret") or request.args.get("token") or ""
        if not CRON_SECRET or not hmac.compare_digest(provided, CRON_SECRET):
            abort(403)

        today = date.today()
        questions = DailyQuestion.query.filter_by(q_date=today).all()
        couples_with_question = 0
        reminders_sent = 0
        for q in questions:
            couples_with_question += 1
            couple = q.couple
            if couple is None:
                continue
            for member in couple.approved_members:
                if q.answer_by(member.id) is not None:
                    continue  # already answered — no nudge
                try:
                    notify(
                        member,
                        "reminder",
                        "오늘의 질문에 아직 답 안 했어! 답해줘 💌",
                        url_for("index"),
                    )
                    db.session.commit()
                except Exception:  # noqa: BLE001
                    db.session.rollback()
                    log.exception("reminder notify failed for user %s", member.id)
                    continue
                send_push(
                    member,
                    "오늘의 질문 💌",
                    "오늘의 질문에 아직 답 안 했어! 답해줘",
                    "/",
                )
                reminders_sent += 1
        return jsonify(
            {
                "couples_with_question": couples_with_question,
                "reminders_sent": reminders_sent,
            }
        )

    @app.route("/internal/cron/refresh-events", methods=["POST"])
    def cron_refresh_events():
        """'데이트 뉴스' 피드를 밤마다 갱신 — 서울 문화행사 upsert + 만료 삭제.

        cron_daily_reminder와 동일하게 CRON_SECRET(헤더 ``X-Cron-Secret`` 또는
        ``?token=``)으로 상수시간 비교 보호. 키가 없으면 500이 아니라 no_key로
        우아하게 빠진다. 동시 중복 실행은 모듈 락으로 막는다(비차단)."""
        provided = request.headers.get("X-Cron-Secret") or request.args.get("token") or ""
        if not CRON_SECRET or not hmac.compare_digest(provided, CRON_SECRET):
            abort(403)

        # 인증키가 없으면 피드는 빈 상태 — 크래시 대신 no_key로 알린다.
        if not events.events_enabled():
            return jsonify({"ok": False, "reason": "no_key"})

        # 동시 실행 방지(락 못 잡으면 이미 도는 중 — 조용히 skip).
        if not _events_refresh_lock.acquire(blocking=False):
            return jsonify({"ok": False, "reason": "busy"})
        try:
            items = events.fetch_seoul_events()
            fetched = len(items)
            upserted = 0
            for it in items:
                try:
                    row = EventItem.query.filter_by(
                        source=it["source"], source_uid=it["source_uid"]
                    ).first()
                    if row is None:
                        row = EventItem(source=it["source"], source_uid=it["source_uid"])
                        db.session.add(row)
                    # 존재하면 필드 갱신, 없으면 새 행에 채움.
                    row.title = it["title"]
                    row.category = it.get("category")
                    row.description = it.get("description")
                    row.place = it.get("place")
                    row.district = it.get("district")
                    row.image_url = it.get("image_url")
                    row.link = it.get("link")
                    row.fee = it.get("fee")
                    row.is_free = it.get("is_free")
                    row.start_date = it.get("start_date")
                    row.end_date = it.get("end_date")
                    db.session.commit()
                    upserted += 1
                except Exception:  # noqa: BLE001 — 한 행 실패가 전체를 막지 않게
                    db.session.rollback()
                    log.exception("event upsert failed (uid=%s)", it.get("source_uid"))

            # 만료 행 삭제(end_date가 있고 오늘보다 과거).
            expired_deleted = 0
            try:
                today = date.today()
                expired_deleted = (
                    EventItem.query.filter(
                        EventItem.end_date.isnot(None),
                        EventItem.end_date < today,
                    ).delete(synchronize_session=False)
                )
                db.session.commit()
            except Exception:  # noqa: BLE001
                db.session.rollback()
                log.exception("event expiry delete failed")

            return jsonify(
                {
                    "ok": True,
                    "fetched": fetched,
                    "upserted": upserted,
                    "expired_deleted": expired_deleted,
                }
            )
        finally:
            _events_refresh_lock.release()

    # ---- PWA plumbing ----
    @app.route("/manifest.json")
    def manifest():
        name = Setting.get("app_name", DEFAULT_APP_NAME)
        return jsonify(
            {
                "name": name,
                "short_name": name,
                "start_url": "/",
                "scope": "/",
                "display": "standalone",
                "background_color": "#fff1f6",
                "theme_color": "#ff6b9d",
                "lang": "ko",
                "icons": [
                    {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png"},
                    {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png"},
                    {
                        "src": "/static/icons/maskable-512.png",
                        "sizes": "512x512",
                        "type": "image/png",
                        "purpose": "maskable",
                    },
                ],
            }
        )

    @app.route("/sw.js")
    def service_worker():
        # Served from root so the SW scope covers the whole app.
        resp = send_from_directory(app.static_folder, "sw.js")
        resp.headers["Content-Type"] = "application/javascript"
        resp.headers["Service-Worker-Allowed"] = "/"
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.route("/offline")
    def offline():
        return render_template("offline.html")

    @app.route("/healthz")
    def healthz():
        return {"status": "ok"}

    # ---- themed error handlers ----
    @app.errorhandler(404)
    @app.errorhandler(405)
    def _not_found(e):
        # Missing page or wrong method → same gentle "not ready yet" state.
        return (
            render_template(
                "error.html",
                heading="여기엔 아무것도 없어",
                message="아직 준비되지 않은 기능이에요",
            ),
            404,
        )

    @app.errorhandler(413)
    def _too_large(e):
        # 요청이 MAX_CONTENT_LENGTH를 넘었을 때. AJAX 업로더면 그 파일만 "too_large"로
        # 알려 나머지는 계속 올리게 하고, 일반 페이지 로드면 친절한 에러 화면을 보여준다.
        if _wants_json():
            return jsonify(ok=False, error="too_large",
                           reason="사진 1장은 50MB 이하만 올릴 수 있어."), 413
        return (
            render_template(
                "error.html",
                heading="사진이 너무 커",
                message="사진 1장은 50MB 이하만 올릴 수 있어. 조금 작은 파일로 다시 시도해줘.",
            ),
            413,
        )

    @app.errorhandler(500)
    @app.errorhandler(Exception)
    def _server_error(e):
        # Let HTTP errors (404/405/etc.) keep their own handling/status.
        from werkzeug.exceptions import HTTPException

        if isinstance(e, HTTPException):
            return e
        # Surface the real root cause in the logs (Render), never to the user.
        app.logger.exception("unhandled exception rendering %s", request.path)
        # 오염된 트랜잭션(죽은 커넥션 등)을 먼저 비워, 에러 페이지 렌더가
        # 같은 죽은 커넥션에 다시 걸려 무너지지 않게 한다.
        try:
            db.session.rollback()
        except Exception:
            log.debug("error handler: rollback 실패", exc_info=True)
        try:
            # context_processor가 이제 DB 장애 내성이 있어 정상 렌더돼야 하지만,
            # 그래도 실패하면 아래 인라인 폴백으로 넘어간다.
            return (
                render_template(
                    "error.html",
                    heading="이런, 문제가 생겼어",
                    message="에러가 발생했습니다",
                ),
                500,
            )
        except Exception:
            # 최후의 안전망: 템플릿·DB 없이도 테마 화면을 반드시 보여준다.
            # (원시 흰색 Werkzeug 500 페이지는 절대 노출하지 않는다.)
            log.exception("error handler: error.html 렌더 실패 — 인라인 폴백 사용")
            html = (
                "<!doctype html>"
                "<html lang=\"ko\"><head><meta charset=\"UTF-8\"/>"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\"/>"
                "<title>문제가 생겼어</title><style>"
                "html,body{margin:0;height:100%}"
                "body{background:#fff5f8;color:#5a4a52;"
                "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
                "display:flex;align-items:center;justify-content:center;text-align:center;padding:24px}"
                ".box{max-width:22rem}"
                "h1{font-size:1.4rem;margin:0 0 .6rem;color:#ff6b9d}"
                "p{font-size:1rem;line-height:1.6;margin:0;color:#8a7a82}"
                "</style></head><body><div class=\"box\">"
                "<h1>이런, 문제가 생겼어</h1>"
                "<p>잠시 후 다시 시도해줘</p>"
                "</div></body></html>"
            )
            return html, 500, {"Content-Type": "text/html; charset=utf-8"}


app = create_app()

if __name__ == "__main__":
    # Local dev only. Production uses gunicorn (see Dockerfile / fly.toml).
    app.run(host="127.0.0.1", port=5000, debug=True)
