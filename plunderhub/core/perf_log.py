# PlunderHub - a GTK3 game launcher for Linux
# Copyright (C) 2026 IkrajaDev
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""A game's performance, session after session - MangoHud's own log.

Switched on for a game (set_recording), its launcher's options get
MangoHud with logging: MANGOHUD_CONFIG names a folder of PlunderHub's
(one per game; inside the sandbox for a Flatpak launcher) and starts the
log at once. With the HUD off before, it stays out of sight: an empty,
see-through HUD (MangoHud's no_display stops the log too, 0.8.4).

After a session (collect) every CSV there becomes a summary - average
FPS, 1 % low, frame time spread, loads, temperatures, a short FPS curve -
kept in CONFIG_DIR/perf_log/<game>.json, and the CSV goes."""

import csv
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from plunderhub.constants import CONFIG_DIR
from plunderhub.logging_setup import logger

SUMMARY_DIR = CONFIG_DIR / "perf_log"
RAW_NAME = "plunderhub-perf"          # in output_folder: how ours are known
KEEP = 60
CURVE_POINTS = 120
MIN_SECONDS = 20                      # shorter logs say nothing
LOG_INTERVAL_MS = 20
# the HUD out of sight while it logs
HIDDEN_HUD = "alpha=0,background_alpha=0,fps=0,frame_timing=0,cpu_stats=0,gpu_stats=0"
_OURS = ("output_folder", "autostart_log", "log_interval", "read_cfg", "alpha", "background_alpha",
         "fps", "frame_timing", "cpu_stats", "gpu_stats")

SINCE_FILE = ".recording-since"
_LOCK = threading.Lock()


def _safe(uid: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", uid)


# --- where the logs go -----------------------------------------------------------------------

def flatpak_id(game) -> Optional[str]:
    """The Flatpak the game's launcher runs as, if it does."""
    from plunderhub.core.scan_paths import LAUNCHER_FLATPAK_IDS
    cmd = list(game.launch_cmd or [])
    if cmd[:2] == ["flatpak", "run"]:
        app_id = next((w for w in cmd[2:] if not w.startswith("-")), None)
        if app_id:
            return app_id
    words = " ".join(cmd) + " " + (game.install_dir or "")
    for app_id in LAUNCHER_FLATPAK_IDS.get(game.launcher, []):
        if app_id in words:
            return app_id
    return None


def raw_dir(game) -> Path:
    app_id = flatpak_id(game)
    if app_id:
        # the one folder of the home a Flatpak may write to - the same path inside
        return Path.home() / ".var/app" / app_id / "data" / RAW_NAME / _safe(game.uid)
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return data / "plunderhub" / RAW_NAME / _safe(game.uid)


def _parse_config(value: str) -> List[Tuple[str, str]]:
    items = []
    for part in (value or "").split(","):
        part = part.strip()
        if part:
            key, _sep, val = part.partition("=")
            items.append((key.strip(), val.strip()))
    return items


def _join_config(items: List[Tuple[str, str]]) -> str:
    return ",".join(f"{k}={v}" if v else k for k, v in items)


def recording(opts) -> Optional[bool]:
    """None: not recording; else whether the HUD shows while it does."""
    if not opts.mangohud:
        return None
    items = dict(_parse_config(opts.env.get("MANGOHUD_CONFIG", "")))
    if RAW_NAME not in items.get("output_folder", ""):
        return None
    return "read_cfg" in items


def set_recording(opts, game, on: bool, hud: bool) -> None:
    """Changes the LaunchOptions: MangoHud logging on (with the HUD shown
    or not) or off - the user's own MANGOHUD_CONFIG words kept."""
    was = recording(opts)
    own = [(k, v) for k, v in _parse_config(opts.env.get("MANGOHUD_CONFIG", "")) if k not in _OURS
           and k != "no_display"]
    if on:
        folder = raw_dir(game)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if was is None:
                # since when: a session after it that left no log says MangoHud is too old
                (folder / SINCE_FILE).write_text(str(time.time()))
        except OSError as e:
            logger.warning(f"Performance log folder {folder}: {e}")
        ours = [("read_cfg", "")] if hud else _parse_config(HIDDEN_HUD)
        ours += [("output_folder", str(folder)), ("autostart_log", "1"), ("log_interval", str(LOG_INTERVAL_MS))]
        opts.env["MANGOHUD_CONFIG"] = _join_config(ours + own)
        opts.mangohud = True
        return
    if own:
        opts.env["MANGOHUD_CONFIG"] = _join_config(own)
    else:
        opts.env.pop("MANGOHUD_CONFIG", None)
    if was is False:
        opts.mangohud = False       # it was only there to log


def set_hud(opts, hud: bool) -> None:
    """While recording: MangoHud's overlay shown (the user's own config
    read) or kept out of sight - the log goes on either way."""
    items = _parse_config(opts.env.get("MANGOHUD_CONFIG", ""))
    folder = dict(items).get("output_folder", "")
    if RAW_NAME not in folder:
        return
    keep = [(k, v) for k, v in items if k not in _OURS and k != "no_display"]
    ours = [("read_cfg", "")] if hud else _parse_config(HIDDEN_HUD)
    ours += [("output_folder", folder), ("autostart_log", "1"), ("log_interval", str(LOG_INTERVAL_MS))]
    opts.env["MANGOHUD_CONFIG"] = _join_config(ours + keep)
    opts.mangohud = True


# --- the summaries ---------------------------------------------------------------------------

def _summary_path(uid: str) -> Path:
    return SUMMARY_DIR / f"{_safe(uid)}.json"


def load(uid: str) -> List[Dict]:
    try:
        data = json.loads(_summary_path(uid).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    sessions = data.get("sessions") if isinstance(data, dict) else None
    return [s for s in sessions or [] if isinstance(s, dict) and "avg" in s]


def _save(uid: str, sessions: List[Dict]):
    path = _summary_path(uid)
    path.parent.mkdir(parents=True, exist_ok=True)
    sessions = sorted(sessions, key=lambda s: s.get("at", 0))[-KEEP:]
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"sessions": sessions}), encoding="utf-8")
    os.replace(tmp, path)


