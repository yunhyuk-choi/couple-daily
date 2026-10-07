/* 썸네일 렌더 — 브라우저가 그린다 (노선 2 Step 4).
 *
 * gf-blog 는 Playwright + Chrome headless 로 렌더했다. 우리는 Render 무료티어
 * (512MB · 0.1 CPU)라 Chromium 을 올릴 수 없어서, **이미 인앱 미리보기를 그리고 있는
 * 바로 그 브라우저** 에서 같은 일을 한다:
 *
 *   1. DOM 미리보기 — design/thumbnail-template.css 의 선언을 **그대로** 붙인
 *      1080×1350 박스. 선택자만 붙이고 값은 보존한다(gf-blog render.py 와 같은 규율).
 *      진짜 브라우저가 레이아웃을 계산하므로 넘침 검증이 진짜다.
 *   2. 검증 — document.fonts.check() + 상자별 scrollWidth <= clientWidth.
 *      gf-blog render.py 의 두 assert 와 **같은 식**이다.
 *   3. 래스터화 — Canvas 2D 가 같은 수치로 그린다(외부 라이브러리 없음).
 *      transform: scale() 은 레이아웃 크기를 바꾸지 않으므로 축소 미리보기여도
 *      scrollWidth/clientWidth 는 1080 기준 실측값이다.
 *
 * ⛔ 넘치면 **폰트를 줄이지 않는다. 문구를 줄인다.** 그래서 이 파일 어디에도 글자
 *    크기를 건드리는 코드가 없고, 넘치는 동안 내려받기 버튼이 잠긴다.
 * ⛔ AI 이미지 생성 금지 — 배경은 사용자가 올린 실제 사진뿐이다.
 */
