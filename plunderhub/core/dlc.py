# -*- coding: utf-8 -*-
#
# PlunderHub - a unified game launcher for Linux
# Copyright (C) 2026 Ikraja
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/>.

"""An installed game's DLCs: which there are, which are in, and putting
them in or taking them out - for the game page's DLC tab (ui/dlc_tab.py).

Steam      the DLCs the account owns, from Steam's appinfo (depots with a
           dlcappid = files of their own; the rest of extended/listofdlc only
           unlock). In = its depots in the manifest's InstalledDepots and not
           in UserConfig DisabledDLC. A change is what Steam's Properties >
           DLC ticks write: UserConfig DisabledDLC, the game marked for an
           update - Steam (closed meanwhile) downloads or drops the depots
           when it starts.
Epic       legendary info's owned_dlc ("installable" = files of their own);
           in = legendary's installed.json has it. `legendary install <dlc>`
           / `legendary uninstall <dlc>` - what Heroic runs.
GOG        gogdl info's dlcs; in = Heroic's installed.json installedDLCs.
           A change is Heroic's Modify installation: `gogdl update` with the
           DLCs to keep, then installedDLCs written (Heroic closed meanwhile).

Amazon, Lutris, Faugus, Bottles: no DLC list - no tab."""

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from plunderhub.logging_setup import logger

CACHE_S = 24 * 3600          # the store's catalogue part; what's in is read fresh


@dataclass
class Dlc:
    id: str
    title: str
    size: int = 0               # bytes on disk (installed) or to download (not), 0 = unknown
    installed: bool = False
    files: bool = True          # False: only unlocks something - nothing to add or take out


@dataclass
class DlcList:
    store: str                  # steam | epic | gog
    app_id: str
    dlcs: List[Dlc] = field(default_factory=list)

    @property
    def actionable(self) -> List[Dlc]:
        return [d for d in self.dlcs if d.files]


def target(game) -> Optional[Tuple[str, str]]:
    """(store, app id) of a game whose DLCs can be handled, else None."""
    uid = getattr(game, "uid", "") or ""
    if uid.startswith("steam:") and uid[6:].isdigit():
        return "steam", uid[6:]
    if getattr(game, "launcher", "") == "Heroic" and game.store_platform in ("epic", "gog"):
        return game.store_platform, uid.rsplit(":", 1)[-1]
    return None


def _cache_file(store: str, app_id: str) -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "plunderhub" / "dlc" / f"{store}-{re.sub(r'[^A-Za-z0-9_.-]', '_', app_id)}.json"


def _cached(store: str, app_id: str) -> Optional[List[Dict]]:
    path = _cache_file(store, app_id)
    try:
        if time.time() - path.stat().st_mtime < CACHE_S:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
    except (OSError, ValueError):
        pass
    return None


