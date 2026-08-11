"""Quantitative monthly metrics, computed in code from the DB.

Deliberately NO love score / compatibility percentage — just honest counts and
trends grounded in real activity.
"""
from calendar import monthrange
from datetime import date, datetime

from models import (
    DailyQuestion,
    Answer,
    User,
    Photo,
    Case,
    EventItem,
    EventPick,
    db,
)


def _month_bounds(year, month):
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    return start, end


def compute_monthly_stats(couple_id, year, month, user_a, user_b):
    """Return a dict of quantitative stats for the given couple + month.

    user_a is treated as "me" for the who-answers-first framing; both names are
    included so the caller can render neutrally.

    Defensive: if ``user_b`` is None (partner hasn't joined yet), return a safe,
    neutral stats dict rather than raising — no caller can crash on a missing
    partner.
    """
    if user_b is None:
        return {
            "month_label": f"{year}년 {month}월",
            "total_days": 0,
            "both_answered_days": 0,
            "participation_a": {"name": user_a.display_name, "days": 0},
            "participation_b": {"name": "파트너", "days": 0},
            "best_streak": 0,
            "first_counts": {user_a.display_name: 0, "파트너": 0},
            "first_answerer": "tie",
            "avg_len_a": {"name": user_a.display_name, "avg": 0, "trend": "n/a"},
            "avg_len_b": {"name": "파트너", "avg": 0, "trend": "n/a"},
        }
    start, end = _month_bounds(year, month)
    questions = (
        DailyQuestion.query.filter(
            DailyQuestion.couple_id == couple_id,
            DailyQuestion.q_date >= start,
            DailyQuestion.q_date <= end,
        )
        .order_by(DailyQuestion.q_date.asc())
        .all()
    )

    total_days = len(questions)
    both_answered_days = 0
    days_a = 0
    days_b = 0
    first_a = 0  # a answered before b
    first_b = 0
    lengths_a = []
    lengths_b = []
    streak_dates = []  # dates where both answered, for streak calc

    for q in questions:
        ans = {a.user_id: a for a in q.answers.all()}
        a_ans = ans.get(user_a.id)
        b_ans = ans.get(user_b.id)
        if a_ans:
            days_a += 1
            lengths_a.append(len(a_ans.text.strip()))
        if b_ans:
            days_b += 1
            lengths_b.append(len(b_ans.text.strip()))
        if a_ans and b_ans:
            both_answered_days += 1
            streak_dates.append(q.q_date)
            if a_ans.created_at <= b_ans.created_at:
                first_a += 1
            else:
                first_b += 1

    # Best streak of consecutive calendar days where BOTH answered.
    best_streak = _best_consecutive_streak(streak_dates)

    # Average answer length trend: first half vs second half of the month.
    trend_a = _length_trend(questions, user_a.id)
    trend_b = _length_trend(questions, user_b.id)

    # Who tends to answer first
    if first_a == first_b:
        first_answerer = "tie"
    else:
        first_answerer = user_a.display_name if first_a > first_b else user_b.display_name

    return {
        "month_label": f"{year}년 {month}월",
        "total_days": total_days,
        "both_answered_days": both_answered_days,
        "participation_a": {"name": user_a.display_name, "days": days_a},
        "participation_b": {"name": user_b.display_name, "days": days_b},
        "best_streak": best_streak,
        "first_counts": {
            user_a.display_name: first_a,
            user_b.display_name: first_b,
        },
        "first_answerer": first_answerer,
        "avg_len_a": {
            "name": user_a.display_name,
            "avg": round(sum(lengths_a) / len(lengths_a), 1) if lengths_a else 0,
            "trend": trend_a,
        },
        "avg_len_b": {
            "name": user_b.display_name,
            "avg": round(sum(lengths_b) / len(lengths_b), 1) if lengths_b else 0,
            "trend": trend_b,
        },
    }


def _best_consecutive_streak(dates):
    if not dates:
        return 0
    dates = sorted(set(dates))
    best = cur = 1
    for i in range(1, len(dates)):
        if (dates[i] - dates[i - 1]).days == 1:
            cur += 1
            best = max(best, cur)
        else:
            cur = 1
    return best


def _length_trend(questions, user_id):
    """Compare avg answer length in first vs second half of the month.

    Returns 'up' | 'down' | 'flat' | 'n/a'.
    """
    lengths = []
    for q in questions:
        a = q.answer_by(user_id)
        if a:
            lengths.append(len(a.text.strip()))
    if len(lengths) < 4:
        return "n/a"
    mid = len(lengths) // 2
    first = sum(lengths[:mid]) / mid
    second = sum(lengths[mid:]) / (len(lengths) - mid)
    if second > first * 1.15:
        return "up"
    if second < first * 0.85:
        return "down"
    return "flat"


