// director_lib.js: helpers pre-injected (document start) into the page by `rec`. Everything is a global.
// Director scripts are the body of an async function; use e.g.  await click('New Tour');  rec.mark('title');
// Contract: waiters return a truthy value or null on timeout (they log, never throw, never hang) unless {throw:true}.
(function () {
  if (window.__recLib) return;
  window.__recLib = true;
  var R = window.rec || (window.rec = { cursor: true, log: function () {}, mark: function () {}, done: function () {} });
  var log = function () { try { R.log.apply(R, arguments); } catch (e) {} };

  // ---------- basics
  var sleep = function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); };
  var frame = function () { return new Promise(function (r) { requestAnimationFrame(function () { r(); }); }); };
  var q = function (sel, root) { return (root || document).querySelector(sel); };
  var qa = function (sel, root) { return Array.prototype.slice.call((root || document).querySelectorAll(sel)); };
  var norm = function (s) { return String(s == null ? '' : s).replace(/\s+/g, ' ').trim(); };
  var lower = function (s) { return norm(s).toLowerCase(); };

  var isVisible = function (el) {
    if (!el || !el.getClientRects) return false;
    if (!el.isConnected) return false;
    var rs = el.getClientRects();
    if (!rs.length) return false;
    var r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    var cs = getComputedStyle(el);
    if (cs.visibility === 'hidden' || cs.display === 'none' || parseFloat(cs.opacity) === 0) return false;
    return true;
  };

  // match helper: string (case-insensitive, whitespace-normalised) or RegExp
  var matcher = function (text, exact) {
    if (text instanceof RegExp) return function (s) { return text.test(norm(s)); };
    var t = lower(text);
    return exact ? function (s) { return lower(s) === t; } : function (s) { return lower(s).indexOf(t) >= 0; };
  };

  var SKIP = { SCRIPT: 1, STYLE: 1, NOSCRIPT: 1, TEMPLATE: 1, HEAD: 1, META: 1, LINK: 1, TITLE: 1, SVG: 0 };

  // All elements whose text (or aria-label/title/placeholder/value) matches; deepest match only.
  // opts: {tag: 'button' | 'button,a', exact, visible (default true), root}
  function byTextAll(text, opts) {
    opts = opts || {};
    var root = opts.root || document.body || document.documentElement;
    var vis = opts.visible !== false;
    var m = matcher(text, opts.exact);
    var cands = Array.prototype.slice.call(root.querySelectorAll(opts.tag || '*'));
    var hits = [];
    var set = new Set();
    var test = function (el) {
      if (SKIP[el.tagName]) return false;
      var t = el.textContent;
      return !!t && m(t);
    };
    for (var i = 0; i < cands.length; i++) {
      var el = cands[i];
      if (SKIP[el.tagName]) continue;
      if (test(el)) { hits.push(el); set.add(el); }
    }
    // keep deepest: drop a hit if one of its element-children (transitively) is also a hit
    var out = [];
    if (opts.tag) {
      // with an explicit tag the caller wants that element itself, not a deeper one
      out = hits;
    } else {
      out = hits.filter(function (el) {
        for (var c = el.firstElementChild; c; c = c.nextElementSibling) { if (set.has(c)) return false; }
        return true;
      });
    }
    if (!out.length) {
      var attr = Array.prototype.slice.call(root.querySelectorAll('[aria-label],[title],[placeholder],input[type=button],input[type=submit],img[alt]'));
      out = attr.filter(function (el) {
        return m(el.getAttribute('aria-label') || '') || m(el.getAttribute('title') || '') || m(el.getAttribute('placeholder') || '') ||
          ((el.type === 'button' || el.type === 'submit') && m(el.value || '')) || m(el.getAttribute('alt') || '');
      });
    }
    if (vis) out = out.filter(isVisible);
    return out;
  }
  function byText(text, opts) {
    var all = byTextAll(text, opts);
    if (!all.length) return null;
    var o = opts || {};
    if (o.last) return all[all.length - 1];
    return all[o.nth || 0] || null;
  }
  var CLICKABLE = 'button,a[href],[role=button],[role=tab],[role=link],[role=menuitem],[role=option],summary,label,input,select,textarea,[onclick],[tabindex]:not([tabindex="-1"])';
  var clickableOf = function (el) { return (el.closest && el.closest(CLICKABLE)) || el; };

  // text -> element to act on: prefer the deepest CLICKABLE element whose text matches; else the deepest text match's clickable ancestor
  var resolveText = function (target, opts) {
    var o = Object.assign({}, opts || {}); delete o.tag;
    var hits = byTextAll(target, Object.assign({}, o, { tag: CLICKABLE }));
    var deep = hits.filter(function (h) { return !hits.some(function (x) { return x !== h && h.contains(x); }); });
    if (deep.length) return o.last ? deep[deep.length - 1] : (deep[o.nth || 0] || deep[0]);
    var t = byText(target, o);
    return t ? clickableOf(t) : null;
  };
  var resolve = function (target, opts) {
    if (!target) return null;
    if (typeof target === 'string') {
      // CSS selector if it looks like one and matches, else text
      if (/^(css:|[#.\[])/.test(target)) { try { var e = document.querySelector(target.replace(/^css:/, '')); if (e) return e; } catch (x) {} return null; }
      return resolveText(target, opts);
    }
    if (target instanceof RegExp) return resolveText(target, opts);
    if (typeof target === 'function') return target();
    return target;
  };

  // ---------- waiting
  async function waitFor(fn, timeout, label, opts) {
    timeout = timeout == null ? 15000 : timeout;
    opts = opts || {};
    var t0 = performance.now();
    for (;;) {
      var v = null;
      try { v = await fn(); } catch (e) { v = null; }
      if (v) return v;
      if (performance.now() - t0 > timeout) {
        var msg = 'waitFor timed out after ' + timeout + ' ms' + (label ? ': ' + label : '');
        log('WARN ' + msg);
        if (opts.throw) throw new Error(msg);
        return null;
      }
      await sleep(opts.interval || 120);
    }
  }
  var bodyText = function () { return (document.body && document.body.innerText) || ''; };
  function hasText(text, opts) {
    opts = opts || {};
    if (opts.exact || opts.tag) return !!byText(text, opts);
    return matcher(text, false)(bodyText());
  }
  async function waitText(text, timeout, opts) {
    return waitFor(function () { return hasText(text, opts) ? true : null; }, timeout == null ? 20000 : timeout, 'text ' + String(text), opts);
  }
  async function waitGone(text, timeout, opts) {
    return waitFor(function () { return hasText(text, opts) ? null : true; }, timeout == null ? 20000 : timeout, 'gone ' + String(text), opts);
  }
  async function waitEl(target, timeout, opts) {
    return waitFor(function () { var e = resolve(target, opts); return e && isVisible(e) ? e : null; }, timeout == null ? 15000 : timeout, 'element ' + String(target), opts);
  }
  async function waitUrl(frag, timeout) {
    return waitFor(function () { return location.href.indexOf(frag) >= 0 ? true : null; }, timeout == null ? 15000 : timeout, 'url ' + frag);
  }

  // ---------- smooth scrolling
  function scrollParent(el) {
    for (var p = el && el.parentElement; p; p = p.parentElement) {
      var cs = getComputedStyle(p);
      if (/(auto|scroll)/.test(cs.overflowY) && p.scrollHeight > p.clientHeight + 4) return p;
    }
    return document.scrollingElement || document.documentElement;
  }
  var ease = function (t) { return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2; };
  async function smoothScrollBy(container, dy, dur) {
    container = container || document.scrollingElement;
    dur = dur == null ? Math.min(1400, 350 + Math.abs(dy) * 0.9) : dur;
    var start = container.scrollTop, t0 = performance.now();
    var max = container.scrollHeight - container.clientHeight;
    var target = Math.max(0, Math.min(max, start + dy));
    for (;;) {
      var p = Math.min(1, (performance.now() - t0) / dur);
      container.scrollTop = start + (target - start) * ease(p);
      if (p >= 1) break;
      await frame();
    }
    return container.scrollTop;
  }
  // bring el into view (centred) with a smooth scroll; no-op if comfortably visible
  async function scrollToEl(el, o) {
    o = o || {};
    el = resolve(el);
    if (!el) return null;
    var r = el.getBoundingClientRect();
    var margin = o.margin == null ? 70 : o.margin;
    var inView = r.top >= margin && r.bottom <= innerHeight - margin;
    if (inView && !o.force) return el;
    var sp = o.container || scrollParent(el);
    var cr = sp === document.scrollingElement || sp === document.documentElement ? { top: 0, height: innerHeight } : sp.getBoundingClientRect();
    var block = o.block || 'center';
    var delta = block === 'start' ? (r.top - cr.top - margin) : (r.top + r.height / 2) - (cr.top + cr.height / 2);
    if (r.height > cr.height - 2 * margin && block !== 'start') delta = r.top - cr.top - margin;
    await smoothScrollBy(sp, delta, o.duration);
    await sleep(80);
    return el;
  }

  // ---------- fake cursor overlay
  var cur = { el: null, x: null, y: null, ringEl: null, idle: null };
  function ensureCursor() {
    if (R.cursor === false) return null;
    if (cur.el && cur.el.isConnected) return cur.el;
    var d = document.createElement('div');
    d.id = '__rec_cursor';
    d.setAttribute('aria-hidden', 'true');
    d.style.cssText = 'position:fixed;left:0;top:0;z-index:2147483647;pointer-events:none;will-change:transform,opacity;opacity:0;transition:opacity .5s ease;';
    d.innerHTML = '<svg width="34" height="34" viewBox="0 0 24 24" style="display:block;margin-left:-3px;margin-top:-2px;filter:drop-shadow(0 2px 3px rgba(0,0,0,.45));transform-origin:3px 2px;transition:transform .09s ease">' +
      '<path d="M3 2 L3 19.2 L7.4 15 L10.5 21.8 L13.6 20.4 L10.5 13.8 L16.8 13.6 Z" fill="#ffffff" stroke="#14110f" stroke-width="1.5" stroke-linejoin="round"/></svg>';
    document.documentElement.appendChild(d);
    cur.el = d;
    if (cur.x == null) { cur.x = innerWidth * 0.86; cur.y = innerHeight * 0.88; }
    place(cur.x, cur.y);
    return d;
  }
  function place(x, y) { cur.x = x; cur.y = y; if (cur.el) cur.el.style.transform = 'translate3d(' + x.toFixed(1) + 'px,' + y.toFixed(1) + 'px,0)'; }
  async function cursorMove(x, y, dur) {
    var d = ensureCursor();
    if (!d) return;
    if (cur.idle) { clearTimeout(cur.idle); cur.idle = null; }
    d.style.opacity = '1';
    var x0 = cur.x, y0 = cur.y, dx = x - x0, dy = y - y0, dist = Math.sqrt(dx * dx + dy * dy);
    if (dist < 2) return;
    dur = dur == null ? Math.max(420, Math.min(900, 380 + dist * 0.45)) : dur;
    // quadratic bezier with a gentle sideways bow so it glides rather than slides
    var bow = Math.min(90, dist * 0.14) * (dx >= 0 ? -1 : 1);
    var cx = (x0 + x) / 2 + (-dy / dist) * bow, cy = (y0 + y) / 2 + (dx / dist) * bow;
    var t0 = performance.now();
    for (;;) {
      var p = Math.min(1, (performance.now() - t0) / dur), e = ease(p), u = 1 - e;
      place(u * u * x0 + 2 * u * e * cx + e * e * x, u * u * y0 + 2 * u * e * cy + e * e * y);
      if (p >= 1) break;
      await frame();
    }
  }
  function ripple(x, y) {
    if (R.cursor === false) return;
    var r = document.createElement('div');
    r.style.cssText = 'position:fixed;left:' + x + 'px;top:' + y + 'px;width:14px;height:14px;margin:-7px 0 0 -7px;border-radius:50%;border:3px solid #f5c542;box-shadow:0 0 14px rgba(245,197,66,.8);z-index:2147483646;pointer-events:none;opacity:.95;';
    document.documentElement.appendChild(r);
    var t0 = performance.now();
    (function step() {
      var p = Math.min(1, (performance.now() - t0) / 560);
      var s = 1 + p * 4.2;
      r.style.transform = 'scale(' + s + ')';
      r.style.opacity = String(0.95 * (1 - p));
      r.style.borderWidth = (3 * (1 - p * 0.6)) + 'px';
      if (p < 1) requestAnimationFrame(step); else r.remove();
    })();
  }
  async function press() {
    var d = ensureCursor();
    if (d && d.firstChild) d.firstChild.style.transform = 'scale(.84)';
    await sleep(110);
    if (d && d.firstChild) d.firstChild.style.transform = 'scale(1)';
  }
  var cursor = {
    move: cursorMove, ripple: ripple, press: press,
    show: function () { var d = ensureCursor(); if (d) d.style.opacity = '1'; },
    hide: function () { if (cur.el) cur.el.style.opacity = '0'; },
    remove: function () { R.cursor = false; if (cur.el) { cur.el.remove(); cur.el = null; } },
    pos: function () { return { x: cur.x, y: cur.y }; },
    moveToEl: async function (el, dur) { var p = pointOf(el); await cursorMove(p.x, p.y, dur); return p; },
  };

  function pointOf(el) {
    var r = el.getBoundingClientRect();
    var cands = [[0.5, 0.5], [0.3, 0.5], [0.7, 0.5], [0.5, 0.3], [0.5, 0.7], [0.15, 0.5], [0.85, 0.5]];
    for (var i = 0; i < cands.length; i++) {
      var x = r.left + r.width * cands[i][0], y = r.top + r.height * cands[i][1];
      x = Math.max(2, Math.min(innerWidth - 2, x)); y = Math.max(2, Math.min(innerHeight - 2, y));
      var top = document.elementFromPoint(x, y);
      if (top && (top === el || el.contains(top) || top.contains(el))) return { x: x, y: y, top: top };
    }
    var cx = Math.max(2, Math.min(innerWidth - 2, r.left + r.width / 2)), cy = Math.max(2, Math.min(innerHeight - 2, r.top + r.height / 2));
    return { x: cx, y: cy, top: document.elementFromPoint(cx, cy) || el };
  }

  // ---------- synthetic input
  function mouseInit(x, y, extra) {
    return Object.assign({ bubbles: true, cancelable: true, composed: true, view: window, clientX: x, clientY: y, screenX: x, screenY: y, button: 0, buttons: 0, detail: 1 }, extra || {});
  }
  function fire(el, type, x, y, extra) {
    var isPtr = type.indexOf('pointer') === 0;
    var ev;
    try {
      ev = isPtr ? new PointerEvent(type, mouseInit(x, y, Object.assign({ pointerId: 1, pointerType: 'mouse', isPrimary: true }, extra))) : new MouseEvent(type, mouseInit(x, y, extra));
    } catch (e) { ev = new MouseEvent(type.replace('pointer', 'mouse'), mouseInit(x, y, extra)); }
    el.dispatchEvent(ev);
  }
  async function click(target, o) {
    o = Object.assign({ timeout: 12000, hold: 90, enabledTimeout: 15000 }, o || {});
    var el = null;
    if (typeof target === 'string' || target instanceof RegExp || typeof target === 'function') {
      el = await waitFor(function () { var e = resolve(target, o); return e && isVisible(e) ? e : null; }, o.timeout, 'click target ' + String(target));
    } else el = target;
    if (!el) { log('click: NOT FOUND ' + String(target)); return null; }
    // wait for enabled
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') {
      var en = await waitFor(function () { return !(el.disabled || el.getAttribute('aria-disabled') === 'true') ? el : null; }, o.enabledTimeout, 'enabled ' + String(target));
      if (!en) { log('click: still disabled ' + String(target)); if (!o.force) return null; }
    }
    await scrollToEl(el);
    var p = pointOf(el);
    if (R.cursor !== false) await cursorMove(p.x, p.y, o.moveMs);
    if (o.before) await sleep(o.before);
    // the element may have re-rendered while the cursor travelled
    if (!el.isConnected) { var again = typeof target === 'string' ? resolve(target, o) : null; if (again) { el = again; p = pointOf(el); } }
    var t = (p.top && (p.top === el || el.contains(p.top) || p.top.contains(el))) ? p.top : el;
    ['pointerover', 'mouseover', 'pointerenter', 'mouseenter', 'pointermove', 'mousemove'].forEach(function (ty) { fire(t, ty, p.x, p.y); });
    if (R.cursor !== false) ripple(p.x, p.y);
    fire(t, 'pointerdown', p.x, p.y, { buttons: 1 });
    fire(t, 'mousedown', p.x, p.y, { buttons: 1 });
    if (el.focus && /^(INPUT|TEXTAREA|SELECT|BUTTON|A)$/.test(el.tagName)) { try { el.focus({ preventScroll: true }); } catch (e) {} }
    if (R.cursor !== false) await press(); else await sleep(o.hold);
    fire(t, 'pointerup', p.x, p.y);
    fire(t, 'mouseup', p.x, p.y);
    fire(t, 'click', p.x, p.y);
    if (R.cursor !== false && o.hideAfter !== 0) { if (cur.idle) clearTimeout(cur.idle); cur.idle = setTimeout(function () { cursor.hide(); }, o.hideAfter || 2400); }
    if (o.after) await sleep(o.after);
    log('click ' + (typeof target === 'string' ? JSON.stringify(target) : (el.tagName + (el.id ? '#' + el.id : ''))));
    return el;
  }
  async function hover(target, o) {
    var el = typeof target === 'string' ? await waitEl(target, (o || {}).timeout) : target;
    if (!el) return null;
    await scrollToEl(el);
    var p = pointOf(el);
    if (R.cursor !== false) await cursorMove(p.x, p.y, (o || {}).moveMs);
    ['pointerover', 'mouseover', 'pointerenter', 'mouseenter', 'pointermove', 'mousemove'].forEach(function (ty) { fire(p.top || el, ty, p.x, p.y); });
    return el;
  }

  var setNative = function (el, v) {
    var proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : el.tagName === 'SELECT' ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    var d = Object.getOwnPropertyDescriptor(proto, 'value');
    if (d && d.set) d.set.call(el, v); else el.value = v;
  };
  async function typeInto(target, text, o) {
    o = Object.assign({ perChar: false, delay: 65, click: true, clear: true }, o || {});
    var el = typeof target === 'string' || target instanceof RegExp ? await waitFor(function () { var e = resolve(target, o); return e && isVisible(e) ? e : null; }, o.timeout || 12000, 'typeInto target ' + String(target)) : target;
    if (!el) { log('typeInto: NOT FOUND ' + String(target)); return null; }
    if (o.click) { await click(el, { timeout: 4000 }); } else { try { el.focus({ preventScroll: true }); } catch (e) {} }
    try { el.focus({ preventScroll: true }); } catch (e) {}
    text = String(text);
    if (o.clear) setNative(el, '');
    if (!o.perChar) {
      setNative(el, text);
      el.dispatchEvent(new Event('input', { bubbles: true }));
    } else {
      var cur = '';
      for (var i = 0; i < text.length; i++) {
        var ch = text[i];
        var kinit = { key: ch, code: /[0-9]/.test(ch) ? 'Digit' + ch : /[a-z]/i.test(ch) ? 'Key' + ch.toUpperCase() : '', bubbles: true, cancelable: true, composed: true };
        el.dispatchEvent(new KeyboardEvent('keydown', kinit));
        el.dispatchEvent(new KeyboardEvent('keypress', kinit));
        cur += ch;
        setNative(el, cur);
        try { el.dispatchEvent(new InputEvent('input', { bubbles: true, data: ch, inputType: 'insertText' })); } catch (e) { el.dispatchEvent(new Event('input', { bubbles: true })); }
        el.dispatchEvent(new KeyboardEvent('keyup', kinit));
        await sleep(o.delay + Math.random() * o.delay * 0.5);
      }
    }
    el.dispatchEvent(new Event('change', { bubbles: true }));
    log('typed ' + JSON.stringify(text.length > 40 ? text.slice(0, 40) + '...' : text));
    return el;
  }

  var KEYS = {
    Enter: ['Enter', 13], Escape: ['Escape', 27], Tab: ['Tab', 9], ' ': ['Space', 32], Space: ['Space', 32], Backspace: ['Backspace', 8],
    ArrowUp: ['ArrowUp', 38], ArrowDown: ['ArrowDown', 40], ArrowLeft: ['ArrowLeft', 37], ArrowRight: ['ArrowRight', 39],
  };
  async function pressKey(key, o) {
    o = o || {};
    var tgt = o.target || document.activeElement || document.body;
    var code, kc;
    if (KEYS[key]) { code = KEYS[key][0]; kc = KEYS[key][1]; if (key === 'Space') key = ' '; }
    else if (/^[0-9]$/.test(key)) { code = 'Digit' + key; kc = 48 + Number(key); }
    else if (/^[a-z]$/i.test(key)) { code = 'Key' + key.toUpperCase(); kc = key.toUpperCase().charCodeAt(0); }
    else { code = key; kc = 0; }
    var init = { key: key, code: code, keyCode: kc, which: kc, bubbles: true, cancelable: true, composed: true, shiftKey: !!o.shift, ctrlKey: !!o.ctrl, altKey: !!o.alt, metaKey: !!o.meta };
    var mk = function (type) {
      var ev = new KeyboardEvent(type, init);
      if (ev.keyCode !== kc) { try { Object.defineProperty(ev, 'keyCode', { get: function () { return kc; } }); Object.defineProperty(ev, 'which', { get: function () { return kc; } }); } catch (e) {} }
      return ev;
    };
    tgt.dispatchEvent(mk('keydown'));
    if (key.length === 1) tgt.dispatchEvent(mk('keypress'));
    await sleep(o.hold == null ? 70 : o.hold);
    tgt.dispatchEvent(mk('keyup'));
    log('key ' + key);
  }

  // run a block; log + swallow errors so a director never aborts mid-film
  async function safe(label, fn) {
    try { return await fn(); } catch (e) { log('ERROR in ' + label + ': ' + (e && e.stack || e)); return null; }
  }
  // scroll positions / geometry helpers
  var textOf = function (sel) { var e = q(sel); return e ? norm(e.textContent) : ''; };

  var api = { sleep: sleep, frame: frame, q: q, qa: qa, norm: norm, isVisible: isVisible, byText: byText, byTextAll: byTextAll, resolve: resolve, clickableOf: clickableOf,
    waitFor: waitFor, waitText: waitText, waitGone: waitGone, waitEl: waitEl, waitUrl: waitUrl, hasText: hasText, bodyText: bodyText,
    click: click, hover: hover, typeInto: typeInto, pressKey: pressKey, cursor: cursor, scrollToEl: scrollToEl, smoothScrollBy: smoothScrollBy, scrollParent: scrollParent,
    safe: safe, textOf: textOf, pointOf: pointOf };
  Object.keys(api).forEach(function (k) { window[k] = api[k]; });
  R.lib = api;
})();
