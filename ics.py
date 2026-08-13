"""iCalendar(.ics) 생성 — 순수 함수. 기기 캘린더 '추가'용 최소 VEVENT 하나.

요청 경로에서 바로 부르는 값싼 문자열 조립이라 claude·DB·네트워크가 없다.
RFC5545 준수 핵심:
  * 줄바꿈은 반드시 CRLF(``\\r\\n``).
  * TEXT 값(SUMMARY·DESCRIPTION·LOCATION·URL)은 백슬래시·세미콜론·콤마·줄바꿈을
    이스케이프한다.
  * 종일 일정은 ``DTSTART;VALUE=DATE`` + '배타적' ``DTEND;VALUE=DATE``(다음 날).
  * 시간 일정은 플로팅 로컬 시각(``Z``·``TZID`` 없음) — 한국 커플의 기기가 로컬
    (KST)로 해석한다(가장 단순하고 올바르다).
접기(line folding)는 단순함을 위해 생략한다(값이 길지 않다).
"""
from datetime import datetime, timedelta


def _esc(text) -> str:
    """RFC5545 TEXT 이스케이프: 백슬래시·세미콜론·콤마·줄바꿈."""
    if text is None:
        return ""
    s = str(text)
    s = s.replace("\\", "\\\\")
    s = s.replace(";", "\\;")
    s = s.replace(",", "\\,")
    s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    return s


def build_ics(uid, summary, dt_start, dt_end=None, all_day=False,
              description=None, location=None, url=None) -> str:
    """VCALENDAR 1개(그 안에 VEVENT 1개)의 .ics 전문을 돌려준다(CRLF 종결).

    all_day=True  → ``dt_start``/``dt_end``는 ``date``. ``DTSTART;VALUE=DATE``와
                    '배타적' ``DTEND;VALUE=DATE``. ``dt_end``가 없으면 ``dt_start+1일``.
                    (여러 날 일정은 호출부가 end_date+1을 ``dt_end``로 넘긴다.)
    all_day=False → ``dt_start``는 ``datetime``. ``DTSTART:...``(플로팅 로컬,
                    ``Z``/``TZID`` 없음). ``dt_end``가 없으면 시작 +1시간.
    """
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//우리의하루//KR",
        "CALSCALE:GREGORIAN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}",
    ]
    if all_day:
        d0 = dt_start
        d1 = dt_end if dt_end is not None else d0 + timedelta(days=1)
        lines.append(f"DTSTART;VALUE=DATE:{d0.strftime('%Y%m%d')}")
        lines.append(f"DTEND;VALUE=DATE:{d1.strftime('%Y%m%d')}")
    else:
        s0 = dt_start
        s1 = dt_end if dt_end is not None else s0 + timedelta(hours=1)
        lines.append(f"DTSTART:{s0.strftime('%Y%m%dT%H%M%S')}")
        lines.append(f"DTEND:{s1.strftime('%Y%m%dT%H%M%S')}")
    lines.append(f"SUMMARY:{_esc(summary)}")
    if description:
        lines.append(f"DESCRIPTION:{_esc(description)}")
    if location:
        lines.append(f"LOCATION:{_esc(location)}")
    if url:
        lines.append(f"URL:{_esc(url)}")
    lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
