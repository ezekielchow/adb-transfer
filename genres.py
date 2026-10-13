"""Movie genres and running time, looked up on Wikidata (no API key needed).

Results are cached in ~/.config/adb-transfer/movies.json, keyed by side and
full path, so each file is looked up once. "Update" only asks about files that
have no genres yet; a wrong match can be fixed by giving the title and year.
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

CONF_DIR = os.path.expanduser("~/.config/adb-transfer")
CACHE = os.path.join(CONF_DIR, "movies.json")
API = "https://www.wikidata.org/w/api.php"
UA = "adb-transfer/1.0 (local movie genre lookup)"
VIDEO_EXT = (".mkv", ".mp4", ".avi", ".mov", ".m4v", ".webm", ".wmv",
             ".ts", ".m2ts", ".mpg", ".mpeg", ".flv", ".3gp")
UNIT_MIN = {"Q7727": 1, "Q11574": 1 / 60, "Q25235": 60}  # min, s, h

LOCK = threading.Lock()
STATE = {"running": False, "done": 0, "total": 0, "current": "",
         "found": 0, "missed": 0, "error": ""}

# --------------------------------------------------------------------- cache


def _load():
    try:
        with open(CACHE) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    d.setdefault("movies", {})
    d.setdefault("genreLabels", {})
    return d


def _save(d):
    os.makedirs(CONF_DIR, exist_ok=True)
    tmp = CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, indent=1, ensure_ascii=False)
    os.replace(tmp, CACHE)


DB = _load()


def key(side, path):
    return f"{side}:{path}"


def listing():
    with LOCK:
        return {"movies": list(DB["movies"].values()), "state": dict(STATE),
                "home": os.path.expanduser("~")}


# ------------------------------------------------------------ filename parse

YEAR_RE = re.compile(r"(?<![\d])((?:19|20)\d{2})(?![\dpPkK])")
JUNK_RE = re.compile(r"[\[(][^\])]*[\])]")


def parse_name(filename):
    """'Nobody.2.2025.1080p.WEB-DL...mkv' -> ('Nobody 2', 2025).

    The title is everything before the first plausible year; release junk
    after it (resolution, codec, audio, site) is dropped. Without a year,
    bracketed notes are removed and the rest is the title.
    """
    base = os.path.splitext(filename)[0]
    base = re.sub(r"[._]+", " ", base)
    year = None
    for m in YEAR_RE.finditer(base):
        head = base[:m.start()]
        if head.strip(" -([") and int(m.group(1)) <= time.localtime().tm_year + 1:
            year = int(m.group(1))
            base = head
            break
    title = JUNK_RE.sub(" ", base)
    title = re.sub(r"\S*movies\S*", " ", title, flags=re.I)   # 'VegamoviesHD Nope'
    title = title.replace(" - ", " ")
    title = re.split(r"\b(?:1080p|2160p|720p|480p|4k|uhd|bluray|web-?dl|hdrip|x26[45]|hevc)\b",
                     title, flags=re.I)[0]
    title = re.sub(r"\s+", " ", title).strip(" -([")
    return title, year


# ------------------------------------------------------------------ wikidata

_last = [0.0]


def _get(params):
    """GET the Wikidata API, at most ~1 request/second, retrying on 429."""
    url = API + "?" + urllib.parse.urlencode({**params, "format": "json"})
    for attempt in range(5):
        wait = 1.0 - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 503) or attempt == 4:
                raise
            try:
                delay = float(e.headers.get("Retry-After") or 0)
            except ValueError:
                delay = 0
            time.sleep(max(delay, 3 * (attempt + 1)))
    raise RuntimeError("unreachable")


def _label(ent):
    labels = ent.get("labels") or {}
    for lang in ("en", "mul"):
        if lang in labels:
            return labels[lang]["value"]
    return ""


def _values(claims, prop):
    for c in claims.get(prop, []):
        snak = c.get("mainsnak", {})
        if "datavalue" in snak:
            yield snak["datavalue"]["value"]


def _years(claims):
    return {int(v["time"][1:5]) for v in _values(claims, "P577")}


def _minutes(claims):
    for v in _values(claims, "P2047"):
        unit = v.get("unit", "").rsplit("/", 1)[-1]
        if unit in UNIT_MIN:
            return round(float(v["amount"]) * UNIT_MIN[unit])
    return None


def _norm(s):
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def nice_genre(label):
    g = re.sub(r"\s+(film|movie)$", "", label.strip(), flags=re.I)
    return g[:1].upper() + g[1:]


def _entities(ids):
    if not ids:
        return []
    ents = _get({"action": "wbgetentities", "ids": "|".join(ids),
                 "props": "claims|labels", "languages": "en|mul"})["entities"]
    return [ents[i] for i in ids if i in ents]


def _choose(cands, title, year):
    """Pick the film from search candidates, in search order, or None."""
    # a film has a director; this drops 'Batman Begins (video game)', which
    # label search lets through as "…based on the film"
    cands = [e for e in cands if "P57" in e["claims"]]

    def best(pool):
        # an exact title with genres beats a near miss
        # ('Spirited Away' over 'Spirited Away: Live On Stage')
        same = [e for e in pool if _norm(_label(e)) == _norm(title)]
        for group in (same, pool):
            for e in group:
                if "P136" in e["claims"]:
                    return e
        return pool[0] if pool else None

    if not year:
        return best(cands)
    # exact release year first, then ±1 (festival vs. wide release)
    for slack in (0, 1):
        pick = best([e for e in cands
                     if any(abs(y - year) <= slack for y in _years(e["claims"]))])
        if pick:
            return pick
    return None


def lookup(title, year):
    """Best Wikidata film match for title/year, or None."""
    if not title:
        return None
    # Two searches, because each misses films the other finds: label search
    # ranks exact titles well ('Coco' -> Pixar's), but returns everything
    # called that, so keep only items described as a film; full-text search
    # limited to items with a director and a genre catches the rest.
    # The full-text search is the one Wikidata throttles hardest, so it only
    # runs when label search found no film with the right year.
    r = _get({"action": "wbsearchentities", "search": title, "language": "en",
              "type": "item", "limit": 50})
    ids = [x["id"] for x in r.get("search", [])
           if re.search(r"\b(film|movie)\b", x.get("description", ""), re.I)][:50]
    pick = _choose(_entities(ids), title, year)
    if not pick:
        r = _get({"action": "query", "list": "search", "srlimit": 10,
                  "srsearch": f"{title} haswbstatement:P57 haswbstatement:P136"})
        more = [x["title"] for x in r.get("query", {}).get("search", [])
                if x["title"] not in ids]
        pick = _choose(_entities(more), title, year)
    if not pick:
        return None

    claims = pick["claims"]
    gids = [v["id"] for v in _values(claims, "P136")]
    with LOCK:
        unknown = [g for g in gids if g not in DB["genreLabels"]]
    if unknown:
        labels = _get({"action": "wbgetentities", "ids": "|".join(unknown[:50]),
                       "props": "labels", "languages": "en|mul"})["entities"]
        with LOCK:
            for g in unknown:
                DB["genreLabels"][g] = nice_genre(_label(labels.get(g, {})) or g)
    with LOCK:
        genres = sorted({DB["genreLabels"][g] for g in gids if g in DB["genreLabels"]})
    ys = _years(claims)
    label = _label(pick)
    # Prefer Wikidata's nicer title ('Mission: Impossible III'), unless it
    # does not look like the filename at all (labels do get vandalised).
    a, b = _norm(label), _norm(title)
    return {
        "qid": pick["id"],
        "label": label,
        "match": label if a and a in b else title,
        "matchYear": min(ys) if ys else None,
        "genres": genres,
        "minutes": _minutes(claims),
    }


# ------------------------------------------------------------------- update


def _resolve(entry):
    """Fill one cache entry in place from Wikidata."""
    title = entry.get("fixTitle") or entry["title"]
    year = entry.get("fixYear") or entry["year"]
    try:
        hit = lookup(title, year)
    except Exception as exc:  # network trouble: keep it "missing" for next time
        entry.update(status="error", error=str(exc))
        return False
    if hit:
        entry.update(status="found", error="", **hit)
        return True
    entry.update(status="notfound", error="", genres=[], minutes=None,
                 qid=None, match="")
    return False


def start_update(side, folder, files):
    """files: [{"path", "size"}] of every video in `folder` (recursive).

    Forgets cached files that are gone from the folder, adds new ones, then
    looks up in the background those without genres.
    """
    with LOCK:
        if STATE["running"]:
            return {"error": "an update is already running"}
        prefix = folder.rstrip("/") + "/"
        present = {key(side, f["path"]) for f in files}
        for k in [k for k, m in DB["movies"].items()
                  if m["side"] == side and m["path"].startswith(prefix) and k not in present]:
            del DB["movies"][k]
        todo = []
        for f in files:
            k = key(side, f["path"])
            m = DB["movies"].get(k)
            if not m:
                name = os.path.basename(f["path"])
                title, year = parse_name(name)
                m = DB["movies"][k] = {
                    "key": k, "side": side, "path": f["path"], "file": name,
                    "size": f.get("size", 0), "title": title, "year": year,
                    "status": "new", "genres": [], "minutes": None,
                }
            if m["status"] != "found":
                todo.append(k)
        STATE.update(running=bool(todo), done=0, total=len(todo), current="",
                     found=0, missed=0, error="")
        _save(DB)
    if todo:
        threading.Thread(target=_run, args=(todo,), daemon=True).start()
    return {"ok": True, "scanned": len(files), "todo": len(todo)}


def _run(todo):
    try:
        for k in todo:
            with LOCK:
                m = DB["movies"].get(k)
                if not m:
                    continue
                STATE["current"] = m.get("fixTitle") or m["title"]
                entry = dict(m)
            ok = _resolve(entry)
            with LOCK:
                if k in DB["movies"]:
                    DB["movies"][k] = entry
                STATE["done"] += 1
                STATE["found" if ok else "missed"] += 1
                _save(DB)
    except Exception as exc:
        with LOCK:
            STATE["error"] = repr(exc)
    finally:
        with LOCK:
            STATE.update(running=False, current="")


def fix(k, title, year):
    """Re-look-up one movie under a title/year the user typed."""
    with LOCK:
        m = DB["movies"].get(k)
        if not m:
            return {"error": "unknown movie"}
        entry = dict(m)
    entry["fixTitle"] = (title or "").strip() or None
    try:
        entry["fixYear"] = int(year) if year else None
    except ValueError:
        return {"error": "year must be a number"}
    ok = _resolve(entry)
    with LOCK:
        DB["movies"][k] = entry
        _save(DB)
    return {"ok": ok, "movie": entry,
            "error": "" if ok else (entry.get("error") or "no film found with that title and year")}


def scan_local(folder):
    folder = os.path.abspath(os.path.expanduser(folder))
    out = []
    for root, _dirs, names in os.walk(folder):
        for n in names:
            if n.lower().endswith(VIDEO_EXT):
                p = os.path.join(root, n)
                try:
                    out.append({"path": p, "size": os.path.getsize(p)})
                except OSError:
                    pass
    return folder, out
