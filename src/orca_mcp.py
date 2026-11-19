r"""
Orca MCP - slice with YOUR OrcaSlicer and YOUR user presets from Claude (or any MCP client).

- Uses the OrcaSlicer CLI that ships with OrcaSlicer (2.x). No modified build needed.
- Works with your *user* presets (the ones that inherit from system presets). Orca's CLI only
  resolves that inheritance for presets inside its data dir, so the server keeps a private
  working copy (system bundles + your user presets) and refreshes it before every slice.
  Your real OrcaSlicer folder is never written to.
- Never overwrites files: saved outputs get _2, _3... suffixes.

Configuration (environment variables, all optional):
  ORCA_MCP_ORCA_EXE        path to orca-slicer(.exe) / OrcaSlicer.app binary  (auto-detected)
  ORCA_MCP_ORCA_DATA       OrcaSlicer data dir                                 (auto-detected)
  ORCA_MCP_DEFAULT_PRINTER default printer preset name or part of it           (first user printer)
  ORCA_MCP_WORKDIR         private working dir                                 (per-user cache)
  ORCA_MCP_TIMEOUT_MIN     max minutes per model                               (30)
"""

import csv
import datetime as _dt
import fnmatch
import glob
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile

from mcp.server.fastmcp import FastMCP

__version__ = "1.2.0"
KINDS = ("machine", "process", "filament")


# ------------------------------------------------------------------ configuration
def _env(name, default=""):
    v = os.environ.get("ORCA_MCP_" + name, "").strip()
    # unresolved ${user_config.x} placeholders from a bundle count as empty
    return default if (not v or v.startswith("${")) else v


def orca_exe():
    v = _env("ORCA_EXE")
    if v:
        return json.loads(v) if v.startswith("[") else [v]
    cands = []
    s = platform.system()
    if s == "Windows":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     os.environ.get("LOCALAPPDATA", "")):
            cands += [os.path.join(base, "OrcaSlicer", "orca-slicer.exe")]
    elif s == "Darwin":
        cands += ["/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer",
                  os.path.expanduser("~/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer")]
    else:
        cands += [shutil.which("orca-slicer") or "", shutil.which("OrcaSlicer") or "",
                  "/usr/bin/orca-slicer", "/opt/OrcaSlicer/AppRun"]
    for c in cands:
        if c and os.path.isfile(c):
            return [c]
    raise RuntimeError("OrcaSlicer not found. Set ORCA_MCP_ORCA_EXE to the orca-slicer executable.")


def orca_data():
    v = _env("ORCA_DATA")
    if v:
        return v
    s = platform.system()
    if s == "Windows":
        return os.path.join(os.environ.get("APPDATA", ""), "OrcaSlicer")
    if s == "Darwin":
        return os.path.expanduser("~/Library/Application Support/OrcaSlicer")
    return os.path.expanduser("~/.config/OrcaSlicer")


def workdir():
    v = _env("WORKDIR")
    if v:
        return v
    if platform.system() == "Windows":
        return os.path.join(os.environ.get("LOCALAPPDATA", tempfile.gettempdir()), "orca-mcp")
    return os.path.expanduser("~/.cache/orca-mcp")


def _timeout():
    try:
        return int(_env("TIMEOUT_MIN", "30")) * 60
    except ValueError:
        return 1800


def _run(cmd, timeout):
    flags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    r = subprocess.run(cmd, capture_output=True, text=True, errors="ignore",
                       timeout=timeout, creationflags=flags)
    if (r.returncode != 0 and platform.system() == "Linux" and "xvfb-run" not in cmd[0]
            and shutil.which("xvfb-run") and not os.environ.get("DISPLAY")):
        r = subprocess.run(["xvfb-run", "-a"] + cmd, capture_output=True, text=True,
                           errors="ignore", timeout=timeout)
    return r


# ------------------------------------------------------------------ presets
def user_dir():
    base = os.path.join(orca_data(), "user")
    if not os.path.isdir(base):
        raise RuntimeError(f"No OrcaSlicer user presets found in {base}")
    best = None
    for d in os.listdir(base):
        p = os.path.join(base, d)
        if os.path.isdir(os.path.join(p, "machine")):
            n = sum(len(os.listdir(os.path.join(p, k))) for k in KINDS if os.path.isdir(os.path.join(p, k)))
            if not best or n > best[0]:
                best = (n, p)
    if not best:
        raise RuntimeError(f"No user printer presets in {base}. Create one in OrcaSlicer first.")
    return best[1]


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def presets():
    u = user_dir()
    out = {k: {} for k in KINDS}
    for k in KINDS:
        d = os.path.join(u, k)
        if not os.path.isdir(d):
            continue
        for f in os.listdir(d):
            if f.lower().endswith(".json"):
                try:
                    j = _load(os.path.join(d, f))
                except Exception:
                    continue
                out[k][j.get("name") or f[:-5]] = j
    return out


def _nozzle(name):
    m = re.search(r"(\d\.\d+)\s*(?:mm\s*)?nozzle|nozzle\s*(\d\.\d+)", name or "", re.I)
    return (m.group(1) or m.group(2)) if m else None


