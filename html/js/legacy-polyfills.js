/**
 * Minimal legacy browser polyfills for Next.js 15 compatibility.
 *
 * Next.js 15 runtime requires Chrome 111+; UC Browser / older Chromium-based
 * browsers ship with engines as old as Chrome 60-90 and crash on missing APIs.
 *
 * This file patches the most common culprits:
 *   - localStorage safety   (null / SecurityError / Safari Private Browsing)
 *   - structuredClone       (Chrome 98+)
 *   - Object.hasOwn         (Chrome 93+)
 *   - Array.prototype.at    (Chrome 92+)
 *   - String.prototype.at   (Chrome 92+)
 *   - AggregateError        (Chrome 85+)
 *   - Promise.allSettled    (Chrome 76+)
 *   - String.prototype.replaceAll (Chrome 85+)
 *
 * Loaded synchronously before any Next.js code via <script> in <head>.
 */

/* eslint-disable no-extend-native */

// ── localStorage safety ──────────────────────────────────────────────────
// Must run FIRST. Some environments make localStorage null or throw on access:
//   - Safari Private Browsing: "The operation is insecure"
//   - Edge iframe sandbox: "Failed to read the 'localStorage' property"
//   - Android WebView: localStorage is null
// Replace with a no-op MemoryStorage so ALL code (including 3rd-party libs)
// can safely call localStorage.getItem / setItem without crashing.
(function () {
  try {
    if (localStorage !== null && localStorage !== undefined) {
      // Try a real read to catch Safari Private Browsing (object exists but throws)
      localStorage.getItem("__ls_probe__");
      return; // localStorage works, no patch needed
    }
  } catch (_e) {
    // localStorage is broken — fall through to patch
  }

  // Minimal in-memory Storage polyfill
  var data = {};
  function noop() {}
  var MemoryStorage = {
    getItem: function (k) { return Object.prototype.hasOwnProperty.call(data, k) ? data[k] : null; },
    setItem: function (k, v) { data[k] = String(v); },
    removeItem: function (k) { delete data[k]; },
    clear: function () { data = {}; },
    key: noop,
    length: 0,
  };
  try {
    Object.defineProperty(window, "localStorage", { value: MemoryStorage, writable: false });
  } catch (_e) {
    // Some browsers don't allow overriding — at least we tried
  }
})();

// structuredClone — used heavily by React 19 / Next.js 15 runtime
if (typeof globalThis.structuredClone !== "function") {
  globalThis.structuredClone = function structuredClone(value) {
    // JSON round-trip handles plain data; for unsupported types it will throw,
    // matching native behaviour for non-cloneable values.
    return JSON.parse(JSON.stringify(value));
  };
}

// Object.hasOwn — used by react-markdown internals and other deps
if (typeof Object.hasOwn !== "function") {
  Object.hasOwn = function hasOwn(obj, prop) {
    return Object.prototype.hasOwnProperty.call(obj, prop);
  };
}

// Array.prototype.at
if (typeof Array.prototype.at !== "function") {
  Array.prototype.at = function at(index) {
    var len = this.length;
    var i = index >= 0 ? index : len + index;
    return i >= 0 && i < len ? this[i] : undefined;
  };
}

// String.prototype.at
if (typeof String.prototype.at !== "function") {
  String.prototype.at = function at(index) {
    var len = this.length;
    var i = index >= 0 ? index : len + index;
    return i >= 0 && i < len ? this.charAt(i) : undefined;
  };
}

// AggregateError — may be used by Promise.any / some libraries
if (typeof AggregateError === "undefined") {
  // @ts-expect-error polyfill
  globalThis.AggregateError = function AggregateError(errors, message) {
    var instance = new Error(message);
    instance.name = "AggregateError";
    instance.errors = errors;
    return instance;
  };
}

// Promise.allSettled — used by some data-fetching libs
if (typeof Promise.allSettled !== "function") {
  Promise.allSettled = function allSettled(promises) {
    return Promise.all(
      promises.map(function (p) {
        return Promise.resolve(p).then(
          function (value) { return { status: "fulfilled", value: value }; },
          function (reason) { return { status: "rejected", reason: reason }; },
        );
      }),
    );
  };
}

// Promise.any — Chrome 85+
if (typeof Promise.any !== "function") {
  Promise.any = function any(promises) {
    return new Promise(function (resolve, reject) {
      var errors = [];
      var remaining = promises.length;
      if (remaining === 0) {
        reject(new (globalThis.AggregateError || Error)(errors, "All promises were rejected"));
        return;
      }
      promises.forEach(function (p, i) {
        Promise.resolve(p).then(
          function (v) { resolve(v); },
          function (e) {
            errors[i] = e;
            remaining--;
            if (remaining === 0) {
              reject(new (globalThis.AggregateError || Error)(errors, "All promises were rejected"));
            }
          },
        );
      });
    });
  };
}

// ResizeObserver — Chrome 64+ / 缺失于老 iPad Safari、UCBrowser 等古董内核。
// 线上 /home 报 "Can't find variable: ResizeObserver"。提供最小兜底：observe 时
// 用 getBoundingClientRect 触发一次回调，并在 window resize 时复触发；不追求精确
// 增量，仅保证依赖它的组件不崩、能拿到初始尺寸。
if (typeof globalThis.ResizeObserver === "undefined") {
  globalThis.ResizeObserver = (function () {
    function PolyfillResizeObserver(callback) {
      this._cb = callback;
      this._els = [];
      var self = this;
      this._onResize = function () { self._emit(); };
      try { window.addEventListener("resize", this._onResize); } catch (_e) {}
    }
    PolyfillResizeObserver.prototype._emit = function () {
      var self = this;
      var entries = [];
      for (var i = 0; i < this._els.length; i++) {
        var el = this._els[i];
        var rect;
        try { rect = el.getBoundingClientRect(); } catch (_e) { rect = { width: 0, height: 0, top: 0, left: 0, right: 0, bottom: 0 }; }
        entries.push({ target: el, contentRect: rect });
      }
      if (entries.length) {
        try { this._cb(entries, self); } catch (_e) {}
      }
    };
    PolyfillResizeObserver.prototype.observe = function (el) {
      if (this._els.indexOf(el) === -1) this._els.push(el);
      var self = this;
      // 异步触发一次初始回调，模拟原生 observe 行为
      try { (window.requestAnimationFrame || window.setTimeout)(function () { self._emit(); }, 0); } catch (_e) {}
    };
    PolyfillResizeObserver.prototype.unobserve = function (el) {
      var idx = this._els.indexOf(el);
      if (idx !== -1) this._els.splice(idx, 1);
    };
    PolyfillResizeObserver.prototype.disconnect = function () {
      this._els = [];
      try { window.removeEventListener("resize", this._onResize); } catch (_e) {}
    };
    return PolyfillResizeObserver;
  })();
}

// String.prototype.replaceAll — Chrome 85+
if (typeof String.prototype.replaceAll !== "function") {
  String.prototype.replaceAll = function replaceAll(search, replacement) {
    if (typeof search === "object" && search !== null && search[Symbol.replace]) {
      return this.replace(search, replacement);
    }
    var escaped = String(search).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    return this.replace(new RegExp(escaped, "g"), replacement);
  };
}
