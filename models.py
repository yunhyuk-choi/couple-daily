"""SQLAlchemy models for couple-daily.

Data model is intentionally small — the app serves exactly one couple (two
people) per couple-space, though the schema does not forbid several couples
existing in one database.
"""
import json
import logging
from datetime import datetime, date

from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import inspect, text

db = SQLAlchemy()

log = logging.getLogger(__name__)


def run_startup_migrations():
    """Idempotent, best-effort schema evolution run on every boot.

    ``db.create_all()`` creates missing tables but never ALTERs existing ones,
    and production runs on a live Postgres (Neon) with pre-existing tables. This
    brings an already-created ``users`` table up to date for Kakao login:
      * add the ``kakao_id`` column if missing,
      * drop NOT NULL on ``email`` / ``password_hash`` (Kakao users have neither).

    Works on both SQLite and PostgreSQL, and is safe to run repeatedly. Any
    failure is logged and swallowed so a migration hiccup never crashes boot.
    """
    engine = db.engine
    dialect = engine.dialect.name  # 'sqlite' | 'postgresql' | ...
    try:
        insp = inspect(engine)
        if "users" not in insp.get_table_names():
            return  # fresh DB — create_all() already made the current schema

        cols = {c["name"]: c for c in insp.get_columns("users")}

        # 1) add kakao_id if it's missing
        if "kakao_id" not in cols:
            try:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE users ADD COLUMN kakao_id VARCHAR"))
                log.info("startup migration: added users.kakao_id")
            except Exception:  # noqa: BLE001
                log.exception("startup migration: failed adding users.kakao_id")

        # 1b) add profile_image_url (Kakao profile photo) if it's missing
        if "profile_image_url" not in cols:
            try:
                with engine.begin() as conn:
                    conn.execute(
                        text("ALTER TABLE users ADD COLUMN profile_image_url VARCHAR")
                    )
                log.info("startup migration: added users.profile_image_url")
            except Exception:  # noqa: BLE001
                log.exception(
                    "startup migration: failed adding users.profile_image_url"
                )

        # 2) drop NOT NULL on email / password_hash so Kakao users can exist.
        #    SQLite cannot ALTER a column's nullability in place; older SQLite
        #    DBs keep NOT NULL, but Kakao rows simply never touch those columns
        #    with NULL on SQLite (dev only), so this is a no-op there.
        if dialect == "postgresql":
            for col in ("email", "password_hash"):
                info = cols.get(col)
                if info is not None and not info.get("nullable", True):
                    try:
                        with engine.begin() as conn:
                            conn.execute(
                                text(f"ALTER TABLE users ALTER COLUMN {col} DROP NOT NULL")
                            )
                        log.info("startup migration: dropped NOT NULL on users.%s", col)
                    except Exception:  # noqa: BLE001
                        log.exception(
                            "startup migration: failed dropping NOT NULL on users.%s", col
                        )

        # 3) photos: Phase-2 vision captioning columns (tags + caption_status).
        #    The `photos` table pre-exists from Phase 1; add the two new columns
        #    if missing. caption_status is added with a server DEFAULT 'ready' so
        #    EXISTING Phase-1 rows backfill to 'ready' (they are never auto-
        #    captioned and must not get stuck showing "분석 중"); NEW rows get
        #    'pending' from the model-level Python default at insert time.
        if "photos" in insp.get_table_names():
            pcols = {c["name"]: c for c in insp.get_columns("photos")}
            if "tags" not in pcols:
                try:
                    with engine.begin() as conn:
                        conn.execute(text("ALTER TABLE photos ADD COLUMN tags TEXT"))
                    log.info("startup migration: added photos.tags")
                except Exception:  # noqa: BLE001
                    log.exception("startup migration: failed adding photos.tags")
            if "caption_status" not in pcols:
                try:
                    with engine.begin() as conn:
                        conn.execute(
                            text(
                                "ALTER TABLE photos ADD COLUMN caption_status "
                                "VARCHAR DEFAULT 'ready'"
                            )
                        )
                    log.info("startup migration: added photos.caption_status")
                except Exception:  # noqa: BLE001
                    log.exception(
                        "startup migration: failed adding photos.caption_status"
                    )
    except Exception:  # noqa: BLE001 — never let a migration hiccup crash boot
        log.exception("startup migration: unexpected error; continuing boot")


