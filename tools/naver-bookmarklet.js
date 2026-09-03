/*
 * 우리의 하루 — 네이버 후기 사진 업로더 (PLAIN BOOKMARKLET, PHASE 1)
 * ==========================================================================
 * 확장/Tampermonkey 없이 '즐겨찾기(북마클릿)' 하나로 동작한다. 폰에서도 됨.
 *
 * 세션키 얻는 법(우선순위):
 *   (a) 에디터가 부른 session-key '응답'에서 잡은 sessionKey(가로챈 것)를 재사용.
 *   (b) 아무 요청 헤더에서 잡은 se-authorization(+se-app-id)로 우리가 직접 GET.
 *   (c) 위가 다 안 되면, 오버레이의 '🔑 토큰 수동입력'에 붙여넣은 se-authorization
 *       으로 우리가 직접 GET(보장 폴백).
 *   se-authorization = 약 1시간 유효 HS256 JWT(재사용 가능). se-app-id = SE-… 문자열.
 *
 * 왜 iframe까지 패치하나:
 *   - 네이버 SmartEditor는 iframe 안에서 도는 경우가 많아, top window의 fetch/XHR만
 *     후킹하면 에디터 요청을 아예 못 본다. 그래서 도달 가능한 same-origin 프레임을
 *     모두 패치하고, 늦게 뜨는 iframe도 재스캔(setInterval + MutationObserver)한다.
 *     cross-origin 프레임은 접근 불가라 스킵하고 진단에 남긴다.
 *
 * 왜 오버레이 + 붙여넣기(textarea)인가(폰/포커스 안전):
 *   - clipboard.readText()는 포커스가 필요해 북마클릿 클릭 직후 막힌다. 그래서 읽지
 *     않고 textarea에 직접 붙여넣게 한다. 최종 결과 HTML도 '📋 결과 복사'(새 제스처)
 *     에서만 쓴다. 진행/오류는 전부 오버레이 + 콘솔에.
 *
 * ⚠️ CONFIG(라이브 튜닝): DISPLAY_HOST_PREFIX / IMG_TYPE_QUERY / BLOG_ID_FALLBACK /
 *   UPPHOTO_QUERY. 정본 한 줄은 sibling tools/naver-bookmarklet.txt.
 */
