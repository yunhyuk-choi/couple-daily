"""couple-daily — a tiny daily-question app for exactly two people.

Identity constraint (do not violate): the AI mechanism is the `claude` CLI run
as a backend subprocess (see ai.py), NOT the Anthropic API/SDK. This file wires
Flask + SQLAlchemy, auth, couple linking, the daily question, monthly insight,
settings and PWA plumbing on top of that.

Run locally:   python app.py            (SQLite fallback, debug reloader)
Run in prod:   gunicorn 'app:create_app()'   (Postgres via DATABASE_URL)
"""
import calendar
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
    Bet,
    BetCheckin,
    Case,
    CaseStatement,
    Comment,
    Couple,
    DailyQuestion,
    DateRecommendation,
    EventItem,
    EventPick,
    EventScore,
    MonthlyReport,
    Notification,
    Photo,
    PushSubscription,
    Schedule,
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

# 팝업 수집(claude 웹검색)이 동시에 두 번 돌지 않게 하는 최소 가드(비차단).
_popups_refresh_lock = threading.Lock()


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


# --------------------------------------------------------------------------- #
# 내기(Bet) — 롤링 주 헬퍼 (순수/테스트 가능; HTTP·요청 컨텍스트 불필요)
# --------------------------------------------------------------------------- #
def bet_week_window(bet, on=None):
    """습관 내기의 '현재 롤링 주' 창을 계산한다.

    주(week)는 달력 주(월~일)가 아니라 ``bet.start_date``를 기점으로 7일씩 굴러가는
    윈도우다. ``week_index = (on - start_date).days // 7`` 이고
    ``week_start = start_date + 7*week_index``, ``week_end = week_start + 6일``.

    반환: ``(week_start, week_end, week_index)``.
    ``on < start_date`` (아직 시작 전)이면 첫 주(week 0) 창을 돌려주되 ``week_index``
    를 음수로 반환해 '시작 안 함'을 신호한다(호출부가 부드럽게 처리)."""
    if on is None:
        on = date.today()
    start = bet.start_date
    delta_days = (on - start).days
    if delta_days < 0:
        # 시작 전 — 첫 주 창을 주되 음수 인덱스로 not-started 신호
        return start, start + timedelta(days=6), delta_days // 7
    k = delta_days // 7
    week_start = start + timedelta(days=7 * k)
    week_end = week_start + timedelta(days=6)
    return week_start, week_end, k


def bet_participants(bet, couple):
    """이 내기의 참여자 목록(안정적 id 순).

    ``target_user_id``가 NULL이면 승인된 커플 구성원 두 명 모두(같이), 아니면 그
    한 명만(아직 커플에 남아 있을 때). 커플에서 빠진 대상이면 빈 목록."""
    members = sorted(couple.approved_members, key=lambda u: u.id)
    if bet.target_user_id is None:
        return members
    return [u for u in members if u.id == bet.target_user_id]


def bet_progress(bet, couple, on=None):
    """현재 롤링 주 기준 참여자별 진행 상황.

    반환 dict: ``week_start``·``week_end``·``week_index``·``started`` +
    ``participants``: 각 ``{user_id, name, count, target, done_today, met}``.
    ``count`` = 그 사용자의 [week_start, week_end] 내 BetCheckin 수,
    ``met`` = count ≥ count_target, ``done_today`` = ``on`` 날짜 체크인 존재."""
    if on is None:
        on = date.today()
    week_start, week_end, week_index = bet_week_window(bet, on)
    started = week_index >= 0
    target = bet.count_target or 0
    # 이 내기의 이번 주 체크인만 한 번에 조회(bet 자체가 커플 스코프라 커플 안전).
    rows = (
        BetCheckin.query.filter(
            BetCheckin.bet_id == bet.id,
            BetCheckin.date >= week_start,
            BetCheckin.date <= week_end,
        ).all()
    )
    by_user = {}
    for r in rows:
        by_user.setdefault(r.user_id, set()).add(r.date)
    participants = []
    for u in bet_participants(bet, couple):
        dates = by_user.get(u.id, set())
        count = len(dates)
        participants.append(
            {
                "user_id": u.id,
                "name": u.display_name,
                "count": count,
                "target": target,
                "done_today": on in dates,
                "met": started and target > 0 and count >= target,
            }
        )
    return {
        "week_start": week_start,
        "week_end": week_end,
        "week_index": week_index,
        "started": started,
        "participants": participants,
    }


def bet_week_days(week_start):
    """롤링 주 창의 7개 날짜(week_start..week_start+6)."""
    return [week_start + timedelta(days=i) for i in range(7)]


def bet_week_checkins(bet, week_start, week_end):
    """[week_start, week_end] 창의 (user_id, date) 체크인 집합 맵을 돌려준다.
    상세 화면의 요일별 체크 표시에 쓴다."""
    rows = (
        BetCheckin.query.filter(
            BetCheckin.bet_id == bet.id,
            BetCheckin.date >= week_start,
            BetCheckin.date <= week_end,
        ).all()
    )
    by_user = {}
    for r in rows:
        by_user.setdefault(r.user_id, set()).add(r.date)
    return by_user


def bet_past_weeks(bet, couple, on=None, cap=8):
    """완료된 지난 주들의 참여자별 지킴/못지킴 요약(최근 ~cap개, 최신 먼저).

    현재 주(week_index) 직전부터 과거로 최대 cap개 창을 훑는다. 작업량을 묶기 위해
    전체 범위 체크인을 한 번만 조회한 뒤 파이썬에서 창별로 센다."""
    if on is None:
        on = date.today()
    _, _, cur_index = bet_week_window(bet, on)
    if cur_index <= 0:
        return []  # 아직 완료된 지난 주가 없음
    parts = bet_participants(bet, couple)
    target = bet.count_target or 0
    start = bet.start_date
    first = max(0, cur_index - cap)
    range_start = start + timedelta(days=7 * first)
    range_end = start + timedelta(days=7 * cur_index - 1)  # 현재 주 직전까지
    rows = (
        BetCheckin.query.filter(
            BetCheckin.bet_id == bet.id,
            BetCheckin.date >= range_start,
            BetCheckin.date <= range_end,
        ).all()
    )
    weeks = []
    for k in range(cur_index - 1, first - 1, -1):  # 직전 주 → 과거 순
        ws = start + timedelta(days=7 * k)
        we = ws + timedelta(days=6)
        per = []
        for u in parts:
            cnt = sum(1 for r in rows if r.user_id == u.id and ws <= r.date <= we)
            per.append(
                {
                    "user_id": u.id,
                    "name": u.display_name,
                    "count": cnt,
                    "target": target,
                    "met": target > 0 and cnt >= target,
                }
            )
        weeks.append(
            {
                "week_index": k,
                "week_start": ws,
                "week_end": we,
                "participants": per,
            }
        )
    return weeks


def bet_end_info(bet, on=None):
    """내기의 '종료일(마감)' 표시 정보(순수). 목록·상세·체크인 판정 공용.

    반환 dict:
      * ``has_end``   — end_date가 설정돼 있나.
      * ``end_date``  — 종료일(또는 None).
      * ``closed``    — 마감됐나. status가 'ended'거나 (종료일이 오늘보다 과거).
      * ``days_left`` — 오늘 기준 남은 일수(종료일 - 오늘). 종료일 없으면 None.
                        0이면 오늘이 마감일(아직 체크인 가능), 음수면 지남."""
    if on is None:
        on = date.today()
    end = bet.end_date
    if end is None:
        return {
            "has_end": False,
            "end_date": None,
            "closed": bet.status == "ended",
            "days_left": None,
        }
    return {
        "has_end": True,
        "end_date": end,
        "closed": bet.status == "ended" or end < on,
        "days_left": (end - on).days,
    }


# --------------------------------------------------------------------------- #
# 내기(Bet) — 예측 내기(2b) 헬퍼 (순수/테스트 가능; HTTP·요청 컨텍스트 불필요)
# --------------------------------------------------------------------------- #
def prediction_members(couple):
    """예측 내기의 '안정적 a/b → 구성원' 매핑을 돌려준다.

    a = 승인 구성원 중 id가 작은 사람(첫째), b = 둘째. 모든 곳에서 *같은* id 오름차순
    정렬을 써서 'a'/'b'가 항상 같은 사람을 가리키게 한다(별도 컬럼 없이 렌더 시 파생).
    승인 구성원이 2명 미만이면 부족분은 None."""
    members = sorted(couple.approved_members, key=lambda u: u.id) if couple else []
    a = members[0] if len(members) >= 1 else None
    b = members[1] if len(members) >= 2 else None
    return a, b


