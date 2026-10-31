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

"""The recommended launch options, and a newly added game getting them.

Recommended: GameMode (where it's installed), an FPS limit at the
monitor's refresh rate, Esync and Fsync, Wayland on a Wayland session,
NVAPI with an NVIDIA card, the BattlEye and EasyAntiCheat runtimes where
the launcher has a switch for them (Steam uses them by itself), and the
game's -fullscreen argument - only switched on, nothing else changed;
the ones gamescope does itself (window mode, Wayland, FPS limit) left
alone while it's on.

Launch options' Recommended button sets them; a game that's new to
PlunderHub gets them once by itself. Games already there when this came
- and those of a launcher seen for the first time, which were set up in
that launcher before - are never changed by themselves."""

import copy
import os
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, List, Optional, Set, Tuple

from plunderhub.core.launch_options import (
    LaunchOptions, LaunchOptionsBackend, backend_for, missing_tools, window_mode_in, with_window_mode,
)
from plunderhub.core.models import GameItem
from plunderhub.logging_setup import logger

# {"launchers": [...], "games": [...]}: what PlunderHub knew already
SETTINGS_KEY = "launch_defaults_known"


@dataclass
class Context:
    refresh: int        # the monitor's refresh rate in Hz, 0 when unknown
    wayland: bool       # a Wayland session
    nvidia: bool        # an NVIDIA card in the computer


def context(refresh: int) -> Context:
    wayland = bool(os.environ.get("WAYLAND_DISPLAY")) or os.environ.get("XDG_SESSION_TYPE") == "wayland"
    try:
        from plunderhub.core.inspection import gpus
        nvidia = any(gpu.vendor == "nvidia" for gpu in gpus())
    except Exception as e:
        logger.debug(f"Couldn't list the graphics cards: {e}")
        nvidia = False
    return Context(refresh, wayland, nvidia)


def tools_missing(game: GameItem) -> Dict[str, bool]:
    """The tools the game can't use (a Flatpak launcher needs their
    Flathub layers, not the system's)."""
    cmd = game.launch_cmd or []
    if cmd[:1] == ["flatpak"]:
        from plunderhub.core import flatpak_layers
        app_id = next((w for w in cmd[2:] if not w.startswith("-")), None) if cmd[1:2] == ["run"] else None
        return flatpak_layers.missing_for_app(app_id) if app_id else {}
    return missing_tools()


def recommended(opts: LaunchOptions, ctx: Context, backend: LaunchOptionsBackend, game: GameItem,
                missing: Dict[str, bool]) -> LaunchOptions:
    """opts with the recommended ones switched on."""
    new = copy.deepcopy(opts)
    unsupported = backend.unsupported(game)
    gamescope = new.gamescope.enabled
    if not missing.get("gamemode"):
        new.gamemode = True     # gamemoderun missing would stop the game starting
    if ctx.refresh and not gamescope and not new.fps_limit:     # a cap already set stays
        new.fps_limit = str(ctx.refresh)
    new.esync = new.fsync = True
    if ctx.wayland and not gamescope and "wayland" not in unsupported:
        new.wayland = True
    if ctx.nvidia and "nvapi" not in unsupported:
        new.nvapi = True
    if backend.anticheat_mode == "toggles":
        for key in ("battleye", "eac"):
            if key not in unsupported:
                setattr(new, key, True)
    if not gamescope and window_mode_in(new.args) is None:
        new.args = with_window_mode(new.args, "fullscreen")
    return new


# what changes() says of each switch: (field, i18n key of its name)
_SWITCHES = (("gamemode", "lo_gamemode"), ("esync", "lo_esync"), ("fsync", "lo_fsync"),
             ("wayland", "lo_wayland"), ("nvapi", "lo_nvapi"), ("battleye", "lo_battleye"), ("eac", "lo_eac"))


def changes(before: LaunchOptions, after: LaunchOptions) -> List[Tuple[str, str, str]]:
    """What differs, for the confirmation: (i18n key of the option,
    before, after) - "on"/"off" for a switch (the window mode's too), the
    value otherwise ("" for none)."""
    found = []
    for key, name in _SWITCHES:
        if getattr(before, key) != getattr(after, key):
            found.append((name, "on" if getattr(before, key) else "off", "on" if getattr(after, key) else "off"))
    if before.fps_limit != after.fps_limit:
        found.append(("lo_fps_limit", before.fps_limit, after.fps_limit))
    mode_before, mode_after = window_mode_in(before.args), window_mode_in(after.args)
    for mode in ("fullscreen", "borderless"):
        if (mode_before == mode) != (mode_after == mode):
            found.append((f"lo_win_{mode}", "on" if mode_before == mode else "off",
                          "on" if mode_after == mode else "off"))
    return found


def apply_to_new(games: Iterable[GameItem], known: Optional[dict], ctx: Context,
                 saved: Callable[[GameItem], None] = lambda g: None) -> dict:
    """Gives the games new to PlunderHub the recommended options, once
    each (worker thread); returns what's known now. The first time (known
    None) every game there is known and nothing changes; a launcher's
    games the first time it's seen are known as they are too. A Steam
    game waits while Steam runs (its options can't be written then)."""
    games = list(games)
    launchers: Set[str] = {g.launcher for g in games}
    if not isinstance(known, dict):
        return {"launchers": sorted(launchers), "games": sorted(g.uid for g in games)}
    known_launchers = set(known.get("launchers") or [])
    known_games = set(known.get("games") or [])
    for game in games:
        if game.uid in known_games:
            continue
        if game.launcher not in known_launchers:
            known_games.add(game.uid)       # set up in its launcher before PlunderHub saw it
            continue
        backend = backend_for(game)
        if backend is None:
            known_games.add(game.uid)
            continue
        try:
            if backend.blocking_process_running():
                continue                    # Steam runs - next round
            before = backend.load(game)
            after = recommended(before, ctx, backend, game, tools_missing(game))
            if after != before:
                backend.save(game, after)
                logger.info(f"Launch options: the recommended ones for the new game {game.title!r} "
                            f"({backend.where(game)})")
                saved(game)
        except Exception as e:
            logger.warning(f"Couldn't give {game.title!r} the recommended launch options: {e}")
        known_games.add(game.uid)
    return {"launchers": sorted(known_launchers | launchers), "games": sorted(known_games)}
