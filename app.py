from flask import Flask, render_template, request, Response, jsonify
import subprocess
import re

app = Flask(__name__)

# ANSI 색상 코드(터미널 특수문자) 제거용 정규식
ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

# 글로벌 변수로 로그인 프로세스 상태 보관
login_process = None 

@app.route('/')
def index():
    """메인 화면 렌더링"""
    return render_template('intex.html')

@app.route('/start-login', methods=['POST'])
def start_login():
    """클로드 로그인 프로세스 시작 및 URL 추출"""
    global login_process
    
    # 기존 로그인 프로세스가 있으면 강제 종료
    if login_process:
        login_process.kill()
        
    try:
        # 백그라운드에서 로그인 명령어 실행
        login_process = subprocess.Popen(
            ['claude', 'auth', 'login'],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1
        )
        
        auth_url = None
        
        # 출력 스트림에서 https:// 로 시작하는 인증 링크 찾기
        for line in iter(login_process.stdout.readline, ''):
            url_match = re.search(r'(https://[^\s]+)', line)
            if url_match:
                auth_url = url_match.group(1)
                break # URL을 찾았으므로 읽기 중단 (프로세스는 로컬호스트 콜백 대기 상태로 유지됨)

        if auth_url:
            return jsonify({"status": "success", "url": auth_url})
        else:
            return jsonify({"status": "error", "message": "로그인 URL을 찾을 수 없습니다."})
            
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/complete-login', methods=['POST'])
def complete_login():
    """프론트에서 받은 인증 코드(code#state)를 로그인 프로세스 stdin에 써서 완료"""
    global login_process
    data = request.json
    code = (data.get('code') or '').strip()

    if not code:
        return jsonify({"status": "error", "message": "인증 코드가 비어 있습니다."})

    if not login_process or login_process.poll() is not None:
        return jsonify({"status": "error", "message": "대기 중인 로그인 프로세스가 없습니다. 1단계부터 다시 시작하세요."})

    try:
        # 코드 붙여넣기를 기다리는 CLI의 stdin에 코드를 써줌
        login_process.stdin.write(code + "\n")
        login_process.stdin.flush()

        # 코드 교환이 끝나면 CLI가 남은 출력을 뱉고 종료 → EOF까지 읽어 성공/실패 판정
        output_lines = []
        for line in iter(login_process.stdout.readline, ''):
            clean_line = ansi_escape.sub('', line).strip()
            if clean_line:
                output_lines.append(clean_line)

        login_process.wait(timeout=15)
        rc = login_process.returncode
        result_text = "\n".join(output_lines)
        login_process = None

        if rc == 0:
            return jsonify({"status": "success", "message": "로그인 완료!", "detail": result_text[-500:]})
        return jsonify({"status": "error", "message": f"로그인 실패 (exit {rc}): {result_text[-300:]}"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

@app.route('/ask', methods=['POST'])
def ask():
    """클로드 코드 CLI 호출 및 스트리밍 응답 (채팅)"""
    user_input = request.form.get('prompt')
    
    if not user_input:
        return "입력값이 없습니다.", 400

    def generate_output():
        # 클로드 코드 실행 (사용자 입력을 인자로 전달)
        process = subprocess.Popen(
            ['claude', '-p', user_input],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1
        )
        
        for line in iter(process.stdout.readline, ''):
            if line:
                # 터미널 색상 찌꺼기 제거 후 줄바꿈 태그로 변환
                clean_line = ansi_escape.sub('', line).strip()
                if clean_line:
                    yield f"{clean_line}<br>\n"
                    
        process.stdout.close()
        process.wait()

    return Response(generate_output(), mimetype='text/html')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)