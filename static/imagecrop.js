/* imagecrop.js — 발행용 크롭을 **브라우저가** 굽는다. 서버는 픽셀을 만지지 않는다.
 *
 * 왜 여기 있나
 * ------------
 * 크롭 좌표([x,y,w,h], 0~1 정규화)는 **해상도와 무관하다.** 그래서 "어디를 자를지"는
 * 서버(claude 비전)가 작은 렌디션만 보고 정하고, "실제로 자르기"는 **원본 픽셀을 가진
 * 쪽**이 하면 된다. 그 쪽이 브라우저다:
 *
 *   * 서버는 0.1 CPU · 512MB 한 칸인데 브라우저는 그렇지 않다.
 *   * 자르는 소스가 **원본**이라 화질이 다운스케일본에서 자르는 것보다 낫다.
 *   * 이 레포는 이미 썸네일(thumbnail.js)·GIF(gif.js)를 이렇게 만든다 — 새 설비가 아니다.
 *
 * ⛔ 규율
 * -------
 *  1. **표시용 크롭에는 이걸 쓰지 않는다.** 화면에 잘린 모습을 보여 주는 건 CSS
 *     (object-fit/object-position)가 한다 — 디코딩·인코딩이 없고 GPU 가 한다.
 *     여기 Canvas 인코딩은 **발행 버튼을 눌렀을 때 딱 1회**다.
 *  2. **한 장씩 순차 처리**한다. 12MP 한 장의 캔버스가 48MB라, 여러 장을 동시에
 *     올리면 폰에서 탭이 죽는다. 느려 보이지 않도록 진행률을 콜백으로 보고한다.
 *  3. EXIF 방향을 반드시 적용한다(`imageOrientation: 'from-image'`). 서버의
 *     `_exif_upright` 가 하던 일이고, 안 하면 세로 사진이 눕는다.
 *
 * 화질 메모 — 서버가 하던 것과 **세대 수가 같다**
 * ------------------------------------------------
 * 예전: 원본 JPEG → (서버)디코드 → 크롭 → q95 인코드.
 * 지금: 원본 JPEG → (브라우저)디코드 → 크롭 → q0.95 인코드.
 * 재인코딩은 양쪽 다 한 번이고, 소스는 양쪽 다 원본이다. 색 프로파일도 손해가 없다 —
 * 서버의 Pillow 경로는 `icc_profile` 을 넘기지 않아 **ICC 를 그냥 버리고 있었다**.
 * 크롭이 없는 사진은 아예 **원본 바이트를 그대로 통과**시킨다(세대 0 — 전보다 낫다).
 */