def _store_cache(store: str, app_id: str, items: List[Dict]):
    path = _cache_file(store, app_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(items), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def forget(game):
    """The catalogue read again next time (after a change)."""
    t = target(game)
    if t:
        try:
            _cache_file(*t).unlink()
        except OSError:
            pass


def list_dlcs(game, fresh: bool = False) -> Optional[DlcList]:
    """The game's DLCs, sorted: in first, then by title. Blocks (a worker
    thread) - Heroic's tools ask their store. None: not a game with DLCs
    handled here."""
    t = target(game)
    if t is None:
        return None
    store, app_id = t
    try:
        if store == "steam":
            dlcs = _steam_list(app_id)
        else:
            catalogue = None if fresh else _cached(store, app_id)
            if catalogue is None:
                catalogue = _heroic_catalogue(store, app_id)
                if catalogue is not None:
                    _store_cache(store, app_id, catalogue)
            installed = _heroic_installed_dlcs(store, app_id)
            dlcs = [Dlc(c["id"], c["title"], int(installed.get(c["id"]) or c.get("size") or 0),
                        c["id"] in installed, bool(c.get("files", True)))
                    for c in catalogue or []]
    except Exception as e:  # noqa: BLE001 - an unreadable store: no tab, said in the log
        logger.warning(f"DLC: {game.title}'s list not read: {e}")
        return DlcList(store, app_id, [])
    dlcs.sort(key=lambda d: (not d.files, not d.installed, d.title.lower()))
    return DlcList(store, app_id, dlcs)


# --- Steam ------------------------------------------------------------------------

def _steam_manifest_path(appid: str) -> Optional[Path]:
    from plunderhub.core.steam_heroic import steam_libraries
    for lib in steam_libraries():
        acf = Path(lib) / "steamapps" / f"appmanifest_{appid}.acf"
        if acf.is_file():
            return acf
    return None


def _steam_manifest(appid: str) -> Tuple[Optional[Path], Dict]:
    from plunderhub.core.steam_heroic import parse_vdf_text
    acf = _steam_manifest_path(appid)
    if acf is None:
        return None, {}
    data = parse_vdf_text(acf.read_text(encoding="utf-8", errors="replace"))
    # the root block's insides (parse_vdf_text unwraps "AppState"), or still wrapped
    if isinstance(data.get("AppState"), dict):
        data = data["AppState"]
    return acf, data if isinstance(data, dict) else {}


def _user_config(state: Dict, key: str):
    """UserConfig's key whatever its case (Steam writes "OptionalDLC" back as "optionaldlc")."""
    cfg = state.get("UserConfig") or {}
    return next((v for k, v in cfg.items() if k.lower() == key.lower()), None) if isinstance(cfg, dict) else None


def _id_list(state: Dict, key: str) -> set:
    raw = str(_user_config(state, key) or "")
    return {p.strip() for p in raw.split(",") if p.strip().isdigit()}


def _disabled(state: Dict) -> set:
    return _id_list(state, "DisabledDLC")


def _steam_optional(appid: str) -> set:
    """The game's optional DLCs (a depot config "optionaldlc": Steam leaves
    them out unless opted in - UserConfig OptionalDLC; CS2's Workshop Tools)."""
    from plunderhub.core import treasure
    from plunderhub.core.steam_heroic import steam_roots
    root = treasure._steam_signed_in_root(steam_roots())
    if root is None:
        return set()
    info = treasure._app_infos(root, {int(appid)}).get(int(appid)) or {}
    out = set()
    for depot in (info.get("depots") or {}).values():
        if isinstance(depot, dict) and str((depot.get("config") or {}).get("optionaldlc") or "").isdigit():
            out.add(str(depot["config"]["optionaldlc"]))
    return out


def _steam_list(appid: str) -> List[Dlc]:
    from plunderhub.core import treasure
    from plunderhub.core.steam_heroic import steam_roots
    root = treasure._steam_signed_in_root(steam_roots())
    if root is None:
        return []
    owned = treasure._licensed_apps(root)
    info = treasure._app_infos(root, {int(appid)}).get(int(appid)) or {}
    _acf, state = _steam_manifest(appid)
    installed_depots = state.get("InstalledDepots") or {}
    in_dlc: Dict[str, int] = {}
    for _depot, d in installed_depots.items():
        if isinstance(d, dict) and str(d.get("dlcappid") or "").isdigit():
            in_dlc[str(d["dlcappid"])] = in_dlc.get(str(d["dlcappid"]), 0) + int(d.get("size") or 0)
    off = _disabled(state)
    sizes: Dict[int, int] = {}
    for key, depot in (info.get("depots") or {}).items():
        if not str(key).isdigit() or not isinstance(depot, dict) or not depot.get("dlcappid"):
            continue
        try:
            dlc = int(depot["dlcappid"])
        except (TypeError, ValueError):
            continue
        if dlc not in owned or not treasure._depot_counts(depot):
            continue
        public = (depot.get("manifests") or {}).get("public")
        size = public.get("size") if isinstance(public, dict) else depot.get("maxsize")
        if size is None:
            continue        # a depot with nothing published: the DLC's content is in the game's own
        try:
            sizes[dlc] = sizes.get(dlc, 0) + int(size or 0)
        except (TypeError, ValueError):
            sizes.setdefault(dlc, 0)
    listed = str((info.get("extended") or {}).get("listofdlc") or "")
    unlocks = {int(x) for x in listed.split(",") if x.strip().isdigit()} & owned - set(sizes)
    names = treasure._app_infos(root, set(sizes) | unlocks)
    out = []
    for dlc in set(sizes) | unlocks:
        common = (names.get(dlc) or {}).get("common") or {}
        title = str(common.get("name") or "")
        if not title:
            continue                    # nothing to call it by: an internal package
        if dlc in sizes:
            sid = str(dlc)
            here = sid in in_dlc and sid not in off
            out.append(Dlc(sid, title, in_dlc.get(sid) or sizes[dlc] if here else sizes[dlc], here, True))
        else:
            out.append(Dlc(str(dlc), title, 0, True, False))
    return out


def _write_manifest(acf: Path, text: str):
    tmp = acf.with_suffix(".acf.plunderhub-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, acf)


def steam_change(appid: str, install: List[str], remove: List[str]) -> Tuple[bool, str, str]:
    """Writes the new DLC ticks into the manifest (Steam must be closed):
    UserConfig DisabledDLC, the game marked for an update - and its
    AutoUpdateBehavior set to high priority, else Steam's scheduler puts
    the update off (days, seen 11-15: "Update delayed for 1098314 secs").
    Returns (ok, why, the AutoUpdateBehavior to put back afterwards)."""
    from plunderhub.core.launch_options import vdf_set
    acf, state = _steam_manifest(appid)
    if acf is None:
        return False, "the game's Steam manifest wasn't found", ""
    off = (_disabled(state) | {d for d in remove if d.isdigit()}) - set(install)
    optional = _steam_optional(appid)
    opted = (_id_list(state, "OptionalDLC") | (set(install) & optional)) - set(remove)
    before = str(state.get("AutoUpdateBehavior") or "0")
    text = acf.read_text(encoding="utf-8", errors="replace")
    try:
        text = vdf_set(text, ["UserConfig"], "DisabledDLC", ",".join(sorted(off, key=int)))
        if opted or _user_config(state, "OptionalDLC") is not None:
            text = vdf_set(text, ["UserConfig"], "OptionalDLC", ",".join(sorted(opted, key=int)))
        flags = int(state.get("StateFlags") or 4)
        text = vdf_set(text, [], "StateFlags", str(flags | 2))      # update required
        text = vdf_set(text, [], "AutoUpdateBehavior", "2")          # high priority: now
        _write_manifest(acf, text)
    except (ValueError, OSError) as e:
        return False, str(e), before
    logger.info(f"DLC: Steam {appid}: DisabledDLC {sorted(off)} (in {install}, out {remove})")
    return True, "", before


def steam_restore_priority(appid: str, value: str) -> bool:
    """Puts the game's own AutoUpdateBehavior back (Steam closed)."""
    from plunderhub.core.launch_options import vdf_set
    acf, state = _steam_manifest(appid)
    if acf is None or str(state.get("AutoUpdateBehavior") or "0") == value:
        return acf is not None
    try:
        _write_manifest(acf, vdf_set(acf.read_text(encoding="utf-8", errors="replace"), [], "AutoUpdateBehavior", value))
    except (ValueError, OSError) as e:
        logger.warning(f"DLC: Steam {appid}: AutoUpdateBehavior not put back to {value}: {e}")
        return False
    return True


def steam_settled(appid: str) -> bool:
    """Steam has applied it: the manifest says installed, nothing pending."""
    _acf, state = _steam_manifest(appid)
    try:
        flags = int(state.get("StateFlags") or 0)
    except ValueError:
        return False
    return bool(flags & 4) and not flags & (2 | 1024)


def _steam_running() -> bool:
    from plunderhub.core.launch_options import launcher_pids
    try:
        return bool(launcher_pids("Steam"))
    except Exception:  # noqa: BLE001
        return False


def _steam_shutdown(wait_s: int = 60) -> bool:
    import subprocess
    from plunderhub.core.launch_options import steam_shutdown_invocations
    for inv in steam_shutdown_invocations() or [["steam"]]:
        try:
            subprocess.Popen([*inv, "-shutdown"], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
    for _ in range(wait_s * 2):
        time.sleep(0.5)
        if not _steam_running():
            return True
    return False


def _steam_start(cmd: List[str]):
    import subprocess
    try:
        subprocess.Popen(list(cmd) + ["-silent"], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        logger.info(f"DLC: Steam didn't start: {e}")


def steam_apply(appid: str, install: List[str], remove: List[str], start_cmd: List[str], ran_before: bool,
                on_status: Optional[Callable[[str], None]] = None, wait_s: int = 4 * 3600) -> Tuple[bool, str]:
    """The whole Steam change, Steam already closed: the ticks written,
    Steam started in the tray to apply them (no window), closed again to
    put the game's update priority back, started again if it ran before.
    Blocks (a worker thread). on_status("applying" | "finishing")."""
    ok, why, before = steam_change(appid, install, remove)
    if not ok:
        if ran_before:
            _steam_start(start_cmd)
        return False, why
    _steam_start(start_cmd)
    on_status and on_status("applying")
    t0 = time.monotonic()
    time.sleep(5)
    settled = False
    while time.monotonic() - t0 < wait_s:
        if steam_settled(appid):
            settled = True
            break
        time.sleep(2)
    on_status and on_status("finishing")
    if _steam_shutdown():
        steam_restore_priority(appid, before)
    else:
        logger.warning(f"DLC: Steam didn't close - {appid}'s AutoUpdateBehavior stays 2")
    if ran_before:
        _steam_start(start_cmd)
    logger.info(f"DLC: Steam applied the change to {appid} ({'done' if settled else 'timed out'})")
    return (True, "") if settled else (False, "timeout")


# --- Heroic: Epic and GOG ---------------------------------------------------------

def _heroic_catalogue(store: str, app_id: str) -> Optional[List[Dict]]:
    """[{id, title, size, files}] from Heroic's tool - None if it couldn't ask."""
    import subprocess
    from plunderhub.core import treasure
    cmd, env = treasure._tool_cmd(store)
    if cmd is None:
        return None
    args = ["info", app_id, "--json"] if store == "epic" else ["info", app_id, "--os", "windows"]
    res = subprocess.run(cmd + args, env=env, capture_output=True, text=True, timeout=60)
    start = res.stdout.find("{")
    if start < 0:
        return None
    data = json.loads(res.stdout[start:])
    out = []
    if store == "epic":
        for d in (data.get("game") or {}).get("owned_dlc") or []:
            if isinstance(d, dict) and d.get("app_name"):
                out.append({"id": str(d["app_name"]), "title": str(d.get("title") or d["app_name"]),
                            "size": 0, "files": bool(d.get("installable"))})
        return out
    langs = data.get("languages") or []
    lang = "en-US" if "en-US" in langs else (langs[0] if langs else "en-US")
    for d in data.get("dlcs") or []:
        if not isinstance(d, dict) or not d.get("id"):
            continue
        sizes = d.get("size") or {}
        one = sizes.get(lang) or sizes.get("*") or {}
        out.append({"id": str(d["id"]), "title": str(d.get("title") or d["id"]),
                    "size": int(one.get("disk_size") or 0), "files": True})
    return out


def _gog_entry(app_id: str) -> Tuple[Optional[Path], Optional[Dict]]:
    from plunderhub.core import treasure
    root = treasure.heroic_root()
    if root is None:
        return None, None
    path = root / "gog_store" / "installed.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return path, None
    for e in (data.get("installed") if isinstance(data, dict) else None) or []:
        if isinstance(e, dict) and str(e.get("appName")) == app_id:
            return path, e
    return path, None


def _heroic_installed_dlcs(store: str, app_id: str) -> Dict[str, int]:
    """id -> bytes (0 unknown) of the DLCs in."""
    from plunderhub.core import treasure
    if store == "gog":
        _path, entry = _gog_entry(app_id)
        return {str(d): 0 for d in (entry or {}).get("installedDLCs") or []}
    root = treasure.heroic_root()
    if root is None:
        return {}
    try:
        data = json.loads((root / "legendaryConfig" / "legendary" / "installed.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out = {}
    for name, e in (data.items() if isinstance(data, dict) else []):
        if isinstance(e, dict) and e.get("is_dlc"):
            out[str(name)] = int(e.get("install_size") or 0)
    return out


def heroic_change(store: str, app_id: str, title: str, install: List[str], remove: List[str],
                  on_step: Optional[Callable[[int, int, str, float], None]] = None,
                  on_proc=None) -> Tuple[bool, str]:
    """Installs and removes DLCs the way Heroic does. Blocks (a worker
    thread). on_step(done, total, dlc id, percent of it)."""
    from plunderhub.core import treasure
    root = treasure.heroic_root()
    cmd, env = treasure._tool_cmd(store)
    if root is None or cmd is None:
        return False, "Heroic's tools weren't found"
    if store == "epic":
        runs = [(d, ["uninstall", d, "-y"]) for d in remove] + \
               [(d, ["install", d, "--platform", "Windows", "--skip-sdl", "-y"]) for d in install]
        failed = []
        for i, (dlc, args) in enumerate(runs):
            if on_step:
                on_step(i, len(runs), dlc, 0.0)
            ok, why = treasure._run_tool(cmd + args, env,
                                         lambda pct, _g, _s, i=i, dlc=dlc: on_step and on_step(i, len(runs), dlc, pct),
                                         on_proc)
            if not ok:
                logger.warning(f"DLC: {title}: legendary {args[0]} {dlc}: {why}")
                failed.append(why)
        if on_step:
            on_step(len(runs), len(runs), "", 100.0)
        return (not failed), (failed[0] if failed else "")
    # GOG: one update with the DLCs to keep (Heroic's Modify installation)
    path, entry = _gog_entry(app_id)
    if entry is None:
        return False, "the game isn't in Heroic's installed list"
    keep = [d for d in (str(x) for x in entry.get("installedDLCs") or []) if d not in remove]
    keep += [d for d in install if d not in keep]
    with_dlcs = ["--with-dlcs", "--dlcs", ",".join(keep)] if keep else ["--skip-dlcs"]
    args = ["update", app_id, "--platform", str(entry.get("platform") or "windows"),
            "--path", str(entry.get("install_path") or ""),
            "--support", str(root / "gogdlConfig" / "heroic_gogdl" / "gog-support" / app_id),
            *with_dlcs, "--lang", str(entry.get("language") or "en-US")]
    if entry.get("branch"):
        args += ["--branch", str(entry["branch"])]
    if not entry.get("install_path"):
        return False, "no install folder in Heroic's installed list"
    if on_step:
        on_step(0, 1, "", 0.0)
    ok, why = treasure._run_tool(cmd + args, env, lambda pct, _g, _s: on_step and on_step(0, 1, "", pct), on_proc)
    if not ok:
        return False, why
    from plunderhub.core.file_lock import held
    with held(path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False, "Heroic's installed list couldn't be read back"
        for e in data.get("installed") or []:
            if isinstance(e, dict) and str(e.get("appName")) == app_id:
                e["installedDLCs"] = keep
                e["install_size"] = f"{treasure.folder_bytes(Path(e.get('install_path') or ''), 30) / 1024 ** 3:.2f} GiB"
        tmp = path.with_suffix(".json.plunderhub-tmp")
        tmp.write_text(json.dumps(data, indent="\t", ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
    if on_step:
        on_step(1, 1, "", 100.0)
    logger.info(f"DLC: GOG {title}: installedDLCs {keep}")
    return True, ""
