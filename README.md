# claude-web-wrapper

브라우저에서 Claude Code CLI를 감싸 쓰는 웹 래퍼. `claude auth login`을 통한
브라우저 로그인과, `claude -p` 결과의 스트리밍 응답(채팅)을 제공한다.

## 구성

- **Backend** — Flask (`app.py`). `claude` CLI를 서브프로세스로 실행한다.
- **Frontend** — 단일 페이지 (`templates/intex.html`).

## 동작

1. **세션 로그인** — `/start-login`이 `claude auth login`을 실행해 인증 URL을 추출·표시한다.
   브라우저에서 로그인 후 표시되는 인증 코드(`code#state`)를 붙여넣으면,
   `/complete-login`이 그 코드를 로그인 프로세스의 stdin에 써서 인증을 완료한다.
2. **채팅** — `/ask`가 `claude -p <프롬프트>`를 실행하고 출력을 스트리밍한다.

## 실행

```bash
pip install -r requirements.txt
python app.py
```

- 로컬: http://127.0.0.1:5000
- 기본 바인딩은 `0.0.0.0:5000` (같은 네트워크에서 접속 가능).

## ⚠️ 보안 주의

- `/ask`는 도구 권한을 가진 **Claude Code 에이전트**를 그대로 호출한다 (파일 편집·셸 실행 가능).
  이를 네트워크에 노출하는 것은 **원격 코드 실행 구멍**과 같다. 로컬·신뢰된 환경에서만 사용할 것.
- `debug=True` + `0.0.0.0` 바인딩은 개발 전용이다. 외부 공개 금지.
