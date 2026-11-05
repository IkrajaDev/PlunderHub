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

"""PlunderHub as a non-Steam game in Steam ("Add to Steam"), so Steam's
Big Picture / Game Mode (Steam Deck, Bazzite, SteamOS) can start it.

Steam keeps its non-Steam games in userdata/<account>/config/shortcuts.vdf,
a binary key-value file, and their pictures in .../config/grid named by
the shortcut's appid. Steam reads the file when it starts and writes it
back when it quits: it's changed only while Steam is closed (the caller
closes it first). Only PlunderHub's own entry is ever touched - every
other entry is written back byte for byte as it was read.
No GTK here.
"""

import os
import random
import re
import shutil
import struct
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from plunderhub.core.steam_heroic import steam_roots
from plunderhub.logging_setup import logger

APP_NAME = "PlunderHub"
# what Steam starts PlunderHub with: it then knows where it was started from
FROM_STEAM = "--from-steam"
_STEAM_ID64_BASE = 76561197960265728
_FLATPAK_STEAM = "com.valvesoftware.Steam"
_SNAP_STEAM = "/snap/steam/"
ASSETS = Path(__file__).resolve().parent.parent / "assets" / "steam"
# grid picture suffixes Steam looks for: capsule, wide capsule, hero, logo
_GRID = {"p": "capsule.png", "": "wide.png", "_hero": "hero.png", "_logo": "logo.png"}


# --- the binary key-value format --------------------------------------------------------
#
# A map is a run of entries, each a type byte, a key (0-ended utf-8), a
# value, and ends with 0x08. Types: 0x00 a map, 0x01 a string (0-ended),
# 0x02 a 32-bit int, 0x07 a 64-bit int; 0x03 float and 0x06 color are
# 4 bytes. Entries are kept as (type, key, value) to be written back as read.

_SIZES = {0x02: 4, 0x03: 4, 0x06: 4, 0x07: 8}


def _cstr(data: bytes, i: int) -> Tuple[str, int]:
    end = data.index(b"\x00", i)
    return data[i:end].decode("utf-8", "surrogateescape"), end + 1


def parse(data: bytes, i: int = 0) -> Tuple[list, int]:
    """[(type, key, value)] of the map starting at i, and where it ended."""
    entries = []
    while True:
        kind = data[i]
        i += 1
        if kind == 0x08:
            return entries, i
        key, i = _cstr(data, i)
        if kind == 0x00:
            value, i = parse(data, i)
        elif kind == 0x01:
            value, i = _cstr(data, i)
        elif kind in _SIZES:
            value, i = data[i:i + _SIZES[kind]], i + _SIZES[kind]
        else:
            raise ValueError(f"unknown entry type {kind:#x} at {i - 1}")
        entries.append((kind, key, value))


def dump(entries: list) -> bytes:
    out = bytearray()
    for kind, key, value in entries:
        out.append(kind)
        out += key.encode("utf-8", "surrogateescape") + b"\x00"
        if kind == 0x00:
            out += dump(value)
        elif kind == 0x01:
            out += value.encode("utf-8", "surrogateescape") + b"\x00"
        else:
            out += value
    out.append(0x08)
    return bytes(out)


def read_shortcuts(path: Path) -> list:
    """The file's top map ([(0, "shortcuts", [entries])]); an empty one
    when there's no file yet."""
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return [(0x00, "shortcuts", [])]
    if not data:
        return [(0x00, "shortcuts", [])]
    top, _end = parse(data)
    if not top or top[0][1].lower() != "shortcuts" or top[0][0] != 0x00:
        raise ValueError(f"{path} isn't a shortcuts file")
    return top


def _field(entry: list, name: str):
    for kind, key, value in entry:
        if key.lower() == name.lower():
            return kind, value
    return None, None


def _int32(raw: bytes) -> int:
    return struct.unpack("<i", raw)[0]


def _uint32(value: int) -> int:
    return value & 0xFFFFFFFF


# --- where, and with what ---------------------------------------------------------------

@dataclass
class SteamUser:
    root: Path          # the Steam install
    folder: Path        # userdata/<account id>
    flatpak: bool
    snap: bool

    @property
    def shortcuts(self) -> Path:
        return self.folder / "config" / "shortcuts.vdf"

    @property
    def grid(self) -> Path:
        return self.folder / "config" / "grid"


