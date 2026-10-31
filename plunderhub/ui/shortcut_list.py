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
"""Every shortcut, in groups - one list for the shortcuts window
(DialogsMixin._build_shortcuts_panels) and Ship's Controls' Console &
controls page, so the two never tell different stories.

Each group: (title key, icon, [(keys, what it does key), ...]). A
keyboard entry's keys are the caps pressed together; a controller's are
(Xbox, PlayStation)."""

KEYBOARD = [
    ("sc_group_move", "input-dpad-symbolic", [
        (("↑", "↓", "←", "→"), "sc_move"),
        (("Enter",), "sc_launch"),
        (("Esc",), "sc_back_close"),
        (("1-9",), "shortcut_places"),
        (("B",), "sc_top"),
    ]),
    ("sc_group_games", "applications-games-symbolic", [
        (("Menu",), "shortcut_menu"),
        (("F",), "shortcut_favorite"),
        (("V",), "selection_mode_btn"),
        (("E",), "reorder_menu"),
    ]),
    ("sc_group_views", "view-grid-symbolic", [
        (("G",), "view_grid_tooltip"),
        (("L",), "view_list_tooltip"),
        (("Ctrl", "F"), "sc_search"),
        (("F5",), "sc_refresh"),
    ]),
    ("sc_group_open", "app-window-symbolic", [
        (("S",), "sw_title"),
        (("D",), "sc_dashboard"),
        (("O",), "sc_settings"),
        (("C",), "sc_console"),
        (("F1",), "sc_this_list"),
    ]),
    # in Console Mode the arrows, Enter and Esc as above; these on top
    ("settings_console_group", "input-gaming-symbolic", [
        (("Q",), "sc_prev_launcher"),
        (("E",), "sc_next_launcher"),
        (("F",), "sc_favorite"),
        (("S",), "sc_sort"),
    ]),
]

# groups shown as one card across the shortcuts window, under the two
# columns, their rows side by side - two columns of cards came out too
# uneven (and too tall for 1080p) with them in one
WIDE = {"settings_console_group", "sc_group_in_game"}

CONTROLLER = [
    ("sc_group_move", "input-dpad-symbolic", [
        (("D-pad", "D-pad"), "sc_move"),
        (("Stick", "Stick"), "sc_scroll"),
        (("A", "✕"), "sc_gp_pick"),
        (("B", "○"), "sc_back"),
    ]),
    ("settings_console_group", "input-gaming-symbolic", [
        (("LB", "L1"), "sc_prev_launcher"),
        (("RB", "R1"), "sc_next_launcher"),
        (("X", "□"), "sc_favorite"),
        (("Y", "△"), "sc_sort"),
    ]),
    ("sc_group_open", "app-window-symbolic", [
        (("View", "Share"), "sc_settings"),
        (("Start", "Options"), "sc_console"),
    ]),
    # held together for a second (input/joystick.py CHORD_HOLD_S)
    ("sc_group_in_game", "applications-games-symbolic", [
        (("View+Start", "Share+Options"), "sc_game_switch"),
    ]),
]

# buttons pressed together ("+" between them): their own keys column, so
# the other rows' don't widen with them
OWN_KEYS_COLUMN = {"sc_group_in_game"}

# the face buttons in the colours printed on the pads
FACE_COLOURS = {"A": "#6cc24a", "B": "#ef5f57", "X": "#4a9ff0", "Y": "#f2c94c",
                "✕": "#7aa7ff", "○": "#ff7b7b", "□": "#f3a6d6", "△": "#46d6ad"}
