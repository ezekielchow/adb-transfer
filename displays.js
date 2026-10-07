/* Displays tab: switch screens on/off, arrange them, pick modes.
 * Talks to /api/displays*. Relies on api() and esc() from index.html. */
window.Displays = (() => {
  const $ = id => document.getElementById(id);
  const el = {
    banner: $("dBanner"), cards: $("dCards"), absent: $("dAbsent"),
    summary: $("dSummary"), quick: $("dQuick"), arena: $("dArena"),
    panel: $("dPanel"), apply: $("dApply"), reset: $("dReset"), dirty: $("dDirty"),
  };

  let data = null;        // last state from the server
  let draft = null;       // editable copy of the enabled outputs' layout
  let selected = null;    // output name shown in the settings panel
  let dirty = false;
  let busy = false;
  let visible = false;
  let pollTimer = null;
  let countdown = null;   // {left, timer}

  const post = (action, body) => api("/api/displays/" + action, {
    method: "POST", body: JSON.stringify(body || {}),
  });

  /* ------------------------------------------------------------ helpers */
  const isPanel = o => /^(eDP|LVDS|DSI)/.test(o.name);
  const label = o => isPanel(o) ? "Built-in display"
    : (o.monitor && !/^[A-Z]{3}$/.test(o.monitor) ? o.monitor : o.name);
  const hz = r => (Math.abs(r - Math.round(r)) < 0.005 ? Math.round(r) : r.toFixed(2)) + " Hz";
  const modeOf = (o, id) => o.modes.find(m => m.id === id);
  const outByName = n => data.outputs.find(o => o.name === n);
  const rotated = r => r === "left" || r === "right";

  function logical(d) {
    const o = outByName(d.name), m = modeOf(o, d.modeId);
    let w = m.w, h = m.h;
    if (rotated(d.rotation)) [w, h] = [h, w];
    return { w: Math.round(w * d.scaleX), h: Math.round(h * d.scaleY) };
  }

  function showBanner(html, warn) {
    el.banner.className = "banner show" + (warn ? " warn" : "");
    el.banner.innerHTML = html;
    const x = el.banner.querySelector("[data-dismiss]");
    if (x) x.onclick = hideBanner;
  }
  function hideBanner() {
    if (countdown) return;          // the keep/revert prompt must stay up
    el.banner.className = "banner";
    el.banner.innerHTML = "";
  }
  function problem(title, lines) {
    showBanner(`<div class="stack"><b>${esc(title)}</b>
      ${lines.length ? `<ul>${lines.map(l => `<li>${esc(l)}</li>`).join("")}</ul>` : ""}
      </div><div class="spacer"></div><button data-dismiss>Dismiss</button>`, true);
  }

  /* -------------------------------------------------------------- state */
  async function load(force) {
    const d = await api("/api/displays");
    if (d.error) {
      el.cards.innerHTML = `<div class="err">${esc(d.error)}</div>`;
      el.quick.innerHTML = ""; el.arena.innerHTML = ""; el.panel.innerHTML = "";
      return;
    }
    data = d;
    if (force || !dirty) resetDraft();
    render();
    if (d.pending && !countdown) startCountdown(d.pending.secondsLeft);
    if (d.autoReverted && !countdown && !load.shownRevert) {
      load.shownRevert = true;
      showBanner(`<div class="stack"><b>Display layout reverted</b>
        <span>It was not confirmed in time, so the previous layout is back.</span></div>
        <div class="spacer"></div><button data-dismiss>OK</button>`, true);
    }
    if (!d.autoReverted) load.shownRevert = false;
  }

  function resetDraft() {
    draft = data.outputs.filter(o => o.enabled && o.connected).map(o => ({
      name: o.name, enabled: true, modeId: o.modeId, x: o.x, y: o.y,
      rotation: o.rotation, reflect: o.reflect, scaleX: o.scaleX, scaleY: o.scaleY,
      primary: o.primary,
    }));
    dirty = false;
    if (!draft.some(d => d.name === selected)) selected = draft[0] ? draft[0].name : null;
  }

  function poll() {
    clearTimeout(pollTimer);
    if (!visible) return;
    pollTimer = setTimeout(async () => {
      if (!busy && !drag) await load(false);
      poll();
    }, 3000);
  }

  /* -------------------------------------------------------------- render */
  function render() {
    renderCards();
    renderQuick();
    renderArena();
    renderPanel();
    el.apply.disabled = !dirty || busy || !!countdown;
    el.reset.disabled = !dirty || busy;
    el.dirty.textContent = countdown ? "Confirm or revert the change above first."
      : dirty ? "Not applied yet. You get 15 seconds to confirm, otherwise it switches back." : "";
  }

  function renderCards() {
    const shown = data.outputs.filter(o => o.connected || o.enabled);
    const on = shown.filter(o => o.enabled && o.connected).length;
    el.summary.textContent = `${on} of ${shown.filter(o => o.connected).length} on`;
    el.cards.innerHTML = shown.map(o => {
      const ghost = o.enabled && !o.connected;
      const mode = o.enabled ? modeOf(o, o.modeId) : null;
      const detail = o.enabled && mode
        ? `${o.name} · ${mode.w}×${mode.h} · ${hz(o.rate || mode.rate)}`
        : o.name + (o.connected ? "" : " · unplugged");
      const onlyOne = o.enabled && on === 1 && o.connected;
      const bl = o.backlight;
      return `
      <div class="dCard ${o.enabled ? "on" : ""}" data-name="${esc(o.name)}">
        <div class="dTop">
          <div class="mon ${isPanel(o) ? "laptop" : ""}"></div>
          <div class="dInfo">
            <div class="dName">${esc(label(o))}
              ${o.primary && o.enabled ? `<span class="pill">Primary</span>` : ""}
              ${ghost ? `<span class="pill warn">Unplugged</span>` : ""}</div>
            <div class="meta">${esc(detail)}</div>
          </div>
          <label class="switch" title="${onlyOne ? "This is the only screen that is on" : (o.enabled ? "Turn off" : "Turn on")}">
            <input type="checkbox" data-power ${o.enabled ? "checked" : ""}
              ${onlyOne || busy || countdown || (!o.connected && !o.enabled) ? "disabled" : ""}
              aria-label="${esc(label(o))} power"><i></i>
          </label>
        </div>
        <div class="dState">${ghost
          ? "The cable is out but this screen still holds desktop space. Turn it off to reclaim it."
          : o.enabled
            ? `<b>On</b>${onlyOne ? " · the only screen on, so it can't be turned off" : ""}`
            : "Off"}</div>
        ${o.enabled && o.connected && bl ? `
          <div class="sl" title="Hardware backlight">
            <span>Backlight</span>
            <input type="range" min="1" max="100" value="${bl.percent}" data-backlight>
            <output>${bl.percent}%</output>
          </div>` : ""}
        ${o.enabled && o.connected && !bl ? `
          <div class="sl" title="Software dimming: the monitor's own backlight stays the same">
            <span>Brightness</span>
            <input type="range" min="20" max="100" value="${Math.round(o.brightness * 100)}" data-gamma>
            <output>${Math.round(o.brightness * 100)}%</output>
          </div>` : ""}
      </div>`;
    }).join("");

    const empty = data.outputs.filter(o => !o.connected && !o.enabled).map(o => o.name);
    el.absent.textContent = empty.length ? `Nothing plugged into ${empty.join(", ")}.` : "";

    el.cards.querySelectorAll(".dCard").forEach(card => {
      const name = card.dataset.name;
      const sw = card.querySelector("[data-power]");
      sw.onchange = () => power(name, sw.checked);
      const bl = card.querySelector("[data-backlight]");
      if (bl) slider(bl, v => post("backlight", { name, percent: +v }), v => v + "%");
      const g = card.querySelector("[data-gamma]");
      if (g) slider(g, v => post("brightness", { name, value: v / 100 }), v => v + "%");
    });
  }

  function slider(input, send, fmt) {
    const out = input.nextElementSibling;
    let t = null, inflight = false, queued = null;
    const flush = async () => {
      if (inflight) return;
      inflight = true;
      while (queued !== null) {
        const v = queued; queued = null;
        const r = await send(v);
        if (r.error) problem("Could not change brightness", [r.error]);
      }
      inflight = false;
    };
    input.oninput = () => {
      busy = true;                  // don't let a poll re-render mid-drag
      out.textContent = fmt(input.value);
      queued = input.value;
      clearTimeout(t);
      t = setTimeout(flush, 60);
    };
    input.onchange = () => { busy = false; };
  }

  /* ----------------------------------------------------------- on / off */
  async function power(name, on) {
    busy = true;
    const o = outByName(name);
    render();
    const card = el.cards.querySelector(`.dCard[data-name="${CSS.escape(name)}"] .dState`);
    if (card) card.textContent = on ? "Turning on…" : "Turning off…";
    let r;
    try {
      r = await post("power", { name, on });
    } finally {
      busy = false;
    }
    if (r.error) problem(`Could not turn ${label(o)} ${on ? "on" : "off"}`, [r.error]);
    else if (r.problems && r.problems.length) problem("The change did not fully take", r.problems);
    else hideBanner();
    await load(true);
  }

  async function only(name) {
    busy = true; render();
    const errors = [];
    try {
      const target = outByName(name);
      if (!target.enabled) {
        const r = await post("power", { name, on: true });
        if (r.error) errors.push(r.error);
      }
      if (!errors.length) {
        for (const o of data.outputs) {
          if (o.name !== name && o.enabled) {
            const r = await post("power", { name: o.name, on: false });
            if (r.error) errors.push(`${label(o)}: ${r.error}`);
          }
        }
      }
    } finally {
      busy = false;
    }
    if (errors.length) problem("Some screens could not be switched", errors);
    else hideBanner();
    await load(true);
  }

  /* ------------------------------------------------------ quick layouts */
  function renderQuick() {
    const conn = data.outputs.filter(o => o.connected);
    const chips = [];
    if (conn.length > 1) {
      conn.forEach(o => chips.push({ text: `${label(o)} only`, run: () => only(o.name) }));
      chips.push({ text: "Extend", run: extend, title: "All screens on, side by side" });
      chips.push({ text: "Mirror", run: mirror, title: "All screens show the same picture" });
    }
    const saved = Object.entries(data.layouts || {});
    const locked = busy || !!countdown;
    el.quick.innerHTML = chips.map((c, i) =>
      `<span class="chip"><button data-q="${i}" ${locked ? "disabled" : ""}
        title="${esc(c.title || "")}">${esc(c.text)}</button></span>`).join("") +
      saved.map(([t, v]) => `<span class="chip saved">
        <button data-s="${esc(t)}" ${locked ? "disabled" : ""} title="Saved ${esc(v.saved || "")}">★ ${esc(t)}</button>
        <button class="x" data-del="${esc(t)}" title="Delete this layout">&times;</button></span>`).join("") +
      `<span class="chip dashed"><button id="dSave" ${locked || dirty ? "disabled" : ""}
        title="${dirty ? "Apply your changes first" : "Save the current layout so you can switch back to it"}">+ Save current layout…</button></span>`;
    if (conn.length <= 1 && !saved.length) {
      el.quick.insertAdjacentHTML("afterbegin",
        `<span class="meta" style="align-self:center">Plug in another screen for extend and mirror options.</span>`);
    }
    el.quick.querySelectorAll("[data-q]").forEach(b => b.onclick = () => chips[+b.dataset.q].run());
    el.quick.querySelectorAll("[data-s]").forEach(b => b.onclick = () => applyRequest(
      post("layout/apply", { title: b.dataset.s })));
    el.quick.querySelectorAll("[data-del]").forEach(b => b.onclick = async () => {
      if (!confirm(`Delete the saved layout “${b.dataset.del}”?`)) return;
      const r = await post("layout/delete", { title: b.dataset.del });
      if (r.error) problem("Could not delete", [r.error]);
      load(false);
    });
    $("dSave").onclick = async () => {
      const title = prompt("Name this layout (e.g. Desk, Presenting):");
      if (!title) return;
      const outs = data.outputs.filter(o => o.connected).map(o => o.enabled ? {
        name: o.name, enabled: true, modeId: o.modeId, x: o.x, y: o.y, rotation: o.rotation,
        reflect: o.reflect, scaleX: o.scaleX, scaleY: o.scaleY, primary: o.primary,
      } : { name: o.name, enabled: false });
      const r = await post("layout/save", { title, outputs: outs });
      if (r.error) problem("Could not save the layout", [r.error]);
      load(false);
    };
  }

  const preferred = o => o.modes.find(m => m.preferred) || o.modes[0];

  function extend() {
    const conn = data.outputs.filter(o => o.connected && o.modes.length);
    // built-in panel first (leftmost), then the rest in their current order
    conn.sort((a, b) => (isPanel(b) - isPanel(a)) || (a.x - b.x));
    let x = 0;
    const hasPrimary = conn.some(o => o.enabled && o.primary);
    const want = conn.map((o, i) => {
      const m = o.enabled ? modeOf(o, o.modeId) : preferred(o);
      const d = {
        name: o.name, enabled: true, modeId: m.id, x, y: 0,
        rotation: o.enabled ? o.rotation : "normal", reflect: o.enabled ? o.reflect : "normal",
        scaleX: o.enabled ? o.scaleX : 1, scaleY: o.enabled ? o.scaleY : 1,
        primary: hasPrimary ? (o.enabled && o.primary) : i === 0,
      };
      x += logical(d).w;
      return d;
    });
    applyRequest(post("apply", { outputs: want }));
  }

  function mirror() {
    const conn = data.outputs.filter(o => o.connected && o.modes.length);
    // largest resolution every screen supports
    const sizes = conn.map(o => new Set(o.modes.map(m => m.w + "x" + m.h)));
    const common = [...sizes[0]].filter(s => sizes.every(z => z.has(s)))
      .map(s => s.split("x").map(Number)).sort((a, b) => b[0] * b[1] - a[0] * a[1]);
    const base = conn.find(o => o.primary && o.enabled) || conn.find(isPanel) || conn[0];
    const want = conn.map(o => {
      const d = { name: o.name, enabled: true, x: 0, y: 0, rotation: "normal",
        reflect: "normal", scaleX: 1, scaleY: 1, primary: o === base };
      if (common.length) {
        const [w, h] = common[0];
        const ms = o.modes.filter(m => m.w === w && m.h === h && !m.interlace);
        const pick = ms.length ? ms : o.modes.filter(m => m.w === w && m.h === h);
        d.modeId = pick.sort((a, b) => b.rate - a.rate)[0].id;
      } else {
        // no shared size: scale every screen to show the base screen's desktop
        const bm = preferred(base), m = preferred(o);
        d.modeId = m.id;
        d.scaleX = +(bm.w / m.w).toFixed(4);
        d.scaleY = +(bm.h / m.h).toFixed(4);
      }
      return d;
    });
    applyRequest(post("apply", { outputs: want }));
  }

  /* ------------------------------------------------ apply & confirmation */
  async function applyRequest(promise) {
    busy = true; render();
    let r;
    try { r = await promise; } finally { busy = false; }
    if (r.error) {
      problem("Display settings not changed", [r.error]);
      await load(false);
      return;
    }
    await load(true);
    if (r.pending) startCountdown(r.pending.secondsLeft, r.problems || []);
  }

  function startCountdown(seconds, problems) {
    clearInterval(countdown && countdown.timer);
    countdown = { left: Math.ceil(seconds), problems: problems || [] };
    const draw = () => {
      el.banner.className = "banner show warn";
      el.banner.innerHTML = `<div class="stack">
          <b>Keep these display settings?</b>
          <span>Switching back in <span class="countdown">${countdown.left}s</span> unless you keep them.</span>
          ${countdown.problems.length ? `<ul>${countdown.problems.map(p => `<li>${esc(p)}</li>`).join("")}</ul>` : ""}
        </div><div class="spacer"></div>
        <button id="dKeep">Keep changes</button><button id="dRevert">Revert</button>`;
      $("dKeep").onclick = () => finish("keep");
      $("dRevert").onclick = () => finish("revert");
    };
    draw();
    render();
    $("dKeep").focus();
    countdown.timer = setInterval(async () => {
      countdown.left -= 1;
      if (countdown.left > 0) { draw(); return; }
      clearInterval(countdown.timer);
      countdown = null;
      hideBanner();
      setTimeout(() => load(true), 800);   // the server reverts on its own clock
    }, 1000);
  }

  async function finish(action) {
    if (!countdown) return;
    clearInterval(countdown.timer);
    countdown = null;
    const r = await post(action);
    hideBanner();
    if (r.error) problem(action === "keep" ? "Could not keep the settings" : "Could not revert", [r.error]);
    await load(true);
  }

  el.apply.onclick = () => {
    if (!dirty) return;
    // include switched-off connected outputs so the request is complete
    const off = data.outputs.filter(o => o.connected && !o.enabled)
      .map(o => ({ name: o.name, enabled: false }));
    applyRequest(post("apply", { outputs: [...draft, ...off] }));
  };
  el.reset.onclick = () => { resetDraft(); render(); };

  /* ----------------------------------------------------------- arrange */
  let drag = null;   // {name, startX, startY, px, py, scale}
  let view = null;   // {scale, ox, oy} mapping desktop px -> arena px

  function bounds(items) {
    const r = items.map(d => ({ ...d, ...logical(d) }));
    return {
      r, minX: Math.min(...r.map(d => d.x)), minY: Math.min(...r.map(d => d.y)),
      maxX: Math.max(...r.map(d => d.x + d.w)), maxY: Math.max(...r.map(d => d.y + d.h)),
    };
  }

  function renderArena() {
    if (!draft.length) {
      el.arena.innerHTML = `<div class="arenaEmpty">No screens are on.</div>`;
      return;
    }
    const b = bounds(draft);
    const W = el.arena.clientWidth || 600, H = el.arena.clientHeight || 280, pad = 28;
    if (!drag) {
      const scale = Math.min((W - pad * 2) / (b.maxX - b.minX), (H - pad * 2) / (b.maxY - b.minY));
      view = {
        scale,
        ox: (W - (b.maxX - b.minX) * scale) / 2 - b.minX * scale,
        oy: (H - (b.maxY - b.minY) * scale) / 2 - b.minY * scale,
      };
    }
    el.arena.innerHTML = b.r.map(d => {
      const o = outByName(d.name), m = modeOf(o, d.modeId);
      return `<div class="rect ${d.name === selected ? "sel" : ""} ${d.primary ? "primary" : ""}"
          data-name="${esc(d.name)}"
          style="left:${d.x * view.scale + view.ox}px; top:${d.y * view.scale + view.oy}px;
                 width:${d.w * view.scale}px; height:${d.h * view.scale}px">
          <div class="rn">${esc(label(o))}</div>
          <div class="rs">${m.w}×${m.h}${d.scaleX !== 1 ? ` → ${d.w}×${d.h}` : ""}</div>
        </div>`;
    }).join("");
    el.arena.querySelectorAll(".rect").forEach(rect => {
      rect.onpointerdown = ev => startDrag(ev, rect);
    });
  }

  function startDrag(ev, rect) {
    if (busy || countdown) return;
    const d = draft.find(x => x.name === rect.dataset.name);
    selected = d.name;
    drag = { d, startX: d.x, startY: d.y, px: ev.clientX, py: ev.clientY, moved: false, rect };
    rect.setPointerCapture(ev.pointerId);
    el.arena.querySelectorAll(".rect").forEach(r => r.classList.toggle("sel", r === rect));
    rect.classList.add("dragging");
    renderPanel();
    rect.onpointermove = e => {
      const dx = (e.clientX - drag.px) / view.scale, dy = (e.clientY - drag.py) / view.scale;
      if (Math.abs(e.clientX - drag.px) + Math.abs(e.clientY - drag.py) > 3) drag.moved = true;
      rect.style.left = (drag.startX + dx) * view.scale + view.ox + "px";
      rect.style.top = (drag.startY + dy) * view.scale + view.oy + "px";
    };
    rect.onpointerup = rect.onpointercancel = e => {
      rect.onpointermove = rect.onpointerup = rect.onpointercancel = null;
      const dx = (e.clientX - drag.px) / view.scale, dy = (e.clientY - drag.py) / view.scale;
      const moved = drag.moved;
      drag = null;
      if (moved && draft.length > 1) {
        snap(d, Math.round(d.x + dx), Math.round(d.y + dy));
        normalize();
        markDirty();
      }
      render();
    };
  }

  const overlap = (a, b) =>
    a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

  /* Put a dropped screen flush against an edge of another one, at the
   * non-overlapping spot closest to where it was let go. Gaps between
   * screens trap the mouse, so they are never left behind. */
  function snap(d, x, y) {
    const me = logical(d);
    const others = draft.filter(o => o !== d).map(o => ({ ...o, ...logical(o) }));
    const cands = [];
    const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
    for (const o of others) {
      const ys = [clamp(y, o.y - me.h + 1, o.y + o.h - 1), o.y, o.y + o.h - me.h];
      const xs = [clamp(x, o.x - me.w + 1, o.x + o.w - 1), o.x, o.x + o.w - me.w];
      // snap nearly-aligned edges so they line up exactly
      const near = (v, t) => Math.abs(v - t) < Math.max(me.w, me.h) * 0.06 ? t : v;
      ys.forEach(yy => {
        yy = near(near(yy, o.y), o.y + o.h - me.h);
        cands.push({ x: o.x + o.w, y: yy }, { x: o.x - me.w, y: yy });
      });
      xs.forEach(xx => {
        xx = near(near(xx, o.x), o.x + o.w - me.w);
        cands.push({ x: xx, y: o.y + o.h }, { x: xx, y: o.y - me.h });
      });
    }
    let best = null, bestD = Infinity;
    for (const c of cands) {
      const box = { x: c.x, y: c.y, w: me.w, h: me.h };
      if (others.some(o => overlap(box, o))) continue;
      const dist = Math.hypot(c.x - x, c.y - y);
      if (dist < bestD) { best = c; bestD = dist; }
    }
    if (best) { d.x = best.x; d.y = best.y; }
  }

  function normalize() {
    const minX = Math.min(...draft.map(d => d.x)), minY = Math.min(...draft.map(d => d.y));
    draft.forEach(d => { d.x -= minX; d.y -= minY; });
  }

  function markDirty() {
    const cur = data.outputs.filter(o => o.enabled && o.connected);
    const keys = ["modeId", "x", "y", "rotation", "reflect", "scaleX", "scaleY", "primary"];
    dirty = draft.some(d => {
      const o = cur.find(c => c.name === d.name);
      return !o || keys.some(k => o[k] !== d[k]);
    });
  }

  /* Changing a screen's size: shift the screens to its right/below by the
   * same amount so the arrangement keeps its shape. */
  function resize(d, change) {
    const before = logical(d);
    Object.assign(d, change);
    const after = logical(d);
    const dw = after.w - before.w, dh = after.h - before.h;
    draft.forEach(o => {
      if (o === d || (o.x === d.x && o.y === d.y)) return;
      if (o.x >= d.x + before.w) o.x += dw;
      if (o.y >= d.y + before.h) o.y += dh;
    });
    normalize();
    markDirty();
    render();
  }

  /* ------------------------------------------------------------ panel */
  const ROT = [["normal", "Landscape"], ["left", "Portrait (turned left)"],
    ["right", "Portrait (turned right)"], ["inverted", "Landscape (upside down)"]];
  const SCALES = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2];

  function renderPanel() {
    const d = draft.find(x => x.name === selected);
    if (!d) {
      el.panel.innerHTML = `<div class="meta">Select a screen to change its resolution, rotation and size.</div>`;
      return;
    }
    const o = outByName(d.name), cur = modeOf(o, d.modeId);
    const sizes = [];
    o.modes.forEach(m => {
      if (!sizes.some(s => s.w === m.w && s.h === m.h)) sizes.push(m);
    });
    sizes.sort((a, b) => b.w * b.h - a.w * a.h || b.w - a.w);
    const prefSize = preferred(o);
    const rates = o.modes.filter(m => m.w === cur.w && m.h === cur.h)
      .sort((a, b) => b.rate - a.rate || a.interlace - b.interlace);
    const locked = busy || countdown ? "disabled" : "";
    const baseW = rotated(d.rotation) ? cur.h : cur.w, baseH = rotated(d.rotation) ? cur.w : cur.h;

    el.panel.innerHTML = `
      <h3>${esc(label(o))} <span class="meta">${esc(o.name)}</span></h3>
      <label class="field">Resolution
        <select id="pRes" ${locked}>${sizes.map(m => `<option value="${m.w}x${m.h}"
          ${m.w === cur.w && m.h === cur.h ? "selected" : ""}>${m.w} × ${m.h}${
          m.w === prefSize.w && m.h === prefSize.h ? " (recommended)" : ""}</option>`).join("")}</select>
      </label>
      <label class="field">Refresh rate
        <select id="pRate" ${locked}>${rates.map(m => `<option value="${m.id}"
          ${m.id === d.modeId ? "selected" : ""}>${hz(m.rate)}${m.interlace ? " interlaced" : ""}</option>`).join("")}</select>
      </label>
      <label class="field">Orientation
        <select id="pRot" ${locked}>${ROT.map(([v, t]) => `<option value="${v}"
          ${v === d.rotation ? "selected" : ""}>${t}</option>`).join("")}</select>
      </label>
      <label class="field">Desktop size (larger = more space, smaller text)
        <select id="pScale" ${locked}>${
          [...new Set([...SCALES, d.scaleX])].sort((a, b) => a - b).map(s => `<option value="${s}"
          ${s === d.scaleX && d.scaleX === d.scaleY ? "selected" : ""}>${Math.round(baseW * s)} × ${Math.round(baseH * s)}${
          s === 1 ? " (native, sharpest)" : ""}</option>`).join("")}</select>
      </label>
      <label class="check"><input type="checkbox" id="pPrim" ${d.primary ? "checked" : ""} ${locked}>
        Main display (panel and new windows)</label>`;

    $("pRes").onchange = e => {
      const [w, h] = e.target.value.split("x").map(Number);
      const same = o.modes.filter(m => m.w === w && m.h === h)
        .sort((a, b) => a.interlace - b.interlace || b.rate - a.rate);
      // keep the current refresh rate if this size offers it
      const keep = same.find(m => Math.abs(m.rate - cur.rate) < 0.05 && !m.interlace);
      resize(d, { modeId: (keep || same[0]).id });
    };
    $("pRate").onchange = e => { d.modeId = e.target.value; markDirty(); render(); };
    $("pRot").onchange = e => resize(d, { rotation: e.target.value });
    $("pScale").onchange = e => { const s = +e.target.value; resize(d, { scaleX: s, scaleY: s }); };
    $("pPrim").onchange = e => {
      draft.forEach(x => { x.primary = e.target.checked ? x === d : (x === d ? false : x.primary); });
      markDirty(); render();
    };
  }

  window.addEventListener("resize", () => { if (visible && data && !drag) renderArena(); });

  return {
    show() { visible = true; load(false).then(poll); },
    hide() { visible = false; clearTimeout(pollTimer); },
  };
})();