def compatible(preset, printer, pdata):
    """Is a process/filament preset usable with this printer?"""
    cp = preset.get("compatible_printers") or []
    if isinstance(cp, str):
        cp = [cp]
    if cp:
        return printer in cp or pdata.get("inherits", "") in cp
    nz = _nozzle(printer)
    if nz and _nozzle(preset.get("name", "")):
        return _nozzle(preset.get("name", "")) == nz
    return True


def find(kind, text, allp, printer=None):
    names = list(allp[kind])
    if text in allp[kind]:
        return text
    words = text.lower().split()
    hits = [n for n in names if all(w in n.lower() for w in words)]
    if printer and kind != "machine" and len(hits) > 1:
        pd = allp["machine"].get(printer, {})
        h2 = [n for n in hits if compatible(allp[kind][n], printer, pd)]
        hits = h2 or hits
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ValueError(f"No {kind} preset matches '{text}'. Use list_profiles.")
    if kind == "machine":
        # 'AD5M Pro 0.4' also matching '... @MULTIPLEMATERIALS': take the plainest name
        return min(hits, key=lambda n: (len(n), n))
    raise ValueError(f"'{text}' matches several {kind} presets: " + "; ".join(sorted(hits)[:12]))


def _settings_path():
    return os.path.join(workdir(), "settings.json")


def _settings():
    try:
        return _load(_settings_path())
    except Exception:
        return {}


def _save_settings(d):
    os.makedirs(workdir(), exist_ok=True)
    with open(_settings_path(), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)


def active_printer(allp=None):
    """Explicitly chosen (set_printer) > configured default > plainest user printer."""
    allp = allp or presets()
    if not allp["machine"]:
        raise RuntimeError("No user printer presets. Create one in OrcaSlicer first.")
    chosen = _settings().get("active_printer")
    if chosen in allp["machine"]:
        return chosen, "set_printer"
    dp = _env("DEFAULT_PRINTER")
    if dp:
        try:
            return find("machine", dp, allp), "default_printer setting"
        except ValueError:
            pass
    return min(allp["machine"], key=lambda n: (len(n), n)), "automatic"


def choose(printer=None, process=None, filament=None):
    allp = presets()
    m = find("machine", printer, allp) if printer else active_printer(allp)[0]
    md = allp["machine"][m]
    if process:
        p = find("process", process, allp, m)
    else:
        p = md.get("default_print_profile")
        if p not in allp["process"]:
            cands = [n for n in allp["process"] if compatible(allp["process"][n], m, md)]
            if not cands:
                raise ValueError(f"Say which process preset to use (no user process preset for {m}).")
            p = sorted(cands)[0]
    if filament:
        f = find("filament", filament, allp, m)
    else:
        f = (md.get("default_filament_profile") or [None])[0]
        if f not in allp["filament"]:
            cands = [n for n in allp["filament"] if compatible(allp["filament"][n], m, md)]
            if not cands:
                raise ValueError(f"Say which filament preset to use (no user filament preset for {m}).")
            f = sorted(cands)[0]
    return m, p, f


def sync():
    """Private data dir: system bundles + user presets under user/default."""
    wd = os.path.join(workdir(), "orca_data")
    src = os.path.join(orca_data(), "system")
    dst = os.path.join(wd, "system")
    os.makedirs(dst, exist_ok=True)
    if os.path.isdir(src):
        for root, _, files in os.walk(src):
            rel = os.path.relpath(root, src)
            for f in files:
                s = os.path.join(root, f)
                d = os.path.join(dst, rel, f)
                if not os.path.exists(d) or os.path.getmtime(s) > os.path.getmtime(d) \
                        or os.path.getsize(s) != os.path.getsize(d):
                    os.makedirs(os.path.dirname(d), exist_ok=True)
                    shutil.copy2(s, d)
    u = user_dir()
    for k in KINDS:
        a, b = os.path.join(u, k), os.path.join(wd, "user", "default", k)
        if not os.path.isdir(a):
            continue
        os.makedirs(b, exist_ok=True)
        for f in os.listdir(a):
            if f.lower().endswith(".json"):
                s, d = os.path.join(a, f), os.path.join(b, f)
                if not os.path.exists(d) or os.path.getmtime(s) > os.path.getmtime(d):
                    shutil.copy2(s, d)
    return wd


def _preset_file(wd, kind, name):
    d = os.path.join(wd, "user", "default", kind)
    p = os.path.join(d, name + ".json")
    if os.path.exists(p):
        return p
    for f in os.listdir(d):
        if f.lower().endswith(".json"):
            try:
                if _load(os.path.join(d, f)).get("name") == name:
                    return os.path.join(d, f)
            except Exception:
                pass
    raise ValueError(f"Preset file not found: {name}")


