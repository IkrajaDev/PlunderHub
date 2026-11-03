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
"""Thin launcher script - the actual application lives in the plunderhub/
package (see plunderhub/__init__.py for the full module layout).

This file itself only exists so `python3 plunderhub.py` (running straight
from a source checkout, without installing anything - see README.md's Install,
"No install") and the installed `plunderhub` command
(PKGBUILD/install.sh - see the sys.path candidates below) keep working
exactly like they did before this file was one giant script instead of a
package.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# Where the plunderhub/ package might actually be found, checked in order:
#   1. Right next to this script - a source checkout, where plunderhub.py
#      and the plunderhub/ package sit side by side (this repo's own
#      layout - see "No install" under Install in README.md).
#   2/3. Wherever install.sh (manual, /usr/local/...) or PKGBUILD (pacman,
#      /usr/...) actually copies the package during a real install - see
#      both files' package()/install steps.
#
# Inserted in REVERSE priority order on purpose: each sys.path.insert(0,
# ...) call pushes everything already there one slot further back, so
# whichever candidate is inserted LAST ends up FIRST in sys.path (index
# 0) - i.e. checked first by `import plunderhub`. Looping over the
# candidates in their natural, already-in-priority-order sequence and
# inserting each at 0 (the previous, buggy version of this loop) has
# exactly the opposite effect: the LAST candidate in the tuple
# (/usr/lib/plunderhub) ends up FIRST in sys.path, ahead of a real
# source checkout right next to this very script - so running
# `python3 plunderhub.py` straight from a source checkout could
# silently import and run a stale, previously-installed copy instead of
# the checkout it was actually launched from, with no visible error.
# Reversed here so the intended priority order (source checkout, then
# /usr/local/..., then /usr/...) is what's actually checked first.
# Python has already put this script's folder first in sys.path itself:
# skipping a candidate that's "already there" left _HERE BEHIND the two
# install folders - a checkout ran the installed copy whenever there was
# one. Each candidate is moved to the front instead.
for _candidate in reversed((_HERE, "/usr/local/lib/plunderhub", "/usr/lib/plunderhub")):
    while _candidate in sys.path:
        sys.path.remove(_candidate)
    sys.path.insert(0, _candidate)

# GNOME on Wayland lets no program bring another one's window to the
# front - only X11 windows (_NET_ACTIVE_WINDOW): there PlunderHub runs on
# XWayland, so the controller's switch between a game and PlunderHub works
# both ways. Before GTK starts (the check below already starts it);
# gi_setup takes GDK_BACKEND out again once it's up, so the games and
# launchers PlunderHub starts don't get it. PLUNDERHUB_WAYLAND=1: native.
if (os.environ.get("XDG_SESSION_TYPE") == "wayland" and os.environ.get("DISPLAY")
        and "gnome" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
        and not os.environ.get("GDK_BACKEND") and os.environ.get("PLUNDERHUB_WAYLAND") != "1"):
    os.environ["GDK_BACKEND"] = "x11"
    os.environ["PLUNDERHUB_X11_ON_GNOME"] = "1"

# Checked before anything from plunderhub.ui/plunderhub.gi_setup is ever
# imported - GTK 3 and PyYAML are hard dependencies PlunderHub can't run
# without at all, so this has to happen before the app itself (which
# would otherwise crash with a bare Python traceback on the very first
# `import gi`) is ever touched. dependency_check.py is itself entirely
# standalone (no gi/GTK import of its own beyond the check attempts it
# makes internally), so it's always safe to import and call here.
from plunderhub.core.dependency_check import (
    HARD_DEPS, detect_pkg_manager, missing_dependencies, prompt_and_maybe_install,
)


def _check_hard_dependencies_or_exit():
    missing = missing_dependencies(HARD_DEPS)
    if not missing:
        return
    pkg_manager, distro_name = detect_pkg_manager()
    if not prompt_and_maybe_install(pkg_manager, missing, distro_name):
        sys.exit(1)
    # Re-checked independently rather than trusting prompt_and_maybe_
    # install's own return value blindly - belt and suspenders, since
    # this is the one check standing between "starts fine" and "crashes
    # with a bare traceback" for every launch from here on.
    if missing_dependencies(HARD_DEPS):
        sys.exit(1)


_check_hard_dependencies_or_exit()

from plunderhub.ui.app import main

if __name__ == "__main__":
    main()
