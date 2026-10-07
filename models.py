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

        # 1c) add export_key (네이버 업로더 유저스크립트 인증 토큰) if missing.
        if "export_key" not in cols:
            try:
                with engine.begin() as conn:
                    conn.execute(text("ALTER TABLE users ADD COLUMN export_key VARCHAR"))
                log.info("startup migration: added users.export_key")
            except Exception:  # noqa: BLE001
                log.exception("startup migration: failed adding users.export_key")

        # 1d) 네이버 API HUB 자격증명(키워드 조사용) — **사용자별**. 두 사람의 키가
        #     다를 수 있어 커플이 아니라 users에 둔다. 둘 다 nullable이라 기존 행은
        #     NULL(미설정)로 남고, 조사만 꺼진 채 앱은 지금처럼 그대로 돈다.
        #     Postgres·SQLite 모두 안전하고 반복 실행에 멱등하다.
        for col, ddl in (
            ("naver_api_key_id", "VARCHAR(200)"),
            ("naver_api_key", "VARCHAR(400)"),
        ):
            if col in cols:
                continue
            try:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE users ADD COLUMN {col} {ddl}"))
                log.info("startup migration: added users.%s", col)
            except Exception:  # noqa: BLE001 — 한 컬럼 실패가 나머지를 막지 않게
                log.exception("startup migration: failed adding users.%s", col)

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
            # photos.taken_at: EXIF 촬영일시 컬럼. 없을 때만 추가한다. Postgres·
            # SQLite 모두 안전하고 반복 실행에 멱등하다. 기존 행은 NULL로 남아
            # 캘린더가 created_at(업로드시각)으로 폴백한다(백필 불필요).
            if "taken_at" not in pcols:
                try:
                    with engine.begin() as conn:
                        conn.execute(
                            text("ALTER TABLE photos ADD COLUMN taken_at TIMESTAMP")
                        )
                    log.info("startup migration: added photos.taken_at")
                except Exception:  # noqa: BLE001
                    log.exception("startup migration: failed adding photos.taken_at")

        # 4) monthly_reports: 월간 '우리 리포트'의 구조화된 전문(JSON) 컬럼.
        #    monthly_reports 테이블은 프로덕션에 이미 존재하므로 create_all이
        #    ALTER하지 않는다 — 없을 때만 report_json 컬럼을 추가한다. Postgres·
        #    SQLite 모두 안전하고 반복 실행에도 멱등하다. 기존 행은 report_json이
        #    NULL로 남아 UI가 레거시 필드로 폴백한다(하위 호환).
        if "monthly_reports" in insp.get_table_names():
            mcols = {c["name"]: c for c in insp.get_columns("monthly_reports")}
            if "report_json" not in mcols:
                try:
                    with engine.begin() as conn:
                        conn.execute(
                            text(
                                "ALTER TABLE monthly_reports ADD COLUMN report_json TEXT"
                            )
                        )
                    log.info("startup migration: added monthly_reports.report_json")
                except Exception:  # noqa: BLE001
                    log.exception(
                        "startup migration: failed adding monthly_reports.report_json"
                    )

        # 5) calendar_schedules: 여러 날 일정용 end_date 컬럼(포함 종료일).
        #    calendar_schedules 테이블은 프로덕션에 이미 존재하므로 create_all이
        #    ALTER하지 않는다 — 없을 때만 추가한다. Postgres·SQLite 모두 안전하고
        #    반복 실행에 멱등하다. 기존 행은 NULL(단일 날짜 일정)로 남는다.
        if "calendar_schedules" in insp.get_table_names():
            scols = {c["name"]: c for c in insp.get_columns("calendar_schedules")}
            if "end_date" not in scols:
                try:
                    with engine.begin() as conn:
                        conn.execute(
                            text("ALTER TABLE calendar_schedules ADD COLUMN end_date DATE")
                        )
                    log.info("startup migration: added calendar_schedules.end_date")
                except Exception:  # noqa: BLE001
                    log.exception(
                        "startup migration: failed adding calendar_schedules.end_date"
                    )
            # 일정 시작 시각(선택) start_time. 없을 때만 추가한다. Postgres·SQLite
            # 모두 TIME 타입을 지원하고, 반복 실행에 멱등하다. 기존 행은 NULL(종일).
            if "start_time" not in scols:
                try:
                    with engine.begin() as conn:
                        conn.execute(
                            text(
                                "ALTER TABLE calendar_schedules ADD COLUMN start_time TIME"
                            )
                        )
                    log.info("startup migration: added calendar_schedules.start_time")
                except Exception:  # noqa: BLE001
                    log.exception(
                        "startup migration: failed adding calendar_schedules.start_time"
                    )

        # 6) bets: 내기 종료일(마감일) end_date 컬럼. bets 테이블은 프로덕션에 이미
        #    존재하므로 없을 때만 추가한다. Postgres·SQLite 모두 안전하고 멱등하다.
        #    기존 행은 NULL(무기한)로 남는다.
        if "bets" in insp.get_table_names():
            bcols = {c["name"]: c for c in insp.get_columns("bets")}
            if "end_date" not in bcols:
                try:
                    with engine.begin() as conn:
                        conn.execute(text("ALTER TABLE bets ADD COLUMN end_date DATE"))
                    log.info("startup migration: added bets.end_date")
                except Exception:  # noqa: BLE001
                    log.exception("startup migration: failed adding bets.end_date")

        # 7) blog_reviews: v2 '재료' 칸 8개. blog_reviews 테이블은 프로덕션에 이미
        #    존재하므로 create_all이 ALTER하지 않는다 — 없는 컬럼만 추가한다.
        #    전부 nullable이라 **기존 후기는 그대로 NULL로 남고** 화면·재생성 모두
        #    문제없다(값이 없으면 프롬프트에서 그 줄이 통째로 빠질 뿐). Postgres·
        #    SQLite 모두 안전하고 반복 실행에 멱등하다.
        if "blog_reviews" in insp.get_table_names():
            rcols = {c["name"] for c in insp.get_columns("blog_reviews")}
            for col, ddl in (
                ("visited_when", "VARCHAR(100)"),
                ("visit_reason", "VARCHAR(300)"),
                ("access_note", "VARCHAR(300)"),
                ("waiting", "VARCHAR(200)"),
                ("order_items", "TEXT"),
                ("highlight", "VARCHAR(300)"),
                ("downside", "VARCHAR(300)"),
                ("researched", "TEXT"),
                # Step 3 키워드 조사 메모(JSON). 없으면 NULL — 조사 없이 만든
                # 옛 후기는 메모 카드가 안 그려질 뿐 전부 그대로 동작한다.
                ("research_json", "TEXT"),
                # Step 4 썸네일 상태(JSON). 없으면 NULL — 썸네일을 안 만든 후기는
                # 카드에 "만들기" 버튼만 보인다.
                ("thumbnail_json", "TEXT"),
                # Step 5 동영상→GIF 상태(JSON). 없으면 NULL — GIF를 안 만든 후기는
                # 카드에 "영상 올리기"만 보인다. (``videos`` 테이블 자체는 brand-new라
                # create_all()이 만든다 — ALTER가 필요한 건 이 컬럼 하나뿐이다.)
                ("gif_json", "TEXT"),
            ):
                if col in rcols:
                    continue
                try:
                    with engine.begin() as conn:
                        conn.execute(
                            text(f"ALTER TABLE blog_reviews ADD COLUMN {col} {ddl}")
                        )
                    log.info("startup migration: added blog_reviews.%s", col)
                except Exception:  # noqa: BLE001 — 한 컬럼 실패가 나머지를 막지 않게
                    log.exception(
                        "startup migration: failed adding blog_reviews.%s", col
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
    # 네이버 업로더 유저스크립트(공개 pending API) 인증용 개인 토큰. 없으면 최초
    # 접근 시 생성한다. URL-safe(~24자). 추가는 run_startup_migrations가 ALTER로 반영.
    export_key = db.Column(db.String(32), unique=True, nullable=True, index=True)
    # --- 네이버 API HUB 자격증명 (키워드 조사용, 선택) ------------------------
    # **사용자별**로 둔다 — 두 사람이 각자 발급받은 키를 쓸 수 있어야 한다.
    #
    # 저장 위치·방식의 근거:
    #   * 브라우저(localStorage)에 두지 않는다 — 평문으로 남고 XSS에 그대로 노출된다.
    #     이 앱은 로그인 세션이 있으니 서버에 둔다.
    #   * 컬럼 값 자체는 이 레포의 **기존 비밀 취급 방식과 같다** — OneDrive refresh
    #     token(`settings.onedrive_refresh_token`)도 DB 평문이고, 앱에 암호화 계층이
    #     없다. 새 비밀 하나만 암호화하면 더 센 비밀이 평문인 채로 남아 실효는 없이
    #     키 관리(SECRET_KEY 교체 시 복호 불가) 위험만 는다. 암호화를 한다면 모든
    #     비밀에 한 번에 적용하는 별도 작업이어야 한다.
    #   * 실질적인 노출 차단은 **값을 다시 내보내지 않는 것**으로 한다: 화면에는
    #     '설정됨/미설정'만 그리고, 폼은 쓰기 전용이며, 로그·에러 메시지·조사 메모
    #     어디에도 값이 들어가지 않는다(`naver_api.NaverApiError` 참고).
    naver_api_key_id = db.Column(db.String(200), nullable=True)
    naver_api_key = db.Column(db.String(400), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    answers = db.relationship("Answer", backref="user", lazy="dynamic")

    @property
    def has_naver_api_keys(self):
        """키워드 조사를 쓸 수 있는가(값은 돌려주지 않는다 — 화면용 불리언)."""
        return bool((self.naver_api_key_id or "").strip()
                    and (self.naver_api_key or "").strip())

    @property
    def naver_api_credentials(self):
        """``(client_id, client_secret)`` 또는 ``(None, None)``. 서버 내부 전용."""
        if not self.has_naver_api_keys:
            return (None, None)
        return (self.naver_api_key_id.strip(), self.naver_api_key.strip())

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
    # EXIF 촬영일시(DateTimeOriginal 등). null = 알 수 없음 → 캘린더는 업로드시각
    # (created_at)으로 폴백한다. 업로드 시 exifutil.extract_taken_at으로 채운다.
    taken_at = db.Column(db.DateTime, nullable=True)
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


class AiJob(db.Model):
    """AI 작업 큐 한 줄 — **할 일**이지 역사가 아니다(끝나면 지운다).

    왜 DB 인가: 데몬 스레드는 프로세스가 죽으면 같이 죽지만 '아직 안 끝난 일'은
    남아야 한다. 이 행이 그 기억이다 — 다음 부팅의 펌프가 **아무도 화면을 열지
    않아도** 이어서 한다. 규율·구조는 ``aijobs.py`` 머리말이 정본.

    ``job_key`` 는 '무엇에 대한 일인가'의 유일 키다(``"review:12"`` ·
    ``"monthly:3:2026:10"``). 같은 대상에 대한 중복 요청은 이 유니크 제약에서
    한 줄로 합쳐진다 — 더블탭·여러 화면 동시 방문이 claude 를 두 번 부르지 않는다.

    ⛔ ``payload_json`` 에는 **참조만** 담는다(사진 id·후기 id 같은 작은 값).
    이미지 바이트는 절대 넣지 않는다 — 업로드 경로가 이미 스트리밍이라 '메모리에
    들고 차례를 기다리는' 구간이 없고(실측: 41.5MB 사진 피크 6.5MB), 넣는 순간
    없던 비용이 생긴다. ``aijobs.MAX_PAYLOAD_BYTES`` 가 코드로 막는다.

    브랜드-뉴 테이블이라 ``db.create_all()`` 이 만든다 — ALTER 마이그레이션 없음.
    """
    __tablename__ = "ai_jobs"

    id = db.Column(db.Integer, primary_key=True)  # 오름차순 = 도착순 = 처리 순서
    kind = db.Column(db.String(32), nullable=False, index=True)
    job_key = db.Column(db.String(128), nullable=False, unique=True, index=True)
    payload_json = db.Column(db.Text, nullable=True)
    couple_id = db.Column(db.Integer, nullable=True, index=True)
    # 'queued' | 'running'. 'done' 은 없다 — 끝난 행은 지운다.
    status = db.Column(db.String(16), nullable=False, default="queued", index=True)
    # 지금 이 행을 쥔 프로세스 토큰. 다른 토큰이면 그 프로세스는 죽은 것이다.
    owner = db.Column(db.String(64), nullable=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    started_at = db.Column(db.DateTime, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class PhotoPreview(db.Model):
    """**우리가 소유하는** 미리보기 자산 한 칸 — 사진 × 티어.

    갤러리 그리드·크롭 UI·라이트박스가 쓰는 작은 그림이다. Graph 가 한 번 만들어
    준 바이트를 **여기에 영구 보관**해서, 두 번째부터는 화면이 **DB 읽기 + 리페인트**
    말고 아무것도 하지 않게 한다. 자세한 근거(왜 캐시가 아니라 자산인지, 왜 남의
    URL 수명에 걸지 않는지)는 ``previews.py`` 머리말이 정본이다.

    ⚠️ 이건 **캐시가 아니다.** Render 의 임시 디스크는 재배포마다 비고, 프로세스
    메모리는 재시작마다 빈다 — 둘 중 어디에 둬도 '배포할 때마다 전부 다시 받기'가
    돌아온다. 그래서 DB 행이다(재시작·재배포에 안전).

    브랜드-뉴 테이블이라 ``db.create_all()`` 이 만든다 — ALTER 마이그레이션 없음.
    """
    __tablename__ = "photo_previews"
    __table_args__ = (
        db.UniqueConstraint("photo_id", "tier", name="uq_photo_preview_tier"),
    )

    id = db.Column(db.Integer, primary_key=True)
    photo_id = db.Column(
        db.Integer, db.ForeignKey("photos.id"), nullable=False, index=True
    )
    # 'grid'(목록용 작은 것) / 'view'(크롭 UI·라이트박스용). previews.TIERS 가 정본.
    tier = db.Column(db.String(16), nullable=False)
    # 자산 바이트 그대로. Postgres 에서는 BYTEA.
    data = db.Column(db.LargeBinary, nullable=False)
    content_type = db.Column(db.String(64), nullable=True)
    # 알 때만 채운다(렌디션은 가로세로를 알려 주고, 이름 있는 썸네일은 안 알려 준다).
    # 목록이 <img width height> 를 박아 레이아웃 흔들림을 없애는 데 쓴다.
    width = db.Column(db.Integer, nullable=True)
    height = db.Column(db.Integer, nullable=True)
    byte_len = db.Column(db.Integer, nullable=True)
    # 무엇으로 만들었는지(진단용): 'rendition:c384' / 'thumbnail:medium' 등.
    source = db.Column(db.String(32), nullable=True)
    # 조건부 요청(304)용. 내용 해시라 같은 바이트면 항상 같다.
    etag = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class Video(db.Model):
    """커플이 올린 동영상 한 건 — 바이트는 OneDrive(``couple-daily`` 폴더)에 있다.

    ``Photo`` 와 **같은 구조**다(OneDrive item id + 가벼운 메타만, 커플 스코프).
    사진과 테이블을 나눈 이유는 두 가지다:

    * 사진 경로에는 자동 캡션·EXIF·블로그 서명 URL·썸네일 프록시가 줄줄이 달려 있다.
      동영상은 그중 어느 것도 타면 안 된다(vision 캡셔너에 50MB 영상을 먹이면
      0.1 CPU 워커가 죽는다).
    * 추억 갤러리는 사진 그리드다. 영상이 섞여 들어가면 ``<img>`` 가 깨진다.

    브랜드-뉴 테이블이라 ``db.create_all()`` 이 만든다 — ALTER 마이그레이션 없음
    (``blog_reviews`` · ``photos`` 가 처음 들어올 때와 같은 방식).

    ``cors_probe`` 는 **실측 기록**이다 — 이 영상의 OneDrive 직접 다운로드 URL을
    브라우저가 Canvas 로 읽을 수 있는지(ACAO 유무)를 서버가 한 번 재 본 결과를
    JSON 으로 남긴다. 후보 (a)(CDN 직접 스트리밍)가 가능한 환경이면 그 사실이
    주장이 아니라 **데이터로** 남게 하려는 칸이다. ⚠️ 다운로드 URL 자체는 쿼리에
    토큰이 박힌 자격증명이라 **여기 절대 저장하지 않는다**(판정과 헤더 값만).
    """
    __tablename__ = "videos"

    id = db.Column(db.Integer, primary_key=True)
    couple_id = db.Column(
        db.Integer, db.ForeignKey("couples.id"), nullable=False, index=True
    )
    # OneDrive drive-item id (opaque). 스트리밍/삭제의 손잡이.
    onedrive_item_id = db.Column(db.String(255), nullable=False)
    # OneDrive에 실제로 저장된 이름(정규화·유니크).
    filename = db.Column(db.String(255), nullable=False)
    # 사용자가 올린 원래 파일명(표시용).
    original_name = db.Column(db.String(255), nullable=True)
    uploaded_by = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    # 바이트 수 · 선언된 MIME. Range 응답과 화면 표시에 쓴다.
    size_bytes = db.Column(db.Integer, nullable=False, default=0)
    content_type = db.Column(db.String(100), nullable=True)
    # 직접-스트리밍 가능 여부 실측 결과(JSON). NULL = 아직 재 보지 않음.
    cors_probe = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    uploader = db.relationship("User")

    @property
    def cors_probe_data(self):
        """``cors_probe`` 를 dict로 디코드(없거나 깨졌으면 None)."""
        if not self.cors_probe:
            return None
        try:
            v = json.loads(self.cors_probe)
            return v if isinstance(v, dict) else None
        except (ValueError, TypeError):
            return None


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

    # ---- 월간 '우리 리포트' 구조화 전문(JSON) — 새 크로스도메인 리포트 전체를
    #      담는다. 위 레거시 컬럼은 하위 호환/폴백용으로 계속 채운다. 값이 NULL이면
    #      (과거 캐시된 리포트) UI는 레거시 필드로 렌더한다. ----
    report_json = db.Column(db.Text, nullable=True)

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

    @property
    def report(self):
        """report_json을 구조화 dict로 디코드(없거나 깨졌으면 None)."""
        if not self.report_json:
            return None
        try:
            v = json.loads(self.report_json)
            return v if isinstance(v, dict) else None
        except (ValueError, TypeError):
            return None


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


class EventPick(db.Model):
    """'데이트 뉴스' 행사에 대한 한 사용자의 찜/방문함/관심없음 상태 (P3).

    EventItem은 전역 카탈로그, EventScore는 커플별 점수인 것과 달리, 이 행은
    *사용자별* 반응이다. (event_id, user_id) 유니크로 사용자·행사당 한 행(상태
    변경은 upsert). couple_id도 들고 있어 커플 단위 조회(공유 찜 목록·확정 판정)를
    한 방에 한다.

    ``status``:
      * 'interested' — 찜(가고 싶음). 두 파트너가 모두 interested면 '확정'(파생, 별도
        컬럼 없음 — interested 서로 다른 user 수 == 2).
      * 'visited'    — 다녀옴(그 사용자 피드에서 숨김).
      * 'dismissed'  — 관심없음(그 사용자 피드에서 숨김).

    brand-new 테이블 — db.create_all()가 만들어 ALTER 불필요."""
    __tablename__ = "event_picks"
    __table_args__ = (
        db.UniqueConstraint("event_id", "user_id", name="uq_eventpick_event_user"),
    )

    id = db.Column(db.Integer, primary_key=True)
    couple_id = db.Column(
        db.Integer, db.ForeignKey("couples.id"), nullable=False, index=True
    )
    event_id = db.Column(
        db.Integer, db.ForeignKey("event_items.id"), nullable=False, index=True
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=False, index=True
    )
    status = db.Column(db.String(16), default="interested", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    # 편의용 관계(찜 → 사용자·행사 조회).
    user = db.relationship("User")
    event = db.relationship("EventItem")


class DateRecommendation(db.Model):
    """커플별 '추천받기' 최신 결과 (P4) — 온디맨드 비동기 AI 데이트 추천.

    사용자가 피드에서 '✨ 추천받기'를 누르면 백그라운드 스레드가 이 커플의 취향
    프로필 + 현재 피드(서울 행사 + 팝업 포함) 상위 후보로 `claude`를 '한 번'
    돌려(요청 경로가 아님·_CAPTION_SEM으로 직렬화) 따뜻한 추천 문구 + 2~3개 픽을
    만든다. couple_id 유니크라 커플당 '최신 하나'만 upsert한다. brand-new 테이블 —
    db.create_all()가 만들어 ALTER 불필요.

    ``status`` 라이프사이클:
      * 'pending' — 추천 생성 대기/진행 중(UI "추천 뽑는 중").
      * 'ready'   — message + picks_json 채워짐(패널 렌더 가능).
      * 'failed'  — 이번 생성 실패 & 직전 ready 결과도 없음.
    """
    __tablename__ = "date_recommendations"

    id = db.Column(db.Integer, primary_key=True)
    # 커플당 최신 추천 하나만 — 유니크로 upsert한다.
    couple_id = db.Column(
        db.Integer,
        db.ForeignKey("couples.id"),
        nullable=False,
        unique=True,
        index=True,
    )
    status = db.Column(db.String(16), default="pending", nullable=False)
    message = db.Column(db.Text, nullable=True)       # 따뜻한 추천 문단
    picks_json = db.Column(db.Text, nullable=True)    # JSON list[{event_id, why}]
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    @property
    def picks(self):
        """picks_json을 리스트로 디코드(없거나 깨졌으면 [])."""
        if not self.picks_json:
            return []
        try:
            v = json.loads(self.picks_json)
            return v if isinstance(v, list) else []
        except (ValueError, TypeError):
            return []


class Schedule(db.Model):
    """커플 캘린더의 '일정' 한 건 — 특정 날짜에 잡은 계획.

    사진(Photo)이 '지난 날의 기록'이라면 일정은 '앞으로/특정 날의 계획'이다. 한
    날짜에 사진과 일정이 함께 있을 수 있다. ``event_id``가 채워진 일정은 '데이트
    뉴스' 확정 행사에서 '날짜 정하기'로 만든 것(선택적 역참조). couple_id로 커플
    스코프. brand-new 테이블 — db.create_all()가 만들어 ALTER 불필요."""
    __tablename__ = "calendar_schedules"

    id = db.Column(db.Integer, primary_key=True)
    couple_id = db.Column(
        db.Integer, db.ForeignKey("couples.id"), nullable=False, index=True
    )
    date = db.Column(db.Date, nullable=False, index=True)
    # 여러 날 일정의 종료일(포함). NULL이면 단일 날짜 일정(=date 하루).
    # 값이 있으면 일정은 [date, end_date] 구간을 덮는다.
    end_date = db.Column(db.Date, nullable=True)
    # 일정 시작 시각(선택). NULL이면 '종일'(시간 미지정). 값이 있으면 그 시각에 시작.
    start_time = db.Column(db.Time, nullable=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    # '데이트 뉴스' 확정 행사에서 만들었으면 그 행사 id(선택적). 일반 일정은 NULL.
    event_id = db.Column(
        db.Integer, db.ForeignKey("event_items.id"), nullable=True, index=True
    )
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    creator = db.relationship("User")
    event = db.relationship("EventItem")

    @property
    def time_label(self):
        """시작 시각을 '오전/오후 h:MM' 한국어로. 시간 미지정이면 ''(종일)."""
        t = self.start_time
        if t is None:
            return ""
        ampm = "오전" if t.hour < 12 else "오후"
        h12 = t.hour % 12 or 12
        return f"{ampm} {h12}:{t.minute:02d}"


class Bet(db.Model):
    """커플 '내기' 한 건 — 캘린더 PHASE 2의 게임화 요소.

    두 종류를 담는 단일 테이블이다(2a=habit만 구현, 2b=prediction은 컬럼만 미리):
      * 'habit'      — '일주일에 N번 <활동> 하기' 습관 내기. 주(week)는 달력 주가
        아니라 ``start_date``를 기점으로 7일씩 굴러가는 *롤링 윈도우*다. 대상
        (``target_user_id``)이 NULL이면 둘 다(같이), 아니면 그 한 명만 참여한다.
      * 'prediction' — (2b, 아직 미구현) '언제 ~할까?' 날짜 맞히기 내기.
        ``guess_a_date``/``guess_b_date``/``actual_date``/``winner`` 컬럼은 2b가
        마이그레이션 없이 바로 쓰도록 지금 nullable로 추가만 해 둔다(2a 미사용).

    couple_id로 커플 스코프. brand-new 테이블 — db.create_all()가 만들어 ALTER 불필요."""
    __tablename__ = "bets"

    id = db.Column(db.Integer, primary_key=True)
    couple_id = db.Column(
        db.Integer, db.ForeignKey("couples.id"), nullable=False, index=True
    )
    # 'habit' | 'prediction' (2a는 habit만 생성)
    type = db.Column(db.String(16), nullable=False, default="habit")
    # 습관 활동명(예: "하루 한 편 시 쓰기") / 예측 질문
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    # NULL = 같이(둘 다) · 값이 있으면 그 한 명만 대상
    target_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True
    )
    # 롤링 주 기준점(달력 주가 아니라 이 날짜 + 7*k 로 굴러간다)
    start_date = db.Column(db.Date, nullable=False)
    # 선택적 종료일(마감일). NULL이면 무기한(ongoing). 값이 있고 오늘보다 과거면
    # '마감'으로 간주해 더 이상 체크인을 받지 않는다(표시 레벨 판정).
    end_date = db.Column(db.Date, nullable=True)
    # habit: 주 N회 목표
    count_target = db.Column(db.Integer, nullable=True)
    penalty = db.Column(db.Text, nullable=True)  # 벌칙
    # 'active' | 'ended'
    status = db.Column(db.String(16), nullable=False, default="active")
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)

    # ---- 2b(prediction) 전용 컬럼 — 지금은 미사용, 마이그레이션 회피용 선반영 ----
    guess_a_date = db.Column(db.Date, nullable=True)
    guess_b_date = db.Column(db.Date, nullable=True)
    actual_date = db.Column(db.Date, nullable=True)
    winner = db.Column(db.String(8), nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    target_user = db.relationship("User", foreign_keys=[target_user_id])
    creator = db.relationship("User", foreign_keys=[created_by])
    checkins = db.relationship(
        "BetCheckin", backref="bet", cascade="all, delete-orphan"
    )


class BetCheckin(db.Model):
    """습관 내기의 '오늘 달성' 체크인 한 건 — (내기, 사용자, 날짜)당 최대 하나.

    롤링 주 안에서의 체크인 개수가 그 주의 달성 카운트가 된다. 유니크 제약으로
    같은 날 중복 체크인을 막고, 토글(생성/삭제)로 오늘 상태를 뒤집는다."""
    __tablename__ = "bet_checkins"
    __table_args__ = (
        db.UniqueConstraint("bet_id", "user_id", "date", name="uq_betcheckin"),
    )

    id = db.Column(db.Integer, primary_key=True)
    bet_id = db.Column(
        db.Integer, db.ForeignKey("bets.id"), nullable=False, index=True
    )
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    date = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User")


class BlogReview(db.Model):
    """'데이트 후기 → 네이버 블로그 포스트' 한 건 (P1: 데이터+작성 UX).

    커플이 다녀온 데이트를 주제/장소 · 위치 · 느낀점(산문) · 반쪽별 별점(0~10) ·
    추억 사진(순서 유지)으로 남긴다. P1은 여기까지 저장만 한다(status='draft').
    P2가 이 원문을 재료로 백그라운드 `claude`로 SEO/AEO 블로그 포스트를 만들어
    ``ai_json``에 담고, 사용자가 편집한 복사텍스트를 ``edited_text``에 담는다.
    커플 스코프(``couple_id``). brand-new 테이블 — db.create_all()가 만들어 ALTER
    마이그레이션이 필요 없다.

    ``status`` 라이프사이클:
      * 'draft'   — 생성됨, AI 아직 안 돌림(P1의 유일한 상태).
      * 'pending' — (P2) 백그라운드 생성 대기/진행 중.
      * 'ready'   — (P2) ai_json 채워짐(미리보기 렌더 가능).
      * 'failed'  — (P2) 생성 실패.
    """
    __tablename__ = "blog_reviews"

    id = db.Column(db.Integer, primary_key=True)
    couple_id = db.Column(
        db.Integer, db.ForeignKey("couples.id"), nullable=False, index=True
    )
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    # 주제/장소명(필수)
    topic = db.Column(db.String(200), nullable=False)
    # 위치(선택) — 주소·동네 등
    location = db.Column(db.String(300), nullable=True)
    # 자유 서술 원문(필수) — 사람 말투의 원천. v2에서 '3~4줄' 가이드를 걷어내고
    # 길게 받는다(짧은 산문으로 긴 글을 만들면 AI가 지어낸다).
    prose = db.Column(db.Text, nullable=False)
    # --- v2 '재료' 칸 (전부 선택 입력) --------------------------------------
    # 레퍼런스 맛집 글(조회수가 더 높은 쪽) 5편을 역산해 뽑은 정보 조각들. 짧은
    # 산문만으로는 구체적인 수치(대기 분·팀 수·인분·가격)가 안 나와서 AI가 지어내던
    # 자리를, 사람이 아는 만큼만 채우게 한다. **모르면 비워 둔다** — 빈칸이
    # 지어내기보다 낫다. 각 칸의 근거는 prompts/blog-review.md·docs/blog-review-spec.md.
    visited_when = db.Column(db.String(100), nullable=True)   # 언제(요일·시간대)
    visit_reason = db.Column(db.String(300), nullable=True)   # 왜 갔는지(도입 단락)
    access_note = db.Column(db.String(300), nullable=True)    # 찾아가는 길·자리·주차
    waiting = db.Column(db.String(200), nullable=True)        # 웨이팅·입장 방식
    order_items = db.Column(db.Text, nullable=True)           # 주문·가격·총액
    highlight = db.Column(db.String(300), nullable=True)      # 가장 기억에 남은 것
    downside = db.Column(db.String(300), nullable=True)       # 아쉬운 점
    # 검색해서 확인한 공개 정보(영업시간·휴무·주소·주차). 경험과 **어미가 다르다**
    # (~것으로 안내돼 있어요) — 그래서 자유 서술과 칸을 분리한다.
    researched = db.Column(db.Text, nullable=True)
    # 반쪽별 별점 합계(0~10). 별 5개 × 2점, 홀수는 반쪽.
    overall_score = db.Column(db.Integer, nullable=False)
    # 선택한 Photo id들을 JSON list[int]로, **순서 유지**해 저장.
    photo_ids = db.Column(db.Text, nullable=True)
    # (P2) AI가 만든 구조화 포스트 JSON.
    ai_json = db.Column(db.Text, nullable=True)
    # (P2) 사용자가 편집한 네이버 복사텍스트.
    edited_text = db.Column(db.Text, nullable=True)
    # (Step 3) 키워드 조사 메모 JSON — 후보·근거·선정 이유·데이터 한계. 키가 없으면
    # {"status":"skipped"}로 남고 상세 화면이 "키를 넣으면 켜진다"를 안내한다.
    research_json = db.Column(db.Text, nullable=True)
    # (Step 4) 썸네일 상태 JSON — 카피 후보 3개·선정·사람이 줄인 최종 문구·쓸 사진·
    # 브라우저가 보고한 렌더 검증(좌표·폰트·넘침). **픽셀은 여기 안 들어간다** —
    # PNG는 사용자의 브라우저가 그려 바로 내려받는다(서버엔 Chromium이 없다).
    thumbnail_json = db.Column(db.Text, nullable=True)
    # (Step 5) 동영상→GIF 상태 JSON — 고른 영상·구간(시작/길이)·프리셋(가로폭/fps)·
    # 브라우저가 보고한 결과(용량·해상도·프레임 수·디코딩 경로)와 진단 코드.
    # **픽셀은 여기 안 들어간다** — GIF도 사용자의 브라우저가 만들어 바로 내려받는다
    # (서버엔 ffmpeg가 없다 — gifmaker.py 머리말).
    gif_json = db.Column(db.Text, nullable=True)
    status = db.Column(db.String(16), default="draft", nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )

    creator = db.relationship("User")

    @property
    def photo_ids_list(self):
        """photo_ids(JSON)를 int 리스트로 디코드(없거나 깨졌으면 [])."""
        if not self.photo_ids:
            return []
        try:
            v = json.loads(self.photo_ids)
        except (ValueError, TypeError):
            return []
        if not isinstance(v, list):
            return []
        out = []
        for x in v:
            try:
                out.append(int(x))
            except (ValueError, TypeError):
                continue
        return out

    @property
    def photos_ordered(self):
        """photo_ids 순서대로 이 커플의 Photo 행을 돌려준다.

        JSON 디코드 → 한 번의 IN 조회 → photo_ids 순서로 재정렬 → 커플 밖이거나
        사라진 id는 스킵. (커플 스코프를 여기서 다시 강제해 안전하게.)
        """
        ids = self.photo_ids_list
        if not ids:
            return []
        rows = (
            Photo.query.filter(
                Photo.id.in_(ids), Photo.couple_id == self.couple_id
            ).all()
        )
        by_id = {p.id: p for p in rows}
        return [by_id[i] for i in ids if i in by_id]

    # v2 '재료' 칸 이름 — 모델이 단일 원천이고, 폼·프롬프트가 이 순서를 따른다.
    DETAIL_FIELDS = (
        "visited_when",
        "visit_reason",
        "access_note",
        "waiting",
        "order_items",
        "highlight",
        "downside",
        "researched",
    )

    @property
    def details(self):
        """v2 재료 칸을 ``{필드: 값}`` dict로(빈 칸은 빼고). ``ai.write_review``의
        ``details`` 인자이자 폼 재렌더의 입력이다. 옛 행(컬럼이 NULL)이면 ``{}``."""
        out = {}
        for f in self.DETAIL_FIELDS:
            v = (getattr(self, f, None) or "").strip()
            if v:
                out[f] = v
        return out

    @property
    def ai(self):
        """ai_json을 구조화 dict로 디코드(없거나 깨졌으면 None) — P2용."""
        if not self.ai_json:
            return None
        try:
            v = json.loads(self.ai_json)
            return v if isinstance(v, dict) else None
        except (ValueError, TypeError):
            return None

    @property
    def research(self):
        """research_json을 dict로 디코드(없거나 깨졌으면 None) — 조사 메모 카드용."""
        if not self.research_json:
            return None
        try:
            v = json.loads(self.research_json)
            return v if isinstance(v, dict) else None
        except (ValueError, TypeError):
            return None

    @property
    def thumbnail(self):
        """thumbnail_json을 dict로 디코드(없거나 깨졌으면 None) — 썸네일 카드용."""
        if not self.thumbnail_json:
            return None
        try:
            v = json.loads(self.thumbnail_json)
            return v if isinstance(v, dict) else None
        except (ValueError, TypeError):
            return None

    @property
    def gif(self):
        """gif_json을 dict로 디코드(없거나 깨졌으면 None) — 동영상→GIF 카드용.

        ``thumbnail`` 과 **같은 모양**이다. 픽셀은 여기 안 들어간다 — GIF 는 사용자의
        브라우저가 만들어 바로 내려받고, 서버엔 고른 구간·프리셋·브라우저가 보고한
        결과(용량·프레임 수)와 진단만 남는다(`gifmaker.py` 머리말).
        """
        if not self.gif_json:
            return None
        try:
            v = json.loads(self.gif_json)
            return v if isinstance(v, dict) else None
        except (ValueError, TypeError):
            return None
