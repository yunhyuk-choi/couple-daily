/* 동영상 → GIF — 브라우저가 만든다 (노선 2 Step 5).
 *
 * 썸네일(static/thumbnail.js)과 **같은 철학**이다. 거기서 서버에 Chromium 을 올리지
 * 않고 보는 사람의 브라우저가 DOM+Canvas 로 그렸듯이, 여기서도 서버에 ffmpeg 를
 * 올리지 않고 **보는 사람의 브라우저가** 디코딩·프레임 추출·GIF 인코딩을 전부 한다.
 * 서버(gifmaker.py)가 하는 일은 프리셋·한도를 내려보내고, 결과를 다시 재 보는 것뿐.
 *
 * ──────────────────────────────────────────────────────────────────────────
 * 디코딩은 2단이다 — **네이티브 우선, 안 되면 그때 비로소 WASM**
 * ──────────────────────────────────────────────────────────────────────────
 * 브라우저는 자체 코덱이 아니라 **OS 코덱을 빌려 쓴다.** 아이폰 MOV(HEVC) 기준:
 *
 *   아이폰 Safari   — iOS 네이티브 HEVC            → ① 네이티브
 *   맥북            — macOS 시스템 HEVC            → ① 네이티브
 *   갤럭시(Chrome)  — Android 하드웨어 디코더      → ① 네이티브
 *   윈도우 데스크톱 — HEVC Video Extensions 가 깔려 있어야 한다 → 있으면 ①, 없으면 ②
 *
 * 즉 **구멍은 윈도우 한 경우뿐**이다. 그래서 ②(ffmpeg.wasm, 코어 약 32MB)는
 * **기본 경로가 아니라 폴백**이다 — 되는 기기에 32MB 다운로드와 느린 디코딩을
 * 물리는 건 손해다. ①이 실패한 것이 *확인된 뒤에만* 로드한다(lazy).
 *
 * ⚠️ 위 표는 **추정이 아니라 분기 설명**이다. 실제 판정은 이 파일의 진단 코드가
 *    그 기기에서 직접 한다(아래 §3) — 되는 척하지 않는다.
 *
 * ──────────────────────────────────────────────────────────────────────────
 * 조용한 실패를 만들지 않는다
 * ──────────────────────────────────────────────────────────────────────────
 * 이 기능의 최악의 실패 모드는 "에러 없이 빈 프레임"이다. 그래서 모든 실패에
 * 이름(gifmaker.DIAG_CODES)이 있고, 화면이 그 이름으로 **말한다**:
 *   native-ok / wasm-ok / decode-failed / tainted / blank-frames / loader-failed
 *
 * ⛔ 용량이 한도를 넘으면 **색·fps·크기를 몰래 깎지 않는다.** 내려받기를 막고
 *    "길이를 줄여라"라고 말한다(썸네일이 폰트 대신 문구를 줄인 것과 같은 규율).
 */
