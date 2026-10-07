"""디스크에 사는 바이트 캐시 — 512MB 티어에서 '용량'과 '메모리'를 떼어 놓는 장치.

왜 이게 있나 (2026-10-07 OOM 사고)
----------------------------------
이 앱은 사진 바이트를 두 군데에 캐시했다 — OneDrive 원본(`onedrive`)과 가공완료
JPEG(`app`). 둘 다 **모듈 dict + 개수 상한**이었는데, 개수 상한은 메모리를 전혀
묶지 못했다. 실측(3024×4032 아이폰 원본 기준):

    원본 1장 3.03MB × 64개   = 197MB 상주
    hq 가공본 3.28MB × 200개 = 656MB 상주   ← 512MB 티어를 혼자 넘긴다

거기에 `claude -p` 한 개가 실측 약 400MB다. 캐시가 RAM 에 있는 한, '얼마나 담을
것인가'와 '죽지 않을 것인가'가 정면으로 충돌한다.

그래서 **담는 양을 줄이는 대신 자리를 옮겼다.** 바이트는 컨테이너의 임시 디스크에
두고(Render 의 ephemeral 저장소는 tmpfs 가 아니라 **disk** 다 — 캡션 임시 이미지도
이미 파일로 쓴다), 프로세스 메모리에는 **아무것도 남기지 않는다.** 결과:

  * 상주 RAM     → 0 (히트할 때 응답 바이트만 잠깐 올라온다)
  * 담는 양      → 오히려 늘었다 (예산을 MB 단위로 크게 잡을 수 있다)
  * 히트 비용    → OneDrive 왕복 + Pillow 재인코딩(0.1 CPU 에서 수 초)
                   → 로컬 파일 read 수 ms

규율
----
* **절대 raise 하지 않는다.** 읽기전용·권한 없음·경로 불가 환경에서는 조용히
  '항상 미스'로 동작한다 — 느려질 뿐 요청은 그대로 산다.
* 쓰기는 temp→``os.replace`` 로 **원자적**이다(반쪽 파일을 읽을 일이 없다).
* 축출은 TTL 먼저, 그다음 **오래된 것부터** 바이트 예산 안으로.
* 캐시 디렉토리는 시스템 임시 디렉토리 아래다 — **레포 워킹트리를 더럽히지 않는다.**
"""
import hashlib
import logging
import os
import tempfile
import time

log = logging.getLogger(__name__)


class DiskByteCache:
    """키 → 바이트를 디스크에 담는 TTL+예산 캐시. 메모리에는 아무것도 안 남긴다."""

    def __init__(self, name, ttl, budget, directory=None):
        self.name = name
        self.ttl = ttl
        self.budget = budget
        self.dir = directory or os.path.join(
            tempfile.gettempdir(), f"couple-daily-{name}"
        )

    # -- 경로 ---------------------------------------------------------------
    def path(self, key):
        """캐시 키 → 파일 경로. 키를 해시해 파일명 제약(길이·특수문자)을 피한다."""
        h = hashlib.sha256(repr(key).encode("utf-8")).hexdigest()
        return os.path.join(self.dir, h + ".bin")

    # -- 읽기 ---------------------------------------------------------------
    def get(self, key):
        """유효(미만료) 히트면 바이트, 아니면 None. 절대 raise 하지 않는다."""
        try:
            path = self.path(key)
            st = os.stat(path)
            if (time.time() - st.st_mtime) > self.ttl:
                return None  # 만료 — 다음 put 의 축출이 치운다
            with open(path, "rb") as fh:
                return fh.read()
        except Exception:  # noqa: BLE001 — 캐시는 요청을 깨뜨릴 이유가 아니다
            return None

    # -- 쓰기 ---------------------------------------------------------------
    def put(self, key, data):
        """원자적으로 쓰고 예산 안으로 축출한다. 절대 raise 하지 않는다."""
        if not data:
            return
        try:
            os.makedirs(self.dir, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self.dir, suffix=".part")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, self.path(key))
            except BaseException:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise
            self.evict()
        except Exception:  # noqa: BLE001 — 디스크를 못 쓰면 캐시 없이 계속한다
            log.debug("%s: 디스크 캐시 쓰기 실패 — 캐시 없이 계속", self.name,
                      exc_info=True)

    def delete(self, key):
        """한 항목을 지운다(원본이 삭제됐을 때). 절대 raise 하지 않는다."""
        try:
            os.remove(self.path(key))
        except Exception:  # noqa: BLE001
            pass

    # -- 축출 ---------------------------------------------------------------
    def evict(self):
        """만료분을 지우고, 그래도 예산을 넘으면 오래된 것부터 지운다."""
        try:
            now = time.time()
            entries = []
            with os.scandir(self.dir) as it:
                for e in it:
                    try:
                        if not e.is_file():
                            continue
                        st = e.stat()
                    except OSError:
                        continue
                    if (now - st.st_mtime) > self.ttl:
                        try:
                            os.remove(e.path)
                        except OSError:
                            pass
                        continue
                    entries.append((st.st_mtime, st.st_size, e.path))
            total = sum(s for _, s, _ in entries)
            if total <= self.budget:
                return
            for _mtime, size, path in sorted(entries):  # 오래된 것부터
                try:
                    os.remove(path)
                except OSError:
                    continue
                total -= size
                if total <= self.budget:
                    return
        except Exception:  # noqa: BLE001 — 축출 실패가 요청을 깨뜨리지 않게
            pass

    # -- 점검용(테스트·진단) -------------------------------------------------
    def stats(self):
        """``(개수, 총 바이트)``. 디렉토리가 없으면 (0, 0)."""
        try:
            n = total = 0
            with os.scandir(self.dir) as it:
                for e in it:
                    try:
                        if e.is_file():
                            n += 1
                            total += e.stat().st_size
                    except OSError:
                        continue
            return n, total
        except Exception:  # noqa: BLE001
            return 0, 0
