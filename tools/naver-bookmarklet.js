/*
 * 우리의 하루 — 네이버 후기 사진 업로더 (PLAIN BOOKMARKLET, PHASE 1)
 * ==========================================================================
 * 확장/Tampermonkey 없이 '즐겨찾기(북마클릿)' 하나로 동작한다. 폰에서도 됨.
 *
 * 어떻게 세션키를 얻나 (2단):
 *   - session-key GET은 에디터가 보내는 두 요청 헤더가 있어야 200이다:
 *       se-authorization (약 1시간 유효 HS256 JWT), se-app-id (SE-… 문자열).
 *     이 토큰은 1회용 논스가 아니라 유효기간(~1h) 안에서 재사용 가능하다.
 *   - 그래서 fetch/XMLHttpRequest를 몽키패치해 '아무 요청에서나' 이 두 헤더 값을
 *     가로채(window.__cdAuth / window.__cdAppId), 그걸 붙여 우리가 직접
 *     session-key를 GET한다(주 경로). 에디터가 이미 부른 session-key '응답'의
 *     sessionKey를 잡았으면 그걸 바로 재사용한다(더 빠른 경로).
 *   - 토큰이 아직 안 잡혔으면: 에디터를 한 번 클릭하거나 사진 1장 추가하면 에디터가
 *     se-* 헤더 요청을 보내고, 우리가 잡는다(폴백).
 *
 * 왜 오버레이 + 붙여넣기(textarea)인가(폰/포커스 안전):
 *   - navigator.clipboard.readText()는 '문서 포커스'가 필요해 북마클릿 클릭 직후엔
 *     막힌다("Document is not focused"). 그래서 클립보드를 읽지 않고, 오버레이의
 *     textarea에 사용자가 직접 붙여넣게 한다(포커스된 입력창 붙여넣기는 항상 허용).
 *   - 최종 결과 HTML도 자동으로 쓰지 않고 '📋 결과 복사' 버튼(새 제스처)에서 쓴다.
 *   - 모든 진행/오류는 오버레이에 표시(폰엔 DevTools가 없다) + 콘솔에도 로그.
 *
 * 사용 흐름(오버레이 안내와 동일):
 *   ① 에디터를 한 번 클릭(또는 사진 1장 추가)하면 토큰 확보 — '토큰 확보 ✓'가
 *      자동으로 떠 있으면 건너뛰어도 됨(폴백 단계)
 *   ② 아래 칸에 앱 페이로드 붙여넣기 (Ctrl+V 또는 길게 눌러 붙여넣기)
 *   ③ 처리 ▶ (임시로 넣은 사진은 나중에 지워도 됨)
 *   ④ 완료되면 '📋 결과 복사' → 네이버 본문에 붙여넣기.
 *
 * ⚠️ 라이브에서 튜닝할 CONFIG(아래):
 *   - DISPLAY_HOST_PREFIX: upphoto가 주는 <url>(상대경로)에 붙일 표시 호스트.
 *     캡처된 published documentModel에서 blogfiles.pstatic.net로 확정.
 *   - IMG_TYPE_QUERY: 표시 크기 타입 쿼리(?type=w966). 캡처는 ?type=w1이었음.
 *   - BLOG_ID_FALLBACK: userId(블로그 아이디) 페이지 추출 실패 시 폴백.
 *   - UPPHOTO_QUERY: 업로드 쿼리스트링(사용자 캡처 그대로).
 *
 * 이 파일은 '가독용 소스'다. 즐겨찾기에 넣을 최종 javascript: 한 줄은 sibling
 * tools/naver-bookmarklet.txt(정본). 전부 인라인 — 외부 로드/eval 없음(CSP 준수).
 */
