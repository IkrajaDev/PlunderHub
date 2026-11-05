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

"""Makes the pictures Steam shows for PlunderHub as a non-Steam game
(plunderhub/assets/steam/): capsule 600x900, wide 920x430, hero
1920x620, logo 1280x360 (see-through), icon 256x256. From the app icon
and the title logo, on the Pirate theme's colours.

    python3 tools/make_steam_art.py
"""
from pathlib import Path

import cairo
import gi

gi.require_version("Rsvg", "2.0")
from gi.repository import Rsvg  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
ICON = REPO / "plunderhub" / "assets" / "PlunderHub_ikon.svg"
TITLE = REPO / "docs" / "PlunderHub_title.svg"
OUT = REPO / "plunderhub" / "assets" / "steam"
BG_TOP, BG_BOTTOM = (0x0b / 255, 0x24 / 255, 0x26 / 255), (0x07 / 255, 0x1a / 255, 0x1b / 255)
ACCENT = (0x22 / 255, 0xd3 / 255, 0xac / 255)


def _svg(path: Path):
    handle = Rsvg.Handle.new_from_file(str(path))
    ok, w, h = handle.get_intrinsic_size_in_pixels()
    if not ok:
        dim = handle.get_dimensions()
        w, h = dim.width, dim.height
    return handle, w, h


def _draw_svg(cr, path: Path, x: float, y: float, width: float, height: float) -> None:
    """The svg fitted into the box, centred."""
    handle, w, h = _svg(path)
    scale = min(width / w, height / h)
    vw, vh = w * scale, h * scale
    rect = Rsvg.Rectangle()
    rect.x, rect.y, rect.width, rect.height = x + (width - vw) / 2, y + (height - vh) / 2, vw, vh
    handle.render_document(cr, rect)


def _background(cr, w: int, h: int, glow_x: float, glow_y: float, glow_r: float) -> None:
    grad = cairo.LinearGradient(0, 0, 0, h)
    grad.add_color_stop_rgb(0, *BG_TOP)
    grad.add_color_stop_rgb(1, *BG_BOTTOM)
    cr.set_source(grad)
    cr.paint()
    glow = cairo.RadialGradient(glow_x, glow_y, 0, glow_x, glow_y, glow_r)
    glow.add_color_stop_rgba(0, *ACCENT, 0.28)
    glow.add_color_stop_rgba(1, *ACCENT, 0.0)
    cr.set_source(glow)
    cr.paint()


def _save(name: str, w: int, h: int, draw) -> None:
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
    draw(cairo.Context(surface), w, h)
    surface.write_to_png(str(OUT / name))
    print(f"{name}: {w}x{h}")


def capsule(cr, w, h):
    _background(cr, w, h, w / 2, h * 0.38, w * 0.75)
    _draw_svg(cr, ICON, w * 0.14, h * 0.13, w * 0.72, w * 0.72)
    _draw_svg(cr, TITLE, w * 0.08, h * 0.70, w * 0.84, h * 0.16)


def wide(cr, w, h):
    _background(cr, w, h, w * 0.27, h / 2, h * 0.9)
    _draw_svg(cr, ICON, h * 0.12, h * 0.12, h * 0.76, h * 0.76)
    _draw_svg(cr, TITLE, h * 0.98, h * 0.25, w - h * 1.1, h * 0.5)


def hero(cr, w, h):
    # Steam lays the logo over the hero's left: only the backdrop and a big faint icon on the right
    _background(cr, w, h, w * 0.72, h * 0.5, h * 1.2)
    cr.push_group()
    _draw_svg(cr, ICON, w * 0.58, -h * 0.15, h * 1.3, h * 1.3)
    cr.pop_group_to_source()
    cr.paint_with_alpha(0.35)


def logo(cr, w, h):
    _draw_svg(cr, TITLE, 0, 0, w, h)


def icon(cr, w, h):
    _draw_svg(cr, ICON, 0, 0, w, h)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    _save("capsule.png", 600, 900, capsule)
    _save("wide.png", 920, 430, wide)
    _save("hero.png", 1920, 620, hero)
    _save("logo.png", 1280, 360, logo)
    _save("icon.png", 256, 256, icon)
