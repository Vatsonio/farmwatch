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
    el.className = "stat__val" + (cls ? " " + cls : "") + (id === "s-version" || id === "s-users" || id === "s-groups" || id === "s-dumps" ? " num" : "");
  }
  async function loadStatus() {
    try {
      const s = await (await fetch("/api/status")).json();
      $("#ver").textContent = "v" + s.version;
      document.title = "FarmWatch v" + s.version;
      setStat("s-version", s.version);
      setStat("s-bot", s.bot_running ? "running" : "stopped", s.bot_running ? "ok" : "");
      $("#bot-led").className = "led " + (s.bot_running ? "on" : "off");
      $("#bot-label").textContent = s.bot_running ? "BOT LIVE" : "BOT OFF";
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
  async function loadMetrics() {
    try {
      const m = await (await fetch("/api/metrics")).json();
      const conn = $("#metrics-conn");
      const s = m.summary || {};
      $$("[data-m]").forEach((n) => { n.textContent = m.connected ? (s[n.dataset.m] ?? 0) : "."; });
      conn.textContent = m.connected ? "live" : "monitor offline";
      conn.style.color = m.connected ? "var(--ok)" : "var(--text-muted)";
      const box = $("#metrics-active");
      if (!m.connected) {
        box.innerHTML = '<div class="metrics-off">Monitor offline. Start the Bambu client with the '
          + 'debug port (start-bambu-debug.bat) and keep a Dashboard window open.</div>';
        return;
      }
      const act = m.active || [];
      if (!act.length) { box.innerHTML = '<div class="metrics-off">No active prints right now.</div>'; return; }
      box.innerHTML = act.map((p) => {
        const pct = Math.max(0, Math.min(100, p.progress || 0));
        const eta = _eta(p.remaining_time);
        const kv = (k, v) => v ? '<span class="kv"><span class="kv__k">' + k + '</span><span class="kv__v">' + _esc(v) + '</span></span>' : "";
        const stats = [
          p.file ? '<span class="kv kv--file"><span class="kv__k">file</span><span class="kv__v" title="' + _esc(p.file) + '">' + _esc(p.file) + '</span></span>' : "",
          kv("nozzle", p.nozzle),
          kv("bed", p.bed),
          kv("speed", p.speed),
        ].join("");
        return '<div class="active-row">'
          + '<div class="active-row__top">'
          + '<span class="active-row__name">' + _esc(p.name) + '</span>'
          + (p.model ? '<span class="active-row__model">' + _esc(p.model) + '</span>' : "")
          + '<span class="active-row__eta">' + (eta ? _esc(eta) : "") + '</span>'
          + '</div>'
          + '<div class="active-row__progress">'
          + '<div class="active-row__bar"><div class="active-row__fill" style="width:' + pct + '%"></div></div>'
          + '<span class="active-row__pct">' + pct + '%</span>'
          + '</div>'
          + (stats ? '<div class="active-row__stats">' + stats + '</div>' : "")
          + '</div>';
      }).join("");
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
