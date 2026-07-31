"""couple-daily — a tiny daily-question app for exactly two people.

Identity constraint (do not violate): the AI mechanism is the `claude` CLI run
as a backend subprocess (see ai.py), NOT the Anthropic API/SDK. This file wires
Flask + SQLAlchemy, auth, couple linking, the daily question, monthly insight,
settings and PWA plumbing on top of that.

Run locally:   python app.py            (SQLite fallback, debug reloader)
Run in prod:   gunicorn 'app:create_app()'   (Postgres via DATABASE_URL)
"""
import functools
import logging
import os
import secrets
import string
import time
from datetime import date, datetime, timedelta
from urllib.parse import urlencode

import requests
from flask import (
    Flask,
    abort,
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
import insights
from models import Answer, Couple, DailyQuestion, Setting, User, db, run_startup_migrations

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


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
def _register_routes(app: Flask):
    @app.context_processor
    def inject_globals():
        return {
            "app_name": Setting.get("app_name", DEFAULT_APP_NAME),
            "me": current_user(),
            "kakao_enabled": kakao_enabled(),
        }

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
        return render_template(
            "dashboard.html",
            question=q,
            my_ans=my_ans,
            partner_ans=partner_ans,
            partner=partner,
            revealed=revealed,
            today=q.q_date,
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
                db.session.commit()
                flash(
                    "카카오 계정이 연결됐어! 이제 카카오로도 로그인할 수 있어.", "ok"
                )
                return redirect(url_for("settings"))
            # Link mode but somehow not logged in → fall through to normal login.

        # Existing Kakao user → just log in.
        user = User.query.filter_by(kakao_id=kakao_id).first()
        if user:
            session.clear()
            session["user_id"] = user.id
            session.permanent = True
            return redirect(url_for("index"))

        # New Kakao user → needs to choose/join a couple space next.
        session["pending_kakao_id"] = kakao_id
        session["pending_kakao_nickname"] = nickname
        return redirect(url_for("kakao_connect"))

    @app.route("/auth/kakao/connect", methods=["GET", "POST"])
    def kakao_connect():
        if current_user():
            return redirect(url_for("index"))
        kakao_id = session.get("pending_kakao_id")
        nickname = session.get("pending_kakao_nickname")
        if not kakao_id:
            # No pending Kakao identity — start over.
            return redirect(url_for("login"))

        if request.method == "POST":
            # Guard against a duplicate that appeared between callback and submit.
            if User.query.filter_by(kakao_id=kakao_id).first():
                session.pop("pending_kakao_id", None)
                session.pop("pending_kakao_nickname", None)
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
        if existing:
            existing.text = text  # allow editing until reveal is fine; keep simple
        else:
            db.session.add(Answer(question_id=q.id, user_id=u.id, text=text))
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
        return redirect(url_for("index"))

    # ---- history ----
    @app.route("/history")
    @active_couple_required
    def history():
        u = current_user()
        partner = u.partner
        qs = (
            DailyQuestion.query.filter_by(couple_id=u.couple_id)
            .order_by(DailyQuestion.q_date.desc())
            .all()
        )
        items = []
        for q in qs:
            my = q.answer_by(u.id)
            pa = q.answer_by(partner.id) if partner else None
            items.append(
                {
                    "date": q.q_date,
                    "question": q.text,
                    "revealed": q.both_answered,
                    "mine": my.text if my else None,
                    "partner": pa.text if (pa and q.both_answered) else None,
                }
            )
        return render_template("history.html", items=items, partner=partner)

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

        stats = insights.compute_monthly_stats(u.couple_id, year, month, u, partner)
        qa_items = insights.collect_month_qa(u.couple_id, year, month, u, partner)
        qualitative = ai.generate_monthly_qualitative(
            stats["month_label"], u.display_name, partner.display_name, qa_items
        )
        # previous / next month links
        prev_m = (month - 1) or 12
        prev_y = year - 1 if month == 1 else year
        next_m = 1 if month == 12 else month + 1
        next_y = year + 1 if month == 12 else year
        return render_template(
            "insight.html",
            stats=stats,
            qualitative=qualitative,
            has_data=bool(qa_items),
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

    @app.errorhandler(500)
    @app.errorhandler(Exception)
    def _server_error(e):
        # Let HTTP errors (404/405/etc.) keep their own handling/status.
        from werkzeug.exceptions import HTTPException

        if isinstance(e, HTTPException):
            return e
        # Surface the real root cause in the logs (Render), never to the user.
        app.logger.exception("unhandled exception rendering %s", request.path)
        return (
            render_template(
                "error.html",
                heading="이런, 문제가 생겼어",
                message="에러가 발생했습니다",
            ),
            500,
        )


app = create_app()

if __name__ == "__main__":
    # Local dev only. Production uses gunicorn (see Dockerfile / fly.toml).
    app.run(host="127.0.0.1", port=5000, debug=True)
