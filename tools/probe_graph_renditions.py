#!/usr/bin/env python
"""Graph 커스텀 렌디션의 **실제 한도**를 잰다 — 문서에 안 적힌 것을 측정으로 안다.

왜 필요한가
-----------
앱의 이미지 경로는 "원본 대신 Graph 렌디션(`c{W}x{H}`)을 받는다"에 기대고 있다.
Microsoft 문서는 `c{W}x{H}` 가 "박스 안에 들어가도록 비율을 지켜 줄인다"고만 말하고
**상한을 적어 두지 않았으며**, "요청한 크기와 정확히 같지 않을 수 있다"고 덧붙인다.
그래서 코드는 추측하지 않고 `onedrive.get_rendition` 이 **돌아온 가로세로를 보고**
판정하도록 돼 있다(모자라면 원본으로 폴백). 이 스크립트는 그 폴백이 **언제부터
일어나는지**를 운영 환경에서 한 번 재서 알려 준다.

쓰는 법 (OneDrive 자격이 설정된 환경에서)
-----------------------------------------
    python tools/probe_graph_renditions.py            # 폴더의 첫 사진으로
    python tools/probe_graph_renditions.py <item-id>  # 특정 아이템으로

출력은 표 하나다 — 요청한 크기, 실제로 받은 크기, 바이트, 비율 일치 여부.
'실제로 받은 긴 변'이 요청값을 따라가다 **멈추는 지점**이 그 드라이브의 한도다.

⚠️ 출력에 **URL 을 담지 않는다** — pre-auth CDN 링크는 그 자체가 자격증명이다
(`onedrive.probe_direct_cors` 와 같은 규율). 자격증명을 인자로 받지도 않는다.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import onedrive  # noqa: E402
from app import create_app  # noqa: E402


def main():
    app = create_app()
    with app.app_context():
        item_id = sys.argv[1] if len(sys.argv) > 1 else None
        if not item_id:
            items = [i for i in onedrive.list_folder() if i.get("file")]
            if not items:
                print("couple-daily 폴더에 사진이 없다 — item-id 를 인자로 줘.")
                return 1
            item_id = items[0]["id"]
            print(f"대상: {items[0].get('name')}")

        out = onedrive.probe_renditions(item_id)
        o = out["original"]
        print(f"\n원본: {o.get('width')}x{o.get('height')} · "
              f"{(o.get('size') or 0) / 1024 / 1024:.2f}MB\n")
        print(f"{'요청':>12} | {'받은 크기':>13} | {'바이트':>10} | 비율")
        print("-" * 52)
        for r in out["renditions"]:
            if not r["ok"]:
                got, size = "(거부/없음)", "-"
            else:
                got = f"{r['width']}x{r['height']}"
                size = (f"{r['bytes'] / 1024:.0f}KB" if r["bytes"] else "-")
            aspect = {True: "같음", False: "⚠️다름", None: "?"}[r["aspect_ok"]]
            print(f"{r['spec']:>12} | {got:>13} | {size:>10} | {aspect}")
        print("\n'받은 긴 변'이 요청을 따라가다 멈추는 지점이 이 드라이브의 한도다.")
        print("앱은 그 한도를 넘는 요구가 생기면 자동으로 원본 경로로 폴백한다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
