# CLAUDE.md — claude-web-wrapper

> 이 파일은 Claude Code 세션이 이 리포지토리에서 작업할 때 읽는 온보딩 문서다.
> 사람용 개요는 [README.md](README.md) 참고. 여기서는 *다른 Claude가 빠르게 맥락을 잡고
> 실수를 피하도록* 아키텍처·함정·의도를 기록한다.

## 이 프로젝트가 하는 일

브라우저에서 **Claude Code CLI(`claude`)를 감싸 쓰는 웹 래퍼**다. 두 가지를 제공한다:

1. **브라우저 로그인** — 터미널 없이 `claude auth login`을 웹에서 완료.
2. **채팅** — 웹 입력창 → `claude -p <프롬프트>` → 출력 스트리밍.

즉, CLI 인증과 프롬프트 실행을 HTTP 엔드포인트로 노출한 얇은 Flask 앱이다.

## 아키텍처

- **`app.py`** — Flask 백엔드. `claude` CLI를 `subprocess`로 실행한다. 상태를 가진
  로그인 프로세스는 전역 변수 `login_process`로 보관한다(로그인은 2-스텝이라 프로세스가
  두 요청에 걸쳐 살아있어야 함).
- **`templates/intex.html`** — 단일 페이지 프론트(로그인 섹션 + 채팅 섹션). 순수 JS.
  > ⚠️ 파일명이 `index.html`이 아니라 **`intex.html`**(오타에서 유래). `app.py`의
  > `render_template('intex.html')`도 이 이름을 가리킨다. 둘 중 하나만 바꾸면 깨진다.

### 엔드포인트

| 경로 | 하는 일 |
|---|---|
| `GET /` | 메인 페이지 렌더 |
| `POST /start-login` | `claude auth login` 실행 → stdout에서 인증 URL 추출해 반환. 프로세스는 코드 입력 대기 상태로 유지 |
| `POST /complete-login` | 프론트가 보낸 인증 코드(`code#state`)를 `login_process.stdin`에 써서 인증 완료 |
| `POST /ask` | `claude -p <prompt>` 실행, 출력을 `text/html`로 스트리밍(줄마다 `<br>`) |

## 반드시 알아야 할 함정 (실측으로 확인된 것)

1. **인코딩** — Windows(한글 로케일)에서 `subprocess`가 기본 cp949로 디코딩하면
   `claude` 출력의 UTF-8 문자(예: `—`)에서 `UnicodeDecodeError`가 난다.
   모든 `Popen`은 `text=True, encoding='utf-8', errors='replace'`로 열어야 한다.
2. **CLI 커맨드명** — `claude login`/`claude code`는 **존재하지 않는다**. 로그인은
   `claude auth login`, 비대화형 실행은 `claude -p <prompt>`다. (프롬프트가 서브커맨드로
   오인되는 것을 조심)
3. **로그인은 "코드 붙여넣기" 방식** — `claude auth login`은 자동 localhost 콜백이 아니라,
   인증 후 화면에 뜨는 `code#state` 문자열을 CLI **stdin에 붙여넣어** 완료하는 흐름이다.
   그래서 `/start-login`은 `stdin=subprocess.PIPE`로 프로세스를 열어두고,
   `/complete-login`이 코드를 stdin에 써준다.
   > 리스크: CLI가 코드를 순수 stdin이 아니라 **TTY 전용**으로 읽으면 파이프 주입이 안 먹힐
   > 수 있다. 그 경우 pty(`winpty`/`pywinpty`) 우회가 필요하다. (현재는 stdin 파이프로 동작 확인됨)

## ⚠️ 보안 (설계상 중요)

- `/ask`가 호출하는 `claude -p`는 **도구 권한을 가진 에이전트**다 — 파일 편집·셸 실행까지
  가능. 이걸 `0.0.0.0:5000`에 열어둔 현재 구조는 사실상 **원격 코드 실행(RCE) 표면**이다.
- 신뢰된 로컬 환경 전용. 외부/사내 공유로 확장할 거면 **먼저** `--allowedTools`를 비우거나
  `--bare`·샌드박스로 권한을 죽인 "프롬프트-only" 모드로 바꿔야 한다.
- `debug=True`도 개발 전용.

## 개발

```bash
pip install -r requirements.txt
python app.py          # http://127.0.0.1:5000  (debug 리로드 켜짐)
```

## 로드맵 / 의도 (진행 중 아이디어)

- **라이브 프리뷰형 생성기**로 발전 방향 논의됨: 프롬프트 → self-contained HTML 생성 →
  `sandbox` 걸린 iframe(`srcdoc`)에 라이브 렌더 = 개인용 Artifacts/v0 스타일.
  이 방향으로 가면 `/ask`를 "HTML 생성 전용 + 코드펜스 추출" + **권한 죽인 안전 모드**로
  개조하는 것이 전제.