(function () {
  'use strict';

  // ======================= CONFIG (라이브에서 조정) =========================
  // 캡처된 published documentModel 확인: 내부 이미지 컴포넌트는 blogfiles.pstatic.net를 쓴다.
  var DISPLAY_HOST_PREFIX = 'https://blogfiles.pstatic.net';
  // 표시 크기 타입 쿼리. 캡처 src는 ?type=w1 이었고 w966이 일반 표시 크기 — 라이브 튜닝용.
  var IMG_TYPE_QUERY = '?type=w966';
  var BLOG_ID_FALLBACK = 'yhc9355';                          // TODO(live): 사용자 블로그 아이디
  var UPPHOTO_QUERY =
    'extractExif=true&extractAnimatedCnt=false&extractAnimatedInfo=true' +
    '&autorotate=true&extractDominantColor=false&type=&customQuery=' +
    '&denyAnimatedImage=false&skipXcamFiltering=false';       // TODO(live): 캡처와 일치 확인
  var UPPHOTO_BASE = 'https://blog.upphoto.naver.com';
  // 이제 활성: 가로챈 se-authorization/se-app-id를 붙여 우리가 직접 GET한다.
  var SESSION_KEY_URL =
    'https://platform.editor.naver.com/api/blogpc001/v1/photo-uploader/session-key';
  var SESSION_KEY_MATCH = 'photo-uploader/session-key';       // 요청/응답 URL 매칭 힌트
  // ========================================================================

  var L = function () {
    try { console.log.apply(console, ['[nv]'].concat([].slice.call(arguments))); } catch (e) {}
  };

  // --------- 토큰/세션키 가로채기 (fetch + XMLHttpRequest 몽키패치) ---------
  // 응답 sessionKey → window.__cdSK, 요청 헤더 se-authorization/se-app-id →
  // window.__cdAuth / window.__cdAppId (모두 재클릭에도 유지, 최신 non-empty).
  // 요청 메타(메서드·URL·헤더)는 window.__cdReq(진단 패널). 콜백으로 오버레이 갱신.
  function extractSessionKey(text) {
    if (!text) return null;
    var m = /"sessionKey"\s*:\s*"([^"]+)"/.exec(text);
    return m && m[1] ? m[1] : null;
  }

  function gotKey(k) {
    if (!k) return;                 // 빈 것 무시 → 최신 non-empty 유지
    window.__cdSK = k;
    L('세션키 확보', k);
    if (window.__cdSKcb) { try { window.__cdSKcb(k); } catch (e) {} }
  }

  function gotAuth() {
    if (window.__cdAuthCb) { try { window.__cdAuthCb(); } catch (e) {} }
  }

  // 한 헤더(name,value)를 보고 se-authorization / se-app-id면 보관(대소문자 무시).
  function setAuthHeader(name, value) {
    if (!value) return;
    var n = ('' + name).toLowerCase();
    if (n === 'se-authorization') { window.__cdAuth = value; gotAuth(); }
    else if (n === 'se-app-id') { window.__cdAppId = value; gotAuth(); }
  }

  // 헤더 컬렉션(Headers / 배열쌍 / 객체)에서 se-* 토큰을 훑어 보관.
  function grabAuth(h) {
    try {
      if (!h) return;
      if (Object.prototype.toString.call(h) === '[object Array]') {
        for (var i = 0; i < h.length; i++) {
          if (h[i] && h[i].length >= 2) setAuthHeader(h[i][0], h[i][1]);
        }
      } else if (typeof h.forEach === 'function') {
        h.forEach(function (v, k) { setAuthHeader(k, v); });
      } else {
        for (var k in h) {
          if (Object.prototype.hasOwnProperty.call(h, k)) setAuthHeader(k, h[k]);
        }
      }
    } catch (e) {}
  }

  // 헤더 컬렉션 → "name: value" 라인 배열(진단 패널 표시용).
  function serializeHeaders(h) {
    var out = [];
    try {
      if (!h) return out;
      if (Object.prototype.toString.call(h) === '[object Array]') {  // [[k,v],…] 배열(먼저)
        for (var i = 0; i < h.length; i++) {
          if (h[i] && h[i].length >= 2) out.push(h[i][0] + ': ' + h[i][1]);
        }
      } else if (typeof h.forEach === 'function') {   // Headers 인스턴스
        h.forEach(function (v, k) { out.push(k + ': ' + v); });
      } else {                                        // 평범한 객체
        for (var k in h) {
          if (Object.prototype.hasOwnProperty.call(h, k)) out.push(k + ': ' + h[k]);
        }
      }
    } catch (e) {}
    return out;
  }

  // 요청 메타 기록(진단 패널용).
  function recordReq(method, url, headerLines) {
    var info = { method: method || 'GET', url: url || '', headers: headerLines || [] };
    window.__cdReq = info;
    L('session-key 요청 캡처', info);
    if (window.__cdReqCb) { try { window.__cdReqCb(info); } catch (e) {} }
  }

  function installInterceptor() {
    if (window.__cdItcp) { L('인터셉터 이미 설치됨'); return; } // 한 번만(재클릭 안전)
    window.__cdItcp = true;
    // fetch 래핑
    try {
      var of = window.fetch;
      if (typeof of === 'function') {
        window.fetch = function () {
          var args = arguments, url;
          try { url = (args[0] && args[0].url) ? args[0].url : ('' + args[0]); }
          catch (e) { url = ''; }
          var a0 = args[0], a1 = args[1];
          // 아무 요청에서나 se-* 토큰 헤더 가로채기(우리가 직접 session-key 부를 재료).
          try {
            if (a1 && a1.headers) grabAuth(a1.headers);
            if (typeof Request !== 'undefined' && a0 instanceof Request) grabAuth(a0.headers);
          } catch (e) {}
          // session-key 요청 메타(메서드·URL·헤더) 캡처 — 진단 패널용.
          try {
            if (url && url.indexOf(SESSION_KEY_MATCH) !== -1) {
              var method = 'GET', hdrs = [];
              if (a1) {
                if (a1.method) method = a1.method;
                if (a1.headers) hdrs = hdrs.concat(serializeHeaders(a1.headers));
              }
              if (typeof Request !== 'undefined' && a0 instanceof Request) {
                if (a0.method) method = a0.method;
                hdrs = hdrs.concat(serializeHeaders(a0.headers));
              }
              recordReq(method, url, hdrs);
            }
          } catch (e) {}
          var p = of.apply(this, args);
          // session-key 응답에서 sessionKey 추출(빠른 경로).
          try {
            if (url && url.indexOf(SESSION_KEY_MATCH) !== -1 && p && p.then) {
              p.then(function (resp) {
                try {
                  resp.clone().text().then(function (t) { gotKey(extractSessionKey(t)); })
                    .catch(function () {});
                } catch (e) {}
              }).catch(function () {});
            }
          } catch (e) {}
          return p;
        };
        L('fetch 인터셉터 설치');
      }
    } catch (e) { L('fetch 패치 실패', e); }
    // XMLHttpRequest 래핑(open=메서드·URL, setRequestHeader=헤더 수집+se-* 캡처,
    // send=요청 메타 기록 + load에서 응답 sessionKey 추출)
    try {
      var oOpen = XMLHttpRequest.prototype.open;
      var oSetHdr = XMLHttpRequest.prototype.setRequestHeader;
      var oSend = XMLHttpRequest.prototype.send;
      XMLHttpRequest.prototype.open = function (method, url) {
        try { this.__cdMethod = method; this.__cdUrl = url; this.__cdHdrs = []; } catch (e) {}
        return oOpen.apply(this, arguments);
      };
      XMLHttpRequest.prototype.setRequestHeader = function (name, value) {
        try {
          if (!this.__cdHdrs) this.__cdHdrs = [];
          this.__cdHdrs.push(name + ': ' + value);
          setAuthHeader(name, value);           // 아무 요청 헤더에서나 se-* 캡처
        } catch (e) {}
        return oSetHdr.apply(this, arguments);
      };
      XMLHttpRequest.prototype.send = function () {
        try {
          var xhr = this;
          if (xhr.__cdUrl && ('' + xhr.__cdUrl).indexOf(SESSION_KEY_MATCH) !== -1) {
            recordReq(xhr.__cdMethod, '' + xhr.__cdUrl, xhr.__cdHdrs || []);
            xhr.addEventListener('load', function () {
              try { gotKey(extractSessionKey(xhr.responseText || '')); } catch (e) {}
            });
          }
        } catch (e) {}
        return oSend.apply(this, arguments);
      };
      L('XHR 인터셉터 설치');
    } catch (e) { L('XHR 패치 실패', e); }
  }

  // 세션키 확보: (1) 가로챈 응답 sessionKey 있으면 재사용, (2) 없으면 가로챈
  // se-authorization으로 우리가 직접 GET, (3) 둘 다 없으면 안내 메시지로 reject.
  function getSessionKey() {
    if (window.__cdSK) {
      L('세션키 재사용(가로챈 응답)', window.__cdSK);
      return Promise.resolve(window.__cdSK);
    }
    if (window.__cdAuth) {
      L('세션키 직접 요청(se-authorization 사용)', window.__cdAppId ? '(se-app-id 포함)' : '');
      var hdrs = { 'accept': 'application/json', 'se-authorization': window.__cdAuth };
      if (window.__cdAppId) hdrs['se-app-id'] = window.__cdAppId;
      return fetch(SESSION_KEY_URL, { credentials: 'include', headers: hdrs })
        .then(function (r) {
          if (!r.ok) throw new Error('session-key HTTP ' + r.status);
          return r.json();
        })
        .then(function (j) {
          L('세션키 응답', j);
          var k = j && j.sessionKey;
          if (!k) throw new Error('세션키 없음(응답에 sessionKey 없음)');
          window.__cdSK = k;
          return k;
        });
    }
    return Promise.reject(new Error(
      '에디터 토큰을 아직 못 잡았어 — 에디터를 한 번 클릭하거나 사진 1장 추가 후 다시 처리해줘'));
  }

  // --------- 네이버 통신 유틸 ---------
  function guessBlogId() {
    try {
      var m = location.pathname.match(/^\/([A-Za-z0-9_-]+)(?:\/|$)/);
      if (m && m[1] && m[1].indexOf('.naver') === -1) return m[1];
    } catch (e) {}
    return BLOG_ID_FALLBACK;
  }

  function dataUriToBlob(dataUri) {
    var comma = dataUri.indexOf(',');
    var header = dataUri.slice(0, comma);
    var b64 = dataUri.slice(comma + 1);
    var mime = (header.match(/data:([^;]+)/) || [])[1] || 'image/jpeg';
    var bin = atob(b64);
    var n = bin.length;
    var bytes = new Uint8Array(n);
    for (var i = 0; i < n; i++) bytes[i] = bin.charCodeAt(i);
    return new Blob([bytes], { type: mime });
  }

  function parseUploadedUrl(xmlText) {
    var el = null;
    try {
      var doc = new DOMParser().parseFromString(xmlText, 'text/xml');
      el = doc.querySelector('url') || doc.getElementsByTagName('url')[0];
    } catch (e) {}
    var raw = el && el.textContent ? el.textContent.trim() : '';
    if (!raw) {
      var m = xmlText.match(/<url>([^<]+)<\/url>/i);
      raw = m ? m[1].trim() : '';
    }
    if (!raw) return null;
    if (/^https?:\/\//i.test(raw)) return raw; // 절대 URL은 그대로(접두·타입쿼리 안 붙임)
    var abs = DISPLAY_HOST_PREFIX + (raw.charAt(0) === '/' ? '' : '/') + raw;
    // 표시 크기 타입 쿼리 추가(이미 ?가 있으면 &type=…, 없으면 ?type=…)
    if (!IMG_TYPE_QUERY) return abs;
    var q = IMG_TYPE_QUERY.replace(/^[?&]/, '');
    return abs + (abs.indexOf('?') === -1 ? '?' : '&') + q;
  }

  function replaceSrc(html, fromSrc, toUrl) {
    var out = html;
    var vs = [fromSrc, fromSrc.replace(/&/g, '&amp;')];
    for (var i = 0; i < vs.length; i++) {
      out = out.split('"' + vs[i] + '"').join('"' + toUrl + '"');
      out = out.split("'" + vs[i] + "'").join("'" + toUrl + "'");
    }
    return out;
  }

  function writeHtmlToClipboard(html, plain) {
    if (window.ClipboardItem && navigator.clipboard && navigator.clipboard.write) {
      return navigator.clipboard.write([new ClipboardItem({
        'text/html': new Blob([html], { type: 'text/html' }),
        'text/plain': new Blob([plain || ''], { type: 'text/plain' })
      })]);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(html);
    }
    return Promise.reject(new Error('clipboard write unsupported'));
  }

  // 잡은 sessionKey로 우리 이미지를 upphoto에 업로드.
  function uploadOne(sessionKey, blogId, blob, idx) {
    var url = UPPHOTO_BASE + '/' + sessionKey + '/simpleUpload/0?userId=' +
      encodeURIComponent(blogId) + '&' + UPPHOTO_QUERY;
    L('업로드[' + idx + '] POST', url, blob.size + 'B');
    return fetch(url, { method: 'POST', credentials: 'include', body: blob })
      .then(function (r) {
        return r.text().then(function (t) {
          L('업로드[' + idx + '] status', r.status);
          if (!r.ok) throw new Error('upphoto HTTP ' + r.status + ' — ' + t.slice(0, 160));
          var nu = parseUploadedUrl(t);
          L('업로드[' + idx + '] 네이버 URL', nu);
          if (!nu) throw new Error('응답에서 URL 못 찾음');
          return nu;
        });
      });
  }

  // 요청 메타 → 읽기 좋은 텍스트(진단 패널에 채움).
  function formatReq(info) {
    if (!info) return '';
    var lines = [(info.method || 'GET') + ' ' + (info.url || '')];
    if (info.headers && info.headers.length) {
      lines.push('');
      for (var i = 0; i < info.headers.length; i++) lines.push(info.headers[i]);
    } else {
      lines.push('(보이는 커스텀 헤더 없음 — 쿠키/HttpOnly는 스크립트에서 안 보임)');
    }
    return lines.join('\n');
  }

  // --------- 오버레이 UI (포커스/제스처 안전) ---------
  function el(tag, css, text) {
    var e = document.createElement(tag);
    if (css) e.style.cssText = css;
    if (text != null) e.textContent = text;
    return e;
  }

  function buildOverlay() {
    var old = document.getElementById('cd-nv-ov');
    if (old && old.parentNode) old.parentNode.removeChild(old);

    var ov = el('div',
      'position:fixed;left:50%;top:16px;transform:translateX(-50%);z-index:2147483647;' +
      'width:560px;max-width:94vw;background:#fff;color:#222;border:1px solid #e5e5e5;' +
      'border-radius:12px;box-shadow:0 8px 40px rgba(0,0,0,.3);padding:16px;' +
      "font-family:-apple-system,BlinkMacSystemFont,'Malgun Gothic',sans-serif;" +
      'font-size:14px;line-height:1.5;max-height:90vh;overflow:auto;');
    ov.id = 'cd-nv-ov';
    // 네이버 에디터 단축키로 새지 않게 오버레이 안 이벤트는 버블에서 멈춘다(버튼은 동작).
    ['keydown', 'keyup', 'keypress', 'click', 'mousedown', 'paste'].forEach(function (ev) {
      ov.addEventListener(ev, function (e) { e.stopPropagation(); }, false);
    });

    ov.appendChild(el('div', 'font-weight:800;font-size:15px;margin-bottom:8px;',
      '🖼 우리 후기 사진 → 네이버 업로드'));
    ov.appendChild(el('div', 'color:#444;margin-bottom:4px;',
      "① (토큰 자동확보 안 되면) 에디터를 한 번 클릭하거나 사진 1장 추가 → '토큰 확보 ✓'"));
    ov.appendChild(el('div', 'color:#444;margin-bottom:4px;',
      '② 아래 칸에 앱 페이로드 붙여넣기 (Ctrl+V 또는 길게 눌러 붙여넣기)'));
    ov.appendChild(el('div', 'color:#444;margin-bottom:8px;',
      '③ 처리 ▶ (임시로 넣은 사진은 나중에 지워도 됨)'));

    var ta = el('textarea',
      'width:100%;height:70px;box-sizing:border-box;border:1px solid #ccc;' +
      'border-radius:8px;padding:8px;font-size:12px;resize:vertical;');
    ta.setAttribute('placeholder', '여기에 붙여넣기 (Ctrl+V / 길게 눌러 붙여넣기)');
    ov.appendChild(ta);

    var status = el('div', 'margin:8px 0;min-height:1.2em;color:#333;font-weight:700;');
    ov.appendChild(status);

    var actions = el('div', 'display:flex;align-items:center;gap:8px;margin-top:2px;');
    var go = el('button',
      'background:#e64980;color:#fff;border:0;border-radius:8px;padding:9px 16px;' +
      'font-weight:800;cursor:pointer;', '처리 ▶');
    var close = el('button',
      'background:#eee;color:#333;border:0;border-radius:8px;padding:9px 14px;cursor:pointer;',
      '닫기');
    actions.appendChild(go);
    actions.appendChild(close);
    ov.appendChild(actions);

    var resultWrap = el('div', 'margin-top:10px;display:none;');
    var copyBtn = el('button',
      'background:#1c7ed6;color:#fff;border:0;border-radius:8px;padding:9px 16px;' +
      'font-weight:800;cursor:pointer;', '📋 결과 복사');
    var resultTa = el('textarea',
      'width:100%;height:70px;box-sizing:border-box;border:1px solid #ccc;' +
      'border-radius:8px;padding:8px;font-size:11px;margin-top:8px;resize:vertical;');
    resultWrap.appendChild(copyBtn);
    resultWrap.appendChild(resultTa);
    resultWrap.appendChild(el('div', 'color:#666;font-size:12px;margin-top:4px;',
      '복사가 안 되면 위 칸을 전체선택(Ctrl+A)→복사(Ctrl+C)해서 붙여넣어'));
    ov.appendChild(resultWrap);

    // 🔍 진단: 에디터 session-key '요청' 필드(참고용) — 폰에서 DevTools 대신.
    var reqDetails = el('details', 'margin-top:12px;border-top:1px solid #eee;padding-top:8px;');
    reqDetails.appendChild(el('summary', 'cursor:pointer;font-weight:700;color:#555;',
      '🔍 에디터 session-key 요청 (참고용)'));
    var reqTa = el('textarea',
      'width:100%;height:96px;box-sizing:border-box;border:1px solid #ccc;' +
      'border-radius:8px;padding:8px;font-size:11px;margin-top:8px;resize:vertical;' +
      'background:#fafafa;');
    reqTa.readOnly = true;
    reqTa.setAttribute('placeholder',
      '아직 안 잡힘 — 에디터에서 사진 1장 추가하면 요청 정보가 여기 채워져요 (선택/복사 가능)');
    reqDetails.appendChild(reqTa);
    ov.appendChild(reqDetails);

    document.body.appendChild(ov);
    setTimeout(function () { try { ta.focus(); } catch (e) {} }, 50);
    close.addEventListener('click', function () {
      if (ov.parentNode) ov.parentNode.removeChild(ov);
    });

    return { ov: ov, ta: ta, status: status, go: go, resultWrap: resultWrap,
      copyBtn: copyBtn, resultTa: resultTa, reqTa: reqTa };
  }

  function setStatus(u, msg, isErr) {
    u.status.textContent = msg;
    u.status.style.color = isErr ? '#c0392b' : '#333';
  }

  // 업로드 완료 — 자동 클립보드 쓰기 금지. '📋 결과 복사'(새 제스처)에서만 쓴다.
  function finish(u, html, done, total) {
    setStatus(u, '완료 (' + done + '/' + total + ') — 아래 버튼 눌러 복사한 뒤 본문에 붙여넣어');
    u.resultTa.value = html;
    u.resultWrap.style.display = 'block';
    u.go.disabled = false;
    u.copyBtn.onclick = function () {
      writeHtmlToClipboard(html, '').then(function () {
        setStatus(u, '복사됨 ✓ — 네이버 본문에 Ctrl+V로 붙여넣어');
      }, function (err) {
        L('clipboard write 실패', err);
        setStatus(u, '자동복사 실패 — 아래 칸 전체선택 후 복사해줘', true);
        try { u.resultTa.focus(); u.resultTa.select(); } catch (e) {}
      });
    };
  }

  function process(u) {
    var raw = (u.ta.value || '').trim();
    var p;
    try { p = JSON.parse(raw); } catch (e) { p = null; }
    if (!p || !p.copy_html) {
      setStatus(u, "페이로드가 안 보여 — 앱에서 '네이버로 보내기' 후 이 칸에 붙여넣어줘", true);
      return;
    }
    var html = p.copy_html;
    var imgs = Array.isArray(p.images) ? p.images : [];
    L('페이로드 OK. 이미지', imgs.length);
    u.go.disabled = true;
    if (!imgs.length) { finish(u, html, 0, 0); return; }

    setStatus(u, '세션키 준비 중…');
    getSessionKey().then(function (key) {
      L('세션키', key);
      var blogId = guessBlogId();
      L('blogId', blogId);
      var chain = Promise.resolve();
      var done = 0;
      imgs.forEach(function (img, i) {
        chain = chain.then(function () {
          setStatus(u, '사진 업로드 중… (' + (i + 1) + '/' + imgs.length + ')');
          var blob = dataUriToBlob(img.dataUri);
          return uploadOne(key, blogId, blob, i).then(function (nu) {
            html = replaceSrc(html, img.src, nu);
            done++;
          }).catch(function (e) {
            L('img' + i + ' 실패', e);
            setStatus(u, '사진 ' + (i + 1) + ' 실패 — 화면 로그 확인 (계속 진행)', true);
          });
        });
      });
      return chain.then(function () { finish(u, html, done, imgs.length); });
    }).catch(function (e) {
      L('세션키 실패', e);
      setStatus(u, (e && e.message ? e.message : '' + e), true);
      u.go.disabled = false;
    });
  }

  // --------- 부팅 ---------
  var TOKEN_OK = '토큰 확보 ✓ (페이로드 붙여넣고 처리)';
  var WAITING = '대기 중 — 에디터를 한 번 클릭(또는 사진 1장 추가)하면 토큰을 잡아요';
  function tokenReady() { return !!(window.__cdAuth || window.__cdSK); }

  var u = buildOverlay();
  installInterceptor();
  // 토큰/세션키/요청 캡처 시 오버레이 갱신(재클릭이면 최신 오버레이가 받음).
  window.__cdSKcb = function () { setStatus(u, TOKEN_OK); };
  window.__cdAuthCb = function () { setStatus(u, TOKEN_OK); };
  window.__cdReqCb = function (info) { try { u.reqTa.value = formatReq(info); } catch (e) {} };
  setStatus(u, tokenReady() ? TOKEN_OK : WAITING);
  if (window.__cdReq) { try { u.reqTa.value = formatReq(window.__cdReq); } catch (e) {} }
  u.go.addEventListener('click', function () {
    try { process(u); }
    catch (e) { L('처리 예외', e); setStatus(u, '오류: ' + (e && e.message ? e.message : e), true); }
  });
  L('오버레이 준비됨. URL=', location.href);
})();