(function () {
  "use strict";

  var root = document.getElementById("rv-thumb");
  if (!root) return;

  var SPEC = readJson("rv-thumb-spec");
  var STATE = readJson("rv-thumb-state");
  if (!SPEC || !STATE || !STATE.candidates || !STATE.candidates.length) return;

  var STATE_URL = root.getAttribute("data-state-url");
  var TOPIC = root.getAttribute("data-topic") || "썸네일";
  var FIELDS = ["main_line1", "main_line2", "sub", "badge"];
  // 재는 상자 — gf-blog 가 assert 한 셋 그대로.
  var BOXES = ["main", "sub", "badge_label"];

  var el = {
    stage: root.querySelector(".rv-thumb-stage"),
    cover: root.querySelector(".rv-thumb-cover"),
    photo: root.querySelector(".rv-thumb-photo"),
    gradient: root.querySelector(".rv-thumb-gradient"),
    main: root.querySelector(".rv-thumb-main"),
    mainL1: root.querySelector(".rv-thumb-main-l1"),
    mainL2: root.querySelector(".rv-thumb-main-l2"),
    sub: root.querySelector(".rv-thumb-sub"),
    badge: root.querySelector(".rv-thumb-badge"),
    label: root.querySelector(".rv-thumb-badge-label"),
    status: root.querySelector(".rv-thumb-status"),
    warn: root.querySelector(".rv-thumb-warn"),
    download: root.querySelector(".rv-thumb-download"),
    check: root.querySelector(".rv-thumb-check"),
    photoSel: root.querySelector(".rv-thumb-photo-select"),
  };
  // 상자 이름 → DOM. 측정과 캔버스가 같은 이름을 쓴다.
  var boxEl = { main: el.main, sub: el.sub, badge_label: el.label };

  var fontsReady = false;
  var lastCheck = null;
  var saveTimer = null;

  // --------------------------------------------------------------------- //
  // 0. 유틸
  // --------------------------------------------------------------------- //
  function readJson(id) {
    var node = document.getElementById(id);
    if (!node) return null;
    try { return JSON.parse(node.textContent || "null"); } catch (e) { return null; }
  }
  function rgba(c) {
    return "rgba(" + c[0] + "," + c[1] + "," + c[2] + "," + c[3] + ")";
  }
  function fontShorthand(box) {
    return box.font_weight + " " + box.font_size + "px " + box.font_family;
  }
  function say(msg, kind) {
    if (!el.status) return;
    el.status.textContent = msg || "";
    el.status.className = "rv-thumb-status muted small" + (kind ? " " + kind : "");
  }

  // --------------------------------------------------------------------- //
  // 1. DOM 미리보기 — template.css 선언을 그대로 붙이고 선택자만 준다
  // --------------------------------------------------------------------- //
  // gf-blog render.py 의 `*{box-sizing:border-box}` — 배지의 width/height 가
  // padding 을 포함한다는 뜻이고, 캔버스 쪽 계산도 이 전제 위에 서 있다.
  var BORDER_BOX = "box-sizing:border-box;";

  function applyTemplateCss() {
    var css = SPEC.css || {};
    // 값 보존: 파싱한 숫자가 아니라 **원래 선언 문자열**을 그대로 올린다.
    // 앞뒤로 붙는 것은 선택자 역할을 하는 최소한의 거들뿐(gf-blog 와 같은 목록).
    if (el.cover) el.cover.style.cssText = BORDER_BOX + (css.canvas || "") + ";overflow:hidden;";
    if (el.gradient) el.gradient.style.cssText = BORDER_BOX + (css.gradient || "");
    if (el.main) el.main.style.cssText = BORDER_BOX + (css.main || "") + ";white-space:nowrap;";
    if (el.sub) el.sub.style.cssText = BORDER_BOX + (css.sub || "") + ";white-space:nowrap;";
    if (el.badge) el.badge.style.cssText = BORDER_BOX + (css.badge || "");
    if (el.label) {
      el.label.style.cssText =
        BORDER_BOX + (css.badge_label || "") + ";white-space:nowrap;text-align:center;";
    }
    // 사진은 Figma 내보내기에 없는 레이어(원본 사진 자리) — cover 로 채운다.
    if (el.photo) {
      el.photo.style.cssText =
        "position:absolute;inset:0;width:100%;height:100%;" +
        "object-fit:cover;object-position:center;";
    }
    fitStage();
  }

  // 미리보기를 카드 폭에 맞춰 축소한다. **transform 은 레이아웃을 바꾸지 않으므로**
  // 아래 측정값은 여전히 1080 기준의 진짜 수치다.
  function fitStage() {
    if (!el.stage || !el.cover) return;
    var avail = el.stage.parentElement.clientWidth || SPEC.canvas.width;
    var k = Math.min(1, avail / SPEC.canvas.width);
    el.cover.style.transform = "scale(" + k + ")";
    el.cover.style.transformOrigin = "top left";
    el.stage.style.height = Math.round(SPEC.canvas.height * k) + "px";
  }

  // --------------------------------------------------------------------- //
  // 2. 폰트 — 확인되기 전에는 아무것도 확정하지 않는다
  // --------------------------------------------------------------------- //
  function fontCheckList() {
    return [SPEC.main, SPEC.sub, SPEC.badge_label].map(fontShorthand);
  }
  function loadFonts() {
    if (!document.fonts) return Promise.resolve(false);
    var text = FIELDS.map(function (f) { return currentCopy()[f] || ""; }).join("");
    var jobs = fontCheckList().map(function (f) {
      return document.fonts.load(f, text).catch(function () { return null; });
    });
    return Promise.all(jobs).then(function () {
      return fontCheckList().every(function (f) {
        try { return document.fonts.check(f, text); } catch (e) { return false; }
      });
    });
  }

  // --------------------------------------------------------------------- //
  // 3. 상태 ↔ 화면
  // --------------------------------------------------------------------- //
  function inputFor(field) {
    return root.querySelector('.rv-thumb-input[data-field="' + field + '"]');
  }
  function currentCopy() {
    var out = {};
    FIELDS.forEach(function (f) {
      var input = inputFor(f);
      out[f] = input ? input.value : ((STATE.copy || {})[f] || "");
    });
    return out;
  }
  function paintDom() {
    var c = currentCopy();
    if (el.mainL1) el.mainL1.textContent = c.main_line1;
    if (el.mainL2) el.mainL2.textContent = c.main_line2;
    if (el.sub) el.sub.textContent = c.sub;
    if (el.label) el.label.textContent = c.badge;
    // 배지는 문구가 비면 알약만 남으므로 숨긴다. **display 가 아니라 visibility** 로
    // 숨긴다 — display 를 건드리면 템플릿의 `display: flex` 선언이 지워져 배지
    // 안쪽 정렬이 무너지고(실측된 버그), 레이아웃이 사라져 넘침도 못 잰다.
    if (el.badge) el.badge.style.visibility = c.badge ? "visible" : "hidden";
    // 메인·서브는 글자가 없으면 아무것도 안 보이므로 따로 숨길 필요가 없다.
  }
  function selectedPhoto() {
    if (!el.photoSel) return null;
    var opt = el.photoSel.options[el.photoSel.selectedIndex];
    // url     = 1080×1350 캔버스 래스터용 **원본**('내려받기'를 누를 때만 로드)
    // preview = 화면 DOM 미리보기용 우리 자산(고정 URL·만료 없음·재방문 네트워크 0)
    return opt ? {
      index: parseInt(opt.value, 10),
      url: opt.getAttribute("data-url"),
      preview: opt.getAttribute("data-preview") || opt.getAttribute("data-url"),
    } : null;
  }
  // 사진이 없는 후기의 폴백 배경 — 그라데이션의 끝 색(어두운 쪽)을 그대로 쓴다.
  // **AI 로 그림을 지어내지 않는다** — 단색 위에 타이포만 올린다.
  function fallbackBg() {
    var last = SPEC.gradient.stops[SPEC.gradient.stops.length - 1].rgba;
    return "rgb(" + last[0] + "," + last[1] + "," + last[2] + ")";
  }

  // --------------------------------------------------------------------- //
  // 4. 검증 — gf-blog 의 두 assert 와 같은 식
  // --------------------------------------------------------------------- //
  function measure() {
    var boxes = {};
    BOXES.forEach(function (name) {
      var node = boxEl[name];
      if (!node) return;
      var r = node.getBoundingClientRect();
      var cs = window.getComputedStyle(node);
      var cover = el.cover.getBoundingClientRect();
      var k = cover.width / SPEC.canvas.width || 1;
      boxes[name] = {
        // 캔버스 좌표계(1080 기준)로 되돌려 기록한다 — 축소 미리보기여도 좌표가
        // template.css 수치와 바로 대조된다.
        x: (r.left - cover.left) / k,
        y: (r.top - cover.top) / k,
        width: r.width / k,
        height: r.height / k,
        // scrollWidth/clientWidth 는 레이아웃 값이라 transform 과 무관하다.
        scrollWidth: node.scrollWidth,
        clientWidth: node.clientWidth,
        font: cs.font || (cs.fontWeight + " " + cs.fontSize + " " + cs.fontFamily),
      };
    });
    return {
      fonts_ok: fontsReady,
      font_checks: fontCheckList(),
      boxes: boxes,
    };
  }
  function verdict(m) {
    var over = [];
    BOXES.forEach(function (n) {
      var b = m.boxes[n];
      if (b && b.scrollWidth > b.clientWidth + 0.5) over.push(n);
    });
    return { ok: m.fonts_ok && !over.length, overflowing: over };
  }
  var LABEL = { main: "메인 문구", sub: "서브 문구", badge_label: "배지" };

  function refresh() {
    paintDom();
    var m = measure();
    var v = verdict(m);
    lastCheck = m;
    if (el.warn) {
      if (!m.fonts_ok) {
        el.warn.hidden = false;
        el.warn.textContent =
          "Pretendard 폰트를 불러오지 못했어. 다른 폰트로 그리면 검증된 디자인이 " +
          "아니라서 렌더를 막았어 — 네트워크를 확인하고 새로고침해줘.";
      } else if (v.overflowing.length) {
        el.warn.hidden = false;
        el.warn.textContent =
          v.overflowing.map(function (n) {
            var b = m.boxes[n];
            return LABEL[n] + "가 " + Math.round(b.scrollWidth - b.clientWidth) +
              "px 넘쳐";
          }).join(" · ") +
          " — 글자 크기는 줄이지 않아. 문구를 줄이거나 더 짧은 후보를 골라줘.";
      } else {
        el.warn.hidden = true;
        el.warn.textContent = "";
      }
    }
    if (el.download) el.download.disabled = !v.ok;
    if (el.check) {
      el.check.textContent = JSON.stringify(
        { ok: v.ok, fonts_ok: m.fonts_ok, overflowing: v.overflowing, boxes: m.boxes },
        null, 2
      );
    }
    return v;
  }

  // --------------------------------------------------------------------- //
  // 5. 서버에 저장 — 측정값만 보낸다(ok 판정은 서버가 다시 한다)
  // --------------------------------------------------------------------- //
  function save(extra) {
    if (!STATE_URL) return Promise.resolve(null);
    var photo = selectedPhoto();
    var body = {
      picked: pickedIndex(),
      copy: currentCopy(),
      photo_index: photo ? photo.index : 0,
      render_check: lastCheck || measure(),
    };
    if (extra) Object.keys(extra).forEach(function (k) { body[k] = extra[k]; });
    return fetch(STATE_URL, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest" },
      body: JSON.stringify(body),
    }).then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; });
  }
  function saveSoon() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(function () { save(); }, 900);
  }
  function pickedIndex() {
    var r = root.querySelector('input[name="rv-thumb-pick"]:checked');
    return r ? parseInt(r.value, 10) : (STATE.picked || 0);
  }

  // --------------------------------------------------------------------- //
  // 6. 래스터화 — Canvas 2D 가 같은 수치로 그린다
  // --------------------------------------------------------------------- //
  function loadImage(url) {
    return new Promise(function (resolve, reject) {
      var img = new Image();
      img.crossOrigin = "anonymous";   // /blog-img 는 ACAO:* — 캔버스가 오염되지 않는다
      img.onload = function () { resolve(img); };
      img.onerror = function () { reject(new Error("photo load failed")); };
      img.src = url;
    });
  }
  // CSS 줄상자의 베이스라인 — 반행간(half-leading) + ascent. 브라우저가 인라인
  // 박스를 놓는 방식과 같아서 DOM 미리보기와 캔버스가 어긋나지 않는다.
  // ⚠️ 여기서 쓰는 size 는 **재는 데만** 쓴다 — 그려 넣을 글자 크기는 ctx.font 가
  //    이미 정했고(스펙 값 그대로), 이 함수는 그 크기를 바꾸지 않는다.
  function baseline(ctx, top, lineHeight, size, lineIndex) {
    var m = ctx.measureText("가");
    var asc = m.fontBoundingBoxAscent;
    var desc = m.fontBoundingBoxDescent;
    if (!(asc > 0)) { asc = size * 0.88; desc = size * 0.12; }
    var half = (lineHeight - (asc + desc)) / 2;
    return top + lineIndex * lineHeight + half + asc;
  }
  function drawCover(ctx, img, W, H) {
    var scale = Math.max(W / img.naturalWidth, H / img.naturalHeight);
    var w = img.naturalWidth * scale, h = img.naturalHeight * scale;
    ctx.drawImage(img, (W - w) / 2, (H - h) / 2, w, h);   // object-position: center
  }
  function render() {
    var photo = selectedPhoto();
    var W = SPEC.canvas.width, H = SPEC.canvas.height;
    // 배경은 **후기에 붙인 사진**이다(기본값 = 첫 사진). 사진이 없는 후기만 단색 폴백.
    var bg = photo
      ? loadImage(photo.url)
      : Promise.resolve(null);
    return bg.then(function (img) {
      var canvas = document.createElement("canvas");
      canvas.width = W; canvas.height = H;          // device_scale_factor = 1
      var ctx = canvas.getContext("2d");
      if (img) {
        drawCover(ctx, img, W, H);
      } else {
        ctx.fillStyle = fallbackBg();
        ctx.fillRect(0, 0, W, H);
      }

      // 그라데이션 — template.css 의 색·투명도·위치 그대로.
      var g = ctx.createLinearGradient(0, SPEC.gradient.top, 0, SPEC.gradient.top + SPEC.gradient.height);
      SPEC.gradient.stops.forEach(function (s) { g.addColorStop(s.at, rgba(s.rgba)); });
      ctx.fillStyle = g;
      ctx.fillRect(SPEC.gradient.left, SPEC.gradient.top, SPEC.gradient.width, SPEC.gradient.height);

      var c = currentCopy();

      // 배지 — 둥근 사각형 + 가운데 정렬 라벨(flex center 와 같은 자리).
      if (c.badge) {
        var b = SPEC.badge, lb = SPEC.badge_label;
        var r = Math.min(b.radius, b.width / 2, b.height / 2);
        ctx.beginPath();
        if (ctx.roundRect) ctx.roundRect(b.left, b.top, b.width, b.height, r);
        else roundRectPath(ctx, b.left, b.top, b.width, b.height, r);
        ctx.fillStyle = b.background;
        ctx.fill();
        ctx.font = fontShorthand(lb);
        ctx.fillStyle = lb.color;
        ctx.textAlign = "center";
        ctx.textBaseline = "alphabetic";
        var inner = b.height - 2 * b.padding;
        var labelTop = b.top + b.padding + (inner - lb.height) / 2;
        ctx.fillText(
          c.badge, b.left + b.width / 2,
          baseline(ctx, labelTop, lb.line_height, lb.font_size, 0)
        );
      }

      // 메인 두 줄 · 서브 한 줄.
      ctx.textAlign = "left";
      ctx.textBaseline = "alphabetic";
      ctx.font = fontShorthand(SPEC.main);
      ctx.fillStyle = SPEC.main.color;
      [c.main_line1, c.main_line2].forEach(function (line, i) {
        if (!line) return;
        var m = SPEC.main;
        ctx.fillText(line, m.left, baseline(ctx, m.top, m.line_height, m.font_size, i));
      });
      if (c.sub) {
        var s = SPEC.sub;
        ctx.font = fontShorthand(s);
        ctx.fillStyle = s.color;
        ctx.fillText(c.sub, s.left, baseline(ctx, s.top, s.line_height, s.font_size, 0));
      }
      return canvas;
    });
  }
  function roundRectPath(ctx, x, y, w, h, r) {
    ctx.moveTo(x + r, y);
    ctx.arcTo(x + w, y, x + w, y + h, r);
    ctx.arcTo(x + w, y + h, x, y + h, r);
    ctx.arcTo(x, y + h, x, y, r);
    ctx.arcTo(x, y, x + w, y, r);
    ctx.closePath();
  }

  function download() {
    var v = refresh();
    if (!v.ok) return;                 // 넘치면 내보내지 않는다 — 규율이다
    say("그리는 중…");
    render().then(function (canvas) {
      canvas.toBlob(function (blob) {
        if (!blob) { say("PNG를 만들지 못했어.", "error"); return; }
        var a = document.createElement("a");
        a.href = URL.createObjectURL(blob);
        a.download = TOPIC.replace(/[\\/:*?"<>|]/g, "") + "-썸네일.png";
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        setTimeout(function () { URL.revokeObjectURL(a.href); }, 5000);
        say("내려받았어 — " + SPEC.canvas.width + "×" + SPEC.canvas.height + " PNG 💾");
        save();
      }, "image/png");
    }).catch(function (e) {
      say("사진을 불러오지 못했어 — " + (e && e.message ? e.message : "알 수 없는 오류"), "error");
    });
  }

  // --------------------------------------------------------------------- //
  // 7. 배선
  // --------------------------------------------------------------------- //
  applyTemplateCss();
  paintDom();

  root.querySelectorAll('input[name="rv-thumb-pick"]').forEach(function (r) {
    r.addEventListener("change", function () {
      var cand = STATE.candidates[parseInt(r.value, 10)];
      if (!cand) return;
      // 후보를 바꾸면 편집칸도 그 후보로 되돌린다(옛 후보의 편집본이 남지 않게).
      FIELDS.forEach(function (f) {
        var input = inputFor(f);
        if (input) input.value = cand[f] || "";
      });
      refresh();
      save();
    });
  });
  root.querySelectorAll(".rv-thumb-input").forEach(function (input) {
    input.addEventListener("input", function () { refresh(); saveSoon(); });
  });
  if (el.photoSel) {
    el.photoSel.addEventListener("change", function () {
      var p = selectedPhoto();
      // 화면 미리보기는 **미리보기 자산**이다 — 원본(수 MB)을 띄우지 않는다.
      if (p && el.photo) el.photo.src = p.preview;
      save();
    });
  }
  if (el.download) el.download.addEventListener("click", download);
  window.addEventListener("resize", function () { fitStage(); refresh(); });

  var first = selectedPhoto();
  if (first && el.photo) {
    // ⛔ 여기에 원본을 걸면 **상세 화면을 열 때마다** 수 MB를 받는다(아무도
    //    '내려받기'를 누르지 않아도). 화면엔 미리보기 자산, 캔버스엔 원본.
    el.photo.src = first.preview;                  // 기본 = 후기의 첫 사진
  } else if (el.cover) {
    if (el.photo) el.photo.remove();               // 사진 없는 후기 — 단색 폴백
    el.cover.style.backgroundColor = fallbackBg();
  }

  if (el.download) el.download.disabled = true;
  say("폰트 불러오는 중…");
  loadFonts().then(function (ok) {
    fontsReady = ok;
    say(ok ? "" : "폰트를 불러오지 못했어.", ok ? "" : "error");
    refresh();
  });
})();
