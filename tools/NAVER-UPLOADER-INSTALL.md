# 네이버 사진 업로더 — 유저스크립트 설치 (Android · iPhone)

후기 사진을 네이버 블로그에 **정식(내부) 이미지**로 올리고 본문까지 조립해 주는
유저스크립트다. 설치해 두면 네이버 **글쓰기** 페이지를 열 때 오버레이가 **자동으로**
뜬다(즐겨찾기 탭 불필요). 앱의 후기 화면에서 **📤 네이버로 보내기**로 페이로드를 복사한
뒤, 오버레이 칸에 붙여넣고 **🅢 정식 삽입** 을 누르면 된다.

- 원본(raw) URL:
  `https://raw.githubusercontent.com/yunhyuk-choi/couple-daily/main/tools/naver-uploader.user.js`
- 기존 **북마클릿**(`tools/naver-bookmarklet.txt`)도 그대로 쓸 수 있다 — 유저스크립트는
  "자동으로 뜨는" 편의 버전일 뿐 로직은 동일하다.

---

## Android (Firefox 또는 Kiwi Browser)

1. **Firefox** 또는 **Kiwi Browser** 를 설치한다(둘 다 확장 지원).
2. 브라우저 확장 스토어에서 **Tampermonkey** 를 설치한다.
   - Firefox: `about:addons` → 부가 기능 검색 → Tampermonkey 추가.
   - Kiwi: 메뉴 → Extensions → Chrome 웹스토어에서 Tampermonkey 설치.
3. 위 **raw `.user.js` URL** 을 주소창에 열면 Tampermonkey 가 **설치 창**을 띄운다 → *설치*.
4. 네이버 블로그 **글쓰기** 페이지를 연다 → 오버레이가 자동으로 나타난다.

## iPhone (Safari + "Userscripts")

1. App Store 에서 **Userscripts**(무료) 를 설치한다.
2. **설정 → Safari → 확장 프로그램** 에서 **Userscripts** 를 켜고,
   `blog.naver.com`(및 `m.blog.naver.com`) 접근을 **허용** 한다.
3. Userscripts 앱을 열어 관리자로 들어가 **새 스크립트 추가** →
   위 `.user.js` 내용을 **붙여넣기**(또는 파일로 불러오기) 후 저장한다.
   - 파일이 필요하면 raw URL 을 사파리로 열어 내용을 복사한다.
4. Safari 에서 네이버 블로그 **글쓰기** 페이지를 연다 → 오버레이가 자동으로 나타난다.

---

## 사용법 (공통)

1. 앱의 후기 상세에서 **📤 네이버로 보내기** → 페이로드(JSON)가 클립보드에 복사됨.
2. 네이버 글쓰기 화면의 오버레이 칸에 **붙여넣기**(Ctrl+V / 길게 눌러 붙여넣기).
3. **🅢 정식 삽입 (setDocumentData)** 을 누른다 → 사진이 네이버 인프라로 업로드되고
   제목·본문·별점·해시태그까지 조립되어 편집기에 들어간다. 확인 후 발행.
4. 자동 업로드가 안 되면 **처리 ▶** / **🅳 데이터URI** / **🔬 에디터 API 탐색** 과
   **🔍 진단 로그** 로 원인을 확인한다(토큰이 안 잡히면 오버레이의 **🔑 토큰 수동입력**).

## 메모

- 스크립트는 **최상위 프레임에서만** 오버레이를 띄우고(중복 방지), 에디터 iframe 은
  same-origin 프레임-워크로 커버한다.
- 글쓰기 페이지가 아니면 잠잠(dormant)하다 — 다른 네이버 페이지에선 아무것도 안 뜬다.
- `@grant none` 이라 페이지 컨텍스트에서 동작하며, 격리 엔진 대비로 `unsafeWindow` 를
  우선 사용해 `SmartEditor`·fetch/XHR 후킹이 실제 페이지 window 에서 이뤄지게 한다.
- 업데이트: 리포의 `.user.js` 가 바뀌면 Tampermonkey 는 raw URL 로 갱신 확인, iPhone
  Userscripts 는 내용을 다시 붙여넣으면 된다.
