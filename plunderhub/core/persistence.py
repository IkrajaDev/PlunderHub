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


import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Dict, List

from plunderhub.logging_setup import logger
from plunderhub.constants import (
    CACHE_DIR, CONFIG_DIR, COVERS_DIR, COVER_MAP_FILE, GAME_ICONS_DIR, ICONS_DIR, FAVORITES_FILE, FIRST_SEEN_FILE, GAME_ICON_MAP_FILE, HERO_MAP_FILE, HIDDEN_FILE,
    ICON_MAP_FILE, KNOWN_TITLES_FILE, PLAYTIME_FILE, PLAYTIME_HISTORY_FILE, RECENT_FILE, SETTINGS_FILE,
)


def load_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# (path, mtime, size) of unreadable config files already copied aside, so a
# file that's loaded repeatedly before it's next saved (hidden.json is read
# on every scan) is preserved once, not once per load.
_PRESERVED_UNREADABLE: set = set()


def _preserve_unreadable(path: Path, reason: Exception) -> None:
    """Copies an unreadable config file aside before anything overwrites
    it. Its loader falls back to empty defaults, and the next save of that
    same data would otherwise silently replace the only copy - e.g. every
    minute of recorded playtime, over a single damaged byte."""
    try:
        stat = path.stat()
        key = (str(path), stat.st_mtime, stat.st_size)
    except OSError:
        return
    if key in _PRESERVED_UNREADABLE:
        return
    _PRESERVED_UNREADABLE.add(key)
    backup = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
    try:
        shutil.copy2(path, backup)
        logger.error(f"{path.name} couldn't be read ({reason}) - starting from defaults; "
                     f"the original was kept as {backup}.")
        return
    except OSError as e:
        copy_error = e
    # A file we can't read (e.g. root-owned after one `sudo plunderhub`)
    # can't be copied either - but it can still be renamed, which only
    # needs write access to the folder. Without this the next save's
    # os.replace() would silently delete it.
    try:
        os.replace(path, backup)
        logger.error(f"{path.name} couldn't be read ({reason}) - starting from defaults; "
                     f"the original was moved to {backup}.")
    except OSError as e:
        logger.error(f"{path.name} couldn't be read ({reason}), and keeping it failed too: "
                     f"{copy_error}; {e}")