def prediction_result(bet, couple):
    """예측 내기 렌더용 결과 dict(순수). habit 내기면 None.

    반환: ``a_name``·``b_name``(안정적 a/b 순서의 표시 이름), ``guess_a``·``guess_b``
    (각자 예측 날짜), ``actual``(실제 날짜 또는 None), ``winner``('a'|'b'|'tie'|None),
    ``winner_name``(승자 이름, 무승부/미해결이면 None), ``da``·``db``(실제 날짜와의
    |차이| 일수 — 실제 날짜가 없으면 None). 목록·상세·결과 flash에서 공용으로 쓴다."""
    if bet.type != "prediction":
        return None
    a, b = prediction_members(couple)
    a_name = a.display_name if a else "A"
    b_name = b.display_name if b else "B"
    da = dist_b = None
    if bet.actual_date is not None:
        if bet.guess_a_date is not None:
            da = abs((bet.guess_a_date - bet.actual_date).days)
        if bet.guess_b_date is not None:
            dist_b = abs((bet.guess_b_date - bet.actual_date).days)
    winner_name = None
    if bet.winner == "a":
        winner_name = a_name
    elif bet.winner == "b":
        winner_name = b_name
    return {
        "a_name": a_name,
        "b_name": b_name,
        "guess_a": bet.guess_a_date,
        "guess_b": bet.guess_b_date,
        "actual": bet.actual_date,
        "winner": bet.winner,  # 'a' | 'b' | 'tie' | None
        "winner_name": winner_name,  # 무승부/미해결이면 None
        "da": da,
        "db": dist_b,
    }


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

                # 크로스도메인 컨텍스트(Q&A + 사진 + 판결 + 다녀온 데이트)를 모아
                # 하나의 따뜻한 '우리의 N월' 회고를 생성한다. 전부 저렴한 DB 쿼리로
                # 모으고, 느린 claude 호출은 이 백그라운드 스레드에서만 돈다.
                context = insights.collect_month_context(
                    couple_id, year, month, user_a, user_b
                )
                month_label = f"{year}년 {month}월"
                try:
                    result = ai.generate_monthly_report(
                        month_label,
                        user_a.display_name,
                        user_b.display_name,
                        context,
                    )
                except Exception:  # noqa: BLE001 — claude must never crash the thread
                    log.exception(
                        "claude monthly report raised (couple=%s %s-%s)",
                        couple_id, year, month,
                    )
                    result = None

                report = MonthlyReport.query.filter_by(
                    couple_id=couple_id, year=year, month=month
                ).first()
                if report is None:
                    report = MonthlyReport(
                        couple_id=couple_id, year=year, month=month
                    )
                    db.session.add(report)

                now = datetime.utcnow()
                if result:
                    # 새 구조화 리포트 전문을 저장 + 레거시 필드도 계속 채워
                    # 하위 호환(과거 캐시/폴백)을 유지한다.
                    report.report_json = json.dumps(result, ensure_ascii=False)
                    report.summary = result.get("summary") or ""
                    report.themes = json.dumps(
                        result.get("themes") or [], ensure_ascii=False
                    )
                    report.tone = result.get("tone") or ""
                    report.fun = result.get("fun") or ""
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

    반환값: 이 패스에서 새로 'ready'가 된 행사 수(int). 채점할 게 없거나
    이미 이 커플 채점 중이거나 에러면 0. 선채점 루프(prewarm_scores)가 이 값이
    0이 될 때까지(또는 하드 캡까지) 반복 호출한다.
    """
    with _scoring_lock:
        if couple_id in _scoring:
            return 0  # 이미 이 커플 채점 중 — 겹치지 않게
        _scoring.add(couple_id)

    try:
        with app.app_context():
            try:
                couple = db.session.get(Couple, couple_id)
                if couple is None:
                    return 0
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
                    return 0

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
                    return 0

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
                # scored = 이 패스에서 새로 ready가 된 수(선채점 루프 종료 신호).
                now = datetime.utcnow()
                scored = 0
                for ev, sc_row in selected:
                    r = by_ref.get(ev.id)
                    if r is not None:
                        sc_row.score = r.get("score")
                        sc_row.reason = (r.get("reason") or "")[:1000]
                        sc_row.status = "ready"
                        scored += 1
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
                    return 0  # 반영 실패 — 진전 없음(루프 종료)
                return scored
            except Exception:  # noqa: BLE001 — belt & suspenders; 절대 탈출 금지
                db.session.rollback()
                log.exception("score_events_for_couple failed (couple=%s)", couple_id)
                return 0
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
# 데이트 뉴스 '추천받기' (P4) — 온디맨드 비동기 AI 맞춤 데이트 추천(팝업 포함)
# --------------------------------------------------------------------------- #
# 채점 워커와 같은 규율: 자기 app_context(→ 새 스레드로컬 세션)를 열고, 캡션·채점과
# 공유하는 _CAPTION_SEM으로 claude 콜을 직렬화하고(512MB: 동시에 claude 하나만),
# 모든 예외를 삼키며 스레드 밖으로 절대 raise 안 하고, finally에서 가드를 해제한다.
# 커플당 in-process 가드 집합으로 중복 실행을 막는다.
_recommending_lock = threading.Lock()
_recommending: set[int] = set()
_RECO_CANDIDATES = 20  # claude에 넘길 현재 피드 후보 상한


def recommend_dates_for_couple(app, couple_id):
    """백그라운드 워커: 이 커플의 현재 피드에서 맞춤 데이트 하나를 추천한다.

    DateRecommendation 행을 'pending'으로 보장 → 취향 프로필 1회 조립 → 현재
    진행 중(만료 안 된) 행사 최대 20개를 후보로(EventScore 높은 순 우선, 팝업 포함)
    모아 _CAPTION_SEM 안에서 ai.recommend_dates를 '한 번' 호출한다. 성공하면
    message + picks_json + status='ready', 실패하면 status='failed'(단, 직전
    ready 추천이 있으면 그걸 그대로 유지). 커밋은 rollback 가드. 절대 raise 안 하고
    finally에서 가드 키를 해제한다.
    """
    with _recommending_lock:
        if couple_id in _recommending:
            return  # 이미 이 커플 추천 생성 중 — 겹치지 않게
        _recommending.add(couple_id)

    try:
        with app.app_context():
            try:
                couple = db.session.get(Couple, couple_id)
                if couple is None:
                    return

                # 최신 추천 행을 'pending'으로 보장. 직전 ready 여부/내용을 먼저
                # 보관해 두어(실패 시 유지용) 덮어쓰기 전에 캡처한다.
                rec = DateRecommendation.query.filter_by(
                    couple_id=couple_id
                ).first()
                prior_ready = (
                    rec is not None and rec.status == "ready" and bool(rec.message)
                )
                prior_message = rec.message if rec is not None else None
                prior_picks = rec.picks_json if rec is not None else None
                if rec is None:
                    rec = DateRecommendation(couple_id=couple_id, status="pending")
                    db.session.add(rec)
                else:
                    rec.status = "pending"
                    rec.updated_at = datetime.utcnow()
                try:
                    db.session.commit()
                except IntegrityError:
                    # 동시 요청이 행을 먼저 만들었을 수 있음 — 재조회.
                    db.session.rollback()
                    rec = DateRecommendation.query.filter_by(
                        couple_id=couple_id
                    ).first()
                    if rec is None:
                        return

                # 취향 프로필(비-AI) 1회.
                profile_text = build_couple_taste_profile(couple)

                # 현재(만료 안 된) 행사 + 이 커플 점수 LEFT JOIN → EventScore가
                # ready·높은 점수 먼저 오도록 정렬해 상위 후보를 고른다(팝업 포함).
                today = date.today()
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
                        )
                    )
                    .all()
                )

                def _cand_key(row):
                    _ev, sc = row
                    # ready 점수가 높은 것 우선(-score), 나머지는 뒤로(-(-1)=1).
                    s = (
                        sc.score
                        if (sc is not None and sc.status == "ready" and sc.score is not None)
                        else -1
                    )
                    return -s
                rows.sort(key=_cand_key)

                candidates = []
                for ev, sc in rows[:_RECO_CANDIDATES]:
                    candidates.append(
                        {
                            "ref": ev.id,
                            "title": ev.title,
                            "category": ev.category,
                            "place": ev.place,
                            "description": ev.description,
                            "score": sc.score if sc is not None else None,
                        }
                    )

                if not candidates:
                    # 추천할 후보가 없음 — 직전 ready가 있으면 유지, 없으면 실패.
                    rec = DateRecommendation.query.filter_by(
                        couple_id=couple_id
                    ).first()
                    if rec is not None:
                        rec.status = "ready" if prior_ready else "failed"
                        rec.updated_at = datetime.utcnow()
                        try:
                            db.session.commit()
                        except Exception:  # noqa: BLE001
                            db.session.rollback()
                    return

                # 캡션·채점과 '같은' 세마포어로 claude 콜을 직렬화(512MB: 동시에
                # claude 하나만). claude가 터져도 실패로만 취급.
                _CAPTION_SEM.acquire()
                try:
                    try:
                        result = ai.recommend_dates(profile_text, candidates)
                    except Exception:  # noqa: BLE001 — claude가 스레드 죽이지 못하게
                        log.exception(
                            "claude date recommend raised (couple=%s)", couple_id
                        )
                        result = None
                finally:
                    _CAPTION_SEM.release()

                # 결과 반영(행이 바뀌었을 수 있어 재조회).
                rec = DateRecommendation.query.filter_by(
                    couple_id=couple_id
                ).first()
                if rec is None:
                    return
                now = datetime.utcnow()
                if result and result.get("message"):
                    rec.message = result["message"]
                    rec.picks_json = json.dumps(
                        result.get("picks") or [], ensure_ascii=False
                    )
                    rec.status = "ready"
                elif prior_ready:
                    # 실패했지만 직전 ready 추천이 있으니 그걸 그대로 유지.
                    rec.message = prior_message
                    rec.picks_json = prior_picks
                    rec.status = "ready"
                else:
                    rec.status = "failed"
                rec.updated_at = now
                try:
                    db.session.commit()
                except Exception:  # noqa: BLE001
                    db.session.rollback()
                    log.exception(
                        "commit failed for date recommendation (couple=%s)", couple_id
                    )
            except Exception:  # noqa: BLE001 — belt & suspenders; 절대 탈출 금지
                db.session.rollback()
                log.exception(
                    "recommend_dates_for_couple failed (couple=%s)", couple_id
                )
    finally:
        with _recommending_lock:
            _recommending.discard(couple_id)


def _spawn_recommend_if_idle(app, couple_id):
    """이 커플 추천 생성 백그라운드 스레드를 스폰(이미 진행 중이면 no-op).

    _spawn_scoring_if_idle과 같은 패턴 — _recommending 가드로 중복을 떨군다.
    best-effort, 절대 raise 안 함."""
    with _recommending_lock:
        if couple_id in _recommending:
            return
    try:
        threading.Thread(
            target=recommend_dates_for_couple, args=(app, couple_id), daemon=True
        ).start()
    except Exception:  # noqa: BLE001 — 추천은 best-effort
        log.exception("failed to spawn recommend thread for couple %s", couple_id)


# 선채점 커플당 하드 캡 — 워커 배치(≈24행)를 최대 이만큼 반복(≈480행)해
# 부분 실패로 인한 폭주(무한 재선택)를 막는다.
_PREWARM_MAX_BATCHES = 20


def prewarm_scores(app):
    """야간 갱신 직후 선채점(先採点) — 활성 커플마다 진행 중 행사 중 'ready'
    점수가 없는 것을 미리 채점해, 누가 탭을 열기 전에 점수가 준비되게 한다.

    활성 커플 = 승인 멤버(status='approved')가 2명 이상인 커플. 커플당
    score_events_for_couple을 '무점수가 사라질 때까지'(반환 0) 또는 하드 캡
    (_PREWARM_MAX_BATCHES)까지 순차 반복한다. 워커가 자기 app_context를 열고
    캡션과 공유하는 _CAPTION_SEM으로 claude를 직렬화하므로 동시에 claude는
    항상 하나뿐이다. _scoring 가드는 호출 사이에 풀리므로 순차 루프는 데드락
    없다. 절대 스레드 밖으로 raise하지 않는다(모두 감싸고 로그).

    ⚠️ 중복 안전(dedupe 유지): 행사 원본의 (source, source_uid) upsert와
    uq_event_source_uid 제약은 전혀 손대지 않는다 — 신규 행사 삽입이 기존 행을
    복제하지 않는다. 선채점은 워커의 아웃터조인(EventScore 없음 OR status!=
    'ready') 대상만 골라 EventScore를 만들고, uq_eventscore_couple_event가
    커플·행사당 한 행을 보장하므로 점수 행도 중복되지 않는다.
    """
    try:
        active_ids = []
        with app.app_context():
            try:
                for c in Couple.query.all():
                    try:
                        if len(c.approved_members) >= 2:
                            active_ids.append(c.id)
                    except Exception:  # noqa: BLE001 — 한 커플 실패가 전체 막지 않게
                        continue
            except Exception:  # noqa: BLE001
                db.session.rollback()
                log.exception("prewarm: 활성 커플 조회 실패")
                return

        # 커플별로 순차 선채점(워커가 각자 자기 app_context를 연다).
        for couple_id in active_ids:
            try:
                batches = 0
                while batches < _PREWARM_MAX_BATCHES:
                    n = score_events_for_couple(app, couple_id)
                    batches += 1
                    if not n:  # 남은 무점수 없음(또는 진전 없음) → 이 커플 종료
                        break
            except Exception:  # noqa: BLE001 — best-effort, 절대 탈출 금지
                log.exception("prewarm: 커플 %s 선채점 실패", couple_id)
    except Exception:  # noqa: BLE001 — belt & suspenders
        log.exception("prewarm_scores failed")


def refresh_popups_worker(app):
    """백그라운드 워커: claude 웹검색으로 팝업을 모아 EventItem(source='popup')로
    upsert하고 만료 팝업을 지운다. cron_refresh_popups가 데몬 스레드로 띄운다.

    캡션·채점과 공유하는 _CAPTION_SEM으로 claude 콜을 직렬화한다(512MB 티어:
    동시에 claude는 하나만). 서울 upsert와 '똑같이' (source, source_uid)로
    upsert하고 per-row try/except+rollback로 한 행 실패가 전체를 막지 않게 한다.
    절대 스레드 밖으로 raise하지 않으며, finally에서 세마포어와 락을 반드시 푼다.

    ⚠️ 중복 안전(dedupe 유지): (source, source_uid) upsert + 기존
    uq_event_source_uid 제약 덕에 팝업끼리도, 서울 행사와도(source가 달라)
    복제되지 않는다."""
    try:
        with app.app_context():
            # claude 웹검색 콜을 캡션/채점과 같은 세마포어로 직렬화.
            _CAPTION_SEM.acquire()
            try:
                try:
                    items = events.fetch_popups()
                except Exception:  # noqa: BLE001 — claude가 스레드 죽이지 못하게
                    log.exception("popup fetch raised")
                    items = []
            finally:
                _CAPTION_SEM.release()

            fetched = len(items)
            upserted = 0
            for it in items:
                try:
                    # 서울 upsert와 동일: (source, source_uid)로 조회→갱신, 없으면 삽입.
                    row = EventItem.query.filter_by(
                        source=it["source"], source_uid=it["source_uid"]
                    ).first()
                    if row is None:
                        row = EventItem(source=it["source"], source_uid=it["source_uid"])
                        db.session.add(row)
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
                    log.exception("popup upsert failed (uid=%s)", it.get("source_uid"))

            # 만료 팝업 삭제(source='popup' AND end_date 있고 오늘보다 과거).
            expired_deleted = 0
            try:
                today = date.today()
                expired_deleted = (
                    EventItem.query.filter(
                        EventItem.source == "popup",
                        EventItem.end_date.isnot(None),
                        EventItem.end_date < today,
                    ).delete(synchronize_session=False)
                )
                db.session.commit()
            except Exception:  # noqa: BLE001
                db.session.rollback()
                log.exception("popup expiry delete failed")

            # 무기한(end_date NULL) 팝업 안전 만료: 종료일을 못 뽑은 팝업은
            # 21일 넘게 묵으면 지운다(무기한 잔류 방지). dedupe upsert는 그대로.
            stale_deleted = 0
            try:
                cutoff = datetime.utcnow() - timedelta(days=21)
                stale_deleted = (
                    EventItem.query.filter(
                        EventItem.source == "popup",
                        EventItem.end_date.is_(None),
                        EventItem.created_at < cutoff,
                    ).delete(synchronize_session=False)
                )
                db.session.commit()
            except Exception:  # noqa: BLE001
                db.session.rollback()
                log.exception("popup stale(undated) expiry delete failed")

            log.info(
                "popup refresh done: fetched=%s upserted=%s expired=%s stale=%s",
                fetched, upserted, expired_deleted, stale_deleted,
            )
    except Exception:  # noqa: BLE001 — 스레드 밖으로 절대 raise 금지
        log.exception("refresh_popups_worker failed")
    finally:
        # 스폰 측이 잡아둔 모듈 락을 워커 종료 시 반드시 해제(중복 실행 재허용).
        try:
            _popups_refresh_lock.release()
        except RuntimeError:
            pass  # 이미 풀렸으면 무시


# 데이트 뉴스 목록 무한스크롤 배치 크기(초기 렌더·/dates/more 공통).
_DATES_BATCH = 18

# 찜 상태 값(EventPick.status). interested만 확정/공유목록에 든다.
_PICK_STATUSES = ("interested", "visited", "dismissed")


def _event_pick(u, event_id):
    """현재 사용자의 이 행사 EventPick(없으면 None)."""
    if u is None:
        return None
    return EventPick.query.filter_by(event_id=event_id, user_id=u.id).first()


def _pick_states(couple_id, user_id, event_ids):
    """행사 여러 건의 이 커플 찜 상태를 한 번의 쿼리로 모아
    event_id -> {my_status, partner_status, confirmed, interested_count}로 준다.

    N+1을 피하려 (couple, 대상 event들)의 EventPick을 한 번에 읽고 파이썬에서
    묶는다. confirmed = interested한 서로 다른 사용자가 2명(둘 다 찜)."""
    states = {}
    ids = list(event_ids)
    if not ids:
        return states
    picks = EventPick.query.filter(
        EventPick.couple_id == couple_id,
        EventPick.event_id.in_(ids),
    ).all()
    by_event = {}
    for p in picks:
        by_event.setdefault(p.event_id, []).append(p)
    for eid in ids:
        my = None
        partner_status = None
        interested_users = set()
        for p in by_event.get(eid, []):
            if p.user_id == user_id:
                my = p.status
            else:
                partner_status = p.status
            if p.status == "interested":
                interested_users.add(p.user_id)
        states[eid] = {
            "my_status": my,
            "partner_status": partner_status,
            "confirmed": len(interested_users) >= 2,
            "interested_count": len(interested_users),
        }
    return states


def _upsert_pick(u, event_id, status):
    """현재 사용자의 이 행사 찜 상태를 status로 upsert(유니크 충돌은 재조회 갱신)."""
    pick = EventPick.query.filter_by(event_id=event_id, user_id=u.id).first()
    if pick is None:
        pick = EventPick(
            couple_id=u.couple_id, event_id=event_id, user_id=u.id, status=status
        )
        db.session.add(pick)
    else:
        pick.couple_id = u.couple_id
        pick.status = status
        pick.updated_at = datetime.utcnow()
    try:
        db.session.commit()
    except IntegrityError:
        # 동시 삽입이 내 행을 먼저 만든 경우 — 재조회해 상태만 갱신.
        db.session.rollback()
        pick = EventPick.query.filter_by(event_id=event_id, user_id=u.id).first()
        if pick is not None:
            pick.status = status
            pick.updated_at = datetime.utcnow()
            db.session.commit()
    return pick


def _pick_msg(prefix, title, suffix=""):
    """알림 메시지 조립 — 제목이 길면 잘라 Notification.message(255) 상한을 넘기지 않게."""
    t = (title or "")[:60]
    return f"{prefix}{t}{suffix}"


def _ordered_date_items(couple_id, user_id, cat=None, sort="score", direction="desc"):
    """진행 중(만료 안 된) 행사를 이 커플 EventScore와 LEFT JOIN해
    {event, score, reason, score_status, bucket} 리스트로 만들고, 카테고리
    버킷(cat)으로 거르고 정렬해 반환한다.

    cat: None/"전체"이면 전체(“기타” 버킷은 여기서만 노출). 그 외는
         events.event_category_bucket 결과가 cat과 같은 것만("팝업"은 아직
         소스가 없어 빈 목록).
    sort/direction(모르는 값은 기본값으로 정규화):
      * sort="score"(추천순) — ready 먼저(점수순, direction=="desc"면 높은 순·
        "asc"면 낮은 순, 동점이면 마감 임박·시작 순) → pending → 나머지.
      * sort="date"(최신순) — 필터된 전부를 start_date(폴백 created_at)로만 정렬,
        direction=="desc"면 최신 먼저·"asc"면 오래된 먼저. NULL은 항상 맨 뒤.
    ~300행 기준 저렴하다. dates()·dates_more()가 이 단일 원천을 공유해 일관된다."""
    # sort/direction 정규화 — 알 수 없는 값은 안전한 기본으로.
    if sort not in ("score", "date"):
        sort = "score"
    if direction not in ("desc", "asc"):
        direction = "desc"
    want_bucket = cat if (cat and cat != "전체") else None

    today = date.today()
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
            db.or_(EventItem.end_date.is_(None), EventItem.end_date >= today)
        )
        .all()
    )

    items = []
    for ev, sc in rows:
        bucket = events.event_category_bucket(ev.category)
        if want_bucket is not None and bucket != want_bucket:
            continue
        status = sc.status if sc is not None else None
        items.append(
            {
                "event": ev,
                "score": sc.score if sc is not None else None,
                "reason": sc.reason if sc is not None else None,
                "score_status": status,
                "bucket": bucket,
            }
        )

    # 사용자별 찜 상태 부착 + 피드 정리: 현재 사용자가 방문함/관심없음 한 행사는
    # 그 사용자 피드에서 제외한다(커플 스코프, 한 번의 쿼리로 배치 조회).
    states = _pick_states(couple_id, user_id, [it["event"].id for it in items])
    kept = []
    for it in items:
        st = states.get(it["event"].id, {})
        if st.get("my_status") in ("visited", "dismissed"):
            continue
        it["my_status"] = st.get("my_status")
        it["confirmed"] = st.get("confirmed", False)
        it["interested_count"] = st.get("interested_count", 0)
        kept.append(it)
    items = kept

    # 마감 없는(end_date NULL) 건 날짜 정렬에서 맨 뒤로 가도록 큰 값을 준다.
    _far = date.max

    if sort == "date":
        # 최신순: 점수 그룹 무시, start_date(폴백 created_at)로만 정렬. NULL은
        # 방향과 무관하게 항상 맨 뒤(플래그 0=있음/1=없음이 1차 키).
        asc = (direction == "asc")

        def _dkey(it):
            ev = it["event"]
            d = ev.start_date
            if d is None:
                ca = getattr(ev, "created_at", None)
                d = ca.date() if ca is not None else None
            if d is None:
                return (1, 0)  # 날짜 없음 → 맨 뒤
            ordv = d.toordinal()
            return (0, ordv if asc else -ordv)

        items.sort(key=_dkey)
        return items

    def _rank(it):
        st = it["score_status"]
        ev = it["event"]
        end = ev.end_date or _far
        if st == "ready":
            # ready 내부: direction=="desc"면 점수 높은 순(-score), "asc"면 낮은 순.
            s = it["score"] or 0
            skey = -s if direction == "desc" else s
            return (0, skey, end, ev.start_date or _far)
        if st == "pending":
            return (1, 0, end, ev.start_date or _far)
        return (2, 0, end, ev.start_date or _far)

    items.sort(key=_rank)
    return items


def _date_recommendation_payload(couple_id):
    """이 커플 최신 '추천받기' 결과를 렌더/JSON 공용 dict로 만든다(순수 DB 읽기).

    반환: ``{status, message, picks:[{event_id, title, why, image_url, category,
    source}]}``. picks는 status=='ready'일 때만 채우고, 각 픽의 행사를 조회해
    존재하지 않거나 만료된(end_date < 오늘) 픽은 떨군다. 행이 없으면 status
    'none'. dates()·date_recommend_status()가 이 단일 원천을 공유한다."""
    rec = DateRecommendation.query.filter_by(couple_id=couple_id).first()
    if rec is None:
        return {"status": "none", "message": None, "picks": []}
    picks = []
    if rec.status == "ready":
        today = date.today()
        for pk in rec.picks:
            if not isinstance(pk, dict):
                continue
            ref = pk.get("ref")
            if ref is None:
                continue
            ev = db.session.get(EventItem, ref)
            if ev is None:
                continue
            # 더 이상 존재하지 않거나 만료된 행사는 픽에서 제외.
            if ev.end_date is not None and ev.end_date < today:
                continue
            picks.append(
                {
                    "event_id": ev.id,
                    "title": ev.title,
                    "why": (pk.get("why") or ""),
                    "image_url": ev.image_url,
                    "category": ev.category,
                    "source": ev.source,
                }
            )
    return {"status": rec.status, "message": rec.message or "", "picks": picks}


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

    # ---- landing: 캘린더 홈 (질문 대시보드는 /today 로 이동) ----
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

        # Full, active couple → 캘린더 홈(이번 달 달력 + 날짜별 사진).
        return _render_calendar_home(couple)

    def _render_calendar_home(couple):
        """이번 달(또는 ?month=YYYY-MM) 달력을 렌더한다. 각 날짜에 그 날 올린
        사진(커버 썸네일·개수)을 얹고, 아래 날짜 패널은 클라이언트가 채운다."""
        today = date.today()
        raw = (request.args.get("month") or "").strip()
        year, month = today.year, today.month
        if raw:
            try:
                year, month = (int(x) for x in raw.split("-", 1))
                date(year, month, 1)  # 유효성 검증(엉뚱한 값이면 이번 달로 폴백)
            except (ValueError, TypeError):
                year, month = today.year, today.month

        month_start = date(year, month, 1)
        days_in_month = calendar.monthrange(year, month)[1]
        # 다음 달 1일(반열림 상한). created_at 은 datetime 이라 경계로 쓴다.
        next_start = date(year, month, days_in_month) + timedelta(days=1)

        # 이번 달 커플 사진을 조회해 '날짜 → 사진들'로 묶는다(커플 스코프).
        photos = (
            Photo.query.filter(
                Photo.couple_id == couple.id,
                Photo.created_at >= datetime(year, month, 1),
                Photo.created_at
                < datetime(next_start.year, next_start.month, next_start.day),
            )
            .order_by(Photo.created_at.asc(), Photo.id.asc())
            .all()
        )
        by_day = {}
        for p in photos:
            by_day.setdefault(p.created_at.day, []).append(p)

        # 셀/패널이 함께 쓰는 JSON: "day" -> {count, cover, ids, schedules}
        PANEL_CAP = 60  # 하루 패널 썸네일 상한(과다 방지)
        days_data = {}
        for d, plist in by_day.items():
            ids = [p.id for p in plist]
            days_data[str(d)] = {
                "count": len(ids),
                "cover": ids[-1],  # 그 날 가장 최근 사진을 커버로
                "ids": ids[:PANEL_CAP],
                "schedules": [],
            }

        # 이번 달 일정(Schedule)을 조회해 같은 days_data에 병합한다(커플 스코프).
        # 사진만 있는 날·일정만 있는 날·둘 다 있는 날 모두 한 엔트리에 담긴다.
        # 여러 날 일정(end_date)은 이 달과 겹치는 모든 날에 제목을 얹는다 —
        # 이 달 전에 시작했거나 다음 달까지 이어지는 일정도 겹치는 구간만 표시한다.
        month_end = date(year, month, days_in_month)
        schedules = (
            Schedule.query.filter(
                Schedule.couple_id == couple.id,
                Schedule.date < next_start,
                db.or_(
                    Schedule.end_date >= month_start,
                    db.and_(
                        Schedule.end_date.is_(None),
                        Schedule.date >= month_start,
                    ),
                ),
            )
            .order_by(Schedule.date.asc(), Schedule.id.asc())
            .all()
        )
        for s in schedules:
            multi = s.end_date is not None and s.end_date != s.date
            # 이 달과 겹치는 구간 [span_start, span_end]의 모든 날에 표시한다.
            span_start = max(s.date, month_start)
            span_end = min(s.end_date or s.date, month_end)
            cur = span_start
            while cur <= span_end:
                key = str(cur.day)
                entry = days_data.get(key)
                if entry is None:
                    # 사진 없는 '일정만' 있는 날 — 사진 필드는 비워 둔다.
                    entry = {"count": 0, "cover": None, "ids": [], "schedules": []}
                    days_data[key] = entry
                entry["schedules"].append(
                    {
                        "id": s.id,
                        "title": s.title,
                        "event_id": s.event_id,
                        "multi": multi,
                    }
                )
                cur += timedelta(days=1)

        # 이번 달 '내기 체크인'을 조회해 같은 days_data에 병합한다(커플 스코프).
        # 이 커플의 내기(Bet)에 속한 체크인만 세며, 체크인이 하나라도 있는 날은
        # 셀에 🔥 아이콘을 띄운다("bet": True). 사진·일정이 전혀 없고 체크인만 있는
        # 날도 엔트리를 만들어 셀이 눌리고 아이콘이 보이게 한다.
        bet_days = (
            db.session.query(BetCheckin.date)
            .join(Bet, BetCheckin.bet_id == Bet.id)
            .filter(
                Bet.couple_id == couple.id,
                BetCheckin.date >= month_start,
                BetCheckin.date < next_start,
            )
            .distinct()
            .all()
        )
        for (cd,) in bet_days:
            key = str(cd.day)
            entry = days_data.get(key)
            if entry is None:
                entry = {"count": 0, "cover": None, "ids": [], "schedules": []}
                days_data[key] = entry
            entry["bet"] = True

        # 일요일 시작 그리드 — 앞쪽 빈칸 = (첫날 요일 +1) % 7 (월=0..일=6).
        lead = (month_start.weekday() + 1) % 7
        cells = [None] * lead + list(range(1, days_in_month + 1))
        while len(cells) % 7:
            cells.append(None)
        weeks = [cells[i:i + 7] for i in range(0, len(cells), 7)]

        prev_last = month_start - timedelta(days=1)
        prev_month = f"{prev_last.year:04d}-{prev_last.month:02d}"
        next_month = f"{next_start.year:04d}-{next_start.month:02d}"

        today_day = today.day if (today.year == year and today.month == month) else None
        selected_day = today_day or 1  # 기본 선택일: 오늘(이번 달)이면 오늘, 아니면 1일

        return render_template(
            "calendar.html",
            year=year,
            month=month,
            month_label=f"{year}년 {month}월",
            weeks=weeks,
            weekday_headers=["일", "월", "화", "수", "목", "금", "토"],
            days_data=days_data,
            today_day=today_day,
            selected_day=selected_day,
            prev_month=prev_month,
            next_month=next_month,
        )

    # ---- 캘린더 일정(Schedule) CRUD ----
    def _get_schedule_or_404(u, sid):
        """일정을 로드하되, 요청자 커플의 것이 아니면 404 — 커플 간 접근 차단."""
        s = db.session.get(Schedule, sid)
        if s is None or s.couple_id != u.couple_id:
            abort(404)
        return s

    def _parse_date(raw):
        """'YYYY-MM-DD' → date. 형식이 틀리면 None(폴백 판단은 호출부가)."""
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            y, m, d = (int(x) for x in raw.split("-", 2))
            return date(y, m, d)
        except (ValueError, TypeError):
            return None

    @app.route("/calendar/schedule/new", methods=["GET", "POST"])
    @active_couple_required
    def schedule_new():
        """일정 추가. GET은 폼(날짜·제목·설명; ?date=·?event_id= 프리필), POST는
        검증 후 생성하고 그 달 캘린더로 리다이렉트한다. ?event_id=가 이 커플 피드의
        유효한 행사면 event_id를 걸고 제목/설명을 프리필한다(무효면 무시)."""
        u = current_user()

        # 확정→'날짜 정하기' 연동: event_id가 이 커플 피드의 행사면 프리필/링크.
        def _load_event(raw):
            try:
                eid = int(raw)
            except (TypeError, ValueError):
                return None
            return db.session.get(EventItem, eid)

        if request.method == "POST":
            d = _parse_date(request.form.get("date"))
            end_raw = (request.form.get("end_date") or "").strip()
            end = _parse_date(end_raw)
            title = (request.form.get("title") or "").strip()[:200]
            description = (request.form.get("description") or "").strip() or None
            event = _load_event(request.form.get("event_id"))

            def _rerender():
                # 입력값을 살려 폼을 다시 보여준다.
                return render_template(
                    "schedule_form.html",
                    mode="new",
                    schedule=None,
                    form_date=(request.form.get("date") or ""),
                    form_end=end_raw,
                    form_title=title,
                    form_description=description or "",
                    event=event,
                )

            if not title or d is None:
                flash("날짜와 제목을 모두 입력해줘.", "error")
                return _rerender()
            if end_raw and (end is None or end < d):
                flash("종료일은 시작일과 같거나 이후 날짜여야 해.", "error")
                return _rerender()
            sched = Schedule(
                couple_id=u.couple_id,
                date=d,
                end_date=(end if end_raw else None),
                title=title,
                description=description,
                created_by=u.id,
                event_id=event.id if event is not None else None,
            )
            db.session.add(sched)
            db.session.commit()
            flash("일정을 추가했어. 📌", "ok")
            return redirect(url_for("index", month=d.strftime("%Y-%m")))

        # GET — ?date= / ?event_id= 프리필.
        prefill_date = _parse_date(request.args.get("date")) or date.today()
        event = _load_event(request.args.get("event_id"))
        form_title = event.title if event is not None else ""
        # 설명 프리필: 장소·링크 힌트를 부드럽게 채운다(있을 때만).
        form_description = ""
        if event is not None:
            bits = []
            if event.place:
                bits.append(event.place)
            if event.link:
                bits.append(event.link)
            form_description = "\n".join(bits)
        return render_template(
            "schedule_form.html",
            mode="new",
            schedule=None,
            form_date=prefill_date.strftime("%Y-%m-%d"),
            form_end="",
            form_title=form_title,
            form_description=form_description,
            event=event,
        )

    @app.route("/calendar/schedule/<int:sid>")
    @active_couple_required
    def schedule_detail(sid):
        """일정 상세 — 날짜·제목·설명, event_id가 있으면 데이트 뉴스로 링크.
        이 커플의 일정이 아니면 404."""
        u = current_user()
        sched = _get_schedule_or_404(u, sid)
        return render_template("schedule_detail.html", schedule=sched, event=sched.event)

    @app.route("/calendar/schedule/<int:sid>/edit", methods=["GET", "POST"])
    @active_couple_required
    def schedule_edit(sid):
        """일정 수정(날짜·제목·설명). 커플 구성원 누구나 수정 가능.
        POST 후 상세로 리다이렉트."""
        u = current_user()
        sched = _get_schedule_or_404(u, sid)
        if request.method == "POST":
            d = _parse_date(request.form.get("date"))
            end_raw = (request.form.get("end_date") or "").strip()
            end = _parse_date(end_raw)
            title = (request.form.get("title") or "").strip()[:200]
            description = (request.form.get("description") or "").strip() or None

            def _rerender():
                return render_template(
                    "schedule_form.html",
                    mode="edit",
                    schedule=sched,
                    form_date=(request.form.get("date") or ""),
                    form_end=end_raw,
                    form_title=title,
                    form_description=description or "",
                    event=sched.event,
                )

            if not title or d is None:
                flash("날짜와 제목을 모두 입력해줘.", "error")
                return _rerender()
            if end_raw and (end is None or end < d):
                flash("종료일은 시작일과 같거나 이후 날짜여야 해.", "error")
                return _rerender()
            sched.date = d
            sched.end_date = end if end_raw else None
            sched.title = title
            sched.description = description
            sched.updated_at = datetime.utcnow()
            db.session.commit()
            flash("일정을 수정했어.", "ok")
            return redirect(url_for("schedule_detail", sid=sched.id))
        return render_template(
            "schedule_form.html",
            mode="edit",
            schedule=sched,
            form_date=sched.date.strftime("%Y-%m-%d"),
            form_end=(sched.end_date.strftime("%Y-%m-%d") if sched.end_date else ""),
            form_title=sched.title,
            form_description=sched.description or "",
            event=sched.event,
        )

    @app.route("/calendar/schedule/<int:sid>/delete", methods=["POST"])
    @active_couple_required
    def schedule_delete(sid):
        """일정 삭제 후 그 달 캘린더로 리다이렉트."""
        u = current_user()
        sched = _get_schedule_or_404(u, sid)
        month = sched.date.strftime("%Y-%m")
        db.session.delete(sched)
        db.session.commit()
        flash("일정을 삭제했어.", "ok")
        return redirect(url_for("index", month=month))

    # ---- 캘린더 '내기'(Bet) — 습관 내기(2a) ----
    def _get_bet_or_404(u, bid):
        """내기를 로드하되, 요청자 커플의 것이 아니면 404 — 커플 간 접근 차단."""
        b = db.session.get(Bet, bid)
        if b is None or b.couple_id != u.couple_id:
            abort(404)
        return b

    def _resolve_target(u, raw):
        """대상 select 값 → target_user_id 매핑.
        ""(같이) → None · 커플 구성원 id → 그 id · 그 외 → (None, False) 무효."""
        raw = (raw or "").strip()
        if not raw:
            return None, True  # 같이(둘 다)
        try:
            tid = int(raw)
        except (TypeError, ValueError):
            return None, False
        member_ids = {m.id for m in u.couple.approved_members}
        if tid in member_ids:
            return tid, True
        return None, False

    @app.route("/calendar/bets")
    @active_couple_required
    def calendar_bets():
        """이 커플의 내기 목록(진행 중 먼저, 종료 나중). 습관 내기는 이번 주 진행·
        오늘 달성 토글·벌칙을 함께 보여준다."""
        u = current_user()
        couple = u.couple
        bets = (
            Bet.query.filter(Bet.couple_id == couple.id)
            .order_by(Bet.status.asc(), Bet.created_at.desc())
            .all()
        )
        # 'active' < 'ended' 알파벳 순이라 status asc면 active가 먼저 온다.
        today = date.today()
        rows = []
        for b in bets:
            if b.type == "prediction":
                # 예측 내기 — 습관 헬퍼(주 진행) 대신 판정 결과를 붙인다.
                rows.append(
                    {
                        "bet": b,
                        "type": "prediction",
                        "prediction": prediction_result(b, couple),
                        "end": bet_end_info(b, today),
                    }
                )
                continue
            prog = bet_progress(b, couple, today)
            # 지금 사용자가 이 내기 참여자인지 + 오늘 이미 달성했는지
            mine = next(
                (p for p in prog["participants"] if p["user_id"] == u.id), None
            )
            rows.append(
                {
                    "bet": b,
                    "type": "habit",
                    "progress": prog,
                    "is_participant": mine is not None,
                    "done_today": bool(mine and mine["done_today"]),
                    "target_name": (b.target_user.display_name
                                    if b.target_user_id else None),
                    "end": bet_end_info(b, today),
                }
            )
        return render_template("bets.html", rows=rows, me=u)

    def _bet_form_ctx(u, mode, bet, **overrides):
        """bet_form.html 렌더용 공용 컨텍스트(습관+예측 필드 전부 프리필 가능).
        overrides로 폼 값(form_*)을 덮어쓴다."""
        member_a, member_b = prediction_members(u.couple)
        ctx = dict(
            mode=mode,
            bet=bet,
            me=u,
            partner=u.partner,
            member_a=member_a,
            member_b=member_b,
            form_type="habit",
            form_title="",
            form_count="",
            form_target="",
            form_start=date.today().strftime("%Y-%m-%d"),
            form_end="",
            form_penalty="",
            form_description="",
            form_guess_a="",
            form_guess_b="",
        )
        ctx.update(overrides)
        return ctx

    @app.route("/calendar/bets/new", methods=["GET", "POST"])
    @active_couple_required
    def bet_new():
        """내기 추가 — 타입 선택(습관/예측). 습관은 활동·주 N회·대상·시작일, 예측은
        각자 날짜(안정적 a/b)로 갈린다. 벌칙·설명은 공통. 서버가 type을 읽어 분기."""
        u = current_user()
        if request.method == "POST":
            bet_type = (request.form.get("type") or "habit").strip()
            penalty = (request.form.get("penalty") or "").strip() or None
            description = (request.form.get("description") or "").strip() or None
            # 종료일(마감일) — 두 종류 공통. 비면 NULL(무기한).
            end_raw = (request.form.get("end_date") or "").strip()
            end = _parse_date(end_raw)

            if bet_type == "prediction":
                # 예측 제목은 ptitle로 받는다(JS 없이도 정확히 서버가 읽게 필드 분리).
                title = (request.form.get("ptitle") or "").strip()[:200]
                member_a, member_b = prediction_members(u.couple)
                a_name = member_a.display_name if member_a else "A"
                b_name = member_b.display_name if member_b else "B"
                guess_a = _parse_date(request.form.get("guess_a_date"))
                guess_b = _parse_date(request.form.get("guess_b_date"))
                errs = []
                if not title:
                    errs.append("예측 내용")
                if guess_a is None:
                    errs.append(f"{a_name} 날짜")
                if guess_b is None:
                    errs.append(f"{b_name} 날짜")
                if end_raw and end is None:
                    errs.append("종료일(올바른 날짜)")
                if errs:
                    flash("입력을 확인해줘: " + ", ".join(errs) + ".", "error")
                    return render_template("bet_form.html", **_bet_form_ctx(
                        u, "new", None,
                        form_type="prediction",
                        form_title=title,
                        form_end=end_raw,
                        form_penalty=penalty or "",
                        form_description=description or "",
                        form_guess_a=(request.form.get("guess_a_date") or ""),
                        form_guess_b=(request.form.get("guess_b_date") or ""),
                    ))
                bet = Bet(
                    couple_id=u.couple_id,
                    type="prediction",
                    title=title,
                    description=description,
                    start_date=date.today(),  # 예측은 롤링 주 미사용 — 생성 마커
                    end_date=(end if end_raw else None),
                    guess_a_date=guess_a,
                    guess_b_date=guess_b,
                    penalty=penalty,
                    status="active",
                    created_by=u.id,
                )
                db.session.add(bet)
                db.session.commit()
                flash("예측 내기를 만들었어. 누가 더 가까울까? 🔮", "ok")
                return redirect(url_for("bet_detail", bid=bet.id))

            # ---- 습관 내기(기존 경로 — 동작 불변) ----
            title = (request.form.get("title") or "").strip()[:200]
            start = _parse_date(request.form.get("start_date")) or date.today()
            target_id, target_ok = _resolve_target(u, request.form.get("target"))
            try:
                count_target = int((request.form.get("count_target") or "").strip())
            except (TypeError, ValueError):
                count_target = 0
            errs = []
            if not title:
                errs.append("활동")
            if count_target < 1:
                errs.append("주 N회(1 이상)")
            if not target_ok:
                errs.append("대상")
            if end_raw and (end is None or end < start):
                errs.append("종료일(시작일 이후)")
            if errs:
                flash("입력을 확인해줘: " + ", ".join(errs) + ".", "error")
                return render_template("bet_form.html", **_bet_form_ctx(
                    u, "new", None,
                    form_type="habit",
                    form_title=title,
                    form_count=(request.form.get("count_target") or ""),
                    form_target=(request.form.get("target") or ""),
                    form_start=(request.form.get("start_date")
                                or date.today().strftime("%Y-%m-%d")),
                    form_end=end_raw,
                    form_penalty=penalty or "",
                    form_description=description or "",
                ))
            bet = Bet(
                couple_id=u.couple_id,
                type="habit",
                title=title,
                description=description,
                target_user_id=target_id,
                start_date=start,
                end_date=(end if end_raw else None),
                count_target=count_target,
                penalty=penalty,
                status="active",
                created_by=u.id,
            )
            db.session.add(bet)
            db.session.commit()
            flash("내기를 만들었어. 화이팅! 🔥", "ok")
            return redirect(url_for("bet_detail", bid=bet.id))

        return render_template("bet_form.html", **_bet_form_ctx(u, "new", None))

    @app.route("/calendar/bets/<int:bid>/checkin", methods=["POST"])
    @active_couple_required
    def bet_checkin(bid):
        """오늘의 '달성' 토글(행동하는 사용자 기준). 참여자가 아니면 부드러운 안내.
        (내기, 나, 오늘) 행이 없으면 생성, 있으면 삭제(오클릭 되돌리기).
        온 곳(목록/상세)으로 돌아간다."""
        u = current_user()
        bet = _get_bet_or_404(u, bid)
        couple = u.couple
        today = date.today()

        # 되돌아갈 곳: next 파라미터 > referrer > 목록
        nxt = (request.form.get("next") or request.referrer
               or url_for("calendar_bets"))

        participant_ids = {p.id for p in bet_participants(bet, couple)}
        if u.id not in participant_ids:
            flash("이 내기의 대상이 아니야 — 응원만 해줘! 😊", "error")
            return redirect(nxt)
        if bet.status != "active":
            flash("종료된 내기야.", "error")
            return redirect(nxt)
        if bet.end_date is not None and bet.end_date < today:
            flash("이 내기는 마감됐어 — 종료일이 지났어.", "error")
            return redirect(nxt)

        existing = BetCheckin.query.filter_by(
            bet_id=bet.id, user_id=u.id, date=today
        ).first()
        if existing is None:
            db.session.add(
                BetCheckin(bet_id=bet.id, user_id=u.id, date=today)
            )
            try:
                db.session.commit()
            except IntegrityError:
                # 동시 중복(유니크 충돌) — 이미 있는 것으로 간주.
                db.session.rollback()
            flash("오늘 달성 체크! 🔥", "ok")
            # 파트너에게 가벼운 알림(선택·비차단).
            _safe_notify(
                u.partner,
                "answer",
                f"{u.display_name}님이 '{bet.title}' 내기를 오늘 달성했어! 🔥",
                url_for("bet_detail", bid=bet.id),
            )
        else:
            db.session.delete(existing)
            db.session.commit()
            flash("오늘 달성을 취소했어.", "ok")
        return redirect(nxt)

    @app.route("/calendar/bets/<int:bid>")
    @active_couple_required
    def bet_detail(bid):
        """내기 상세 — 타입별. 습관은 이번 주 요일 체크·지난 주 요약, 예측은 각자
        예측·실제 날짜·판정(더 가까운 사람)·벌칙. 커플 것이 아니면 404."""
        u = current_user()
        bet = _get_bet_or_404(u, bid)
        couple = u.couple
        today = date.today()

        if bet.type == "prediction":
            return render_template(
                "bet_detail.html",
                bet=bet,
                me=u,
                pred=prediction_result(bet, couple),
                end=bet_end_info(bet, today),
            )

        prog = bet_progress(bet, couple, today)
        week_days = bet_week_days(prog["week_start"])
        by_user = bet_week_checkins(bet, prog["week_start"], prog["week_end"])
        # 참여자별 이번 주 요일 셀(체크 여부) 구성
        week_rows = []
        for p in prog["participants"]:
            checks = by_user.get(p["user_id"], set())
            cells = [{"date": d, "checked": d in checks, "is_today": d == today}
                     for d in week_days]
            week_rows.append({"p": p, "cells": cells})
        past = bet_past_weeks(bet, couple, today, cap=8)
        mine = next(
            (p for p in prog["participants"] if p["user_id"] == u.id), None
        )
        return render_template(
            "bet_detail.html",
            bet=bet,
            me=u,
            progress=prog,
            week_days=week_days,
            week_rows=week_rows,
            past=past,
            is_participant=mine is not None,
            done_today=bool(mine and mine["done_today"]),
            target_name=(bet.target_user.display_name if bet.target_user_id else None),
            end=bet_end_info(bet, today),
        )

    @app.route("/calendar/bets/<int:bid>/edit", methods=["GET", "POST"])
    @active_couple_required
    def bet_edit(bid):
        """내기 수정 — 타입별 분기. 습관은 활동·주 N회·대상·시작일, 예측은 예측
        내용·각자 날짜. 벌칙·설명은 공통. 내기의 type은 바꾸지 않는다."""
        u = current_user()
        bet = _get_bet_or_404(u, bid)
        if request.method == "POST":
            penalty = (request.form.get("penalty") or "").strip() or None
            description = (request.form.get("description") or "").strip() or None
            # 종료일(마감일) — 두 종류 공통. 비면 NULL(무기한).
            end_raw = (request.form.get("end_date") or "").strip()
            end = _parse_date(end_raw)

            if bet.type == "prediction":
                title = (request.form.get("ptitle") or "").strip()[:200]
                member_a, member_b = prediction_members(u.couple)
                a_name = member_a.display_name if member_a else "A"
                b_name = member_b.display_name if member_b else "B"
                guess_a = _parse_date(request.form.get("guess_a_date"))
                guess_b = _parse_date(request.form.get("guess_b_date"))
                errs = []
                if not title:
                    errs.append("예측 내용")
                if guess_a is None:
                    errs.append(f"{a_name} 날짜")
                if guess_b is None:
                    errs.append(f"{b_name} 날짜")
                if end_raw and end is None:
                    errs.append("종료일(올바른 날짜)")
                if errs:
                    flash("입력을 확인해줘: " + ", ".join(errs) + ".", "error")
                    return render_template("bet_form.html", **_bet_form_ctx(
                        u, "edit", bet,
                        form_type="prediction",
                        form_title=title,
                        form_end=end_raw,
                        form_penalty=penalty or "",
                        form_description=description or "",
                        form_guess_a=(request.form.get("guess_a_date") or ""),
                        form_guess_b=(request.form.get("guess_b_date") or ""),
                    ))
                bet.title = title
                bet.guess_a_date = guess_a
                bet.guess_b_date = guess_b
                bet.end_date = end if end_raw else None
                bet.penalty = penalty
                bet.description = description
                bet.updated_at = datetime.utcnow()
                db.session.commit()
                flash("내기를 수정했어.", "ok")
                return redirect(url_for("bet_detail", bid=bet.id))

            # ---- 습관 내기(기존 경로 — 동작 불변) ----
            title = (request.form.get("title") or "").strip()[:200]
            start = _parse_date(request.form.get("start_date")) or bet.start_date
            target_id, target_ok = _resolve_target(u, request.form.get("target"))
            try:
                count_target = int((request.form.get("count_target") or "").strip())
            except (TypeError, ValueError):
                count_target = 0
            errs = []
            if not title:
                errs.append("활동")
            if count_target < 1:
                errs.append("주 N회(1 이상)")
            if not target_ok:
                errs.append("대상")
            if end_raw and (end is None or end < start):
                errs.append("종료일(시작일 이후)")
            if errs:
                flash("입력을 확인해줘: " + ", ".join(errs) + ".", "error")
                return render_template("bet_form.html", **_bet_form_ctx(
                    u, "edit", bet,
                    form_type="habit",
                    form_title=title,
                    form_count=(request.form.get("count_target") or ""),
                    form_target=(request.form.get("target") or ""),
                    form_start=(request.form.get("start_date")
                                or bet.start_date.strftime("%Y-%m-%d")),
                    form_end=end_raw,
                    form_penalty=penalty or "",
                    form_description=description or "",
                ))
            bet.title = title
            bet.count_target = count_target
            bet.target_user_id = target_id
            bet.start_date = start
            bet.end_date = end if end_raw else None
            bet.penalty = penalty
            bet.description = description
            bet.updated_at = datetime.utcnow()
            db.session.commit()
            flash("내기를 수정했어.", "ok")
            return redirect(url_for("bet_detail", bid=bet.id))

        # GET — 현재 값으로 프리필(타입별).
        _form_end = bet.end_date.strftime("%Y-%m-%d") if bet.end_date else ""
        if bet.type == "prediction":
            return render_template("bet_form.html", **_bet_form_ctx(
                u, "edit", bet,
                form_type="prediction",
                form_title=bet.title,
                form_end=_form_end,
                form_penalty=bet.penalty or "",
                form_description=bet.description or "",
                form_guess_a=(bet.guess_a_date.strftime("%Y-%m-%d")
                              if bet.guess_a_date else ""),
                form_guess_b=(bet.guess_b_date.strftime("%Y-%m-%d")
                              if bet.guess_b_date else ""),
            ))
        return render_template("bet_form.html", **_bet_form_ctx(
            u, "edit", bet,
            form_type="habit",
            form_title=bet.title,
            form_count=(bet.count_target if bet.count_target else ""),
            form_target=(str(bet.target_user_id) if bet.target_user_id else ""),
            form_start=bet.start_date.strftime("%Y-%m-%d"),
            form_end=_form_end,
            form_penalty=bet.penalty or "",
            form_description=bet.description or "",
        ))

    @app.route("/calendar/bets/<int:bid>/resolve", methods=["POST"])
    @active_couple_required
    def bet_resolve(bid):
        """예측 내기 결과 입력 — 실제 날짜를 받아 더 가까운 예측을 승자로 판정한다.
        da=|guess_a-actual|, db=|guess_b-actual| → da<db면 a, db<da면 b, 같으면 무승부.
        status를 ended로. 예측 내기만·커플 스코프."""
        u = current_user()
        bet = _get_bet_or_404(u, bid)
        couple = u.couple
        if bet.type != "prediction":
            flash("예측 내기만 결과를 입력할 수 있어.", "error")
            return redirect(url_for("bet_detail", bid=bet.id))
        if bet.guess_a_date is None or bet.guess_b_date is None:
            flash("두 사람의 예측 날짜가 있어야 결과를 낼 수 있어.", "error")
            return redirect(url_for("bet_detail", bid=bet.id))
        actual = _parse_date(request.form.get("actual_date"))
        if actual is None:
            flash("실제 날짜를 올바르게 입력해줘.", "error")
            return redirect(url_for("bet_detail", bid=bet.id))
        da = abs((bet.guess_a_date - actual).days)
        dist_b = abs((bet.guess_b_date - actual).days)
        bet.actual_date = actual
        bet.winner = "a" if da < dist_b else "b" if dist_b < da else "tie"
        bet.status = "ended"
        bet.updated_at = datetime.utcnow()
        db.session.commit()
        res = prediction_result(bet, couple)
        if bet.winner == "tie":
            flash("무승부! 둘 다 똑같이 가까웠어. 🤝", "ok")
        else:
            flash(f"{res['winner_name']}님이 더 가까웠어! 🎉", "ok")
        return redirect(url_for("bet_detail", bid=bet.id))

    @app.route("/calendar/bets/<int:bid>/end", methods=["POST"])
    @active_couple_required
    def bet_end(bid):
        """내기 종료(status→ended). 삭제 없이 기록은 남긴다."""
        u = current_user()
        bet = _get_bet_or_404(u, bid)
        bet.status = "ended"
        bet.updated_at = datetime.utcnow()
        db.session.commit()
        flash("내기를 종료했어.", "ok")
        return redirect(url_for("bet_detail", bid=bet.id))

    @app.route("/calendar/bets/<int:bid>/delete", methods=["POST"])
    @active_couple_required
    def bet_delete(bid):
        """내기 삭제(체크인도 함께 정리). 목록으로 돌아간다."""
        u = current_user()
        bet = _get_bet_or_404(u, bid)
        db.session.delete(bet)  # cascade로 BetCheckin도 삭제
        db.session.commit()
        flash("내기를 삭제했어.", "ok")
        return redirect(url_for("calendar_bets"))

    # ---- 오늘의 질문 대시보드 (예전 '/' 본문 — 온보딩 게이트 이후 부분) ----
    @app.route("/today")
    @active_couple_required
    def today():
        u = current_user()
        q = get_or_create_today_question(u.couple)
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
            return redirect(url_for("today"))
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
                url_for("today"),
            )
        # Freshen this month's cached insight in the BACKGROUND (never blocks the
        # response, never runs claude here). Only meaningful once there's a
        # partner to have a two-person conversation. Guarded against duplicates.
        if u.partner is not None:
            today = date.today()
            _kick_monthly_report(u.couple_id, today.year, today.month)
        return redirect(url_for("today"))

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
            link = url_for("today") + f"#c-q{q.id}"
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
            return redirect(url_for("today") + f"#c-q{q.id}")
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
        # 데이트 '다녀왔어'에서 넘어오면 caption 프리필 힌트(행사 제목)를 받는다.
        compose_hint = (request.args.get("compose") or "").strip()[:1000]
        if not onedrive.onedrive_enabled():
            return render_template(
                "memories.html", enabled=False, reconnect=False, photos=[],
                compose_hint=compose_hint,
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
            compose_hint=compose_hint,
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
        # 새 구조화 리포트(report_json)가 있으면 그걸로 리치 카드를 렌더한다.
        # 없으면(과거 캐시된 리포트) 아래 레거시 qualitative로 폴백한다.
        report_data = report.report if has_content else None
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
            report=report_data,
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

        전체 정렬 목록(_ordered_date_items)의 첫 배치(BATCH)만 렌더하고, 나머지는
        스크롤에 맞춰 /dates/more가 배치로 붙인다(무한 스크롤·페이지네이션 UI 없음).
        'ready' 점수가 없는 진행 중 행사가 있으면 백그라운드 채점 스레드를 스폰한다
        (요청은 절대 블록 안 함)."""
        u = current_user()
        # 선택 쿼리 파라미터(칩/정렬). 첫 로드가 일관되도록 기본값을 명시한다.
        cat = request.args.get("cat") or "전체"
        sort = request.args.get("sort") or "score"
        direction = request.args.get("dir") or "desc"
        items = _ordered_date_items(
            u.couple_id, u.id, cat=cat, sort=sort, direction=direction
        )

        # 셀프힐: 'ready' 점수가 없는 진행 중 행사가 있으면 백그라운드 채점을 스폰.
        # (뷰를 반복 조회해도 _scoring 가드로 스레드가 쌓이지 않는다. 요청 블록 X.)
        needs_scoring = any(it["score_status"] != "ready" for it in items)
        if items and needs_scoring:
            _spawn_scoring_if_idle(current_app._get_current_object(), u.couple_id)

        # ready 추천은 첫 로드에서 바로 그리고, pending이면 클라가 '뽑는 중' 상태·폴링을 재개한다.
        rec_payload = _date_recommendation_payload(u.couple_id)
        recommendation = rec_payload if rec_payload["status"] in ("ready", "pending") else None

        # 첫 배치만 그리고, 더 있으면 센티넬을 띄운다(클라가 이어서 당겨온다).
        first = items[:_DATES_BATCH]
        has_more = len(items) > _DATES_BATCH
        return render_template(
            "dates.html",
            items=first,
            has_more=has_more,
            recommendation=recommendation,
            cur_cat=cat,
            cur_sort=sort,
            cur_dir=direction,
        )

    @app.route("/dates/recommend", methods=["POST"])
    @active_couple_required
    def date_recommend():
        """'✨ 추천받기' — 이 커플 추천을 pending으로 세팅하고 백그라운드 워커를
        (가드로) 스폰한다. claude는 절대 여기(요청 경로)서 돌지 않는다 — 워커에서만.
        FAB이 fetch로 치므로 JSON을 돌려준다."""
        u = current_user()
        rec = DateRecommendation.query.filter_by(couple_id=u.couple_id).first()
        if rec is None:
            rec = DateRecommendation(couple_id=u.couple_id, status="pending")
            db.session.add(rec)
        else:
            rec.status = "pending"
            rec.updated_at = datetime.utcnow()
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            rec = DateRecommendation.query.filter_by(couple_id=u.couple_id).first()
            if rec is not None:
                rec.status = "pending"
                rec.updated_at = datetime.utcnow()
                db.session.commit()

        # 백그라운드 워커 스폰(요청은 절대 블록 안 함). 중복은 _recommending 가드가 떨군다.
        _spawn_recommend_if_idle(current_app._get_current_object(), u.couple_id)
        return jsonify({"ok": True, "status": "pending"})

    @app.route("/dates/recommend/status")
    @active_couple_required
    def date_recommend_status():
        """이 커플 최신 추천 상태 JSON — FAB 패널 폴링용. 순수 DB 읽기(claude 없음).

        {status, message, picks:[{event_id, title, why, image_url, category,
        source}]}. 존재하지 않거나 만료된 픽은 떨군다."""
        u = current_user()
        return jsonify(_date_recommendation_payload(u.couple_id))

    @app.route("/dates/more")
    @active_couple_required
    def dates_more():
        """무한스크롤 다음 배치 — offset부터 BATCH개 카드 HTML 조각.

        범위를 벗어나면 카드 없는 빈 조각을 돌려주고, 클라이언트는 빈 조각을
        받으면 로딩을 멈춘다. 초기 렌더와 같은 _date_cards.html을 쓴다."""
        u = current_user()
        try:
            offset = int(request.args.get("offset", 0))
        except (TypeError, ValueError):
            offset = 0
        if offset < 0:
            offset = 0
        cat = request.args.get("cat") or "전체"
        sort = request.args.get("sort") or "score"
        direction = request.args.get("dir") or "desc"
        items = _ordered_date_items(
            u.couple_id, u.id, cat=cat, sort=sort, direction=direction
        )
        batch = items[offset:offset + _DATES_BATCH]
        return render_template("_date_cards.html", items=batch)

    @app.route("/dates/scores")
    @active_couple_required
    def dates_scores():
        """지정 행사 id들의 이 커플 채점 상태 JSON — 제자리 칩 갱신용.

        ids: 콤마 구분 정수(최대 60개). 각 id에 {status, score, reason}을 준다.
        EventScore 행이 없거나 존재하지 않는 id는 status 'none'으로 채운다."""
        u = current_user()
        raw = request.args.get("ids", "") or ""
        ids = []
        for tok in raw.split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                ids.append(int(tok))
            except ValueError:
                continue
            if len(ids) >= 60:
                break

        out = {}
        if ids:
            scores = (
                EventScore.query.filter(
                    EventScore.couple_id == u.couple_id,
                    EventScore.event_id.in_(ids),
                ).all()
            )
            by_event = {s.event_id: s for s in scores}
            for eid in ids:
                s = by_event.get(eid)
                if s is None:
                    out[str(eid)] = {
                        "status": "none", "score": None, "reason": None
                    }
                else:
                    out[str(eid)] = {
                        "status": s.status,
                        "score": s.score,
                        "reason": s.reason,
                    }
        return jsonify(out)

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
        pk = _pick_states(u.couple_id, u.id, [item.id]).get(item.id, {})
        return render_template(
            "date_detail.html",
            item=item,
            score=score,
            reason=reason,
            score_status=score_status,
            my_status=pk.get("my_status"),
            partner_status=pk.get("partner_status"),
            confirmed=pk.get("confirmed", False),
        )

    # ---- 데이트 찜/방문함/관심없음 액션(AJAX JSON) ----
    def _pick_json(u, event_id):
        """공통 응답 상태 — 이 (커플,사용자,행사)의 최신 찜 상태 JSON dict."""
        st = _pick_states(u.couple_id, u.id, [event_id]).get(event_id, {})
        return {
            "ok": True,
            "my_status": st.get("my_status"),
            "confirmed": st.get("confirmed", False),
            "interested_count": st.get("interested_count", 0),
        }

    @app.route("/dates/<int:event_id>/pick", methods=["POST"])
    @active_couple_required
    def date_pick(event_id):
        """내 찜(interested) upsert + 상대 알림. 이번 찜으로 '확정'이 새로 성립하면
        (직전엔 미확정) 두 사람 모두에게 확정 알림을 *한 번만* 보낸다."""
        u = current_user()
        item = db.session.get(EventItem, event_id)
        if item is None:
            return jsonify({"ok": False}), 404
        # 확정 전이(not-confirmed → confirmed) 감지를 위해 직전 상태를 먼저 읽는다.
        before = _pick_states(u.couple_id, u.id, [event_id]).get(event_id, {})
        was_confirmed = before.get("confirmed", False)

        _upsert_pick(u, event_id, "interested")

        after = _pick_states(u.couple_id, u.id, [event_id]).get(event_id, {})
        now_confirmed = after.get("confirmed", False)

        partner = u.partner
        link = url_for("date_detail", item_id=event_id)
        # 상대에게 찜 알림(행위자 본인에겐 절대 보내지 않음).
        _safe_notify(
            partner, "date_pick",
            _pick_msg("상대가 데이트를 찜했어! 💖 ", item.title), link,
        )
        # 새로 확정됐으면(전이 순간에만) 두 사람 모두에게 확정 알림 — 재찜엔 안 울림.
        if now_confirmed and not was_confirmed:
            msg = _pick_msg("데이트 확정! 💘 ", item.title, " 둘 다 찜했어")
            _safe_notify(u, "date_confirm", msg, link)
            _safe_notify(partner, "date_confirm", msg, link)

        return jsonify(_pick_json(u, event_id))

    @app.route("/dates/<int:event_id>/unpick", methods=["POST"])
    @active_couple_required
    def date_unpick(event_id):
        """내 찜 취소 — 내 EventPick 행을 삭제(토글 오프). 알림 없음."""
        u = current_user()
        item = db.session.get(EventItem, event_id)
        if item is None:
            return jsonify({"ok": False}), 404
        EventPick.query.filter_by(event_id=event_id, user_id=u.id).delete()
        db.session.commit()
        return jsonify(_pick_json(u, event_id))

    @app.route("/dates/<int:event_id>/visited", methods=["POST"])
    @active_couple_required
    def date_visited(event_id):
        """내 상태를 '다녀옴'으로 — 내 피드에서 숨김. 알림 없음."""
        u = current_user()
        item = db.session.get(EventItem, event_id)
        if item is None:
            return jsonify({"ok": False}), 404
        _upsert_pick(u, event_id, "visited")
        return jsonify(_pick_json(u, event_id))

    @app.route("/dates/<int:event_id>/dismiss", methods=["POST"])
    @active_couple_required
    def date_dismiss(event_id):
        """내 상태를 '관심없음'으로 — 내 피드에서 숨김. 알림 없음."""
        u = current_user()
        item = db.session.get(EventItem, event_id)
        if item is None:
            return jsonify({"ok": False}), 404
        _upsert_pick(u, event_id, "dismissed")
        return jsonify(_pick_json(u, event_id))

    @app.route("/dates/<int:event_id>/went", methods=["POST"])
    @active_couple_required
    def date_went(event_id):
        """확정(둘 다 찜)된 데이트를 '다녀왔어'로 마감 → 추억 앨범으로 연결한다.

        확정 상태일 때만 동작한다: 커플 두 사람 모두의 EventPick.status를
        'visited'로 바꿔(활성 플랜/피드에서 빠짐) 추억 업로드 화면으로 보낸다 —
        행사 제목을 caption 프리필 힌트로 넘겨(url `?compose=<title>`) 그 날의
        사진이 데이트 이름으로 태깅되게 한다. 확정이 아니면 부드러운 안내 후
        되돌아간다(상태 변경 없음)."""
        u = current_user()
        item = db.session.get(EventItem, event_id)
        if item is None:
            abort(404)
        st = _pick_states(u.couple_id, u.id, [event_id]).get(event_id, {})
        if not st.get("confirmed"):
            flash("아직 둘 다 찜한 확정 데이트가 아니야.", "error")
            return redirect(request.referrer or url_for("dates_wishlist"))
        # 커플 두 사람 모두의 이 행사 찜을 'visited'로 — 활성 플랜에서 빠진다.
        for member in u.couple.approved_members:
            _upsert_pick(member, event_id, "visited")
        # 추억 업로드로 이동 — 캡션 프리필 힌트(행사 제목)를 쿼리로 넘긴다.
        return redirect(url_for("memories", compose=item.title))

    @app.route("/dates/wishlist")
    @active_couple_required
    def dates_wishlist():
        """커플 공유 찜 목록 — interested가 1개 이상인(만료 안 된) 행사.
        확정(둘 다 찜)을 위에, 찜(한 명만)을 아래에 나눠 보여준다."""
        u = current_user()
        today = date.today()
        rows = (
            db.session.query(EventPick, EventItem)
            .join(EventItem, EventItem.id == EventPick.event_id)
            .filter(
                EventPick.couple_id == u.couple_id,
                EventPick.status == "interested",
                db.or_(EventItem.end_date.is_(None), EventItem.end_date >= today),
            )
            .all()
        )
        # 행사별로 누가 찜했는지 모은다.
        by_event = {}
        for p, ev in rows:
            entry = by_event.setdefault(ev.id, {"event": ev, "uids": set()})
            entry["uids"].add(p.user_id)

        confirmed_items, wish_items = [], []
        for entry in by_event.values():
            ev = entry["event"]
            uids = entry["uids"]
            me = u.id in uids
            partner = any(x != u.id for x in uids)
            confirmed = len(uids) >= 2
            if confirmed:
                who = "나 · 상대 둘 다 찜"
            elif me:
                who = "내가 찜"
            else:
                who = "상대가 찜"
            it = {
                "event": ev,
                "score_status": None,
                "my_status": "interested" if me else None,
                "confirmed": confirmed,
                "interested_count": len(uids),
                "picked_by_me": me,
                "picked_by_partner": partner,
                "wishlist_who": who,
            }
            (confirmed_items if confirmed else wish_items).append(it)

        # 마감 임박 순(마감 없는 건 뒤로), 다음 시작 순으로 정렬.
        _far = date.max

        def _wkey(it):
            ev = it["event"]
            return (ev.end_date or _far, ev.start_date or _far)

        confirmed_items.sort(key=_wkey)
        wish_items.sort(key=_wkey)
        return render_template(
            "dates_wishlist.html",
            confirmed_items=confirmed_items,
            wish_items=wish_items,
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
                        url_for("today"),
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
                    "/today",
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

            # 선채점(비차단): 갱신 직후 활성 커플들의 미채점 진행 행사를 백그라운드
            # 데몬 스레드로 미리 채점해 둔다. 요청은 이 스레드를 기다리지 않고 즉시
            # 응답한다(선채점은 자기 안에서 모든 예외를 삼킨다).
            try:
                threading.Thread(
                    target=prewarm_scores,
                    args=(app,),
                    daemon=True,
                ).start()
            except Exception:  # noqa: BLE001 — 선채점 스폰 실패가 응답을 막지 않게
                log.exception("failed to spawn prewarm thread")

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

    @app.route("/internal/cron/refresh-popups", methods=["POST"])
    def cron_refresh_popups():
        """팝업 수집을 '백그라운드로' 시작 — claude 웹검색은 ~120s+라 동기로 돌리면
        gunicorn 워커 타임아웃에 죽는다. 그래서 데몬 스레드를 띄우고 즉시 응답한다.

        cron_refresh_events와 '똑같이' CRON_SECRET(헤더 ``X-Cron-Secret`` 또는
        ``?token=``)으로 상수시간 비교 보호. 모듈 락을 비차단으로 잡아 두 번째
        트리거가 이미 도는 중이면 busy를 돌려준다(락은 워커가 finally에서 푼다)."""
        provided = request.headers.get("X-Cron-Secret") or request.args.get("token") or ""
        if not CRON_SECRET or not hmac.compare_digest(provided, CRON_SECRET):
            abort(403)

        # 동시 실행 방지(락 못 잡으면 이미 도는 중 — busy). 잡았으면 워커가 finally
        # 에서 푼다. 스레드 스폰이 실패하면 여기서 되돌려 락을 즉시 푼다.
        if not _popups_refresh_lock.acquire(blocking=False):
            return jsonify({"ok": False, "reason": "busy"})
        try:
            threading.Thread(
                target=refresh_popups_worker,
                args=(app,),
                daemon=True,
            ).start()
        except Exception:  # noqa: BLE001 — 스폰 실패 시 락 되돌리고 알림
            _popups_refresh_lock.release()
            log.exception("failed to spawn popup refresh thread")
            return jsonify({"ok": False, "reason": "spawn_failed"})
        # 120s 페치를 기다리지 않고 즉시 응답(백그라운드에서 진행).
        return jsonify({"ok": True, "started": True})

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