/*
 * ============================ 최종 북마클릿 ================================
 * 아래 한 줄(javascript:...)을 즐겨찾기 URL에 붙여넣고 이름을 '📷 사진 올리기'로
 * 저장. sibling tools/naver-bookmarklet.txt 가 정본이다(같은 내용).
 *
 * javascript:(function(){'use strict';var DISPLAY_HOST_PREFIX='https://blogfiles.pstatic.net';var IMG_TYPE_QUERY='?type=w966';var BLOG_ID_FALLBACK='yhc9355';var UPPHOTO_QUERY='extractExif=true&extractAnimatedCnt=false&extractAnimatedInfo=true&autorotate=true&extractDominantColor=false&type=&customQuery=&denyAnimatedImage=false&skipXcamFiltering=false';var UPPHOTO_BASE='https://blog.upphoto.naver.com';var SK_URL='https://platform.editor.naver.com/api/blogpc001/v1/photo-uploader/session-key';var SK_MATCH='photo-uploader/session-key';var L=function(){try{console.log.apply(console,['[nv]'].concat([].slice.call(arguments)))}catch(e){}};function exK(t){if(!t)return null;var m=/"sessionKey"\s*:\s*"([^"]+)"/.exec(t);return m&&m[1]?m[1]:null}function gotK(k){if(!k)return;window.__cdSK=k;L('세션키 확보',k);if(window.__cdSKcb){try{window.__cdSKcb(k)}catch(e){}}}function gotA(){if(window.__cdAuthCb){try{window.__cdAuthCb()}catch(e){}}}function setA(name,val){if(!val)return;var n=(''+name).toLowerCase();if(n==='se-authorization'){window.__cdAuth=val;gotA()}else if(n==='se-app-id'){window.__cdAppId=val;gotA()}}function grabA(h){try{if(!h)return;if(Object.prototype.toString.call(h)==='[object Array]'){for(var i=0;i<h.length;i++){if(h[i]&&h[i].length>=2)setA(h[i][0],h[i][1])}}else if(typeof h.forEach==='function'){h.forEach(function(v,k){setA(k,v)})}else{for(var k in h){if(Object.prototype.hasOwnProperty.call(h,k))setA(k,h[k])}}}catch(e){}}function sh(h){var o=[];try{if(!h)return o;if(Object.prototype.toString.call(h)==='[object Array]'){for(var i=0;i<h.length;i++){if(h[i]&&h[i].length>=2)o.push(h[i][0]+': '+h[i][1])}}else if(typeof h.forEach==='function'){h.forEach(function(v,k){o.push(k+': '+v)})}else{for(var k in h){if(Object.prototype.hasOwnProperty.call(h,k))o.push(k+': '+h[k])}}}catch(e){}return o}function recR(method,url,hl){var info={method:method||'GET',url:url||'',headers:hl||[]};window.__cdReq=info;L('session-key 요청 캡처',info);if(window.__cdReqCb){try{window.__cdReqCb(info)}catch(e){}}}function itcp(){if(window.__cdItcp){L('인터셉터 이미 설치됨');return}window.__cdItcp=true;try{var of=window.fetch;if(typeof of==='function'){window.fetch=function(){var a=arguments,url;try{url=(a[0]&&a[0].url)?a[0].url:(''+a[0])}catch(e){url=''}var a0=a[0],a1=a[1];try{if(a1&&a1.headers)grabA(a1.headers);if(typeof Request!=='undefined'&&a0 instanceof Request)grabA(a0.headers)}catch(e){}try{if(url&&url.indexOf(SK_MATCH)!==-1){var mth='GET',hd=[];if(a1){if(a1.method)mth=a1.method;if(a1.headers)hd=hd.concat(sh(a1.headers))}if(typeof Request!=='undefined'&&a0 instanceof Request){if(a0.method)mth=a0.method;hd=hd.concat(sh(a0.headers))}recR(mth,url,hd)}}catch(e){}var p=of.apply(this,a);try{if(url&&url.indexOf(SK_MATCH)!==-1&&p&&p.then){p.then(function(r){try{r.clone().text().then(function(t){gotK(exK(t))}).catch(function(){})}catch(e){}}).catch(function(){})}}catch(e){}return p};L('fetch 인터셉터 설치')}}catch(e){L('fetch 패치 실패',e)}try{var oo=XMLHttpRequest.prototype.open,osh=XMLHttpRequest.prototype.setRequestHeader,os=XMLHttpRequest.prototype.send;XMLHttpRequest.prototype.open=function(m,url){try{this.__cdMethod=m;this.__cdUrl=url;this.__cdHdrs=[]}catch(e){}return oo.apply(this,arguments)};XMLHttpRequest.prototype.setRequestHeader=function(n,v){try{if(!this.__cdHdrs)this.__cdHdrs=[];this.__cdHdrs.push(n+': '+v);setA(n,v)}catch(e){}return osh.apply(this,arguments)};XMLHttpRequest.prototype.send=function(){try{var x=this;if(x.__cdUrl&&(''+x.__cdUrl).indexOf(SK_MATCH)!==-1){recR(x.__cdMethod,''+x.__cdUrl,x.__cdHdrs||[]);x.addEventListener('load',function(){try{gotK(exK(x.responseText||''))}catch(e){}})}}catch(e){}return os.apply(this,arguments)};L('XHR 인터셉터 설치')}catch(e){L('XHR 패치 실패',e)}}function getSK(){if(window.__cdSK){L('세션키 재사용',window.__cdSK);return Promise.resolve(window.__cdSK)}if(window.__cdAuth){L('세션키 직접 요청');var hd={'accept':'application/json','se-authorization':window.__cdAuth};if(window.__cdAppId)hd['se-app-id']=window.__cdAppId;return fetch(SK_URL,{credentials:'include',headers:hd}).then(function(r){if(!r.ok)throw new Error('session-key HTTP '+r.status);return r.json()}).then(function(j){L('세션키 응답',j);var k=j&&j.sessionKey;if(!k)throw new Error('세션키 없음');window.__cdSK=k;return k})}return Promise.reject(new Error('에디터 토큰을 아직 못 잡았어 — 에디터를 한 번 클릭하거나 사진 1장 추가 후 다시 처리해줘'))}function gb(){try{var m=location.pathname.match(/^\/([A-Za-z0-9_-]+)(?:\/|$)/);if(m&&m[1]&&m[1].indexOf('.naver')===-1)return m[1]}catch(e){}return BLOG_ID_FALLBACK}function d2b(d){var c=d.indexOf(','),h=d.slice(0,c),b=d.slice(c+1),mm=(h.match(/data:([^;]+)/)||[])[1]||'image/jpeg',bin=atob(b),n=bin.length,a=new Uint8Array(n);for(var i=0;i<n;i++)a[i]=bin.charCodeAt(i);return new Blob([a],{type:mm})}function pu(x){var el=null;try{var doc=new DOMParser().parseFromString(x,'text/xml');el=doc.querySelector('url')||doc.getElementsByTagName('url')[0]}catch(e){}var r=el&&el.textContent?el.textContent.trim():'';if(!r){var m=x.match(/<url>([^<]+)<\/url>/i);r=m?m[1].trim():''}if(!r)return null;if(/^https?:\/\//i.test(r))return r;var a=DISPLAY_HOST_PREFIX+(r.charAt(0)==='/'?'':'/')+r;if(!IMG_TYPE_QUERY)return a;var q=IMG_TYPE_QUERY.replace(/^[?&]/,'');return a+(a.indexOf('?')===-1?'?':'&')+q}function rs(h,f,t){var o=h,v=[f,f.replace(/&/g,'&amp;')];for(var i=0;i<v.length;i++){o=o.split('"'+v[i]+'"').join('"'+t+'"');o=o.split("'"+v[i]+"'").join("'"+t+"'")}return o}function wc(h){if(window.ClipboardItem&&navigator.clipboard&&navigator.clipboard.write){return navigator.clipboard.write([new ClipboardItem({'text/html':new Blob([h],{type:'text/html'}),'text/plain':new Blob([''],{type:'text/plain'})})])}if(navigator.clipboard&&navigator.clipboard.writeText){return navigator.clipboard.writeText(h)}return Promise.reject(new Error('clipboard'))}function up(k,b,bl,i){var u=UPPHOTO_BASE+'/'+k+'/simpleUpload/0?userId='+encodeURIComponent(b)+'&'+UPPHOTO_QUERY;L('업로드['+i+']',u,bl.size);return fetch(u,{method:'POST',credentials:'include',body:bl}).then(function(r){return r.text().then(function(t){L('업로드['+i+'] status',r.status);if(!r.ok)throw new Error('upphoto HTTP '+r.status+' '+t.slice(0,160));var nu=pu(t);L('업로드['+i+'] url',nu);if(!nu)throw new Error('URL 못 찾음');return nu})})}function fmtR(info){if(!info)return '';var l=[(info.method||'GET')+' '+(info.url||'')];if(info.headers&&info.headers.length){l.push('');for(var i=0;i<info.headers.length;i++)l.push(info.headers[i])}else{l.push('(보이는 커스텀 헤더 없음 — 쿠키/HttpOnly는 스크립트에서 안 보임)')}return l.join('\n')}function el(tag,css,text){var e=document.createElement(tag);if(css)e.style.cssText=css;if(text!=null)e.textContent=text;return e}function ui(){var old=document.getElementById('cd-nv-ov');if(old&&old.parentNode)old.parentNode.removeChild(old);var ov=el('div',"position:fixed;left:50%;top:16px;transform:translateX(-50%);z-index:2147483647;width:560px;max-width:94vw;background:#fff;color:#222;border:1px solid #e5e5e5;border-radius:12px;box-shadow:0 8px 40px rgba(0,0,0,.3);padding:16px;font-family:-apple-system,BlinkMacSystemFont,'Malgun Gothic',sans-serif;font-size:14px;line-height:1.5;max-height:90vh;overflow:auto;");ov.id='cd-nv-ov';['keydown','keyup','keypress','click','mousedown','paste'].forEach(function(ev){ov.addEventListener(ev,function(e){e.stopPropagation()},false)});ov.appendChild(el('div','font-weight:800;font-size:15px;margin-bottom:8px;','🖼 우리 후기 사진 → 네이버 업로드'));ov.appendChild(el('div','color:#444;margin-bottom:4px;',"① (토큰 자동확보 안 되면) 에디터를 한 번 클릭하거나 사진 1장 추가 → '토큰 확보 ✓'"));ov.appendChild(el('div','color:#444;margin-bottom:4px;','② 아래 칸에 앱 페이로드 붙여넣기 (Ctrl+V 또는 길게 눌러 붙여넣기)'));ov.appendChild(el('div','color:#444;margin-bottom:8px;','③ 처리 ▶ (임시로 넣은 사진은 나중에 지워도 됨)'));var ta=el('textarea','width:100%;height:70px;box-sizing:border-box;border:1px solid #ccc;border-radius:8px;padding:8px;font-size:12px;resize:vertical;');ta.setAttribute('placeholder','여기에 붙여넣기 (Ctrl+V / 길게 눌러 붙여넣기)');ov.appendChild(ta);var st=el('div','margin:8px 0;min-height:1.2em;color:#333;font-weight:700;');ov.appendChild(st);var ac=el('div','display:flex;align-items:center;gap:8px;margin-top:2px;');var go=el('button','background:#e64980;color:#fff;border:0;border-radius:8px;padding:9px 16px;font-weight:800;cursor:pointer;','처리 ▶');var cl=el('button','background:#eee;color:#333;border:0;border-radius:8px;padding:9px 14px;cursor:pointer;','닫기');ac.appendChild(go);ac.appendChild(cl);ov.appendChild(ac);var rw=el('div','margin-top:10px;display:none;');var cb=el('button','background:#1c7ed6;color:#fff;border:0;border-radius:8px;padding:9px 16px;font-weight:800;cursor:pointer;','📋 결과 복사');var rt=el('textarea','width:100%;height:70px;box-sizing:border-box;border:1px solid #ccc;border-radius:8px;padding:8px;font-size:11px;margin-top:8px;resize:vertical;');rw.appendChild(cb);rw.appendChild(rt);rw.appendChild(el('div','color:#666;font-size:12px;margin-top:4px;','복사가 안 되면 위 칸을 전체선택(Ctrl+A)→복사(Ctrl+C)해서 붙여넣어'));ov.appendChild(rw);var rd=el('details','margin-top:12px;border-top:1px solid #eee;padding-top:8px;');rd.appendChild(el('summary','cursor:pointer;font-weight:700;color:#555;','🔍 에디터 session-key 요청 (참고용)'));var qt=el('textarea','width:100%;height:96px;box-sizing:border-box;border:1px solid #ccc;border-radius:8px;padding:8px;font-size:11px;margin-top:8px;resize:vertical;background:#fafafa;');qt.readOnly=true;qt.setAttribute('placeholder','아직 안 잡힘 — 에디터에서 사진 1장 추가하면 요청 정보가 여기 채워져요 (선택/복사 가능)');rd.appendChild(qt);ov.appendChild(rd);document.body.appendChild(ov);setTimeout(function(){try{ta.focus()}catch(e){}},50);cl.addEventListener('click',function(){if(ov.parentNode)ov.parentNode.removeChild(ov)});return{ov:ov,ta:ta,status:st,go:go,resultWrap:rw,copyBtn:cb,resultTa:rt,reqTa:qt}}function ss(u,m,er){u.status.textContent=m;u.status.style.color=er?'#c0392b':'#333'}function fin(u,h,done,total){ss(u,'완료 ('+done+'/'+total+') — 아래 버튼 눌러 복사한 뒤 본문에 붙여넣어');u.resultTa.value=h;u.resultWrap.style.display='block';u.go.disabled=false;u.copyBtn.onclick=function(){wc(h).then(function(){ss(u,'복사됨 ✓ — 네이버 본문에 Ctrl+V로 붙여넣어')},function(err){L('write 실패',err);ss(u,'자동복사 실패 — 아래 칸 전체선택 후 복사해줘',true);try{u.resultTa.focus();u.resultTa.select()}catch(e){}})}}function proc(u){var raw=(u.ta.value||'').trim();var p;try{p=JSON.parse(raw)}catch(e){p=null}if(!p||!p.copy_html){ss(u,"페이로드가 안 보여 — 앱에서 '네이버로 보내기' 후 이 칸에 붙여넣어줘",true);return}var h=p.copy_html,imgs=Array.isArray(p.images)?p.images:[];L('이미지',imgs.length);u.go.disabled=true;if(!imgs.length){fin(u,h,0,0);return}ss(u,'세션키 준비 중…');getSK().then(function(key){L('세션키',key);var b=gb();L('blogId',b);var ch=Promise.resolve(),done=0;imgs.forEach(function(img,i){ch=ch.then(function(){ss(u,'사진 업로드 중… ('+(i+1)+'/'+imgs.length+')');var bl=d2b(img.dataUri);return up(key,b,bl,i).then(function(nu){h=rs(h,img.src,nu);done++}).catch(function(e){L('img'+i+' 실패',e);ss(u,'사진 '+(i+1)+' 실패 — 화면 로그 확인 (계속 진행)',true)})})});return ch.then(function(){fin(u,h,done,imgs.length)})}).catch(function(e){L('세션키 실패',e);ss(u,(e&&e.message?e.message:''+e),true);u.go.disabled=false})}var TOKEN_OK='토큰 확보 ✓ (페이로드 붙여넣고 처리)';var WAITING='대기 중 — 에디터를 한 번 클릭(또는 사진 1장 추가)하면 토큰을 잡아요';var U=ui();itcp();window.__cdSKcb=function(){ss(U,TOKEN_OK)};window.__cdAuthCb=function(){ss(U,TOKEN_OK)};window.__cdReqCb=function(info){try{U.reqTa.value=fmtR(info)}catch(e){}};ss(U,(window.__cdAuth||window.__cdSK)?TOKEN_OK:WAITING);if(window.__cdReq){try{U.reqTa.value=fmtR(window.__cdReq)}catch(e){}}U.go.addEventListener('click',function(){try{proc(U)}catch(e){L('처리 예외',e);ss(U,'오류: '+(e&&e.message?e.message:e),true)}});L('오버레이 준비됨');})();
 */
