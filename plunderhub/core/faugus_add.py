#!/usr/bin/env python3
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

"""Bringing a game or a store's launcher aboard Faugus - what Faugus'
own "Add game" window does, done by PlunderHub (the Treasure Map's
Faugus tab), so Faugus and PlunderHub both list it afterwards.

Copied from Faugus Launcher 2.5.7 (MIT, faugus/launcher.py
download_launcher, LAUNCHER_EXE_PATHS; faugus/ea_fix.py; faugus/utils.py
format_title, unique_gameid, the .lnk reading; faugus/launcher.py
build_desktop_file_content) - if a newer Faugus changes these, compare:
  - a store launcher: its maker's installer, downloaded, run in the
    prefix through Faugus' own umu-run with its quiet switches; done when
    the launcher's .exe is where Faugus expects it;
  - a game's installer: run in the prefix; the game's .exe found from
    the shortcuts (.lnk) the installer made, else the new .exe files;
  - a game already on disk / a Linux game: only written down;
  - then the games.json entry (Faugus closed meanwhile - it writes its
    own list back), the .exe's icon, an app-menu entry if asked.

Faugus native, Flatpak (io.github.Faugus.faugus-launcher) and AppImage
alike: where its files are decides (scan_paths.faugus_games_path, the
same place the library reads its games from)."""

import json
import os
import re
import shutil
import signal
import struct
import subprocess
import tarfile
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

from plunderhub.logging_setup import logger
from plunderhub.core.scan_paths import faugus_games_path, faugus_umu_path, flatpak_app_installed
from plunderhub.core.steam_heroic import steam_roots
from plunderhub.core.utils import which

FLATPAK_ID = "io.github.Faugus.faugus-launcher"
GE_LATEST = "Proton-GE Latest"         # PlunderHub's own GE (the Proton page keeps it), Faugus' name too
USER_AGENT = "PlunderHub (Faugus add)"

# kind -> about how big it is installed (GB), name, installer URL, file name, arguments after the installer, extra env,
#         the launcher's .exe inside the prefix (Faugus 2.5.7 LAUNCHER_EXE_PATHS)
_DOWNLOAD_SHARE = 0.3       # of a launcher's bar: its installer's download

LAUNCHERS: Dict[str, Dict] = {
    "battle": dict(gb=0.6, name="Battle.net",
                   url="https://downloader.battle.net/download/getInstaller?os=win&installer=Battle.net-Setup.exe",
                   file="Battle.net-Setup.exe",
                   args=["--installpath=C:\\Program Files (x86)\\Battle.net", "--lang=enUS"],
                   env={"PROTON_ENABLE_WAYLAND": "0", "WINE_SIMULATE_WRITECOPY": "1"},
                   exe="drive_c/Program Files (x86)/Battle.net/Battle.net.exe", signin=True),
    "ea": dict(gb=0.9, name="EA app",
               url="https://origin-a.akamaihd.net/EA-Desktop-Client-Download/installer-releases/EAappInstaller.exe",
               file="EAappInstaller.exe", args=["/S"], env={"PROTON_ENABLE_WAYLAND": "0"},
               exe="drive_c/Program Files/Electronic Arts/EA Desktop/EA Desktop/EALauncher.exe", signin=True),
    "epic": dict(gb=1.2, name="Epic Games Launcher",
                 url="https://launcher-public-service-prod06.ol.epicgames.com/launcher/api/installer/download/"
                     "EpicGamesLauncherInstaller.msi",
                 file="EpicGamesLauncherInstaller.msi", msi=True, args=["/passive"],
                 env={"PROTON_ENABLE_WAYLAND": "0"},
                 exe="drive_c/Program Files/Epic Games/Launcher/Portal/Binaries/Win64/EpicGamesLauncher.exe",
                 heroic="epic"),
    "gog": dict(gb=0.8, name="GOG Galaxy", url="https://github.com/Faugus/components/releases/download/v1.0.1/gog.tar.gz",
                file="gog.tar.gz", tar="gog/GalaxySetup.exe", args=["/VERYSILENT", "/NORESTART", "/SUPPRESSMSGBOXES"],
                env={}, exe="drive_c/Program Files/GOG Galaxy/GalaxyClient.exe", heroic="gog"),
    "ubisoft": dict(gb=0.7, name="Ubisoft Connect",
                    url="https://static3.cdn.ubi.com/orbit/launcher_installer/UbisoftConnectInstaller.exe",
                    file="UbisoftConnectInstaller.exe", args=["/S"], env={"PROTON_ENABLE_WAYLAND": "0"},
                    exe="drive_c/Program Files (x86)/Ubisoft/Ubisoft Game Launcher/UbisoftConnect.exe"),
    "rockstar": dict(gb=0.9, name="Rockstar Games Launcher",
                     url="https://gamedownloads.rockstargames.com/public/installer/Rockstar-Games-Launcher.exe",
                     file="Rockstar-Games-Launcher.exe", args=[], env={"PROTON_ENABLE_WAYLAND": "0"},
                     exe="drive_c/Program Files/Rockstar Games/Launcher/Launcher.exe", signin=True, keep_path=True),
    "amazon": dict(gb=0.5, name="Amazon Games", url="https://download.amazongames.com/AmazonGamesSetup.exe",
                   file="AmazonGamesSetup.exe", args=[], env={"PROTON_ENABLE_WAYLAND": "0"},
                   exe="drive_c/users/steamuser/AppData/Local/Amazon Games/App/Amazon Games.exe", signin=True,
                   heroic="amazon"),
    "wargaming": dict(gb=0.4, name="Wargaming Game Center",
                      url="https://redirect.wargaming.net/WGC/Wargaming_Game_Center_Install_NA.exe",
                      file="Wargaming_Game_Center_Install_NA.exe", args=["/SILENT"], env={},
                      exe="drive_c/ProgramData/Wargaming.net/GameCenter/wgc.exe", close_to_finish=True),
}
GAME_KINDS = ("setup", "exe", "linux")

