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

"""Proton-GE Latest as each launcher's own default Proton - so every game
without a choice of its own runs on it; a game where someone picked another
one keeps that.

Once per launcher (and per install of it - native and Flatpak each): the
first time PlunderHub finds the launcher installed, closed, and the build in
its folder. Afterwards it's the person's setting again - changed back, it
stays changed. Steam and the others must be closed: they keep these files in
memory and write them back.

  Steam    config.vdf: CompatToolMapping "0" (Steam Play's default)
  Heroic   config.json: defaultSettings.wineVersion
  Lutris   runners/wine.yml: wine.version
  Faugus   config.json: default-runner (Faugus calls this build by the very
           same name and keeps it up to date the same way)
  Bottles  none: a runner is picked per bottle, ours is in its list."""

import json
from pathlib import Path
from typing import Callable, Dict, List, Tuple

from plunderhub.core import proton_store
from plunderhub.logging_setup import logger

SETTINGS_KEY = "proton_defaults_set"      # place key -> the build set there

# What a step answers
DONE, ALREADY, LATER, ABSENT, FAILED = "done", "already", "later", "absent", "failed"


def _home() -> Path:
    return proton_store._home()


def _config_home() -> Path:
    return proton_store._config_home()


# --- Steam ----------------------------------------------------------------------------------

def _steam(name: str) -> Tuple[str, str]:
    from plunderhub.core.launch_options import SteamBackend, _STEAM_COMPAT_PATH, _write_with_backup, vdf_set
    backend = SteamBackend()
    try:
        config = backend._config_vdf()
    except FileNotFoundError:
        return ABSENT, "no Steam user yet"
    if not config.is_file():
        return ABSENT, "no config.vdf"
    if backend._mapped_tool(backend._compat_mapping(), "0") == name:
        return ALREADY, str(config)
    if backend.blocking_process_running():
        return LATER, "Steam is running"
    text = config.read_text(encoding="utf-8", errors="ignore")
    new = text
    # what Steam writes for its own default: priority 75 (a game's is 250)
    for key, value in (("name", name), ("config", ""), ("priority", "75")):
        new = vdf_set(new, [*_STEAM_COMPAT_PATH, "0"], key, value)
    if new != text:
        _write_with_backup(config, new)
    return DONE, str(config)


# --- Heroic ---------------------------------------------------------------------------------

def _heroic_configs() -> List[Path]:
    home = _home()
    return [_config_home() / "heroic/config.json",
            home / ".var/app/com.heroicgameslauncher.hgl/config/heroic/config.json",
            home / ".var/app/io.github.heroicgameslauncher.hgl/config/heroic/config.json"]


def _heroic(config: Path, name: str) -> Tuple[str, str]:
    from plunderhub.core.launch_options import _process_running, _write_with_backup
    if not config.is_file():
        return ABSENT, str(config)
    proton = config.parent / "tools/proton" / name / "proton"
    if not proton.exists():
        return LATER, f"{name} isn't in {proton.parent.parent} yet"
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return FAILED, f"{config}: {e}"
    defaults = data.get("defaultSettings") if isinstance(data, dict) else None
    if not isinstance(defaults, dict):
        return FAILED, f"{config}: no defaultSettings"
    wanted = {"bin": str(proton), "name": name, "type": "proton"}
    current = defaults.get("wineVersion")
    if isinstance(current, dict) and current.get("bin") == wanted["bin"]:
        return ALREADY, str(config)
    if _process_running(("heroic",)):
        return LATER, "Heroic is running"
    defaults["wineVersion"] = wanted
    _write_with_backup(config, json.dumps(data, indent=2))
    return DONE, str(config)


# --- Lutris ---------------------------------------------------------------------------------

def _lutris(config_dir: Path, name: str) -> Tuple[str, str]:
    from plunderhub.core.launch_options import _dump_yaml, _load_yaml, _process_running
    if not config_dir.is_dir():
        return ABSENT, str(config_dir)
    path = config_dir / "runners/wine.yml"
    data = _load_yaml(path)
    wine = data.get("wine") if isinstance(data.get("wine"), dict) else {}
    if wine.get("version") == name:
        return ALREADY, str(path)
    if _process_running(("lutris",)):
        return LATER, "Lutris is running"
    wine["version"] = name
    data["wine"] = wine
    _dump_yaml(path, data)
    return DONE, str(path)


# --- Faugus ---------------------------------------------------------------------------------

def _faugus(config: Path, name: str) -> Tuple[str, str]:
    from plunderhub.core.launch_options import _process_running, _write_with_backup
    if not config.is_file():
        return ABSENT, str(config)
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return FAILED, f"{config}: {e}"
    if not isinstance(data, dict):
        return FAILED, f"{config}: not a settings file"
    if str(data.get("default-runner", "")).strip('"') == name:
        return ALREADY, str(config)
    if _process_running(("faugus-launcher", "faugus-launche")):
        return LATER, "Faugus is running"
    data["default-runner"] = name
    _write_with_backup(config, json.dumps(data, indent=4))
    return DONE, str(config)


# --- all of them ------------------------------------------------------------------------------

def _places(name: str) -> List[Tuple[str, Callable[[], Tuple[str, str]]]]:
    from plunderhub.core.scan_paths import LUTRIS_CONFIG_DIR_CANDIDATES
    home = _home()
    places = [("steam", lambda: _steam(name))]
    for config in _heroic_configs():
        places.append((f"heroic:{config}", lambda c=config: _heroic(c, name)))
    for config_dir in LUTRIS_CONFIG_DIR_CANDIDATES:
        places.append((f"lutris:{config_dir}", lambda d=config_dir: _lutris(d, name)))
    for config in (_config_home() / "faugus-launcher/config.json",
                   home / ".var/app/io.github.Faugus.faugus-launcher/config/faugus-launcher/config.json"):
        places.append((f"faugus:{config}", lambda c=config: _faugus(c, name)))
    return places


def apply(done_before: Dict[str, str], family: str = "ge") -> Dict[str, str]:
    """Makes the family's Latest the default wherever it hasn't been made so
    once already (done_before: place -> build, from the settings). Returns
    the places it's now done for (to keep in the settings), with those
    done before."""
    name = proton_store.FAMILIES[family].latest_name
    if proton_store.latest_build(family) is None:
        return dict(done_before)
    done = dict(done_before)
    for place, step in _places(name):
        if done.get(place) == name:
            continue
        try:
            outcome, detail = step()
        except Exception as e:
            outcome, detail = FAILED, str(e)
        if outcome in (DONE, ALREADY):
            done[place] = name
            if outcome == DONE:
                logger.info(f"Proton: {name} is now the default in {detail}")
        elif outcome in (LATER, FAILED):
            logger.info(f"Proton: {name} as the default in {place}: {outcome} ({detail})")
    return done
