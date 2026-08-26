/*
 * 우리의 하루 — 네이버 후기 사진 업로더 (PLAIN BOOKMARKLET, PHASE 1)
 * ==========================================================================
 * Tampermonkey/확장 없이 '즐겨찾기(북마클릿)' 하나로 동작한다.
 *
 * 왜 북마클릿 + 클립보드 핸드오프인가:
 *   - 북마클릿 코드는 네이버 글쓰기 '페이지 안'에서 실행된다 → 네이버 로그인 쿠키가
 *     ambient하고, 네이버 엔드포인트 호출이 same-site라 CSP가 허용한다.
 *   - 하지만 북마클릿이 '우리 앱'을 fetch하는 건 네이버 페이지 CSP(connect-src)가
 *     막을 수 있다(Tampermonkey는 GM_xmlhttpRequest로 우회했지만 순수 북마클릿은
 *     불가). 그래서 우리 앱의 '📤 네이버로 보내기' 버튼이 '본문 HTML + 각 사진
 *     바이트(base64 data URI)'를 통째로 클립보드에 담고, 북마클릿은 클립보드만 읽고
 *     네이버 엔드포인트하고만 통신한다.
 *
 * 사용 흐름:
 *   1) 우리 앱 후기 상세에서 '📤 네이버로 보내기' 클릭(클립보드에 페이로드 복사).
 *   2) 네이버 PC 글쓰기 페이지에서 이 북마클릿(📷 사진 올리기) 클릭.
 *   3) 완료되면 '네이버 이미지로 교체된 본문 HTML'이 클립보드에 담긴다 → 본문에 붙여넣기.
 *
 * ⚠️ 라이브에서 확정할 CONFIG(아래) — 실제 붙여넣기로 값 확인:
 *   - DISPLAY_HOST_PREFIX: upphoto가 주는 <url>은 상대경로. 표시 호스트 접두
 *     (postfiles.pstatic.net vs blogfiles.pstatic.net)를 붙인다.
 *   - BLOG_ID_FALLBACK: userId 쿼리에 쓰는 블로그 아이디(페이지에서 추출 실패 시).
 *   - UPPHOTO_QUERY: 업로드 쿼리스트링(사용자 캡처 그대로).
 *
 * 이 파일은 '가독용 소스'다. 실제 즐겨찾기에 넣을 최종 javascript: 한 줄은 파일
 * 맨 아래 주석의 BOOKMARKLET(그리고 sibling naver-bookmarklet.txt)에 있다.
 * 프로덕션 최소화 규칙: 외부 스크립트 로드/eval 금지(CSP) — 전부 인라인.
 */
