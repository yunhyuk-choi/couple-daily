"""Quantitative monthly metrics, computed in code from the DB.

Deliberately NO love score / compatibility percentage — just honest counts and
trends grounded in real activity.
"""
from calendar import monthrange
from datetime import date

from models import DailyQuestion, Answer, User


def _month_bounds(year, month):
    start = date(year, month, 1)
    end = date(year, month, monthrange(year, month)[1])
    return start, end


def compute_monthly_stats(couple_id, year, month, user_a, user_b):
    """Return a dict of quantitative stats for the given couple + month.

    user_a is treated as "me" for the who-answers-first framing; both names are
    included so the caller can render neutrally.
    """
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
    """Collect the month's Q&A (both answered OR partial) for the qualitative pass."""
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