def delete(uid: str, at: Optional[float] = None):
    """One session (its start), or all of them (None)."""
    with _LOCK:
        if at is None:
            try:
                _summary_path(uid).unlink(missing_ok=True)
            except OSError:
                pass
            return
        _save(uid, [s for s in load(uid) if s.get("at") != at])


def _percentile(values: List[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))]


def summarise(path: Path) -> Optional[Dict]:
    """One MangoHud CSV -> its summary (None: too short, or not one)."""
    rows: List[Dict[str, float]] = []
    machine: Dict[str, str] = {}
    try:
        with open(path, newline="", encoding="utf-8", errors="replace") as f:
            reader = csv.reader(f)
            header: Optional[List[str]] = None
            spec_head: Optional[List[str]] = None
            for line in reader:
                if not line:
                    continue
                if header is None:
                    if line[0] == "os":
                        spec_head = line
                    elif spec_head is not None and not machine:
                        machine = dict(zip(spec_head, line))
                    elif line[0] == "fps":
                        header = line
                    continue
                try:
                    rows.append({k: float(v) for k, v in zip(header, line) if v != ""})
                except ValueError:
                    continue
    except OSError:
        return None
    if len(rows) < 10 or "fps" not in rows[0]:
        return None
    elapsed = [r.get("elapsed", 0.0) for r in rows]
    seconds = (elapsed[-1] - elapsed[0]) / 1e9 if elapsed[-1] > elapsed[0] else len(rows) * LOG_INTERVAL_MS / 1000
    if seconds < MIN_SECONDS:
        return None
    frametimes = [r["frametime"] for r in rows if r.get("frametime", 0) > 0]
    fps = [r["fps"] for r in rows if r.get("fps", 0) > 0]
    if not frametimes or not fps:
        return None
    mean_ft = sum(frametimes) / len(frametimes)
    worst1 = _percentile(frametimes, 0.99)
    worst01 = _percentile(frametimes, 0.999)

    def mean(key: str) -> Optional[float]:
        values = [r[key] for r in rows if key in r]
        return round(sum(values) / len(values), 1) if values else None

    def peak(key: str) -> Optional[float]:
        values = [r[key] for r in rows if key in r]
        return round(max(values), 1) if values else None
    # the curve: average FPS per slice of the session
    step = max(1, len(rows) // CURVE_POINTS)
    curve = []
    for i in range(0, len(rows), step):
        chunk = [r["frametime"] for r in rows[i:i + step] if r.get("frametime", 0) > 0]
        if chunk:
            curve.append(round(1000.0 / (sum(chunk) / len(chunk)), 1))
    try:
        stamp = time.mktime(time.strptime(re.search(r"(\d{4}-\d\d-\d\d_\d\d-\d\d-\d\d)", path.name).group(1),
                                          "%Y-%m-%d_%H-%M-%S"))
    except (AttributeError, ValueError):
        stamp = path.stat().st_mtime - seconds
    return {
        "at": stamp, "seconds": round(seconds),
        "avg": round(1000.0 / mean_ft, 1),
        "low1": round(1000.0 / worst1, 1) if worst1 else 0.0,
        "low01": round(1000.0 / worst01, 1) if worst01 else 0.0,
        "ft_avg": round(mean_ft, 2), "ft_p99": round(worst1, 2),
        "max": round(max(fps), 1), "min": round(min(fps), 1),
        "cpu_load": mean("cpu_load"), "gpu_load": mean("gpu_load"),
        "cpu_temp": peak("cpu_temp"), "gpu_temp": peak("gpu_temp"),
        "vram": peak("gpu_vram_used"), "ram": peak("ram_used"),
        "curve": curve, "gpu": machine.get("gpu", "").strip(), "cpu": machine.get("cpu", "").strip(),
    }


def collect(game) -> int:
    """Turns the game's finished MangoHud logs into summaries (worker
    thread); returns how many sessions were added. A log still being
    written (changed in the last seconds) waits for the next time."""
    folder = raw_dir(game)
    try:
        logs = sorted(p for p in folder.glob("*.csv") if not p.name.endswith("_summary.csv"))
    except OSError:
        return 0
    added = []
    for path in logs:
        try:
            if time.time() - path.stat().st_mtime < 5:
                continue
        except OSError:
            continue
        summary = summarise(path)
        if summary is not None:
            summary["snap"] = _snap_at(game.uid, summary["at"])
            added.append(summary)
        for leftover in (path, path.with_name(path.stem + "_summary.csv")):
            try:
                leftover.unlink(missing_ok=True)
            except OSError:
                pass
    if added:
        with _LOCK:
            sessions = load(game.uid)
            known = {s.get("at") for s in sessions}
            _save(game.uid, sessions + [s for s in added if s["at"] not in known])
    return len(added)


def log_missing(game) -> bool:
    """Recording is on, a session of a minute or more ended since, and no
    log came of it: MangoHud before 0.7 writes its log only when logging is
    switched off (Debian 12, Ubuntu 22.04), never while the game runs."""
    try:
        since = float((raw_dir(game) / SINCE_FILE).read_text().strip())
    except (OSError, ValueError):
        return False
    from plunderhub.core import launch_history
    played = [s for s in launch_history.load(game.uid)
              if s.get("at", 0) > since and (s.get("ran") or 0) >= 60 and s.get("result") == "ok"]
    if not played:
        return False
    return not any(s.get("at", 0) >= since - 120 for s in load(game.uid))


def _snap_at(uid: str, at: float) -> Dict:
    """What the game ran with then (launch_history's snapshot of the
    launch it was): Proton, driver, game version - the graph's marks."""
    from plunderhub.core import launch_history
    best = None
    for session in launch_history.load(uid):
        if session.get("at", 0) <= at + 120:
            best = session
    snap = (best or {}).get("snap") or {}
    return {key: launch_history.describe(snap, key) for key in ("proton", "driver", "game")}
