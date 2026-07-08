/* farmwatch v2 settings panel.
   Loads config.json into the form, tracks unsaved changes, saves back,
   and shows program status, logs and the printer disappearance diagnostics. */
(() => {
  "use strict";

  let cfg = {};
  let original = "{}";
  const pad = (n) => String(n).padStart(2, "0");
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => [...r.querySelectorAll(s)];

  /* ---- path helpers ---- */
  function getPath(obj, path) {
    return path.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj);
  }
  function setPath(obj, path, val) {
    const keys = path.split(".");
    let o = obj;
    for (let i = 0; i < keys.length - 1; i++) {
      if (o[keys[i]] == null || typeof o[keys[i]] !== "object") o[keys[i]] = {};
      o = o[keys[i]];
    }
    o[keys[keys.length - 1]] = val;
  }

  /* ---- dirty tracking ---- */
  function markDirty() {
    const dirty = JSON.stringify(cfg) !== original;
    $("#savebar").classList.toggle("show", dirty);
  }

  /* ---- bind inputs / selects / toggles ---- */
  function bindControls() {
    $$("[data-path]").forEach((el) => {
      const path = el.dataset.path;
      const val = getPath(cfg, path);
      if (el.classList.contains("toggle")) {
        el.setAttribute("aria-checked", val ? "true" : "false");
        el.onclick = () => {
          const next = el.getAttribute("aria-checked") !== "true";
          el.setAttribute("aria-checked", next ? "true" : "false");
          setPath(cfg, path, next);
          markDirty();
        };
      } else if (el.tagName === "SELECT") {
        if (val != null) el.value = val;
        el.onchange = () => { setPath(cfg, path, el.value); markDirty(); };
      } else {
        el.value = val == null ? "" : val;
        el.oninput = () => {
          let v = el.value;
          if (el.type === "number") v = v === "" ? 0 : parseInt(v, 10) || 0;
          setPath(cfg, path, v);
          markDirty();
        };
      }
    });
  }

  /* ---- chips (int lists) ---- */
  function renderChips() {
    $$("[data-chips]").forEach((box) => {
      const path = box.dataset.chips;
      const list = getPath(cfg, path) || [];
      if (!list.length) { box.innerHTML = '<span class="chip-empty">none</span>'; return; }
      box.innerHTML = "";
      list.forEach((id, i) => {
        const chip = document.createElement("span");
        chip.className = "chip";
        chip.innerHTML = "<span>" + id + '</span><button class="chip__x" aria-label="Remove">✕</button>';
        chip.querySelector(".chip__x").onclick = () => {
          const arr = getPath(cfg, path).slice();
          arr.splice(i, 1);
          setPath(cfg, path, arr);
          renderChips(); markDirty();
        };
        box.appendChild(chip);
      });
    });
  }
  function bindChipAdders() {
    $$("[data-chip-add]").forEach((btn) => {
      const path = btn.dataset.chipAdd;
      const input = $('[data-chip-input="' + path + '"]');
      const add = () => {
        const v = parseInt(input.value, 10);
        if (Number.isNaN(v)) return;
        const arr = (getPath(cfg, path) || []).slice();
        if (!arr.includes(v)) arr.push(v);
        setPath(cfg, path, arr);
        input.value = "";
        renderChips(); markDirty();
      };
      btn.onclick = add;
      input.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); add(); } };
    });
  }

  /* ---- status strip ---- */
  function setStat(id, text, cls) {
    const el = $("#" + id);
    el.textContent = text;
    el.className = "stat__val" + (cls ? " " + cls : "") + (id === "s-users" || id === "s-groups" || id === "s-dumps" ? " num" : "");
  }
  async function loadStatus() {
    try {
      const s = await (await fetch("/api/status")).json();
      $("#ver").textContent = "v" + s.version;
      document.title = "FarmWatch v" + s.version;
      setStat("s-bot", s.bot_running ? "running" : "stopped", s.bot_running ? "ok" : "");
      $("#bot-led").className = "led " + (s.bot_running ? "on" : "off");
      $("#bot-label").textContent = s.bot_running ? "BOT LIVE" : "BOT OFF";
      const bp = $("#bot-power");
      if (bp && !bp.dataset.busy) {
        bp.dataset.running = s.bot_running ? "1" : "0";
        bp.textContent = s.bot_running ? "Stop bot" : "Start bot";
      }
      setStat("s-token", s.token_set ? "set" : "missing", s.token_set ? "ok" : "bad");
      setStat("s-users", s.users);
      setStat("s-groups", s.groups);
      setStat("s-debug", s.debug_logging ? "on" : "off", s.debug_logging ? "warn" : "");
      setStat("s-dumps", s.dumps, s.dumps > 0 ? "warn" : "");
    } catch (e) { /* keep last */ }
  }

  /* ---- diagnostics + logs ---- */
  async function loadDiagnostics() {
    try {
      const d = await (await fetch("/api/diagnostics")).json();
      const box = $("#diag-list");
      if (!d.disappearances || !d.disappearances.length) {
        box.innerHTML = '<div class="diag-empty">No disappearance events recorded. Turn on Debug logging to capture them.</div>';
      } else {
        box.innerHTML = "";
        d.disappearances.slice().reverse().forEach((ln) => {
          const div = document.createElement("div");
          div.className = "diag-line";
          div.textContent = ln;
          box.appendChild(div);
        });
      }
    } catch (e) {}
  }
  async function loadLog(forceBottom) {
    const name = $("#log-name").value;
    const view = $("#log-view");
    // Do not yank the view while the user is selecting text inside it.
    const sel = window.getSelection();
    if (sel && !sel.isCollapsed && view.contains(sel.anchorNode)) return;
    try {
      const atBottom = view.scrollHeight - view.scrollTop - view.clientHeight < 60;
      const txt = await (await fetch("/api/logs?name=" + name + "&tail=600")).text();
      if (txt !== view.textContent) {
        view.textContent = txt || "(empty)";
        $("#log-meta").textContent = txt ? (txt.split("\n").length + " lines") : "";
      }
      if (forceBottom || atBottom) view.scrollTop = view.scrollHeight;
    } catch (e) { view.textContent = "(could not load log)"; }
  }

  /* ---- farm metrics ---- */
  function _esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g,
      c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }
  function _eta(rt) {
    const m = /-?(\d+)h(\d+)m/.exec(rt || "");
    if (m) return (+m[1]) + "h " + String(+m[2]).padStart(2, "0") + "m left";
    const o = /-?(\d+)m/.exec(rt || "");
    return o ? (+o[1]) + "m left" : "";
  }
  function _activeCard(p) {
    const paused = (p.status === "paused");
    const finished = (p.status === "finished");
    const pct = finished ? 100 : Math.max(0, Math.min(100, p.progress || 0));
    const cls = paused ? " is-paused" : (finished ? " is-finished" : "");
    const right = finished ? "done" : (paused ? "paused" : (_eta(p.remaining_time) || ""));
    // file on its own truncating line; temps + speed on a second line that always
    // stays visible no matter how long the file name is.
    const spec = [p.nozzle, p.bed, p.speed].filter(Boolean).map(_esc).join("  ·  ");
    return '<div class="active-card' + cls + '">'
      + '<div class="active-card__top">'
      + '<span class="active-dot" aria-hidden="true"></span>'
      + '<span class="active-card__name" title="' + _esc(p.file || p.name) + '">' + _esc(p.name) + '</span>'
      + (p.model ? '<span class="active-card__model">' + _esc(p.model) + '</span>' : "")
      + '<span class="active-card__eta">' + _esc(right) + '</span>'
      + '</div>'
      + '<div class="active-card__progress">'
      + '<div class="active-card__bar"><div class="active-card__fill" style="width:' + pct + '%"></div></div>'
      + '<span class="active-card__pct">' + pct + '%</span>'
      + '</div>'
      + (p.file ? '<div class="active-card__file" title="' + _esc(p.file) + '">' + _esc(p.file) + '</div>' : "")
      + (spec ? '<div class="active-card__spec">' + spec + '</div>' : "")
      + (p.message ? '<div class="active-card__msg" title="' + _esc(p.message) + '">' + _esc(p.message) + '</div>' : "")
      + '</div>';
  }

  // Each section can be sorted independently; the chosen sort persists across refreshes.
  function _etaMins(rt) {
    const m = /-?(\d+)h(\d+)m/.exec(rt || "");
    if (m) return (+m[1]) * 60 + (+m[2]);
    const o = /-?(\d+)m/.exec(rt || "");
    if (o) return +o[1];
    return 1e9;  // unknown / none sorts last
  }
  const _SORTS = {
    progress: { label: "%", fn: (a, b) => (b.progress || 0) - (a.progress || 0) },
    eta: { label: "eta", fn: (a, b) => _etaMins(a.remaining_time) - _etaMins(b.remaining_time) },
    name: { label: "a-z", fn: (a, b) => String(a.name).localeCompare(String(b.name)) },
  };
  const _SORT_ORDER = ["progress", "eta", "name"];
  const _AGROUPS = [
    { st: "printing", label: "Printing" },
    { st: "paused", label: "Paused" },
    { st: "finished", label: "Finished" },
  ];
  const _sortState = { printing: "progress", paused: "progress", finished: "name" };
  let _activeCache = [];

  function _paintGroups(box) {
    const groups = { printing: [], paused: [], finished: [] };
    _activeCache.forEach((p) => { if (groups[p.status]) groups[p.status].push(p); });
    _AGROUPS.forEach((g) => {
      const key = _sortState[g.st];
      const arr = groups[g.st].slice().sort((_SORTS[key] || _SORTS.progress).fn);
      const grp = box.querySelector('.agroup[data-st="' + g.st + '"]');
      box.querySelector('[data-c="' + g.st + '"]').textContent = arr.length;
      grp.style.display = arr.length ? "" : "none";
      box.querySelector('[data-grid="' + g.st + '"]').innerHTML = arr.map(_activeCard).join("");
      grp.querySelectorAll(".sortbtn").forEach((b) => b.classList.toggle("active", b.dataset.sort === key));
    });
  }

  function _ensureGroups(box) {
    if (box.dataset.mode === "groups") return;
    box.dataset.mode = "groups";
    box.innerHTML = _AGROUPS.map((g) =>
      '<div class="agroup" data-st="' + g.st + '">'
      + '<div class="agroup__head"><span class="agroup__title">' + g.label + '</span>'
      + '<span class="agroup__sorts">'
      + _SORT_ORDER.map((k) => '<button class="sortbtn" type="button" data-sort="' + k + '">' + _SORTS[k].label + '</button>').join("")
      + '</span>'
      + '<span class="agroup__count" data-c="' + g.st + '">0</span></div>'
      + '<div class="active-grid" data-grid="' + g.st + '"></div></div>').join("");
    box.querySelectorAll(".agroup").forEach((grp) => {
      const st = grp.dataset.st;
      grp.querySelector(".agroup__head").onclick = () => grp.classList.toggle("is-collapsed");
      grp.querySelectorAll(".sortbtn").forEach((b) => {
        b.onclick = (e) => { e.stopPropagation(); _sortState[st] = b.dataset.sort; _paintGroups(box); };
      });
    });
  }

  async function loadMetrics() {
    try {
      const m = await (await fetch("/api/metrics")).json();
      const conn = $("#metrics-conn");
      const s = m.summary || {};
      $$("[data-m]").forEach((n) => {
        const v = s[n.dataset.m] ?? 0;
        n.textContent = m.connected ? v : ".";
        if (n.dataset.m === "printing" || n.dataset.m === "paused") {
          n.classList.toggle("pulse", m.connected && v > 0);
        }
        // emphasise the parent tile: tint critical states when non-zero,
        // dim context tiles when zero
        const tile = n.closest(".metric");
        if (!tile) return;
        const nonzero = m.connected && v > 0;
        if (tile.classList.contains("metric--printing") ||
            tile.classList.contains("metric--paused") ||
            tile.classList.contains("metric--offline")) {
          tile.toggleAttribute("data-nonzero", nonzero);
        }
        if (tile.classList.contains("metric--context")) {
          tile.toggleAttribute("data-zero", m.connected && v === 0);
        }
      });
      conn.textContent = m.connected ? "live" : "monitor offline";
      conn.style.color = m.connected ? "var(--ok)" : "var(--text-muted)";
      const box = $("#metrics-active");
      if (!m.connected) {
        box.dataset.mode = "off";
        box.innerHTML = '<div class="metrics-off">Monitor offline. Start the Bambu client with the '
          + 'debug port (start-bambu-debug.bat) and keep a Dashboard window open.</div>';
        return;
      }
      const act = m.active || [];
      const cnt = $("#active-count");
      const c = { printing: 0, paused: 0, finished: 0 };
      act.forEach((p) => { if (c[p.status] != null) c[p.status]++; });
      if (cnt) {
        cnt.textContent = act.length
          ? (c.printing + " printing · " + c.paused + " paused · " + c.finished + " finished") : "";
      }
      if (!act.length) {
        box.dataset.mode = "off";
        box.innerHTML = '<div class="metrics-off">No active prints right now.</div>';
        return;
      }
      _ensureGroups(box);
      _activeCache = act;
      _paintGroups(box);
    } catch (e) { /* keep last */ }
  }

  /* ---- copy to clipboard ---- */
  function copyText(t) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      return navigator.clipboard.writeText(t).catch(() => fallbackCopy(t));
    }
    return Promise.resolve(fallbackCopy(t));
  }
  function fallbackCopy(t) {
    const ta = document.createElement("textarea");
    ta.value = t; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.focus(); ta.select();
    try { document.execCommand("copy"); } catch (e) { /* ignore */ }
    document.body.removeChild(ta);
  }
  function copyBtn(btn, text) {
    copyText(text || "");
    const old = btn.textContent;
    btn.textContent = "Copied";
    btn.classList.add("copied");
    setTimeout(() => { btn.textContent = old; btn.classList.remove("copied"); }, 1200);
  }

  /* ---- serial ports (Display panel) ---- */
  async function loadSerialPorts() {
    const sel = $("#serial-port");
    if (!sel) return;
    let data = { ports: [], detected: null };
    try { data = await (await fetch("/api/serial-ports")).json(); } catch (e) {}
    const cur = getPath(cfg, "serial.port") || "auto";
    const devs = [];
    const opts = ['<option value="auto">auto' + (data.detected ? " (" + _esc(data.detected) + ")" : "") + "</option>"];
    (data.ports || []).forEach((p) => {
      devs.push(p.device);
      const tag = p.is_esp ? " [ESP]" : (p.description ? " — " + p.description : "");
      opts.push('<option value="' + _esc(p.device) + '">' + _esc(p.device + tag) + "</option>");
    });
    if (cur !== "auto" && !devs.includes(cur)) {
      opts.push('<option value="' + _esc(cur) + '">' + _esc(cur) + " (offline)</option>");
    }
    sel.innerHTML = opts.join("");
    sel.value = cur;
    const hint = $("#serial-hint");
    if (hint) hint.textContent = data.detected ? ("ESP on " + data.detected) : "no ESP detected";
  }

  /* ---- save / revert ---- */
  async function save() {
    const btn = $("#save");
    btn.disabled = true;
    try {
      const res = await fetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ config: cfg }),
      });
      const out = await res.json();
      if (out.ok) {
        cfg = out.config;
        original = JSON.stringify(cfg);
        bindControls(); renderChips();
        $("#savebar").classList.remove("show");
        flash();
        loadStatus();
      } else {
        alert("Save failed: " + (out.error || "unknown"));
      }
    } catch (e) {
      alert("Save failed: " + e);
    } finally {
      btn.disabled = false;
    }
  }
  function flash() {
    const f = $("#flash");
    f.classList.add("show");
    setTimeout(() => f.classList.remove("show"), 1600);
  }
  function revert() {
    cfg = JSON.parse(original);
    bindControls(); renderChips();
    $("#savebar").classList.remove("show");
  }

  /* ---- clock ---- */
  function tick() {
    const d = new Date();
    $("#clock").textContent = pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }

  /* ---- init ---- */
  async function init() {
    try {
      const data = await (await fetch("/api/config")).json();
      cfg = data.config || {};
    } catch (e) { cfg = {}; }
    original = JSON.stringify(cfg);

    bindControls();
    renderChips();
    bindChipAdders();

    $("#token-reveal").onclick = () => {
      const inp = $("#bot_token");
      const show = inp.type === "password";
      inp.type = show ? "text" : "password";
      $("#token-reveal").textContent = show ? "hide" : "show";
    };
    $("#save").onclick = save;
    $("#revert").onclick = revert;
    loadSerialPorts();
    $("#serial-refresh").onclick = () => loadSerialPorts();
    $("#log-refresh").onclick = () => loadLog(true);
    $("#log-name").onchange = () => loadLog(true);
    $("#diag-copy").onclick = () => copyBtn($("#diag-copy"), $("#diag-list").innerText);
    $("#log-copy").onclick = () => copyBtn($("#log-copy"), $("#log-view").innerText);
    $("#mon-restart").onclick = async () => {
      const b = $("#mon-restart"), prev = b.textContent;
      b.disabled = true; b.textContent = "Restarting";
      try {
        const r = await (await fetch("/api/monitor/restart", { method: "POST" })).json();
        b.textContent = r.ok ? "Reconnected" : "No client";
      } catch (e) { b.textContent = "Error"; }
      loadMetrics();
      setTimeout(() => { b.textContent = prev; b.disabled = false; }, 1600);
    };
    $("#bot-power").onclick = async () => {
      const b = $("#bot-power");
      const running = b.dataset.running === "1";
      b.dataset.busy = "1"; b.disabled = true; b.textContent = running ? "Stopping" : "Starting";
      try {
        await fetch(running ? "/api/bot/stop" : "/api/bot/start", { method: "POST" });
      } catch (e) {}
      setTimeout(() => { delete b.dataset.busy; b.disabled = false; loadStatus(); }, 2500);
    };
    $("#bot-restart").onclick = async () => {
      const b = $("#bot-restart"), prev = b.textContent;
      b.disabled = true; b.textContent = "Restarting";
      try {
        const r = await (await fetch("/api/bot/restart", { method: "POST" })).json();
        b.textContent = r.ok ? (r.action === "starting" ? "Starting" : "Restarted") : (r.error || "Failed");
      } catch (e) { b.textContent = "Error"; }
      setTimeout(loadStatus, 2500);
      setTimeout(() => { b.textContent = prev; b.disabled = false; }, 2600);
    };
    // collapsible panels: click the head to fold/unfold
    $$(".panel--collapsible > .panel__head").forEach((h) => {
      h.onclick = () => h.parentElement.classList.toggle("is-collapsed");
    });

    // tools
    const toolRun = async (btn, url, okText) => {
      const prev = btn.textContent;
      btn.disabled = true; btn.textContent = "...";
      try {
        const r = await (await fetch(url, { method: "POST" })).json();
        btn.textContent = r.ok ? okText : (r.error || "Failed");
        btn.classList.add(r.ok ? "ok" : "bad");
      } catch (e) { btn.textContent = "Error"; btn.classList.add("bad"); }
      setTimeout(() => { btn.textContent = prev; btn.disabled = false; btn.classList.remove("ok", "bad"); }, 2200);
    };
    $("#tool-test").onclick = () => toolRun($("#tool-test"), "/api/bot/test", "Sent");
    $("#tool-folder").onclick = () => toolRun($("#tool-folder"), "/api/open-folder", "Opened");
    $("#tool-reload").onclick = () => toolRun($("#tool-reload"), "/api/bot/restart", "Restarted");

    // clicking the Printing / Paused / Finished counters jumps to that section
    ["printing", "paused", "finished"].forEach((k) => {
      const cell = $('[data-m="' + k + '"]')?.closest(".metric");
      if (!cell) return;
      cell.classList.add("metric--jump");
      cell.title = "Show " + k;
      cell.onclick = () => {
        const g = $('.agroup[data-st="' + k + '"]');
        if (g) { g.classList.remove("is-collapsed"); g.scrollIntoView({ behavior: "smooth", block: "start" }); }
      };
    });

    // theme toggle (persisted; applied early by an inline head script to avoid flash)
    const applyTheme = (t) => {
      document.documentElement.setAttribute("data-theme", t);
      const b = $("#theme-toggle");
      if (b) { b.textContent = t === "light" ? "☀" : "◐"; b.title = t === "light" ? "Switch to dark" : "Switch to light"; }
    };
    let theme = (() => { try { return localStorage.getItem("fw-theme") || "dark"; } catch (e) { return "dark"; } })();
    applyTheme(theme);
    $("#theme-toggle").onclick = () => {
      theme = theme === "light" ? "dark" : "light";
      try { localStorage.setItem("fw-theme", theme); } catch (e) {}
      applyTheme(theme);
    };

    // fullscreen toggle. Inside the pywebview window the browser Fullscreen API does
    // nothing, so toggle the native window via the exposed pywebview api; in a plain
    // browser (dev) fall back to the Fullscreen API.
    const fsBtn = $("#fs-toggle");
    let fsOn = false;
    const hasPyWebview = () => window.pywebview && window.pywebview.api && window.pywebview.api.toggle_fullscreen;
    fsBtn.onclick = () => {
      if (hasPyWebview()) {
        window.pywebview.api.toggle_fullscreen();
        fsOn = !fsOn;
        fsBtn.textContent = fsOn ? "🗗" : "⛶";
      } else if (document.fullscreenElement) {
        document.exitFullscreen().catch(() => {});
      } else {
        document.documentElement.requestFullscreen().catch(() => {});
      }
    };
    document.addEventListener("fullscreenchange", () => { fsBtn.textContent = document.fullscreenElement ? "🗗" : "⛶"; });

    window.addEventListener("beforeunload", (e) => {
      if (JSON.stringify(cfg) !== original) { e.preventDefault(); e.returnValue = ""; }
    });

    loadStatus();
    loadMetrics();
    loadDiagnostics();
    loadLog(true);
    setInterval(loadStatus, 5000);
    setInterval(loadMetrics, 4000);
    setInterval(loadDiagnostics, 10000);
    setInterval(() => { const lv = $("#log-live"); if (lv && lv.checked) loadLog(); }, 3000);
    tick(); setInterval(tick, 1000);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