(function (root) {
  'use strict';

  var JPEG_QUALITY = 0.95;   // 서버가 쓰던 Pillow quality=95 와 같은 자리

  /** Blob → ImageBitmap|HTMLImageElement (EXIF 방향 적용). */
  function decode(blob) {
    if (typeof createImageBitmap === 'function') {
      try {
        return createImageBitmap(blob, { imageOrientation: 'from-image' })
          .catch(function () { return decodeViaImg(blob); });
      } catch (e) { /* 옵션 미지원 런타임 → 아래 폴백 */ }
    }
    return decodeViaImg(blob);
  }

  function decodeViaImg(blob) {
    return new Promise(function (resolve, reject) {
      var url = URL.createObjectURL(blob);
      var img = new Image();
      // <img> 경로는 브라우저가 EXIF 방향을 기본으로 적용한다(image-orientation:
      // from-image 가 CSS 기본값). 명시해도 해롭지 않다.
      try { img.style.imageOrientation = 'from-image'; } catch (e) {}
      img.onload = function () { resolve(img); };
      img.onerror = function () {
        URL.revokeObjectURL(url);
        reject(new Error('이미지를 디코드하지 못했어'));
      };
      img.src = url;
    });
  }

  function sizeOf(src) {
    return {
      w: src.width || src.naturalWidth || 0,
      h: src.height || src.naturalHeight || 0
    };
  }

  /** 정규화 crop 이 '자르지 않음'인가 (전체 프레임). */
  function isWholeFrame(crop) {
    if (!crop || crop.length !== 4) return true;
    var x = +crop[0], y = +crop[1], w = +crop[2], h = +crop[3];
    if (!isFinite(x) || !isFinite(y) || !isFinite(w) || !isFinite(h)) return true;
    return x <= 0.001 && y <= 0.001 && w >= 0.999 && h >= 0.999;
  }

  /**
   * 원본 Blob + 정규화 crop → 잘린 JPEG Blob.
   * 크롭이 없거나 전체 프레임이면 **원본 Blob 을 그대로** 돌려준다(재인코딩 0회).
   */
  function cropBlob(blob, crop) {
    if (isWholeFrame(crop)) return Promise.resolve(blob);
    return decode(blob).then(function (src) {
      var s = sizeOf(src);
      if (!s.w || !s.h) throw new Error('이미지 크기를 못 읽었어');
      var sx = Math.max(0, Math.min(s.w - 1, Math.round(+crop[0] * s.w)));
      var sy = Math.max(0, Math.min(s.h - 1, Math.round(+crop[1] * s.h)));
      var sw = Math.max(1, Math.min(s.w - sx, Math.round(+crop[2] * s.w)));
      var sh = Math.max(1, Math.min(s.h - sy, Math.round(+crop[3] * s.h)));
      var canvas = document.createElement('canvas');
      canvas.width = sw;
      canvas.height = sh;
      var ctx = canvas.getContext('2d');
      ctx.drawImage(src, sx, sy, sw, sh, 0, 0, sw, sh);
      if (src.close) { try { src.close(); } catch (e) {} }   // ImageBitmap 즉시 해제
      return new Promise(function (resolve, reject) {
        if (canvas.toBlob) {
          canvas.toBlob(function (out) {
            canvas.width = canvas.height = 0;                // 캔버스 메모리 해제
            out ? resolve(out) : reject(new Error('JPEG 인코딩 실패'));
          }, 'image/jpeg', JPEG_QUALITY);
        } else {
          try {
            var uri = canvas.toDataURL('image/jpeg', JPEG_QUALITY);
            canvas.width = canvas.height = 0;
            resolve(dataUriToBlob(uri));
          } catch (e) { reject(e); }
        }
      });
    });
  }

  function dataUriToBlob(d) {
    var c = d.indexOf(','), head = d.slice(0, c), b64 = d.slice(c + 1);
    var mime = (head.match(/data:([^;]+)/) || [])[1] || 'image/jpeg';
    var bin = atob(b64), n = bin.length, arr = new Uint8Array(n);
    for (var i = 0; i < n; i++) arr[i] = bin.charCodeAt(i);
    return new Blob([arr], { type: mime });
  }

  function blobToDataUri(blob) {
    return new Promise(function (resolve, reject) {
      var fr = new FileReader();
      fr.onload = function () { resolve(fr.result); };
      fr.onerror = function () { reject(fr.error || new Error('read fail')); };
      fr.readAsDataURL(blob);
    });
  }

  /**
   * 발행용 이미지 준비 — **한 장씩 순차로** 받아서 자른다.
   *
   * items: [{url, crop}] · onProgress(done, total, phase) · 반환 [{blob}] (입력 순서 유지)
   * phase 는 '받는 중' / '자르는 중' — 호출부가 사람에게 보여 줄 말이다.
   */
  function prepare(items, onProgress) {
    var out = [];
    var total = items.length;
    function step(i) {
      if (i >= total) return Promise.resolve(out);
      if (onProgress) onProgress(i, total, 'fetch');
      return fetch(items[i].url, { credentials: 'same-origin', cache: 'no-store' })
        .then(function (r) {
          if (!r.ok) throw new Error('img fetch ' + r.status);
          return r.blob();
        })
        .then(function (blob) {
          if (onProgress) onProgress(i, total, 'crop');
          return cropBlob(blob, items[i].crop);
        })
        .then(function (blob) {
          out.push({ blob: blob });
          if (onProgress) onProgress(i + 1, total, 'done');
          return step(i + 1);
        });
    }
    return step(0);
  }

  root.cdImageCrop = {
    cropBlob: cropBlob,
    blobToDataUri: blobToDataUri,
    prepare: prepare,
    isWholeFrame: isWholeFrame,
    JPEG_QUALITY: JPEG_QUALITY
  };
})(window);
