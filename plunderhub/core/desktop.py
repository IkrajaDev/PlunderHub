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

"""What kind of desktop PlunderHub runs on."""

import os

# Compositors / window managers that tile windows themselves - minimize,
# maximize and close buttons in the app's own top bar are noise there.
_TILING_SOCKETS = ("HYPRLAND_INSTANCE_SIGNATURE", "SWAYSOCK", "I3SOCK", "NIRI_SOCKET")
_TILING_NAMES = (
    "hyprland", "sway", "i3", "niri", "river", "bspwm", "dwm", "dwl", "awesome", "qtile",
    "xmonad", "herbstluftwm", "leftwm", "spectrwm", "ratpoison", "stumpwm", "cosmic-comp-tiling",
)


def is_tiling_window_manager() -> bool:
    if any(os.environ.get(name) for name in _TILING_SOCKETS):
        return True
    names = " ".join(os.environ.get(var, "") for var in
                     ("XDG_CURRENT_DESKTOP", "XDG_SESSION_DESKTOP", "DESKTOP_SESSION")).lower()
    words = set(names.replace(":", " ").replace("/", " ").split())
    return any(name in words for name in _TILING_NAMES)
