// ==UserScript==
// @name         우리의 하루 — 네이버 후기 사진 업로더
// @namespace    couple-daily
// @version      0.1.0
// @description  우리 후기 사진을 네이버 이미지 호스팅에 올려 붙여넣기용 HTML을 만든다 (PHASE 1: 업로드+클립보드)
// @author       couple-daily
// @match        https://blog.naver.com/*
// @match        https://*.blog.naver.com/*
// @run-at       document-idle
// @grant        none
// ==/UserScript==

/*
 * 무엇을 하나 (PHASE 1)
 * --------------------------------------------------------------------------
 * 1) 네이버 글쓰기 페이지에 떠 있는 버튼(🖼)을 누른다.
 * 2) 클립보드에서 '우리 앱 익스포트 URL'을 읽는다(앱 상세페이지의 📤 버튼이 복사해 둠).
 * 3) 그 URL을 fetch → {title, copy_html, images:[{block_index,url,alt}]}.
 * 4) 네이버 photo-uploader 세션키를 받고, images[].url(우리 앱의 공개 이미지)을
 *    바이트로 받아 네이버 upphoto로 재업로드한다.
 * 5) 업로드 응답 XML의 <url>로 네이버 표시 URL을 만들고, copy_html 안의 원래 <img src>를
 *    네이버 URL로 치환한 뒤, 결과 HTML을 클립보드(text/html + text/plain)에 넣는다.
 * 6) 사용자가 네이버 글쓰기 본문에 그대로 붙여넣으면 '네이버 자체 호스팅' 이미지라
 *    외부 이미지 자동 삭제를 피한다.
 *
 * ⚠️ 라이브에서 확정할 CONFIG(아래) — 브라우저 검증 중에 값을 맞춰 넣을 것:
 *   - DISPLAY_HOST_PREFIX: upphoto가 주는 <url>은 '상대 경로'라 표시 호스트 접두가 필요.
 *     postfiles.pstatic.net / blogfiles.pstatic.net 중 무엇인지 실제 붙여넣기로 확인.
 *   - BLOG_ID: userId 쿼리에 쓰는 블로그 아이디. 페이지에서 자동 추출 시도 + 폴백 상수.
 *   - WRITE_URL 매칭: 글쓰기(에디터) 페이지에서만 버튼을 띄우도록 조정.
 *   - UPPHOTO_QUERY: 업로드 쿼리스트링(사용자 캡처 그대로) — 필요 시 수정.
 *
 * PHASE 2(나중): 에디터에 자동 삽입/발행. 지금은 클립보드까지만.
 */

