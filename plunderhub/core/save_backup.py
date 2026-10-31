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

"""Zip backups of a game's save folders - what Heroic asks for before a
cloud sync - and putting one back.

Each backup is one zip in save-backups/<game>/, a folder per save
location inside (by its name) and backup.json saying where each one
belongs. A restore first backs up what's there now, so it can always be
undone."""

import json
import os
import re
import shutil
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from plunderhub.logging_setup import logger

_MANIFEST = "backup.json"
# made by a restore, of the saves it replaced
BEFORE_RESTORE = "before-restore"


def backups_root() -> Path:
    data = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share")
    return data / "plunderhub" / "save-backups"


def backup_dir(uid: str) -> Path:
    """Where the game's backups go."""
    return backups_root() / re.sub(r"[^A-Za-z0-9._-]+", "_", uid)


@dataclass
class SaveBackup:
    path: Path
    created: float
    size: int
    before_restore: bool
    # "native" or "flatpak" - which install's saves, when that was asked
    variant: str = ""


def list_backups(uid: str) -> List[SaveBackup]:
    """The game's backups, newest first."""
    found = []
    for path in backup_dir(uid).glob("*.zip"):
        try:
            st = path.stat()
        except OSError:
            continue
        variant = ""
        try:
            with zipfile.ZipFile(path) as zf:
                variant = str(json.loads(zf.read(_MANIFEST)).get("variant", ""))
        except (OSError, KeyError, ValueError, zipfile.BadZipFile):
            pass
        found.append(SaveBackup(path, st.st_mtime, st.st_size, BEFORE_RESTORE in path.stem, variant))
    return sorted(found, key=lambda b: b.created, reverse=True)


def back_up(uid: str, title: str, locations: List[Tuple[str, str]], kind: str = "",
            variant: str = "") -> Path:
    """Zips the save folders; returns the zip. Raises OSError when
    there's nothing to back up or it can't be written."""
    folders = [(name, Path(folder)) for name, folder in locations if Path(folder).is_dir()]
    if not folders:
        raise OSError("the save folder doesn't exist")
    directory = backup_dir(uid)
    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    target = directory / (f"{stamp}-{kind}.zip" if kind else f"{stamp}.zip")
    n = 2
    while target.exists():
        target = directory / f"{target.stem.split('~')[0]}~{n}.zip"
        n += 1
    tmp = target.with_suffix(".zip.part")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(_MANIFEST, json.dumps({
                "uid": uid, "title": title, "created": time.time(), "variant": variant,
                "locations": [{"name": name, "path": str(folder)} for name, folder in folders],
            }, indent=2))
            for name, folder in folders:
                for file in sorted(folder.rglob("*")):
                    if file.is_file():
                        zf.write(file, f"{name}/{file.relative_to(folder).as_posix()}")
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    logger.info(f"Backed up {title}'s saves to {target}")
    return target


def restore(backup: Path, uid: str, title: str, locations: List[Tuple[str, str]],
            variant: str = "") -> Optional[Path]:
    """Puts the backup's saves back in place of the current ones, after
    backing those up (that backup is returned, None when there was
    nothing to keep). The folders come from the backup itself, the
    game's current ones taking precedence by name."""
    current = dict(locations)
    with zipfile.ZipFile(backup) as zf:
        manifest = json.loads(zf.read(_MANIFEST))
        targets = {loc["name"]: Path(current.get(loc["name"], loc["path"])) for loc in manifest["locations"]}
        names = zf.namelist()
        for member in names:
            if member != _MANIFEST and (".." in Path(member).parts or member.startswith("/")):
                raise OSError(f"unsafe path in backup: {member}")
        kept = None
        if any(folder.is_dir() and any(folder.iterdir()) for folder in targets.values()):
            kept = back_up(uid, title, [(name, str(folder)) for name, folder in targets.items()],
                           BEFORE_RESTORE, variant)
        for name, folder in targets.items():
            if folder.is_dir():
                for child in folder.iterdir():
                    shutil.rmtree(child) if child.is_dir() and not child.is_symlink() else child.unlink()
            folder.mkdir(parents=True, exist_ok=True)
            prefix = f"{name}/"
            for member in names:
                if not member.startswith(prefix) or member.endswith("/"):
                    continue
                dest = folder / member[len(prefix):]
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
    logger.info(f"Restored {title}'s saves from {backup}")
    return kept