def _most_recent_account(root: Path) -> Optional[str]:
    """The account Steam last signed in with (config/loginusers.vdf's
    MostRecent), as its userdata folder name."""
    try:
        text = (root / "config" / "loginusers.vdf").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for m in re.finditer(r'"(\d{17})"\s*\{([^{}]*)\}', text):
        if re.search(r'"(?:MostRecent|mostrecent)"\s*"1"', m.group(2)):
            return str(int(m.group(1)) - _STEAM_ID64_BASE)
    return None


def steam_users() -> List[SteamUser]:
    """Each Steam install's signed-in account (the most recent one; the
    only one there is, else the folder changed last)."""
    users = []
    for root in steam_roots():
        base = root / "userdata"
        try:
            folders = [p for p in base.iterdir() if p.is_dir() and p.name.isdigit() and p.name != "0"]
        except OSError:
            continue
        if not folders:
            continue
        recent = _most_recent_account(root)
        pick = next((f for f in folders if f.name == recent), None)
        if pick is None:
            pick = max(folders, key=lambda f: (f / "config").stat().st_mtime if (f / "config").exists() else 0)
        text = str(root)
        users.append(SteamUser(root, pick, _FLATPAK_STEAM in text, text.startswith(_SNAP_STEAM)
                               or "/snap/steam/" in text))
    return users


def plunderhub_command() -> List[str]:
    """How the PlunderHub that runs now was started: its launcher
    (/usr/bin/plunderhub, /usr/local/bin/plunderhub...) or a copy's
    plunderhub.py with the Python running it; else the installed one."""
    started = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    if started is not None and started.is_file():
        if started.name == "plunderhub" and os.access(started, os.X_OK):
            return [str(started)]
        if started.name == "plunderhub.py":
            return [sys.executable or "python3", str(started)]
    found = shutil.which("plunderhub")
    if found:
        return [found]
    script = Path(__file__).resolve().parent.parent.parent / "plunderhub.py"
    return [sys.executable or "python3", str(script)]


def _quote(text: str) -> str:
    return '"' + text + '"'


def _launch(user: SteamUser) -> Tuple[str, str, str]:
    """(Exe, StartDir, LaunchOptions) as Steam wants them: Exe and
    StartDir in quotes. A Flatpak Steam runs its games in its own box:
    PlunderHub is started outside it through flatpak-spawn --host."""
    cmd = plunderhub_command()
    if user.flatpak:
        rest = " ".join(_quote(c) if " " in c else c for c in cmd)
        return _quote("/usr/bin/flatpak-spawn"), _quote(str(Path.home())), f"--host {rest} {FROM_STEAM}"
    exe = cmd[0]
    rest = " ".join(_quote(c) if " " in c else c for c in cmd[1:])
    return _quote(exe), _quote(str(Path(cmd[-1]).parent)), (rest + " " + FROM_STEAM).strip()


def flatpak_steam_can_start_host() -> Optional[bool]:
    """Whether the Flatpak Steam may start programs outside its box
    (talk-name org.freedesktop.Flatpak); None when it can't be told."""
    import subprocess
    try:
        out = subprocess.run(["flatpak", "info", "--show-permissions", _FLATPAK_STEAM],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return "org.freedesktop.Flatpak=talk" in out.replace(" ", "")


# --- adding and removing -----------------------------------------------------------------

def _ours(entry: list, appid: Optional[int]) -> bool:
    """PlunderHub's entry: the appid remembered when it was added, or -
    the setting lost - its name and the --from-steam it's started with."""
    kind, raw = _field(entry, "appid")
    if appid is not None and kind == 0x02 and _uint32(_int32(raw)) == _uint32(appid):
        return True
    _k, name = _field(entry, "AppName")
    _k, options = _field(entry, "LaunchOptions")
    return name == APP_NAME and FROM_STEAM in (options or "")


def find(user: SteamUser, appid: Optional[int] = None) -> Optional[int]:
    """The appid of PlunderHub's entry in this account's Steam, if it's there."""
    try:
        top = read_shortcuts(user.shortcuts)
    except (OSError, ValueError) as e:
        logger.warning(f"Steam shortcut: reading {user.shortcuts}: {e}")
        return None
    for _kind, _key, entry in top[0][2]:
        if isinstance(entry, list) and _ours(entry, appid):
            kind, raw = _field(entry, "appid")
            return _uint32(_int32(raw)) if kind == 0x02 else (appid or 0)
    return None


def _write(path: Path, top: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".plunderhub-bak"))
    tmp = path.with_name(f".{path.name}.plunderhub-tmp")
    tmp.write_bytes(dump(top))
    # read back: what's written must be what was meant
    if parse(tmp.read_bytes())[0] != top:
        tmp.unlink()
        raise IOError(f"{path}: the written file doesn't read back the same")
    os.replace(tmp, path)