(function () {
  'use strict';

  // ======================= CONFIG (라이브에서 조정) =========================
  var DISPLAY_HOST_PREFIX = 'https://blogfiles.pstatic.net';
  var IMG_TYPE_QUERY = '?type=w1';
  var BLOG_ID_FALLBACK = 'yhc9355';
  var UPPHOTO_QUERY =
    'extractExif=true&extractAnimatedCnt=false&extractAnimatedInfo=true' +
    '&autorotate=true&extractDominantColor=false&type=&customQuery=' +
    '&denyAnimatedImage=false&skipXcamFiltering=false';
  var UPPHOTO_BASE = 'https://blog.upphoto.naver.com';
  var SESSION_KEY_URL =
    'https://platform.editor.naver.com/api/blogpc001/v1/photo-uploader/session-key';
  var SESSION_KEY_MATCH = 'session-key';      // 요청/응답 URL 매칭 힌트(넓게)
  // ========================================================================

  var L = function () {
    try { console.log.apply(console, ['[nv]'].concat([].slice.call(arguments))); } catch (e) {}
  };

  // 진단 로그(오버레이 🔍 패널 + 콘솔). window에 쌓아 재클릭에도 유지.
  function diag(s) {
    try {
      if (!window.__cdDiag) window.__cdDiag = [];
      window.__cdDiag.push(s);
      if (window.__cdDiag.length > 200) window.__cdDiag = window.__cdDiag.slice(-200);
      L('[diag]', s);
      if (window.__cdDiagCb) { try { window.__cdDiagCb(); } catch (e) {} }
    } catch (e) {}
  }
  // 같은 key(주로 iframe src)로는 최초 1회만 diag — itcp 재스캔(1s×30)+MutationObserver로
  // cross-origin 스킵 로그가 매초 쏟아져 진짜 로그를 밀어내는 것을 막는다.
  var __cdSeenXO = {};
  function diagOnce(key, msg) { if (__cdSeenXO[key]) return; __cdSeenXO[key] = true; diag(msg); }

  // --------- 캡처 상태 ---------
  function extractSessionKey(text) {
    if (!text) return null;
    var m = /"sessionKey"\s*:\s*"([^"]+)"/.exec(text);
    return m && m[1] ? m[1] : null;
  }
  function gotKey(k) {
    if (!k || window.__cdSK === k) return;
    window.__cdSK = k;
    L('세션키 확보', k);
    diag('✓ sessionKey 확보 (' + ('' + k).slice(0, 12) + '…)');
    if (window.__cdSKcb) { try { window.__cdSKcb(k); } catch (e) {} }
  }
  function gotAuth() { if (window.__cdAuthCb) { try { window.__cdAuthCb(); } catch (e) {} } }

  // 에디터 컨텍스트(iframe) 지정 — 우리 fetch도 이 창에서 돌려 referer/origin을 맞춘다.
  function markEditorWin(win, why) {
    try {
      if (win && window.__cdEditorWin !== win) {
        window.__cdEditorWin = win;
        diag('에디터 프레임 지정: ' + why);
      }
    } catch (e) {}
  }
  function edWin() { return window.__cdEditorWin || window; }

  // 한 헤더가 se-authorization/se-app-id면 보관(대소문자 무시). se-authorization이면 true.
  function setAuthHeader(name, value) {
    if (!value) return false;
    var n = ('' + name).toLowerCase();
    if (n === 'se-authorization') {
      if (window.__cdAuth !== value) {
        window.__cdAuth = value;
        diag('✓ se-authorization 확보 (' + ('' + value).slice(0, 12) + '…)');
        gotAuth();
      }
      return true;
    }
    if (n === 'se-app-id') {
      if (window.__cdAppId !== value) { window.__cdAppId = value; gotAuth(); }
    }
    return false;
  }
  // 헤더 컬렉션(Headers / 배열쌍 / 객체)에서 se-* 훑기. se-authorization 봤으면 true.
  function grabAuth(h) {
    var saw = false;
    try {
      if (!h) return false;
      if (Object.prototype.toString.call(h) === '[object Array]') {
        for (var i = 0; i < h.length; i++) {
          if (h[i] && h[i].length >= 2 && setAuthHeader(h[i][0], h[i][1])) saw = true;
        }
      } else if (typeof h.forEach === 'function') {
        h.forEach(function (v, k) { if (setAuthHeader(k, v)) saw = true; });
      } else {
        for (var k in h) {
          if (Object.prototype.hasOwnProperty.call(h, k) && setAuthHeader(k, h[k])) saw = true;
        }
      }
    } catch (e) {}
    return saw;
  }
  // 헤더 컬렉션 → "name: value" 라인 배열(진단 표시용).
  function serializeHeaders(h) {
    var out = [];
    try {
      if (!h) return out;
      if (Object.prototype.toString.call(h) === '[object Array]') {
        for (var i = 0; i < h.length; i++) {
          if (h[i] && h[i].length >= 2) out.push(h[i][0] + ': ' + h[i][1]);
        }
      } else if (typeof h.forEach === 'function') {
        h.forEach(function (v, k) { out.push(k + ': ' + v); });
      } else {
        for (var k in h) {
          if (Object.prototype.hasOwnProperty.call(h, k)) out.push(k + ': ' + h[k]);
        }
      }
    } catch (e) {}
    return out;
  }
  function recordReq(method, url, headerLines) {
    window.__cdReq = { method: method || 'GET', url: url || '', headers: headerLines || [] };
    diag('요청상세: ' + (method || 'GET') + ' ' + url);
    if (headerLines && headerLines.length) {
      for (var i = 0; i < headerLines.length; i++) diag('  ' + headerLines[i]);
    }
  }

  // Request-비슷한 첫 인자(다른 realm 대비 duck-typing).
  function isRequestLike(a0) {
    return !!(a0 && typeof a0 === 'object' && a0.headers && typeof a0.url === 'string');
  }

  // --------- 한 window(프레임)에 fetch + XHR 후킹 설치 ---------
  // 신규 패치했으면 true, 이미 패치됐거나 접근 불가면 false(멱등).
  function patchWindow(win, label) {
    if (!win) return false;
    try {
      if (win.__cdItcp) return false;   // 이미 패치됨(멱등) → newly=false
      win.__cdItcp = true;              // cross-origin이면 여기서 throw
    } catch (e) { diagOnce('flag:' + label, '프레임 접근불가(플래그): ' + label); return false; }
    // 에디터 iframe(PostWriteForm)이면 우리 요청도 이 창에서 돌리도록 지정.
    try { var href = ''; try { href = win.location.href || ''; } catch (e2) {}
      if (href.indexOf('PostWriteForm') !== -1) markEditorWin(win, 'PostWriteForm 프레임'); } catch (e) {}
    // fetch
    try {
      var of = win.fetch;
      if (typeof of === 'function') {
        win.fetch = function () {
          var args = arguments, url;
          try { url = (args[0] && args[0].url) ? args[0].url : ('' + args[0]); }
          catch (e) { url = ''; }
          var a0 = args[0], a1 = args[1], sawAuth = false;
          try {
            if (a1 && a1.headers) sawAuth = grabAuth(a1.headers) || sawAuth;
            if (isRequestLike(a0)) sawAuth = grabAuth(a0.headers) || sawAuth;
          } catch (e) {}
          var isSK = url && url.indexOf(SESSION_KEY_MATCH) !== -1;
          if (isSK || sawAuth) {
            var method = 'GET', hdrs = [];
            try {
              if (a1) { if (a1.method) method = a1.method; if (a1.headers) hdrs = hdrs.concat(serializeHeaders(a1.headers)); }
              if (isRequestLike(a0)) { if (a0.method) method = a0.method; hdrs = hdrs.concat(serializeHeaders(a0.headers)); }
            } catch (e) {}
            diag('[' + label + '] ' + method + ' ' + url + ' — auth=' + sawAuth + ' sk=' + (!!window.__cdSK));
            if (sawAuth) markEditorWin(win, label + ' (se-authorization)');
            if (isSK) recordReq(method, url, hdrs);
          }
          var p = of.apply(win, args);
          try {
            if (isSK && p && p.then) {
              p.then(function (r) {
                try { r.clone().text().then(function (t) { gotKey(extractSessionKey(t)); }).catch(function () {}); }
                catch (e) {}
              }).catch(function () {});
            }
          } catch (e) {}
          return p;
        };
      }
    } catch (e) { L('fetch 패치 실패(' + label + ')', e); }
    // XMLHttpRequest (프레임별 고유 prototype)
    try {
      var XHR = win.XMLHttpRequest;
      if (XHR && XHR.prototype && !XHR.prototype.__cdPatched) {
        XHR.prototype.__cdPatched = true;
        var oOpen = XHR.prototype.open, oSetHdr = XHR.prototype.setRequestHeader,
            oSend = XHR.prototype.send;
        XHR.prototype.open = function (method, url) {
          try { this.__cdMethod = method; this.__cdUrl = url; this.__cdHdrs = []; this.__cdSawAuth = false; }
          catch (e) {}
          return oOpen.apply(this, arguments);
        };
        XHR.prototype.setRequestHeader = function (name, value) {
          try {
            if (!this.__cdHdrs) this.__cdHdrs = [];
            this.__cdHdrs.push(name + ': ' + value);
            if (setAuthHeader(name, value)) this.__cdSawAuth = true;
          } catch (e) {}
          return oSetHdr.apply(this, arguments);
        };
        XHR.prototype.send = function () {
          try {
            var xhr = this;
            var isSK = xhr.__cdUrl && ('' + xhr.__cdUrl).indexOf(SESSION_KEY_MATCH) !== -1;
            if (isSK || xhr.__cdSawAuth) {
              diag('[' + label + '/xhr] ' + (xhr.__cdMethod || '') + ' ' + xhr.__cdUrl +
                ' — auth=' + (!!xhr.__cdSawAuth) + ' sk=' + (!!window.__cdSK));
              if (xhr.__cdSawAuth) markEditorWin(win, label + ' (se-authorization xhr)');
              if (isSK) recordReq(xhr.__cdMethod, '' + xhr.__cdUrl, xhr.__cdHdrs || []);
            }
            if (isSK) {
              xhr.addEventListener('load', function () {
                try {
                  var k = null, rt = xhr.responseType;
                  if (rt === 'json' && xhr.response) {
                    k = xhr.response && xhr.response.sessionKey;    // json은 response 직접
                  } else if (rt === '' || rt === 'text') {
                    k = extractSessionKey(xhr.responseText || '');  // text는 responseText 파싱
                  }
                  if (k) gotKey(k);
                } catch (e) { L('xhr load 읽기 실패', e); }
              });
            }
          } catch (e) {}
          return oSend.apply(this, arguments);
        };
      }
    } catch (e) { L('XHR 패치 실패(' + label + ')', e); }
    diag('프레임 패치 ✓ ' + label);
    return true;
  }

  // 도달 가능한 same-origin iframe을 재귀로 패치(cross-origin은 스킵+진단).
  function scanFrames(win, depth) {
    if (depth > 6) return 0;
    var frames;
    try { frames = win.document.querySelectorAll('iframe'); } catch (e) { return 0; }
    var n = 0;
    for (var i = 0; i < frames.length; i++) {
      var fr = frames[i], src = '(inline)';
      try { src = fr.getAttribute('src') || fr.src || '(inline)'; } catch (e) { src = '?'; }
      var cw = null;
      try { cw = fr.contentWindow; } catch (e) { L('iframe 접근불가:', src); continue; }
      if (!cw) continue;
      var acc = true;
      try { void cw.location.href; } catch (e) { acc = false; }
      if (!acc) { L('iframe cross-origin 스킵:', src); continue; }
      if (patchWindow(cw, 'iframe:' + src)) n++;
      n += scanFrames(cw, depth + 1);
    }
    return n;
  }
  function patchEverything() {
    var n = 0;
    if (patchWindow(window, 'top')) n++;
    n += scanFrames(window, 0);
    if (n > 0) diag('프레임 신규 패치: ' + n + '개');
    return n;
  }
  function installInterceptor() {
    patchEverything();
    // 늦게 뜨는/추가되는 same-origin iframe 대비 재스캔(~30초) + MutationObserver.
    var tries = 0;
    var iv = setInterval(function () {
      tries++;
      try { patchEverything(); } catch (e) {}
      if (tries >= 30) clearInterval(iv);
    }, 1000);
    try {
      if (typeof MutationObserver !== 'undefined') {
        var mo = new MutationObserver(function () { try { patchEverything(); } catch (e) {} });
        mo.observe(document.documentElement || document.body || document,
          { childList: true, subtree: true });
      }
    } catch (e) {}
  }

  // 세션키 확보: (a) 가로챈 __cdSK, (b) 가로챈 __cdAuth, (c) 수동 입력값 순.
  function getSessionKey(manualAuth, manualAppId) {
    if (window.__cdSK) {
      diag('세션키: 재사용(가로챈 응답)');
      return Promise.resolve(window.__cdSK);
    }
    var auth = window.__cdAuth || manualAuth;
    var appId = window.__cdAppId || manualAppId;
    if (auth) {
      diag('세션키: 직접요청 ' + (window.__cdAuth ? '(가로챈 JWT)' : '(수동)'));
      var hdrs = { 'accept': 'application/json', 'se-authorization': auth };
      if (appId) hdrs['se-app-id'] = appId;
      // 에디터 iframe 컨텍스트에서 GET(referer/origin을 에디터와 맞춘다).
      return edWin().fetch(SESSION_KEY_URL, { credentials: 'include', headers: hdrs })
        .then(function (r) {
          diag('session-key HTTP ' + r.status);
          if (!r.ok) throw new Error('session-key HTTP ' + r.status);
          return r.json();
        })
        .then(function (j) {
          var k = j && j.sessionKey;
          if (!k) throw new Error('세션키 없음(응답에 sessionKey 없음)');
          window.__cdSK = k;
          return k;
        });
    }
    return Promise.reject(new Error(
      '토큰을 못 잡았어 — 에디터를 한 번 클릭/사진 추가하거나, 아래 🔑 토큰 수동입력에 se-authorization을 붙여넣어줘'));
  }

  // --------- 네이버 통신/변환 유틸 ---------
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
    if (/^https?:\/\//i.test(raw)) return raw;
    var abs = DISPLAY_HOST_PREFIX + (raw.charAt(0) === '/' ? '' : '/') + raw;
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
  // upphoto <item> XML에서 한 필드 텍스트 추출(없으면 '').
  function xmlField(x, tag) {
    try {
      var m = x.match(new RegExp('<' + tag + '>([^<]*)</' + tag + '>', 'i'));
      return m ? m[1].trim() : '';
    } catch (e) { return ''; }
  }
  function uploadOne(sessionKey, blogId, blob, idx) {
    var url = UPPHOTO_BASE + '/' + sessionKey + '/simpleUpload/0?userId=' +
      encodeURIComponent(blogId) + '&' + UPPHOTO_QUERY;
    L('업로드[' + idx + '] POST', url, blob.size + 'B');
    // 네이버 에디터처럼 multipart/form-data(필드명 'image')로 보낸다. Content-Type은
    // 브라우저가 boundary와 함께 자동 설정하므로 수동 지정하지 않는다.
    var fd = new FormData();
    fd.append('image', blob, 'image.jpg');
    diag('업로드[' + idx + '] multipart image 필드 전송');
    // 에디터 iframe 컨텍스트에서 POST(referer/origin을 에디터와 맞춘다).
    return edWin().fetch(url, { method: 'POST', credentials: 'include', body: fd })
      .then(function (r) {
        return r.text().then(function (t) {
          diag('업로드[' + idx + '] HTTP ' + r.status);
          if (!r.ok) throw new Error('upphoto HTTP ' + r.status + ' — ' + t.slice(0, 160));
          var displayUrl = parseUploadedUrl(t);  // ?type=w1 붙은 표시 URL(붙여넣기용)
          if (!displayUrl) throw new Error('응답에서 URL 못 찾음(' + t.slice(0, 120) + ')');
          // 내부(internal) 이미지 컴포넌트 구성에 필요한 전체 메타(다음 단계용).
          var meta = {
            displayUrl: displayUrl,
            path: xmlField(t, 'path'),
            width: xmlField(t, 'width'),
            height: xmlField(t, 'height'),
            fileSize: xmlField(t, 'fileSize'),
            fileName: xmlField(t, 'fileName'),
            imageType: xmlField(t, 'imageType'),
            thumbnail: xmlField(t, 'thumbnail')
          };
          if (!window.__cdUploads) window.__cdUploads = [];
          window.__cdUploads.push(meta);
          diag('업로드[' + idx + '] path=' + meta.path + ' ' + meta.width + '×' + meta.height +
            ' size=' + meta.fileSize + ' url=' + displayUrl);
          return meta;
        });
      });
  }

  // --------- 에디터 API 탐색(읽기전용) — 주입 진입점 찾기용 ---------
  // SmartEditor 전역을 '실제로' 보유한 window를 찾는다 — 헤더 가로채기로 잡은
  // __cdEditorWin(PostWriteForm 프레임)이 SmartEditor를 가진 프레임과 다를 수 있으므로,
  // getEditor/_editors 가 존재하는 프레임만 자격으로 인정한다.
  function findSeWin() {
    function qualifies(w) {
      try { return !!(w && w.SmartEditor && (typeof w.SmartEditor.getEditor === 'function' || w.SmartEditor._editors)); }
      catch (e) { return false; }
    }
    try { if (qualifies(window.__cdEditorWin)) return window.__cdEditorWin; } catch (e) {}
    try { if (qualifies(window.top)) return window.top; } catch (e) {}
    if (qualifies(window)) return window;
    function walk(w, depth) {
      if (depth > 6) return null;
      var frames;
      try { frames = w.document.querySelectorAll('iframe'); } catch (e) { return null; }
      for (var i = 0; i < frames.length; i++) {
        var cw = null;
        try { cw = frames[i].contentWindow; } catch (e) { continue; }
        if (!cw) continue;
        try { void cw.location.href; } catch (e) { continue; } // cross-origin 스킵
        if (qualifies(cw)) return cw;
        var deep = walk(cw, depth + 1);
        if (deep) return deep;
      }
      return null;
    }
    return walk(window, 0);
  }
  function findEditorFrame() {
    if (window.__cdEditorWin) return window.__cdEditorWin;
    try {
      var frames = document.querySelectorAll('iframe');
      for (var i = 0; i < frames.length; i++) {
        var cw = null;
        try { cw = frames[i].contentWindow; } catch (e) { continue; }
        if (!cw) continue;
        var href = '';
        try { href = cw.location.href || ''; } catch (e) { continue; } // cross-origin
        if (href.indexOf('PostWriteForm') !== -1 || href.indexOf('editor') !== -1) return cw;
      }
    } catch (e) {}
    return null;
  }
  // 후보 전역/API를 읽기만 하고 진단 패널에 덤프한다(아무것도 set 안 함).
  function exploreEditorApi() {
    var win = findSeWin() || findEditorFrame() || window;
    var where = (win === window) ? 'top' : 'editorFrame';
    diag('🔬 API 탐색 시작 — win=' + where);
    diag('SmartEditor 보유 프레임: ' + (win === window.top ? 'top' : (win === window ? 'self' : 'iframe')) + ' · SmartEditor=' + (typeof (win.SmartEditor)));
    var RE = /edit|smart|se3|se2|doc|content|store|nhn|blog|component|image|nsi|__|pmodel|prosemirror/i;
    var keys = [];
    try { keys = Object.keys(win); } catch (e) { diag('키 열거 실패: ' + e); }
    var hits = [];
    for (var i = 0; i < keys.length; i++) { if (RE.test(keys[i])) hits.push(keys[i]); }
    diag('후보 전역 키 ' + hits.length + '개 (전체 ' + keys.length + ')');
    diag('후보: ' + hits.slice(0, 80).join(', '));
    for (var j = 0; j < hits.length && j < 40; j++) {
      var k = hits[j];
      try {
        var v = win[k];
        var ty = typeof v;
        diag('· ' + k + ' : ' + ty);
        if (v && (ty === 'object' || ty === 'function')) {
          var sub = [];
          try { sub = Object.keys(v); } catch (e) {}
          if (sub.length) diag('    [' + k + '] ' + sub.slice(0, 40).join(', '));
        }
      } catch (e) { diag('· ' + k + ' : (접근불가)'); }
    }
    // 흔한 에디터 전역 훅.
    ['se', '__se', 'smartEditor', 'SmartEditor', 'SE', 'editor', 'nhn', '__store__', 'oEditors', 'g_oEditor']
      .forEach(function (g) {
        try { if (g in win) diag('전역 ' + g + ' 존재: ' + (typeof win[g])); } catch (e) {}
      });
    // React 루트/파이버 탐지(에디터 문서에서).
    try {
      var doc = win.document, all = doc.querySelectorAll('*'), rf = 0;
      for (var m = 0; m < all.length && m < 2500; m++) {
        var elm = all[m], own = [];
        try { own = Object.keys(elm); } catch (e) {}
        for (var q = 0; q < own.length; q++) {
          if (own[q].indexOf('__reactContainer') === 0 || own[q] === '_reactRootContainer' ||
              own[q].indexOf('__reactFiber') === 0) {
            rf++;
            if (rf <= 4) diag('React: <' + elm.tagName + ' id=' + (elm.id || '') +
              ' class=' + ('' + (elm.className || '')).slice(0, 40) + '> key=' + own[q]);
            break;
          }
        }
      }
      diag('React 루트/파이버 발견 수(≈): ' + rf);
    } catch (e) { diag('React 스캔 실패: ' + e); }

    // ---- 2단계 드릴: 이미지 삽입 진입점 찾기(읽기전용) ----
    var HL = /image|photo|insert|component|add|content|document|data|model|store|body|paragraph|attach|media|upload/i;
    function dumpObj(label, o, cap) {
      try {
        if (!o) { diag(label + ' : (없음)'); return; }
        var names = [];
        try { names = Object.getOwnPropertyNames(o); } catch (e) { try { names = Object.keys(o); } catch (e2) {} }
        try {  // 프로토타입 체인 ~2단계의 메서드명까지 수집
          var pr = Object.getPrototypeOf(o), depth = 0;
          while (pr && depth < 2) {
            try { Object.getOwnPropertyNames(pr).forEach(function (n) { if (names.indexOf(n) === -1) names.push(n); }); } catch (e) {}
            pr = Object.getPrototypeOf(pr); depth++;
          }
        } catch (e) {}
        diag(label + ' 키(' + names.length + '): ' + names.slice(0, cap || 80).join(', '));
        var hl = names.filter(function (n) { return HL.test(n); });
        if (hl.length) diag(label + ' ★후보: ' + hl.join(', '));
      } catch (e) { diag(label + ' dump 실패: ' + e); }
    }
    try {
      var Sm = win.SmartEditor;
      diag('SmartEditor typeof: ' + (typeof Sm));
      if (Sm) {
        try {
          if (Sm.COMMAND) {
            var ck = Object.keys(Sm.COMMAND);
            diag('SmartEditor.COMMAND(' + ck.length + '): ' + ck.slice(0, 120).join(', '));
            ck.forEach(function (kk) {
              if (HL.test(kk)) { try { diag('  COMMAND.' + kk + ' = ' + JSON.stringify(Sm.COMMAND[kk]).slice(0, 80)); } catch (e) {} }
            });
          }
        } catch (e) { diag('COMMAND 실패: ' + e); }
        try { if (Sm.OPTION) diag('SmartEditor.OPTION: ' + Object.keys(Sm.OPTION).slice(0, 120).join(', ')); } catch (e) {}
        try { if (Sm.PLUGIN) diag('SmartEditor.PLUGIN: ' + Object.keys(Sm.PLUGIN).slice(0, 120).join(', ')); } catch (e) {}
        var ed = null;
        try { if (typeof Sm.getEditor === 'function') ed = Sm.getEditor(); } catch (e) { diag('getEditor() throw: ' + e); }
        if (!ed) {
          try {
            var eds = Sm._editors;
            diag('_editors typeof: ' + (typeof eds));
            if (eds) {
              if (Object.prototype.toString.call(eds) === '[object Array]') { ed = eds[0]; }
              else { var vk = Object.keys(eds); diag('_editors keys: ' + vk.slice(0, 20).join(', ')); if (vk.length) ed = eds[vk[0]]; }
            }
          } catch (e) { diag('_editors 실패: ' + e); }
        }
        if (ed) {
          dumpObj('ed', ed, 80);
          ['getStore', 'store', '_store', 'vm', 'viewModel', 'getDocument', 'document', 'model']
            .forEach(function (sk) {
              try {
                var sv = (typeof ed[sk] === 'function') ? ed[sk]() : ed[sk];
                if (sv) dumpObj('ed.' + sk, sv, 60);
              } catch (e) {}
            });
        } else { diag('ed 없음 — getEditor()/_editors 모두 실패'); }
      }
    } catch (e) { diag('SmartEditor 탐색 실패: ' + e); }
    try {
      var SEg = win.SE;
      if (SEg) {
        var sek = Object.keys(SEg);
        diag('SE 키(' + sek.length + '): ' + sek.slice(0, 120).join(', '));
        sek.forEach(function (kk) {
          if (HL.test(kk)) {
            try { var sv = SEg[kk]; if (sv && typeof sv === 'object') diag('  SE.' + kk + ' 키: ' + Object.keys(sv).slice(0, 40).join(', ')); } catch (e) {}
          }
        });
      }
    } catch (e) { diag('SE 탐색 실패: ' + e); }

    // ---- 3단계: 문서 스키마 + 이미지 커맨드 + 서비스(주입 설계용, 읽기전용) ----
    try {
      if (ed && typeof ed.getDocumentData === 'function') {
        var dd = ed.getDocumentData();
        try { diag('getDocumentData top keys: ' + Object.keys(dd).join(', ')); } catch (e) {}
        try { diag('getDocumentData JSON: ' + JSON.stringify(dd).slice(0, 4000)); }
        catch (e) { diag('getDocumentData stringify 실패: ' + e); }
      } else { diag('ed.getDocumentData 없음'); }
    } catch (e) { diag('getDocumentData() 실패: ' + e); }
    try {
      if (Sm && Sm.COMMAND) {
        if (Sm.COMMAND.IMAGE) diag('COMMAND.IMAGE(full): ' + JSON.stringify(Sm.COMMAND.IMAGE));
        if (Sm.COMMAND.COMMON) diag('COMMAND.COMMON(full): ' + JSON.stringify(Sm.COMMAND.COMMON));
      }
    } catch (e) { diag('COMMAND.IMAGE/COMMON 실패: ' + e); }
    try { if (ed && ed._commandManager) dumpObj('ed._commandManager', ed._commandManager, 80); }
    catch (e) { diag('_commandManager 실패: ' + e); }
    try { if (ed && ed._documentService) dumpObj('ed._documentService', ed._documentService, 80); }
    catch (e) { diag('_documentService 실패: ' + e); }
    try { if (ed && ed._editingService) dumpObj('ed._editingService', ed._editingService, 80); }
    catch (e) { diag('_editingService 실패: ' + e); }

    // ---- 4단계: 커맨드맵 실체 + insert 시그니처 (silent no-op 진단, 읽기전용) ----
    function fnSrc(fn, cap) { try { return String(fn).replace(/\s+/g, ' ').slice(0, cap || 300); } catch (e) { return '(src 실패)'; } }
    // (1) 커맨드맵: 등록된 커맨드 이름 전체 + 이미지 3종 등록 여부
    try {
      var cm = ed && ed._commandManager;
      var cmap = cm && (cm._commandMap || cm.commandMap || cm._commands);
      if (cmap) {
        var cmk = Object.keys(cmap);
        diag('_commandMap keys(' + cmk.length + '): ' + cmk.slice(0, 200).join(', '));
        diag('이미지명령 등록여부: insertImagesByFile=' + (cmap.insertImagesByFile != null) +
          ' insertImages=' + (cmap.insertImages != null) +
          ' insertImagesByUrl=' + (cmap.insertImagesByUrl != null));
      } else { diag('_commandMap 없음 (cm=' + (typeof cm) + ')'); }
    } catch (e) { diag('_commandMap 덤프 실패: ' + e); }
    // (2) 커맨드 객체 형태: 이미지 3종 각각 getCommand → 키/proto/실행함수 소스
    try {
      var cm2 = ed && ed._commandManager;
      ['insertImages', 'insertImagesByFile', 'insertImagesByUrl'].forEach(function (nm) {
        try {
          var c = cm2 && typeof cm2.getCommand === 'function' ? cm2.getCommand(nm) : null;
          if (c == null) { diag('getCommand(' + nm + ') → null/none'); return; }
          var own = []; try { own = Object.getOwnPropertyNames(c); } catch (e) {}
          var proto = []; try { var pp = Object.getPrototypeOf(c); if (pp) proto = Object.getOwnPropertyNames(pp); } catch (e) {}
          diag('getCommand(' + nm + '): ' + (typeof c) + ' own=[' + own.slice(0, 40).join(', ') + '] proto=[' + proto.slice(0, 40).join(', ') + ']');
          ['execute', 'exec', '_execute', 'run'].forEach(function (fn) {
            try { if (c[fn] && typeof c[fn] === 'function') diag('  ' + nm + '.' + fn + ' = ' + fnSrc(c[fn], 300)); } catch (e) {}
          });
        } catch (e) { diag('getCommand(' + nm + ') 실패: ' + e); }
      });
    } catch (e) { diag('getCommand 덤프 실패: ' + e); }
    // (3) execCommand 소스
    try {
      if (ed && ed._commandManager && ed._commandManager.execCommand)
        diag('execCommand src: ' + fnSrc(ed._commandManager.execCommand, 400));
    } catch (e) { diag('execCommand src 실패: ' + e); }
    // (4) loadCommand 소스
    try {
      if (ed && ed._commandManager && ed._commandManager.loadCommand)
        diag('loadCommand src: ' + fnSrc(ed._commandManager.loadCommand, 300));
      else diag('loadCommand 없음');
    } catch (e) { diag('loadCommand src 실패: ' + e); }
    // (5) _editingService insert 계열 시그니처
    try {
      var es = ed && ed._editingService;
      if (es) {
        ['insertByExternalPaste', 'insertByDrop', 'insertComponentsWithData', 'insert', 'insertImagesByFile'].forEach(function (m) {
          try { if (es[m] && typeof es[m] === 'function') diag('_editingService.' + m + ' = ' + fnSrc(es[m], 300)); } catch (e) {}
        });
      } else { diag('_editingService 없음'); }
    } catch (e) { diag('_editingService insert 덤프 실패: ' + e); }
    // (6) 플러그인
    try {
      if (Sm && Sm.PLUGIN) {
        try { diag('SmartEditor.PLUGIN(full): ' + JSON.stringify(Sm.PLUGIN)); }
        catch (e) { diag('SmartEditor.PLUGIN keys: ' + Object.keys(Sm.PLUGIN).join(', ')); }
      }
      if (ed) {
        var ek = []; try { ek = Object.keys(ed); } catch (e) {}
        ek.forEach(function (k) {
          if (/plugin/i.test(k)) { try { dumpObj('ed.' + k, ed[k], 60); } catch (e) {} }
        });
      }
    } catch (e) { diag('플러그인 덤프 실패: ' + e); }
    // (7) 이미지/업로드 서비스
    try {
      if (ed) {
        var names = []; try { names = Object.getOwnPropertyNames(ed); } catch (e) {}
        try { var prt = Object.getPrototypeOf(ed); if (prt) Object.getOwnPropertyNames(prt).forEach(function (n) { if (names.indexOf(n) === -1) names.push(n); }); } catch (e) {}
        names.forEach(function (k) {
          if (!/image|upload|photo/i.test(k)) return;
          try {
            var v = ed[k];
            if (v && typeof v === 'object') {
              var vn = []; try { vn = Object.getOwnPropertyNames(v); } catch (e) {}
              var hits = vn.filter(function (n) { return /upload|insert|add|image|file/i.test(n); });
              diag('ed.' + k + ' (' + vn.length + ') ★' + hits.slice(0, 40).join(', '));
            } else { diag('ed.' + k + ' : ' + (typeof v)); }
          } catch (e) { diag('ed.' + k + ' 접근불가'); }
        });
        try { if (ed._representativeImageService) dumpObj('ed._representativeImageService', ed._representativeImageService, 60); } catch (e) {}
      }
    } catch (e) { diag('이미지/업로드 서비스 덤프 실패: ' + e); }
    // (8) 현재 문서의 기존 image 컴포넌트 전체 JSON(있으면)
    try {
      var ds = ed && ed._documentService;
      if (ds && typeof ds.getComponentsByCtype === 'function') {
        var imgs0 = ds.getComponentsByCtype('image');
        if (imgs0 && imgs0.length) diag('기존 image 컴포넌트[0]: ' + JSON.stringify(imgs0[0]).slice(0, 800));
        else diag('기존 image 컴포넌트 없음');
      }
    } catch (e) { diag('기존 image 컴포넌트 덤프 실패: ' + e); }

    // ---- 5단계: run이 위임하는 _method 실체 + 이미지 업로드 서비스 (읽기전용) ----
    try {
      var cm3 = ed && ed._commandManager;
      ['insertImages', 'insertImagesByFile', 'insertImagesByUrl'].forEach(function (nm) {
        try {
          var c = cm3 && typeof cm3.getCommand === 'function' ? cm3.getCommand(nm) : null;
          if (c == null) { diag('getCommand(' + nm + ') → null/none'); return; }
          try { if (c._method) diag(nm + '._method = ' + fnSrc(c._method, 700)); else diag(nm + '._method 없음'); } catch (e) {}
          try { diag(nm + '._instance ctor = ' + (c._instance && c._instance.constructor && c._instance.constructor.name)); } catch (e) {}
          try {
            if (c._instance) {
              var ip = Object.getPrototypeOf(c._instance);
              if (ip) diag(nm + '._instance proto: ' + Object.getOwnPropertyNames(ip).slice(0, 80).join(', '));
            }
          } catch (e) {}
        } catch (e) { diag(nm + ' _method 덤프 실패: ' + e); }
      });
    } catch (e) { diag('_method 덤프 실패: ' + e); }
    // 중첩 이미지 업로드 서비스 (앞서 발견) — 업로드 진입점 후보.
    try {
      var ius = ed && ed._videoUploadService && ed._videoUploadService._imageUploadService;
      if (ius) {
        dumpObj('_imageUploadService', ius, 80);
        ['upload', 'uploadImages', 'uploadByFile', 'uploadFiles', 'uploadImage', 'uploadImagesByFile'].forEach(function (m) {
          try { if (ius[m] && typeof ius[m] === 'function') diag('_imageUploadService.' + m + ' = ' + fnSrc(ius[m], 500)); } catch (e) {}
        });
      } else { diag('_imageUploadService 없음'); }
    } catch (e) { diag('_imageUploadService 덤프 실패: ' + e); }
    // _editingService 의 진짜 insert 진입 후보.
    try {
      var es2 = ed && ed._editingService;
      if (es2) {
        try { if (es2.insertImagesByFile && typeof es2.insertImagesByFile === 'function') diag('_editingService.insertImagesByFile = ' + fnSrc(es2.insertImagesByFile, 500)); } catch (e) {}
        try { if (es2.getComponentInfoList && typeof es2.getComponentInfoList === 'function') diag('_editingService.getComponentInfoList = ' + fnSrc(es2.getComponentInfoList, 300)); } catch (e) {}
      }
    } catch (e) { diag('_editingService 진입 덤프 실패: ' + e); }

    diag('🔬 API 탐색 끝 — 위 로그를 복사해줘');
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

    var actions = el('div', 'display:flex;align-items:center;gap:8px;margin-top:2px;flex-wrap:wrap;');
    var go = el('button',
      'background:#e64980;color:#fff;border:0;border-radius:8px;padding:9px 16px;' +
      'font-weight:800;cursor:pointer;', '처리 ▶');
    var dataBtn = el('button',
      'background:#12b886;color:#fff;border:0;border-radius:8px;padding:9px 12px;' +
      'font-weight:700;cursor:pointer;', '🅳 데이터URI로 복사 (업로드 없이 테스트)');
    var nativeBtn = el('button',
      'background:#7048e8;color:#fff;border:0;border-radius:8px;padding:9px 12px;' +
      'font-weight:800;cursor:pointer;', '🅢 정식 삽입 (setDocumentData)');
    var explore = el('button',
      'background:#495057;color:#fff;border:0;border-radius:8px;padding:9px 12px;' +
      'font-weight:700;cursor:pointer;', '🔬 에디터 API 탐색');
    var close = el('button',
      'background:#eee;color:#333;border:0;border-radius:8px;padding:9px 14px;cursor:pointer;', '닫기');
    actions.appendChild(go);
    actions.appendChild(nativeBtn);
    actions.appendChild(dataBtn);
    actions.appendChild(explore);
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

    // 🔑 토큰 수동입력(자동이 안 될 때 보장 폴백)
    var manDetails = el('details', 'margin-top:12px;border-top:1px solid #eee;padding-top:8px;');
    manDetails.appendChild(el('summary', 'cursor:pointer;font-weight:700;color:#555;',
      '🔑 토큰 수동입력 (자동이 안 될 때)'));
    manDetails.appendChild(el('div', 'color:#666;font-size:12px;margin:6px 0;',
      'DevTools → session-key 요청의 se-authorization 헤더 값(약 1시간 유효)을 첫 줄에, ' +
      '(선택) se-app-id를 둘째 줄에 붙여넣어.'));
    var manualTa = el('textarea',
      'width:100%;height:64px;box-sizing:border-box;border:1px solid #ccc;' +
      'border-radius:8px;padding:8px;font-size:11px;resize:vertical;');
    manualTa.setAttribute('placeholder', '첫 줄: se-authorization JWT\n둘째 줄(선택): se-app-id');
    manDetails.appendChild(manualTa);
    ov.appendChild(manDetails);

    // 🔍 진단: 무엇을 봤나(프레임 패치·session-key 요청·토큰 캡처) — 폰에서 DevTools 대신.
    var diagDetails = el('details', 'margin-top:10px;border-top:1px solid #eee;padding-top:8px;');
    diagDetails.appendChild(el('summary', 'cursor:pointer;font-weight:700;color:#555;',
      '🔍 진단 로그 (참고용)'));
    var diagTa = el('textarea',
      'width:100%;height:240px;box-sizing:border-box;border:1px solid #ccc;' +
      'border-radius:8px;padding:8px;font-size:11px;margin-top:8px;resize:vertical;background:#fafafa;');
    diagTa.readOnly = true;
    diagTa.setAttribute('placeholder',
      '아직 관측 없음 — 에디터를 클릭/사진 추가하면 여기에 무엇을 봤는지 쌓여요 (선택/복사 가능)');
    diagDetails.appendChild(diagTa);
    ov.appendChild(diagDetails);

    document.body.appendChild(ov);
    setTimeout(function () { try { ta.focus(); } catch (e) {} }, 50);
    close.addEventListener('click', function () {
      if (ov.parentNode) ov.parentNode.removeChild(ov);
    });

    return { ov: ov, ta: ta, status: status, go: go, nativeBtn: nativeBtn, dataBtn: dataBtn, explore: explore,
      resultWrap: resultWrap, copyBtn: copyBtn, resultTa: resultTa, manualTa: manualTa,
      diagTa: diagTa };
  }

  function setStatus(u, msg, isErr) {
    u.status.textContent = msg;
    u.status.style.color = isErr ? '#c0392b' : '#333';
  }

  function finish(u, html, done, total) {
    diag('완료 ' + done + '/' + total);
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
    var nimg = (p && Array.isArray(p.images)) ? p.images.length : 0;
    diag('처리 시작 — 이미지 ' + nimg + '장, 페이로드 ' + (p && p.copy_html ? 'OK' : '없음'));
    if (!p || !p.copy_html) {
      diag('중단: 페이로드 없음');
      setStatus(u, "페이로드가 안 보여 — 앱에서 '네이버로 보내기' 후 이 칸에 붙여넣어줘", true);
      return;
    }
    var html = p.copy_html;
    var imgs = Array.isArray(p.images) ? p.images : [];
    L('페이로드 OK. 이미지', imgs.length);
    u.go.disabled = true;
    if (!imgs.length) { finish(u, html, 0, 0); return; }

    var manual = (u.manualTa.value || '').trim();
    var manualAuth = '', manualAppId = '';
    if (manual) {
      var mlines = manual.split(/\r?\n/);
      manualAuth = (mlines[0] || '').trim();
      manualAppId = (mlines[1] || '').trim();
    }

    setStatus(u, '세션키 준비 중…');
    getSessionKey(manualAuth, manualAppId).then(function (key) {
      L('세션키', key);
      var blogId = guessBlogId();
      L('blogId', blogId);
      var chain = Promise.resolve();
      var done = 0;
      imgs.forEach(function (img, i) {
        chain = chain.then(function () {
          setStatus(u, '사진 업로드 중… (' + (i + 1) + '/' + imgs.length + ')');
          var blob = dataUriToBlob(img.dataUri);
          return uploadOne(key, blogId, blob, i).then(function (meta) {
            html = replaceSrc(html, img.src, meta.displayUrl);
            done++;
          }).catch(function (e) {
            L('img' + i + ' 실패', e);
            diag('업로드[' + i + '] 실패: ' + (e && e.message ? e.message : e));
            setStatus(u, '사진 ' + (i + 1) + ' 실패 — 진단 로그 확인 (계속 진행)', true);
          });
        });
      });
      return chain.then(function () { finish(u, html, done, imgs.length); });
    }).catch(function (e) {
      L('세션키 실패', e);
      diag('세션키 실패: ' + (e && e.message ? e.message : e));
      setStatus(u, (e && e.message ? e.message : '' + e), true);
      u.go.disabled = false;
    });
  }

  // 업로드 없이: 각 이미지 src를 그 이미지의 dataUri로 바꿔 심고 클립보드에.
  // 네이버 에디터가 붙여넣기 시 data:이미지를 스스로 업로드/등록(internal)하는지 테스트.
  function processDataUri(u) {
    var raw = (u.ta.value || '').trim();
    var p;
    try { p = JSON.parse(raw); } catch (e) { p = null; }
    var nimg = (p && Array.isArray(p.images)) ? p.images.length : 0;
    diag('데이터URI 모드 — 이미지 ' + nimg + '장, 페이로드 ' + (p && p.copy_html ? 'OK' : '없음'));
    if (!p || !p.copy_html) {
      diag('중단: 페이로드 없음');
      setStatus(u, "페이로드가 안 보여 — 앱에서 '네이버로 보내기' 후 이 칸에 붙여넣어줘", true);
      return;
    }
    var html = p.copy_html;
    var imgs = Array.isArray(p.images) ? p.images : [];
    var done = 0;
    imgs.forEach(function (img, i) {
      if (!img || !img.dataUri || !img.src) return;
      var b64 = img.dataUri.slice(img.dataUri.indexOf(',') + 1);
      var kb = Math.round(b64.length * 3 / 4 / 1024);
      diag('dataURI 심기[' + i + '] (크기 ~' + kb + 'kb)');
      html = replaceSrc(html, img.src, img.dataUri);   // 업로드 없이 데이터 자체를 심는다
      done++;
    });
    diag('데이터URI 심기 완료 ' + done + '/' + imgs.length);
    setStatus(u, '데이터URI로 심음 — 네이버 본문에 붙여넣어 (네이버가 알아서 업로드/등록하는지 확인)');
    u.resultTa.value = html;
    u.resultWrap.style.display = 'block';
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

  // ============ 정식(네이버 내부이미지) 문서 빌더 — setDocumentData용 ============
  // 앱이 보낸 doc(blocks/title/visitDate/hashtags/overallScore)을 SmartEditor
  // 네이티브 문서의 components 배열로 조립한다. 이미지는 upphoto 업로드 메타 +
  // internalResource:true 로 '내부 이미지'가 되어, 붙여넣기가 아니라 정식 삽입이라
  // 발행 후 자동 삭제되지 않는다.
  function sid() {
    try { if (window.crypto && crypto.randomUUID) return 'SE-' + crypto.randomUUID(); } catch (e) {}
    return 'SE-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 10);
  }
  function rep(ch, n) { var o = ''; for (var i = 0; i < n; i++) o += ch; return o; }
  function starBar(sc) {
    var s = Math.max(0, Math.min(10, Number(sc) || 0));
    var full = Math.floor(s / 2), half = (s % 2) ? 1 : 0, empty = 5 - full - half;
    return rep('★', full) + rep('⯪', half) + rep('☆', empty);
  }
  function tn(value, style) {
    var o = { '@ctype': 'textNode', id: sid(), value: '' + (value == null ? '' : value) };
    if (style) { style['@ctype'] = 'nodeStyle'; o.style = style; }
    return o;
  }
  function para(nodes, align, lineHeight) {
    var o = { '@ctype': 'paragraph', id: sid(), nodes: nodes };
    if (align || lineHeight) {
      var ps = { '@ctype': 'paragraphStyle' };
      if (align) ps.align = align;
      if (lineHeight) ps.lineHeight = lineHeight;
      o.paragraphStyle = ps;
    }
    return o;
  }
  function textComp(paras) { return { '@ctype': 'text', id: sid(), layout: 'default', value: paras }; }
  function hrComp() { return { '@ctype': 'horizontalLine', id: sid(), layout: 'line1' }; }
  function imageComp(meta) {
    return {
      '@ctype': 'image', id: sid(), layout: 'default', align: 'center',
      src: meta.displayUrl, internalResource: true, represent: false,
      path: meta.path || '', domain: 'https://blogfiles.pstatic.net',
      fileSize: Number(meta.fileSize) || 0,
      width: Number(meta.width) || 0, height: Number(meta.height) || 0,
      originalWidth: Number(meta.width) || 0, originalHeight: Number(meta.height) || 0,
      fileName: meta.fileName || 'image.jpg', format: 'normal', displayFormat: 'normal',
      imageLoaded: true, contentMode: 'fit',
      origin: { '@ctype': 'imageOrigin', srcFrom: 'local' }, ai: false
    };
  }
  function cellText(text, bold, bg) {
    var c = {
      '@ctype': 'tableCell', borderInlineStyle: 'border: 1px solid #000000;',
      colSpan: 1, rowSpan: 1, width: 50.0, height: 40.0,
      value: [para([tn(text, bold ? { bold: true } : null)], 'center')]
    };
    if (bg) c.backgroundColor = bg;
    return c;
  }
  function ratingsTable(items, overall) {
    var rows = [];
    rows.push({ '@ctype': 'tableRow', cells: [cellText('항목', true, '#faf3f6'), cellText('별점', true, '#faf3f6')] });
    (items || []).forEach(function (it) {
      if (!it) return;
      var aspect = ('' + (it.aspect || '')).trim(); if (!aspect) return;
      var sc = Math.max(0, Math.min(10, Number(it.score) || 0));
      rows.push({ '@ctype': 'tableRow', cells: [cellText(aspect, false), cellText(starBar(sc) + ' (' + sc + '/10)', false)] });
    });
    var ov = Math.max(0, Math.min(10, Number(overall) || 0));
    rows.push({ '@ctype': 'tableRow', cells: [cellText('총점', true), cellText(starBar(ov) + ' (' + ov + '/10)', true)] });
    return { '@ctype': 'table', id: sid(), layout: 'default', width: 100.0, rows: rows };
  }
  var EMPH_RE = /(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`|==[^=]+==)/g;
  function emphNodes(line) {
    var s = '' + (line == null ? '' : line), nodes = [], last = 0, m;
    EMPH_RE.lastIndex = 0;
    while ((m = EMPH_RE.exec(s)) !== null) {
      if (m.index > last) nodes.push(tn(s.slice(last, m.index), null));
      var tok = m[0], inner, style;
      if (tok.slice(0, 2) === '**') { inner = tok.slice(2, -2); style = { bold: true }; }
      else if (tok.charAt(0) === '*') { inner = tok.slice(1, -1); style = { fontColor: '#e64980', bold: true }; }
      else if (tok.charAt(0) === '`') { inner = tok.slice(1, -1); style = { fontColor: '#1c7ed6', bold: true }; }
      else { inner = tok.slice(2, -2); style = { backgroundColor: '#ffec99', bold: true }; }
      nodes.push(tn(inner, style));
      last = EMPH_RE.lastIndex;
    }
    if (last < s.length) nodes.push(tn(s.slice(last), null));
    if (!nodes.length) nodes.push(tn('', null));
    return nodes;
  }
  function quotationComp(text, foot) {
    return {
      '@ctype': 'quotation', id: sid(), layout: 'default',
      value: [para(emphNodes(('' + (text || '')).replace(/\n/g, ' ')), null)],
      source: [para([tn(foot || '내돈내산', null)], null)],
      align: 'justify'
    };
  }
  // copy_html의 이미지 src ↔ 블록 __src 매칭 키(토큰 e/t는 뷰마다 달라지므로 무시하고
  // photo id + 크롭으로만 매칭).
  function imgKey(u) {
    if (!u) return '';
    u = '' + u;
    var m = u.match(/\/blog-img\/(\d+)/), id = m ? m[1] : u;
    var c = u.match(/[?&](?:amp;)?c=([0-9.,]+)/);
    return id + '|' + (c ? c[1] : '');
  }
  function pushHr(comps) {
    if (!comps.length || comps[comps.length - 1]['@ctype'] !== 'horizontalLine') comps.push(hrComp());
  }
  function buildComponents(doc, metaBySrc) {
    var comps = [];
    var imgCount = 0;  // 첫 이미지에만 represent:true
    comps.push({
      '@ctype': 'documentTitle', id: sid(), layout: 'default',
      title: [{ '@ctype': 'paragraph', id: sid(), nodes: [{ '@ctype': 'textNode', id: sid(), value: '' + (doc.title || '') }] }],
      subTitle: null, align: 'center'
    });
    var headerParas = [];
    if (doc.visitDate) headerParas.push(para([tn('방문 날짜 : ' + doc.visitDate, null)], 'center'));
    headerParas.push(para([tn('✱ 내돈내산 데이트 후기 ✱', { fontColor: '#e64980', bold: true })], 'center'));
    comps.push(textComp(headerParas));
    var blocks = doc.blocks || [];
    for (var b = 0; b < blocks.length; b++) {
      var blk = blocks[b];
      if (!blk || typeof blk !== 'object') continue;
      var t = blk.type;
      if (t === 'heading') {
        var htext = ('' + (blk.text || '')).trim(); if (!htext) continue;
        pushHr(comps);
        comps.push(textComp([para([tn(htext, { fontColor: '#222', fontSizeCode: 'fs19', bold: true })], 'center')]));
      } else if (t === 'para') {
        var lines = ('' + (blk.text || '')).replace(/\r\n/g, '\n').split('\n'), paras = [];
        for (var li = 0; li < lines.length; li++) { var ln = lines[li].trim(); if (ln) paras.push(para(emphNodes(ln), 'center')); }
        if (paras.length) comps.push(textComp(paras));
      } else if (t === 'image') {
        var meta = metaBySrc[imgKey(blk.__src)];
        if (meta && meta['@ctype'] === 'image') {
          // 네이티브 업로드로 얻은 실제 image 컴포넌트를 그대로 재사용(정확한 멀티렌디션
          // src/path). 중복 id 충돌 방지로 새 id 부여, 정렬·represent만 조정.
          var nc = {}; for (var kk in meta) nc[kk] = meta[kk];
          nc.id = sid(); nc.align = 'center'; nc.represent = (imgCount === 0);
          imgCount++; comps.push(nc);
        } else if (meta) {
          var ic = imageComp(meta); ic.represent = (imgCount === 0);
          imgCount++; comps.push(ic);
        } else {
          diag('정식: 이미지 메타 없음 (src=' + (blk.__src || '') + ')');
        }
      } else if (t === 'info') {
        var ip = [];
        (blk.items || []).forEach(function (it) {
          if (!it) return;
          var label = ('' + (it.label || '')).trim(), value = ('' + (it.value || '')).trim();
          if (!label || !value) return;
          if (label.indexOf('방문') !== -1 && (label.indexOf('날짜') !== -1 || label.indexOf('일') !== -1)) return;
          ip.push(para([tn(label + ' ', { bold: true }), tn(': ' + value, null)], 'center', 2.0));
        });
        if (ip.length) comps.push(textComp(ip));
      } else if (t === 'ratings') {
        comps.push(ratingsTable(blk.items || [], doc.overallScore));
      } else if (t === 'faq') {
        var fp = [];
        (blk.items || []).forEach(function (it) {
          if (!it) return;
          var q = ('' + (it.q || '')).trim(), a = ('' + (it.a || '')).trim();
          if (!q || !a) return;
          fp.push(para([tn('Q. ' + q, { bold: true })], null));
          fp.push(para([tn('A. ' + a, null)], null));
        });
        if (fp.length) comps.push(textComp(fp));
      } else if (t === 'quote') {
        pushHr(comps);
        comps.push(quotationComp(blk.text || '', doc.visitFoot ? (doc.visitFoot + ' 방문 · 내돈내산') : '내돈내산'));
      }
    }
    var tags = (doc.hashtags || []).map(function (x) { return ('' + x).trim(); }).filter(Boolean);
    if (tags.length) comps.push(textComp([para([tn(tags.join(' '), { fontColor: '#e0559b', bold: true })], 'center')]));
    return comps;
  }
  // 실제 에디터 인스턴스(getDocumentData/setDocumentData 보유) 찾기 — explore와 같은 경로.
  function getEditorInstance() {
    try {
      var w = findSeWin();
      if (!w) return null;
      var Sm = w.SmartEditor;
      if (!Sm) return null;
      var e = null;
      try { if (typeof Sm.getEditor === 'function') e = Sm.getEditor(); } catch (x) {}
      if (!e && Sm._editors) {
        try { var vk = Object.keys(Sm._editors); e = Sm._editors.blogpc001 || (vk.length ? Sm._editors[vk[0]] : null); } catch (x) {}
      }
      return e;
    } catch (x) { return null; }
  }
  // 🅢 정식 삽입 — 에디터의 네이티브 이미지 업로더(_imageUploadService, NAVER_PHOTO_INFRA)로
  // 파일을 직접 업로드해 진짜 내부이미지 URL/메타를 얻고, doc.blocks 순서로 컴포넌트를
  // 조립해 setDocumentData 한다. 업로더가 없으면 execCommand(INSERT_IMAGE_FILES) + readback
  // 폴백으로 떨어진다. upphoto(getSessionKey/uploadOne) 경로는 쓰지 않는다.
  function processNative(u) {
    var raw = (u.ta.value || '').trim(), p;
    try { p = JSON.parse(raw); } catch (e) { p = null; }
    diag('정식 삽입 시작 — 페이로드 ' + (p && p.copy_html ? 'OK' : '없음'));
    if (!p || !p.copy_html) {
      diag('중단: 페이로드 없음');
      setStatus(u, "페이로드가 안 보여 — 앱에서 '네이버로 보내기' 후 이 칸에 붙여넣어줘", true);
      return;
    }
    if (!p.doc || !p.doc.blocks) {
      diag('중단: doc 없음(앱 업데이트 필요)');
      setStatus(u, '앱을 업데이트해야 해(정식 삽입용 데이터 없음)', true);
      return;
    }
    var win = findSeWin() || window;
    var Sm = win && win.SmartEditor;
    var ed = getEditorInstance();
    if (!ed || typeof ed.getDocumentData !== 'function' || typeof ed.setDocumentData !== 'function') {
      diag('중단: 에디터(getDocumentData/setDocumentData) 못 찾음');
      setStatus(u, '에디터를 못 찾았어 — 네이버 글쓰기 화면에서 실행해줘', true);
      return;
    }

    // 업로더가 사는 프레임(win) '실체'로 File/Blob을 만든다 — TOP 렐름의 File을
    // iframe 렐름 uploader가 instanceof/속성으로 읽으면 undefined→.toString 크래시.
    var RB = (win && win.Blob) || Blob;
    var RF = (win && win.File) || File;
    var RU8 = (win && win.Uint8Array) || Uint8Array;
    function dataUriToBlobIn(d) {
      var c = d.indexOf(','), head = d.slice(0, c), b64 = d.slice(c + 1);
      var mime = (head.match(/data:([^;]+)/) || [])[1] || 'image/jpeg';
      var bin = atob(b64), n = bin.length, arr = new RU8(n);
      for (var j = 0; j < n; j++) arr[j] = bin.charCodeAt(j);
      return new RB([arr], { type: mime });
    }

    var imgs = Array.isArray(p.images) ? p.images : [];
    // dataUri → File(업로더 렐름), 순서 보존 + 각 파일이 대응하는 블록 __src를 병렬 배열로.
    var files = [], fileSrcs = [];
    for (var i = 0; i < imgs.length; i++) {
      var im = imgs[i];
      if (!im || !im.dataUri || !im.src) continue;
      var blob = dataUriToBlobIn(im.dataUri);
      var name = 'image' + i + '.jpg';
      var f;
      try { f = new RF([blob], name, { type: blob.type || 'image/jpeg' }); }
      catch (e) { f = blob; try { f.name = name; } catch (e2) {} }
      files.push(f); fileSrcs.push(im.src);
    }
    diag('업로드 대상 파일 ' + files.length + '장 (렐름=' + (win === window ? 'self' : 'iframe') + ')');

    u.go.disabled = true;
    var metaByBlockSrc = {};

    function buildAndSet(label) {
      try {
        var comps = buildComponents(p.doc, metaByBlockSrc);
        var got = Object.keys(metaByBlockSrc).length;
        diag('컴포넌트 ' + comps.length + '개 생성 (이미지 ' + got + '/' + files.length + ')');
        var d = ed.getDocumentData();
        if (!d || !d.document) { setStatus(u, 'getDocumentData 형식 예상밖', true); u.go.disabled = false; return; }
        d.document.components = comps;
        ed.setDocumentData(d);
        diag('정식 문서 삽입 완료 (이미지 ' + got + '장)' + (label ? ' [' + label + ']' : ''));
        setStatus(u, '정식 문서 삽입 완료 (이미지 ' + got + '장) — 편집기 확인 후 발행');
      } catch (e) { diag('buildAndSet 실패: ' + (e && e.message ? e.message : e)); setStatus(u, '문서 삽입 실패 — 진단 로그 확인', true); }
      u.go.disabled = false;
    }

    if (!files.length) { buildAndSet(); return; }

    var ius = (ed._videoUploadService && ed._videoUploadService._imageUploadService) || null;
    diag('업로더 점검: ius=' + (!!ius) + ' uploadImagesFromFiles=' + (ius && typeof ius.uploadImagesFromFiles) + ' uploadImages=' + (ius && typeof ius.uploadImages) + ' createSourceList=' + (ius && typeof ius.createSourceList));

    // ---- 폴백: execCommand(INSERT_IMAGE_FILES) + 폴링(readback) ----
    function runCommandFallback() {
      try { if (ed.focusFirstText) ed.focusFirstText(); } catch (e) {}
      if (!ed._commandManager || typeof ed._commandManager.execCommand !== 'function' || !Sm || !Sm.COMMAND || !Sm.COMMAND.IMAGE) {
        diag('폴백 불가: _commandManager.execCommand / COMMAND.IMAGE 없음');
        setStatus(u, '에디터 명령 API를 못 찾았어 — 다시 시도해줘', true); u.go.disabled = false; return;
      }
      diag('폴백: execCommand · COMMAND.IMAGE=' + JSON.stringify(Sm.COMMAND.IMAGE));
      function docComps() { try { return ed.getDocumentData().document.components || []; } catch (e) { return []; } }
      var before = {};
      docComps().forEach(function (c) { if (c && c['@ctype'] === 'image') before[c.id] = true; });
      var CMD = Sm.COMMAND.IMAGE.INSERT_IMAGE_FILES;
      var uploaded = false, usedShape = '';
      function tryShape(shape, arg) {
        if (uploaded) return;
        try { ed._commandManager.execCommand(CMD, arg); uploaded = true; usedShape = shape; diag('폴백 호출 OK — shape=' + shape); }
        catch (e) { diag('폴백 호출 실패 shape=' + shape + ': ' + (e && e.message ? e.message : e)); }
      }
      tryShape('files[]', files);
      if (!uploaded) tryShape('{files}', { files: files });
      if (!uploaded) tryShape('{imageFiles}', { imageFiles: files });
      if (!uploaded) { try { files.forEach(function (f) { ed._commandManager.execCommand(CMD, [f]); }); uploaded = true; usedShape = 'loop[f]'; diag('폴백 호출 OK — shape=loop[f]'); } catch (e) { diag('폴백 단일루프 실패: ' + (e && e.message ? e.message : e)); } }
      if (!uploaded) { setStatus(u, '네이티브 업로드 호출 실패 — 진단 로그 확인', true); u.go.disabled = false; return; }
      var t0 = Date.now(), MAXMS = 40000, loggedFirst = false;
      setStatus(u, '네이티브 업로드 대기…');
      var iv = setInterval(function () {
        var processing = false;
        try {
          if (typeof ed.isDocumentProcessing === 'function') processing = !!ed.isDocumentProcessing();
          else if (typeof ed.isDocumentProcessing === 'boolean') processing = ed.isDocumentProcessing;
        } catch (e) {}
        var ready = [];
        docComps().forEach(function (c) {
          if (!c || c['@ctype'] !== 'image') return;
          if (before[c.id]) return;
          var s = '' + (c.src || '');
          if (s.indexOf('https://blogfiles.pstatic.net') !== 0) return;
          if (c.imageLoaded === false) return;
          ready.push(c);
        });
        if (!loggedFirst && ready.length) { loggedFirst = true; try { diag('첫 네이티브 이미지: ' + JSON.stringify(ready[0]).slice(0, 600)); } catch (e) {} }
        setStatus(u, '네이티브 업로드 ' + ready.length + '/' + files.length + ' 준비');
        var timeUp = (Date.now() - t0) > MAXMS;
        if ((ready.length >= files.length && !processing) || timeUp) {
          clearInterval(iv);
          diag('폴백 완료 ' + ready.length + '/' + files.length + (timeUp ? ' (타임아웃)' : '') + ' shape=' + usedShape);
          var n = Math.min(ready.length, fileSrcs.length);
          for (var k = 0; k < n; k++) metaByBlockSrc[imgKey(fileSrcs[k])] = ready[k];
          buildAndSet('fallback');
        }
      }, 500);
    }

    if (!ius) {
      diag('_imageUploadService 없음 → execCommand 폴백');
      runCommandFallback();
      return;
    }
    diag('네이티브 업로더 사용 — _imageUploadService');

    // 업로더 반환을 '결과 배열'로 정규화: 배열-of-프라미스 / 프라미스-of-배열 / 평면배열 모두 흡수.
    function normalize(ret) {
      return Promise.resolve(ret).then(function (arr) {
        arr = Array.isArray(arr) ? arr : [arr];
        return Promise.all(arr.map(function (x) { return Promise.resolve(x); }));
      });
    }
    // 결과 배열 → 사용가능한(url 있는) 메타 목록 [{idx, meta}] + 진단 로그.
    function extractMetas(results) {
      var out = [];
      for (var r = 0; r < results.length; r++) {
        var res = results[r] || {};
        var code = res.code;
        var resp = (res && (res.response || res.result || res)) || {};
        if (code && code !== 'SUCCESS') { diag('업로드결과[' + r + '] code=' + code + ' (스킵)'); continue; }
        var rawUrl = resp.url || resp.src || resp.imageUrl || resp.originalUrl || (resp.image && resp.image.url) || '';
        var path = resp.path || resp.fullPath || (resp.image && resp.image.path) || '';
        var width = resp.width || resp.originalWidth || (resp.image && resp.image.width) || 0;
        var height = resp.height || resp.originalHeight || (resp.image && resp.image.height) || 0;
        var fileSize = resp.fileSize || resp.size || 0;
        var fileName = resp.fileName || resp.name || 'image.jpg';
        // 서빙 가능한 URL 조립. 네이티브 _imageUploader.upload 결과의 resp.url 은 bare
        // path(파일명 없음)라 신뢰하지 않는다 — path + '/' + fileName 을 우선 조립한다
        // (사용자 확인: blogfiles.pstatic.net/<path>.JPEG/image6.jpg?type=w1 서빙됨).
        var base = '';
        if (path) {
          base = 'https://blogfiles.pstatic.net' + (String(path).charAt(0) === '/' ? '' : '/') + path;
          if (fileName && base.indexOf('/' + fileName) === -1) base += '/' + fileName;
        } else if (rawUrl) {
          base = /^https?:/i.test(rawUrl) ? rawUrl : ('https://blogfiles.pstatic.net' + (String(rawUrl).charAt(0) === '/' ? '' : '/') + rawUrl);
          if (fileName && base.indexOf('/' + fileName) === -1 && !/\.[a-z0-9]{2,4}(\?|$)/i.test(base.split('/').pop())) base += '/' + fileName;
        }
        if (base && base.indexOf('?type=') === -1) base += (base.indexOf('?') === -1 ? '?' : '&') + 'type=w1';
        diag('업로드결과[' + r + '] code=' + (code || '?') + ' url=' + base + ' path=' + path + ' ' + width + 'x' + height);
        diag('정식 src[' + r + ']=' + base);
        if (base) out.push({ idx: r, meta: { displayUrl: base, path: path, width: width, height: height, fileSize: fileSize, fileName: fileName } });
      }
      return out;
    }
    // 한 시도: fn() 호출→normalize→extract. rejection/throw/결과0 이면 {ok:false}로 수렴(다음 시도로).
    function attempt(label, fn) {
      return new Promise(function (resolve) {
        var ret;
        try { ret = fn(); }
        catch (e) { diag('시도 ' + label + ': err ' + (e && e.message ? e.message : e)); resolve({ ok: false }); return; }
        normalize(ret).then(function (results) {
          results = results || [];
          diag('시도 ' + label + ' 결과 개수=' + results.length);
          var metas = extractMetas(results);
          if (metas.length) {
            try { diag('첫 업로드 원본: ' + JSON.stringify(results[0]).slice(0, 700)); } catch (e) {}
            diag('시도 ' + label + ': ok 결과 ' + metas.length);
            resolve({ ok: true, metas: metas });
          } else {
            diag('시도 ' + label + ': 결과 0 (usable url 없음)');
            resolve({ ok: false });
          }
        }, function (e) {
          diag('시도 ' + label + ': err ' + (e && e.message ? e.message : e));
          resolve({ ok: false });
        });
      });
    }

    setStatus(u, '네이버 인프라 업로드 중… (0/' + files.length + ')');
    attempt('A', function () {
      diag('uploadImagesFromFiles 호출 (files=' + files.length + ')');
      if (typeof ius.uploadImagesFromFiles !== 'function') throw new Error('uploadImagesFromFiles 미존재');
      var ret = ius.uploadImagesFromFiles(files);
      diag('A 반환 typeof=' + typeof ret + ' isPromise=' + (ret && typeof ret.then === 'function') + ' isArray=' + Array.isArray(ret));
      return ret;
    }).then(function (a) {
      if (a.ok) return a;
      if (typeof ius.uploadImages === 'function' && typeof ius.createSourceList === 'function') {
        return attempt('B', function () { return ius.uploadImages(ius.createSourceList(files)); });
      }
      diag('시도 B 스킵 (uploadImages/createSourceList 미존재)');
      return { ok: false };
    }).then(function (b) {
      if (b.ok) return b;
      if (typeof ius.uploadImages === 'function') {
        return attempt('C', function () { return ius.uploadImages(files.map(function (f, i) { return { id: 'cd' + i, source: f }; })); });
      }
      diag('시도 C 스킵 (uploadImages 미존재)');
      return { ok: false };
    }).then(function (res) {
      if (!res.ok || !res.metas || !res.metas.length) { diag('A/B/C 모두 실패 → execCommand 폴백'); runCommandFallback(); return; }
      var metas = res.metas, okCount = 0;
      for (var k = 0; k < metas.length; k++) {
        var idx = metas[k].idx;
        if (idx < fileSrcs.length) { metaByBlockSrc[imgKey(fileSrcs[idx])] = metas[k].meta; okCount++; }
      }
      setStatus(u, '네이버 인프라 업로드 중… (' + okCount + '/' + files.length + ')');
      buildAndSet('native');
    }).catch(function (e) {
      diag('네이티브 업로드 예외: ' + (e && e.message ? e.message : e) + ' → execCommand 폴백');
      runCommandFallback();
    });
  }

  // --------- 부팅 ---------
  var TOKEN_OK = '토큰 확보 ✓ (페이로드 붙여넣고 처리)';
  var WAITING = '대기 중 — 에디터를 한 번 클릭(또는 사진 1장 추가)하면 토큰을 잡아요';
  function tokenReady() { return !!(window.__cdAuth || window.__cdSK); }

  var u = buildOverlay();
  window.__cdSKcb = function () { setStatus(u, TOKEN_OK); };
  window.__cdAuthCb = function () { setStatus(u, TOKEN_OK); };
  window.__cdDiagCb = function () {
    try { u.diagTa.value = (window.__cdDiag || []).slice(-150).join('\n'); } catch (e) {}
  };
  installInterceptor();
  setStatus(u, tokenReady() ? TOKEN_OK : WAITING);
  if (window.__cdDiag && window.__cdDiag.length) window.__cdDiagCb();
  u.go.addEventListener('click', function () {
    try { process(u); }
    catch (e) { L('처리 예외', e); setStatus(u, '오류: ' + (e && e.message ? e.message : e), true); }
  });
  u.nativeBtn.addEventListener('click', function () {
    try { processNative(u); }
    catch (e) { L('정식 삽입 예외', e); setStatus(u, '오류: ' + (e && e.message ? e.message : e), true); }
  });
  u.dataBtn.addEventListener('click', function () {
    try { processDataUri(u); }
    catch (e) { L('데이터URI 예외', e); setStatus(u, '오류: ' + (e && e.message ? e.message : e), true); }
  });
  u.explore.addEventListener('click', function () {
    try { exploreEditorApi(); }
    catch (e) { diag('탐색 예외: ' + e); }
  });
  L('오버레이 준비됨. URL=', location.href);
})();

