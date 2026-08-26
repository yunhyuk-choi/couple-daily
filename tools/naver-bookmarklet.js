/*
 * 우리의 하루 — 네이버 후기 사진 업로더 (PLAIN BOOKMARKLET, PHASE 1)
 * ==========================================================================
 * 확장/Tampermonkey 없이 '즐겨찾기(북마클릿)' 하나로 동작한다.
 *
 * 왜 오버레이 + 붙여넣기(textarea)인가:
 *   - navigator.clipboard.readText()는 '문서 포커스'가 필요한데, 북마클릿을 클릭하면
 *     포커스가 북마크바(브라우저 크롬)에 남아 "Document is not focused"로 막힌다.
 *   - 그래서 클립보드 API로 '읽지' 않는다. 대신 오버레이에 textarea를 띄우고 사용자가
 *     Ctrl+V로 직접 붙여넣게 한다(포커스된 입력창 붙여넣기는 언제나 허용).
 *   - 최종 결과 HTML 쓰기도 업로드(비동기) 후엔 user-activation이 만료돼 실패할 수
 *     있어, 자동으로 쓰지 않고 '📋 결과 복사' 버튼(새 제스처)에서 clipboard.write를
 *     실행한다. 그래도 실패하면 결과 textarea에서 전체선택→복사하는 폴백을 준다.
 *
 * 사용 흐름:
 *   1) 우리 앱 후기 상세에서 '📤 네이버로 보내기' 클릭(페이로드가 클립보드에 담김).
 *   2) 네이버 PC 글쓰기 페이지에서 이 북마클릿(📷 사진 올리기) 클릭 → 오버레이 등장.
 *   3) 오버레이 칸을 클릭하고 Ctrl+V로 붙여넣은 뒤 '처리 ▶' 클릭.
 *   4) 업로드가 끝나면 '📋 결과 복사'를 눌러 복사 → 네이버 본문에 Ctrl+V로 붙여넣기.
 *
 * ⚠️ 라이브에서 확정할 CONFIG(아래):
 *   - DISPLAY_HOST_PREFIX: upphoto가 주는 <url>은 상대경로 → 표시 호스트 접두
 *     (postfiles.pstatic.net vs blogfiles.pstatic.net) 확인.
 *   - BLOG_ID_FALLBACK: userId(블로그 아이디) 페이지 추출 실패 시 폴백.
 *   - UPPHOTO_QUERY: 업로드 쿼리스트링(사용자 캡처 그대로).
 *
 * 이 파일은 '가독용 소스'다. 즐겨찾기에 넣을 최종 javascript: 한 줄은 sibling
 * tools/naver-bookmarklet.txt(정본)와 파일 맨 아래 주석. 전부 인라인 — 외부 로드/
 * eval 없음(네이버 페이지 CSP 준수).
 */