(function () {
  'use strict';

  // ======================= CONFIG (라이브에서 조정) =========================
  // upphoto <url>(상대경로) 앞에 붙일 표시 호스트. TODO(live): postfiles vs blogfiles 확인.
  var DISPLAY_HOST_PREFIX = 'https://postfiles.pstatic.net';
  // 블로그 아이디(userId). 페이지에서 못 뽑으면 이 값. TODO(live): 사용자 아이디로.
  var BLOG_ID_FALLBACK = 'yhc9355';
  // 업로드 쿼리스트링(userId는 코드가 채운다). TODO(live): 캡처와 일치 확인.
  var UPPHOTO_QUERY =
    'extractExif=true&extractAnimatedCnt=false&extractAnimatedInfo=true' +
    '&autorotate=true&extractDominantColor=false&type=&customQuery=' +
    '&denyAnimatedImage=false&skipXcamFiltering=false';
  var SESSION_KEY_URL =
    'https://platform.editor.naver.com/api/blogpc001/v1/photo-uploader/session-key';
  var UPPHOTO_BASE = 'https://blog.upphoto.naver.com';
  // ========================================================================

  var L = function () {
    try { console.log.apply(console, ['[nv]'].concat([].slice.call(arguments))); } catch (e) {}
  };

  function guessBlogId() {
    try {
      var m = location.pathname.match(/^\/([A-Za-z0-9_-]+)(?:\/|$)/);
      if (m && m[1] && m[1].indexOf('.naver') === -1) return m[1];
    } catch (e) {}
    return BLOG_ID_FALLBACK;
  }

  // data:image/...;base64,XXXX → Blob
  function dataUriToBlob(dataUri) {
    var comma = dataUri.indexOf(',');
    var header = dataUri.slice(0, comma);
    var b64 = dataUri.slice(comma + 1);
    var mime = (header.match(/data:([^;]+)/) || [])[1] || 'image/jpeg';
    var bin = atob(b64);
    var len = bin.length;
    var bytes = new Uint8Array(len);
    for (var i = 0; i < len; i++) bytes[i] = bin.charCodeAt(i);
    return new Blob([bytes], { type: mime });
  }

  // 업로드 응답 XML의 첫 <url> → 표시 URL(상대면 접두 붙임)
  function parseUploadedUrl(xmlText) {
    var el = null;
    try {
      var doc = new DOMParser().parseFromString(xmlText, 'text/xml');
      el = doc.querySelector('url') || doc.getElementsByTagName('url')[0];
    } catch (e) {}
    var raw = el && el.textContent ? el.textContent.trim() : '';
    if (!raw) {
      var m = xmlText.match(/<url>([^<]+)<\/url>/i); // 폴백: 정규식
      raw = m ? m[1].trim() : '';
    }
    if (!raw) return null;
    if (/^https?:\/\//i.test(raw)) return raw;
    return DISPLAY_HOST_PREFIX + (raw.charAt(0) === '/' ? '' : '/') + raw;
  }

  // copy_html 안의 정확한 src 문자열을 네이버 URL로 치환(&amp; 형태도 시도)
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
      var item = new ClipboardItem({
        'text/html': new Blob([html], { type: 'text/html' }),
        'text/plain': new Blob([plain || ''], { type: 'text/plain' })
      });
      return navigator.clipboard.write([item]);
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(html);
    }
    return Promise.reject(new Error('clipboard write unsupported'));
  }

  function getSessionKey() {
    L('세션키 요청…');
    return fetch(SESSION_KEY_URL, { credentials: 'include' })
      .then(function (r) {
        if (!r.ok) throw new Error('session-key HTTP ' + r.status);
        return r.json();
      })
      .then(function (j) {
        L('세션키 응답', j);
        if (!j || !j.sessionKey) throw new Error('세션키 없음(네이버 로그인 확인)');
        return j.sessionKey;
      });
  }

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

  function run() {
    L('시작. URL=', location.href);
    if (!navigator.clipboard || !navigator.clipboard.readText) {
      alert('이 브라우저는 클립보드 읽기를 지원하지 않아 (PC 크롬/엣지 최신 권장)');
      return;
    }
    navigator.clipboard.readText().then(function (text) {
      var payload;
      try { payload = JSON.parse(text); } catch (e) { payload = null; }
      if (!payload || !payload.copy_html) {
        alert("먼저 우리 앱에서 '📤 네이버로 보내기'를 눌러 페이로드를 복사해줘");
        return;
      }
      var html = payload.copy_html;
      var images = Array.isArray(payload.images) ? payload.images : [];
      L('페이로드 OK. 이미지', images.length, '장');
      if (!images.length) {
        return writeHtmlToClipboard(html, '').then(function () {
          alert('사진 없는 후기 — 본문만 클립보드에 담았어. 붙여넣어줘');
        });
      }
      var blogId = guessBlogId();
      L('blogId', blogId);
      return getSessionKey().then(function (sessionKey) {
        var chain = Promise.resolve();
        var done = 0;
        images.forEach(function (img, i) {
          chain = chain.then(function () {
            var blob = dataUriToBlob(img.dataUri);
            return uploadOne(sessionKey, blogId, blob, i).then(function (naverUrl) {
              html = replaceSrc(html, img.src, naverUrl);
              done++;
            }).catch(function (e) {
              L('이미지[' + i + '] 실패', e);
            });
          });
        });
        return chain.then(function () {
          return writeHtmlToClipboard(html, '').then(function () {
            alert('네이버 이미지로 교체 완료 (' + done + '/' + images.length + ') — 본문에 붙여넣어');
            L('완료', done, '/', images.length);
          });
        });
      });
    }).catch(function (e) {
      L('실패', e);
      alert('실패: ' + (e && e.message ? e.message : e));
    });
  }

  run();
})();

