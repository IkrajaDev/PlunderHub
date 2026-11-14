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

"""Whether a Flatpak app can see a file of the host: its "filesystems"
permissions - from the app's metadata, then the overrides on top (all
apps', then its own; system-wide, then the user's: what Flatseal and
`flatpak override` write). Read from the files, no flatpak command run."""

import os
from pathlib import Path
from typing import List, Optional

_ROOTS = (Path("/var/lib/flatpak"), Path.home() / ".local/share/flatpak")
_XDG = {"xdg-desktop": "DESKTOP", "xdg-documents": "DOCUMENTS", "xdg-download": "DOWNLOAD",
        "xdg-music": "MUSIC", "xdg-pictures": "PICTURES", "xdg-public-share": "PUBLICSHARE",
        "xdg-templates": "TEMPLATES", "xdg-videos": "VIDEOS"}
_XDG_DEFAULT = {"DESKTOP": "Desktop", "DOCUMENTS": "Documents", "DOWNLOAD": "Downloads", "MUSIC": "Music",
                "PICTURES": "Pictures", "PUBLICSHARE": "Public", "TEMPLATES": "Templates", "VIDEOS": "Videos"}


def _filesystems(path: Path) -> Optional[List[str]]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    in_context = False
    for line in lines:
        line = line.strip()
        if line.startswith("["):
            in_context = line == "[Context]"
        elif in_context and line.startswith("filesystems="):
            return [w for w in line.split("=", 1)[1].split(";") if w]
    return []


def _xdg_dir(name: str) -> Path:
    home = Path.home()
    try:
        for line in (home / ".config/user-dirs.dirs").read_text().splitlines():
            if line.startswith(f"XDG_{name}_DIR="):
                value = line.split("=", 1)[1].strip().strip('"').replace("$HOME", str(home))
                return Path(value)
    except OSError:
        pass
    return home / _XDG_DEFAULT[name]


def _place(entry: str) -> Optional[Path]:
    """A filesystems entry as a host folder (None: everything, or nothing
    a game's file could be in)."""
    name = entry.split(":", 1)[0]
    if name in ("host", "host-all"):
        return Path("/")
    if name == "home":
        return Path.home()
    if name.startswith("~/"):
        return Path.home() / name[2:]
    if name.startswith("/"):
        return Path(name)
    base = name.split("/", 1)
    if base[0] in _XDG:
        folder = _xdg_dir(_XDG[base[0]])
        return folder / base[1] if len(base) > 1 else folder
    return None


def _under(path: str, root: Path) -> bool:
    root_s = os.path.realpath(root)
    return path == root_s or path.startswith(root_s.rstrip("/") + "/")


def sees(app_id: str, path: str) -> Optional[bool]:
    """Whether the Flatpak app_id can open path - None when its metadata
    isn't found (not a Flatpak here: nothing to say)."""
    entries: Optional[List[str]] = None
    for root in _ROOTS:
        found = _filesystems(root / "app" / app_id / "current/active/metadata")
        if found is not None:
            entries = found
            break
    if entries is None:
        return None
    for root in _ROOTS:
        for name in ("global", app_id):
            entries += _filesystems(root / "overrides" / name) or []
    real = os.path.realpath(path)
    if _under(real, Path.home() / ".var/app" / app_id):
        return True
    granted = False
    for entry in entries:
        negate = entry.startswith("!")
        place = _place(entry.lstrip("!"))
        if place is None or not _under(real, place):
            continue
        # the later entry wins (an override after the app's own)
        granted = not negate
    return granted
