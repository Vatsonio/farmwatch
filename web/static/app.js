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
  async function loadLog() {
    const name = $("#log-name").value;
    const view = $("#log-view");
    try {
      const txt = await (await fetch("/api/logs?name=" + name + "&tail=300")).text();
      view.textContent = txt || "(empty)";
      $("#log-meta").textContent = txt ? txt.split("\n").length + " lines" : "";
      view.scrollTop = view.scrollHeight;
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
        return '<div class="active-row"><span class="active-row__name" title="' + _esc(p.file || "") + '">'
          + _esc(p.name) + '</span>'
          + '<div class="active-row__bar"><div class="active-row__fill" style="width:' + pct + '%"></div></div>'
          + '<span class="active-row__meta">' + pct + '%   ' + _esc(_eta(p.remaining_time)) + '</span></div>';
      }).join("");
    } catch (e) { /* keep last */ }
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
    $("#log-refresh").onclick = loadLog;
    $("#log-name").onchange = loadLog;
    window.addEventListener("beforeunload", (e) => {
      if (JSON.stringify(cfg) !== original) { e.preventDefault(); e.returnValue = ""; }
    });

    loadStatus();
    loadMetrics();
    loadDiagnostics();
    loadLog();
    setInterval(loadStatus, 5000);
    setInterval(loadMetrics, 4000);
    tick(); setInterval(tick, 1000);
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