(function () {
  'use strict';

  // ======================= CONFIG (라이브에서 조정) =========================
  const CONFIG = {
    // upphoto <url>은 상대경로(/MjAy.../xxx.PNG/image.png). 표시용 절대 URL 접두.
    // TODO(live): 실제 붙여넣기로 postfiles vs blogfiles 확인 후 확정.
    DISPLAY_HOST_PREFIX: 'https://postfiles.pstatic.net',

    // 업로드 userId(블로그 아이디). 페이지에서 못 뽑으면 이 값으로.
    // TODO(live): 사용자 블로그 아이디로 확정.
    BLOG_ID_FALLBACK: 'yhc9355',

    // 세션키 발급.
    SESSION_KEY_URL:
      'https://platform.editor.naver.com/api/blogpc001/v1/photo-uploader/session-key',

    // 업로드 베이스(뒤에 /{sessionKey}/simpleUpload/0?... 를 붙인다).
    UPPHOTO_BASE: 'https://blog.upphoto.naver.com',

    // 업로드 쿼리스트링(사용자 캡처 그대로; userId는 코드가 채운다).
    // TODO(live): 캡처와 정확히 일치하는지 확인.
    UPPHOTO_QUERY:
      'extractExif=true&extractAnimatedCnt=false&extractAnimatedInfo=true' +
      '&autorotate=true&extractDominantColor=false&type=&customQuery=' +
      '&denyAnimatedImage=false&skipXcamFiltering=false',

    // 글쓰기(에디터) 페이지 URL 감지용 힌트(하나라도 매칭되면 글쓰기로 간주).
    // TODO(live): 실제 글쓰기 URL 패턴에 맞게 조정.
    WRITE_URL_HINTS: ['/postwrite', 'PostWriteForm', 'Redirect=Write', '/GoBlogWrite'],
  };
  // ========================================================================

  const LOG = (...a) => console.log('[naver-uploader]', ...a);
  const ERR = (...a) => console.error('[naver-uploader]', ...a);

  // --------- 작은 유틸 ---------
  function toast(msg, isErr) {
    let t = document.getElementById('cd-nu-toast');
    if (!t) {
      t = document.createElement('div');
      t.id = 'cd-nu-toast';
      t.style.cssText =
        'position:fixed;left:50%;bottom:88px;transform:translateX(-50%);' +
        'z-index:2147483647;background:#26262b;color:#fff;padding:11px 18px;' +
        'border-radius:20px;font-size:14px;font-weight:700;max-width:80vw;' +
        'box-shadow:0 4px 18px rgba(0,0,0,.25);text-align:center;';
      document.body.appendChild(t);
    }
    t.style.background = isErr ? '#c0392b' : '#26262b';
    t.textContent = msg;
    t.style.opacity = '1';
    clearTimeout(t._h);
    t._h = setTimeout(() => { t.style.opacity = '0'; }, 3200);
  }

  function looksLikeWritePage() {
    const u = location.href;
    return CONFIG.WRITE_URL_HINTS.some((h) => u.indexOf(h) !== -1);
  }

  function guessBlogId() {
    // blog.naver.com/<blogId>/... 형태에서 추출 시도.
    try {
      const m = location.pathname.match(/^\/([A-Za-z0-9_-]+)(?:\/|$)/);
      if (m && m[1] && m[1] !== 'PostWriteForm.naver') return m[1];
    } catch (e) {}
    return CONFIG.BLOG_ID_FALLBACK;
  }

  // XML 응답에서 첫 <url> 텍스트를 뽑는다.
  function parseUploadedUrl(xmlText) {
    try {
      const doc = new DOMParser().parseFromString(xmlText, 'text/xml');
      const el = doc.querySelector('url') || doc.getElementsByTagName('url')[0];
      const raw = el && el.textContent ? el.textContent.trim() : '';
      if (!raw) return null;
      // 절대면 그대로, 상대면 표시 호스트 접두를 붙인다.
      if (/^https?:\/\//i.test(raw)) return raw;
      const sep = raw.charAt(0) === '/' ? '' : '/';
      return CONFIG.DISPLAY_HOST_PREFIX + sep + raw;
    } catch (e) {
      ERR('XML 파싱 실패', e, xmlText && xmlText.slice(0, 200));
      return null;
    }
  }

  async function getSessionKey() {
    LOG('세션키 요청…');
    const r = await fetch(CONFIG.SESSION_KEY_URL, { credentials: 'include' });
    if (!r.ok) throw new Error('session-key HTTP ' + r.status);
    const j = await r.json();
    LOG('세션키 응답', j);
    if (!j || !j.sessionKey) throw new Error('세션키 없음(네이버 로그인 확인)');
    return j.sessionKey;
  }

  async function uploadOne(sessionKey, blogId, bytes, idx) {
    const url =
      CONFIG.UPPHOTO_BASE + '/' + sessionKey + '/simpleUpload/0?userId=' +
      encodeURIComponent(blogId) + '&' + CONFIG.UPPHOTO_QUERY;
    LOG('업로드[' + idx + '] POST', url, bytes.byteLength + ' bytes');
    const r = await fetch(url, {
      method: 'POST',
      credentials: 'include',
      body: bytes,
    });
    const text = await r.text();
    LOG('업로드[' + idx + '] status', r.status);
    if (!r.ok) throw new Error('upphoto HTTP ' + r.status + ' — ' + text.slice(0, 160));
    const naverUrl = parseUploadedUrl(text);
    LOG('업로드[' + idx + '] 네이버 URL', naverUrl);
    if (!naverUrl) throw new Error('업로드 응답에서 URL을 못 찾음');
    return naverUrl;
  }

  // copy_html 안의 특정 원본 src를 네이버 URL로 치환(속성 안전하게 정확 매칭).
  function replaceSrc(html, fromUrl, toUrl) {
    // &amp; 형태로 이스케이프됐을 수도 있으니 두 형태 모두 시도.
    const variants = [fromUrl, fromUrl.replace(/&/g, '&amp;')];
    let out = html;
    for (const v of variants) {
      out = out.split('"' + v + '"').join('"' + toUrl + '"');
    }
    return out;
  }

  async function writeHtmlToClipboard(html, plain) {
    if (window.ClipboardItem && navigator.clipboard && navigator.clipboard.write) {
      const item = new ClipboardItem({
        'text/html': new Blob([html], { type: 'text/html' }),
        'text/plain': new Blob([plain || ''], { type: 'text/plain' }),
      });
      await navigator.clipboard.write([item]);
      return true;
    }
    // 폴백: 텍스트만.
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(html);
      return true;
    }
    return false;
  }

  async function run() {
    try {
      toast('클립보드에서 익스포트 링크 읽는 중…');
      let exportUrl = '';
      try {
        exportUrl = (await navigator.clipboard.readText() || '').trim();
      } catch (e) {
        ERR('클립보드 읽기 실패', e);
      }
      if (!/\/api\/review-export\//.test(exportUrl)) {
        toast('먼저 앱에서 📤 네이버로 보내기를 눌러 링크를 복사해줘', true);
        return;
      }
      LOG('익스포트 URL', exportUrl);

      LOG('익스포트 데이터 fetch…');
      const resp = await fetch(exportUrl, { credentials: 'omit' });
      if (!resp.ok) throw new Error('export HTTP ' + resp.status);
      const data = await resp.json();
      LOG('익스포트 데이터', data);
      const images = Array.isArray(data.images) ? data.images : [];
      let html = data.copy_html || '';
      const plain = (data.title || '') + '\n';
      if (!html) throw new Error('copy_html 비어 있음');
      if (!images.length) {
        // 사진이 없으면 그냥 원본 HTML을 클립보드에.
        await writeHtmlToClipboard(html, plain);
        toast('사진 없는 후기 — 본문만 복사했어. 붙여넣어줘');
        return;
      }

      const blogId = guessBlogId();
      LOG('blogId', blogId);
      const sessionKey = await getSessionKey();

      let done = 0;
      for (let i = 0; i < images.length; i++) {
        const img = images[i];
        try {
          toast('사진 업로드 중… (' + (i + 1) + '/' + images.length + ')');
          LOG('이미지[' + i + '] 원본 fetch', img.url);
          const ir = await fetch(img.url, { credentials: 'omit' });
          if (!ir.ok) throw new Error('이미지 fetch HTTP ' + ir.status);
          const buf = await ir.arrayBuffer();
          const naverUrl = await uploadOne(sessionKey, blogId, buf, i);
          html = replaceSrc(html, img.url, naverUrl);
          done++;
        } catch (e) {
          ERR('이미지[' + i + '] 실패', e);
          toast('사진 ' + (i + 1) + ' 업로드 실패 — 콘솔 확인', true);
        }
      }

      const okClip = await writeHtmlToClipboard(html, plain);
      if (!okClip) {
        toast('클립보드 쓰기 실패 — 콘솔의 HTML을 수동 복사해줘', true);
        LOG('=== 붙여넣을 HTML ===\n' + html);
        return;
      }
      toast('네이버 이미지로 교체 완료(' + done + '/' + images.length + ') — 본문에 붙여넣어');
      LOG('완료. 교체', done, '/', images.length);
    } catch (e) {
      ERR('실행 실패', e);
      toast('실패: ' + (e && e.message ? e.message : e), true);
    }
  }

  function injectButton() {
    if (document.getElementById('cd-nu-btn')) return;
    const b = document.createElement('button');
    b.id = 'cd-nu-btn';
    b.type = 'button';
    b.textContent = '🖼 우리 후기 사진 올리기';
    b.style.cssText =
      'position:fixed;right:18px;bottom:18px;z-index:2147483647;' +
      'background:#e64980;color:#fff;border:0;border-radius:24px;' +
      'padding:12px 18px;font-size:14px;font-weight:800;cursor:pointer;' +
      'box-shadow:0 4px 18px rgba(0,0,0,.28);';
    b.addEventListener('click', run);
    document.body.appendChild(b);
    LOG('버튼 주입 완료');
  }

  // 글쓰기 페이지로 보이면 버튼을 띄운다. 감지가 애매하면(iframe/SPA 전환)
  // 항상 띄우되, 콘솔 로그로 현재 URL을 남겨 라이브에서 매칭을 조정하게 한다.
  LOG('로드됨. URL =', location.href, 'writePage?', looksLikeWritePage());
  if (looksLikeWritePage()) {
    injectButton();
  } else {
    // SPA 전환 대비: 3초 간격으로 몇 번 더 확인.
    let tries = 0;
    const iv = setInterval(function () {
      tries++;
      if (looksLikeWritePage()) {
        injectButton();
        clearInterval(iv);
      } else if (tries >= 10) {
        clearInterval(iv);
        // 그래도 못 찾으면 개발/조정용으로 버튼을 띄운다(주석 참고).
        injectButton();
        LOG('write-page 매칭 실패 — 임시로 버튼 노출(콘솔의 URL로 WRITE_URL_HINTS 조정)');
      }
    }, 3000);
  }
})();