# What Faugus writes into a store launcher's own entry (launch_arguments,
# faugus/launcher.py on choosing the launcher in Add game): its Chromium-based
# window breaks on Proton's Wayland driver; Battle.net needs the write-copy
# emulation too. Kept on every save (launch_options.FaugusBackend).
LAUNCHER_ENV: Dict[str, Dict[str, str]] = {
    "amazon": {"PROTON_ENABLE_WAYLAND": "0"}, "ea": {"PROTON_ENABLE_WAYLAND": "0"},
    "epic": {"PROTON_ENABLE_WAYLAND": "0"}, "rockstar": {"PROTON_ENABLE_WAYLAND": "0"},
    "ubisoft": {"PROTON_ENABLE_WAYLAND": "0"},
    "battle": {"WINE_SIMULATE_WRITECOPY": "1", "PROTON_ENABLE_WAYLAND": "0"},
}

# the launcher's own program, as its process shows (a Windows path's tail, lower case): once it
# runs in our prefix with its .exe in place, the install is done - its window may stay open
# (Faugus waits for it to be closed). The EA app isn't cut short: its folders are rearranged
# after it closes (ea_fix).
RUNNING_AS = {
    "battle": "battle.net\\battle.net.exe", "epic": "epicgameslauncher.exe", "gog": "galaxyclient.exe",
    "ubisoft": "ubisoftconnect.exe", "rockstar": "rockstar games\\launcher\\launcher.exe",
    "amazon": "amazon games.exe", "wargaming": "gamecenter\\wgc.exe",
}

# Faugus' games.json fields (faugus/utils.py GAME_FIELDS) and its defaults for a new entry
GAME_FIELDS = (
    "gameid", "title", "path", "prefix", "launch_arguments", "game_arguments", "mangohud", "gamemode",
    "sdl_enabled", "runner", "addapp_enabled", "addapp", "addapp_bat", "addapp_delay", "addapp_first", "cover",
    "lossless_enabled", "lossless_multiplier", "lossless_flow", "lossless_performance", "lossless_hdr",
    "lossless_present", "playtime", "hidden", "no_sleep", "category", "icon", "steamgriddb_id", "pre_launch",
    "post_launch", "steam_user", "disable_umu", "runtime", "last_played", "discord_rpc",
)


# --- where Faugus is --------------------------------------------------------------------