def load_config_json(path: Path):
    """load_json for PlunderHub's own files: a missing file is simply
    None, but one that exists and can't be parsed is preserved first."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as e:
        _preserve_unreadable(path, e)
        return None
    try:
        return json.loads(text)
    except ValueError as e:
        _preserve_unreadable(path, e)
        return None


def save_json(obj, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, indent=2, ensure_ascii=False))
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, str(path))
        try:
            dir_fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


SETTINGS_SCHEMA_VERSION = 2

_UNVERSIONED = 0


def _migrate_settings_v0_to_v1(settings: Dict) -> Dict:
    return settings


def _migrate_settings_v1_to_v2(settings: Dict) -> Dict:
    # v2 adds Heroic's hand-added ("sideload") games as a store of their
    # own. Someone who had every store shown keeps seeing everything; a
    # deliberately narrowed filter is left as it was.
    stores = settings.get("heroic_store_filter")
    if isinstance(stores, list) and {"epic", "gog", "amazon", "zoom"} <= set(stores):
        settings["heroic_store_filter"] = sorted(set(stores) | {"sideload"})
    return settings


_SETTINGS_MIGRATIONS = [
    _migrate_settings_v0_to_v1,
    _migrate_settings_v1_to_v2,
]


def migrate_settings(settings: Dict) -> Dict:
    version = settings.get("schema")
    if not isinstance(version, int):
        version = _UNVERSIONED
    if version >= SETTINGS_SCHEMA_VERSION:
        if version > SETTINGS_SCHEMA_VERSION:
            logger.warning(
                f"settings.json is schema v{version}, newer than this build's "
                f"v{SETTINGS_SCHEMA_VERSION} - leaving it as-is."
            )
        return settings
    for step in range(version, SETTINGS_SCHEMA_VERSION):
        try:
            settings = _SETTINGS_MIGRATIONS[step](settings)
        except Exception as e:
            logger.error(f"Settings migration v{step}->v{step + 1} failed: {e}")
            return settings
        logger.info(f"Migrated settings from schema v{step} to v{step + 1}.")
    settings["schema"] = SETTINGS_SCHEMA_VERSION
    return settings


def migrate_cache_dirs() -> None:
    """Moves cover/icon art from the OLD cache location (~/.cache/plunderhub/)
    to the current one under CONFIG_DIR - see COVERS_DIR's own comment in
    constants.py for why it lives there now. Also covers the original,
    even older CONFIG_DIR/covers etc. layout, in case someone is
    upgrading from a build that predates either move."""
    for old_dir, new_dir in ((CACHE_DIR / "covers", COVERS_DIR),
                             (CACHE_DIR / "icons", ICONS_DIR),
                             (CACHE_DIR / "game_icons", GAME_ICONS_DIR)):
        if old_dir == new_dir or not old_dir.is_dir():
            continue
        moved = 0
        try:
            new_dir.mkdir(parents=True, exist_ok=True)
            for item in old_dir.iterdir():
                if not item.is_file():
                    continue
                target = new_dir / item.name
                try:
                    if target.exists():
                        item.unlink()
                    else:
                        shutil.move(str(item), str(target))
                        moved += 1
                except OSError as e:
                    logger.warning(f"Couldn't move {item.name} to {new_dir}: {e}")
            try:
                old_dir.rmdir()
            except OSError:
                pass
        except OSError as e:
            logger.warning(f"Couldn't migrate {old_dir} to {new_dir}: {e}")
            continue
        if moved:
            logger.info(f"Moved {moved} file(s) from {old_dir} to {new_dir}.")


def load_settings() -> Dict:
    settings = load_config_json(SETTINGS_FILE) or {}
    if not isinstance(settings, dict):
        _preserve_unreadable(SETTINGS_FILE, ValueError("not a JSON object"))
        return {}
    if not settings:
        return settings
    return migrate_settings(settings)


def save_settings(settings: Dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    settings["schema"] = SETTINGS_SCHEMA_VERSION
    save_json(settings, SETTINGS_FILE)


def save_hidden(hidden: Dict[str, List[str]]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    save_json(hidden, HIDDEN_FILE)


def load_hidden() -> Dict[str, List[str]]:
    if not HIDDEN_FILE.exists():
        return {}
    data = load_config_json(HIDDEN_FILE)
    if not isinstance(data, dict):
        return {}
    out: Dict[str, List[str]] = {}
    for launcher, ids in data.items():
        if isinstance(ids, list):
            out[str(launcher)] = [str(x) for x in ids]
    return out


def load_favorites() -> set:
    data = load_config_json(FAVORITES_FILE)
    if isinstance(data, list):
        return set(str(x) for x in data)
    return set()


def save_favorites(favorites: set) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    save_json(sorted(favorites), FAVORITES_FILE)


def load_recent() -> Dict[str, float]:
    data = load_config_json(RECENT_FILE)
    if isinstance(data, dict):
        out: Dict[str, float] = {}
        for k, v in data.items():
            try:
                out[str(k)] = float(v)
            except (TypeError, ValueError):
                pass
        return out
    return {}


def save_recent(recent: Dict[str, float]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    save_json(recent, RECENT_FILE)


def load_playtime() -> Dict[str, int]:
    data = load_config_json(PLAYTIME_FILE)
    if isinstance(data, dict):
        out: Dict[str, int] = {}
        for k, v in data.items():
            try:
                out[str(k)] = int(v)
            except (TypeError, ValueError):
                pass
        return out
    return {}


def save_playtime(playtime: Dict[str, int]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    save_json(playtime, PLAYTIME_FILE)


def load_playtime_history() -> Dict[str, Dict[str, int]]:
    data = load_config_json(PLAYTIME_HISTORY_FILE)
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Dict[str, int]] = {}
    for date_str, day_value in data.items():
        if isinstance(day_value, dict):
            day_out: Dict[str, int] = {}
            for uid, minutes in day_value.items():
                try:
                    day_out[str(uid)] = int(minutes)
                except (TypeError, ValueError):
                    pass
            if day_out:
                out[str(date_str)] = day_out
        else:
            try:
                minutes = int(day_value)
            except (TypeError, ValueError):
                continue
            if minutes:
                out[str(date_str)] = {"__unknown__": minutes}
    return out


def save_playtime_history(history: Dict[str, Dict[str, int]]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    save_json(history, PLAYTIME_HISTORY_FILE)


def load_first_seen() -> Dict[str, float]:
    data = load_config_json(FIRST_SEEN_FILE)
    if isinstance(data, dict):
        out: Dict[str, float] = {}
        for k, v in data.items():
            try:
                out[str(k)] = float(v)
            except (TypeError, ValueError):
                pass
        return out
    return {}


def save_first_seen(first_seen: Dict[str, float]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    save_json(first_seen, FIRST_SEEN_FILE)


def load_cover_map() -> Dict[str, Dict]:
    data = load_config_json(COVER_MAP_FILE)
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Dict] = {}
    for uid, entry in data.items():
        if isinstance(entry, dict):
            out[str(uid)] = entry
    return out


def save_cover_map(cover_map: Dict[str, Dict]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    save_json(cover_map, COVER_MAP_FILE)


def load_icon_map() -> Dict[str, Dict]:
    data = load_config_json(ICON_MAP_FILE)
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Dict] = {}
    for name, entry in data.items():
        if isinstance(entry, dict):
            out[str(name)] = entry
    return out


def save_icon_map(icon_map: Dict[str, Dict]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    save_json(icon_map, ICON_MAP_FILE)


def load_game_icon_map() -> Dict[str, Dict]:
    data = load_config_json(GAME_ICON_MAP_FILE)
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Dict] = {}
    for uid, entry in data.items():
        if isinstance(entry, dict):
            out[str(uid)] = entry
    return out


def save_game_icon_map(game_icon_map: Dict[str, Dict]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    save_json(game_icon_map, GAME_ICON_MAP_FILE)


def load_hero_map() -> Dict[str, Dict]:
    data = load_config_json(HERO_MAP_FILE)
    if not isinstance(data, dict):
        return {}
    return {str(uid): entry for uid, entry in data.items() if isinstance(entry, dict)}


def save_hero_map(hero_map: Dict[str, Dict]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    save_json(hero_map, HERO_MAP_FILE)


def load_known_titles() -> Dict[str, Dict]:
    data = load_config_json(KNOWN_TITLES_FILE)
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Dict] = {}
    for uid, entry in data.items():
        if isinstance(entry, dict):
            out[str(uid)] = entry
    return out


def save_known_titles(known_titles: Dict[str, Dict]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    save_json(known_titles, KNOWN_TITLES_FILE)