/*
 * ============================ 최종 북마클릿 ================================
 * 아래 한 줄(javascript:...)을 즐겨찾기 URL에 붙여넣고 이름을 '📷 사진 올리기'로
 * 저장. sibling tools/naver-bookmarklet.txt 가 정본이다(같은 내용).
 *
 * javascript:(function(){'use strict';var DHP='https://blogfiles.pstatic.net';var ITQ='?type=w1';var BIF='yhc9355';var UQ='extractExif=true&extractAnimatedCnt=false&extractAnimatedInfo=true&autorotate=true&extractDominantColor=false&type=&customQuery=&denyAnimatedImage=false&skipXcamFiltering=false';var UB='https://blog.upphoto.naver.com';var SKU='https://platform.editor.naver.com/api/blogpc001/v1/photo-uploader/session-key';var SKM='session-key';var L=function(){try{console.log.apply(console,['[nv]'].concat([].slice.call(arguments)))}catch(e){}};function diag(s){try{if(!window.__cdDiag)window.__cdDiag=[];window.__cdDiag.push(s);if(window.__cdDiag.length>200)window.__cdDiag=window.__cdDiag.slice(-200);L('[diag]',s);if(window.__cdDiagCb){try{window.__cdDiagCb()}catch(e){}}}catch(e){}}var __cdSeenXO={};function diagOnce(key,msg){if(__cdSeenXO[key])return;__cdSeenXO[key]=true;diag(msg)}function exK(t){if(!t)return null;var m=/"sessionKey"\s*:\s*"([^"]+)"/.exec(t);return m&&m[1]?m[1]:null}function gotK(k){if(!k||window.__cdSK===k)return;window.__cdSK=k;L('세션키 확보',k);diag('✓ sessionKey 확보 ('+(''+k).slice(0,12)+'…)');if(window.__cdSKcb){try{window.__cdSKcb(k)}catch(e){}}}function gotA(){if(window.__cdAuthCb){try{window.__cdAuthCb()}catch(e){}}}function markEd(w,y){try{if(w&&window.__cdEditorWin!==w){window.__cdEditorWin=w;diag('에디터 프레임 지정: '+y)}}catch(e){}}function edw(){return window.__cdEditorWin||window}function setA(name,val){if(!val)return false;var n=(''+name).toLowerCase();if(n==='se-authorization'){if(window.__cdAuth!==val){window.__cdAuth=val;diag('✓ se-authorization 확보 ('+(''+val).slice(0,12)+'…)');gotA()}return true}if(n==='se-app-id'){if(window.__cdAppId!==val){window.__cdAppId=val;gotA()}}return false}function grabA(h){var s=false;try{if(!h)return false;if(Object.prototype.toString.call(h)==='[object Array]'){for(var i=0;i<h.length;i++){if(h[i]&&h[i].length>=2&&setA(h[i][0],h[i][1]))s=true}}else if(typeof h.forEach==='function'){h.forEach(function(v,k){if(setA(k,v))s=true})}else{for(var k in h){if(Object.prototype.hasOwnProperty.call(h,k)&&setA(k,h[k]))s=true}}}catch(e){}return s}function sh(h){var o=[];try{if(!h)return o;if(Object.prototype.toString.call(h)==='[object Array]'){for(var i=0;i<h.length;i++){if(h[i]&&h[i].length>=2)o.push(h[i][0]+': '+h[i][1])}}else if(typeof h.forEach==='function'){h.forEach(function(v,k){o.push(k+': '+v)})}else{for(var k in h){if(Object.prototype.hasOwnProperty.call(h,k))o.push(k+': '+h[k])}}}catch(e){}return o}function recR(method,url,hl){window.__cdReq={method:method||'GET',url:url||'',headers:hl||[]};diag('요청상세: '+(method||'GET')+' '+url);if(hl&&hl.length){for(var i=0;i<hl.length;i++)diag('  '+hl[i])}}function isReq(a0){return !!(a0&&typeof a0==='object'&&a0.headers&&typeof a0.url==='string')}function patchWin(win,label){if(!win)return false;try{if(win.__cdItcp)return false;win.__cdItcp=true}catch(e){diagOnce('flag:'+label,'프레임 접근불가(플래그): '+label);return false}try{var hf='';try{hf=win.location.href||''}catch(e2){}if(hf.indexOf('PostWriteForm')!==-1)markEd(win,'PostWriteForm 프레임')}catch(e){}try{var of=win.fetch;if(typeof of==='function'){win.fetch=function(){var a=arguments,url;try{url=(a[0]&&a[0].url)?a[0].url:(''+a[0])}catch(e){url=''}var a0=a[0],a1=a[1],sa=false;try{if(a1&&a1.headers)sa=grabA(a1.headers)||sa;if(isReq(a0))sa=grabA(a0.headers)||sa}catch(e){}var isSK=url&&url.indexOf(SKM)!==-1;if(isSK||sa){var mth='GET',hd=[];try{if(a1){if(a1.method)mth=a1.method;if(a1.headers)hd=hd.concat(sh(a1.headers))}if(isReq(a0)){if(a0.method)mth=a0.method;hd=hd.concat(sh(a0.headers))}}catch(e){}diag('['+label+'] '+mth+' '+url+' — auth='+sa+' sk='+(!!window.__cdSK));if(sa)markEd(win,label+' (se-authorization)');if(isSK)recR(mth,url,hd)}var p=of.apply(win,a);try{if(isSK&&p&&p.then){p.then(function(r){try{r.clone().text().then(function(t){gotK(exK(t))}).catch(function(){})}catch(e){}}).catch(function(){})}}catch(e){}return p}}}catch(e){L('fetch 패치 실패('+label+')',e)}try{var X=win.XMLHttpRequest;if(X&&X.prototype&&!X.prototype.__cdPatched){X.prototype.__cdPatched=true;var oo=X.prototype.open,osh=X.prototype.setRequestHeader,os=X.prototype.send;X.prototype.open=function(m,url){try{this.__cdMethod=m;this.__cdUrl=url;this.__cdHdrs=[];this.__cdSawAuth=false}catch(e){}return oo.apply(this,arguments)};X.prototype.setRequestHeader=function(n,v){try{if(!this.__cdHdrs)this.__cdHdrs=[];this.__cdHdrs.push(n+': '+v);if(setA(n,v))this.__cdSawAuth=true}catch(e){}return osh.apply(this,arguments)};X.prototype.send=function(){try{var x=this;var isSK=x.__cdUrl&&(''+x.__cdUrl).indexOf(SKM)!==-1;if(isSK||x.__cdSawAuth){diag('['+label+'/xhr] '+(x.__cdMethod||'')+' '+x.__cdUrl+' — auth='+(!!x.__cdSawAuth)+' sk='+(!!window.__cdSK));if(x.__cdSawAuth)markEd(win,label+' (se-authorization xhr)');if(isSK)recR(x.__cdMethod,''+x.__cdUrl,x.__cdHdrs||[])}if(isSK){x.addEventListener('load',function(){try{var k=null,rt=x.responseType;if(rt==='json'&&x.response){k=x.response&&x.response.sessionKey}else if(rt===''||rt==='text'){k=exK(x.responseText||'')}if(k)gotK(k)}catch(e){L('xhr load 읽기 실패',e)}})}}catch(e){}return os.apply(this,arguments)}}}catch(e){L('XHR 패치 실패('+label+')',e)}diag('프레임 패치 ✓ '+label);return true}function scan(win,depth){if(depth>6)return 0;var fr;try{fr=win.document.querySelectorAll('iframe')}catch(e){return 0}var n=0;for(var i=0;i<fr.length;i++){var f=fr[i],src='(inline)';try{src=f.getAttribute('src')||f.src||'(inline)'}catch(e){src='?'}var cw=null;try{cw=f.contentWindow}catch(e){L('iframe 접근불가:',src);continue}if(!cw)continue;var acc=true;try{void cw.location.href}catch(e){acc=false}if(!acc){L('iframe cross-origin 스킵:',src);continue}if(patchWin(cw,'iframe:'+src))n++;n+=scan(cw,depth+1)}return n}function patchAll(){var n=0;if(patchWin(window,'top'))n++;n+=scan(window,0);if(n>0)diag('프레임 신규 패치: '+n+'개');return n}function itcp(){patchAll();var t=0;var iv=setInterval(function(){t++;try{patchAll()}catch(e){}if(t>=30)clearInterval(iv)},1000);try{if(typeof MutationObserver!=='undefined'){var mo=new MutationObserver(function(){try{patchAll()}catch(e){}});mo.observe(document.documentElement||document.body||document,{childList:true,subtree:true})}}catch(e){}}function getSK(mAuth,mApp){if(window.__cdSK){diag('세션키: 재사용');return Promise.resolve(window.__cdSK)}var auth=window.__cdAuth||mAuth;var app=window.__cdAppId||mApp;if(auth){diag('세션키: 직접요청 '+(window.__cdAuth?'(가로챈 JWT)':'(수동)'));var hd={'accept':'application/json','se-authorization':auth};if(app)hd['se-app-id']=app;return edw().fetch(SKU,{credentials:'include',headers:hd}).then(function(r){diag('session-key HTTP '+r.status);if(!r.ok)throw new Error('session-key HTTP '+r.status);return r.json()}).then(function(j){var k=j&&j.sessionKey;if(!k)throw new Error('세션키 없음');window.__cdSK=k;return k})}return Promise.reject(new Error('토큰을 못 잡았어 — 에디터를 한 번 클릭/사진 추가하거나, 아래 🔑 토큰 수동입력에 se-authorization을 붙여넣어줘'))}function gb(){try{var m=location.pathname.match(/^\/([A-Za-z0-9_-]+)(?:\/|$)/);if(m&&m[1]&&m[1].indexOf('.naver')===-1)return m[1]}catch(e){}return BIF}function d2b(d){var c=d.indexOf(','),h=d.slice(0,c),b=d.slice(c+1),mm=(h.match(/data:([^;]+)/)||[])[1]||'image/jpeg',bin=atob(b),n=bin.length,a=new Uint8Array(n);for(var i=0;i<n;i++)a[i]=bin.charCodeAt(i);return new Blob([a],{type:mm})}function pu(x){var el=null;try{var doc=new DOMParser().parseFromString(x,'text/xml');el=doc.querySelector('url')||doc.getElementsByTagName('url')[0]}catch(e){}var r=el&&el.textContent?el.textContent.trim():'';if(!r){var m=x.match(/<url>([^<]+)<\/url>/i);r=m?m[1].trim():''}if(!r)return null;if(/^https?:\/\//i.test(r))return r;var a=DHP+(r.charAt(0)==='/'?'':'/')+r;if(!ITQ)return a;var q=ITQ.replace(/^[?&]/,'');return a+(a.indexOf('?')===-1?'?':'&')+q}function rs(h,f,t){var o=h,v=[f,f.replace(/&/g,'&amp;')];for(var i=0;i<v.length;i++){o=o.split('"'+v[i]+'"').join('"'+t+'"');o=o.split("'"+v[i]+"'").join("'"+t+"'")}return o}function wc(h){if(window.ClipboardItem&&navigator.clipboard&&navigator.clipboard.write){return navigator.clipboard.write([new ClipboardItem({'text/html':new Blob([h],{type:'text/html'}),'text/plain':new Blob([''],{type:'text/plain'})})])}if(navigator.clipboard&&navigator.clipboard.writeText){return navigator.clipboard.writeText(h)}return Promise.reject(new Error('clipboard'))}function xf(x,tag){try{var m=x.match(new RegExp('<'+tag+'>([^<]*)</'+tag+'>','i'));return m?m[1].trim():''}catch(e){return ''}}function up(k,b,bl,i){var u=UB+'/'+k+'/simpleUpload/0?userId='+encodeURIComponent(b)+'&'+UQ;L('업로드['+i+']',u,bl.size);var fd=new FormData();fd.append('image',bl,'image.jpg');diag('업로드['+i+'] multipart image 필드 전송');return edw().fetch(u,{method:'POST',credentials:'include',body:fd}).then(function(r){return r.text().then(function(t){diag('업로드['+i+'] HTTP '+r.status);if(!r.ok)throw new Error('upphoto HTTP '+r.status+' '+t.slice(0,160));var du=pu(t);if(!du)throw new Error('URL 못 찾음('+t.slice(0,120)+')');var meta={displayUrl:du,path:xf(t,'path'),width:xf(t,'width'),height:xf(t,'height'),fileSize:xf(t,'fileSize'),fileName:xf(t,'fileName'),imageType:xf(t,'imageType'),thumbnail:xf(t,'thumbnail')};if(!window.__cdUploads)window.__cdUploads=[];window.__cdUploads.push(meta);diag('업로드['+i+'] path='+meta.path+' '+meta.width+'×'+meta.height+' size='+meta.fileSize+' url='+du);return meta})})}function fsw(){function q(w){try{return !!(w&&w.SmartEditor&&(typeof w.SmartEditor.getEditor==='function'||w.SmartEditor._editors))}catch(e){return false}}try{if(q(window.__cdEditorWin))return window.__cdEditorWin}catch(e){}try{if(q(window.top))return window.top}catch(e){}if(q(window))return window;function walk(w,depth){if(depth>6)return null;var fr;try{fr=w.document.querySelectorAll('iframe')}catch(e){return null}for(var i=0;i<fr.length;i++){var cw=null;try{cw=fr[i].contentWindow}catch(e){continue}if(!cw)continue;try{void cw.location.href}catch(e){continue}if(q(cw))return cw;var d=walk(cw,depth+1);if(d)return d}return null}return walk(window,0)}function findEd(){if(window.__cdEditorWin)return window.__cdEditorWin;try{var fr=document.querySelectorAll('iframe');for(var i=0;i<fr.length;i++){var cw=null;try{cw=fr[i].contentWindow}catch(e){continue}if(!cw)continue;var hf='';try{hf=cw.location.href||''}catch(e){continue}if(hf.indexOf('PostWriteForm')!==-1||hf.indexOf('editor')!==-1)return cw}}catch(e){}return null}function explore(){var win=fsw()||findEd()||window;var wh=(win===window)?'top':'editorFrame';diag('🔬 API 탐색 시작 — win='+wh);diag('SmartEditor 보유 프레임: '+(win===window.top?'top':(win===window?'self':'iframe'))+' · SmartEditor='+(typeof (win.SmartEditor)));var RE=/edit|smart|se3|se2|doc|content|store|nhn|blog|component|image|nsi|__|pmodel|prosemirror/i;var keys=[];try{keys=Object.keys(win)}catch(e){diag('키 열거 실패: '+e)}var hits=[];for(var i=0;i<keys.length;i++){if(RE.test(keys[i]))hits.push(keys[i])}diag('후보 전역 키 '+hits.length+'개 (전체 '+keys.length+')');diag('후보: '+hits.slice(0,80).join(', '));for(var j=0;j<hits.length&&j<40;j++){var k=hits[j];try{var v=win[k];var ty=typeof v;diag('· '+k+' : '+ty);if(v&&(ty==='object'||ty==='function')){var sub=[];try{sub=Object.keys(v)}catch(e){}if(sub.length)diag('    ['+k+'] '+sub.slice(0,40).join(', '))}}catch(e){diag('· '+k+' : (접근불가)')}}['se','__se','smartEditor','SmartEditor','SE','editor','nhn','__store__','oEditors','g_oEditor'].forEach(function(g){try{if(g in win)diag('전역 '+g+' 존재: '+(typeof win[g]))}catch(e){}});try{var doc=win.document,all=doc.querySelectorAll('*'),rf=0;for(var m=0;m<all.length&&m<2500;m++){var elm=all[m],own=[];try{own=Object.keys(elm)}catch(e){}for(var q=0;q<own.length;q++){if(own[q].indexOf('__reactContainer')===0||own[q]==='_reactRootContainer'||own[q].indexOf('__reactFiber')===0){rf++;if(rf<=4)diag('React: <'+elm.tagName+' id='+(elm.id||'')+' class='+(''+(elm.className||'')).slice(0,40)+'> key='+own[q]);break}}}diag('React 루트/파이버 발견 수(≈): '+rf)}catch(e){diag('React 스캔 실패: '+e)}var HL=/image|photo|insert|component|add|content|document|data|model|store|body|paragraph|attach|media|upload/i;function dumpO(label,o,cap){try{if(!o){diag(label+' : (없음)');return}var names=[];try{names=Object.getOwnPropertyNames(o)}catch(e){try{names=Object.keys(o)}catch(e2){}}try{var pr=Object.getPrototypeOf(o),depth=0;while(pr&&depth<2){try{Object.getOwnPropertyNames(pr).forEach(function(n){if(names.indexOf(n)===-1)names.push(n)})}catch(e){}pr=Object.getPrototypeOf(pr);depth++}}catch(e){}diag(label+' 키('+names.length+'): '+names.slice(0,cap||80).join(', '));var hl=names.filter(function(n){return HL.test(n)});if(hl.length)diag(label+' ★후보: '+hl.join(', '))}catch(e){diag(label+' dump 실패: '+e)}}try{var Sm=win.SmartEditor;diag('SmartEditor typeof: '+(typeof Sm));if(Sm){try{if(Sm.COMMAND){var ck=Object.keys(Sm.COMMAND);diag('SmartEditor.COMMAND('+ck.length+'): '+ck.slice(0,120).join(', '));ck.forEach(function(kk){if(HL.test(kk)){try{diag('  COMMAND.'+kk+' = '+JSON.stringify(Sm.COMMAND[kk]).slice(0,80))}catch(e){}}})}}catch(e){diag('COMMAND 실패: '+e)}try{if(Sm.OPTION)diag('SmartEditor.OPTION: '+Object.keys(Sm.OPTION).slice(0,120).join(', '))}catch(e){}try{if(Sm.PLUGIN)diag('SmartEditor.PLUGIN: '+Object.keys(Sm.PLUGIN).slice(0,120).join(', '))}catch(e){}var ed=null;try{if(typeof Sm.getEditor==='function')ed=Sm.getEditor()}catch(e){diag('getEditor() throw: '+e)}if(!ed){try{var eds=Sm._editors;diag('_editors typeof: '+(typeof eds));if(eds){if(Object.prototype.toString.call(eds)==='[object Array]'){ed=eds[0]}else{var vk=Object.keys(eds);diag('_editors keys: '+vk.slice(0,20).join(', '));if(vk.length)ed=eds[vk[0]]}}}catch(e){diag('_editors 실패: '+e)}}if(ed){dumpO('ed',ed,80);['getStore','store','_store','vm','viewModel','getDocument','document','model'].forEach(function(sk){try{var sv=(typeof ed[sk]==='function')?ed[sk]():ed[sk];if(sv)dumpO('ed.'+sk,sv,60)}catch(e){}})}else{diag('ed 없음 — getEditor()/_editors 모두 실패')}}}catch(e){diag('SmartEditor 탐색 실패: '+e)}try{var SEg=win.SE;if(SEg){var sek=Object.keys(SEg);diag('SE 키('+sek.length+'): '+sek.slice(0,120).join(', '));sek.forEach(function(kk){if(HL.test(kk)){try{var sv=SEg[kk];if(sv&&typeof sv==='object')diag('  SE.'+kk+' 키: '+Object.keys(sv).slice(0,40).join(', '))}catch(e){}}})}}catch(e){diag('SE 탐색 실패: '+e)}try{if(ed&&typeof ed.getDocumentData==='function'){var dd=ed.getDocumentData();try{diag('getDocumentData top keys: '+Object.keys(dd).join(', '))}catch(e){}try{diag('getDocumentData JSON: '+JSON.stringify(dd).slice(0,4000))}catch(e){diag('getDocumentData stringify 실패: '+e)}}else{diag('ed.getDocumentData 없음')}}catch(e){diag('getDocumentData() 실패: '+e)}try{if(Sm&&Sm.COMMAND){if(Sm.COMMAND.IMAGE)diag('COMMAND.IMAGE(full): '+JSON.stringify(Sm.COMMAND.IMAGE));if(Sm.COMMAND.COMMON)diag('COMMAND.COMMON(full): '+JSON.stringify(Sm.COMMAND.COMMON))}}catch(e){diag('COMMAND.IMAGE/COMMON 실패: '+e)}try{if(ed&&ed._commandManager)dumpO('ed._commandManager',ed._commandManager,80)}catch(e){diag('_commandManager 실패: '+e)}try{if(ed&&ed._documentService)dumpO('ed._documentService',ed._documentService,80)}catch(e){diag('_documentService 실패: '+e)}try{if(ed&&ed._editingService)dumpO('ed._editingService',ed._editingService,80)}catch(e){diag('_editingService 실패: '+e)}function fnSrc(fn,cap){try{return String(fn).replace(/\s+/g,' ').slice(0,cap||300)}catch(e){return '(src 실패)'}}try{var cm=ed&&ed._commandManager;var cmap=cm&&(cm._commandMap||cm.commandMap||cm._commands);if(cmap){var cmk=Object.keys(cmap);diag('_commandMap keys('+cmk.length+'): '+cmk.slice(0,200).join(', '));diag('이미지명령 등록여부: insertImagesByFile='+(cmap.insertImagesByFile!=null)+' insertImages='+(cmap.insertImages!=null)+' insertImagesByUrl='+(cmap.insertImagesByUrl!=null))}else{diag('_commandMap 없음 (cm='+(typeof cm)+')')}}catch(e){diag('_commandMap 덤프 실패: '+e)}try{var cm2=ed&&ed._commandManager;['insertImages','insertImagesByFile','insertImagesByUrl'].forEach(function(nm){try{var c=cm2&&typeof cm2.getCommand==='function'?cm2.getCommand(nm):null;if(c==null){diag('getCommand('+nm+') → null/none');return}var own=[];try{own=Object.getOwnPropertyNames(c)}catch(e){}var proto=[];try{var pp=Object.getPrototypeOf(c);if(pp)proto=Object.getOwnPropertyNames(pp)}catch(e){}diag('getCommand('+nm+'): '+(typeof c)+' own=['+own.slice(0,40).join(', ')+'] proto=['+proto.slice(0,40).join(', ')+']');['execute','exec','_execute','run'].forEach(function(fn){try{if(c[fn]&&typeof c[fn]==='function')diag('  '+nm+'.'+fn+' = '+fnSrc(c[fn],300))}catch(e){}})}catch(e){diag('getCommand('+nm+') 실패: '+e)}})}catch(e){diag('getCommand 덤프 실패: '+e)}try{if(ed&&ed._commandManager&&ed._commandManager.execCommand)diag('execCommand src: '+fnSrc(ed._commandManager.execCommand,400))}catch(e){diag('execCommand src 실패: '+e)}try{if(ed&&ed._commandManager&&ed._commandManager.loadCommand)diag('loadCommand src: '+fnSrc(ed._commandManager.loadCommand,300));else diag('loadCommand 없음')}catch(e){diag('loadCommand src 실패: '+e)}try{var es=ed&&ed._editingService;if(es){['insertByExternalPaste','insertByDrop','insertComponentsWithData','insert','insertImagesByFile'].forEach(function(m){try{if(es[m]&&typeof es[m]==='function')diag('_editingService.'+m+' = '+fnSrc(es[m],300))}catch(e){}})}else{diag('_editingService 없음')}}catch(e){diag('_editingService insert 덤프 실패: '+e)}try{if(Sm&&Sm.PLUGIN){try{diag('SmartEditor.PLUGIN(full): '+JSON.stringify(Sm.PLUGIN))}catch(e){diag('SmartEditor.PLUGIN keys: '+Object.keys(Sm.PLUGIN).join(', '))}}if(ed){var ek=[];try{ek=Object.keys(ed)}catch(e){}ek.forEach(function(k){if(/plugin/i.test(k)){try{dumpO('ed.'+k,ed[k],60)}catch(e){}}})}}catch(e){diag('플러그인 덤프 실패: '+e)}try{if(ed){var names=[];try{names=Object.getOwnPropertyNames(ed)}catch(e){}try{var prt=Object.getPrototypeOf(ed);if(prt)Object.getOwnPropertyNames(prt).forEach(function(n){if(names.indexOf(n)===-1)names.push(n)})}catch(e){}names.forEach(function(k){if(!/image|upload|photo/i.test(k))return;try{var v=ed[k];if(v&&typeof v==='object'){var vn=[];try{vn=Object.getOwnPropertyNames(v)}catch(e){}var hits=vn.filter(function(n){return /upload|insert|add|image|file/i.test(n)});diag('ed.'+k+' ('+vn.length+') ★'+hits.slice(0,40).join(', '))}else{diag('ed.'+k+' : '+(typeof v))}}catch(e){diag('ed.'+k+' 접근불가')}});try{if(ed._representativeImageService)dumpO('ed._representativeImageService',ed._representativeImageService,60)}catch(e){}}}catch(e){diag('이미지/업로드 서비스 덤프 실패: '+e)}try{var ds=ed&&ed._documentService;if(ds&&typeof ds.getComponentsByCtype==='function'){var imgs0=ds.getComponentsByCtype('image');if(imgs0&&imgs0.length)diag('기존 image 컴포넌트[0]: '+JSON.stringify(imgs0[0]).slice(0,800));else diag('기존 image 컴포넌트 없음')}}catch(e){diag('기존 image 컴포넌트 덤프 실패: '+e)}try{var cm3=ed&&ed._commandManager;['insertImages','insertImagesByFile','insertImagesByUrl'].forEach(function(nm){try{var c=cm3&&typeof cm3.getCommand==='function'?cm3.getCommand(nm):null;if(c==null){diag('getCommand('+nm+') → null/none');return}try{if(c._method)diag(nm+'._method = '+fnSrc(c._method,700));else diag(nm+'._method 없음')}catch(e){}try{diag(nm+'._instance ctor = '+(c._instance&&c._instance.constructor&&c._instance.constructor.name))}catch(e){}try{if(c._instance){var ip=Object.getPrototypeOf(c._instance);if(ip)diag(nm+'._instance proto: '+Object.getOwnPropertyNames(ip).slice(0,80).join(', '))}}catch(e){}}catch(e){diag(nm+' _method 덤프 실패: '+e)}})}catch(e){diag('_method 덤프 실패: '+e)}try{var ius=ed&&ed._videoUploadService&&ed._videoUploadService._imageUploadService;if(ius){dumpO('_imageUploadService',ius,80);['upload','uploadImages','uploadByFile','uploadFiles','uploadImage','uploadImagesByFile'].forEach(function(m){try{if(ius[m]&&typeof ius[m]==='function')diag('_imageUploadService.'+m+' = '+fnSrc(ius[m],500))}catch(e){}})}else{diag('_imageUploadService 없음')}}catch(e){diag('_imageUploadService 덤프 실패: '+e)}try{var es2=ed&&ed._editingService;if(es2){try{if(es2.insertImagesByFile&&typeof es2.insertImagesByFile==='function')diag('_editingService.insertImagesByFile = '+fnSrc(es2.insertImagesByFile,500))}catch(e){}try{if(es2.getComponentInfoList&&typeof es2.getComponentInfoList==='function')diag('_editingService.getComponentInfoList = '+fnSrc(es2.getComponentInfoList,300))}catch(e){}}}catch(e){diag('_editingService 진입 덤프 실패: '+e)}diag('🔬 API 탐색 끝 — 위 로그를 복사해줘')}function sid(){try{if(window.crypto&&crypto.randomUUID)return 'SE-'+crypto.randomUUID()}catch(e){}return 'SE-'+Date.now().toString(36)+Math.random().toString(36).slice(2,10)}function rep(ch,n){var o='';for(var i=0;i<n;i++)o+=ch;return o}function starBar(sc){var s=Math.max(0,Math.min(10,Number(sc)||0));var full=Math.floor(s/2),half=(s%2)?1:0,empty=5-full-half;return rep('★',full)+rep('⯪',half)+rep('☆',empty)}function tn(value,style){var o={"@ctype":"textNode",id:sid(),value:''+(value==null?'':value)};if(style){style["@ctype"]="nodeStyle";o.style=style}return o}function para(nodes,align,lineHeight){var o={"@ctype":"paragraph",id:sid(),nodes:nodes};if(align||lineHeight){var ps={"@ctype":"paragraphStyle"};if(align)ps.align=align;if(lineHeight)ps.lineHeight=lineHeight;o.paragraphStyle=ps}return o}function txtC(paras){return{"@ctype":"text",id:sid(),layout:"default",value:paras}}function hrC(){return{"@ctype":"horizontalLine",id:sid(),layout:"line1"}}function imgC(meta){return{"@ctype":"image",id:sid(),layout:"default",align:"center",src:meta.displayUrl,internalResource:true,represent:false,path:meta.path||'',domain:"https://blogfiles.pstatic.net",fileSize:Number(meta.fileSize)||0,width:Number(meta.width)||0,height:Number(meta.height)||0,originalWidth:Number(meta.width)||0,originalHeight:Number(meta.height)||0,fileName:meta.fileName||"image.jpg",format:"normal",displayFormat:"normal",imageLoaded:true,contentMode:"fit",origin:{"@ctype":"imageOrigin",srcFrom:"local"},ai:false}}function cTxt(text,bold,bg){var c={"@ctype":"tableCell",borderInlineStyle:"border: 1px solid #000000;",colSpan:1,rowSpan:1,width:50.0,height:40.0,value:[para([tn(text,bold?{bold:true}:null)],'center')]};if(bg)c.backgroundColor=bg;return c}function rtT(items,overall){var rows=[];rows.push({"@ctype":"tableRow",cells:[cTxt('항목',true,'#faf3f6'),cTxt('별점',true,'#faf3f6')]});(items||[]).forEach(function(it){if(!it)return;var aspect=(''+(it.aspect||'')).trim();if(!aspect)return;var sc=Math.max(0,Math.min(10,Number(it.score)||0));rows.push({"@ctype":"tableRow",cells:[cTxt(aspect,false),cTxt(starBar(sc)+' ('+sc+'/10)',false)]})});var ov=Math.max(0,Math.min(10,Number(overall)||0));rows.push({"@ctype":"tableRow",cells:[cTxt('총점',true),cTxt(starBar(ov)+' ('+ov+'/10)',true)]});return{"@ctype":"table",id:sid(),layout:"default",width:100.0,rows:rows}}var EMPH_RE=/(\*\*[^*]+\*\*|\*[^*]+\*|`[^`]+`|==[^=]+==)/g;function emN(line){var s=''+(line==null?'':line),nodes=[],last=0,m;EMPH_RE.lastIndex=0;while((m=EMPH_RE.exec(s))!==null){if(m.index>last)nodes.push(tn(s.slice(last,m.index),null));var tok=m[0],inner,style;if(tok.slice(0,2)==='**'){inner=tok.slice(2,-2);style={bold:true}}else if(tok.charAt(0)==='*'){inner=tok.slice(1,-1);style={fontColor:'#e64980',bold:true}}else if(tok.charAt(0)==='`'){inner=tok.slice(1,-1);style={fontColor:'#1c7ed6',bold:true}}else{inner=tok.slice(2,-2);style={backgroundColor:'#ffec99',bold:true}}nodes.push(tn(inner,style));last=EMPH_RE.lastIndex}if(last<s.length)nodes.push(tn(s.slice(last),null));if(!nodes.length)nodes.push(tn('',null));return nodes}function quoC(text,foot){return{"@ctype":"quotation",id:sid(),layout:"default",value:[para(emN((''+(text||'')).replace(/\n/g,' ')),null)],source:[para([tn(foot||'내돈내산',null)],null)],align:"justify"}}function iK(u){if(!u)return '';u=''+u;var m=u.match(/\/blog-img\/(\d+)/),id=m?m[1]:u;var c=u.match(/[?&](?:amp;)?c=([0-9.,]+)/);return id+'|'+(c?c[1]:'')}function pHr(comps){if(!comps.length||comps[comps.length-1]["@ctype"]!=='horizontalLine')comps.push(hrC())}function bC(doc,metaBySrc){var comps=[];var imgCount=0;comps.push({"@ctype":"documentTitle",id:sid(),layout:"default",title:[{"@ctype":"paragraph",id:sid(),nodes:[{"@ctype":"textNode",id:sid(),value:''+(doc.title||'')}]}],subTitle:null,align:"center"});var headerParas=[];if(doc.visitDate)headerParas.push(para([tn('방문 날짜 : '+doc.visitDate,null)],'center'));headerParas.push(para([tn('✱ 내돈내산 데이트 후기 ✱',{fontColor:'#e64980',bold:true})],'center'));comps.push(txtC(headerParas));var blocks=doc.blocks||[];for(var b=0;b<blocks.length;b++){var blk=blocks[b];if(!blk||typeof blk!=='object')continue;var t=blk.type;if(t==='heading'){var htext=(''+(blk.text||'')).trim();if(!htext)continue;pHr(comps);comps.push(txtC([para([tn(htext,{fontColor:'#222',fontSizeCode:'fs19',bold:true})],'center')]))}else if(t==='para'){var lines=(''+(blk.text||'')).replace(/\r\n/g,'\n').split('\n'),paras=[];for(var li=0;li<lines.length;li++){var ln=lines[li].trim();if(ln)paras.push(para(emN(ln),'center'))}if(paras.length)comps.push(txtC(paras))}else if(t==='image'){var meta=metaBySrc[iK(blk.__src)];if(meta&&meta["@ctype"]==='image'){var nc={};for(var kk in meta)nc[kk]=meta[kk];nc.id=sid();nc.align='center';nc.represent=(imgCount===0);imgCount++;comps.push(nc)}else if(meta){var ic=imgC(meta);ic.represent=(imgCount===0);imgCount++;comps.push(ic)}else{diag('정식: 이미지 메타 없음 (src='+(blk.__src||'')+')')}}else if(t==='info'){var ip=[];(blk.items||[]).forEach(function(it){if(!it)return;var label=(''+(it.label||'')).trim(),value=(''+(it.value||'')).trim();if(!label||!value)return;if(label.indexOf('방문')!==-1&&(label.indexOf('날짜')!==-1||label.indexOf('일')!==-1))return;ip.push(para([tn(label+' ',{bold:true}),tn(': '+value,null)],'center',2.0))});if(ip.length)comps.push(txtC(ip))}else if(t==='ratings'){comps.push(rtT(blk.items||[],doc.overallScore))}else if(t==='faq'){var fp=[];(blk.items||[]).forEach(function(it){if(!it)return;var q=(''+(it.q||'')).trim(),a=(''+(it.a||'')).trim();if(!q||!a)return;fp.push(para([tn('Q. '+q,{bold:true})],null));fp.push(para([tn('A. '+a,null)],null))});if(fp.length)comps.push(txtC(fp))}else if(t==='quote'){pHr(comps);comps.push(quoC(blk.text||'',doc.visitFoot?(doc.visitFoot+' 방문 · 내돈내산'):'내돈내산'))}}var tags=(doc.hashtags||[]).map(function(x){return(''+x).trim()}).filter(Boolean);if(tags.length)comps.push(txtC([para([tn(tags.join(' '),{fontColor:'#e0559b',bold:true})],'center')]));return comps}function getEd(){try{var w=fsw();if(!w)return null;var Sm=w.SmartEditor;if(!Sm)return null;var e=null;try{if(typeof Sm.getEditor==='function')e=Sm.getEditor()}catch(x){}if(!e&&Sm._editors){try{var vk=Object.keys(Sm._editors);e=Sm._editors.blogpc001||(vk.length?Sm._editors[vk[0]]:null)}catch(x){}}return e}catch(x){return null}}function procN(u){var raw=(u.ta.value||'').trim(),p;try{p=JSON.parse(raw)}catch(e){p=null}diag('정식 삽입 시작 — 페이로드 '+(p&&p.copy_html?'OK':'없음'));if(!p||!p.copy_html){diag('중단: 페이로드 없음');ss(u,"페이로드가 안 보여 — 앱에서 '네이버로 보내기' 후 이 칸에 붙여넣어줘",true);return}if(!p.doc||!p.doc.blocks){diag('중단: doc 없음(앱 업데이트 필요)');ss(u,'앱을 업데이트해야 해(정식 삽입용 데이터 없음)',true);return}var win=fsw()||window;var Sm=win&&win.SmartEditor;var ed=getEd();if(!ed||typeof ed.getDocumentData!=='function'||typeof ed.setDocumentData!=='function'){diag('중단: 에디터(getDocumentData/setDocumentData) 못 찾음');ss(u,'에디터를 못 찾았어 — 네이버 글쓰기 화면에서 실행해줘',true);return}var RB=(win&&win.Blob)||Blob;var RF=(win&&win.File)||File;var RU8=(win&&win.Uint8Array)||Uint8Array;function d2bIn(d){var c=d.indexOf(','),head=d.slice(0,c),b64=d.slice(c+1);var mime=(head.match(/data:([^;]+)/)||[])[1]||'image/jpeg';var bin=atob(b64),n=bin.length,arr=new RU8(n);for(var j=0;j<n;j++)arr[j]=bin.charCodeAt(j);return new RB([arr],{type:mime})}var imgs=Array.isArray(p.images)?p.images:[];var files=[],fileSrcs=[];for(var i=0;i<imgs.length;i++){var im=imgs[i];if(!im||!im.dataUri||!im.src)continue;var blob=d2bIn(im.dataUri);var name='image'+i+'.jpg';var f;try{f=new RF([blob],name,{type:blob.type||'image/jpeg'})}catch(e){f=blob;try{f.name=name}catch(e2){}}files.push(f);fileSrcs.push(im.src)}diag('업로드 대상 파일 '+files.length+'장 (렐름='+(win===window?'self':'iframe')+')');u.go.disabled=true;var metaByBlockSrc={};function buildAndSet(label){try{var comps=bC(p.doc,metaByBlockSrc);var got=Object.keys(metaByBlockSrc).length;diag('컴포넌트 '+comps.length+'개 생성 (이미지 '+got+'/'+files.length+')');var d=ed.getDocumentData();if(!d||!d.document){ss(u,'getDocumentData 형식 예상밖',true);u.go.disabled=false;return}d.document.components=comps;ed.setDocumentData(d);diag('정식 문서 삽입 완료 (이미지 '+got+'장)'+(label?' ['+label+']':''));ss(u,'정식 문서 삽입 완료 (이미지 '+got+'장) — 편집기 확인 후 발행')}catch(e){diag('buildAndSet 실패: '+(e&&e.message?e.message:e));ss(u,'문서 삽입 실패 — 진단 로그 확인',true)}u.go.disabled=false}if(!files.length){buildAndSet();return}var ius=(ed._videoUploadService&&ed._videoUploadService._imageUploadService)||null;diag('업로더 점검: ius='+(!!ius)+' uploadImagesFromFiles='+(ius&&typeof ius.uploadImagesFromFiles)+' uploadImages='+(ius&&typeof ius.uploadImages)+' createSourceList='+(ius&&typeof ius.createSourceList));function runCommandFallback(){try{if(ed.focusFirstText)ed.focusFirstText()}catch(e){}if(!ed._commandManager||typeof ed._commandManager.execCommand!=='function'||!Sm||!Sm.COMMAND||!Sm.COMMAND.IMAGE){diag('폴백 불가: _commandManager.execCommand / COMMAND.IMAGE 없음');ss(u,'에디터 명령 API를 못 찾았어 — 다시 시도해줘',true);u.go.disabled=false;return}diag('폴백: execCommand · COMMAND.IMAGE='+JSON.stringify(Sm.COMMAND.IMAGE));function docComps(){try{return ed.getDocumentData().document.components||[]}catch(e){return[]}}var before={};docComps().forEach(function(c){if(c&&c["@ctype"]==='image')before[c.id]=true});var CMD=Sm.COMMAND.IMAGE.INSERT_IMAGE_FILES;var uploaded=false,usedShape='';function tryShape(shape,arg){if(uploaded)return;try{ed._commandManager.execCommand(CMD,arg);uploaded=true;usedShape=shape;diag('폴백 호출 OK — shape='+shape)}catch(e){diag('폴백 호출 실패 shape='+shape+': '+(e&&e.message?e.message:e))}}tryShape('files[]',files);if(!uploaded)tryShape('{files}',{files:files});if(!uploaded)tryShape('{imageFiles}',{imageFiles:files});if(!uploaded){try{files.forEach(function(f){ed._commandManager.execCommand(CMD,[f])});uploaded=true;usedShape='loop[f]';diag('폴백 호출 OK — shape=loop[f]')}catch(e){diag('폴백 단일루프 실패: '+(e&&e.message?e.message:e))}}if(!uploaded){ss(u,'네이티브 업로드 호출 실패 — 진단 로그 확인',true);u.go.disabled=false;return}var t0=Date.now(),MAXMS=40000,loggedFirst=false;ss(u,'네이티브 업로드 대기…');var iv=setInterval(function(){var processing=false;try{if(typeof ed.isDocumentProcessing==='function')processing=!!ed.isDocumentProcessing();else if(typeof ed.isDocumentProcessing==='boolean')processing=ed.isDocumentProcessing}catch(e){}var ready=[];docComps().forEach(function(c){if(!c||c["@ctype"]!=='image')return;if(before[c.id])return;var s=''+(c.src||'');if(s.indexOf('https://blogfiles.pstatic.net')!==0)return;if(c.imageLoaded===false)return;ready.push(c)});if(!loggedFirst&&ready.length){loggedFirst=true;try{diag('첫 네이티브 이미지: '+JSON.stringify(ready[0]).slice(0,600))}catch(e){}}ss(u,'네이티브 업로드 '+ready.length+'/'+files.length+' 준비');var timeUp=(Date.now()-t0)>MAXMS;if((ready.length>=files.length&&!processing)||timeUp){clearInterval(iv);diag('폴백 완료 '+ready.length+'/'+files.length+(timeUp?' (타임아웃)':'')+' shape='+usedShape);var n=Math.min(ready.length,fileSrcs.length);for(var k=0;k<n;k++)metaByBlockSrc[iK(fileSrcs[k])]=ready[k];buildAndSet('fallback')}},500)}if(!ius){diag('_imageUploadService 없음 → execCommand 폴백');runCommandFallback();return}diag('네이티브 업로더 사용 — _imageUploadService');function normalize(ret){return Promise.resolve(ret).then(function(arr){arr=Array.isArray(arr)?arr:[arr];return Promise.all(arr.map(function(x){return Promise.resolve(x)}))})}function extractMetas(results){var out=[];for(var r=0;r<results.length;r++){var res=results[r]||{};var code=res.code;var resp=(res&&(res.response||res.result||res))||{};if(code&&code!=='SUCCESS'){diag('업로드결과['+r+'] code='+code+' (스킵)');continue}var rawUrl=resp.url||resp.src||resp.imageUrl||resp.originalUrl||(resp.image&&resp.image.url)||'';var path=resp.path||resp.fullPath||(resp.image&&resp.image.path)||'';var width=resp.width||resp.originalWidth||(resp.image&&resp.image.width)||0;var height=resp.height||resp.originalHeight||(resp.image&&resp.image.height)||0;var fileSize=resp.fileSize||resp.size||0;var fileName=resp.fileName||resp.name||'image.jpg';var base='';if(path){base='https://blogfiles.pstatic.net'+(String(path).charAt(0)==='/'?'':'/')+path;if(fileName&&base.indexOf('/'+fileName)===-1)base+='/'+fileName}else if(rawUrl){base=/^https?:/i.test(rawUrl)?rawUrl:('https://blogfiles.pstatic.net'+(String(rawUrl).charAt(0)==='/'?'':'/')+rawUrl);if(fileName&&base.indexOf('/'+fileName)===-1&&!/\.[a-z0-9]{2,4}(\?|$)/i.test(base.split('/').pop()))base+='/'+fileName}if(base&&base.indexOf('?type=')===-1)base+=(base.indexOf('?')===-1?'?':'&')+'type=w1';diag('업로드결과['+r+'] code='+(code||'?')+' url='+base+' path='+path+' '+width+'x'+height);diag('정식 src['+r+']='+base);if(base)out.push({idx:r,meta:{displayUrl:base,path:path,width:width,height:height,fileSize:fileSize,fileName:fileName}})}return out}function attempt(label,fn){return new Promise(function(resolve){var ret;try{ret=fn()}catch(e){diag('시도 '+label+': err '+(e&&e.message?e.message:e));resolve({ok:false});return}normalize(ret).then(function(results){results=results||[];diag('시도 '+label+' 결과 개수='+results.length);var metas=extractMetas(results);if(metas.length){try{diag('첫 업로드 원본: '+JSON.stringify(results[0]).slice(0,700))}catch(e){}diag('시도 '+label+': ok 결과 '+metas.length);resolve({ok:true,metas:metas})}else{diag('시도 '+label+': 결과 0 (usable url 없음)');resolve({ok:false})}},function(e){diag('시도 '+label+': err '+(e&&e.message?e.message:e));resolve({ok:false})})})}ss(u,'네이버 인프라 업로드 중… (0/'+files.length+')');attempt('A',function(){diag('uploadImagesFromFiles 호출 (files='+files.length+')');if(typeof ius.uploadImagesFromFiles!=='function')throw new Error('uploadImagesFromFiles 미존재');var ret=ius.uploadImagesFromFiles(files);diag('A 반환 typeof='+typeof ret+' isPromise='+(ret&&typeof ret.then==='function')+' isArray='+Array.isArray(ret));return ret}).then(function(a){if(a.ok)return a;if(typeof ius.uploadImages==='function'&&typeof ius.createSourceList==='function'){return attempt('B',function(){return ius.uploadImages(ius.createSourceList(files))})}diag('시도 B 스킵 (uploadImages/createSourceList 미존재)');return{ok:false}}).then(function(b){if(b.ok)return b;if(typeof ius.uploadImages==='function'){return attempt('C',function(){return ius.uploadImages(files.map(function(f,i){return{id:'cd'+i,source:f}}))})}diag('시도 C 스킵 (uploadImages 미존재)');return{ok:false}}).then(function(res){if(!res.ok||!res.metas||!res.metas.length){diag('A/B/C 모두 실패 → execCommand 폴백');runCommandFallback();return}var metas=res.metas,okCount=0;for(var k=0;k<metas.length;k++){var idx=metas[k].idx;if(idx<fileSrcs.length){metaByBlockSrc[iK(fileSrcs[idx])]=metas[k].meta;okCount++}}ss(u,'네이버 인프라 업로드 중… ('+okCount+'/'+files.length+')');buildAndSet('native')}).catch(function(e){diag('네이티브 업로드 예외: '+(e&&e.message?e.message:e)+' → execCommand 폴백');runCommandFallback()})}function el(tag,css,text){var e=document.createElement(tag);if(css)e.style.cssText=css;if(text!=null)e.textContent=text;return e}function ui(){var old=document.getElementById('cd-nv-ov');if(old&&old.parentNode)old.parentNode.removeChild(old);var ov=el('div',"position:fixed;left:50%;top:16px;transform:translateX(-50%);z-index:2147483647;width:560px;max-width:94vw;background:#fff;color:#222;border:1px solid #e5e5e5;border-radius:12px;box-shadow:0 8px 40px rgba(0,0,0,.3);padding:16px;font-family:-apple-system,BlinkMacSystemFont,'Malgun Gothic',sans-serif;font-size:14px;line-height:1.5;max-height:90vh;overflow:auto;");ov.id='cd-nv-ov';['keydown','keyup','keypress','click','mousedown','paste'].forEach(function(ev){ov.addEventListener(ev,function(e){e.stopPropagation()},false)});ov.appendChild(el('div','font-weight:800;font-size:15px;margin-bottom:8px;','🖼 우리 후기 사진 → 네이버 업로드'));ov.appendChild(el('div','color:#444;margin-bottom:4px;',"① (토큰 자동확보 안 되면) 에디터를 한 번 클릭하거나 사진 1장 추가 → '토큰 확보 ✓'"));ov.appendChild(el('div','color:#444;margin-bottom:4px;','② 아래 칸에 앱 페이로드 붙여넣기 (Ctrl+V 또는 길게 눌러 붙여넣기)'));ov.appendChild(el('div','color:#444;margin-bottom:8px;','③ 처리 ▶ (임시로 넣은 사진은 나중에 지워도 됨)'));var ta=el('textarea','width:100%;height:70px;box-sizing:border-box;border:1px solid #ccc;border-radius:8px;padding:8px;font-size:12px;resize:vertical;');ta.setAttribute('placeholder','여기에 붙여넣기 (Ctrl+V / 길게 눌러 붙여넣기)');ov.appendChild(ta);var st=el('div','margin:8px 0;min-height:1.2em;color:#333;font-weight:700;');ov.appendChild(st);var ac=el('div','display:flex;align-items:center;gap:8px;margin-top:2px;flex-wrap:wrap;');var go=el('button','background:#e64980;color:#fff;border:0;border-radius:8px;padding:9px 16px;font-weight:800;cursor:pointer;','처리 ▶');var db=el('button','background:#12b886;color:#fff;border:0;border-radius:8px;padding:9px 12px;font-weight:700;cursor:pointer;','🅳 데이터URI로 복사 (업로드 없이 테스트)');var nb=el('button','background:#7048e8;color:#fff;border:0;border-radius:8px;padding:9px 12px;font-weight:800;cursor:pointer;','🅢 정식 삽입 (setDocumentData)');var xp=el('button','background:#495057;color:#fff;border:0;border-radius:8px;padding:9px 12px;font-weight:700;cursor:pointer;','🔬 에디터 API 탐색');var cl=el('button','background:#eee;color:#333;border:0;border-radius:8px;padding:9px 14px;cursor:pointer;','닫기');ac.appendChild(go);ac.appendChild(nb);ac.appendChild(db);ac.appendChild(xp);ac.appendChild(cl);ov.appendChild(ac);var rw=el('div','margin-top:10px;display:none;');var cb=el('button','background:#1c7ed6;color:#fff;border:0;border-radius:8px;padding:9px 16px;font-weight:800;cursor:pointer;','📋 결과 복사');var rt=el('textarea','width:100%;height:70px;box-sizing:border-box;border:1px solid #ccc;border-radius:8px;padding:8px;font-size:11px;margin-top:8px;resize:vertical;');rw.appendChild(cb);rw.appendChild(rt);rw.appendChild(el('div','color:#666;font-size:12px;margin-top:4px;','복사가 안 되면 위 칸을 전체선택(Ctrl+A)→복사(Ctrl+C)해서 붙여넣어'));ov.appendChild(rw);var md=el('details','margin-top:12px;border-top:1px solid #eee;padding-top:8px;');md.appendChild(el('summary','cursor:pointer;font-weight:700;color:#555;','🔑 토큰 수동입력 (자동이 안 될 때)'));md.appendChild(el('div','color:#666;font-size:12px;margin:6px 0;','DevTools → session-key 요청의 se-authorization 헤더 값(약 1시간 유효)을 첫 줄에, (선택) se-app-id를 둘째 줄에 붙여넣어.'));var mt=el('textarea','width:100%;height:64px;box-sizing:border-box;border:1px solid #ccc;border-radius:8px;padding:8px;font-size:11px;resize:vertical;');mt.setAttribute('placeholder','첫 줄: se-authorization JWT\n둘째 줄(선택): se-app-id');md.appendChild(mt);ov.appendChild(md);var dd=el('details','margin-top:10px;border-top:1px solid #eee;padding-top:8px;');dd.appendChild(el('summary','cursor:pointer;font-weight:700;color:#555;','🔍 진단 로그 (참고용)'));var dt=el('textarea','width:100%;height:240px;box-sizing:border-box;border:1px solid #ccc;border-radius:8px;padding:8px;font-size:11px;margin-top:8px;resize:vertical;background:#fafafa;');dt.readOnly=true;dt.setAttribute('placeholder','아직 관측 없음 — 에디터를 클릭/사진 추가하면 여기에 무엇을 봤는지 쌓여요 (선택/복사 가능)');dd.appendChild(dt);ov.appendChild(dd);document.body.appendChild(ov);setTimeout(function(){try{ta.focus()}catch(e){}},50);cl.addEventListener('click',function(){if(ov.parentNode)ov.parentNode.removeChild(ov)});return{ov:ov,ta:ta,status:st,go:go,nativeBtn:nb,dataBtn:db,explore:xp,resultWrap:rw,copyBtn:cb,resultTa:rt,manualTa:mt,diagTa:dt}}function ss(u,m,er){u.status.textContent=m;u.status.style.color=er?'#c0392b':'#333'}function fin(u,h,done,total){diag('완료 '+done+'/'+total);ss(u,'완료 ('+done+'/'+total+') — 아래 버튼 눌러 복사한 뒤 본문에 붙여넣어');u.resultTa.value=h;u.resultWrap.style.display='block';u.go.disabled=false;u.copyBtn.onclick=function(){wc(h).then(function(){ss(u,'복사됨 ✓ — 네이버 본문에 Ctrl+V로 붙여넣어')},function(err){L('write 실패',err);ss(u,'자동복사 실패 — 아래 칸 전체선택 후 복사해줘',true);try{u.resultTa.focus();u.resultTa.select()}catch(e){}})}}function procD(u){var raw=(u.ta.value||'').trim();var p;try{p=JSON.parse(raw)}catch(e){p=null}var ni=(p&&Array.isArray(p.images))?p.images.length:0;diag('데이터URI 모드 — 이미지 '+ni+'장, 페이로드 '+(p&&p.copy_html?'OK':'없음'));if(!p||!p.copy_html){diag('중단: 페이로드 없음');ss(u,"페이로드가 안 보여 — 앱에서 '네이버로 보내기' 후 이 칸에 붙여넣어줘",true);return}var h=p.copy_html,imgs=Array.isArray(p.images)?p.images:[],done=0;imgs.forEach(function(img,i){if(!img||!img.dataUri||!img.src)return;var b64=img.dataUri.slice(img.dataUri.indexOf(',')+1);var kb=Math.round(b64.length*3/4/1024);diag('dataURI 심기['+i+'] (크기 ~'+kb+'kb)');h=rs(h,img.src,img.dataUri);done++});diag('데이터URI 심기 완료 '+done+'/'+imgs.length);ss(u,'데이터URI로 심음 — 네이버 본문에 붙여넣어 (네이버가 알아서 업로드/등록하는지 확인)');u.resultTa.value=h;u.resultWrap.style.display='block';u.copyBtn.onclick=function(){wc(h).then(function(){ss(u,'복사됨 ✓ — 네이버 본문에 Ctrl+V로 붙여넣어')},function(err){L('write 실패',err);ss(u,'자동복사 실패 — 아래 칸 전체선택 후 복사해줘',true);try{u.resultTa.focus();u.resultTa.select()}catch(e){}})}}function proc(u){var raw=(u.ta.value||'').trim();var p;try{p=JSON.parse(raw)}catch(e){p=null}var ni=(p&&Array.isArray(p.images))?p.images.length:0;diag('처리 시작 — 이미지 '+ni+'장, 페이로드 '+(p&&p.copy_html?'OK':'없음'));if(!p||!p.copy_html){diag('중단: 페이로드 없음');ss(u,"페이로드가 안 보여 — 앱에서 '네이버로 보내기' 후 이 칸에 붙여넣어줘",true);return}var h=p.copy_html,imgs=Array.isArray(p.images)?p.images:[];L('이미지',imgs.length);u.go.disabled=true;if(!imgs.length){fin(u,h,0,0);return}var man=(u.manualTa.value||'').trim(),mAuth='',mApp='';if(man){var ml=man.split(/\r?\n/);mAuth=(ml[0]||'').trim();mApp=(ml[1]||'').trim()}ss(u,'세션키 준비 중…');getSK(mAuth,mApp).then(function(key){L('세션키',key);var b=gb();L('blogId',b);var ch=Promise.resolve(),done=0;imgs.forEach(function(img,i){ch=ch.then(function(){ss(u,'사진 업로드 중… ('+(i+1)+'/'+imgs.length+')');var bl=d2b(img.dataUri);return up(key,b,bl,i).then(function(meta){h=rs(h,img.src,meta.displayUrl);done++}).catch(function(e){L('img'+i+' 실패',e);diag('업로드['+i+'] 실패: '+(e&&e.message?e.message:e));ss(u,'사진 '+(i+1)+' 실패 — 진단 로그 확인 (계속 진행)',true)})})});return ch.then(function(){fin(u,h,done,imgs.length)})}).catch(function(e){L('세션키 실패',e);diag('세션키 실패: '+(e&&e.message?e.message:e));ss(u,(e&&e.message?e.message:''+e),true);u.go.disabled=false})}var TOKEN_OK='토큰 확보 ✓ (페이로드 붙여넣고 처리)';var WAITING='대기 중 — 에디터를 한 번 클릭(또는 사진 1장 추가)하면 토큰을 잡아요';var U=ui();window.__cdSKcb=function(){ss(U,TOKEN_OK)};window.__cdAuthCb=function(){ss(U,TOKEN_OK)};window.__cdDiagCb=function(){try{U.diagTa.value=(window.__cdDiag||[]).slice(-150).join('\n')}catch(e){}};itcp();ss(U,(window.__cdAuth||window.__cdSK)?TOKEN_OK:WAITING);if(window.__cdDiag&&window.__cdDiag.length)window.__cdDiagCb();U.go.addEventListener('click',function(){try{proc(U)}catch(e){L('처리 예외',e);ss(U,'오류: '+(e&&e.message?e.message:e),true)}});U.nativeBtn.addEventListener('click',function(){try{procN(U)}catch(e){L('정식 삽입 예외',e);ss(U,'오류: '+(e&&e.message?e.message:e),true)}});U.dataBtn.addEventListener('click',function(){try{procD(U)}catch(e){L('데이터URI 예외',e);ss(U,'오류: '+(e&&e.message?e.message:e),true)}});U.explore.addEventListener('click',function(){try{explore()}catch(e){diag('탐색 예외: '+e)}});L('오버레이 준비됨');})();
 */
