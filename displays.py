"""Display control for the Displays tab: a thin, verified layer over xrandr.

Every layout change is checked against what the X server reports afterwards,
and is rolled back automatically unless the page confirms it in time -- so a
mode the monitor cannot show, or a screen switched off by mistake, fixes
itself instead of leaving you in the dark.
"""

import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time

XRANDR = shutil.which("xrandr") or "xrandr"
BUSCTL = shutil.which("busctl") or "busctl"

REVERT_AFTER = 15          # seconds to confirm a layout before it rolls back
ROTATIONS = ("normal", "left", "inverted", "right")
REFLECTIONS = ("normal", "x", "y", "xy")
MAX_POS = 16384

LAYOUTS_FILE = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "adb-transfer", "display-layouts.json",
)


def xrandr(*args, timeout=20):
    try:
        r = subprocess.run([XRANDR, *args], capture_output=True, text=True,
                           timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 1, "", "xrandr timed out"
    except FileNotFoundError:
        return 1, "", "xrandr not found on PATH"


# ------------------------------------------------------------------- parsing

SCREEN_RE = re.compile(
    r"^Screen \d+: minimum (\d+) x (\d+), current (\d+) x (\d+), "
    r"maximum (\d+) x (\d+)")
OUTPUT_RE = re.compile(
    r"^(\S+) (connected|disconnected|unknown connection)( primary)?"
    r"(?: (\d+)x(\d+)\+(-?\d+)\+(-?\d+))?"
    r"(?: \((0x[0-9a-f]+)\))?"
    r"(?: (normal|left|inverted|right))?"
    r"(?: (X axis|Y axis|X and Y axis|none))?"
    r" \(.*?\)(?: (\d+)mm x (\d+)mm)?")
MODE_RE = re.compile(r"^  (\S+) \((0x[0-9a-f]+)\)\s+[\d.]+MHz(.*)$")
H_RE = re.compile(r"^\s+h: width\s+(\d+)")
V_RE = re.compile(r"^\s+v: height\s+(\d+).*clock\s+([\d.]+)Hz")
PROP_RE = re.compile(r"^\t([^\t :][^:]*):\s?(.*)$")
FLOAT_RE = re.compile(r"-?\d+\.\d+")

REFLECT_WORDS = {None: "normal", "none": "normal", "X axis": "x",
                 "Y axis": "y", "X and Y axis": "xy"}


def edid_name(hexstr):
    """Monitor name from EDID descriptor 0xFC, else the 3-letter vendor id."""
    try:
        b = bytes.fromhex(hexstr)
    except ValueError:
        return ""
    if len(b) < 128:
        return ""
    for off in (54, 72, 90, 108):
        d = b[off:off + 18]
        if d[0:3] == b"\0\0\0" and d[3] == 0xFC:
            return d[5:].split(b"\n")[0].decode("ascii", "replace").strip()
    v = (b[8] << 8) | b[9]
    return "".join(chr(((v >> s) & 0x1F) + 64) for s in (10, 5, 0))


def parse_verbose(text):
    screen, outputs = {}, []
    out = mode = None
    prop = None
    props = {}

    def finish():
        if out is None:
            return
        tf = [float(x) for x in FLOAT_RE.findall(props.get("Transform", ""))]
        if len(tf) >= 9:
            out["scaleX"], out["scaleY"] = round(tf[0], 4), round(tf[4], 4)
        try:
            out["brightness"] = float(props.get("Brightness", "1").split()[0])
        except ValueError:
            pass
        edid = re.sub(r"\s+", "", props.get("EDID", ""))
        out["monitor"] = edid_name(edid) if edid else ""
        cur = next((m for m in out["modes"] if m["id"] == out["modeId"]), None)
        if cur:
            out["rate"] = cur["rate"]
        outputs.append(out)

    for line in text.splitlines():
        m = SCREEN_RE.match(line)
        if m:
            v = list(map(int, m.groups()))
            screen = {"minW": v[0], "minH": v[1], "w": v[2], "h": v[3],
                      "maxW": v[4], "maxH": v[5]}
            continue
        m = OUTPUT_RE.match(line)
        if m:
            finish()
            g = m.groups()
            enabled = g[3] is not None
            out = {
                "name": g[0], "connected": g[1] == "connected",
                "primary": bool(g[2]), "enabled": enabled,
                "x": int(g[5]) if enabled else 0,
                "y": int(g[6]) if enabled else 0,
                "w": int(g[3]) if enabled else 0,
                "h": int(g[4]) if enabled else 0,
                "modeId": g[7] if enabled else None,
                "rotation": (g[8] or "normal") if enabled else "normal",
                "reflect": REFLECT_WORDS.get(g[9], "normal"),
                "mmW": int(g[10]) if g[10] else 0,
                "mmH": int(g[11]) if g[11] else 0,
                "scaleX": 1.0, "scaleY": 1.0, "brightness": 1.0,
                "rate": None, "modes": [],
            }
            props, prop, mode = {}, None, None
            continue
        if out is None:
            continue
        m = MODE_RE.match(line)
        if m:
            flags = m.group(3)
            mode = {"id": m.group(2), "name": m.group(1), "w": 0, "h": 0,
                    "rate": 0.0, "current": "*current" in flags,
                    "preferred": "+preferred" in flags,
                    "interlace": "Interlace" in flags}
            out["modes"].append(mode)
            prop = None
            continue
        if mode is not None:
            m = H_RE.match(line)
            if m:
                mode["w"] = int(m.group(1))
                continue
            m = V_RE.match(line)
            if m:
                mode["h"], mode["rate"] = int(m.group(1)), float(m.group(2))
                continue
        m = PROP_RE.match(line)
        if m:
            prop = m.group(1).strip()
            props[prop] = m.group(2)
            continue
        if prop and (line.startswith("\t\t") or line.startswith("\t ")):
            props[prop] += " " + line.strip()
    finish()
    return screen, outputs


def query():
    code, out, err = xrandr("--verbose")
    if code != 0:
        return None, err.strip() or "could not run xrandr"
    screen, outputs = parse_verbose(out)
    if not outputs:
        return None, "xrandr reported no outputs (is this an X11 session?)"
    return {"screen": screen, "outputs": outputs}, None


# ------------------------------------------------------------------ backlight


def backlights():
    """{output name: sysfs dir} for panels whose backlight we can drive."""
    found = {}
    for d in sorted(glob.glob("/sys/class/backlight/*")):
        m = re.search(r"/card\d+-([^/]+)/", os.path.realpath(d) + "/")
        found[m.group(1) if m else ""] = d
    return found


def backlight_for(output, outputs):
    bl = backlights()
    if output in bl:
        return bl[output]
    # Some drivers name outputs differently from DRM (eDP1 vs eDP-1).
    panels = [o["name"] for o in outputs
              if re.match(r"(eDP|LVDS|DSI)", o["name"])]
    if len(bl) == 1 and panels == [output]:
        return next(iter(bl.values()))
    return None


def backlight_info(d):
    try:
        with open(os.path.join(d, "max_brightness")) as f:
            mx = int(f.read())
        with open(os.path.join(d, "actual_brightness")) as f:
            cur = int(f.read())
        return {"device": os.path.basename(d), "max": mx, "value": cur,
                "percent": round(cur * 100 / mx) if mx else 0}
    except (OSError, ValueError):
        return None


def set_backlight(output, percent):
    data, err = query()
    if err:
        return {"error": err}
    d = backlight_for(output, data["outputs"])
    info = backlight_info(d) if d else None
    if not info:
        return {"error": f"{output} has no backlight this app can control."}
    try:
        percent = float(percent)
    except (TypeError, ValueError):
        return {"error": "Brightness must be a number."}
    # Never all the way to 0: on many panels that is indistinguishable from off.
    percent = max(1.0, min(100.0, percent))
    value = max(1, round(info["max"] * percent / 100))
    # logind lets the logged-in user set the backlight without root.
    try:
        r = subprocess.run(
            [BUSCTL, "call", "org.freedesktop.login1",
             "/org/freedesktop/login1/session/auto",
             "org.freedesktop.login1.Session", "SetBrightness", "ssu",
             "backlight", info["device"], str(value)],
            capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"error": f"could not reach logind: {exc}"}
    if r.returncode != 0:
        return {"error": r.stderr.strip() or "logind refused the change"}
    after = backlight_info(d)
    if not after or abs(after["value"] - value) > max(2, info["max"] // 100):
        return {"error": "The backlight did not change (another program may "
                         "be managing it).", "backlight": after}
    return {"ok": True, "backlight": after}


def set_gamma_brightness(output, value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return {"error": "Brightness must be a number."}
    # Below ~0.2 the screen is unreadable, which is how people lose a monitor.
    if not 0.2 <= value <= 1.5:
        return {"error": "Brightness must be between 0.2 and 1.5."}
    data, err = query()
    if err:
        return {"error": err}
    o = next((o for o in data["outputs"] if o["name"] == output), None)
    if not o or not o["enabled"]:
        return {"error": f"{output} is not switched on."}
    code, out, e = xrandr("--output", output, "--brightness", f"{value:.2f}")
    if code != 0:
        return {"error": e.strip() or out.strip() or "xrandr failed"}
    data, err = query()
    o = next((o for o in (data or {}).get("outputs", [])
              if o["name"] == output), None)
    if not o or abs(o["brightness"] - value) > 0.02:
        return {"error": "The brightness did not change."}
    return {"ok": True, "brightness": o["brightness"]}


# ------------------------------------------------------------ apply & revert

LOCK = threading.Lock()
PENDING = {}   # {"deadline", "revert": [args], "timer", "summary"}


def snapshot_args(outputs):
    """xrandr arguments that recreate exactly the given state."""
    args = []
    # Switch outputs off first so their CRTCs are free for the others.
    for o in outputs:
        # modeId is set on an output that was on until now, including one
        # whose cable was pulled but still holds part of the desktop.
        if not o["enabled"] and (o["connected"] or o.get("modeId")):
            args += ["--output", o["name"], "--off"]
    for o in outputs:
        if not o["enabled"]:
            continue
        args += ["--output", o["name"], "--mode", o["modeId"],
                 "--pos", f"{o['x']}x{o['y']}",
                 "--rotate", o["rotation"], "--reflect", o["reflect"],
                 "--scale", f"{o['scaleX']:g}x{o['scaleY']:g}"]
        if o["primary"]:
            args.append("--primary")
    if not any(o["primary"] and o["enabled"] for o in outputs):
        args.append("--noprimary")
    return args


def logical_size(mode, rotation, sx, sy):
    w, h = mode["w"], mode["h"]
    if rotation in ("left", "right"):
        w, h = h, w
    return round(w * sx), round(h * sy)


def validate(want, current):
    """Resolve a requested layout against what is actually plugged in.

    Returns (resolved outputs, None) or (None, error message).
    """
    by_name = {o["name"]: o for o in current["outputs"]}
    screen = current["screen"]
    resolved, seen = [], set()
    for w in want:
        name = w.get("name")
        cur = by_name.get(name)
        if not cur:
            return None, f"There is no output called {name!r}."
        if name in seen:
            return None, f"{name} appears twice."
        seen.add(name)
        if not w.get("enabled"):
            resolved.append(cur | {"enabled": False, "primary": False})
            continue
        if not cur["connected"]:
            return None, f"{name} is not plugged in."
        mode = next((m for m in cur["modes"] if m["id"] == w.get("modeId")),
                    None)
        if not mode:
            return None, f"{name} does not support that resolution."
        rotation = w.get("rotation") or "normal"
        reflect = w.get("reflect") or "normal"
        if rotation not in ROTATIONS or reflect not in REFLECTIONS:
            return None, f"Invalid rotation for {name}."
        try:
            sx = float(w.get("scaleX", 1))
            sy = float(w.get("scaleY", sx))
            x, y = int(w.get("x", 0)), int(w.get("y", 0))
        except (TypeError, ValueError):
            return None, f"Invalid position or scale for {name}."
        if not (0.25 <= sx <= 4 and 0.25 <= sy <= 4):
            return None, f"Scale for {name} must be between 0.25 and 4."
        if not (0 <= x < MAX_POS and 0 <= y < MAX_POS):
            return None, f"Position for {name} is off the screen."
        lw, lh = logical_size(mode, rotation, sx, sy)
        resolved.append(cur | {
            "enabled": True, "modeId": mode["id"], "x": x, "y": y,
            "w": lw, "h": lh, "rotation": rotation, "reflect": reflect,
            "scaleX": sx, "scaleY": sy, "primary": bool(w.get("primary")),
            "rate": mode["rate"],
        })
    # Outputs the request did not mention keep their current state.
    for o in current["outputs"]:
        if o["name"] not in seen:
            resolved.append(o)

    on = [o for o in resolved if o["enabled"] and o["connected"]]
    if not on:
        return None, "That would switch off every screen."
    if sum(1 for o in resolved if o["enabled"] and o["primary"]) > 1:
        return None, "Only one screen can be primary."
    fw = max(o["x"] + o["w"] for o in on)
    fh = max(o["y"] + o["h"] for o in on)
    if screen and (fw > screen["maxW"] or fh > screen["maxH"]):
        return None, (f"The combined desktop would be {fw}×{fh}, larger "
                      f"than this GPU allows ({screen['maxW']}×"
                      f"{screen['maxH']}).")
    return resolved, None


def describe_mismatch(want, got):
    by_name = {o["name"]: o for o in got}
    problems = []
    for w in want:
        g = by_name.get(w["name"])
        if not g or not w["connected"]:
            continue
        if w["enabled"] != g["enabled"]:
            problems.append(f"{w['name']} is still "
                            f"{'on' if g['enabled'] else 'off'}.")
            continue
        if not w["enabled"]:
            continue
        if w["modeId"] != g["modeId"]:
            problems.append(f"{w['name']} is running a different mode.")
        if (w["x"], w["y"]) != (g["x"], g["y"]):
            problems.append(f"{w['name']} ended up at {g['x']},{g['y']} "
                            f"instead of {w['x']},{w['y']}.")
        if w["rotation"] != g["rotation"] or w["reflect"] != g["reflect"]:
            problems.append(f"{w['name']} has a different rotation.")
        if abs(w["scaleX"] - g["scaleX"]) > 0.01 or \
                abs(w["scaleY"] - g["scaleY"]) > 0.01:
            problems.append(f"{w['name']} has a different scale.")
        if w["primary"] != g["primary"]:
            problems.append(f"{w['name']} primary setting did not stick.")
    return problems


def _revert_now(reason):
    """Caller holds LOCK."""
    p = PENDING.copy()
    PENDING.clear()
    if not p:
        return {"error": "Nothing to revert."}
    if p.get("timer"):
        p["timer"].cancel()
    code, out, err = xrandr(*p["revert"])
    return {"ok": code == 0, "reverted": reason,
            **({} if code == 0 else
               {"error": err.strip() or out.strip() or "revert failed"})}


def _timer_fired(token):
    with LOCK:
        if PENDING.get("token") == token:
            LAST_AUTO_REVERT.update(at=time.time(), result=_revert_now("timeout"))


LAST_AUTO_REVERT = {}


def _commit(current, resolved, needs_confirm):
    """Run xrandr for a validated layout and check the result. Holds LOCK."""
    revert = snapshot_args(current["outputs"])
    code, out, e = xrandr(*snapshot_args(resolved))
    if code != 0:
        # A failed transaction can still leave some CRTCs changed.
        xrandr(*revert)
        return {"error": (e.strip() or out.strip() or "xrandr failed")
                + " — your previous layout was restored."}

    time.sleep(0.4)   # let the window manager react before re-reading
    after, _ = query()
    problems = describe_mismatch(resolved, after["outputs"]) if after \
        else ["Could not re-read the display state."]

    if not needs_confirm:
        return {"ok": True, "problems": problems}
    token = os.urandom(8).hex()
    t = threading.Timer(REVERT_AFTER, _timer_fired, args=(token,))
    t.daemon = True
    PENDING.update(token=token, revert=revert, timer=t,
                   deadline=time.time() + REVERT_AFTER)
    LAST_AUTO_REVERT.clear()
    t.start()
    return {"ok": True, "problems": problems, "pending": pending_public()}


def apply(want):
    """Change resolution/position/rotation/scale; reverts unless kept."""
    with LOCK:
        if PENDING:
            return {"error": "Keep or revert the previous change first."}
        current, err = query()
        if err:
            return {"error": err}
        resolved, err = validate(want, current)
        if err:
            return {"error": err}
        return _commit(current, resolved, needs_confirm=True)


# ------------------------------------------------------------- on / off

LAST_ON_FILE = os.path.join(os.path.dirname(LAYOUTS_FILE), "display-last.json")


def _load_last_on():
    try:
        with open(LAST_ON_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _remember(o):
    """Store how an output was set up, so switching it back on restores it."""
    mode = next((m for m in o["modes"] if m["id"] == o["modeId"]), None)
    if not mode:
        return
    data = _load_last_on()
    data[o["name"]] = {
        "monitor": o.get("monitor", ""), "width": mode["w"],
        "height": mode["h"], "rate": mode["rate"], "x": o["x"], "y": o["y"],
        "rotation": o["rotation"], "reflect": o["reflect"],
        "scaleX": o["scaleX"], "scaleY": o["scaleY"],
    }
    try:
        os.makedirs(os.path.dirname(LAST_ON_FILE), exist_ok=True)
        tmp = LAST_ON_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, LAST_ON_FILE)
    except OSError:
        pass


def _overlaps(a, b):
    return (a["x"] < b["x"] + b["w"] and b["x"] < a["x"] + a["w"] and
            a["y"] < b["y"] + b["h"] and b["y"] < a["y"] + a["h"])


def _as_request(o):
    return {k: o[k] for k in ("name", "enabled", "modeId", "x", "y",
                              "rotation", "reflect", "scaleX", "scaleY",
                              "primary")}


def set_power(name, on):
    """Switch one output on or off straight away.

    No confirmation countdown: turning a screen off can never leave you
    without one (the last screen is refused), and turning one on uses the
    monitor's own preferred mode or the setup it had last time.
    """
    with LOCK:
        if PENDING:
            return {"error": "Keep or revert the layout change first."}
        current, err = query()
        if err:
            return {"error": err}
        outs = current["outputs"]
        target = next((o for o in outs if o["name"] == name), None)
        if not target:
            return {"error": f"There is no output called {name!r}."}
        others = [o for o in outs
                  if o["name"] != name and o["enabled"] and o["connected"]]

        if not on:
            if not target["enabled"]:
                return {"ok": True, "problems": []}
            if not others:
                return {"error": f"{name} is the only screen that is on. "
                                 f"Switch another one on first."}
            if target["connected"]:
                _remember(target)
            want = [_as_request(o) for o in others]
            if target["primary"]:
                # Hand "primary" (panel, taskbar) to the biggest remaining screen.
                big = max(others, key=lambda o: o["w"] * o["h"])
                for w in want:
                    w["primary"] = w["name"] == big["name"]
            want.append({"name": name, "enabled": False})
        else:
            if target["enabled"]:
                return {"ok": True, "problems": []}
            if not target["connected"]:
                return {"error": f"{name} is not plugged in."}
            if not target["modes"]:
                return {"error": f"{name} did not report any modes."}
            req = _turn_on_request(target, others)
            want = [_as_request(o) for o in others] + [req]

        resolved, err = validate(want, current)
        if err:
            return {"error": err}
        return _commit(current, resolved, needs_confirm=False)


def _turn_on_request(target, others):
    last = _load_last_on().get(target["name"])
    if last and last.get("monitor", "") != target.get("monitor", ""):
        last = None   # a different monitor is on this port now
    mode = None
    if last:
        same = [m for m in target["modes"]
                if (m["w"], m["h"]) == (last["width"], last["height"])]
        if same:
            mode = min(same, key=lambda m: abs(m["rate"] - last["rate"]))
    if mode:
        req = {"name": target["name"], "enabled": True, "modeId": mode["id"],
               "x": last["x"], "y": last["y"], "rotation": last["rotation"],
               "reflect": last["reflect"], "scaleX": last["scaleX"],
               "scaleY": last["scaleY"], "primary": False}
    else:
        mode = next((m for m in target["modes"] if m["preferred"]),
                    target["modes"][0])
        req = {"name": target["name"], "enabled": True, "modeId": mode["id"],
               "x": 0, "y": 0, "rotation": "normal", "reflect": "normal",
               "scaleX": 1.0, "scaleY": 1.0, "primary": False}
    w, h = logical_size(mode, req["rotation"], req["scaleX"], req["scaleY"])
    box = {"x": req["x"], "y": req["y"], "w": w, "h": h}
    if any(_overlaps(box, o) for o in others) or \
            (not last and others):
        # Nowhere remembered (or it now collides): extend to the right.
        req["x"] = max((o["x"] + o["w"] for o in others), default=0)
        req["y"] = 0
    if not others:
        req.update(x=0, y=0, primary=True)
    return req


def pending_public():
    if not PENDING:
        return None
    return {"secondsLeft": max(0, round(PENDING["deadline"] - time.time(), 1)),
            "total": REVERT_AFTER}


def keep():
    with LOCK:
        if not PENDING:
            if LAST_AUTO_REVERT:
                return {"error": "Too late — the layout was already "
                                 "reverted."}
            return {"ok": True}
        PENDING["timer"].cancel()
        PENDING.clear()
        return {"ok": True}


def revert():
    with LOCK:
        return _revert_now("manual")


def state():
    data, err = query()
    if err:
        return {"error": err}
    for o in data["outputs"]:
        d = backlight_for(o["name"], data["outputs"])
        o["backlight"] = backlight_info(d) if d else None
    with LOCK:
        data["pending"] = pending_public()
        if LAST_AUTO_REVERT and time.time() - LAST_AUTO_REVERT["at"] < 30:
            data["autoReverted"] = LAST_AUTO_REVERT["result"]
    data["layouts"] = load_layouts()
    return data


# ------------------------------------------------------------ saved layouts

LAYOUT_KEYS = ("name", "enabled", "x", "y", "rotation", "reflect",
               "scaleX", "scaleY", "primary")


def load_layouts():
    try:
        with open(LAYOUTS_FILE) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_layouts(data):
    os.makedirs(os.path.dirname(LAYOUTS_FILE), exist_ok=True)
    tmp = LAYOUTS_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, LAYOUTS_FILE)


def save_layout(title, want):
    title = (title or "").strip()
    if not title or len(title) > 60:
        return {"error": "Give the layout a name (up to 60 characters)."}
    current, err = query()
    if err:
        return {"error": err}
    resolved, err = validate(want, current)
    if err:
        return {"error": err}
    outs = []
    for o in resolved:
        if not o["connected"]:
            continue
        item = {k: o[k] for k in LAYOUT_KEYS}
        if o["enabled"]:
            # Mode ids change between sessions; store what the mode *is*.
            mode = next(m for m in o["modes"] if m["id"] == o["modeId"])
            item.update(width=mode["w"], height=mode["h"], rate=mode["rate"])
        item["monitor"] = o.get("monitor", "")
        outs.append(item)
    data = load_layouts()
    data[title] = {"outputs": outs, "saved": time.strftime("%Y-%m-%d %H:%M")}
    try:
        _write_layouts(data)
    except OSError as exc:
        return {"error": f"Could not save: {exc.strerror or exc}"}
    return {"ok": True}


def delete_layout(title):
    data = load_layouts()
    if title not in data:
        return {"error": "No such layout."}
    del data[title]
    try:
        _write_layouts(data)
    except OSError as exc:
        return {"error": f"Could not save: {exc.strerror or exc}"}
    return {"ok": True}


def resolve_layout(title):
    """Turn a saved layout back into an apply() request for today's hardware."""
    saved = load_layouts().get(title)
    if not saved:
        return None, "No such layout."
    current, err = query()
    if err:
        return None, err
    by_name = {o["name"]: o for o in current["outputs"]}
    want = []
    for s in saved["outputs"]:
        cur = by_name.get(s["name"])
        if not s["enabled"]:
            if cur and cur["connected"]:
                want.append({"name": s["name"], "enabled": False})
            continue
        if not cur or not cur["connected"]:
            return None, (f"This layout needs {s['name']}"
                          f"{' (' + s['monitor'] + ')' if s.get('monitor') else ''}"
                          f", which is not plugged in.")
        modes = [m for m in cur["modes"]
                 if (m["w"], m["h"]) == (s["width"], s["height"])]
        if not modes:
            return None, (f"{s['name']} no longer offers "
                          f"{s['width']}×{s['height']}.")
        mode = min(modes, key=lambda m: abs(m["rate"] - s["rate"]))
        want.append({k: s[k] for k in LAYOUT_KEYS} | {"modeId": mode["id"]})
    return want, None