class Couple(db.Model):
    """A shared space for two people, joined via an invite code."""
    __tablename__ = "couples"

    id = db.Column(db.Integer, primary_key=True)
    invite_code = db.Column(db.String(16), unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    members = db.relationship("User", backref="couple", lazy="dynamic")
    questions = db.relationship("DailyQuestion", backref="couple", lazy="dynamic")

    @property
    def approved_members(self):
        return [u for u in self.members if u.status == "approved"]

    @property
    def is_full(self):
        # Two-person app: two members (any status) fills the space.
        return self.members.count() >= 2


class User(db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    # Email/password users have both; Kakao social users have neither (nullable).
    email = db.Column(db.String(255), unique=True, nullable=True, index=True)
    password_hash = db.Column(db.String(255), nullable=True)
    # Kakao user id (a number, stored as string). Unique when present, null for
    # email/password users.
    kakao_id = db.Column(db.String(64), unique=True, nullable=True, index=True)
    # Kakao profile photo URL, if the user granted it. Optional for everyone.
    profile_image_url = db.Column(db.String(512), nullable=True)
    display_name = db.Column(db.String(60), nullable=False)
    couple_id = db.Column(db.Integer, db.ForeignKey("couples.id"), nullable=True)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)
    # 'approved' (space creator or accepted partner) | 'pending' (awaiting approval)
    status = db.Column(db.String(16), default="approved", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    answers = db.relationship("Answer", backref="user", lazy="dynamic")

    @property
    def partner(self):
        if not self.couple_id:
            return None
        return User.query.filter(
            User.couple_id == self.couple_id, User.id != self.id
        ).first()

    @property
    def is_active_couple(self):
        """Can this user actually use the app? Approved and linked to a couple."""
        return bool(self.couple_id) and self.status == "approved"


class DailyQuestion(db.Model):
    """One question per couple per day. Stored so it is stable for the day."""
    __tablename__ = "daily_questions"
    __table_args__ = (db.UniqueConstraint("couple_id", "q_date", name="uq_couple_date"),)

    id = db.Column(db.Integer, primary_key=True)
    couple_id = db.Column(db.Integer, db.ForeignKey("couples.id"), nullable=False, index=True)
    q_date = db.Column(db.Date, default=date.today, nullable=False, index=True)
    text = db.Column(db.Text, nullable=False)
    # 'ai' when generated by claude, 'fallback' when the CLI failed
    source = db.Column(db.String(16), default="ai", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    answers = db.relationship(
        "Answer", backref="question", lazy="dynamic", cascade="all, delete-orphan"
    )
    comments = db.relationship(
        "Comment",
        backref="question",
        lazy="dynamic",
        cascade="all, delete-orphan",
        order_by="Comment.created_at",
    )

    def answer_by(self, user_id):
        return self.answers.filter_by(user_id=user_id).first()

    @property
    def both_answered(self):
        return self.answers.count() >= 2

    def ordered_comments(self):
        """All of this question's comments, oldest first (any nesting depth)."""
        return self.comments.order_by(Comment.created_at.asc()).all()


class Answer(db.Model):
    __tablename__ = "answers"
    __table_args__ = (
        db.UniqueConstraint("question_id", "user_id", name="uq_question_user"),
    )

    id = db.Column(db.Integer, primary_key=True)
    question_id = db.Column(
        db.Integer, db.ForeignKey("daily_questions.id"), nullable=False, index=True
    )
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Comment(db.Model):
    """A comment on a day's revealed answers. Supports threaded replies via
    a nullable self-referential ``parent_id``."""
    __tablename__ = "comments"

    id = db.Column(db.Integer, primary_key=True)
    question_id = db.Column(
        db.Integer, db.ForeignKey("daily_questions.id"), nullable=False, index=True
    )
    author_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    # Null for a top-level comment; points at another comment for a reply.
    parent_id = db.Column(
        db.Integer, db.ForeignKey("comments.id"), nullable=True, index=True
    )
    text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    author = db.relationship("User")
    # Direct replies. Deleting a comment deletes its replies (tidy).
    replies = db.relationship(
        "Comment",
        backref=db.backref("parent", remote_side=[id]),
        lazy="dynamic",
        cascade="all, delete-orphan",
        order_by="Comment.created_at",
    )


class Notification(db.Model):
    """An in-app notification for a single recipient.

    Created by the event hooks in app.py (partner answered / commented / couple
    approved). Designed so a future web-push branch can reuse the same hooks:
    push would simply fan out from the same ``notify()`` call site. ``is_read``
    powers the topbar bell dot; viewing ``/notifications`` clears it.
    """
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    # The RECIPIENT (never the actor who triggered the event).
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    # 'answer' | 'comment' | 'approval'
    type = db.Column(db.String(16), nullable=False)
    message = db.Column(db.String(255), nullable=False)
    # Relative URL to navigate to when the row is tapped.
    link = db.Column(db.String(255), nullable=False)
    is_read = db.Column(db.Boolean, default=False, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


def notify(user, type, message, link):
    """Create + add a Notification for ``user`` (the recipient).

    Returns the Notification (added to the session, not yet committed) or None
    when there is no recipient. The caller commits — keeping the commit at the
    call site lets the hook isolate a notification failure from the main action.
    """
    if user is None:
        return None
    n = Notification(user_id=user.id, type=type, message=message, link=link)
    db.session.add(n)
    return n


class PushSubscription(db.Model):
    """A single browser/device Web Push subscription for a user.

    Layered on top of the in-app ``Notification``: when an event fans out, we
    persist the in-app row AND push to every ``PushSubscription`` the recipient
    has registered. One user can have several (phone, laptop, installed PWA).
    ``endpoint`` is globally unique — the same browser re-subscribing yields the
    same endpoint, so we upsert by it. Created by ``db.create_all()``.
    """
    __tablename__ = "push_subscriptions"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    # The push service URL for this subscription. Unique across all users.
    endpoint = db.Column(db.String(512), unique=True, nullable=False)
    # Public key + auth secret the push service needs to encrypt the payload.
    p256dh = db.Column(db.String(255), nullable=False)
    auth = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Photo(db.Model):
    """A photo the couple uploaded, stored in OneDrive (``couple-daily`` folder).

    OneDrive holds the bytes; this row keeps only the item id + light metadata.
    ``caption`` is nullable now — Phase 2 will fill it with a Vision-generated
    description. Scoped to ``couple_id`` so a couple only ever sees their own
    photos. Created by ``db.create_all()`` (brand-new table, no ALTER migration).
    """
    __tablename__ = "photos"

    id = db.Column(db.Integer, primary_key=True)
    couple_id = db.Column(
        db.Integer, db.ForeignKey("couples.id"), nullable=False, index=True
    )
    # The OneDrive drive-item id (opaque). What we delete / fetch a URL by.
    onedrive_item_id = db.Column(db.String(255), nullable=False)
    # The sanitized/unique name we stored it under in OneDrive.
    filename = db.Column(db.String(255), nullable=False)
    # The user's original upload filename (display only; may be absent).
    original_name = db.Column(db.String(255), nullable=True)
    uploaded_by = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    # Optional caption. Null in Phase 1; Phase 2 (Vision) populates it with a
    # one-sentence Korean description produced by `claude` vision.
    caption = db.Column(db.Text, nullable=True)
    # JSON-encoded list[str] of Korean keyword tags reflecting the photo content
    # (Phase 2, filled alongside ``caption`` by the background captioner). Null
    # until captioning succeeds.
    tags = db.Column(db.Text, nullable=True)
    # Captioning lifecycle: 'pending' (queued/in-flight), 'ready' (caption+tags
    # populated, or a manual caption was given), 'failed' (vision errored/timed
    # out — caption stays null). New auto-captioned uploads start 'pending'.
    caption_status = db.Column(db.String(16), default="pending", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    uploader = db.relationship("User")

    @property
    def tags_list(self):
        """Decode the JSON-stored tags back to a list (defensive)."""
        if not self.tags:
            return []
        try:
            v = json.loads(self.tags)
            return v if isinstance(v, list) else []
        except (ValueError, TypeError):
            return []


class Setting(db.Model):
    """Global key/value settings (e.g. the configurable app name)."""
    __tablename__ = "settings"

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text, nullable=False)

    @staticmethod
    def get(key, default=None):
        row = db.session.get(Setting, key)
        return row.value if row else default

    @staticmethod
    def set(key, value):
        row = db.session.get(Setting, key)
        if row:
            row.value = value
        else:
            db.session.add(Setting(key=key, value=value))


class MonthlyReport(db.Model):
    """Cached AI qualitative monthly insight for one couple + month.

    The ``/insight`` page must render INSTANTLY from the DB and never block on
    the slow ``claude -p`` subprocess. This row caches the qualitative content
    produced by ``ai.generate_monthly_qualitative``; it is (re)generated in a
    background thread on answer events, never on the request path. The cheap
    quantitative stats (counts/streaks) are NOT cached — they stay live.

    ``status`` lifecycle:
      * 'generating' — a background thread is (re)building this report.
      * 'ready'      — cached qualitative content is current and renderable.
      * 'failed'     — the last generation failed with no prior good content.

    Unique on (couple_id, year, month) so there is exactly one report per month.
    Created by ``db.create_all()`` — a brand-new table needs no ALTER migration.
    """
    __tablename__ = "monthly_reports"
    __table_args__ = (
        db.UniqueConstraint("couple_id", "year", "month", name="uq_report_couple_month"),
    )

    id = db.Column(db.Integer, primary_key=True)
    couple_id = db.Column(
        db.Integer, db.ForeignKey("couples.id"), nullable=False, index=True
    )
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)

    # ---- qualitative content (mirrors generate_monthly_qualitative's dict) ----
    summary = db.Column(db.Text, nullable=True)
    themes = db.Column(db.Text, nullable=True)  # JSON-encoded list[str]
    tone = db.Column(db.Text, nullable=True)
    divergent_question = db.Column(db.Text, nullable=True)
    fun = db.Column(db.Text, nullable=True)

    # ---- lifecycle ----
    status = db.Column(db.String(16), default="generating", nullable=False)
    generated_at = db.Column(db.DateTime, nullable=True)  # last successful build
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    @property
    def themes_list(self):
        """Decode the JSON-stored themes back to a list (defensive)."""
        if not self.themes:
            return []
        try:
            v = json.loads(self.themes)
            return v if isinstance(v, list) else []
        except (ValueError, TypeError):
            return []


class Case(db.Model):
    """A '사건' — one couple's fight brought before the AI judge.

    One partner opens a case with a 상황 설명; each partner can add their own
    진술(``CaseStatement``). A background thread then runs the slow ``claude -p``
    judge pass (never on the request path) and stores the parsed verdict here.
    Scoped to ``couple_id`` so a couple only ever sees their own cases. Created
    by ``db.create_all()`` — a brand-new table needs no ALTER migration.

    ``status`` lifecycle:
      * 'open'    — created; awaiting judgment (allows judging once ≥1 진술).
      * 'judging' — a background thread is running the AI judge.
      * 'decided' — a verdict is ready (``verdict_json`` populated).
      * 'resolved'— 두 사람이 화해로 종결(진술 수정·재판결 잠김). 새 컬럼 없이
        기존 ``status`` 문자열 값만 추가한 것 (마이그레이션 불필요).
      * 'failed'  — the AI failed AND there is no prior good verdict to keep.
    """
    __tablename__ = "cases"

    id = db.Column(db.Integer, primary_key=True)
    couple_id = db.Column(
        db.Integer, db.ForeignKey("couples.id"), nullable=False, index=True
    )
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    # A short label for the list; if empty, the list shows a situation snippet.
    title = db.Column(db.String(120), nullable=True)
    situation = db.Column(db.Text, nullable=False)  # 상황 설명
    status = db.Column(db.String(16), default="open", nullable=False)
    # The parsed AI verdict as JSON (stored via json.dumps(..., ensure_ascii=False)).
    verdict_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )
    decided_at = db.Column(db.DateTime, nullable=True)  # last successful verdict

    creator = db.relationship("User")
    statements = db.relationship(
        "CaseStatement", backref="case", cascade="all, delete-orphan"
    )

    @property
    def verdict(self):
        """Decode the stored verdict JSON back to a dict (defensive)."""
        if not self.verdict_json:
            return None
        try:
            v = json.loads(self.verdict_json)
            return v if isinstance(v, dict) else None
        except (ValueError, TypeError):
            return None


class CaseStatement(db.Model):
    """One partner's 진술 for a case. Each partner has at most one (upsert)."""
    __tablename__ = "case_statements"
    __table_args__ = (
        db.UniqueConstraint("case_id", "user_id", name="uq_case_user"),
    )

    id = db.Column(db.Integer, primary_key=True)
    case_id = db.Column(
        db.Integer, db.ForeignKey("cases.id"), nullable=False, index=True
    )
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    text = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    author = db.relationship("User")


class EventItem(db.Model):
    """'데이트 뉴스' 피드 한 건 — 외부 공개 API(서울 문화행사)에서 밤마다 긁어온
    실제 전시·공연·행사. 커플에 종속되지 않는 전역 카탈로그다(모두가 같은 피드를
    본다). 명시적 id가 없는 소스라 (source, source_uid)로 dedupe하며, source_uid는
    페처가 title|start_date|place의 sha1로 계산한 안정적 키다. 만료(end_date < 오늘)
    행은 크론이 삭제한다. brand-new 테이블 — db.create_all()가 만들어 ALTER 불필요."""
    __tablename__ = "event_items"
    __table_args__ = (
        db.UniqueConstraint("source", "source_uid", name="uq_event_source_uid"),
    )

    id = db.Column(db.Integer, primary_key=True)
    source = db.Column(db.String(16), nullable=False, default="seoul")
    # 소스에 명시적 id가 없어 페처가 계산하는 안정적 dedupe 키(sha1 hex)
    source_uid = db.Column(db.String(200), nullable=False)
    title = db.Column(db.String(300), nullable=False)
    category = db.Column(db.String(80), nullable=True)
    description = db.Column(db.Text, nullable=True)
    place = db.Column(db.String(200), nullable=True)
    district = db.Column(db.String(60), nullable=True)
    image_url = db.Column(db.String(1000), nullable=True)
    link = db.Column(db.String(1000), nullable=True)
    fee = db.Column(db.String(200), nullable=True)
    is_free = db.Column(db.Boolean, nullable=True)
    start_date = db.Column(db.Date, nullable=True)
    end_date = db.Column(db.Date, nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )


class EventScore(db.Model):
    """커플별 '데이트 뉴스' 행사 AI 추천 점수+사유 (P2).

    EventItem은 전역 카탈로그지만, 점수는 커플마다 다르다 — 각자의 오늘의질문
    답변·추억 캡션·판사 사건에서 만든 취향 프로필로 배치 채점한다. 요청 경로가
    아니라 백그라운드 스레드에서 `claude`를 한 번(배치 1콜) 돌려 채운다. (couple_id,
    event_id) 유니크로 커플·행사당 한 행. brand-new 테이블 — db.create_all()가
    만들어 ALTER 불필요.

    ``status`` 라이프사이클:
      * 'pending' — 채점 대기/진행 중(UI는 "추천 분석 중").
      * 'ready'   — score+reason 채워짐(렌더 가능).
      * 'failed'  — 이번 패스에서 못 받음(무한 재시도 방지; 이후 패스가 재시도 가능).
    """
    __tablename__ = "event_scores"
    __table_args__ = (
        db.UniqueConstraint(
            "couple_id", "event_id", name="uq_eventscore_couple_event"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    couple_id = db.Column(
        db.Integer, db.ForeignKey("couples.id"), nullable=False, index=True
    )
    event_id = db.Column(
        db.Integer, db.ForeignKey("event_items.id"), nullable=False, index=True
    )
    score = db.Column(db.Integer, nullable=True)   # 0~100
    reason = db.Column(db.Text, nullable=True)     # 한 문장 사유
    status = db.Column(db.String(16), default="pending", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    # 편의용 관계(행사 → 점수 조회).
    event = db.relationship("EventItem")
