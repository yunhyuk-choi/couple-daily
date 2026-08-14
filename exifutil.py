"""사진 EXIF에서 '촬영일시'를 뽑는 유틸.

JPEG과 iPhone HEIC를 모두 다룬다(HEIC는 pillow-heif가 libheif 휠을 번들해 순수
pip 설치만으로 열린다). 메타데이터(EXIF)만 읽고 픽셀을 강제 디코드하지 않는다.

``extract_taken_at(image_bytes)`` 는 촬영일시(datetime)를 돌려주거나, 촬영일이
없거나/못 읽거나/이미지가 아니면 ``None`` 을 돌려준다 — *절대 예외를 던지지 않는다.*
Pillow/pillow-heif가 없어도 앱이 깨지지 않도록 임포트 실패는 조용히 삼킨다(그 경우
``extract_taken_at`` 은 항상 None).
"""
import io
import logging
from datetime import datetime

log = logging.getLogger(__name__)

# Pillow(HEIC 오프너 포함)를 한 번만, 방어적으로 로드한다. 없으면 _PIL_OK=False로
# 두고 extract_taken_at이 항상 None을 돌려주게 한다(앱은 계속 동작).
try:  # pragma: no cover - 환경에 따라 분기
    from PIL import Image  # type: ignore

    try:
        import pillow_heif  # type: ignore

        pillow_heif.register_heif_opener()  # HEIC/HEIF를 PIL.Image.open이 열게 등록
    except Exception:  # noqa: BLE001 - HEIC 미지원이어도 JPEG 경로는 살린다
        log.info("exifutil: pillow-heif 미탑재 — HEIC는 건너뛰고 JPEG만 처리")
    _PIL_OK = True
except Exception:  # noqa: BLE001 - Pillow 자체가 없으면 기능 전체 비활성
    Image = None  # type: ignore
    _PIL_OK = False
    log.info("exifutil: Pillow 미탑재 — EXIF 촬영일 추출 비활성(항상 None)")

# EXIF 태그: DateTimeOriginal(0x9003)·DateTimeDigitized(0x9004)는 Exif IFD
# (0x8769) 하위에, DateTime(0x0132)은 IFD0에 있다.
_EXIF_IFD = 0x8769
_TAG_DATETIME_ORIGINAL = 0x9003
_TAG_DATETIME_DIGITIZED = 0x9004
_TAG_DATETIME = 0x0132


def _parse_exif_datetime(value) -> "datetime | None":
    """EXIF 문자열 "YYYY:MM:DD HH:MM:SS" → datetime. 실패하면 None.

    말미의 서브초·타임존 같은 잔여 접미사는 관대하게 잘라낸다(예:
    "2026:08:03 14:30:00.123+09:00" 도 초까지만 취한다).
    """
    if value is None:
        return None
    try:
        s = value.decode("ascii", "ignore") if isinstance(value, bytes) else str(value)
    except Exception:  # noqa: BLE001
        return None
    s = s.strip().strip("\x00").strip()
    if not s:
        return None
    # "날짜 시간" 두 토큰만 취해 앞부분만 파싱한다(잔여 접미사 방어).
    parts = s.split()
    if len(parts) < 2:
        return None
    date_part, time_part = parts[0], parts[1]
    # 시간부의 서브초/타임존 접미사 제거: 초까지(HH:MM:SS)만 남긴다.
    time_part = time_part.split(".", 1)[0]
    for sep in ("+", "-", "Z", "z"):
        idx = time_part.find(sep)
        if idx != -1:
            time_part = time_part[:idx]
            break
    try:
        return datetime.strptime(f"{date_part} {time_part}", "%Y:%m:%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def extract_taken_at(image_bytes: bytes) -> "datetime | None":
    """이미지 바이트에서 촬영일시(datetime)를 뽑는다. 없으면/실패하면 None.

    우선순위: DateTimeOriginal → DateTimeDigitized → DateTime(IFD0).
    메타데이터만 읽고(getexif) 픽셀을 강제 디코드하지 않는다. 어떤 예외도 밖으로
    던지지 않는다(호출부 업로드/캘린더를 절대 깨지 않게).
    """
    if not _PIL_OK or not image_bytes:
        return None
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            exif = img.getexif()  # 메타데이터 수준(전체 디코드 아님)
            if not exif:
                return None
            # 1) Exif IFD(0x8769)의 DateTimeOriginal → DateTimeDigitized
            try:
                ifd = exif.get_ifd(_EXIF_IFD)
            except Exception:  # noqa: BLE001
                ifd = None
            if ifd:
                dt = _parse_exif_datetime(ifd.get(_TAG_DATETIME_ORIGINAL))
                if dt is not None:
                    return dt
                dt = _parse_exif_datetime(ifd.get(_TAG_DATETIME_DIGITIZED))
                if dt is not None:
                    return dt
            # 2) IFD0의 DateTime 폴백
            dt = _parse_exif_datetime(exif.get(_TAG_DATETIME))
            if dt is not None:
                return dt
    except Exception:  # noqa: BLE001 - 이미지 아님/깨진 EXIF/미지원 포맷 등 전부 삼킴
        return None
    return None