(function () {
  'use strict';

  // ======================= CONFIG (라이브에서 조정) =========================
  var DISPLAY_HOST_PREFIX = 'https://postfiles.pstatic.net'; // TODO(live): postfiles vs blogfiles
  var BLOG_ID_FALLBACK = 'yhc9355';                          // TODO(live): 사용자 블로그 아이디
  var UPPHOTO_QUERY =
    'extractExif=true&extractAnimatedCnt=false&extractAnimatedInfo=true' +
    '&autorotate=true&extractDominantColor=false&type=&customQuery=' +
    '&denyAnimatedImage=false&skipXcamFiltering=false';       // TODO(live): 캡처와 일치 확인
  var SESSION_KEY_URL =
    'https://platform.editor.naver.com/api/blogpc001/v1/photo-uploader/session-key';
  var UPPHOTO_BASE = 'https://blog.upphoto.naver.com';
  // ========================================================================

  var L = function () {
    try { console.log.apply(console, ['[nv]'].concat([].slice.call(arguments))); } catch (e) {}
  };

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
    if (/^https?:\/\//i.test(raw)) return raw;
    return DISPLAY_HOST_PREFIX + (raw.charAt(0) === '/' ? '' : '/') + raw;
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
      'position:fixed;left:50%;top:24px;transform:translateX(-50%);z-index:2147483647;' +
      'width:560px;max-width:92vw;background:#fff;color:#222;border:1px solid #e5e5e5;' +
      'border-radius:12px;box-shadow:0 8px 40px rgba(0,0,0,.3);padding:16px;' +
      "font-family:-apple-system,BlinkMacSystemFont,'Malgun Gothic',sans-serif;" +
      'font-size:14px;line-height:1.5;');
    ov.id = 'cd-nv-ov';
    // 네이버 에디터 단축키로 새지 않게 오버레이 안 이벤트는 버블에서 멈춘다(버튼은 동작).
    ['keydown', 'keyup', 'keypress', 'click', 'mousedown', 'paste'].forEach(function (ev) {
      ov.addEventListener(ev, function (e) { e.stopPropagation(); }, false);
    });

    ov.appendChild(el('div', 'font-weight:800;font-size:15px;margin-bottom:8px;',
      '🖼 우리 후기 사진 → 네이버 업로드'));
    ov.appendChild(el('div', 'color:#666;margin-bottom:8px;',
      "여기 칸을 클릭하고 Ctrl+V로 붙여넣은 다음 '처리'를 눌러줘 " +
      "(앱에서 '네이버로 보내기'를 먼저 눌러야 함)"));

    var ta = el('textarea',
      'width:100%;height:70px;box-sizing:border-box;border:1px solid #ccc;' +
      'border-radius:8px;padding:8px;font-size:12px;resize:vertical;');
    ta.setAttribute('placeholder', '여기에 붙여넣기 (Ctrl+V)');
    ov.appendChild(ta);

    var status = el('div', 'margin:8px 0;min-height:1.2em;color:#333;');
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

    document.body.appendChild(ov);
    setTimeout(function () { try { ta.focus(); } catch (e) {} }, 50);
    close.addEventListener('click', function () {
      if (ov.parentNode) ov.parentNode.removeChild(ov);
    });

    return { ov: ov, ta: ta, status: status, go: go,
      resultWrap: resultWrap, copyBtn: copyBtn, resultTa: resultTa };
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

    var blogId = guessBlogId();
    L('blogId', blogId);
    setStatus(u, '세션키 받는 중…');
    getSessionKey().then(function (sessionKey) {
      var chain = Promise.resolve();
      var done = 0;
      imgs.forEach(function (img, i) {
        chain = chain.then(function () {
          setStatus(u, '사진 업로드 중… (' + (i + 1) + '/' + imgs.length + ')');
          var blob = dataUriToBlob(img.dataUri);
          return uploadOne(sessionKey, blogId, blob, i).then(function (nu) {
            html = replaceSrc(html, img.src, nu);
            done++;
          }).catch(function (e) {
            L('img' + i + ' 실패', e);
            setStatus(u, '사진 ' + (i + 1) + ' 실패 — 콘솔 확인 (계속 진행)', true);
          });
        });
      });
      return chain.then(function () { finish(u, html, done, imgs.length); });
    }).catch(function (e) {
      L('실패', e);
      setStatus(u, '실패: ' + (e && e.message ? e.message : e), true);
      u.go.disabled = false;
    });
  }

  var u = buildOverlay();
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
 * javascript:(function(){'use strict';var DISPLAY_HOST_PREFIX='https://postfiles.pstatic.net';var BLOG_ID_FALLBACK='yhc9355';var UPPHOTO_QUERY='extractExif=true&extractAnimatedCnt=false&extractAnimatedInfo=true&autorotate=true&extractDominantColor=false&type=&customQuery=&denyAnimatedImage=false&skipXcamFiltering=false';var SESSION_KEY_URL='https://platform.editor.naver.com/api/blogpc001/v1/photo-uploader/session-key';var UPPHOTO_BASE='https://blog.upphoto.naver.com';var L=function(){try{console.log.apply(console,['[nv]'].concat([].slice.call(arguments)))}catch(e){}};function gb(){try{var m=location.pathname.match(/^\/([A-Za-z0-9_-]+)(?:\/|$)/);if(m&&m[1]&&m[1].indexOf('.naver')===-1)return m[1]}catch(e){}return BLOG_ID_FALLBACK}function d2b(d){var c=d.indexOf(','),h=d.slice(0,c),b=d.slice(c+1),mm=(h.match(/data:([^;]+)/)||[])[1]||'image/jpeg',bin=atob(b),n=bin.length,a=new Uint8Array(n);for(var i=0;i<n;i++)a[i]=bin.charCodeAt(i);return new Blob([a],{type:mm})}function pu(x){var el=null;try{var doc=new DOMParser().parseFromString(x,'text/xml');el=doc.querySelector('url')||doc.getElementsByTagName('url')[0]}catch(e){}var r=el&&el.textContent?el.textContent.trim():'';if(!r){var m=x.match(/<url>([^<]+)<\/url>/i);r=m?m[1].trim():''}if(!r)return null;if(/^https?:\/\//i.test(r))return r;return DISPLAY_HOST_PREFIX+(r.charAt(0)==='/'?'':'/')+r}function rs(h,f,t){var o=h,v=[f,f.replace(/&/g,'&amp;')];for(var i=0;i<v.length;i++){o=o.split('"'+v[i]+'"').join('"'+t+'"');o=o.split("'"+v[i]+"'").join("'"+t+"'")}return o}function wc(h){if(window.ClipboardItem&&navigator.clipboard&&navigator.clipboard.write){return navigator.clipboard.write([new ClipboardItem({'text/html':new Blob([h],{type:'text/html'}),'text/plain':new Blob([''],{type:'text/plain'})})])}if(navigator.clipboard&&navigator.clipboard.writeText){return navigator.clipboard.writeText(h)}return Promise.reject(new Error('clipboard'))}function sk(){L('세션키 요청');return fetch(SESSION_KEY_URL,{credentials:'include'}).then(function(r){if(!r.ok)throw new Error('session-key HTTP '+r.status);return r.json()}).then(function(j){L('세션키',j);if(!j||!j.sessionKey)throw new Error('세션키 없음(로그인 확인)');return j.sessionKey})}function up(k,b,bl,i){var u='https://blog.upphoto.naver.com/'+k+'/simpleUpload/0?userId='+encodeURIComponent(b)+'&'+UPPHOTO_QUERY;L('업로드['+i+']',u,bl.size);return fetch(u,{method:'POST',credentials:'include',body:bl}).then(function(r){return r.text().then(function(t){L('업로드['+i+'] status',r.status);if(!r.ok)throw new Error('upphoto HTTP '+r.status+' '+t.slice(0,160));var nu=pu(t);L('업로드['+i+'] url',nu);if(!nu)throw new Error('URL 못 찾음');return nu})})}function el(tag,css,text){var e=document.createElement(tag);if(css)e.style.cssText=css;if(text!=null)e.textContent=text;return e}function ui(){var old=document.getElementById('cd-nv-ov');if(old&&old.parentNode)old.parentNode.removeChild(old);var ov=el('div',"position:fixed;left:50%;top:24px;transform:translateX(-50%);z-index:2147483647;width:560px;max-width:92vw;background:#fff;color:#222;border:1px solid #e5e5e5;border-radius:12px;box-shadow:0 8px 40px rgba(0,0,0,.3);padding:16px;font-family:-apple-system,BlinkMacSystemFont,'Malgun Gothic',sans-serif;font-size:14px;line-height:1.5;");ov.id='cd-nv-ov';['keydown','keyup','keypress','click','mousedown','paste'].forEach(function(ev){ov.addEventListener(ev,function(e){e.stopPropagation()},false)});ov.appendChild(el('div','font-weight:800;font-size:15px;margin-bottom:8px;','🖼 우리 후기 사진 → 네이버 업로드'));ov.appendChild(el('div','color:#666;margin-bottom:8px;',"여기 칸을 클릭하고 Ctrl+V로 붙여넣은 다음 '처리'를 눌러줘 (앱에서 '네이버로 보내기'를 먼저 눌러야 함)"));var ta=el('textarea','width:100%;height:70px;box-sizing:border-box;border:1px solid #ccc;border-radius:8px;padding:8px;font-size:12px;resize:vertical;');ta.setAttribute('placeholder','여기에 붙여넣기 (Ctrl+V)');ov.appendChild(ta);var st=el('div','margin:8px 0;min-height:1.2em;color:#333;');ov.appendChild(st);var ac=el('div','display:flex;align-items:center;gap:8px;margin-top:2px;');var go=el('button','background:#e64980;color:#fff;border:0;border-radius:8px;padding:9px 16px;font-weight:800;cursor:pointer;','처리 ▶');var cl=el('button','background:#eee;color:#333;border:0;border-radius:8px;padding:9px 14px;cursor:pointer;','닫기');ac.appendChild(go);ac.appendChild(cl);ov.appendChild(ac);var rw=el('div','margin-top:10px;display:none;');var cb=el('button','background:#1c7ed6;color:#fff;border:0;border-radius:8px;padding:9px 16px;font-weight:800;cursor:pointer;','📋 결과 복사');var rt=el('textarea','width:100%;height:70px;box-sizing:border-box;border:1px solid #ccc;border-radius:8px;padding:8px;font-size:11px;margin-top:8px;resize:vertical;');rw.appendChild(cb);rw.appendChild(rt);rw.appendChild(el('div','color:#666;font-size:12px;margin-top:4px;','복사가 안 되면 위 칸을 전체선택(Ctrl+A)→복사(Ctrl+C)해서 붙여넣어'));ov.appendChild(rw);document.body.appendChild(ov);setTimeout(function(){try{ta.focus()}catch(e){}},50);cl.addEventListener('click',function(){if(ov.parentNode)ov.parentNode.removeChild(ov)});return{ov:ov,ta:ta,status:st,go:go,resultWrap:rw,copyBtn:cb,resultTa:rt}}function ss(u,m,er){u.status.textContent=m;u.status.style.color=er?'#c0392b':'#333'}function fin(u,h,done,total){ss(u,'완료 ('+done+'/'+total+') — 아래 버튼 눌러 복사한 뒤 본문에 붙여넣어');u.resultTa.value=h;u.resultWrap.style.display='block';u.go.disabled=false;u.copyBtn.onclick=function(){wc(h).then(function(){ss(u,'복사됨 ✓ — 네이버 본문에 Ctrl+V로 붙여넣어')},function(err){L('write 실패',err);ss(u,'자동복사 실패 — 아래 칸 전체선택 후 복사해줘',true);try{u.resultTa.focus();u.resultTa.select()}catch(e){}})}}function proc(u){var raw=(u.ta.value||'').trim();var p;try{p=JSON.parse(raw)}catch(e){p=null}if(!p||!p.copy_html){ss(u,"페이로드가 안 보여 — 앱에서 '네이버로 보내기' 후 이 칸에 붙여넣어줘",true);return}var h=p.copy_html,imgs=Array.isArray(p.images)?p.images:[];L('이미지',imgs.length);u.go.disabled=true;if(!imgs.length){fin(u,h,0,0);return}var b=gb();L('blogId',b);ss(u,'세션키 받는 중…');sk().then(function(k){var ch=Promise.resolve(),done=0;imgs.forEach(function(img,i){ch=ch.then(function(){ss(u,'사진 업로드 중… ('+(i+1)+'/'+imgs.length+')');var bl=d2b(img.dataUri);return up(k,b,bl,i).then(function(nu){h=rs(h,img.src,nu);done++}).catch(function(e){L('img'+i+' 실패',e);ss(u,'사진 '+(i+1)+' 실패 — 콘솔 확인 (계속 진행)',true)})})});return ch.then(function(){fin(u,h,done,imgs.length)})}).catch(function(e){L('실패',e);ss(u,'실패: '+(e&&e.message?e.message:e),true);u.go.disabled=false})}var U=ui();U.go.addEventListener('click',function(){try{proc(U)}catch(e){L('처리 예외',e);ss(U,'오류: '+(e&&e.message?e.message:e),true)}});L('오버레이 준비됨');})();
 */
