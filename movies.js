/* Movies tab: genres and running time per movie, looked up on Wikidata.
 * Talks to /api/movies*. Relies on api(), esc() and currentSerial() from index.html. */
window.Movies = (() => {
  const $ = id => document.getElementById(id);
  const el = {
    side: $("mSide"), folder: $("mFolder"), update: $("mUpdate"), status: $("mStatus"),
    search: $("mSearch"), views: $("mViews"), lens: $("mLens"), genres: $("mGenres"),
    body: $("mBody"),
  };
  const DEFAULT = { device: "/sdcard/Movies", local: "~/Videos" };
  const LENGTHS = [
    { id: "", label: "Any length" },
    { id: "short", label: "Under 1h 45m", test: m => m < 105 },
    { id: "mid", label: "1h 45m – 2h 15m", test: m => m >= 105 && m <= 135 },
    { id: "long", label: "Over 2h 15m", test: m => m > 135 },
  ];

  let data = { movies: [], state: {} };
  let view = "genre", len = "", genre = "", sortKey = "title", sortDir = 1;
  let visible = false, timer = null, allGenres = false;

  const store = (k, v) => { try { localStorage.setItem("adbfm.movies." + k, v); } catch (_) {} };
  const recall = k => { try { return localStorage.getItem("adbfm.movies." + k); } catch (_) { return null; } };

  el.side.value = recall("side") || "device";
  el.folder.value = recall("folder." + el.side.value) || DEFAULT[el.side.value];
  view = recall("view") || "genre";

  const fmtLen = m => m ? (m >= 60 ? `${Math.floor(m / 60)}h ${String(m % 60).padStart(2, "0")}m` : `${m}m`) : "—";
  const name = m => m.match || m.fixTitle || m.title;
  const year = m => m.matchYear || m.fixYear || m.year || "";

  // movies from the chosen side and folder (the cache holds every folder scanned)
  function inFolder() {
    const side = el.side.value;
    let f = el.folder.value.trim().replace(/\/+$/, "");
    if (side === "local" && f.startsWith("~") && data.home) f = data.home + f.slice(1);
    return data.movies.filter(m => m.side === side && (!f || m.path.startsWith(f + "/")));
  }

  function filtered(all) {
    const q = el.search.value.trim().toLowerCase();
    const lt = LENGTHS.find(l => l.id === len);
    return all.filter(m => m.status === "found"
      && (!q || (name(m) + " " + m.file).toLowerCase().includes(q))
      && (!lt.test || (m.minutes && lt.test(m.minutes)))
      && (!genre || m.genres.includes(genre)));
  }

  function chips(host, items, cur, onPick) {
    host.innerHTML = items.map(it =>
      `<button class="fpill${it.id === cur ? " on" : ""}" data-id="${esc(it.id)}">${esc(it.label)}</button>`).join("");
    host.querySelectorAll(".fpill").forEach(b => { b.onclick = () => onPick(b.dataset.id); });
  }

  function movieRow(m) {
    return `<tr>
      <td class="mt" title="${esc(m.path)}${m.label && m.label !== name(m) ? "\nWikidata: " + esc(m.label) : ""}">${esc(name(m))}</td>
      <td class="my">${esc(String(year(m)))}</td>
      <td class="ml">${fmtLen(m.minutes)}</td>
      <td class="mg">${m.genres.map(g => `<span class="gTag${g === genre ? " on" : ""}">${esc(g)}</span>`).join("")}</td>
    </tr>`;
  }

  function table(list) {
    const arrow = k => sortKey === k ? (sortDir > 0 ? " ▲" : " ▼") : "";
    const cmp = {
      title: (a, b) => name(a).localeCompare(name(b), undefined, { numeric: true, sensitivity: "base" }),
      year: (a, b) => (year(a) || 0) - (year(b) || 0),
      len: (a, b) => (a.minutes || 0) - (b.minutes || 0),
    }[sortKey];
    const rows = [...list].sort((a, b) => cmp(a, b) * sortDir);
    return `<table class="mTable">
      <thead><tr>
        <th data-s="title">Title${arrow("title")}</th>
        <th data-s="year" style="width:60px">Year${arrow("year")}</th>
        <th data-s="len" style="width:80px">Length${arrow("len")}</th>
        <th>Genres</th>
      </tr></thead><tbody>${rows.map(movieRow).join("")}</tbody></table>`;
  }

  function render() {
    const all = inFolder();
    const found = all.filter(m => m.status === "found");
    const missing = all.filter(m => m.status !== "found");

    // genre chips: counted within the current length/search filter
    const counts = {};
    const g0 = genre;
    genre = "";
    filtered(all).forEach(m => m.genres.forEach(g => { counts[g] = (counts[g] || 0) + 1; }));
    genre = g0;
    const gs = Object.keys(counts).sort((a, b) => counts[b] - counts[a] || a.localeCompare(b));
    // one-movie genres ('Ninja', 'White savior') stay folded unless asked for
    const shown = allGenres ? gs : gs.filter(g => counts[g] >= 2 || g === genre);
    const more = gs.length - shown.length;
    chips(el.genres, [
      { id: "", label: "All genres" },
      ...shown.map(g => ({ id: g, label: `${g} ${counts[g]}` })),
      ...(more ? [{ id: "\u0000more", label: `+${more} more` }] : []),
      ...(allGenres ? [{ id: "\u0000less", label: "Fewer" }] : []),
    ], genre, id => {
      if (id === "\u0000more" || id === "\u0000less") allGenres = id === "\u0000more";
      else genre = id;
      render();
    });
    chips(el.lens, LENGTHS, len, id => { len = id; render(); });
    chips(el.views, [{ id: "genre", label: "By genre" }, { id: "list", label: "List" }], view,
      id => { view = id; store("view", id); render(); });

    const list = filtered(all);
    let html = "";
    if (!all.length) {
      html = `<div class="empty">No movies scanned in this folder yet. Press <b>Update missing genres</b>.</div>`;
    } else if (!list.length) {
      html = `<div class="empty">No movies match these filters.</div>`;
    } else if (view === "list" || genre) {
      html = table(list);
    } else {
      // a movie appears under each of its genres; rare genres stay in the chips
      const groups = gs.filter(g => counts[g] >= 2);
      const rare = gs.length - groups.length;
      html = groups.map(g => {
        const ms = list.filter(m => m.genres.includes(g));
        return `<section class="gSec"><h3>${esc(g)} <span class="meta">${ms.length}</span></h3>${table(ms)}</section>`;
      }).join("");
      const none = list.filter(m => !m.genres.length);
      if (none.length) html += `<section class="gSec"><h3>No genre listed <span class="meta">${none.length}</span></h3>${table(none)}</section>`;
      if (rare) html += `<div class="meta" style="padding:4px 2px">${rare} genre${rare === 1 ? " has" : "s have"} a single movie; find them under “+ more” above.</div>`;
    }
    if (missing.length) {
      html += `<section class="gSec miss"><h3>Not matched <span class="meta">${missing.length} — fix with the right title and year</span></h3>
        <table class="mTable"><tbody>${missing.map(m => `<tr>
          <td class="mt" title="${esc(m.path)}">${esc(m.file)}</td>
          <td class="mg meta">${m.status === "error" ? "lookup failed: " + esc(m.error || "") : m.status === "new" ? "not looked up yet" : `nothing found for “${esc(m.fixTitle || m.title)}${(m.fixYear || m.year) ? " " + (m.fixYear || m.year) : ""}”`}</td>
          <td style="width:60px"><button data-fix="${esc(m.key)}">Fix</button></td></tr>`).join("")}</tbody></table></section>`;
    }
    if (found.length) {
      html += `<div class="meta" style="padding:10px 2px">Genres and running times from Wikidata.
        Wrong movie? Use “Not matched” fixes, or rename the file to “Title Year”.</div>`;
    }
    el.body.innerHTML = html;
    el.body.querySelectorAll("th[data-s]").forEach(th => {
      th.onclick = () => {
        sortDir = sortKey === th.dataset.s ? -sortDir : 1;
        sortKey = th.dataset.s; render();
      };
    });
    el.body.querySelectorAll("[data-fix]").forEach(b => { b.onclick = () => fix(b.dataset.fix); });
    renderStatus(all.length, missing.length);
  }

  function renderStatus(total, missing) {
    const s = data.state || {};
    if (s.running) {
      el.status.textContent = `Looking up ${s.done + 1} of ${s.total}: ${s.current || "…"}`;
    } else if (s.error) {
      el.status.textContent = "Stopped: " + s.error;
    } else {
      el.status.textContent = total ? `${total - missing} of ${total} movies have genres` : "";
    }
    el.update.disabled = !!s.running;
  }

  async function load() {
    data = await api("/api/movies");
    if (visible) render();
    clearTimeout(timer);
    if (visible && data.state && data.state.running) timer = setTimeout(load, 1000);
  }

  async function update() {
    const side = el.side.value;
    if (side === "device" && !currentSerial()) { el.status.textContent = "No phone connected."; return; }
    el.update.disabled = true;
    el.status.textContent = "Scanning folder…";
    const d = await api("/api/movies/update", {
      method: "POST",
      body: JSON.stringify({ side, serial: currentSerial(), folder: el.folder.value.trim() }),
    });
    if (d.error) { el.status.textContent = d.error; el.update.disabled = false; return; }
    if (side === "local" && d.folder) {
        el.folder.value = d.folder; store("folder.local", d.folder);
    }
    await load();
    if (!d.todo) el.status.textContent = `${d.scanned} movies scanned — every one already has genres.`;
  }

  async function fix(key) {
    const m = data.movies.find(x => x.key === key);
    const guess = `${m.fixTitle || m.title}${(m.fixYear || m.year) ? " " + (m.fixYear || m.year) : ""}`;
    const ans = prompt(`Title and year for\n${m.file}`, guess);
    if (!ans) return;
    const mm = ans.trim().match(/^(.*?)\s*\(?((?:19|20)\d{2})?\)?$/);
    el.status.textContent = "Looking up " + mm[1] + "…";
    const d = await api("/api/movies/fix", {
      method: "POST", body: JSON.stringify({ key, title: mm[1], year: mm[2] || null }),
    });
    if (d.error) alert(d.error);
    await load();
  }

  el.update.onclick = update;
  el.search.oninput = () => render();
  el.side.onchange = () => {
    store("side", el.side.value);
    el.folder.value = recall("folder." + el.side.value) || DEFAULT[el.side.value];
    genre = ""; render();
  };
  el.folder.onchange = () => { store("folder." + el.side.value, el.folder.value.trim()); render(); };

  return {
    show() { visible = true; load(); },
    hide() { visible = false; clearTimeout(timer); },
  };
})();