/*
 * ============================ 최종 북마클릿 ================================
 * 아래 한 줄(javascript:...)을 브라우저 '즐겨찾기 URL'에 그대로 붙여넣어 이름을
 * '📷 사진 올리기'로 저장하면 된다. (sibling: tools/naver-bookmarklet.txt)
 *
 * BOOKMARKLET:
 * javascript:(function(){'use strict';var DISPLAY_HOST_PREFIX='https://postfiles.pstatic.net';var BLOG_ID_FALLBACK='yhc9355';var UPPHOTO_QUERY='extractExif=true&extractAnimatedCnt=false&extractAnimatedInfo=true&autorotate=true&extractDominantColor=false&type=&customQuery=&denyAnimatedImage=false&skipXcamFiltering=false';var SESSION_KEY_URL='https://platform.editor.naver.com/api/blogpc001/v1/photo-uploader/session-key';var UPPHOTO_BASE='https://blog.upphoto.naver.com';var L=function(){try{console.log.apply(console,['[nv]'].concat([].slice.call(arguments)))}catch(e){}};function gb(){try{var m=location.pathname.match(/^\/([A-Za-z0-9_-]+)(?:\/|$)/);if(m&&m[1]&&m[1].indexOf('.naver')===-1)return m[1]}catch(e){}return BLOG_ID_FALLBACK}function d2b(d){var c=d.indexOf(','),h=d.slice(0,c),b=d.slice(c+1),mm=(h.match(/data:([^;]+)/)||[])[1]||'image/jpeg',bin=atob(b),n=bin.length,a=new Uint8Array(n);for(var i=0;i<n;i++)a[i]=bin.charCodeAt(i);return new Blob([a],{type:mm})}function pu(x){var el=null;try{var doc=new DOMParser().parseFromString(x,'text/xml');el=doc.querySelector('url')||doc.getElementsByTagName('url')[0]}catch(e){}var r=el&&el.textContent?el.textContent.trim():'';if(!r){var m=x.match(/<url>([^<]+)<\/url>/i);r=m?m[1].trim():''}if(!r)return null;if(/^https?:\/\//i.test(r))return r;return DISPLAY_HOST_PREFIX+(r.charAt(0)==='/'?'':'/')+r}function rs(h,f,t){var o=h,v=[f,f.replace(/&/g,'&amp;')];for(var i=0;i<v.length;i++){o=o.split('"'+v[i]+'"').join('"'+t+'"');o=o.split("'"+v[i]+"'").join("'"+t+"'")}return o}function wc(h){if(window.ClipboardItem&&navigator.clipboard&&navigator.clipboard.write){return navigator.clipboard.write([new ClipboardItem({'text/html':new Blob([h],{type:'text/html'}),'text/plain':new Blob([''],{type:'text/plain'})})])}if(navigator.clipboard&&navigator.clipboard.writeText){return navigator.clipboard.writeText(h)}return Promise.reject(new Error('clipboard'))}function sk(){L('세션키 요청');return fetch(SESSION_KEY_URL,{credentials:'include'}).then(function(r){if(!r.ok)throw new Error('session-key HTTP '+r.status);return r.json()}).then(function(j){L('세션키',j);if(!j||!j.sessionKey)throw new Error('세션키 없음(로그인 확인)');return j.sessionKey})}function up(k,b,bl,i){var u=UPPHOTO_BASE+'/'+k+'/simpleUpload/0?userId='+encodeURIComponent(b)+'&'+UPPHOTO_QUERY;L('업로드['+i+']',u,bl.size);return fetch(u,{method:'POST',credentials:'include',body:bl}).then(function(r){return r.text().then(function(t){L('업로드['+i+'] status',r.status);if(!r.ok)throw new Error('upphoto HTTP '+r.status+' '+t.slice(0,160));var nu=pu(t);L('업로드['+i+'] url',nu);if(!nu)throw new Error('URL 못 찾음');return nu})})}if(!navigator.clipboard||!navigator.clipboard.readText){alert('클립보드 읽기 미지원 브라우저');return}navigator.clipboard.readText().then(function(text){var p;try{p=JSON.parse(text)}catch(e){p=null}if(!p||!p.copy_html){alert("먼저 앱에서 '네이버로 보내기'를 눌러줘");return}var h=p.copy_html,imgs=Array.isArray(p.images)?p.images:[];L('이미지',imgs.length);if(!imgs.length){return wc(h).then(function(){alert('사진 없는 후기 — 본문만 담았어')})}var b=gb();L('blogId',b);return sk().then(function(k){var ch=Promise.resolve(),done=0;imgs.forEach(function(img,i){ch=ch.then(function(){var bl=d2b(img.dataUri);return up(k,b,bl,i).then(function(nu){h=rs(h,img.src,nu);done++}).catch(function(e){L('img'+i+' 실패',e)})})});return ch.then(function(){return wc(h).then(function(){alert('네이버 이미지로 교체 완료 ('+done+'/'+imgs.length+') — 본문에 붙여넣어');L('완료',done,imgs.length)})})})}).catch(function(e){L('실패',e);alert('실패: '+(e&&e.message?e.message:e))})})();
 */