(function () {
  "use strict";

  var root = document.getElementById("rv-gif");
  if (!root) return;

  var SPEC = readJson("rv-gif-spec");
  var STATE = readJson("rv-gif-state") || {};
  var VIDEOS = readJson("rv-gif-videos") || [];
  if (!SPEC) return;

  var STATE_URL = root.getAttribute("data-state-url");
  var UPLOAD_URL = root.getAttribute("data-upload-url");
  var TOPIC = root.getAttribute("data-topic") || "후기";
  var GIFENC_URL = root.getAttribute("data-gifenc-url");

  // 폴백 디코더 — **레포에 담을 수 없는 32MB짜리라 CDN 이다.** 그래서 여기엔
  // 로드 실패 가드가 붙는다(§4): 못 받으면 조용히 깨지는 대신 기능을 막고 말한다.
  //
  // ⚠️ 감싸개(`@ffmpeg/ffmpeg`)를 쓰지 않고 **코어를 직접** 띄운다 — 실측 근거:
  //    그 감싸개의 UMD 빌드는 워커를 자기 webpack 청크에서 띄우는데, 크로스 오리진
  //    Worker 가 막혀 blob 으로 우회하면 `classWorkerURL` 이 워커를 *모듈* 워커로
  //    만들고, 그 안의 코드는 `importScripts(coreURL)` 를 쓴다 — 모듈 워커에는
  //    importScripts 가 없다. 그 catch 가 타는 대체 경로는 이 빌드에서 스텁이라
  //    `Cannot find module 'blob:…'` 로 끝난다(이 환경에서 재현함).
  //    코어의 **ESM 빌드를 우리 모듈 워커에서 직접 import** 하면 그 사슬이 통째로
  //    없어지고, 디코딩이 메인 스레드를 막지 않는다(= 진행 표시가 멈추지 않는다).
  var CORE_VER = "0.12.10";
  var CORE_BASE = "https://cdn.jsdelivr.net/npm/@ffmpeg/core@" + CORE_VER + "/dist/esm";
  var FF_CORE_MB = 32;

  // 폴백 디코더 워커. 코어를 dynamic import 하고, wasm 은 **우리가 진행률을 보며
  // 받아 둔 blob** 을 쓰게 한다(locateFile) — 32MB 를 두 번 받지 않는다.
  var DECODER_WORKER_SRC = [
    "let core = null;",
    "self.onmessage = async (e) => {",
    "  const m = e.data;",
    "  try {",
    "    if (m.type === 'load') {",
    "      const mod = await import(m.coreURL);",
    "      core = await mod.default({ locateFile: (p, prefix) =>",
    "        p.endsWith('.wasm') ? m.wasmURL : (prefix || '') + p });",
    "      core.setLogger(x => self.postMessage({ type: 'log',",
    "        message: (x && x.message != null) ? x.message : String(x) }));",
    "      self.postMessage({ type: 'loaded' });",
    "    } else if (m.type === 'run') {",
    "      core.FS.writeFile(m.name, m.bytes);",
    "      const ret = core.exec.apply(core, m.args);",
    "      const raw = ret === 0 ? core.FS.readFile('out.raw') : new Uint8Array(0);",
    "      try { core.FS.unlink(m.name); } catch (_) {}",
    "      try { core.FS.unlink('out.raw'); } catch (_) {}",
    "      self.postMessage({ type: 'done', ret, raw }, [raw.buffer]);",
    "    }",
    "  } catch (err) {",
    "    self.postMessage({ type: 'error', message: (err && err.message) || String(err) });",
    "  }",
    "};",
  ].join("\n");

  var el = {
    videoSel: root.querySelector(".rv-gif-video"),
    upload: root.querySelector(".rv-gif-upload"),
    uploadBtn: root.querySelector(".rv-gif-upload-btn"),
    del: root.querySelector(".rv-gif-delete"),
    video: root.querySelector(".rv-gif-player"),
    start: root.querySelector(".rv-gif-start"),
    startOut: root.querySelector(".rv-gif-start-out"),
    dur: root.querySelector(".rv-gif-dur"),
    durOut: root.querySelector(".rv-gif-dur-out"),
    play: root.querySelector(".rv-gif-play"),
    widths: root.querySelector(".rv-gif-widths"),
    fpses: root.querySelector(".rv-gif-fpses"),
    estimate: root.querySelector(".rv-gif-estimate"),
    make: root.querySelector(".rv-gif-make"),
    download: root.querySelector(".rv-gif-download"),
    bar: root.querySelector(".rv-gif-bar"),
    status: root.querySelector(".rv-gif-status"),
    warn: root.querySelector(".rv-gif-warn"),
    diag: root.querySelector(".rv-gif-diag"),
    result: root.querySelector(".rv-gif-result"),
    preview: root.querySelector(".rv-gif-preview"),
    empty: root.querySelector(".rv-gif-empty"),
    panel: root.querySelector(".rv-gif-panel"),
  };

  var current = null;      // 지금 고른 영상 {id, name, size, stream_url, ...}
  var sourceMode = null;   // 'direct' | 'proxy' — 서버가 실측으로 고른다
  var srcW = 0, srcH = 0;  // 원본 해상도(메타데이터에서)
  var nativeOK = null;     // null=미판정, true/false
  var lastBlob = null;
  var lastResult = null;
  var busy = false;
  var decoder = null;      // 로드된 폴백 디코더 {worker, logs} — 한 번만 받는다

  // ----------------------------------------------------------------- //
  // 0. 유틸
  // ----------------------------------------------------------------- //
  function readJson(id) {
    var n = document.getElementById(id);
    if (!n) return null;
    try { return JSON.parse(n.textContent || "null"); } catch (e) { return null; }
  }
  // 1MB 미만은 KB 로 적는다 — "0.5MB 가 한도 0.5MB 를 넘었어 (0.0MB 초과)" 같은
  // 말이 안 되는 경고를 내지 않으려고(실측된 문구 버그).
  function mb(bytes) {
    return bytes < 1048576 ? Math.round(bytes / 1024) + "KB"
                           : (bytes / 1048576).toFixed(1) + "MB";
  }
  function say(msg, kind) {
    if (!el.status) return;
    el.status.textContent = msg || "";
    el.status.className = "rv-gif-status muted small" + (kind ? " " + kind : "");
  }
  function warn(msg) {
    if (!el.warn) return;
    el.warn.hidden = !msg;
    el.warn.textContent = msg || "";
  }
  function progress(frac, label) {
    if (el.bar) {
      el.bar.hidden = frac == null;
      if (frac != null) el.bar.value = Math.max(0, Math.min(1, frac));
    }
    if (label) say(label);
  }
  // 프레임 사이에 한 틱 양보해 화면이 갱신되게 한다.
  // ⚠️ ``setTimeout(0)`` 을 쓰면 **백그라운드 탭에서 굽다가 멈춘다** — 크롬이 숨은
  //    탭의 타이머를 1초→1분으로 조이고, 길게 숨어 있으면 사실상 멈춘 것처럼 된다
  //    (이 환경에서 재현함: "GIF 로 굽는 중… 20/20" 에서 영원히 머물렀다).
  //    MessageChannel 의 메시지는 같은 '태스크'라 리페인트 기회는 그대로 주면서
  //    그 조임을 받지 않는다. 채널이 없는 환경만 setTimeout 으로 떨어진다.
  var _chan = (typeof MessageChannel === "function") ? new MessageChannel() : null;
  var _waiters = [];
  if (_chan) {
    _chan.port1.onmessage = function () {
      var w = _waiters.shift();
      if (w) w();
    };
  }
  function idle() {
    return new Promise(function (r) {
      if (_chan) { _waiters.push(r); _chan.port2.postMessage(0); }
      else { setTimeout(r, 0); }
    });
  }
  function pick(group) {
    var r = group && group.querySelector("input:checked");
    return r ? parseInt(r.value, 10) : null;
  }

  // ----------------------------------------------------------------- //
  // 1. 설정 ↔ 화면 — 서버 스펙 안에서만 움직인다
  // ----------------------------------------------------------------- //
  function settings() {
    return {
      video_id: current ? current.id : null,
      start: el.start ? parseFloat(el.start.value) || 0 : 0,
      duration: el.dur ? parseFloat(el.dur.value) || SPEC.default_duration
                       : SPEC.default_duration,
      width: pick(el.widths) || SPEC.default_width,
      fps: pick(el.fpses) || SPEC.default_fps,
    };
  }
  // 목표 크기 — 원본 비율 유지, **업스케일 금지**(gifmaker.target_height 와 같은 식).
  function targetSize(width) {
    if (!srcW || !srcH) return null;
    var w = Math.min(width, srcW);
    w = w - (w % 2);
    // **가장 가까운 짝수로** 반올림한다 — ffmpeg 의 ``scale=W:-2`` 와 같은 규칙이라
    // 네이티브 경로와 폴백 경로가 같은 해상도를 낸다(어긋나면 같은 GIF 가 아니다).
    var h = Math.max(2, 2 * Math.round(srcH * (w / srcW) / 2));
    return { w: Math.max(2, w), h: h };
  }
  function frameCount(s) {
    return Math.max(1, Math.round(s.fps * s.duration));
  }
  function refreshEstimate() {
    var s = settings();
    var size = targetSize(s.width);
    if (!el.estimate) return;
    if (!size) {
      el.estimate.textContent = current
        ? "이 기기에서 원본 크기를 못 읽어서 예상 용량은 만든 뒤에 알 수 있어 " +
          "(한도 " + mb(SPEC.max_bytes) + ")."
        : "영상을 고르면 예상 용량을 보여줄게.";
      el.estimate.className = "rv-gif-estimate muted small";
      return;
    }
    // 서버와 **같은 계수**(SPEC.bytes_per_pixel)를 쓴다 — 화면과 서버가 다른 숫자를
    // 말하지 않게. 실제 용량은 인코딩이 끝나야 알고, 판정은 그 실측으로 한다.
    var est = 800 + size.w * size.h * frameCount(s) * SPEC.bytes_per_pixel;
    el.estimate.textContent =
      size.w + "×" + size.h + " · " + frameCount(s) + "장 · 예상 " + mb(est) +
      " (한도 " + mb(SPEC.max_bytes) + ")";
    el.estimate.className =
      "rv-gif-estimate muted small" + (est > SPEC.max_bytes ? " over" : "");
  }
  // 설정이 바뀌면 직전 결과는 이 설정의 것이 아니다 — 버린다(서버 apply_state 와 동일).
  function invalidate() {
    lastBlob = null;
    lastResult = null;
    if (el.download) el.download.disabled = true;
    if (el.result) el.result.textContent = "";
    if (el.preview) { el.preview.hidden = true; el.preview.removeAttribute("src"); }
    warn("");
    refreshEstimate();
  }

  // ----------------------------------------------------------------- //
  // 2. 서버에 저장 — 측정값만 보낸다(ok 판정은 서버가 다시 한다)
  // ----------------------------------------------------------------- //
  function save(body) {
    if (!STATE_URL) return Promise.resolve(null);
    return fetch(STATE_URL, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Requested-With": "fetch" },
      body: JSON.stringify(body),
    }).then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
  }
  function saveDiag(code, extra) {
    var d = { code: code };
    if (extra) Object.keys(extra).forEach(function (k) { d[k] = extra[k]; });
    if (srcW) { d.src_width = srcW; d.src_height = srcH; }
    return save({ diag: d });
  }

  // ----------------------------------------------------------------- //
  // 3. 진단 — 이 기기가 이 영상을 읽을 수 있나? **재 보고 말한다**
  // ----------------------------------------------------------------- //
  var ERR_TEXT = {
    1: "불러오기가 중단됐어",
    2: "네트워크 오류로 영상을 받지 못했어",
    3: "디코딩에 실패했어 (이 기기에 이 코덱이 없어)",
    4: "이 브라우저가 이 영상 형식을 지원하지 않아",
  };
  function videoErrorText(v) {
    var e = v && v.error;
    if (!e) return "";
    return ERR_TEXT[e.code] || ("알 수 없는 오류 (code " + e.code + ")");
  }

  function loadMetadata(v, url, crossOrigin) {
    return new Promise(function (resolve) {
      var done = false;
      function finish(ok, why) {
        if (done) return;
        done = true;
        clearTimeout(timer);
        v.removeEventListener("loadedmetadata", onMeta);
        v.removeEventListener("error", onErr);
        resolve({ ok: ok, why: why || "" });
      }
      function onMeta() { finish(v.videoWidth > 0, v.videoWidth > 0 ? "" : "크기를 못 읽었어"); }
      function onErr() { finish(false, videoErrorText(v)); }
      var timer = setTimeout(function () {
        finish(false, "메타데이터를 읽는 데 너무 오래 걸려 (코덱 미지원일 수 있어)");
      }, 15000);
      v.addEventListener("loadedmetadata", onMeta);
      v.addEventListener("error", onErr);
      if (crossOrigin) v.crossOrigin = "anonymous"; else v.removeAttribute("crossorigin");
      v.preload = "auto";
      v.muted = true;
      v.playsInline = true;
      v.src = url;
      v.load();
    });
  }

  function seek(v, t) {
    return new Promise(function (resolve, reject) {
      if (Math.abs(v.currentTime - t) < 0.001 && v.readyState >= 2) return resolve();
      var done = false;
      function ok() { if (!done) { done = true; cleanup(); resolve(); } }
      function bad() { if (!done) { done = true; cleanup(); reject(new Error(videoErrorText(v) || "탐색 실패")); } }
      function cleanup() {
        clearTimeout(timer);
        v.removeEventListener("seeked", ok);
        v.removeEventListener("error", bad);
      }
      var timer = setTimeout(bad, 15000);
      v.addEventListener("seeked", ok);
      v.addEventListener("error", bad);
      try { v.currentTime = t; } catch (e) { bad(); }
    });
  }

  // 단색(=아무것도 안 그려진) 프레임인가. **조용한 실패를 잡는 유일한 수단**이다 —
  // 코덱이 없는데도 에러 없이 검은 화면만 내는 브라우저가 있다.
  function isUniform(data) {
    var r = data[0], g = data[1], b = data[2];
    for (var i = 4; i < data.length; i += 4 * 97) {  // 성긴 샘플링이면 충분하다
      if (Math.abs(data[i] - r) > 2 || Math.abs(data[i + 1] - g) > 2 ||
          Math.abs(data[i + 2] - b) > 2) return false;
    }
    return true;
  }

  // 네이티브 경로가 이 영상에 쓸 만한가 — 한 프레임을 실제로 그려 본다.
  function probeNative(v) {
    var c = document.createElement("canvas");
    c.width = 48; c.height = 48;
    var ctx = c.getContext("2d", { willReadFrequently: true });
    var t = Math.min(v.duration ? v.duration * 0.5 : 0, Math.max(0, (v.duration || 1) - 0.1));
    return seek(v, t).then(function () {
      ctx.drawImage(v, 0, 0, 48, 48);
      var px;
      try {
        px = ctx.getImageData(0, 0, 48, 48).data;
      } catch (e) {
        return { ok: false, code: "tainted", why: "캔버스가 오염돼 픽셀을 읽을 수 없어 (CORS)" };
      }
      if (isUniform(px)) {
        return { ok: false, code: "blank-frames", why: "디코더가 에러 없이 빈 프레임만 냈어" };
      }
      return { ok: true, code: "native-ok", why: "" };
    }).catch(function (e) {
      return { ok: false, code: "decode-failed", why: (e && e.message) || "디코딩 실패" };
    });
  }

  var DIAG_LINE = {
    "native-ok": "이 기기가 이 영상을 바로 디코딩해 — 빠른 경로로 만들게.",
    "wasm-ok": "이 기기에서는 이 영상이 네이티브로 디코딩되지 않아 — 브라우저 안 " +
               "디코더로 만들게. 처음 한 번 약 " + FF_CORE_MB + "MB를 받고, " +
               "네이티브보다 느려(3~4초 클립이면 견딜 만해).",
    "decode-failed": "이 기기에서는 이 영상을 디코딩할 수 없어.",
    "tainted": "영상 픽셀을 읽을 수 없어 (CORS). 프록시 경로로 다시 시도해줘.",
    "blank-frames": "디코더가 에러 없이 빈 화면만 내고 있어 — 코덱이 없을 때 생기는 증상이야.",
    "loader-failed": "폴백 디코더를 내려받지 못했어 (네트워크/CDN). GIF 만들기를 막았어.",
    "no-video": "고를 영상이 없어.",
  };
  function showDiag(code, why) {
    if (!el.diag) return;
    var line = DIAG_LINE[code] || code;
    el.diag.hidden = false;
    el.diag.textContent =
      line + (why ? " — " + why : "") +
      (sourceMode ? " · 바이트 경로: " + (sourceMode === "direct"
        ? "OneDrive 직접(CORS 허용 확인됨)" : "앱 프록시(Range 206)") : "");
    el.diag.className = "rv-gif-diag small " +
      (code === "native-ok" || code === "wasm-ok" ? "muted" : "bad");
  }

  // ----------------------------------------------------------------- //
  // 4. 폴백 디코더 — **네이티브 실패가 확인된 뒤에만** 받는다 (lazy)
  // ----------------------------------------------------------------- //
  // 진행률을 보여 주며 받아 blob URL 로. 32MB 다운로드는 **진행 표시 없이는
  // 멈춘 것처럼 보인다** — 그래서 Content-Length 를 읽어 바를 움직인다.
  function toBlobURL(url, type, onFrac) {
    return fetch(url).then(function (r) {
      if (!r.ok) throw new Error(url.split("/").pop() + " " + r.status);
      var total = parseInt(r.headers.get("Content-Length") || "0", 10);
      if (!r.body || !total) return r.arrayBuffer();
      var reader = r.body.getReader();
      var chunks = [], got = 0;
      return (function pump() {
        return reader.read().then(function (res) {
          if (res.done) {
            var out = new Uint8Array(got), at = 0;
            chunks.forEach(function (c) { out.set(c, at); at += c.length; });
            return out.buffer;
          }
          chunks.push(res.value);
          got += res.value.length;
          if (onFrac) onFrac(got / total);
          return pump();
        });
      })();
    }).then(function (buf) {
      return URL.createObjectURL(new Blob([buf], { type: type }));
    });
  }

  function loaderError(msg) {
    var e = new Error(msg);
    e.code = "loader-failed";   // 네트워크/CDN 실패와 '이 영상을 못 읽음'은 다른 말이다
    return e;
  }

  // 디코더를 띄운다. **여기 오기 전에 네이티브가 실패했다는 판정이 끝나 있다.**
  function loadDecoder() {
    if (decoder) return Promise.resolve(decoder);
    say("브라우저 안 디코더를 받는 중… (약 " + FF_CORE_MB + "MB, 처음 한 번만)");
    progress(0);
    return toBlobURL(
      CORE_BASE + "/ffmpeg-core.wasm", "application/wasm",
      function (f) { progress(f * 0.25, "디코더 받는 중… " + Math.round(f * 100) + "%"); }
    ).catch(function (e) {
      throw loaderError((e && e.message) || "디코더를 받지 못했어");
    }).then(function (wasmURL) {
      var worker;
      try {
        worker = new Worker(
          URL.createObjectURL(new Blob([DECODER_WORKER_SRC], { type: "text/javascript" })),
          { type: "module" }
        );
      } catch (e) {
        throw loaderError("디코더 워커를 띄우지 못했어");
      }
      var d = { worker: worker, logs: [] };
      return new Promise(function (resolve, reject) {
        function onMsg(ev) {
          var m = ev.data || {};
          if (m.type === "log") { d.logs.push(m.message); return; }
          worker.removeEventListener("message", onMsg);
          if (m.type === "loaded") { decoder = d; resolve(d); return; }
          worker.terminate();
          reject(loaderError(m.message || "디코더를 초기화하지 못했어"));
        }
        worker.addEventListener("message", onMsg);
        worker.addEventListener("error", function () {
          worker.terminate();
          reject(loaderError("디코더 워커가 시작하지 못했어"));
        });
        worker.postMessage({
          type: "load", coreURL: CORE_BASE + "/ffmpeg-core.js", wasmURL: wasmURL,
        });
      });
    });
  }

  // 워커에 한 번 돌린다. 입력 바이트는 **transfer** 로 넘겨 복사본을 만들지 않는다.
  function runDecoder(d, name, bytes, args) {
    return new Promise(function (resolve, reject) {
      function onMsg(ev) {
        var m = ev.data || {};
        if (m.type === "log") { d.logs.push(m.message); return; }
        d.worker.removeEventListener("message", onMsg);
        if (m.type === "error") reject(new Error(m.message || "디코딩에 실패했어"));
        else resolve(m);
      }
      d.logs.length = 0;
      d.worker.addEventListener("message", onMsg);
      d.worker.postMessage(
        { type: "run", name: name, bytes: bytes, args: args }, [bytes.buffer]
      );
    });
  }

  // ----------------------------------------------------------------- //
  // 5. 프레임 추출 — 두 경로가 **같은 모양**(RGBA Uint8ClampedArray[])을 낸다
  // ----------------------------------------------------------------- //
  function extractNative(v, s, size) {
    var c = document.createElement("canvas");
    c.width = size.w; c.height = size.h;
    var ctx = c.getContext("2d", { willReadFrequently: true });
    var n = frameCount(s);
    var frames = [];
    var blanks = 0;
    var i = 0;
    function step() {
      if (i >= n) {
        if (blanks === n) {
          var err = new Error("디코더가 빈 프레임만 냈어");
          err.code = "blank-frames";
          throw err;
        }
        return frames;
      }
      var t = s.start + i / s.fps;
      return seek(v, t).then(function () {
        ctx.drawImage(v, 0, 0, size.w, size.h);
        var data;
        try {
          data = ctx.getImageData(0, 0, size.w, size.h).data;
        } catch (e) {
          var err = new Error("캔버스가 오염돼 픽셀을 읽을 수 없어");
          err.code = "tainted";
          throw err;
        }
        if (isUniform(data)) blanks += 1;
        frames.push(data);
        i += 1;
        progress(i / n * 0.6, "프레임 뽑는 중… " + i + "/" + n);
        return idle().then(step);
      });
    }
    return Promise.resolve().then(step);
  }

  // 원본 바이트를 통째로 받는다(WASM 은 파일 단위로 먹는다). 서버는 이걸 스트리밍으로
  // 흘리므로 워커 메모리는 평평하다 — 무거운 쪽은 브라우저다(그게 설계다).
  function fetchVideoBytes(url, total) {
    return fetch(url, { credentials: "same-origin" }).then(function (r) {
      if (!r.ok) throw new Error("영상을 받지 못했어 (" + r.status + ")");
      var len = parseInt(r.headers.get("Content-Length") || "0", 10) || total || 0;
      if (!r.body || !len) return r.arrayBuffer();
      var reader = r.body.getReader(), chunks = [], got = 0;
      return (function pump() {
        return reader.read().then(function (res) {
          if (res.done) {
            var out = new Uint8Array(got), at = 0;
            chunks.forEach(function (c) { out.set(c, at); at += c.length; });
            return out.buffer;
          }
          chunks.push(res.value);
          got += res.value.length;
          progress(got / len * 0.25, "영상 받는 중… " + Math.round(got / len * 100) + "%");
          return pump();
        });
      })();
    });
  }

  // ffmpeg 로그에서 **출력** 스트림의 실제 크기를 읽는다. 로그를 못 읽으면
  // 바이트 수로 역산한다(프레임 수를 ±1 범위에서 맞춰 본다) — 둘 다 실패하면
  // 조용히 이상한 그림을 내는 대신 에러를 던진다.
  function outputSize(logs, w, rawLen, wantFrames) {
    var after = false;
    for (var i = 0; i < logs.length; i++) {
      if (/Output #0/.test(logs[i])) after = true;
      if (!after) continue;
      var m = /(\d{2,5})x(\d{2,5})/.exec(logs[i]);
      if (m && parseInt(m[1], 10) === w) {
        return { w: w, h: parseInt(m[2], 10) };
      }
    }
    for (var d = 0; d <= 2; d++) {
      for (var sgn = -1; sgn <= 1; sgn += 2) {
        var n = wantFrames + sgn * d;
        if (n <= 0) continue;
        var h = rawLen / (w * 4 * n);
        if (h > 1 && Math.abs(h - Math.round(h)) < 1e-9) return { w: w, h: Math.round(h) };
      }
    }
    return null;
  }

  function extractWasm(s, size) {
    var ext = (current.name.match(/\.[A-Za-z0-9]+$/) || [".mp4"])[0].toLowerCase();
    var name = "in" + ext;
    return loadDecoder().then(function (d) {
      return fetchVideoBytes(current.stream_url, current.size).then(function (buf) {
        progress(0.3, "브라우저 안에서 디코딩 중… (조금 걸려)");
        // 네이티브 경로와 **같은 구간·같은 크기·같은 fps** 를 지시한다 — 그래야
        // 두 경로가 같은 GIF 를 낸다. scale 의 -2 는 비율 유지 + 짝수 높이.
        // ⚠️ ``-ss`` 는 **입력 뒤**에 둔다(정확 탐색). 앞에 두면 빠른(키프레임) 탐색이
        //    되는데, 컨테이너의 duration 이 실제보다 짧게 적힌 파일에서 ffmpeg 가 그
        //    잘못된 길이에 맞춰 구간을 잘라 **프레임이 모자라게 나온다**(실측: 헤더에
        //    5.01초로 적힌 7초짜리에서 12장이어야 할 것이 4장이 됐다). 몇 초짜리
        //    클립이라 정확 탐색의 비용은 작고, 모자란 GIF 보다 낫다.
        return runDecoder(d, name, new Uint8Array(buf), [
          "-i", name, "-ss", String(s.start), "-t", String(s.duration), "-an",
          "-vf", "fps=" + s.fps + ",scale=" + size.w + ":-2:flags=lanczos",
          "-pix_fmt", "rgba", "-f", "rawvideo", "out.raw",
        ]);
      }).then(function (m) {
        var bytes = m.raw instanceof Uint8Array ? m.raw : new Uint8Array(m.raw || 0);
        if (m.ret !== 0 || !bytes.length) {
          var e0 = new Error("이 컨테이너/코덱은 폴백 디코더도 읽지 못했어");
          e0.code = "decode-failed";
          throw e0;
        }
        // 출력 해상도는 **로그에서 읽는다**(scale 의 -2 가 정한 높이라 우리가 모른다).
        var out = outputSize(d.logs, size.w, bytes.length, frameCount(s));
        if (!out) {
          var e1 = new Error("디코더 출력 크기를 읽지 못했어");
          e1.code = "decode-failed";
          throw e1;
        }
        var per = out.w * out.h * 4;
        var n = Math.floor(bytes.length / per);
        if (n < 1) {
          var e2 = new Error("프레임이 하나도 없어");
          e2.code = "decode-failed";
          throw e2;
        }
        var frames = [], blanks = 0;
        for (var i = 0; i < n; i++) {
          var f = new Uint8ClampedArray(bytes.buffer, bytes.byteOffset + i * per, per);
          if (isUniform(f)) blanks += 1;
          frames.push(f);
        }
        if (blanks === n) {
          var e3 = new Error("디코더가 빈 프레임만 냈어");
          e3.code = "blank-frames";
          throw e3;
        }
        return { frames: frames, size: out };
      });
    });
  }

  // ----------------------------------------------------------------- //
  // 6. GIF 인코딩 — 두 경로가 **같은 인코더**를 쓴다(결과가 같아야 한다)
  // ----------------------------------------------------------------- //
  var gifencPromise = null;
  function gifenc() {
    if (!gifencPromise) gifencPromise = import(GIFENC_URL);
    return gifencPromise;
  }
  // ⚠️ gifenc 는 넘겨받은 타입드 배열의 **byteOffset/length 를 보지 않고 밑의
  //    ArrayBuffer 전체를 읽는다**(실측: 길이 8이어야 할 인덱스가 24로 나왔다).
  //    캔버스에서 온 프레임(getImageData().data)은 자기 버퍼를 통째로 쓰니 무사하지만,
  //    WASM 경로의 프레임은 한 덩어리 raw 버퍼를 잘라 본 *뷰* 라 그대로 넣으면
  //    프레임 하나에 **영상 전체**가 들어가 GIF 가 프레임 수만큼 부풀었다
  //    (20프레임짜리가 212KB → 4.6MB. 조용히 커지기만 해서 더 위험했다).
  //    두 경로가 **반드시** 같은 결과를 내도록, 둘이 만나는 이 자리에서 한 번만 고친다.
  function ownBuffer(f, need) {
    if (f.byteOffset === 0 && f.buffer.byteLength === need) return f;
    return new Uint8ClampedArray(f.buffer.slice(f.byteOffset, f.byteOffset + need));
  }

  function encode(frames, w, h, fps) {
    return gifenc().then(function (G) {
      var enc = G.GIFEncoder();
      var delay = Math.round(1000 / fps);
      var need = w * h * 4;
      var i = 0;
      function step() {
        if (i >= frames.length) {
          enc.finish();
          return new Blob([enc.bytes()], { type: "image/gif" });
        }
        var f = ownBuffer(frames[i], need);
        // 프레임마다 256색 팔레트. **한도를 맞추려고 색을 더 깎지 않는다** —
        // 넘치면 내려받기를 막고 길이를 줄이라고 말하는 게 이 기능의 규율이다.
        var pal = G.quantize(f, 256, { format: "rgb565" });
        var idx = G.applyPalette(f, pal, "rgb565");
        enc.writeFrame(idx, w, h, { palette: pal, delay: delay });
        i += 1;
        progress(0.6 + i / frames.length * 0.4,
                 "GIF 로 굽는 중… " + i + "/" + frames.length);
        return idle().then(step);
      }
      return Promise.resolve().then(step);
    });
  }

  // ----------------------------------------------------------------- //
  // 7. 만들기 — 네이티브 → (실패하면) WASM → 인코딩 → **용량 가드**
  // ----------------------------------------------------------------- //
  function make() {
    if (busy || !current) return;
    var s = settings();
    // 원본 해상도는 `<video>` 메타데이터에서 온다 — **네이티브가 실패한 기기에서는
    // 그걸 못 읽는다.** 폴백 경로는 그게 없어도 돈다(ffmpeg 의 `scale=W:-2` 가 높이를
    // 스스로 정하고, 우리는 그 결과를 로그에서 읽는다). 그래서 크기를 모른다는
    // 이유로 폴백을 막지 않는다 — 막으면 폴백이 꼭 필요한 바로 그 기기에서만
    // 기능이 죽는다(실측된 구멍이다).
    var size = targetSize(s.width);
    if (!size) {
      if (nativeOK) { say("영상 크기를 아직 못 읽었어.", "error"); return; }
      size = { w: s.width, h: null };   // 높이는 디코더가 정하고 로그로 알려 준다
    }
    busy = true;
    invalidate();
    if (el.make) el.make.disabled = true;
    warn("");

    var usedPath = null;
    var chain;
    if (nativeOK) {
      usedPath = "native";
      chain = extractNative(el.video, s, size)
        .then(function (frames) { return { frames: frames, size: size }; })
        .catch(function (e) {
          // 미리보기에선 됐는데 본 추출에서 깨졌다 — 조용히 넘어가지 않고
          // 경로를 바꿨다고 **말한 뒤** 폴백한다.
          showDiag(e.code || "decode-failed", (e && e.message) || "");
          nativeOK = false;
          usedPath = "wasm";
          showDiag("wasm-ok", "네이티브 추출이 중간에 실패해서 폴백했어");
          return extractWasm(s, size);
        });
    } else {
      usedPath = "wasm";
      chain = extractWasm(s, size);
    }

    chain.then(function (got) {
      return encode(got.frames, got.size.w, got.size.h, s.fps)
        .then(function (blob) { return { blob: blob, size: got.size, n: got.frames.length }; });
    }).then(function (out) {
      lastBlob = out.blob;
      lastResult = {
        bytes: out.blob.size, width: out.size.w, height: out.size.h,
        frames: out.n, fps: s.fps, duration: s.duration, path: usedPath,
      };
      progress(null);
      var ok = out.blob.size <= SPEC.max_bytes;
      if (el.result) {
        el.result.textContent =
          out.size.w + "×" + out.size.h + " · " + out.n + "장 · " + s.fps + "fps · " +
          mb(out.blob.size) + " · " + (usedPath === "native" ? "네이티브 디코딩" : "브라우저 내 디코딩");
      }
      if (el.preview) {
        el.preview.src = URL.createObjectURL(out.blob);
        el.preview.hidden = false;
      }
      // ⛔ 용량 가드 — 넘치면 **몰래 깎지 않는다.** 막고 말한다.
      if (!ok) {
        warn(
          mb(out.blob.size) + " 로 한도 " + mb(SPEC.max_bytes) + " 를 " +
          mb(out.blob.size - SPEC.max_bytes) + " 넘었어. 화질을 자동으로 낮추지는 않아 — " +
          "길이를 줄여줘 (지금 " + s.duration.toFixed(1) + "초). " +
          "크기나 fps 를 낮추는 건 네가 골라."
        );
      }
      if (el.download) el.download.disabled = !ok;
      say(ok ? "다 됐어 — 내려받으면 돼 🎞" : "", ok ? "" : "error");
      return save({ settings: s, result: lastResult,
                    diag: { code: usedPath === "native" ? "native-ok" : "wasm-ok",
                            src_width: srcW, src_height: srcH } });
    }).catch(function (e) {
      progress(null);
      // 실패에는 반드시 이름이 있다. 던지는 쪽이 ``code`` 를 붙이므로 여기서
      // 메시지를 문자열로 더듬지 않는다(문구가 바뀌면 조용히 오분류되던 자리).
      var code = (e && e.code) || "decode-failed";
      showDiag(code, (e && e.message) || "");
      warn(DIAG_LINE[code] + " — GIF 를 만들지 않았어. 빈 파일을 내려주지는 않아.");
      say("", "error");
      saveDiag(code, { detail: (e && e.message) || "" });
    }).then(function () {
      busy = false;
      if (el.make) el.make.disabled = false;
    });
  }

  function download() {
    if (!lastBlob || !lastResult || lastResult.bytes > SPEC.max_bytes) return;
    var a = document.createElement("a");
    a.href = URL.createObjectURL(lastBlob);
    a.download = TOPIC.replace(/[\\/:*?"<>|]/g, "") + ".gif";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 5000);
    say("내려받았어 — " + mb(lastBlob.size) + " GIF 💾");
  }

  // ----------------------------------------------------------------- //
  // 8. 영상 고르기 — 서버가 실측으로 고른 바이트 경로를 따른다
  // ----------------------------------------------------------------- //
  function selectVideo(id) {
    current = null;
    sourceMode = null;
    srcW = srcH = 0;
    nativeOK = null;
    invalidate();
    for (var i = 0; i < VIDEOS.length; i++) if (VIDEOS[i].id === id) current = VIDEOS[i];
    if (!current) { showDiag("no-video", ""); return; }
    if (el.panel) el.panel.hidden = false;
    if (el.make) el.make.disabled = true;
    say("영상 불러오는 중…");
    // (a) OneDrive 직접 / (b) 앱 프록시 — **서버가 실제로 재 보고** 알려 준다.
    fetch(current.source_url, { credentials: "same-origin" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; })
      .then(function (src) {
        sourceMode = (src && src.mode) || "proxy";
        var url = (src && src.url) || current.stream_url;
        return loadMetadata(el.video, url, sourceMode === "direct");
      })
      .then(function (meta) {
        if (!meta.ok) {
          nativeOK = false;
          showDiag("decode-failed", meta.why);
          // 메타데이터조차 못 읽었다 = 네이티브는 끝. 그래도 **기능을 끄지는 않는다** —
          // 폴백 디코더가 남아 있다. 다만 무슨 일이 벌어지는지 먼저 말한다.
          showDiag("wasm-ok", meta.why);
          say("");
          if (el.make) el.make.disabled = false;
          refreshEstimate();
          return saveDiag("decode-failed", { detail: meta.why });
        }
        srcW = el.video.videoWidth;
        srcH = el.video.videoHeight;
        setupTrim(el.video.duration);
        watchDuration(el.video);
        return probeNative(el.video).then(function (res) {
          nativeOK = res.ok;
          showDiag(res.ok ? "native-ok" : "wasm-ok", res.why);
          say("");
          if (el.make) el.make.disabled = false;
          refreshEstimate();
          return saveDiag(res.ok ? "native-ok" : res.code, { detail: res.why });
        });
      });
  }

  // 길이를 **나중에** 알게 되는 영상이 있다(헤더에 duration 이 없는 스트리밍 녹화물은
  // loadedmetadata 때 Infinity 다). 그때 슬라이더 상한이 어림값에 묶인 채 남지 않게
  // duration 이 확정되면 한 번 다시 잡는다. 폰 카메라 파일은 보통 바로 알 수 있다.
  function watchDuration(v) {
    if (isFinite(v.duration) && v.duration > 0) return;
    function onDur() {
      if (!isFinite(v.duration) || v.duration <= 0) return;
      v.removeEventListener("durationchange", onDur);
      setupTrim(v.duration);
      refreshEstimate();
    }
    v.addEventListener("durationchange", onDur);
  }

  function setupTrim(duration) {
    if (!el.start || !el.dur) return;
    var d = duration && isFinite(duration) ? duration : SPEC.max_duration;
    el.start.min = 0;
    el.start.max = Math.max(0, d - SPEC.min_duration).toFixed(2);
    el.start.step = 0.1;
    if (parseFloat(el.start.value) > parseFloat(el.start.max)) el.start.value = 0;
    clampDuration(d);
    renderTrim();
  }
  function clampDuration(d) {
    var start = parseFloat(el.start.value) || 0;
    var room = Math.max(SPEC.min_duration, (d || SPEC.max_duration) - start);
    el.dur.min = SPEC.min_duration;
    el.dur.max = Math.min(SPEC.max_duration, room).toFixed(2);
    el.dur.step = 0.1;
    if (parseFloat(el.dur.value) > parseFloat(el.dur.max)) el.dur.value = el.dur.max;
  }
  function renderTrim() {
    if (el.startOut) el.startOut.textContent = (parseFloat(el.start.value) || 0).toFixed(1) + "초";
    if (el.durOut) el.durOut.textContent = (parseFloat(el.dur.value) || 0).toFixed(1) + "초";
  }

  // 구간 미리보기 — 시작점으로 가서 길이만큼만 재생하고 멈춘다.
  var playTimer = null;
  function playRange() {
    if (!current || !el.video) return;
    var s = settings();
    clearTimeout(playTimer);
    seek(el.video, s.start).then(function () {
      var p = el.video.play();
      if (p && p.catch) p.catch(function () {});
      playTimer = setTimeout(function () { el.video.pause(); }, s.duration * 1000);
    }).catch(function (e) {
      say("미리보기를 재생하지 못했어 — " + ((e && e.message) || ""), "error");
    });
  }

  // ----------------------------------------------------------------- //
  // 9. 업로드 — 사진과 같은 자리(OneDrive)에 올리고 목록에 더한다
  // ----------------------------------------------------------------- //
  function upload(file) {
    if (!UPLOAD_URL || !file) return;
    var fd = new FormData();
    fd.append("video", file);
    say("영상 올리는 중… (" + mb(file.size) + ")");
    if (el.uploadBtn) el.uploadBtn.disabled = true;
    fetch(UPLOAD_URL, { method: "POST", credentials: "same-origin", body: fd })
      .then(function (r) { return r.json().catch(function () { return null; }); })
      .then(function (j) {
        if (!j || !j.ok) {
          say((j && j.reason) || "영상을 올리지 못했어.", "error");
          return;
        }
        var v = j.video;
        v.stream_url = "/videos/" + v.id + "/stream";
        v.source_url = "/videos/" + v.id + "/source";
        v.delete_url = "/videos/" + v.id + "/delete";
        VIDEOS.unshift(v);
        var opt = document.createElement("option");
        opt.value = v.id;
        opt.textContent = v.name + " (" + mb(v.size) + ")";
        el.videoSel.insertBefore(opt, el.videoSel.firstChild);
        el.videoSel.value = String(v.id);
        if (el.empty) el.empty.hidden = true;
        el.videoSel.closest(".rv-gif-field").hidden = false;
        say("올렸어! 👍");
        selectVideo(v.id);
      })
      .catch(function () { say("영상을 올리지 못했어.", "error"); })
      .then(function () { if (el.uploadBtn) el.uploadBtn.disabled = false; });
  }

  function removeCurrent() {
    if (!current) return;
    if (!window.confirm("이 영상을 OneDrive 에서 지울까?")) return;
    var id = current.id, url = current.delete_url;
    fetch(url, { method: "POST", credentials: "same-origin",
                 headers: { "X-Requested-With": "fetch" } })
      .then(function () {
        VIDEOS = VIDEOS.filter(function (v) { return v.id !== id; });
        var opt = el.videoSel.querySelector('option[value="' + id + '"]');
        if (opt) opt.remove();
        current = null;
        invalidate();
        if (el.videoSel.options.length) {
          selectVideo(parseInt(el.videoSel.value, 10));
        } else {
          if (el.panel) el.panel.hidden = true;
          if (el.empty) el.empty.hidden = false;
          say("지웠어.");
        }
      })
      .catch(function () { say("영상을 지우지 못했어.", "error"); });
  }

  // ----------------------------------------------------------------- //
  // 10. 배선
  // ----------------------------------------------------------------- //
  if (el.upload) {
    el.upload.addEventListener("change", function () {
      if (el.upload.files && el.upload.files[0]) upload(el.upload.files[0]);
      el.upload.value = "";
    });
  }
  if (el.uploadBtn) el.uploadBtn.addEventListener("click", function () { el.upload.click(); });
  if (el.videoSel) {
    el.videoSel.addEventListener("change", function () {
      selectVideo(parseInt(el.videoSel.value, 10));
    });
  }
  if (el.del) el.del.addEventListener("click", removeCurrent);
  if (el.start) {
    el.start.addEventListener("input", function () {
      clampDuration(el.video.duration);
      renderTrim();
      invalidate();
      if (current) seek(el.video, parseFloat(el.start.value) || 0).catch(function () {});
    });
  }
  if (el.dur) {
    el.dur.addEventListener("input", function () { renderTrim(); invalidate(); });
  }
  [el.widths, el.fpses].forEach(function (g) {
    if (g) g.addEventListener("change", invalidate);
  });
  if (el.play) el.play.addEventListener("click", playRange);
  if (el.make) el.make.addEventListener("click", make);
  if (el.download) el.download.addEventListener("click", download);

  // 직전에 고른 설정이 있으면 되살린다(서버에 저장돼 있다).
  var saved = STATE.settings || null;
  if (saved) {
    if (el.start && saved.start != null) el.start.value = saved.start;
    if (el.dur && saved.duration != null) el.dur.value = saved.duration;
    var wr = el.widths && el.widths.querySelector('input[value="' + saved.width + '"]');
    if (wr) wr.checked = true;
    var fr = el.fpses && el.fpses.querySelector('input[value="' + saved.fps + '"]');
    if (fr) fr.checked = true;
  }
  renderTrim();
  if (VIDEOS.length) {
    var want = saved && saved.video_id;
    var has = want && VIDEOS.some(function (v) { return v.id === want; });
    var id = has ? want : VIDEOS[0].id;
    if (el.videoSel) el.videoSel.value = String(id);
    selectVideo(id);
  } else {
    if (el.panel) el.panel.hidden = true;
    if (el.empty) el.empty.hidden = false;
  }
})();