# ------------------------------------------------------------------ slicing
def _read_gcode(path):
    d = {}
    with open(path, encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            if not line.startswith(";"):
                continue
            if "total filament used [g]" in line:
                d["grams"] = sum(float(x) for x in re.findall(r"[\d.]+", line.split("=", 1)[1]))
            elif line.startswith("; estimated printing time (normal mode)") and "{" not in line:
                h = 0.0
                for n, u in re.findall(r"(\d+)\s*([dhms])", line.split("=", 1)[1]):
                    h += int(n) * {"d": 24, "h": 1, "m": 1 / 60, "s": 1 / 3600}[u]
                d["hours"] = h
            elif line.startswith("; enable_support ="):
                d["supports"] = line.split("=", 1)[1].strip() == "1"
            elif line.startswith("; support_type ="):
                d["support_type"] = line.split("=", 1)[1].strip()
            elif line.startswith("; layer_height ="):
                try:
                    d["layer_height"] = float(line.split("=", 1)[1])
                except ValueError:
                    pass
            elif line.startswith("; max_z_height:"):
                try:
                    d["height_mm"] = float(line.split(":", 1)[1])
                except ValueError:
                    pass
            elif line.startswith("; filament_type ="):
                d["material"] = line.split("=", 1)[1].strip().split(";")[0].strip('" ')
            elif line.startswith("; total filament cost") or line.startswith("; filament cost"):
                try:
                    d["cost"] = sum(float(x) for x in re.findall(r"[\d.]+", line.split("=", 1)[1]))
                except Exception:
                    pass
            elif line.startswith("; total layers count") or line.startswith("; total layer number"):
                m = re.search(r"(\d+)", line.split("=", 1)[-1])
                if m:
                    d["layers"] = int(m.group(1))
    return d


def _no_clobber(path):
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    if base.endswith(".gcode"):
        base, ext = base[:-6], ".gcode" + ext
    i = 2
    while os.path.exists(f"{base}_{i}{ext}"):
        i += 1
    return f"{base}_{i}{ext}"


def _hm(h):
    m = int(round(h * 60))
    return f"{m // 60} h {m % 60:02d} min"


SUPPORT_TAG = {"off": " [orcamcp no supports]", "on": " [orcamcp supports]"}


def _process_variant(wd, name, supports):
    """Copy of a user process with supports forced on/off, inside the private data dir only."""
    if supports not in SUPPORT_TAG:
        return name
    src = _preset_file(wd, "process", name)
    d = _load(src)
    new = name + SUPPORT_TAG[supports]
    d["name"] = new
    d["print_settings_id"] = new
    d["enable_support"] = "0" if supports == "off" else "1"
    with open(os.path.join(os.path.dirname(src), new + ".json"), "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    return new


def slice_files(files, printer=None, process=None, filament=None, save_to=None, name=None,
                copies=1, auto_orient=False, rotate_x=0, rotate_y=0, rotate_z=0, supports="profile"):
    if isinstance(files, str):
        files = [files]
    for f in files:
        if not os.path.isfile(f):
            raise ValueError(f"File not found: {f}")
    m, p, fil = choose(printer, process, filament)
    wd = sync()
    pv = _process_variant(wd, p, (supports or "profile").lower())
    tmp = tempfile.mkdtemp(prefix="orcamcp_")
    try:
        cmd = orca_exe() + [
            "--datadir", wd, "--slice", "0", "--arrange", "1", "--orient", "1" if auto_orient else "0",
            "--load-settings", _preset_file(wd, "machine", m) + ";" + _preset_file(wd, "process", pv),
            "--load-filaments", _preset_file(wd, "filament", fil), "--outputdir", tmp]
        if copies and int(copies) > 1:
            cmd += ["--clone-objects", ",".join([str(int(copies))] * len(files))]
        if rotate_x:
            cmd += ["--rotate-x", str(float(rotate_x))]
        if rotate_y:
            cmd += ["--rotate-y", str(float(rotate_y))]
        if rotate_z:
            cmd += ["--rotate", str(float(rotate_z))]
        if save_to:
            cmd += ["--export-3mf", "project.3mf"]
        cmd += list(files)
        r = _run(cmd, _timeout() * max(1, len(files)))
        gcodes = sorted(x for x in os.listdir(tmp) if x.endswith(".gcode"))
        if not gcodes:
            err = ""
            rj = os.path.join(tmp, "result.json")
            if os.path.exists(rj):
                try:
                    err = _load(rj).get("error_string", "")
                except Exception:
                    pass
            raise RuntimeError("OrcaSlicer did not slice: " + (err or (r.stdout + r.stderr)[-600:]))
        plates = [_read_gcode(os.path.join(tmp, g)) for g in gcodes]
        g = sum(x.get("grams", 0) for x in plates)
        h = sum(x.get("hours", 0) for x in plates)
        res = {"grams": round(g, 1), "hours": round(h, 2), "time": _hm(h),
               "material": plates[0].get("material", ""), "plates": len(plates),
               "printer": m, "process": p, "filament": fil,
               "supports": plates[0].get("supports"), "layer_height": plates[0].get("layer_height"),
               "height_mm": plates[0].get("height_mm")}
        if supports in SUPPORT_TAG:
            res["supports_forced"] = supports
        if rotate_x or rotate_y or rotate_z:
            res["rotation"] = {"x": rotate_x, "y": rotate_y, "z": rotate_z}
        if any("cost" in x for x in plates):
            res["orca_filament_cost"] = round(sum(x.get("cost", 0) for x in plates), 2)
        if copies and int(copies) > 1:
            res["copies"] = int(copies)
            res["grams_per_copy"] = round(g / int(copies), 2)
            res["hours_per_copy"] = round(h / int(copies), 3)
        if save_to:
            os.makedirs(save_to, exist_ok=True)
            base = name or os.path.splitext(os.path.basename(files[0]))[0]
            saved = []
            for i, gc in enumerate(gcodes):
                suf = "" if len(gcodes) == 1 else f"_plate{i + 1}"
                d = _no_clobber(os.path.join(save_to, f"{base}{suf}.gcode"))
                shutil.copy2(os.path.join(tmp, gc), d)
                saved.append(d)
            p3 = os.path.join(tmp, "project.3mf")
            if os.path.exists(p3):
                d = _no_clobber(os.path.join(save_to, f"{base}.gcode.3mf"))
                shutil.copy2(p3, d)
                saved.append(d)
            res["saved"] = saved
        return res
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def model_info(path):
    r = _run(orca_exe() + ["--info", path], 300)
    d = dict(re.findall(r"^\s*([a-z_]+)\s*=\s*(.+?)\s*$", r.stdout, re.M))
    out = {}
    try:
        out["size_mm"] = [round(float(d[f"size_{a}"]), 1) for a in "xyz"]
    except Exception:
        pass
    for a, b in (("volume", "volume_mm3"), ("number_of_facets", "facets"),
                 ("manifold", "manifold"), ("number_of_parts", "parts")):
        if a in d:
            out[b] = d[a]
    if "volume_mm3" in out:
        out["volume_cm3"] = round(float(out["volume_mm3"]) / 1000, 2)
    return out or {"raw": r.stdout[-800:]}


# ------------------------------------------------------------------ geometry & advice
import struct

ORIENTATIONS = [  # (label, rotate_x, rotate_y)
    ("as is", 0, 0), ("x +90", 90, 0), ("x 180 (upside down)", 180, 0), ("x -90", 270, 0),
    ("y +90", 0, 90), ("y -90", 0, 270)]

# support threshold (degrees from horizontal) used when the process does not say
MATERIAL = {
    "PLA": {"overhang": 35, "notes": []},
    "PETG": {"overhang": 40, "notes": ["PETG sags more on overhangs and bridges than PLA: keep supports on steep overhangs.",
                                       "PETG sticks hard to supports: use a support/object Z gap of 0.2 mm or more."]},
    "ASA": {"overhang": 40, "notes": ["ASA warps: prefer the orientation with the largest flat contact on the bed, use a brim and an enclosed printer.",
                                      "Avoid tall thin orientations with ASA (layer splitting from drafts)."]},
    "ABS": {"overhang": 40, "notes": ["ABS warps: prefer the largest flat contact on the bed, use a brim and an enclosed printer."]},
    "TPU": {"overhang": 45, "notes": ["Flexible filament: supports are hard to remove, choose an orientation that avoids them."]},
}


def _material_of(name, gcode_type=""):
    t = (gcode_type or name or "").upper()
    for k in ("PETG", "ASA", "ABS", "TPU", "PLA"):
        if k in t:
            return k
    if "FLEX" in t:
        return "TPU"
    return gcode_type.upper() if gcode_type else "PLA"


def _read_stl(path):
    import numpy as np
    with open(path, "rb") as f:
        data = f.read()
    if len(data) >= 84:
        n = struct.unpack("<I", data[80:84])[0]
        if 84 + n * 50 == len(data):
            rec = np.frombuffer(data[84:84 + n * 50], dtype=np.dtype([("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")]))
            return rec["v"].astype(float)
    txt = data.decode("utf-8", "ignore")
    v = [list(map(float, x)) for x in re.findall(r"vertex\s+(\S+)\s+(\S+)\s+(\S+)", txt)]
    if not v or len(v) % 3:
        raise ValueError("Orientation analysis needs an STL file.")
    return np.array(v, dtype=float).reshape(-1, 3, 3)


def _rot(rx, ry):
    import numpy as np
    a, b = np.radians(rx), np.radians(ry)
    Rx = np.array([[1, 0, 0], [0, np.cos(a), -np.sin(a)], [0, np.sin(a), np.cos(a)]])
    Ry = np.array([[np.cos(b), 0, np.sin(b)], [0, 1, 0], [-np.sin(b), 0, np.cos(b)]])
    return Ry @ Rx


def _regions(tri, mask):
    """Group the masked faces into connected regions (shared vertices). Returns list of index arrays."""
    import numpy as np
    idx = np.nonzero(mask)[0]
    if not len(idx):
        return []
    v = np.round(tri[idx].reshape(-1, 3), 3)
    _, vid = np.unique(v, axis=0, return_inverse=True)
    vid = vid.reshape(-1, 3)
    parent = list(range(vid.max() + 1))

    def root(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for a, b, c in vid:
        ra, rb, rc = root(a), root(b), root(c)
        parent[rb] = ra
        parent[rc] = ra
    lab = np.array([root(a) for a in vid[:, 0]])
    return [idx[lab == u] for u in np.unique(lab)]


def analyze_orientations(path, threshold_deg=35.0, min_feature_mm=2.0):
    """Per orientation: overhang that really needs support (connected regions wider than
    min_feature_mm, sloped), flat ceilings (bridges), bed contact, height."""
    import numpy as np
    tri = _read_stl(path)
    total = None
    rows = []
    cos_t = np.cos(np.radians(threshold_deg))
    for label, rx, ry in ORIENTATIONS:
        t = tri @ _rot(rx, ry).T
        cr = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
        area2 = np.linalg.norm(cr, axis=1)
        ok = area2 > 1e-12
        nz = np.zeros(len(t))
        nz[ok] = cr[ok, 2] / area2[ok]
        area = area2 / 2
        if total is None:
            total = float(area.sum())
        zmin = t[:, :, 2].min()
        on_bed = t[:, :, 2].max(axis=1) < zmin + 0.3
        down = (nz < -cos_t) & ~on_bed
        sloped = 0.0
        flat = 0.0
        biggest = 0.0
        for reg in _regions(t, down):
            pts = t[reg].reshape(-1, 3)
            span = float(max(np.ptp(pts[:, 0]), np.ptp(pts[:, 1])))
            depth = float((area[reg] * -nz[reg]).sum()) / max(span, 1e-6)  # how far it sticks out
            if span < min_feature_mm or depth < min_feature_mm / 2:
                continue  # tiny or shallow lip (stone undersides, texture): prints fine
            a_reg = float(area[reg].sum())
            a_flat = float(area[reg][nz[reg] < -0.985].sum())
            flat += a_flat
            sloped += a_reg - a_flat
            biggest = max(biggest, span)
        contact = (nz < -0.985) & (t[:, :, 2].max(axis=1) < zmin + 0.05)
        ext = t.reshape(-1, 3).max(0) - t.reshape(-1, 3).min(0)
        rows.append({"orientation": label, "rotate_x": rx, "rotate_y": ry,
                     "overhang_mm2": round(sloped + flat, 1),
                     "sloped_overhang_mm2": round(sloped, 1),
                     "flat_ceiling_mm2": round(flat, 1),
                     "largest_overhang_mm": round(biggest, 1),
                     "bed_contact_mm2": round(float(area[contact].sum()), 1),
                     "height_mm": round(float(ext[2]), 1),
                     "footprint_mm": [round(float(ext[0]), 1), round(float(ext[1]), 1)]})
    best = min(rows, key=lambda r: (r["overhang_mm2"], -r["bed_contact_mm2"], r["height_mm"]))
    stable = max(rows, key=lambda r: (r["bed_contact_mm2"], -r["overhang_mm2"]))
    return {"threshold_deg": threshold_deg, "ignored_details_below_mm": min_feature_mm,
            "surface_mm2": round(total or 0, 1), "orientations": rows,
            "least_support": best["orientation"], "most_stable": stable["orientation"],
            "note": "flat_ceiling = horizontal undersides; short ones print as bridges without support."}


def _process_threshold(name):
    try:
        v = presets()["process"].get(name, {}).get("support_threshold_angle")
        return float(v) if v not in (None, "", "0") else None
    except Exception:
        return None


def _layer_of(name):
    m = re.match(r"\s*(\d+\.\d+)\s*mm", name or "")
    return float(m.group(1)) if m else None


def _profile_tip(printer, process, height, footprint):
    """Suggest another of the USER's own compatible processes when size and layer height clash."""
    allp = presets()
    md = allp["machine"].get(printer, {})
    mine = [n for n in allp["process"] if compatible(allp["process"][n], printer, md)]
    lh = _layer_of(process)
    size = max([height or 0] + list(footprint or []))
    if not lh or not size:
        return None
    if size < 40 and lh >= 0.2:
        finer = sorted((n for n in mine if (_layer_of(n) or 1) < lh), key=lambda n: -(_layer_of(n) or 0))
        pick = next((n for n in finer if re.search(r"fine|detail|smooth|organic|organico", n, re.I)), finer[0] if finer else None)
        if pick:
            return f"Small part ({size:.0f} mm) at {lh} mm layers: '{pick}' would show more detail (slower)."
    if size > 120 and lh <= 0.16:
        coarser = sorted((n for n in mine if (_layer_of(n) or 0) > lh), key=lambda n: _layer_of(n) or 0)
        if coarser:
            return f"Large part ({size:.0f} mm) at {lh} mm layers: '{coarser[0]}' would be much faster."
    return None


# ------------------------------------------------------------------ MCP server
GUIDE = """Slicing with OrcaMCP. Only ever offer the user's OWN presets (from list_printers /
list_profiles), never factory presets they don't have.

GUIDED mode (default when the user asks to slice and did not give the details):
ask one question at a time, with options, and mark the recommended/current one:
 1. Printer: options from list_printers (mark the active one). Skip if only one printer or already said.
 2. Filament/material: options from list_profiles(printer=...) filaments (only compatible ones).
 3. Process: options from list_profiles(printer=...) processes (compatible only); recommend one by
    part size (small/detailed -> finer layer, big -> thicker layer).
 4. Orientation: run analyze_model first and offer: "as is", the least-support orientation,
    the most stable one (largest bed contact; prefer it for ASA/ABS), or "let OrcaSlicer orient".
 Then call slice_advice with the choices and present: grams, time, and the advice with the
 alternatives it measured (e.g. without supports, rotated), so the user can pick.
QUICK mode (user says "just slice it", batch jobs, or repeats a known setup): use the active
printer and its defaults, call slice (or slice_folder) directly, and give advice only if notable.
Remember the user's choices in the conversation; use set_printer when they switch printers.
Grams and hours are OrcaSlicer estimates. Paths are local paths on the user's computer."""

mcp = FastMCP("orca", instructions=GUIDE)


@mcp.tool()
def list_profiles(filter: str = "", printer: str = "") -> dict:
    """List the user's OrcaSlicer user presets (printers, processes, filaments) and the defaults.
    filter: optional words, e.g. 'PETG'. printer: only processes/filaments compatible with it."""
    allp = presets()
    m = find("machine", printer, allp) if printer else None
    out = {}
    for k, label in (("machine", "printers"), ("process", "processes"), ("filament", "filaments")):
        names = sorted(allp[k])
        if filter:
            names = [n for n in names if all(w in n.lower() for w in filter.lower().split())]
        if m and k != "machine":
            names = [n for n in names if compatible(allp[k][n], m, allp["machine"][m])]
        out[label] = names
    try:
        cm, cp, cf = choose(m)
        out["default"] = {"printer": cm, "process": cp, "filament": cf}
    except Exception as e:
        out["default"] = str(e)
    return out


@mcp.tool()
def list_printers() -> dict:
    """The user's printers (user printer presets) with nozzle, base model, default presets,
    how many process/filament presets fit each one, and which one is active."""
    allp = presets()
    act, why = active_printer(allp)
    rows = []
    for n in sorted(allp["machine"]):
        md = allp["machine"][n]
        rows.append({
            "printer": n, "active": n == act, "nozzle_mm": _nozzle(n) or _nozzle(md.get("inherits", "")),
            "based_on": md.get("inherits", ""),
            "default_process": md.get("default_print_profile"),
            "default_filament": (md.get("default_filament_profile") or [None])[0],
            "processes": sum(compatible(allp["process"][p], n, md) for p in allp["process"]),
            "filaments": sum(compatible(allp["filament"][f], n, md) for f in allp["filament"]),
        })
    return {"active": act, "active_because": why, "printers": rows}


@mcp.tool()
def set_printer(printer: str) -> dict:
    """Choose the active printer (name or part of it, e.g. 'kobra' or 'AD5M 0.6').
    It is remembered for the next slices until changed. Use 'auto' to go back to the default."""
    if printer.strip().lower() in ("auto", "default", ""):
        st = _settings()
        st.pop("active_printer", None)
        _save_settings(st)
        return {"active": active_printer()[0], "note": "Back to the default printer."}
    allp = presets()
    m = find("machine", printer, allp)
    st = _settings()
    st["active_printer"] = m
    _save_settings(st)
    _, p, f = choose(m)
    return {"active": m, "default_process": p, "default_filament": f}


@mcp.tool()
def compare_printers(files: list[str], printers: list[str] | None = None, process: str = "",
                     filament: str = "", copies: int = 1) -> dict:
    """Slice the same model(s) on several printers to compare grams and time (where to print it).
    printers: names or parts of names (empty = all user printers). process/filament: optional
    fragments matched per printer (e.g. 'balanced', 'pla'); each printer otherwise uses its defaults."""
    allp = presets()
    targets = [find("machine", p, allp) for p in printers] if printers else sorted(allp["machine"])
    rows = []
    for m in dict.fromkeys(targets):
        try:
            r = slice_files(files, m, process or None, filament or None, copies=copies)
            rows.append({"printer": m, "grams": r["grams"], "hours": r["hours"], "time": r["time"],
                         "process": r["process"], "filament": r["filament"]})
        except Exception as e:
            rows.append({"printer": m, "error": str(e)[:300]})
    ok = [r for r in rows if "hours" in r]
    out = {"results": rows}
    if ok:
        out["fastest"] = min(ok, key=lambda r: r["hours"])["printer"]
        out["least_material"] = min(ok, key=lambda r: r["grams"])["printer"]
    return out


@mcp.tool()
def slicing_guide() -> str:
    """How to run a guided slicing session (questions to ask, in order). Read before slicing."""
    return GUIDE


@mcp.tool()
def analyze_model(file: str, material: str = "", process: str = "") -> dict:
    """Orientation analysis of an STL without slicing: for 6 orientations, overhang area that needs
    support, bed contact area and height. Material (PLA, PETG, ASA, ABS, TPU) or the process's own
    support threshold sets the overhang angle. Returns least_support and most_stable orientations
    (use their rotate_x / rotate_y with slice or slice_advice)."""
    mat = _material_of(material)
    thr = (_process_threshold(find("process", process, presets())) if process else None) or MATERIAL.get(mat, {}).get("overhang", 40)
    out = analyze_orientations(file, thr)
    out["material"] = mat
    out["material_notes"] = MATERIAL.get(mat, {}).get("notes", [])
    return out


@mcp.tool()
def slice_advice(files: list[str], printer: str = "", process: str = "", filament: str = "",
                 rotate_x: float = 0, rotate_y: float = 0, auto_orient: bool = False,
                 try_alternatives: bool = True) -> dict:
    """Slice with the chosen options AND measure alternatives worth considering, then advise:
    without supports (if the profile uses them), the least-support orientation (if different),
    a better-suited process among the user's own, and material tips. Returns the chosen result,
    the measured alternatives and a list of advice lines."""
    base = slice_files(files, printer or None, process or None, filament or None,
                       auto_orient=auto_orient, rotate_x=rotate_x, rotate_y=rotate_y)
    m, p, f = base["printer"], base["process"], base["filament"]
    mat = _material_of(f, base.get("material", ""))
    thr = _process_threshold(p) or MATERIAL.get(mat, {}).get("overhang", 40)
    advice, alts = [], []

    def delta(r):
        return {"grams_saved": round(base["grams"] - r["grams"], 1),
                "minutes_saved": round((base["hours"] - r["hours"]) * 60)}

    geo = None
    if len(files) == 1 and files[0].lower().endswith(".stl"):
        try:
            geo = analyze_orientations(files[0], thr)
        except Exception:
            geo = None
    cur = None
    if geo and not auto_orient:
        cur = next((o for o in geo["orientations"] if o["rotate_x"] == (rotate_x % 360) and o["rotate_y"] == (rotate_y % 360)), None)

    if try_alternatives and base.get("supports"):
        r = slice_files(files, m, p, f, auto_orient=auto_orient, rotate_x=rotate_x, rotate_y=rotate_y, supports="off")
        alt = {"option": "same, without supports", "grams": r["grams"], "time": r["time"], **delta(r)}
        if alt["grams_saved"] < 0.5 and alt["minutes_saved"] < 3:
            pass  # the profile has supports on but none are generated: nothing to say
        else:
            alts.append(alt)
            gain = f"{alt['grams_saved']} g and {alt['minutes_saved']} min"
            if cur is not None:
                alt["sloped_overhang_mm2"] = cur["sloped_overhang_mm2"]
                alt["flat_ceiling_mm2"] = cur["flat_ceiling_mm2"]
                alt["largest_overhang_mm"] = cur["largest_overhang_mm"]
                if cur["sloped_overhang_mm2"] < 25 and cur["largest_overhang_mm"] < 10:
                    advice.append(f"Supports look unnecessary: no real overhang steeper than {thr:g} deg "
                                  f"(flat undersides {cur['flat_ceiling_mm2']} mm2 print as short bridges). "
                                  f"Without supports you save {gain}.")
                elif cur["largest_overhang_mm"] < (10 if mat in ("PLA", "ASA", "ABS") else 6):
                    advice.append(f"Without supports you save {gain}. The overhangs are many small ones "
                                  f"({cur['sloped_overhang_mm2']} mm2 in total, none wider than {cur['largest_overhang_mm']} mm): "
                                  f"in {mat} these usually print acceptably without supports, with rougher undersides. "
                                  "Worth trying without supports, or with supports on the build plate only.")
                elif cur["sloped_overhang_mm2"] < 150:
                    advice.append(f"Without supports you save {gain}. There are {cur['sloped_overhang_mm2']} mm2 of "
                                  f"overhang beyond {thr:g} deg (largest {cur['largest_overhang_mm']} mm): usually "
                                  "acceptable on hidden or rough faces; try one print or use support painting/blockers.")
                else:
                    advice.append(f"Keep supports: {cur['sloped_overhang_mm2']} mm2 of overhang beyond {thr:g} deg "
                                  f"(largest {cur['largest_overhang_mm']} mm). Without them you would save {gain} "
                                  "but those faces would print in the air.")
            else:
                advice.append(f"Without supports: {gain} less (check the overhangs before printing).")

    if try_alternatives and geo and cur is not None:
        best = next(o for o in geo["orientations"] if o["orientation"] == geo["least_support"])
        if best is not cur and best["overhang_mm2"] < 0.7 * cur["overhang_mm2"]:
            r = slice_files(files, m, p, f, rotate_x=best["rotate_x"], rotate_y=best["rotate_y"])
            alt = {"option": f"rotated {best['orientation']} (rotate_x={best['rotate_x']}, rotate_y={best['rotate_y']})",
                   "grams": r["grams"], "time": r["time"], **delta(r),
                   "overhang_mm2": best["overhang_mm2"], "height_mm": best["height_mm"]}
            alts.append(alt)
            advice.append(f"Orientation {best['orientation']}: overhang {cur['overhang_mm2']} -> {best['overhang_mm2']} mm2, "
                          f"{alt['grams_saved']} g and {alt['minutes_saved']} min saved. Check that the visible faces still look good.")
        if mat in ("ASA", "ABS"):
            st = next(o for o in geo["orientations"] if o["orientation"] == geo["most_stable"])
            if st is not cur and st["bed_contact_mm2"] > 1.5 * max(cur["bed_contact_mm2"], 1):
                advice.append(f"{mat}: orientation {st['orientation']} has {st['bed_contact_mm2']} mm2 on the bed "
                              f"(now {cur['bed_contact_mm2']}): less warping.")

    if try_alternatives and not auto_orient:
        try:
            r = slice_files(files, m, p, f, auto_orient=True)
            d = delta(r)
            if d["grams_saved"] >= 1 or d["minutes_saved"] >= 5:
                alts.append({"option": "let OrcaSlicer orient it (auto_orient=true)", "grams": r["grams"],
                             "time": r["time"], **d, "height_mm": r.get("height_mm")})
                advice.append(f"OrcaSlicer's own orientation saves {d['grams_saved']} g and {d['minutes_saved']} min "
                              "(it ignores looks: check which faces end up on the bed or on supports).")
        except Exception:
            pass

    tip = _profile_tip(m, p, cur["height_mm"] if cur else base.get("height_mm"), cur["footprint_mm"] if cur else None)
    if tip:
        advice.append(tip)
    advice += MATERIAL.get(mat, {}).get("notes", [])
    if not advice:
        advice.append("The chosen setup looks right: no cheaper alternative found.")
    return {"result": base, "material": mat, "overhang_threshold_deg": thr,
            "alternatives": alts, "advice": advice}


@mcp.tool(name="model_info")
def model_info_tool(path: str) -> dict:
    """Size (mm), volume, facet count and whether the mesh is closed, without slicing."""
    return model_info(path)



@mcp.tool()
def slice(files: list[str], printer: str = "", process: str = "", filament: str = "",
          copies: int = 1, auto_orient: bool = False, rotate_x: float = 0, rotate_y: float = 0,
          rotate_z: float = 0, supports: str = "profile", save_to: str = "", name: str = "") -> dict:
    """Slice one or more STL/3MF/STEP files together on one plate with the user's presets.
    Returns grams, hours and the presets used. Empty preset = printer default.
    copies: clone each model N times on the plate (per-copy grams/hours included).
    auto_orient: let OrcaSlicer orient the parts (default keeps the file orientation).
    rotate_x / rotate_y / rotate_z: degrees (e.g. from analyze_model).
    supports: 'profile' (as the process says), 'off' or 'on' (only for this slice; presets untouched).
    save_to: folder to save the .gcode and .gcode.3mf (empty = save nothing; never overwrites)."""
    return slice_files(files, printer or None, process or None, filament or None,
                       save_to or None, name or None, copies, auto_orient,
                       rotate_x, rotate_y, rotate_z, supports)


@mcp.tool()
def slice_folder(folder: str, pattern: str = "*.stl", recursive: bool = True, printer: str = "",
                 process: str = "", filament: str = "", max_files: int = 40,
                 write_csv: bool = True) -> dict:
    """Slice every matching file in a folder separately and return a table (grams, hours).
    write_csv: also write _orca_slicing.csv in the folder (never overwrites)."""
    found = []
    for root, _, files in os.walk(folder):
        found += [os.path.join(root, f) for f in files if fnmatch.fnmatch(f.lower(), pattern.lower())]
        if not recursive:
            break
    found.sort()
    if not found:
        return {"error": f"No {pattern} files in {folder}"}
    rows, errors, used = [], [], None
    for fp in found[:max_files]:
        try:
            r = slice_files(fp, printer or None, process or None, filament or None)
            rows.append({"file": os.path.relpath(fp, folder), "grams": r["grams"],
                         "hours": r["hours"], "time": r["time"]})
            used = (r["printer"], r["process"], r["filament"])
        except Exception as e:
            errors.append({"file": os.path.relpath(fp, folder), "error": str(e)[:300]})
    out = {"sliced": len(rows), "total_grams": round(sum(x["grams"] for x in rows), 1),
           "total_hours": round(sum(x["hours"] for x in rows), 2), "files": rows}
    if used:
        out["printer"], out["process"], out["filament"] = used
    if errors:
        out["errors"] = errors
    if len(found) > max_files:
        out["note"] = f"{len(found)} files found; only {max_files} sliced (raise max_files)."
    if write_csv and rows:
        dst = _no_clobber(os.path.join(folder, "_orca_slicing.csv"))
        with open(dst, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(["file", "grams", "hours", "printer", "process", "filament", "date"])
            for x in rows:
                w.writerow([x["file"], x["grams"], x["hours"], *used, _dt.date.today().isoformat()])
        out["csv"] = dst
    return out


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("--test", "--prueba"):
        print(json.dumps(list_profiles(), ensure_ascii=False, indent=1)[:1500])
        if len(sys.argv) > 2:
            print(json.dumps(model_info(sys.argv[2]), indent=1))
            print(json.dumps(slice([sys.argv[2]]), ensure_ascii=False, indent=1))
        return
    if len(sys.argv) > 1 and sys.argv[1] == "--version":
        print(__version__)
        return
    mcp.run()


if __name__ == "__main__":
    main()