def add(user: SteamUser) -> int:
    """Adds PlunderHub (or updates its entry: its command may have moved)
    with its pictures. Steam must be closed. Its appid."""
    top = read_shortcuts(user.shortcuts)
    entries = top[0][2]
    existing = next((e for e in entries if isinstance(e[2], list) and _ours(e[2], None)), None)
    if existing is not None:
        kind, raw = _field(existing[2], "appid")
        appid = _uint32(_int32(raw)) if kind == 0x02 else (random.getrandbits(31) | 0x80000000)
    else:
        appid = random.getrandbits(31) | 0x80000000
    icon = user.grid / f"{appid}_icon.png"
    exe, start, options = _launch(user)
    signed = struct.pack("<i", struct.unpack("<i", struct.pack("<I", appid))[0])
    entry = [
        (0x02, "appid", signed),
        (0x01, "AppName", APP_NAME),
        (0x01, "Exe", exe),
        (0x01, "StartDir", start),
        (0x01, "icon", str(icon)),
        (0x01, "ShortcutPath", ""),
        (0x01, "LaunchOptions", options),
        (0x02, "IsHidden", struct.pack("<i", 0)),
        (0x02, "AllowDesktopConfig", struct.pack("<i", 1)),
        (0x02, "AllowOverlay", struct.pack("<i", 1)),
        (0x02, "OpenVR", struct.pack("<i", 0)),
        (0x02, "Devkit", struct.pack("<i", 0)),
        (0x01, "DevkitGameID", ""),
        (0x02, "DevkitOverrideAppID", struct.pack("<i", 0)),
        (0x02, "LastPlayTime", struct.pack("<i", 0)),
        (0x01, "FlatpakAppID", ""),
        (0x00, "tags", []),
    ]
    if existing is not None:
        entries[entries.index(existing)] = (0x00, existing[1], entry)
    else:
        used = {int(k) for _t, k, _v in entries if k.isdigit()}
        entries.append((0x00, str(max(used) + 1 if used else 0), entry))
    _write(user.shortcuts, top)
    user.grid.mkdir(parents=True, exist_ok=True)
    for suffix, name in _GRID.items():
        src = ASSETS / name
        if src.is_file():
            shutil.copyfile(src, user.grid / f"{appid}{suffix}.png")
    if (ASSETS / "icon.png").is_file():
        shutil.copyfile(ASSETS / "icon.png", icon)
    logger.info(f"Steam shortcut: PlunderHub added to {user.shortcuts} (appid {appid}, {exe} {options})")
    return appid


def remove(user: SteamUser, appid: Optional[int]) -> bool:
    """Takes PlunderHub's entry and its pictures out; the other entries
    keep their order and numbers (Steam renumbers them itself). Steam
    must be closed. False when it wasn't there."""
    top = read_shortcuts(user.shortcuts)
    entries = top[0][2]
    ours = [e for e in entries if isinstance(e[2], list) and _ours(e[2], appid)]
    if not ours:
        return False
    for e in ours:
        kind, raw = _field(e[2], "appid")
        if kind == 0x02:
            found = _uint32(_int32(raw))
            for suffix in [*_GRID, "_icon"]:
                try:
                    (user.grid / f"{found}{suffix}.png").unlink()
                except OSError:
                    pass
        entries.remove(e)
    _write(user.shortcuts, top)
    logger.info(f"Steam shortcut: PlunderHub removed from {user.shortcuts}")
    return True


# --- how PlunderHub was started ----------------------------------------------------------

def started_from_steam(argv: List[str]) -> bool:
    return FROM_STEAM in argv


def running_under_steam() -> bool:
    """This PlunderHub was started by Steam (Steam gives what it starts
    SteamGameId): closing Steam closes it too."""
    return bool(os.environ.get("SteamGameId"))


def in_gamepad_ui() -> bool:
    """Steam's Big Picture / Game Mode started it: Steam sets
    SteamGamepadUI=1 for what it starts there; a gamescope session
    (Game Mode) says so in XDG_CURRENT_DESKTOP too."""
    return os.environ.get("SteamGamepadUI") == "1" or \
        os.environ.get("XDG_CURRENT_DESKTOP", "").lower() == "gamescope"
