# recorder/selectors.py
RECORDER_JS = r"""
(() => {
  // Build a short textual preview (safe) of innerText
  function textPreview(el) {
    try {
      const t = (el.innerText || "").trim().replace(/\s+/g, " ");
      return t.length > 60 ? t.slice(0, 57) + "..." : t;
    } catch { return null; }
  }

  function valuePreview(el) {
    try {
      if (el.tagName === "INPUT" && el.type === "password") return "••••••";
      if (el.tagName === "INPUT" || el.tagName === "TEXTAREA") {
        const v = (el.value ?? "").toString();
        return v.length > 40 ? v.slice(0, 37) + "..." : v;
      }
      return null;
    } catch { return null; }
  }

  function cssPath(el) {
    if (!(el instanceof Element)) return null;
    if (el.id) return `#${CSS.escape(el.id)}`;
    const parts = [];
    while (el && el.nodeType === 1 && el.tagName.toLowerCase() !== "html") {
      let part = el.tagName.toLowerCase();
      if (el.classList.length) {
        part += "." + Array.from(el.classList).map(c => CSS.escape(c)).join(".");
      }
      const parent = el.parentElement;
      if (!parent) break;
      const siblings = Array.from(parent.children).filter(e => e.tagName === el.tagName);
      if (siblings.length > 1) {
        const idx = siblings.indexOf(el) + 1;
        part += `:nth-of-type(${idx})`;
      }
      parts.unshift(part);
      el = parent;
      if (parts.length > 6) break; // limit depth
    }
    return parts.length ? parts.join(" > ") : null;
  }

  function xPath(el) {
    if (!(el instanceof Element)) return null;
    const idx = (sib) => {
      let i = 1;
      let s = sib;
      while ((s = s.previousSibling) != null) {
        if (s.nodeType === 1 && s.nodeName === sib.nodeName) i++;
      }
      return i;
    };
    const segs = [];
    let e = el;
    for (; e && e.nodeType === 1; e = e.parentNode) {
      let tag = e.nodeName.toLowerCase();
      let i = idx(e);
      segs.unshift(`${tag}[${i}]`);
      if (segs.length > 8) break;
    }
    return "//" + segs.join("/");
  }

  function elInfo(el) {
    if (!el || !(el instanceof Element)) return null;
    const role = el.getAttribute("role");
    const ariaLabel = el.getAttribute("aria-label");
    return {
      tag: el.tagName?.toLowerCase() ?? null,
      id: el.id || null,
      classes: el.className || null,
      name: el.getAttribute("name"),
      type: el.getAttribute("type"),
      role: role || null,
      ariaLabel: ariaLabel || null,
      title: el.getAttribute("title"),
      text: textPreview(el),
      value_preview: valuePreview(el),
      selectors: {
        css: cssPath(el),
        xpath: xPath(el)
      }
    };
  }

  // Debounce helper for noisy input
  const debounce = (fn, ms) => {
    let t; return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
  };

  // Keep a single recorder instance
  if (window.__webflowRecorder) return;
  window.__webflowRecorder = {
    _active: false,
    start() {
      if (this._active) return;
      this._active = true;

      const send = (etype, payload) => {
        if (!this._active) return;
        try {
          // binding defined by Python: window.__recordEventBridge
          window.__recordEventBridge({ etype, ...payload });
        } catch (e) {
          console.warn("recordEventBridge error", e);
        }
      };

      // Clicks
      this._click = (e) => {
        const el = e.target;
        const info = elInfo(el);
        const btn = e.button === 1 ? "middle" : (e.button === 2 ? "right" : "left");
        send("click", {
          url: location.href,
          x: e.clientX, y: e.clientY, button: btn,
          el: info,
          meta: {}
        });
      };
      window.addEventListener("click", this._click, true);

      // Keydown
      this._keydown = (e) => {
        send("keydown", {
          url: location.href,
          key: e.key, code: e.code,
          ctrl: e.ctrlKey, alt: e.altKey, shift: e.shiftKey, meta_key: e.metaKey,
          meta: {}
        });
      };
      window.addEventListener("keydown", this._keydown, true);

      // Input/change (debounced a bit)
      this._input = debounce((e) => {
        const el = e.target;
        if (!(el instanceof Element)) return;
        const info = elInfo(el);
        // never expose raw password
        const val = (el.tagName === "INPUT" && el.type === "password") ? "••••••" : (el.value ?? null);
        send("input", {
          url: location.href,
          el: info,
          input_value: (val && val.length > 40) ? (val.slice(0, 37) + "...") : val,
          meta: {}
        });
      }, 120);
      window.addEventListener("input", this._input, true);

      this._change = (e) => {
        const el = e.target;
        if (!(el instanceof Element)) return;
        const info = elInfo(el);
        const val = (el.tagName === "INPUT" && el.type === "password") ? "••••••" : (el.value ?? null);
        send("change", {
          url: location.href,
          el: info,
          value: (val && val.length > 40) ? (val.slice(0, 37) + "...") : val,
          meta: {}
        });
      };
      window.addEventListener("change", this._change, true);

      // Form submit
      this._submit = (e) => {
        const el = e.target;
        send("submit", { url: location.href, el: elInfo(el), meta: {} });
      };
      window.addEventListener("submit", this._submit, true);

      // Navigation/visibility
      this._vis = () => {
        send("visibility", { url: location.href, state: document.visibilityState === "visible" ? "visible" : "hidden", meta: {} });
      };
      document.addEventListener("visibilitychange", this._vis, true);

      this._pop = () => {
        send("nav", { from_url: null, to_url: location.href, meta: { reason: "popstate" } });
      };
      window.addEventListener("popstate", this._pop, true);

      // Initial visibility
      this._vis();
      send("nav", { from_url: null, to_url: location.href, meta: { reason: "load" } });
    },
    stop() {
      if (!this._active) return;
      window.removeEventListener("click", this._click, true);
      window.removeEventListener("keydown", this._keydown, true);
      window.removeEventListener("input", this._input, true);
      window.removeEventListener("change", this._change, true);
      window.removeEventListener("submit", this._submit, true);
      document.removeEventListener("visibilitychange", this._vis, true);
      window.removeEventListener("popstate", this._pop, true);
      this._active = false;
    }
  };
})();
"""