def collect_month_qa(couple_id, year, month, user_a, user_b):
    """Collect the month's Q&A (both answered OR partial) for the qualitative pass.

    Defensive: if ``user_b`` is None (no partner yet), return an empty list —
    there is no couple conversation to summarize.
    """
    if user_b is None:
        return []
    start, end = _month_bounds(year, month)
    questions = (
        DailyQuestion.query.filter(
            DailyQuestion.couple_id == couple_id,
            DailyQuestion.q_date >= start,
            DailyQuestion.q_date <= end,
        )
        .order_by(DailyQuestion.q_date.asc())
        .all()
    )
    items = []
    for q in questions:
        a = q.answer_by(user_a.id)
        b = q.answer_by(user_b.id)
        if not (a or b):
            continue
        items.append(
            {
                "date": q.q_date.isoformat(),
                "question": q.text,
                "a": a.text if a else None,
                "b": b.text if b else None,
            }
        )
    return items


def _month_dt_bounds(year, month):
    """월의 [시작, 다음달 시작) DateTime 경계 — created_at/updated_at 비교용."""
    start, _end = _month_bounds(year, month)
    start_dt = datetime(start.year, start.month, start.day)
    if month == 12:
        next_start = datetime(year + 1, 1, 1)
    else:
        next_start = datetime(year, month + 1, 1)
    return start_dt, next_start


def collect_month_context(couple_id, year, month, user_a, user_b):
    """한 달치 크로스도메인 컨텍스트를 모은다(비-AI, 전부 저렴/경계 제한).

    반환: {qa, photos, cases, dates}. 각 소스는 독립 try/except로 감싸 하나가
    실패해도 전체는 깨지지 않고 빈 값으로 떨어진다. 모든 쿼리는 couple 범위.
    """
    start_dt, next_start = _month_dt_bounds(year, month)

    # Q&A는 기존 수집기를 재사용(파트너 없으면 [] 반환).
    qa = collect_month_qa(couple_id, year, month, user_a, user_b)

    # 사진: 이번 달 생성 + 캡션 완료(ready)된 것의 캡션 최대 ~15개.
    photos = {"count": 0, "captions": []}
    try:
        rows = (
            Photo.query.filter(
                Photo.couple_id == couple_id,
                Photo.caption_status == "ready",
                Photo.created_at >= start_dt,
                Photo.created_at < next_start,
            )
            .order_by(Photo.created_at.asc())
            .all()
        )
        photos["count"] = len(rows)
        caps = []
        for r in rows:
            c = (r.caption or "").strip()
            if c:
                caps.append(c)
        photos["captions"] = caps[:15]
    except Exception:  # noqa: BLE001 — 소스 하나 실패가 전체를 깨지 않게
        photos = {"count": 0, "captions": []}

    # 판결(사건): 이번 달 생성된 사건 수/화해 종결 수 + 제목/상황 스니펫 최대 ~5개.
    cases = {"count": 0, "resolved": 0, "topics": []}
    try:
        rows = (
            Case.query.filter(
                Case.couple_id == couple_id,
                Case.created_at >= start_dt,
                Case.created_at < next_start,
            )
            .order_by(Case.created_at.asc())
            .all()
        )
        cases["count"] = len(rows)
        cases["resolved"] = sum(1 for r in rows if r.status == "resolved")
        topics = []
        for r in rows:
            snip = (r.title or r.situation or "").strip()
            if snip:
                topics.append(snip[:40])
        cases["topics"] = topics[:5]
    except Exception:  # noqa: BLE001
        cases = {"count": 0, "resolved": 0, "topics": []}

    # 데이트: 이번 달 '다녀왔어'로 표시한 행사 제목 최대 ~8개(중복 제거) +
    # 커플이 둘 다 찜해 '확정'된 행사 수(confirmed_count).
    dates = {"visited": [], "confirmed_count": 0}
    try:
        visited_rows = (
            db.session.query(EventItem.title)
            .join(EventPick, EventPick.event_id == EventItem.id)
            .filter(
                EventPick.couple_id == couple_id,
                EventPick.status == "visited",
                EventPick.updated_at >= start_dt,
                EventPick.updated_at < next_start,
            )
            .distinct()
            .all()
        )
        titles = []
        seen = set()
        for (t,) in visited_rows:
            t = (t or "").strip()
            if t and t not in seen:
                seen.add(t)
                titles.append(t)
        dates["visited"] = titles[:8]

        # confirmed = interested한 서로 다른 사용자가 2명 이상인 행사(기존 로직 재사용).
        picks = EventPick.query.filter(
            EventPick.couple_id == couple_id,
            EventPick.status == "interested",
        ).all()
        by_event = {}
        for p in picks:
            by_event.setdefault(p.event_id, set()).add(p.user_id)
        dates["confirmed_count"] = sum(
            1 for uids in by_event.values() if len(uids) >= 2
        )
    except Exception:  # noqa: BLE001
        dates = {"visited": [], "confirmed_count": 0}

    return {"qa": qa, "photos": photos, "cases": cases, "dates": dates}