@dataclass
class FaugusSite:
    data: Path                    # the folder of games.json (icons/, covers/ beside it)
    config: Path                  # config.json
    flatpak: bool
    umu: Optional[Path]           # Faugus' own umu-run (None: Faugus never started yet)
    exec_cmd: Optional[List[str]]  # what an app-menu entry runs ("--game <id>" after it)
    sees: List[Path] = field(default_factory=list)   # Flatpak: the folders it can reach

    @property
    def games_json(self) -> Path:
        return self.data / "games.json"

    def read_config(self) -> Dict:
        try:
            data = json.loads(self.config.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def default_prefix_root(self) -> Path:
        """Faugus' "default-prefix" setting, else its own default ~/Faugus."""
        value = str(self.read_config().get("default-prefix") or "").strip().strip('"')
        return Path(os.path.expanduser(value)) if value else Path.home() / "Faugus"

    def visible(self, path: Path) -> bool:
        """A Flatpak Faugus only reaches its permitted folders (Flathub:
        home, /mnt, /run/media): a prefix elsewhere it couldn't start."""
        if not self.flatpak:
            return True
        try:
            p = Path(os.path.abspath(path))
        except Exception:
            return False
        return any(p == root or root in p.parents for root in self.sees)


def faugus_status() -> Tuple[str, Optional[FaugusSite]]:
    """("ok", site) | ("no_launcher", None). site.umu is None until umu-run
    is there: an install fetches it first (fetch_runner)."""
    native = which("faugus-launcher")
    flat = flatpak_app_installed(FLATPAK_ID)
    games = faugus_games_path()
    flat_data = Path.home() / ".var/app" / FLATPAK_ID / "data/faugus-launcher"
    if games is not None:
        data = games.parent
        is_flat = f"/.var/app/{FLATPAK_ID}/" in str(data) + "/"
    elif flat and not native:
        data, is_flat = flat_data, True
    elif native or flat:
        data, is_flat = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "faugus-launcher", False
    else:
        return "no_launcher", None
    if is_flat and not flat:
        # only its files are left (uninstalled): written to, nothing would read them
        if not native:
            return "no_launcher", None
        data, is_flat = Path.home() / ".local/share/faugus-launcher", False
    if is_flat:
        config = Path.home() / ".var/app" / FLATPAK_ID / "config/faugus-launcher/config.json"
        exec_cmd = ["flatpak", "run", f"--command=/app/bin/faugus-launcher", FLATPAK_ID]
        sees = [Path.home(), Path("/mnt"), Path("/run/media")]
    else:
        config = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "faugus-launcher/config.json"
        exec_cmd = [native] if native else None          # an AppImage: no entry to point at
        sees = []
    umu = data / "umu-run"
    if not umu.exists():
        umu = None
        if games is not None:
            # the library runs its games with the system's umu-run then - so can we
            sys_umu = faugus_umu_path()
            umu = Path(sys_umu) if sys_umu else None
    site = FaugusSite(data=data, config=config, flatpak=is_flat, umu=umu, exec_cmd=exec_cmd, sees=sees)
    return "ok", site


def faugus_running() -> bool:
    from plunderhub.core.launch_options import _process_running
    return _process_running(("faugus-launcher", "faugus-launche"))


def read_games(site: FaugusSite) -> List[Dict]:
    try:
        data = json.loads(site.games_json.read_text(encoding="utf-8"))
        return [g for g in data if isinstance(g, dict)] if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def title_from_installer(stem: str) -> str:
    """A setup file's name as a game's: "GOG_Galaxy_FAR_Lone_Sails" -> "FAR Lone Sails",
    "setup_some_game_1.2.3_(64bit)_(123)" -> "some game". An art search goes by it."""
    name = re.sub(r"^(gog[_ ]galaxy[_ ]|setup[_ ]|install[_ ]|installer[_ ])", "", stem, flags=re.I)
    name = re.sub(r"[_ ]\(.*$", "", name)
    name = re.sub(r"[_ ]v?\d+(\.\d+)+.*$", "", name)
    name = re.sub(r"[_ ](setup|installer)$", "", name, flags=re.I)
    return re.sub(r"[_ ]+", " ", name).strip()


def format_title(title: str) -> str:
    """Faugus' game id from a title (faugus/utils.py format_title)."""
    title = title.strip().lower()
    title = re.sub(r"[^\w\s-]", "", title)
    return re.sub(r"\s+", "-", title)


def unique_gameid(base: str, existing: Set[str]) -> str:
    if not base or base not in existing:
        return base
    n = 2
    while f"{base}-{n}" in existing:
        n += 1
    return f"{base}-{n}"


def launcher_aboard(site: FaugusSite, kind: str) -> Optional[Dict]:
    """The store launcher already in Faugus (its .exe path in an entry), or None."""
    tail = LAUNCHERS[kind]["exe"].split("/")[-1].lower()
    for g in read_games(site):
        if str(g.get("path") or "").lower().endswith("/" + tail) and not g.get("hidden"):
            return g
    return None


def launcher_kind(path: str) -> Optional[str]:
    """Which store launcher an entry's .exe is (Faugus' own paths), else None."""
    p = str(path or "").replace("\\", "/").lower()
    for kind, info in LAUNCHERS.items():
        if p.endswith("/" + info["exe"].split("/")[-1].lower()):
            return kind
    return None


def proton_choices() -> List[str]:
    """Faugus' Protons: the folders in Steam's compatibilitytools.d (native
    and Flatpak Steam), Proton-GE Latest first - PlunderHub's own."""
    names: List[str] = []
    for root in steam_roots():
        try:
            for d in sorted((root / "compatibilitytools.d").iterdir(), key=lambda p: p.name.lower(), reverse=True):
                if d.is_dir() and d.name not in names:
                    names.append(d.name)
        except OSError:
            continue
    if GE_LATEST in names:
        names.remove(GE_LATEST)
    return [GE_LATEST] + names


def proton_path(runner: str) -> str:
    """PROTONPATH for umu-run: the build's folder; GE Latest not downloaded
    yet: "GE-Proton" (umu fetches the newest itself)."""
    for root in steam_roots():
        d = root / "compatibilitytools.d" / runner
        if d.is_dir():
            return str(d)
    return "GE-Proton" if runner == GE_LATEST else runner


def free_bytes(path: Path) -> int:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        st = os.statvfs(p)
        return st.f_bavail * st.f_frsize
    except OSError:
        return 0


def umu_runtime_ready() -> bool:
    """umu's Steam Runtime is downloaded on its first run (~700 MB)."""
    root = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "umu"
    try:
        return any(p.is_dir() and p.name.startswith("steamrt") for p in root.iterdir())
    except OSError:
        return False


# --- what an installer left: the game's .exe -----------------------------------------------

_UTILITY = ("uninstall", "unins", "setup", "config", "readme", "redist", "vcredist", "vc_redist",
            "directx", "dxsetup", "crashhandler", "crashreport", "crashpad", "updater", "launcherpatcher",
            "dotnet", "physx", "easyanticheat", "battleye", "ue4prereq", "ueprereq", "install")


def _is_utility(name: str) -> bool:
    n = name.lower()
    return any(k in n for k in _UTILITY)


def prefix_shortcuts(prefix: Path) -> Set[str]:
    """The .lnk files in a prefix (Start menu, desktop)."""
    found: Set[str] = set()
    for root in (prefix / "drive_c/users", prefix / "drive_c/ProgramData"):
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if f.lower().endswith(".lnk"):
                    found.add(os.path.join(dirpath, f))
    return found


def prefix_exes(prefix: Path) -> Set[str]:
    found: Set[str] = set()
    for root in (prefix / "drive_c/Program Files", prefix / "drive_c/Program Files (x86)",
                 prefix / "drive_c/GOG Games", prefix / "drive_c/Games", prefix / "drive_c/users"):
        for dirpath, _dirs, files in os.walk(root):
            for f in files:
                if f.lower().endswith(".exe"):
                    found.add(os.path.join(dirpath, f))
    return found


def _lnk_target(path: str) -> Optional[str]:
    """A .lnk's target as a Windows path (faugus/utils.py parse_lnk_target_path)."""
    try:
        with open(path, "rb") as f:
            data = f.read(1 << 20)
        if len(data) < 76 or data[:4] != b"\x4c\x00\x00\x00":
            return None
        flags = struct.unpack_from("<I", data, 20)[0]
        o = 76
        if flags & 0x01:
            o += 2 + struct.unpack_from("<H", data, o)[0]
        if not flags & 0x02:
            return None
        li = o
        header = struct.unpack_from("<I", data, li + 4)[0]
        li_flags = struct.unpack_from("<I", data, li + 8)[0]
        if not li_flags & 0x01:
            return None
        base_off = struct.unpack_from("<I", data, li + 16)[0]
        uni_off = struct.unpack_from("<I", data, li + 28)[0] if header >= 0x24 else 0
        if uni_off:
            s = li + uni_off
            e = data.find(b"\x00\x00", s)
            if (e - s) % 2:
                e += 1
            return data[s:e].decode("utf-16-le", "replace")
        if base_off:
            s = li + base_off
            return data[s:data.find(b"\x00", s)].decode("cp1252", "replace")
    except (OSError, struct.error, ValueError):
        pass
    return None


def _win_to_unix(win: str, prefix: Path) -> Optional[str]:
    win = win.strip().strip('"')
    if len(win) < 2 or win[1] != ":":
        return None
    rest = re.sub(r"\\+", "/", win[2:]).lstrip("/")
    drive = win[0].lower()
    if drive == "c":
        cand = prefix / "drive_c" / rest
    else:
        dev = prefix / "dosdevices" / f"{drive}:"
        if not dev.is_symlink():
            return None
        cand = Path(os.path.realpath(dev)) / rest
    if cand.is_file():
        return str(cand)
    try:                                   # Windows doesn't mind the case, Linux does
        for e in os.listdir(cand.parent):
            if e.lower() == cand.name.lower() and (cand.parent / e).is_file():
                return str(cand.parent / e)
    except OSError:
        pass
    return None


@dataclass
class ExeFound:
    path: str
    size: int
    from_shortcut: bool


def found_exes(prefix: Path, lnk_before: Set[str], exe_before: Set[str]) -> List[ExeFound]:
    """The game's .exe candidates after an installer ran, likeliest first:
    targets of the new shortcuts it made (not uninstall / setup ones),
    then new .exe files, biggest first; helpers (uninstallers, redists,
    crash reporters) left out."""
    out: List[ExeFound] = []
    seen: Set[str] = set()
    for lnk in sorted(prefix_shortcuts(prefix) - lnk_before):
        name = Path(lnk).stem
        if _is_utility(name):
            continue
        win = _lnk_target(lnk)
        target = _win_to_unix(win, prefix) if win else None
        if target and target.lower().endswith(".exe") and not _is_utility(Path(target).stem) and target not in seen:
            seen.add(target)
            out.append(ExeFound(target, _size(target), True))
    rest = [p for p in prefix_exes(prefix) - exe_before if p not in seen and not _is_utility(Path(p).stem)
            and not _wine_own(p)]
    rest.sort(key=lambda p: -_size(p))
    out += [ExeFound(p, _size(p), False) for p in rest[:12]]
    return out


def _wine_own(path: str) -> bool:
    """Wine's own programs: the prefix is new, so they look newly installed."""
    low = path.lower().replace("\\", "/")
    return any(f"/{d}/" in low for d in ("windows nt", "internet explorer", "windows media player", "common files",
                                         "drive_c/windows"))


def gog_games(prefix: Path) -> Dict[str, Tuple[str, str]]:
    """The games GOG Galaxy finished installing in this prefix: {id: (title, its .exe)}.
    An offline GOG setup writes the same record."""
    from plunderhub.core.faugus_store import host_path, read_reg
    games = {}
    try:
        keys = read_reg(prefix / "system.reg", r"Software\Wow6432Node\GOG.com\Games")
    except OSError:
        return games
    for key, values in keys.items():
        exe = host_path(prefix, values.get("exe") or "")
        if "\\" not in key and exe is not None and exe.is_file():
            games[key] = (values.get("gameName") or key, str(exe))
    return games


def gog_version(prefix: str, install_dir: Optional[str]) -> Optional[str]:
    """The version a GOG game's record in this prefix gives."""
    from plunderhub.core.faugus_store import host_path, read_reg
    try:
        keys = read_reg(Path(prefix) / "system.reg", r"Software\Wow6432Node\GOG.com\Games")
    except OSError:
        return None
    wanted = os.path.realpath(install_dir or "")
    for values in keys.values():
        folder = host_path(Path(prefix), values.get("path") or values.get("workingDir") or "")
        if folder is not None and os.path.realpath(str(folder)) == wanted and values.get("ver"):
            return values["ver"]
    return None


def left_out(prefix: Path, exe_before: Set[str]) -> List[str]:
    return sorted({Path(p).name for p in prefix_exes(prefix) - exe_before if _is_utility(Path(p).stem)})[:6]


def _size(p: str) -> int:
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


# --- the EA app's own folder shuffle (faugus/ea_fix.py update_ea_path) ---------------------

def ea_fix(prefix: Path) -> Path:
    """The EA app installs into a version-numbered folder; Faugus copies
    the newest into "EA Desktop/EA Desktop" and starts it from there."""
    base = prefix / "drive_c/Program Files/Electronic Arts/EA Desktop"
    target = base / "EA Desktop"
    try:
        versions = []
        for d in base.iterdir():
            if d.is_dir() and d.name != "EA Desktop" and d.name[:1].isdigit():
                nums = [int(n) for n in re.findall(r"\d+", d.name)]
                versions.append((nums, d))
        if versions:
            versions.sort(key=lambda v: v[0], reverse=True)
            inner = versions[0][1] / "EA Desktop"
            if inner.is_dir():
                shutil.copytree(inner, target, dirs_exist_ok=True)
                for _n, d in versions:
                    shutil.rmtree(d, ignore_errors=True)
    except OSError as e:
        logger.info(f"Faugus add: EA app folders not rearranged: {e}")
    return target / "EALauncher.exe"


# --- the job: download, run, find --------------------------------------------------------

@dataclass
class AddRequest:
    kind: str                     # a LAUNCHERS key or setup / exe / linux
    title: str
    prefix: str                   # "" for linux
    runner: str                   # a compatibilitytools.d name ("Proton-GE Latest")
    file: str = ""                # the installer / the game's .exe / start file
    app_menu: bool = True


class AddJob:
    """Runs in a thread; the UI reads .phase / .frac / .state and is told
    through on_change (called from the thread - hand it to GLib)."""

    def __init__(self, site: FaugusSite, req: AddRequest, cache: Path, on_change: Callable[["AddJob"], None]):
        self.site, self.req, self.cache = site, req, cache
        self.on_change = on_change
        self.state = "working"        # working, pick (choose the .exe), ready (to write), failed, cancelled
        self.phase = "prepare"        # prepare, download, runtime, install, signin, close_it, find
        self.frac = 0.0
        self.error = ""
        self.exe: Optional[str] = None
        self.found: List[ExeFound] = []
        self.left_out: List[str] = []
        self._proc: Optional[subprocess.Popen] = None
        self._cancel = False
        self._made_prefix = False
        self.log = cache / f"{format_title(req.title) or 'game'}.log"

    def start(self):
        threading.Thread(target=self._run, daemon=True).start()

    def cancel(self):
        self._cancel = True
        p = self._proc
        if p is not None and p.poll() is None and self.state == "working":
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                pass

    def _set(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        try:
            self.on_change(self)
        except Exception:
            logger.exception("Faugus add: progress callback")

    def _run(self):
        try:
            self._work()
        except Exception as e:
            logger.exception("Faugus add failed")
            self._fail(str(e) or e.__class__.__name__)

    def _fail(self, why: str):
        self._cleanup()
        self._set(state="cancelled" if self._cancel else "failed", error=why)

    def _cleanup(self):
        """A prefix made for an install that didn't happen goes again (as Faugus does)."""
        if self._made_prefix and self.req.prefix:
            pfx = Path(self.req.prefix)
            if not any(str(g.get("prefix") or "") == str(pfx) for g in read_games(self.site)):
                shutil.rmtree(pfx, ignore_errors=True)

    def _work(self):
        req = self.req
        kind = req.kind
        if kind == "linux" or kind == "exe":
            self.exe = req.file
            self._set(state="ready", frac=1.0)
            return
        pfx = Path(req.prefix)
        if not pfx.exists():
            pfx.mkdir(parents=True, exist_ok=True)
            self._made_prefix = True
        if self.site.umu is None:
            if not fetch_runner(self.site):
                return self._fail("no_runner")
            self.site.umu = self.site.data / "umu-run"
        if kind in LAUNCHERS:
            info = LAUNCHERS[kind]
            installer = self._download(info)
            if self._cancel:
                return self._fail("")
            if info.get("tar"):
                with tarfile.open(installer, "r:gz") as tar:
                    try:
                        tar.extractall(path=installer.parent / "gog-x", filter="data")
                    except TypeError:               # Python before 3.10.12: no filter, check by hand
                        root = (installer.parent / "gog-x").resolve()
                        for m in tar.getmembers():
                            if not (m.isfile() or m.isdir()) or not (root / m.name).resolve().is_relative_to(root):
                                raise RuntimeError("unexpected file in the GOG Galaxy package")
                        tar.extractall(path=root)
                installer = installer.parent / "gog-x" / info["tar"]
            cmd = [str(self.site.umu)] + (["msiexec", "/i", str(installer)] if info.get("msi") else [str(installer)])
            cmd += info["args"]
            self._exec(cmd, info["env"], signin=info.get("signin"), close_it=info.get("close_to_finish"),
                       installed=lambda: _launcher_on_disk(kind, pfx, info),
                       done_when=(pfx / info["exe"], RUNNING_AS[kind]) if kind in RUNNING_AS else None)
            if self._cancel:
                return self._fail("")
            exe = ea_fix(pfx) if kind == "ea" else pfx / info["exe"]
            if not exe.is_file():
                return self._fail("not_installed")
            self.exe = str(exe)
            self._set(state="ready", frac=1.0)
            return
        # a game's installer
        lnk_before, exe_before = prefix_shortcuts(pfx), prefix_exes(pfx)
        gog_before = set(gog_games(pfx))
        inst = Path(req.file)
        cmd = [str(self.site.umu)] + (["msiexec", "/i", str(inst)] if inst.suffix.lower() == ".msi" else [str(inst)])
        # GOG's small setup .exe installs Galaxy, and Galaxy the game - and stays open
        # (a GOG offline setup writes the same record before its own last page: it's waited for)
        name = inst.name.lower()
        self._exec(cmd, {}, signin=False, close_it=False, installer=name,
                   finished=lambda: bool(set(gog_games(pfx)) - gog_before) and not _runs_in_prefix(name, req.prefix))
        if self._cancel:
            return self._fail("")
        self._set(phase="find", frac=0.97)
        self.found = found_exes(pfx, lnk_before, exe_before)
        new_gog = {gid: v for gid, v in gog_games(pfx).items() if gid not in gog_before}
        from_gog = [exe for _title, exe in new_gog.values()]
        if len(new_gog) == 1:       # GOG's name, not its setup file's: art is looked up by it
            req.title = next(iter(new_gog.values()))[0]
        if from_gog:
            self.found = [ExeFound(exe, _size(exe), True) for exe in from_gog] + \
                         [f for f in self.found if f.path not in from_gog and "/gog galaxy/" not in f.path.lower()]
        self.left_out = left_out(pfx, exe_before)
        if not self.found:
            return self._fail("no_exe")
        self.exe = self.found[0].path
        self._set(state="pick", frac=1.0)

    def _download(self, info: Dict) -> Path:
        self._set(phase="download", frac=0.0)
        folder = self.cache / "installers"
        folder.mkdir(parents=True, exist_ok=True)
        dest = folder / info["file"]
        part = dest.with_name(dest.name + ".part")
        req = urllib.request.Request(info["url"], headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as out:
            total = int(r.headers.get("Content-Length") or 0)
            got = 0
            last = 0.0
            while not self._cancel:
                chunk = r.read(256 * 1024)
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
                now = time.monotonic()
                if total and now - last > 0.15:
                    last = now
                    self._set(frac=_DOWNLOAD_SHARE * got / total)
        if self._cancel:
            part.unlink(missing_ok=True)
            return dest
        os.replace(part, dest)
        return dest

    def _exec(self, cmd: List[str], extra: Dict[str, str], signin: bool, close_it: bool,
              done_when: Optional[Tuple[Path, str]] = None, installed=None, finished=None, installer: str = ""):
        """umu-run in the prefix; the phase follows what it says (its first
        run fetches the Steam Runtime), the bar creeps while it works.
        done_when (a launcher): its .exe in place and its program running in
        this prefix = done, though umu-run waits on till its window closes -
        it's left open (not killed), the job goes on. installed() true =
        the launcher's files are there: the bar jumps near its end, what's
        left is only its window (an installer doesn't say how far it is).
        finished() true = done, whatever stays open. installer: its file
        name - once it's gone but umu-run still waits, something it started
        is holding the install open: the player is told to close that."""
        env = os.environ.copy()
        for k in ("PYTHONPATH", "PYTHONHOME", "LD_PRELOAD"):
            env.pop(k, None)
        env.update({"WINEPREFIX": self.req.prefix, "PROTONPATH": proton_path(self.req.runner),
                    "GAMEID": "umu-default", "PROTON_EAC_RUNTIME": str(self.site.data / "components/eac"),
                    "PROTON_BATTLEYE_RUNTIME": str(self.site.data / "components/be")})
        env.update(extra)
        first_runtime = not umu_runtime_ready()
        base = _DOWNLOAD_SHARE + 0.02 if self.req.kind in LAUNCHERS else 0.05
        self._set(phase="runtime" if first_runtime else "install", frac=max(self.frac, base))
        logger.info(f"Faugus add: {' '.join(cmd)} (WINEPREFIX={self.req.prefix}, PROTONPATH={env['PROTONPATH']})")
        log = open(self.log, "w", encoding="utf-8", errors="replace")
        self._proc = subprocess.Popen(cmd, env=env, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                      cwd=str(Path(cmd[-1]).parent) if Path(cmd[-1]).exists() else None,
                                      start_new_session=True)
        start = time.monotonic()
        seen = 0
        up_since = 0.0
        on_disk = False
        installer_seen = False
        while self._proc.poll() is None:
            time.sleep(0.5)
            if self._cancel:
                break
            if done_when is not None and done_when[0].is_file() and _runs_in_prefix(done_when[1], self.req.prefix):
                up_since = up_since or time.monotonic()
                if time.monotonic() - up_since > 3:
                    logger.info(f"Faugus add: {done_when[0].name} is up in the prefix - installed (left open)")
                    self._proc = None
                    break
            else:
                up_since = 0.0
            if finished is not None and finished():
                logger.info("Faugus add: the installed game is on record - done (what's open stays open)")
                self._proc = None
                break
            # the log: runtime download lines while umu sets itself up
            try:
                size = self.log.stat().st_size
            except OSError:
                size = seen
            phase = self.phase
            if first_runtime and size > seen:
                try:
                    with open(self.log, "rb") as f:
                        f.seek(max(0, size - 4096))
                        tail = f.read().decode("utf-8", "replace").lower()
                    if "steamrt" in tail and ("download" in tail or "extract" in tail):
                        phase = "runtime"
                    elif "proton" in tail or "wine" in tail:
                        phase = "install"
                except OSError:
                    pass
                seen = size
            elif not first_runtime:
                phase = "install"
            t = time.monotonic() - start
            if signin and phase == "install" and t > 25:
                phase = "signin"
            if close_it and phase == "install" and t > 25:
                phase = "close_it"
            if installer and phase == "install" and int(t * 2) % 6 == 0:
                if _runs_in_prefix(installer, self.req.prefix):
                    installer_seen = True
                elif installer_seen and t > 15:
                    close_it, installer = True, ""      # it stays so from here on
                    phase = "close_it"
            if installed is not None:
                on_disk = on_disk or installed()
                ceiling = 0.97 if on_disk else 0.85
                creep = 0.93 if on_disk else base + (ceiling - base) * (1 - 1 / (1 + t / 45))
            else:
                ceiling = 0.95
                creep = base + (ceiling - base) * (1 - 1 / (1 + t / 60))
            self._set(phase=phase, frac=max(self.frac, min(ceiling, creep)))
        log.close()
        if self._proc is None:
            return
        # wineserver may outlive the installer a moment: wait for it to settle files
        time.sleep(1.0)


# --- writing it down -------------------------------------------------------------------------

def _launcher_on_disk(kind: str, prefix: Path, info: Dict) -> bool:
    """The launcher's program is installed. The EA app starts out in a
    version-numbered folder (see ea_fix)."""
    if (prefix / info["exe"]).is_file():
        return True
    return kind == "ea" and any((prefix / "drive_c/Program Files/Electronic Arts/EA Desktop").glob("*/EALauncher.exe"))


def _runs_in_prefix(tail: str, prefix: str) -> bool:
    """A Windows program whose command line ends in tail runs in this prefix
    (Proton gives its processes STEAM_COMPAT_DATA_PATH = the prefix)."""
    from plunderhub.core.utils import _proc_pids, _read_proc_cmdline, _read_proc_environ
    want = os.path.normpath(prefix)
    for pid in _proc_pids() or []:
        args = _read_proc_cmdline(int(pid))
        if not args or not args[0].lower().replace("/", "\\").endswith(tail):
            continue
        env = _read_proc_environ(int(pid))
        if os.path.normpath(env.get("STEAM_COMPAT_DATA_PATH") or "") == want \
                or os.path.normpath(env.get("WINEPREFIX") or "").rstrip("/").removesuffix("/pfx") == want:
            return True
    return False


def _prefix_of(env: Dict[str, str]) -> str:
    compat = env.get("STEAM_COMPAT_DATA_PATH")
    if compat:
        return os.path.normpath(compat)
    pfx = env.get("WINEPREFIX") or ""
    return os.path.normpath(pfx.rstrip("/").removesuffix("/pfx")) if pfx else ""


def running_prefixes(prefixes: Set[str]) -> Set[str]:
    """Which of these prefixes have a Windows program running in them now
    (one pass over /proc: only .exe processes have their env read)."""
    from plunderhub.core.utils import _proc_pids, _read_proc_cmdline, _read_proc_environ
    want = {os.path.normpath(p) for p in prefixes if p}
    found: Set[str] = set()
    if not want:
        return found
    for pid in _proc_pids() or []:
        args = _read_proc_cmdline(int(pid))
        if not args or not args[0].lower().endswith(".exe"):
            continue
        base = args[0].replace("\\", "/").rsplit("/", 1)[-1].lower()
        if base in ("services.exe", "winedevice.exe", "plugplay.exe", "svchost.exe", "explorer.exe",
                    "rpcss.exe", "tabtip.exe", "conhost.exe", "start.exe", "umu.exe", "steam.exe"):
            continue                      # Wine's own helpers, alive a while after everything closed
        pfx = _prefix_of(_read_proc_environ(int(pid)))
        if pfx in want:
            found.add(pfx)
    return found


def stop_prefix(prefix: str, wait: float = 5.0):
    """Closes everything running in a prefix: what a desktop's quit sends
    (SIGTERM) to its processes, the rest killed after a few seconds.
    Blocks up to wait seconds - call it from a thread."""
    from plunderhub.core.utils import _proc_pids, _read_proc_environ
    want = os.path.normpath(prefix)
    own = os.getpid()

    def pids():
        return [int(p) for p in _proc_pids() or [] if int(p) != own
                and _prefix_of(_read_proc_environ(int(p))) == want]
    first = pids()
    logger.info(f"Faugus: closing {len(first)} process(es) in {prefix}")
    for pid in first:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    end = time.monotonic() + wait
    while time.monotonic() < end and pids():
        time.sleep(0.3)
    for pid in pids():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def write_entry(site: FaugusSite, req: AddRequest, exe: str,
                art: Optional[Callable[[str], Optional[str]]] = None) -> str:
    """The games.json entry (Faugus must be closed - it writes its own
    list back), the .exe's icon, an app-menu entry. Returns the game id."""
    from plunderhub.core.launch_options import _write_with_backup
    from plunderhub.core.pe_icon import save_icon_png
    games_path = site.games_json
    try:
        data = json.loads(games_path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            raise ValueError("games.json isn't a list")
    except FileNotFoundError:
        data = []
    gid = unique_gameid(format_title(req.title) or "game", {str(g.get("gameid")) for g in data if isinstance(g, dict)})
    cfg = site.read_config()
    entry: Dict = {f: "" for f in GAME_FIELDS}
    icon = site.data / "icons" / f"{gid}.png"
    entry.update(
        gameid=gid, title=req.title, path=exe, prefix=req.prefix if req.kind != "linux" else "",
        runner="Linux-Native" if req.kind == "linux" else req.runner,
        # Faugus' own defaults for a new game: its settings' MangoHud / GameMode / no-sleep
        mangohud=True if str(cfg.get("mangohud")) == "True" else "",
        gamemode=True if str(cfg.get("gamemode")) == "True" else "",
        no_sleep=True if str(cfg.get("no-sleep-enabled")) == "True" else "",
        addapp_bat=f"{os.path.dirname(exe)}/faugus-{gid}.bat",
        playtime=0, hidden=False, category=False, icon=str(icon),
        runtime="umu-steamrt4" if req.kind == "linux" else "",       # Faugus' default runtime
        launch_arguments=" ".join(f"{k}={v}" for k, v in LAUNCHER_ENV.get(req.kind, {}).items()),
    )
    if req.kind != "linux" and save_icon_png(Path(exe), icon):
        pass
    elif not icon.exists():
        entry["icon"] = ""
    if art is not None:
        try:
            entry["cover"] = art(gid) or ""      # a launcher's own cover + banner, into Faugus' folders
        except Exception as e:
            logger.info(f"Faugus add: art for {gid} not drawn: {e}")
    data.append(entry)
    games_path.parent.mkdir(parents=True, exist_ok=True)
    if games_path.exists():
        _write_with_backup(games_path, json.dumps(data, indent=4, ensure_ascii=False))
    else:
        games_path.write_text(json.dumps(data, indent=4, ensure_ascii=False), encoding="utf-8")
    if req.app_menu and site.exec_cmd:
        write_app_menu(site, gid, req.title, exe, icon if icon.exists() else None)
    logger.info(f"Faugus add: {req.title} written to {games_path} as {gid}")
    return gid


def fetch_runner(site: FaugusSite) -> bool:
    """Runs Faugus' own updater, which fetches umu-run - Faugus itself only
    does so when its first game starts. Whether it's there afterwards
    (the download fails quietly). A worker thread."""
    cmd = (["flatpak", "run", "--command=python3", FLATPAK_ID, "-m", "faugus.components"] if site.flatpak
           else ["python3", "-m", "faugus.components"])
    try:
        subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=180, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        logger.info(f"Faugus: its updater didn't run: {e}")
    ok = (site.data / "umu-run").exists()
    logger.info(f"Faugus: umu-run {'in place' if ok else 'still missing after its updater'}")
    return ok


def own_prefix(site: FaugusSite, gid: str) -> Optional[str]:
    """The entry's Wine prefix when it can go with it: a real prefix, and
    no other entry of Faugus' uses it. None otherwise."""
    games = read_games(site)
    expand = lambda g: os.path.realpath(os.path.expandvars(os.path.expanduser(str(g.get("prefix") or ""))))
    entry = next((g for g in games if g.get("gameid") == gid), None)
    if entry is None or not str(entry.get("prefix") or "").strip():
        return None
    prefix = expand(entry)
    if prefix == os.path.realpath(Path.home()) or not os.path.isdir(os.path.join(prefix, "drive_c")):
        return None
    if any(g.get("gameid") != gid and str(g.get("prefix") or "").strip() and expand(g) == prefix for g in games):
        return None
    return prefix


def remove_game(site: FaugusSite, gid: str, with_prefix: bool, to_trash: bool = True) -> Tuple[bool, str]:
    """What Faugus' own Delete does (faugus/launcher.py): the prefix when
    asked (to the Trash or for good), the entry, its art, app menu and
    desktop entries, its .bat. Faugus must be closed - it writes its list
    back. A worker thread. (False, "trash"): that drive has no Trash,
    nothing was changed."""
    if not site.games_json.is_file():
        return False, str(site.games_json)
    try:
        data = json.loads(site.games_json.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return False, str(e)
    entry = next((g for g in data if isinstance(g, dict) and g.get("gameid") == gid), None) \
        if isinstance(data, list) else None
    if entry is None:
        return False, f"{gid} isn't in Faugus' list"
    prefix = own_prefix(site, gid) if with_prefix else None
    if prefix:
        stop_prefix(prefix)
        from plunderhub.core import bilge
        problem = bilge.delete(Path(prefix), to_trash)
        if problem is not None:
            return False, problem
    from plunderhub.core.launch_options import _write_with_backup
    _write_with_backup(site.games_json, json.dumps([g for g in data if g is not entry], indent=4, ensure_ascii=False))
    desktop = Path(os.environ.get("XDG_DESKTOP_DIR") or Path.home() / "Desktop")
    bat = os.path.expandvars(os.path.expanduser(str(entry.get("addapp_bat") or "")))
    leftovers = [site.data / d / f"{gid}.png" for d in ("covers", "icons", "banners")]
    leftovers += [app_menu_dir() / f"{gid}.desktop", desktop / f"{gid}.desktop"] + ([Path(bat)] if bat else [])
    for path in leftovers:
        try:
            path.unlink(missing_ok=True)
        except OSError as e:
            logger.info(f"Faugus remove: {path} stays: {e}")
    order = site.data / "custom_order.json"
    try:
        kept = json.loads(order.read_text(encoding="utf-8"))
        if isinstance(kept, dict) and gid in kept:
            del kept[gid]
            order.write_text(json.dumps(kept, indent=4, ensure_ascii=False), encoding="utf-8")
    except (OSError, ValueError):
        pass
    logger.info(f"Faugus remove: {gid} gone{' with its prefix ' + prefix if prefix else ''}")
    return True, ""


def app_menu_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / "applications"


def _desktop_quote(s: str) -> str:
    if re.search(r"[\s\"'\\$`]", s):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$").replace("`", "\\`") + '"'
    return s


def write_app_menu(site: FaugusSite, gid: str, title: str, exe: str, icon: Optional[Path]):
    """The same entry Faugus' "App menu" switch writes (faugus/launcher.py
    build_desktop_file_content), so Faugus removes it with the game."""
    exec_line = " ".join(_desktop_quote(w) for w in site.exec_cmd) + f" --game {gid}"
    fallback = FLATPAK_ID if site.flatpak else "faugus-launcher"
    text = ("[Desktop Entry]\n"
            f"Name={title}\n"
            f"Exec={exec_line}\n"
            f"Icon={icon or fallback}\n"
            "Type=Application\n"
            "Categories=Game;\n"
            f"Path={os.path.dirname(exe)}\n")
    folder = app_menu_dir()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{gid}.desktop"
        path.write_text(text, encoding="utf-8")
        os.chmod(path, 0o755)
    except OSError as e:
        logger.info(f"Faugus add: app menu entry not written: {e}")
