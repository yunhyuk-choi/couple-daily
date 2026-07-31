"""SQLAlchemy models for couple-daily.

Data model is intentionally small — the app serves exactly one couple (two
people) per couple-space, though the schema does not forbid several couples
existing in one database.
"""
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
