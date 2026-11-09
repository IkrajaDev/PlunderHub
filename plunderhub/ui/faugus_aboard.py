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

"""The Treasure Map's Faugus tab: the "Bring aboard" card (a sea chart's
compass rose round PlunderHub's skull), the window that asks what to
bring (a store's launcher, a game's installer, a game on disk, a Linux
game), the .exe check after an installer, and the install itself on
the tile and the dock (core/faugus_add does the work).

No store logos are shipped: a launcher shows as a monogram in its own
colour; once installed, the tile wears the icon read out of its .exe on
the player's own machine."""

import math
import random
import os
import re
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional

import cairo

from plunderhub.gi_setup import Gdk, GdkPixbuf, GLib, Gtk, Pango
from plunderhub.logging_setup import logger
from plunderhub.ui.home_mixin import _accent_rgb, _on_accent_rgb, _pango_layout, _pango_text, _rounded, _title_gradient
from plunderhub.core import faugus_add as fa

# a launcher's monogram + its own icon colour (looked up 2026-11-07): Battle.net #148EFF
# (its icon), EA blue #255AF6 (its icon), Epic #313131 (Simple Icons, from Epic's guidelines), GOG
# #86328A (Simple Icons, from CD Projekt's logotypes), Ubisoft Connect blue ~#0070FF,
# Rockstar #FCAF17 (Simple Icons), Amazon orange #FF9900, Wargaming red ~#C8102E
MONOGRAM = {
    "battle": ("B", "#148EFF"), "ea": ("EA", "#255AF6"), "epic": ("E", "#313131"),
    "gog": ("G", "#86328A"), "ubisoft": ("U", "#0070FF"), "rockstar": ("R", "#FCAF17"),
    "amazon": ("a", "#FF9900"), "wargaming": ("W", "#C8102E"),
}
KIND_ICON = {"setup": "system-software-install-symbolic", "exe": "application-x-executable-symbolic",
             "linux": "computer-symbolic"}
STORE_SUB = {"battle": "tm_fg_sub_battle", "ea": "tm_fg_sub_ea", "epic": "tm_fg_sub_heroic",
             "gog": "tm_fg_sub_heroic", "ubisoft": "tm_fg_sub_ubisoft", "rockstar": "tm_fg_sub_rockstar",
             "amazon": "tm_fg_sub_heroic", "wargaming": "tm_fg_sub_wargaming"}
HEROIC_STORE = {"epic": "Epic Games", "gog": "GOG", "amazon": "Amazon"}
RUNTIME_MB = 700           # umu's Steam Runtime, fetched on its first run
ADD_UID = "faugus:+add"


def _hex(v):
    v = v.lstrip("#")
    return tuple(int(v[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _mix(a, b, t):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


_LOGO = {}


def _logo(px):
    """PlunderHub's skull at px, in the theme's colour (cached per size + accent)."""
    from plunderhub.theme import logo_accent, logo_at_size
    key = (px, logo_accent())
    if key not in _LOGO:
        _LOGO.clear()
        _LOGO[key] = logo_at_size(px)
    return _LOGO[key]


def _serif(cr, text, cx, cy, size, rgba):
    """A letter in an italic serif, centred on (cx, cy) - the old charts' N E S W."""
    from gi.repository import PangoCairo
    layout = PangoCairo.create_layout(cr)
    desc = Pango.FontDescription.from_string("Serif Italic")
    desc.set_absolute_size(size * Pango.SCALE)
    layout.set_font_description(desc)
    layout.set_text(text, -1)
    lw, lh = layout.get_pixel_size()
    cr.move_to(cx - lw / 2, cy - lh / 2)
    cr.set_source_rgba(*rgba)
    PangoCairo.show_layout(cr, layout)


def draw_add_card(cr, widget, x, y, w, h, c, accent, hover, f, big=False,
                  words=("Bring aboard", "Launcher · installer · game")):
    """An old sea chart's compass rose: long thin needles in three lengths,
    each half lit, half engraved shade; a ring band behind; N E S W in an
    italic serif at the tips; the PlunderHub skull in the heart; its name
    on an accent pill with a "+" (this card adds a game)."""
    lift = 4 if hover else 0
    y -= lift
    for i, a in ((10, 0.05), (6, 0.08), (3, 0.12)):
        _rounded(cr, x - i * 0.3, y + i * 0.9, w + i * 0.6, h + i * 0.4, 14 + i * 0.4)
        cr.set_source_rgba(0, 0, 0, a * (1.6 if hover else 1))
        cr.fill()
    cr.save()
    _rounded(cr, x, y, w, h, 14)
    cr.clip()
    cx, cy = x + w / 2, y + h * 0.39
    fg = c["fg"]
    cr.set_source_rgba(*_mix(c["bg"], c["raised"], 0.6), 1)
    cr.paint()
    glow = cairo.RadialGradient(cx, cy, w * 0.05, cx, cy, max(w, h) * 0.7)
    glow.add_color_stop_rgba(0, *accent, 0.30 if hover else 0.20)
    glow.add_color_stop_rgba(0.5, *accent, 0.06)
    glow.add_color_stop_rgba(1, *accent, 0)
    cr.set_source(glow)
    cr.paint()
    # faint rhumb lines out to the edges
    cr.save()
    cr.set_line_width(1)
    cr.set_dash([2, 5])
    for i in range(16):
        ang = i * math.pi / 8
        cr.move_to(cx + math.cos(ang) * w * 0.30, cy + math.sin(ang) * w * 0.30)
        cr.line_to(cx + math.cos(ang) * w * 1.4, cy + math.sin(ang) * w * 1.4)
    cr.set_source_rgba(*fg, 0.07)
    cr.stroke()
    cr.restore()
    # the ring band behind the needles: a pale band between two thin circles
    rb_out, rb_in = w * 0.285, w * 0.235
    cr.arc(cx, cy, rb_out, 0, 2 * math.pi)
    cr.arc_negative(cx, cy, rb_in, 2 * math.pi, 0)
    cr.set_source_rgba(*fg, 0.10)
    cr.fill()
    cr.set_line_width(1.2)
    for rr, a in ((rb_out, 0.45), (rb_in, 0.35), (w * 0.21, 0.25), (w * 0.31, 0.18)):
        cr.arc(cx, cy, rr, 0, 2 * math.pi)
        cr.set_source_rgba(*fg, a)
        cr.stroke()

    lit = _mix(fg, accent, 0.25)
    dark = _mix(c["bg"], accent, 0.35)

    def needle(ang, length, width, alpha):
        tipx, tipy = cx + math.cos(ang) * length, cy + math.sin(ang) * length
        bx, by = math.cos(ang + math.pi / 2) * width, math.sin(ang + math.pi / 2) * width
        # lit half
        cr.move_to(cx, cy)
        cr.line_to(cx - bx, cy - by)
        cr.line_to(tipx, tipy)
        cr.close_path()
        cr.set_source_rgba(*lit, alpha)
        cr.fill()
        # shaded half with engraved hatching
        cr.move_to(cx, cy)
        cr.line_to(cx + bx, cy + by)
        cr.line_to(tipx, tipy)
        cr.close_path()
        cr.set_source_rgba(*dark, alpha)
        cr.fill_preserve()
        cr.save()
        cr.clip()
        cr.set_line_width(0.8)
        for k in range(1, 14):
            t = k / 14
            px, py = cx + (tipx - cx) * t, cy + (tipy - cy) * t
            cr.move_to(px, py)
            cr.line_to(px + bx * (1 - t) * 1.2, py + by * (1 - t) * 1.2)
        cr.set_source_rgba(*fg, 0.25 * alpha)
        cr.stroke()
        cr.restore()
        # a crisp outline
        cr.move_to(cx - bx, cy - by)
        cr.line_to(tipx, tipy)
        cr.line_to(cx + bx, cy + by)
        cr.set_line_width(0.8)
        cr.set_source_rgba(*fg, 0.5 * alpha)
        cr.stroke()

    L = w * 0.37                                   # N E S W reach the letters
    for i in range(16):                            # the shortest, between all
        needle(i * math.pi / 8 + math.pi / 16, L * 0.62, w * 0.012, 0.7)
    for i in range(4):                             # the diagonals
        needle(i * math.pi / 2 + math.pi / 4, L * 0.86, w * 0.022, 0.9)
    for i in range(4):                             # the cardinals, longest
        needle(i * math.pi / 2, L, w * 0.03, 1.0)
    # N E S W in an italic serif just past the tips
    ls = max(11, w * 0.085)
    for i, ch in enumerate(("E", "S", "W", "N")):
        ang = i * math.pi / 2
        d = L + ls * 0.62
        _serif(cr, ch, cx + math.cos(ang) * d, cy + math.sin(ang) * d, ls,
               (*accent, 1) if ch == "N" else (*fg, 0.8))
    # the skull: PlunderHub's own logo, with a soft shadow
    size = int(w * (0.40 if hover else 0.37))
    pb = _logo(size)
    if pb is not None:
        d = widget.get_scale_factor()
        lx, ly = cx - size / 2, cy - size / 2
        cr.arc(cx, cy + size * 0.06, size * 0.42, 0, 2 * math.pi)
        sh = cairo.RadialGradient(cx, cy + size * 0.06, size * 0.1, cx, cy + size * 0.06, size * 0.48)
        sh.add_color_stop_rgba(0, 0, 0, 0, 0.45)
        sh.add_color_stop_rgba(1, 0, 0, 0, 0)
        cr.set_source(sh)
        cr.fill()
        Gdk.cairo_set_source_pixbuf(cr, pb, lx, ly)
        cr.paint()
    # the words, on a soft dark foot
    foot = cairo.LinearGradient(0, y + h * 0.72, 0, y + h)
    foot.add_color_stop_rgba(0, *c["bg"], 0)
    foot.add_color_stop_rgba(0.35, *c["bg"], 0.75)
    foot.add_color_stop_rgba(1, *c["bg"], 0.9)
    cr.set_source(foot)
    cr.rectangle(x, y + h * 0.72, w, h * 0.28)
    cr.fill()
    kk = min(1.6, max(0.8, w / 300)) * f * (1.25 if big else 1)
    t1 = min(max(13.5 * kk, 10), w / 11)
    t2 = min(max(9.5 * kk, 8), w / 17)
    # the name on an accent pill with its "+": the card's button
    layout = _pango_layout(cr, widget, words[0], w - 20, t1, True, 1, False)
    text_w, text_h = layout.get_pixel_size()
    pill_h = text_h * 1.55
    plus = text_h * 0.62
    gap = text_h * 0.38
    pad = text_h * 0.72
    pill_w = min(w - 16, plus + gap + text_w + 2 * pad)
    px, py = x + (w - pill_w) / 2, y + h * 0.76
    on_accent = _on_accent_rgb(accent)
    _rounded(cr, px, py, pill_w, pill_h, pill_h / 2)
    cr.set_source_rgba(*accent, 1)
    cr.fill()
    mid_x, mid_y, arm = px + pad + plus / 2, py + pill_h / 2, plus / 2
    cr.set_line_width(max(2, text_h * 0.15))
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.move_to(mid_x - arm, mid_y)
    cr.line_to(mid_x + arm, mid_y)
    cr.move_to(mid_x, mid_y - arm)
    cr.line_to(mid_x, mid_y + arm)
    cr.set_source_rgba(*on_accent, 1)
    cr.stroke()
    _pango_text(cr, widget, words[0], px + pad + plus + gap, py + (pill_h - text_h) / 2, text_w + 2, t1, True,
                (*on_accent, 1))
    _pango_text(cr, widget, words[1], x + 10, py + pill_h + text_h * 0.45, w - 20, t2, False,
                (*fg, 0.65), align_center=True)
    cr.restore()


def draw_monogram(cr, widget, x, y, s, kind):
    if kind in MONOGRAM:
        letters, col = MONOGRAM[kind]
        cr.arc(x + s / 2, y + s / 2, s / 2, 0, 2 * math.pi)
        cr.set_source_rgba(*_hex(col), 1)
        cr.fill()
        rgb = _hex(col)
        if 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2] < 0.25:
            cr.arc(x + s / 2, y + s / 2, s / 2 - 0.75, 0, 2 * math.pi)
            cr.set_source_rgba(1, 1, 1, 0.35)
            cr.set_line_width(1.5)
            cr.stroke()
        size = s * (0.24 if len(letters) > 1 else 0.32)
        _pango_text(cr, widget, letters, x, y + s / 2 - size * 0.72, s, size, True, (0.1, 0.1, 0.1, 1) if rgb[0] + rgb[1] > 1.6 and rgb[2] < 0.3 else (1, 1, 1, 1),
                    align_center=True)
        return
    accent = _accent_rgb(widget)
    cr.arc(x + s / 2, y + s / 2, s / 2, 0, 2 * math.pi)
    cr.set_source_rgba(*accent, 0.22)
    cr.fill()
    try:
        theme = Gtk.IconTheme.get_default()
        info = theme.lookup_icon(KIND_ICON[kind], int(s * 0.55), 0)
        rgba = Gdk.RGBA(*accent, 1)
        pb, _sym = info.load_symbolic(rgba, None, None, None)
        Gdk.cairo_set_source_pixbuf(cr, pb, x + (s - pb.get_width()) / 2, y + (s - pb.get_height()) / 2)
        cr.paint()
    except Exception:
        pass


def monogram_widget(kind, s):
    da = Gtk.DrawingArea()
    da.set_size_request(s, s)
    da.set_valign(Gtk.Align.CENTER)
    da.connect("draw", lambda w, cr: draw_monogram(cr, w, 0, 0, s, da._kind) or False)
    da._kind = kind
    return da


def _star(cr, x, y, r, turn=0.0):
    for i in range(10):
        a = turn - math.pi / 2 + i * math.pi / 5
        d = r if i % 2 == 0 else r * 0.42
        cr.line_to(x + d * math.cos(a), y + d * math.sin(a))
    cr.close_path()


def _chevron(cr, x, y, half, tall, thick, up=False):
    """A V (or, up, a Λ) centred on x, its arms' top edge starting at y."""
    tip = -tall if up else tall
    cr.move_to(x - half, y)
    cr.line_to(x, y + tip)
    cr.line_to(x + half, y)
    cr.line_to(x + half, y + thick)
    cr.line_to(x, y + tip + thick)
    cr.line_to(x - half, y + thick)
    cr.close_path()


def _orbits(cr, cx, cy, u):
    """Battle.net: three orbits round the mark, a field of small stars."""
    cr.set_line_width(2.4 * u)
    for turn, alpha in ((-0.55, 0.34), (0.6, 0.26), (1.57, 0.18)):
        cr.save()
        cr.translate(cx, cy)
        cr.rotate(turn)
        cr.scale(1, 0.42)
        cr.arc(0, 0, 300 * u, 0, 2 * math.pi)
        cr.restore()
        cr.set_source_rgba(1, 1, 1, alpha)
        cr.stroke()


def _streaks(cr, w, h, u):
    """EA: slanted speed streaks across the card."""
    for x, width, alpha in ((-0.25, 0.30, 0.13), (0.15, 0.09, 0.20), (0.34, 0.20, 0.09), (0.72, 0.13, 0.17),
                            (0.95, 0.34, 0.08)):
        lean = h * 0.55
        cr.move_to(x * w + lean, 0)
        cr.line_to((x + width) * w + lean, 0)
        cr.line_to((x + width) * w - lean * 0.2, h)
        cr.line_to(x * w - lean * 0.2, h)
        cr.close_path()
        cr.set_source_rgba(1, 1, 1, alpha)
        cr.fill()


def _chevrons(cr, w, h, cx, u):
    """Epic: a light from above, chevrons stacked down the card."""
    beam = cairo.LinearGradient(cx - 190 * u, 0, cx + 190 * u, 0)
    beam.add_color_stop_rgba(0, 1, 1, 1, 0)
    beam.add_color_stop_rgba(0.5, 1, 1, 1, 0.20)
    beam.add_color_stop_rgba(1, 1, 1, 1, 0)
    cr.set_source(beam)
    cr.rectangle(0, 0, w, h)
    cr.fill()
    for i in range(7):
        _chevron(cr, cx, h * 0.04 + i * 92 * u, 250 * u, 74 * u, 15 * u)
        cr.set_source_rgba(1, 1, 1, 0.13 - i * 0.012)
        cr.fill()


def _rings(cr, cx, cy, u):
    """GOG: rings spreading out from the mark."""
    for i in range(1, 9):
        cr.arc(cx, cy, (118 + 50 * i) * u, 0, 2 * math.pi)
        cr.set_line_width((17 - i * 1.5) * u)
        cr.set_source_rgba(0.93, 0.45, 1, 0.26 - i * 0.024)
        cr.stroke()


def _spiral(cr, cx, cy, u):
    """Ubisoft: colour bands swirling round the mark."""
    colours = ((1, 0.27, 0.45), (1, 0.66, 0.15), (0.3, 0.95, 0.55), (0.25, 0.85, 1))
    for n, col in enumerate(colours):
        start = n * math.pi / 2
        cr.new_path()
        for step in range(150):
            t = step / 149
            a = start + t * 3.3 * math.pi
            d = (135 + 420 * t) * u
            cr.line_to(cx + d * math.cos(a), cy + d * math.sin(a))
        cr.set_line_width(26 * u)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_source_rgba(*col, 0.27)
        cr.stroke()


def _stars(cr, w, h, cx, cy, u, kind):
    """Rockstar: rays from the mark, scattered stars."""
    for i in range(14):
        a = i * math.pi / 7 + 0.12
        cr.move_to(cx, cy)
        cr.line_to(cx + 1100 * u * math.cos(a - 0.09), cy + 1100 * u * math.sin(a - 0.09))
        cr.line_to(cx + 1100 * u * math.cos(a + 0.09), cy + 1100 * u * math.sin(a + 0.09))
        cr.close_path()
        cr.set_source_rgba(1, 1, 1, 0.11)
        cr.fill()
    rnd = random.Random(kind)
    for _ in range(int(26 * max(1.0, w / h))):
        x, y = rnd.uniform(0, w), rnd.uniform(0, h)
        if math.hypot(x - cx, y - cy) < 160 * u:
            continue
        _star(cr, x, y, rnd.uniform(9, 28) * u, rnd.uniform(-0.5, 0.5))
        cr.set_source_rgba(0.12, 0.06, 0, rnd.uniform(0.16, 0.34))
        cr.fill()


def _smiles(cr, w, h, u):
    """Amazon: wide warm curves sweeping across."""
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    for i in range(6):
        y = h * 0.03 + i * 118 * u
        cr.move_to(-40 * u, y)
        cr.curve_to(w * 0.28, y + 135 * u, w * 0.72, y + 135 * u, w + 40 * u, y - 20 * u)
        cr.set_line_width((30 - i * 2.2) * u)
        cr.set_source_rgba(1, 0.96, 0.82, 0.21 - i * 0.018)
        cr.stroke()


def _arrows(cr, w, h, u):
    """Wargaming: arrows rising up the card, sparks with them."""
    rows = int(h / (112 * u)) + 2
    columns = max(2, round(w / (250 * u)))
    for col in range(columns):
        x = (col + 0.5) * w / columns
        for row in range(rows):
            y = h + 40 * u - row * 112 * u - col % 2 * 56 * u
            _chevron(cr, x, y, 92 * u, 66 * u, 22 * u, up=True)
            cr.set_source_rgba(0, 0, 0, 0.13)
            cr.fill()
    rnd = random.Random("wargaming")
    for _ in range(int(34 * max(1.0, w / h))):
        cr.arc(rnd.uniform(0, w), rnd.uniform(0, h), rnd.uniform(1.6, 4.4) * u, 0, 2 * math.pi)
        cr.set_source_rgba(1, 0.78, 0.5, rnd.uniform(0.28, 0.66))
        cr.fill()


def draw_launcher_backdrop(cr, w, h, cx, cy, kind):
    """A store launcher's card behind its logo, a look of its own for each:
    its colour, lit from the corner, and a pattern round the logo (cx, cy)."""
    col = _hex(MONOGRAM[kind][1])
    u = min(w, h) / 460
    base = cairo.LinearGradient(0, 0, w * 0.75, h)
    base.add_color_stop_rgba(0, *_mix(col, (1, 1, 1), 0.10), 1)
    base.add_color_stop_rgba(0.5, *col, 1)
    base.add_color_stop_rgba(1, *(v * 0.34 for v in col), 1)
    cr.set_source(base)
    cr.paint()
    if kind == "battle":
        rnd = random.Random(kind)
        for _ in range(int(60 * max(1.0, w / h))):
            cr.arc(rnd.uniform(0, w), rnd.uniform(0, h), rnd.uniform(1.0, 3.4) * u, 0, 2 * math.pi)
            cr.set_source_rgba(1, 1, 1, rnd.uniform(0.25, 0.8))
            cr.fill()
        _orbits(cr, cx, cy, u)
    elif kind == "ea":
        _streaks(cr, w, h, u)
    elif kind == "epic":
        _chevrons(cr, w, h, cx, u)
    elif kind == "gog":
        _rings(cr, cx, cy, u)
    elif kind == "ubisoft":
        _spiral(cr, cx, cy, u)
    elif kind == "rockstar":
        _stars(cr, w, h, cx, cy, u, kind)
    elif kind == "amazon":
        _smiles(cr, w, h, u)
    elif kind == "wargaming":
        _arrows(cr, w, h, u)
    bloom = cairo.RadialGradient(cx, cy, 40 * u, cx, cy, 300 * u)
    bloom.add_color_stop_rgba(0, *_mix(col, (1, 1, 1), 0.55), 0.26)
    bloom.add_color_stop_rgba(1, *_mix(col, (1, 1, 1), 0.55), 0)
    cr.set_source(bloom)
    cr.paint()


def launcher_cover(kind: str, cache: Path, widget: Gtk.Widget) -> Optional[str]:
    """A store launcher's tile art: its colour + monogram, drawn once into
    the cache (3:4, like every cover)."""
    out = cache / f"faugus-card-{kind}.png"
    if out.exists():
        return str(out)
    try:
        w, h = 450, 600
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        cr = cairo.Context(surf)
        draw_launcher_backdrop(cr, w, h, w / 2, h * 0.40, kind)
        s = w * 0.42
        cr.arc(w / 2, h * 0.40, s / 2 + 10, 0, 2 * math.pi)
        cr.set_source_rgba(1, 1, 1, 0.16)
        cr.fill()
        draw_monogram(cr, widget, (w - s) / 2, h * 0.40 - s / 2, s, kind)
        cache.mkdir(parents=True, exist_ok=True)
        surf.write_to_png(str(out))
        return str(out)
    except Exception as e:
        logger.debug(f"Faugus: launcher art for {kind} not drawn: {e}")
        return None


def _draw_launcher_mark(cr, widget, cx: float, cy: float, s: float, kind: str, icon: Path, ring: float):
    """The launcher's own logo, large with a soft shadow - or, without
    one, its monogram in a ring."""
    from plunderhub.gi_setup import GdkPixbuf
    logo = None
    if icon.is_file():
        try:
            side = round(s * 1.3)
            logo = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(icon), side, side, True)
        except Exception as e:
            logger.debug(f"Faugus: logo {icon} not readable: {e}")
    if logo is not None:
        reach = s * 0.82
        shadow = cairo.RadialGradient(cx, cy + s * 0.05, s * 0.35, cx, cy + s * 0.05, reach)
        shadow.add_color_stop_rgba(0, 0, 0, 0, 0.36)
        shadow.add_color_stop_rgba(1, 0, 0, 0, 0)
        cr.set_source(shadow)
        cr.arc(cx, cy + s * 0.05, reach, 0, 2 * math.pi)
        cr.fill()
        Gdk.cairo_set_source_pixbuf(cr, logo, cx - logo.get_width() / 2, cy - logo.get_height() / 2)
        cr.paint()
        return
    cr.arc(cx, cy, s / 2 + ring, 0, 2 * math.pi)
    cr.set_source_rgba(1, 1, 1, 0.16)
    cr.fill()
    draw_monogram(cr, widget, cx - s / 2, cy - s / 2, s, kind)


def write_launcher_art(site: fa.FaugusSite, gid: str, kind: str, widget: Gtk.Widget) -> Optional[str]:
    """A store launcher's cover (460x690, Faugus' own size) and banner, into
    Faugus' covers/ and banners/ - Faugus shows them, and the library takes
    them from there (it looks no art up for a launcher). The cover's path."""
    covers, banners = site.data / "covers", site.data / "banners"
    covers.mkdir(parents=True, exist_ok=True)
    banners.mkdir(parents=True, exist_ok=True)
    for w, h, folder in ((460, 690, covers), (1920, 620, banners)):
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        cr = cairo.Context(surf)
        if w < h:                                       # the cover: the monogram (the name: the tile's own)
            s = w * 0.42
            cx, cy = w / 2, h * 0.40
        else:                                           # the banner: the monogram on the right
            s = h * 0.46
            cx, cy = w * 0.78, h * 0.5
        draw_launcher_backdrop(cr, w, h, cx, cy, kind)
        _draw_launcher_mark(cr, widget, cx, cy, s, kind, site.data / "icons" / f"{gid}.png", 12)
        surf.write_to_png(str(folder / f"{gid}.png"))
    return str(covers / f"{gid}.png")


def render_add_tile(grid, it, c, accent, hover) -> cairo.ImageSurface:
    m, w, h = grid.M, it.w, it.h
    d = grid.get_scale_factor()
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, int(w + 2 * m) * d, int(h + 2 * m) * d)
    surf.set_device_scale(d, d)
    cr = cairo.Context(surf)
    app = grid.app
    draw_add_card(cr, grid, m, m, w, h, c, accent, hover, app._text_factor(),
                  words=(app._("tm_fg_bring"), app._("tm_fg_bring_sub")))
    return surf


def draw_running_chip(grid, cr, x, y, w, accent, f):
    """"Running" on a tile's top right: a program is up in its prefix."""
    text = grid.app._("tm_fg_running")
    from plunderhub.ui.treasure_map import _text_w
    size = 9.5 * f
    tw = _text_w(grid, cr, text, size, True) + 26 * f
    th = 22 * f
    px, py = x + w - tw - 10, y + 10
    _rounded(cr, px, py, tw, th, th / 2)
    cr.set_source_rgba(0, 0, 0, 0.6)
    cr.fill()
    cr.arc(px + 11 * f, py + th / 2, 3.5 * f, 0, 2 * math.pi)
    cr.set_source_rgba(*accent, 1)
    cr.fill()
    _pango_text(cr, grid, text, px + 18 * f, py + (th - size * 1.45) / 2, tw - 22 * f, size, True, (1, 1, 1, 0.95))


def draw_phase_progress(grid, cr, x, y, w, h, dl, accent, f):
    """A Faugus install on its tile: the cover comes to light from the
    bottom up as it goes on, the percent and what it does now over it."""
    app = grid.app
    frac = max(0.0, min(1.0, dl["frac"]))
    grid.reveal(cr, x, y, w, h, frac, accent, f)
    px, py, pw, _ph = grid._panel(cr, x, y, w, h, 74 * f)
    _pango_text(cr, grid, f"{int(frac * 100)} %", px + 10, py + 9 * f, pw - 20, 18 * f, True,
                (1, 1, 1, 0.98), align_center=True)
    _pango_text(cr, grid, app._(f"tm_fg_phase_{dl.get('phase') or 'prepare'}"), px + 10, py + 38 * f, pw - 20,
                9.5 * f, False, (1, 1, 1, 0.8), lines=2, align_center=True)


# --- the tab's own states --------------------------------------------------------------------

def show_empty(view):
    """No Faugus game yet: only a big Bring aboard card, a line under it."""
    app = view.app
    for child in view.state_card.get_children():
        view.state_card.remove(child)
    f = app._text_factor()
    w = int(app.cover_size * 1.35)
    h = int(w * 4 / 3)
    area = Gtk.DrawingArea()
    area.set_size_request(w + 40, h + 40)
    area.set_halign(Gtk.Align.CENTER)
    area.set_can_focus(True)
    area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.ENTER_NOTIFY_MASK
                    | Gdk.EventMask.LEAVE_NOTIFY_MASK | Gdk.EventMask.KEY_PRESS_MASK)
    area._hover = False

    def draw(widget, cr):
        draw_add_card(cr, widget, 20, 20, w, h, view.colors(), _accent_rgb(widget), widget._hover or widget.has_focus(),
                      f, big=True, words=(app._("tm_fg_bring"), app._("tm_fg_bring_sub")))
        return False

    def hover(widget, on):
        widget._hover = on
        widget.queue_draw()

    def key(_w, ev):
        if ev.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
            open_add(view)
            return True
        return False
    area.connect("draw", draw)
    area.connect("enter-notify-event", lambda w, _e: hover(w, True))
    area.connect("leave-notify-event", lambda w, _e: hover(w, False))
    area.connect("focus-in-event", lambda w, _e: w.queue_draw() and False)
    area.connect("focus-out-event", lambda w, _e: w.queue_draw() and False)
    area.connect("button-press-event", lambda *_: open_add(view) or True)
    area.connect("key-press-event", key)
    view.state_card.pack_start(area, False, False, 0)
    head = Gtk.Label(label=app._("tm_fg_empty_t"))
    head.get_style_context().add_class("tm-state-title")
    text = Gtk.Label(label=app._("tm_fg_empty_s"))
    text.set_justify(Gtk.Justification.CENTER)
    text.set_line_wrap(True)
    text.set_max_width_chars(60)
    text.get_style_context().add_class("tm-state-text")
    view.state_card.pack_start(head, False, False, 0)
    view.state_card.pack_start(text, False, False, 0)
    view.state_card.show_all()
    view.body.set_visible_child_name("state")
    view._state_shown = ("faugus", "empty")


def _card_window(view, width: int) -> Gtk.Dialog:
    from plunderhub.ui.treasure_map import _corner
    dlg = Gtk.Dialog(transient_for=view.get_toplevel(), modal=True)
    dlg.set_decorated(False)
    dlg.set_resizable(False)
    visual = dlg.get_screen().get_rgba_visual()
    if visual is not None:
        dlg.set_visual(visual)
    dlg.set_app_paintable(True)
    f = max(0.7, view.app._text_factor())

    def paint(widget, cr):
        a = widget.get_allocation()
        c = view.colors()
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        r = _corner(view.app)
        _rounded(cr, 0, 0, a.width, a.height, r)
        cr.set_source_rgba(*c["alt"], 1)
        cr.fill()
        accent = _accent_rgb(widget)
        g = cairo.LinearGradient(0, 0, 0, 120 * f)
        g.add_color_stop_rgba(0, *accent, 0.16)
        g.add_color_stop_rgba(1, *accent, 0)
        cr.save()
        _rounded(cr, 0, 0, a.width, a.height, r)
        cr.clip()
        cr.rectangle(0, 0, a.width, 120 * f)
        cr.set_source(g)
        cr.fill()
        cr.restore()
        return False
    dlg.connect("draw", paint)
    dlg.get_content_area().set_border_width(0)
    aa = dlg.get_action_area()
    if aa is not None:
        aa.set_no_show_all(True)
        aa.hide()
    return dlg


class AddWindow:
    """PlunderHub's own "Add game" for Faugus. run() -> AddRequest, "heroic:<store>"
    (show that store's tab), "open:<gameid>" (the launcher already aboard) or None."""

    W = 980

    def __init__(self, view, site: fa.FaugusSite):
        self.view, self.app, self.site = view, view.app, site
        self.f = max(0.7, self.app._text_factor())
        self.kind = "setup"
        self.prefix_edited = False
        self._setting_prefix = False
        self.answer = None

    def _(self, key):
        return self.app._(key)

    def _label(self, text, cls="tm-card-hint"):
        lb = Gtk.Label(label=text, xalign=0)
        lb.get_style_context().add_class(cls)
        return lb

    def run(self):
        app = self.app
        dlg = self.dlg = _card_window(self.view, int(self.W * self.f))
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        outer.set_size_request(int(self.W * self.f), -1)
        for side in ("start", "end"):
            getattr(outer, f"set_margin_{side}")(32)
        outer.set_margin_top(26)
        outer.set_margin_bottom(24)

        head = Gtk.Box(spacing=14)
        head.pack_start(app._load_launcher_icon("Faugus", 44), False, False, 0)
        hv = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        hv.set_valign(Gtk.Align.CENTER)
        t = Gtk.Label(label=self._("tm_fg_title"), xalign=0)
        t.get_style_context().add_class("tm-title")
        hv.pack_start(t, False, False, 0)
        hv.pack_start(self._label(self._("tm_fg_sub"), "tm-sub"), False, False, 0)
        head.pack_start(hv, True, True, 0)
        outer.pack_start(head, False, False, 0)

        cols = Gtk.Box(spacing=30)
        left = self.left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        left.pack_start(self._label(self._("tm_fg_what")), False, False, 0)
        self.pick = Gtk.Button()
        self.pick.get_style_context().add_class("home-pill-ghost")
        self.pick.set_focus_on_click(False)
        pr = Gtk.Box(spacing=12)
        pr.set_margin_top(4)
        pr.set_margin_bottom(4)
        self.pick_icon = monogram_widget(self.kind, int(36 * self.f))
        pr.pack_start(self.pick_icon, False, False, 0)
        pv = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        pv.set_valign(Gtk.Align.CENTER)
        self.pick_name = Gtk.Label(xalign=0)
        self.pick_sub = Gtk.Label(xalign=0)
        for lb, cls in ((self.pick_name, "tm-dock-title"), (self.pick_sub, "tm-dock-sub")):
            lb.get_style_context().add_class(cls)
            lb.set_ellipsize(Pango.EllipsizeMode.END)
            lb.set_max_width_chars(20)
            pv.pack_start(lb, False, False, 0)
        pr.pack_start(pv, True, True, 0)
        pr.pack_end(Gtk.Image.new_from_icon_name("pan-down-symbolic", Gtk.IconSize.BUTTON), False, False, 0)
        self.pick.add(pr)
        self.pick.connect("clicked", self._open_menu)
        left.pack_start(self.pick, False, False, 0)

        grid = Gtk.Grid(column_spacing=10, row_spacing=6)
        grid.set_margin_top(10)
        r = 0
        grid.attach(self._label(self._("tm_fg_name")), 0, r, 2, 1)
        r += 1
        self.name = Gtk.Entry()
        self.name.set_width_chars(8)
        self.name.set_hexpand(True)
        self.name.connect("changed", self._name_changed)
        grid.attach(self.name, 0, r, 2, 1)
        r += 1
        self.file_lbl = self._label("")
        self.file_lbl.set_margin_top(8)
        grid.attach(self.file_lbl, 0, r, 2, 1)
        r += 1
        self.file = Gtk.Entry()
        self.file.set_width_chars(8)
        self.file.set_hexpand(True)
        self.file.connect("changed", lambda *_: self._update())
        self.file_btn = Gtk.Button.new_from_icon_name("document-open-symbolic", Gtk.IconSize.BUTTON)
        self.file_btn.set_tooltip_text(self._("tm_fg_choose_file"))
        self.file_btn.connect("clicked", self._choose_file)
        grid.attach(self.file, 0, r, 1, 1)
        grid.attach(self.file_btn, 1, r, 1, 1)
        r += 1
        pl = self._label(self._("tm_fg_prefix"))
        pl.set_margin_top(8)
        grid.attach(pl, 0, r, 2, 1)
        r += 1
        self.prefix = Gtk.Entry()
        self.prefix.set_width_chars(8)
        self.prefix.set_hexpand(True)
        self.prefix.connect("changed", self._prefix_changed)
        pb = Gtk.Button.new_from_icon_name("folder-open-symbolic", Gtk.IconSize.BUTTON)
        pb.set_tooltip_text(self._("tm_fg_choose_prefix"))
        pb.connect("clicked", self._choose_prefix)
        grid.attach(self.prefix, 0, r, 1, 1)
        grid.attach(pb, 1, r, 1, 1)
        self.wine_only = [pl, self.prefix, pb]
        left.pack_start(grid, False, False, 0)

        mv = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        mv.set_margin_top(8)
        proton_lbl = self._label(self._("tm_fg_proton"))
        mv.pack_start(proton_lbl, False, False, 0)
        self.runner = Gtk.ComboBoxText()
        for cell in self.runner.get_cells():
            cell.set_property("ellipsize", Pango.EllipsizeMode.END)
        for name in fa.proton_choices():
            self.runner.append(name, self._("tm_fg_proton_ours").format(name=name) if name == fa.GE_LATEST else name)
        self.runner.set_active_id(fa.GE_LATEST)
        self.runner.connect("changed", lambda *_: self.preview.queue_draw())
        mv.pack_start(self.runner, False, False, 0)
        self.wine_only += [proton_lbl, self.runner]
        self.app_menu = Gtk.CheckButton(label=self._("tm_fg_app_menu"))
        self.app_menu.set_active(True)
        self.app_menu.set_margin_top(8)
        mv.pack_start(self.app_menu, False, False, 0)
        why = self._label(self._("tm_fg_app_menu_why"))
        why.set_line_wrap(True)
        why.set_max_width_chars(40)
        why.set_margin_start(26)
        mv.pack_start(why, False, False, 0)
        if not self.site.exec_cmd:              # an AppImage Faugus: nothing to point an entry at
            self.app_menu.set_active(False)
            self.app_menu.set_sensitive(False)
        left.pack_start(mv, False, False, 0)
        self.heroic_box = self._heroic_hint()
        left.pack_start(self.heroic_box, False, False, 0)
        cols.pack_start(left, False, False, 0)

        self.preview = Gtk.DrawingArea()
        self.preview.connect("draw", self._draw_preview)
        cols.pack_start(self.preview, False, False, 0)
        outer.pack_start(cols, False, False, 0)

        row = Gtk.Box(spacing=10)
        self.warn = self._label("")
        self.warn.set_line_wrap(True)
        self.warn.set_max_width_chars(50)
        row.pack_start(self.warn, True, True, 0)
        cancel = app._dialog_button(self._("tm_not_now"), "ghost", "back", "Esc")
        self.go = app._dialog_button(self._("tm_fg_go"), "accent", "activate", "Enter")
        self.go_label = next((w for w in self.go.get_child().get_children() if isinstance(w, Gtk.Label)), None)
        badge = app._home_launcher_badge("Faugus", 20)
        if badge is not None:
            logo = Gtk.Image.new_from_pixbuf(badge)
            self.go.get_child().pack_start(logo, False, False, 0)
            self.go.get_child().reorder_child(logo, 0)
        cancel.connect("clicked", lambda *_: dlg.response(Gtk.ResponseType.CANCEL))
        self.go.connect("clicked", lambda *_: self._go())
        for b in (cancel, self.go):
            b.set_focus_on_click(False)
            b.set_valign(Gtk.Align.CENTER)
            row.pack_start(b, False, False, 0)
        outer.pack_start(row, False, False, 0)
        dlg.get_content_area().pack_start(outer, False, False, 0)
        dlg.connect("key-press-event", self._on_key)

        dlg.show_all()
        # one size for every choice: the left column as tall as its tallest version
        lw = int(470 * self.f)
        tallest = 0
        for k in list(fa.GAME_KINDS) + list(fa.LAUNCHERS):
            self._set_kind(k, quiet=True)
            tallest = max(tallest, left.get_preferred_height_for_width(lw)[1])
        left.set_size_request(lw, tallest)
        self.preview.set_size_request(int(400 * self.f), max(int(560 * self.f), tallest))
        self.prefix_edited = False
        self.name.set_text("")
        self.file.set_text("")
        self._set_kind("setup")
        self.file_btn.grab_focus()
        dlg.run()
        dlg.destroy()
        return self.answer

    def _on_key(self, _dlg, ev):
        if ev.keyval == Gdk.KEY_Escape:
            self.dlg.response(Gtk.ResponseType.CANCEL)
            return True
        if ev.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and self.go.get_sensitive() \
                and not isinstance(self.dlg.get_focus(), Gtk.Button):
            self._go()
            return True
        return False

    # --- the "what" menu
    def _open_menu(self, button):
        pop = Gtk.Popover(relative_to=button)
        pop.set_position(Gtk.PositionType.BOTTOM)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        for side in ("start", "end", "top", "bottom"):
            getattr(box, f"set_margin_{side}")(8)

        def group(text):
            lb = self._label(text)
            lb.set_margin_start(8)
            lb.set_margin_top(6)
            lb.set_margin_bottom(2)
            box.pack_start(lb, False, False, 0)

        def item(kind, name, sub):
            b = Gtk.Button()
            b.set_relief(Gtk.ReliefStyle.NONE)
            b.set_focus_on_click(False)
            rb = Gtk.Box(spacing=12)
            rb.pack_start(monogram_widget(kind, int(30 * self.f)), False, False, 0)
            v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            n = Gtk.Label(label=name, xalign=0)
            n.get_style_context().add_class("tm-dock-title")
            s = Gtk.Label(label=sub, xalign=0)
            s.get_style_context().add_class("tm-dock-sub")
            v.pack_start(n, False, False, 0)
            v.pack_start(s, False, False, 0)
            rb.pack_start(v, True, True, 0)
            if kind in fa.LAUNCHERS and fa.launcher_aboard(self.site, kind):
                tag = Gtk.Label(label=self._("tm_aboard"))
                tag.get_style_context().add_class("home-chip-count")
                rb.pack_end(tag, False, False, 0)
            b.add(rb)
            b.set_size_request(int(420 * self.f), -1)
            b.connect("clicked", lambda *_: (pop.popdown(), self._set_kind(kind)))
            box.pack_start(b, False, False, 0)
            if kind == self.kind:
                GLib.idle_add(lambda: b.grab_focus() and False)

        group(self._("tm_fg_group_game"))
        for k in fa.GAME_KINDS:
            item(k, self._(f"tm_fg_kind_{k}"), self._(f"tm_fg_kind_{k}_sub"))
        group(self._("tm_fg_group_store"))
        for k, info in fa.LAUNCHERS.items():
            item(k, info["name"], self._(STORE_SUB[k]))
        scroll = Gtk.ScrolledWindow()
        scroll.get_style_context().add_class("settings-scroll")
        scroll.set_overlay_scrolling(False)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_propagate_natural_height(True)
        scroll.set_max_content_height(int(520 * self.f))
        scroll.add(box)
        pop.add(scroll)
        pop.show_all()
        pop.popup()

    # --- the Heroic hint (Epic / GOG / Amazon)
    def _heroic_hint(self):
        box = Gtk.Box(spacing=14)
        box.set_margin_top(14)
        box.set_app_paintable(True)

        def paint(w, cr):
            a = w.get_allocation()
            _rounded(cr, 0, 0, a.width, a.height, 16)
            cr.set_source_rgba(*_accent_rgb(w), 0.13)
            cr.fill()
            return False
        box.connect("draw", paint)
        logo = self.app._load_launcher_icon("Heroic", int(36 * self.f))
        logo.set_valign(Gtk.Align.CENTER)
        logo.set_margin_start(14)
        box.pack_start(logo, False, False, 0)
        v = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        v.set_margin_top(12)
        v.set_margin_bottom(12)
        v.set_margin_end(14)
        self.heroic_t = Gtk.Label(xalign=0)
        self.heroic_t.get_style_context().add_class("tm-dock-title")
        self.heroic_t.set_line_wrap(True)
        self.heroic_t.set_max_width_chars(30)
        v.pack_start(self.heroic_t, False, False, 0)
        self.heroic_go = Gtk.Button()
        self.heroic_go.get_style_context().add_class("home-pill-ghost")
        self.heroic_go.set_halign(Gtk.Align.START)
        self.heroic_go.set_focus_on_click(False)
        self.heroic_go.connect("clicked", self._to_heroic_tab)
        v.pack_start(self.heroic_go, False, False, 0)
        box.pack_start(v, True, True, 0)
        box.set_no_show_all(True)
        return box

    def _to_heroic_tab(self, *_):
        self.answer = f"heroic:{self.kind}"
        self.dlg.response(Gtk.ResponseType.CANCEL)

    def _show_heroic_hint(self):
        store = HEROIC_STORE.get(self.kind)
        if store is None:
            self.heroic_box.hide()
            return
        from plunderhub.ui.treasure_map import STORE_NAMES
        self.heroic_t.set_text(self._("tm_fg_heroic").format(store=store))
        self.heroic_go.set_label(self._("tm_fg_heroic_go").format(store=STORE_NAMES[self.kind]))
        for ch in self.heroic_box.get_children():
            ch.show_all()
        self.heroic_box.show()

    # --- the fields
    def _set_kind(self, kind, quiet=False):
        self.kind = kind
        self.pick_icon._kind = kind
        self.pick_icon.queue_draw()
        launcher = kind in fa.LAUNCHERS
        if launcher:
            name = fa.LAUNCHERS[kind]["name"]
            self.pick_name.set_text(name)
            self.pick_sub.set_text(self._("tm_fg_store_launcher") + "  ·  " + self._(STORE_SUB[kind]))
            self.name.set_text(name)
        else:
            self.pick_name.set_text(self._(f"tm_fg_kind_{kind}"))
            self.pick_sub.set_text(self._(f"tm_fg_kind_{kind}_sub"))
            if self.name.get_text() in [v["name"] for v in fa.LAUNCHERS.values()]:
                self.name.set_text("")
        self.file_lbl.set_text(self._({"setup": "tm_fg_installer", "exe": "tm_fg_game_exe",
                                        "linux": "tm_fg_start_file"}.get(kind, "tm_fg_installer")))
        for w in (self.file_lbl, self.file, self.file_btn):
            w.set_visible(not launcher)
        for w in self.wine_only:
            w.set_visible(kind != "linux")
        self._show_heroic_hint()
        if not quiet:
            self._name_changed()

    def _name_changed(self, *_):
        if not self.prefix_edited:
            self._setting_prefix = True
            gid = fa.format_title(self.name.get_text())
            self.prefix.set_text("" if self.kind == "linux" or not gid else
                                 str(self.site.default_prefix_root() / gid))
            self._setting_prefix = False
        self._update()

    def _prefix_changed(self, *_):
        if not self._setting_prefix:
            self.prefix_edited = True
        self._update()

    def _choose_file(self, *_):
        fc = Gtk.FileChooserNative(title=self._("tm_fg_choose_file"), transient_for=self.dlg,
                                   action=Gtk.FileChooserAction.OPEN)
        if self.kind in ("setup", "exe"):
            flt = Gtk.FileFilter()
            flt.set_name(".exe / .msi" if self.kind == "setup" else ".exe")
            for pat in (("*.exe", "*.EXE", "*.msi", "*.MSI") if self.kind == "setup" else ("*.exe", "*.EXE")):
                flt.add_pattern(pat)
            fc.add_filter(flt)
        if fc.run() == Gtk.ResponseType.ACCEPT and fc.get_filename():
            path = fc.get_filename()
            self.file.set_text(path)
            if not self.name.get_text().strip():
                stem = Path(path).parent.name if self.kind in ("exe", "linux") else fa.title_from_installer(Path(path).stem)
                stem = re.sub(r"^(setup|install(er)?)[_ -]*", "", stem, flags=re.I)
                stem = re.sub(r"[_ -]*v?\d+(\.\d+)+.*$", "", stem)
                stem = re.sub(r"[_]+", " ", stem).strip()
                stem = stem.title() if stem.islower() else stem     # "far lone sails" -> "Far Lone Sails"
                self.name.set_text(stem[:1].upper() + stem[1:] if stem else "")
        fc.destroy()

    def _choose_prefix(self, *_):
        fc = Gtk.FileChooserNative(title=self._("tm_fg_choose_prefix"), transient_for=self.dlg,
                                   action=Gtk.FileChooserAction.SELECT_FOLDER)
        try:
            fc.set_current_folder(str(self.site.default_prefix_root()))
        except Exception:
            pass
        if fc.run() == Gtk.ResponseType.ACCEPT and fc.get_filename():
            self.prefix.set_text(fc.get_filename())
        fc.destroy()

    def _problem(self) -> str:
        name = self.name.get_text().strip()
        if not name or not fa.format_title(name):
            return self._("tm_fg_need_name")
        if self.kind not in fa.LAUNCHERS:
            f = self.file.get_text().strip()
            if not f:
                return self._("tm_fg_need_file")
            if not Path(os.path.expanduser(f)).is_file():
                return self._("tm_fg_file_missing")
        elif fa.launcher_aboard(self.site, self.kind):
            return ""
        if any(str(g.get("title") or "").casefold() == name.casefold() for g in fa.read_games(self.site)):
            return self._("tm_fg_taken").format(name=name)
        if self.kind != "linux":
            pfx = self.prefix.get_text().strip()
            if not pfx:
                return self._("tm_fg_need_prefix")
            if not self.site.visible(Path(os.path.expanduser(pfx))):
                return self._("tm_fg_flatpak_cant_see")
        return ""

    def _update(self):
        bad = self._problem()
        hint = bad
        if not bad and fa.faugus_running():
            hint = self._("tm_fg_running_note")
        self.warn.set_text(hint)
        self.go.set_sensitive(not bad)
        if self.go_label is not None:
            already = self.kind in fa.LAUNCHERS and fa.launcher_aboard(self.site, self.kind)
            self.go_label.set_text(self._("tm_fg_open_it") if already else self._("tm_fg_go"))
        self.preview.queue_draw()

    def _go(self):
        if self._problem():
            return
        already = fa.launcher_aboard(self.site, self.kind) if self.kind in fa.LAUNCHERS else None
        if already is not None:
            self.answer = f"open:{already.get('gameid')}"
        else:
            self.answer = fa.AddRequest(
                kind=self.kind, title=self.name.get_text().strip(),
                prefix=os.path.expanduser(self.prefix.get_text().strip()) if self.kind != "linux" else "",
                runner=self.runner.get_active_id() or fa.GE_LATEST,
                file=os.path.expanduser(self.file.get_text().strip()),
                app_menu=self.app_menu.get_active())
        self.dlg.response(Gtk.ResponseType.OK)

    # --- the right side: the tile as it will be, room, what happens
    def _steps(self) -> List[str]:
        k = self.kind
        steps = []
        if k in fa.LAUNCHERS:
            info = fa.LAUNCHERS[k]
            steps.append(self._("tm_fg_step_dl").format(name=info["name"]))
            if not fa.umu_runtime_ready():
                steps.append(self._("tm_fg_step_runtime"))
            steps.append(self._("tm_fg_step_quiet"))
            if info.get("signin"):
                steps.append(self._("tm_fg_step_signin"))
            if info.get("close_to_finish"):
                steps.append(self._("tm_fg_step_close"))
            if info.get("keep_path"):
                steps.append(self._("tm_fg_step_keep_path"))
            steps.append(self._("tm_fg_step_launcher_done"))
        elif k == "setup":
            if not fa.umu_runtime_ready():
                steps.append(self._("tm_fg_step_runtime"))
            steps += [self._("tm_fg_step_run_setup"), self._("tm_fg_step_find"), self._("tm_fg_step_pick"),
                      self._("tm_fg_step_aboard")]
        elif k == "exe":
            steps += [self._("tm_fg_step_prefix_first"), self._("tm_fg_step_aboard")]
        else:
            steps.append(self._("tm_fg_step_native"))
        return steps

    def _draw_preview(self, widget, cr):
        c = self.view.colors()
        accent = _accent_rgb(widget)
        f = self.f
        W = widget.get_allocated_width()
        _rounded(cr, 0, 0, W, widget.get_allocated_height(), 18)
        cr.set_source_rgba(*c["raised"], 1)
        cr.fill()
        _pango_text(cr, widget, self._("tm_fg_preview"), 20, 16 * f, W - 40, 10.5 * f, True, (*c["fg"], 0.5))
        cw = int(150 * f)
        ch = int(cw * 4 / 3)
        x, y = 20, 40 * f
        for i, a in ((8, 0.06), (4, 0.1)):
            _rounded(cr, x - i * 0.3, y + i * 0.8, cw + i * 0.6, ch + i * 0.4, 14)
            cr.set_source_rgba(0, 0, 0, a)
            cr.fill()
        cr.save()
        _rounded(cr, x, y, cw, ch, 14)
        cr.clip()
        cr.translate(x, y)
        name = self.name.get_text().strip() or "…"
        if self.kind in fa.LAUNCHERS:
            col = _hex(MONOGRAM[self.kind][1])
            g = cairo.LinearGradient(0, 0, cw, ch)
            g.add_color_stop_rgba(0, *col, 1)
            g.add_color_stop_rgba(1, *(v * 0.45 for v in col), 1)
            cr.set_source(g)
            cr.paint()
            s = cw * 0.46
            cr.arc(cw / 2, ch * 0.38, s / 2 + 3, 0, 2 * math.pi)
            cr.set_source_rgba(1, 1, 1, 0.16)
            cr.fill()
            draw_monogram(cr, widget, (cw - s) / 2, ch * 0.38 - s / 2, s, self.kind)
        else:
            _title_gradient(cr, name, cw, ch, 1.0)
        grad = cairo.LinearGradient(0, ch * 0.55, 0, ch)
        grad.add_color_stop_rgba(0, 0, 0, 0, 0)
        grad.add_color_stop_rgba(1, 0, 0, 0, 0.8)
        cr.set_source(grad)
        cr.rectangle(0, 0, cw, ch)
        cr.fill()
        _pango_text(cr, widget, name, 10, ch - 30 * f, cw - 20, 13 * f, True, (1, 1, 1, 0.97))
        cr.restore()
        badge = self.app._home_launcher_badge("Faugus", 24)
        if badge is not None:
            Gdk.cairo_set_source_pixbuf(cr, badge, x + 8, y + 8)
            cr.paint()
        rx = x + cw + 18
        rw = W - rx - 18
        ty = y + 2
        rows = []
        runtime = 0 if fa.umu_runtime_ready() or self.kind == "linux" else RUNTIME_MB
        if self.kind in fa.LAUNCHERS:
            gb = fa.LAUNCHERS[self.kind]["gb"]
            rows.append((self._("tm_fg_needs"), f"~{gb:.1f} GB" if gb >= 1 else f"~{gb * 1000:.0f} MB",
                         self._("tm_fg_needs_runtime").format(size=f"{runtime} MB") if runtime
                         else self._("tm_fg_needs_launcher")))
        elif self.kind == "setup":
            rows.append((self._("tm_fg_needs"), "?", self._("tm_fg_needs_runtime").format(size=f"{runtime} MB")
                         if runtime else self._("tm_fg_installer_tells")))
        where = self.prefix.get_text().strip() if self.kind != "linux" else self.file.get_text().strip()
        free = fa.free_bytes(Path(os.path.expanduser(where))) if where else 0
        rows.append((self._("tm_fg_free"), f"{free / 1e9:.0f} GB" if free else "–",
                     str(Path(os.path.expanduser(where)).parent) if where else ""))
        if self.kind != "linux":
            runner = self.runner.get_active_id() or fa.GE_LATEST
            rows.append((self._("tm_fg_runs_with"), runner, self._("tm_fg_runs_with_sub")))
        for label, big, small in rows:
            _pango_text(cr, widget, label, rx, ty, rw, 10 * f, True, (*c["fg"], 0.5))
            _pango_text(cr, widget, big, rx, ty + 17 * f, rw, 16.25 * f, True, (*c["fg"], 0.95))
            _pango_text(cr, widget, small, rx, ty + 44 * f, rw, 10.6 * f, False, (*c["fg"], 0.55))
            ty += 76 * f
        y2 = max(y + ch, ty) + 26 * f
        already = fa.launcher_aboard(self.site, self.kind) if self.kind in fa.LAUNCHERS else None
        if already is not None:
            amber = (0.98, 0.72, 0.30)
            _rounded(cr, 20, y2, W - 40, 110 * f, 14)
            cr.set_source_rgba(*amber, 0.14)
            cr.fill()
            _pango_text(cr, widget, self._("tm_fg_already_t").format(name=already.get("title") or ""), 36,
                        y2 + 14 * f, W - 72, 14.4 * f, True, (*amber, 1))
            _pango_text(cr, widget, self._("tm_fg_already_s"), 36, y2 + 44 * f, W - 72, 11.9 * f, False,
                        (*c["fg"], 0.8), lines=2)
            return False
        _pango_text(cr, widget, self._("tm_fg_what_happens"), 20, y2, W - 40, 10.6 * f, True, (*c["fg"], 0.5))
        y2 += 24 * f
        for i, step in enumerate(self._steps(), 1):
            r = 13 * f
            cr.arc(20 + r, y2 + r, r, 0, 2 * math.pi)
            cr.set_source_rgba(*accent, 0.2)
            cr.fill()
            _pango_text(cr, widget, str(i), 20, y2 + r - 7.5 * f, 2 * r, 11.25 * f, True, (*accent, 1),
                        align_center=True)
            used = _pango_text(cr, widget, step, 20 + 2 * r + 12, y2 + 1 * f, W - 40 - 2 * r - 12, 12.5 * f, False,
                               (*c["fg"], 0.88), lines=2)
            y2 += max(2 * r, used or 0) + 12 * f
        return False


class ExePicker:
    """After a game's installer: the .exe files it left, the likeliest
    picked. run() -> the chosen path or None."""

    def __init__(self, view, title: str, prefix: str, found: List[fa.ExeFound], left: List[str]):
        self.view, self.app = view, view.app
        self.title, self.prefix, self.found, self.left = title, prefix, found, left

    def run(self) -> Optional[str]:
        app = self.app
        f = max(0.7, app._text_factor())
        dlg = _card_window(self.view, int(640 * f))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_size_request(int(640 * f), -1)
        for side in ("start", "end"):
            getattr(box, f"set_margin_{side}")(32)
        box.set_margin_top(26)
        box.set_margin_bottom(24)
        t = Gtk.Label(label=app._("tm_fg_pick_t"), xalign=0)
        t.get_style_context().add_class("tm-title")
        box.pack_start(t, False, False, 0)
        s = Gtk.Label(label=app._("tm_fg_pick_s").format(name=self.title), xalign=0)
        s.set_line_wrap(True)
        s.get_style_context().add_class("tm-sub")
        box.pack_start(s, False, False, 6)
        chosen = {"path": self.found[0].path if self.found else None}
        first = None
        for i, e in enumerate(self.found[:8]):
            rb = Gtk.RadioButton.new_from_widget(first)
            first = first or rb
            row = Gtk.Box(spacing=10)
            n = Gtk.Label(label=Path(e.path).name, xalign=0)
            n.get_style_context().add_class("tm-dock-title")
            row.pack_start(n, False, False, 0)
            if i == 0:
                tag = Gtk.Label(label=app._("tm_fg_likeliest"))
                tag.get_style_context().add_class("home-chip-count")
                row.pack_start(tag, False, False, 0)
            where = os.path.relpath(os.path.dirname(e.path), self.prefix) if e.path.startswith(self.prefix) \
                else os.path.dirname(e.path)
            meta = Gtk.Label(label=f"{e.size / 1e6:.0f} MB  ·  {where}", xalign=1)
            meta.get_style_context().add_class("tm-dock-sub")
            meta.set_ellipsize(Pango.EllipsizeMode.START)
            row.pack_end(meta, True, True, 0)
            rb.add(row)
            rb.set_active(i == 0)
            rb.connect("toggled", lambda b, p=e.path: b.get_active() and chosen.__setitem__("path", p))
            box.pack_start(rb, False, False, 0)
        if self.left:
            lo = Gtk.Label(label=app._("tm_fg_left_out").format(names=", ".join(self.left)), xalign=0)
            lo.get_style_context().add_class("tm-card-hint")
            lo.set_line_wrap(True)
            lo.set_margin_top(6)
            box.pack_start(lo, False, False, 0)
        row = Gtk.Box(spacing=10)
        row.set_margin_top(10)
        cancel = app._dialog_button(app._("tm_not_now"), "ghost", "back", "Esc")
        other = app._dialog_button(app._("tm_fg_other_file"), "ghost")
        ok = app._dialog_button(app._("tm_fg_thats_it"), "accent", "activate", "Enter")

        def pick_other(*_):
            fc = Gtk.FileChooserNative(title=app._("tm_fg_choose_file"), transient_for=dlg,
                                       action=Gtk.FileChooserAction.OPEN)
            try:
                fc.set_current_folder(os.path.join(self.prefix, "drive_c"))
            except Exception:
                pass
            if fc.run() == Gtk.ResponseType.ACCEPT and fc.get_filename():
                chosen["path"] = fc.get_filename()
                dlg.response(1)
            fc.destroy()
        cancel.connect("clicked", lambda *_: dlg.response(Gtk.ResponseType.CANCEL))
        other.connect("clicked", pick_other)
        ok.connect("clicked", lambda *_: dlg.response(1))
        ok.set_sensitive(bool(self.found))
        row.pack_start(cancel, False, False, 0)
        row.pack_end(ok, False, False, 0)
        row.pack_end(other, False, False, 0)
        box.pack_start(row, False, False, 0)
        dlg.get_content_area().pack_start(box, False, False, 0)
        dlg.show_all()
        ok.grab_focus()
        r = dlg.run()
        dlg.destroy()
        return chosen["path"] if int(r) == 1 else None


# --- the flow: window -> job on the tile + dock -> pick -> Faugus closed -> written ----------

def open_add(view):
    status, site = fa.faugus_status()
    if status != "ok" or site is None:
        view._refresh(keep_scroll=True)
        return
    view.grid._set_hover(None)
    answer = AddWindow(view, site).run()
    if answer is None:
        return
    if isinstance(answer, str) and answer.startswith("heroic:"):
        view._select_tab(answer.split(":", 1)[1])
        return
    if isinstance(answer, str) and answer.startswith("open:"):
        game = view.games_by_uid.get(f"faugus:{answer.split(':', 1)[1]}")
        if game is not None:
            view.set_sail(game)
        return
    start(view, site, answer)


def start(view, site: fa.FaugusSite, req: fa.AddRequest):
    from plunderhub.core.treasure import OwnedGame
    gid = fa.format_title(req.title)
    uid = f"faugus:+{gid}"                       # till written: its own uid
    cover = launcher_cover(req.kind, Path(view.opts["cache"]), view) if req.kind in fa.LAUNCHERS else None
    game = OwnedGame(uid, "faugus", req.title, False, cover, gid, extra={"pending": True})
    view.faugus_pending[uid] = game
    dl = {"frac": 0.0, "state": "downloading", "size": 0, "speed": 0, "rate": 0, "place": 0,
          "launcher": "Faugus", "t0": time.monotonic(), "wall0": time.time(), "last": None, "bytes": 0,
          "direct": True, "got": 0, "phase": "prepare"}
    view.downloads[uid] = dl
    view.queue.append(uid)
    view.reload(keep_scroll=True)
    cache = Path(view.opts["cache"]) / "faugus-add"
    cache.mkdir(parents=True, exist_ok=True)

    def changed(job: fa.AddJob):
        def put():
            dl["frac"] = job.frac
            dl["phase"] = job.phase
            dl["state"] = "installing" if job.phase not in ("prepare", "download") else "downloading"
            view.grid.refresh_game(uid)
            view._update_dock()
            if job.state != "working":
                _finished(view, site, req, job, uid, game, dl)
            return False
        GLib.idle_add(put)
    job = fa.AddJob(site, req, cache, changed)
    dl["job"] = job
    logger.info(f"Faugus add: {req.kind} '{req.title}' -> {req.prefix or req.file}")
    job.start()
    view._update_dock()


def _drop(view, uid):
    view.faugus_pending.pop(uid, None)
    view.downloads.pop(uid, None)
    if uid in view.queue:
        view.queue.remove(uid)


def _finished(view, site, req, job: fa.AddJob, uid, game, dl):
    app = view.app
    if dl.get("finished"):
        return
    dl["finished"] = True
    if job.state in ("failed", "cancelled"):
        _drop(view, uid)
        view.reload(keep_scroll=True)
        view._update_dock()
        if job.state == "failed":
            why = {"not_installed": app._("tm_fg_failed_not_installed").format(name=req.title, log=job.log),
                   "no_exe": app._("tm_fg_failed_no_exe").format(log=job.log),
                   "no_runner": app._("tm_fg_setup_failed")}.get(
                job.error, app._("tm_fg_failed_other").format(why=job.error, log=job.log))
            app._card_dialog(app._("tm_install_failed").format(game=req.title), why,
                             [(app._("dialog_ok"), Gtk.ResponseType.CLOSE, "ghost")],
                             icon_name="circle-alert-symbolic", transient_for=view.get_toplevel())
        return
    exe = job.exe
    if job.state == "pick":
        exe = ExePicker(view, req.title, req.prefix, job.found, job.left_out).run()
        if not exe:
            # kept: the prefix holds the installed game; Faugus' own Add game can take it later
            _drop(view, uid)
            view.reload(keep_scroll=True)
            view._update_dock()
            return
    dl["phase"] = "save"
    dl["frac"] = 1.0
    _write_when_closed(view, site, req, exe, uid, dl, asked=False)


def _write_when_closed(view, site, req, exe, uid, dl, asked: bool):
    """Faugus writes its own list back when it closes: it's closed first
    (asked once); left open, the entry waits on the dock till it's gone."""
    app = view.app
    if fa.faugus_running():
        if not asked:
            r = app._card_dialog(
                app._("tm_fg_close_t"), app._("tm_fg_close_s").format(game=req.title),
                [(app._("tm_not_now"), Gtk.ResponseType.CANCEL, "ghost"),
                 (app._("tm_fg_close_btn"), Gtk.ResponseType.OK, "accent")],
                icon_name="circle-alert-symbolic", default=Gtk.ResponseType.OK, transient_for=view.get_toplevel())
            if r == Gtk.ResponseType.OK:
                app.close_launcher_asking("Faugus", view.get_toplevel())
        if fa.faugus_running():
            view._update_dock()
            GLib.timeout_add_seconds(2, lambda: _write_when_closed(view, site, req, exe, uid, dl, True) and False)
            return False
    try:
        art = (lambda gid: write_launcher_art(site, gid, req.kind, view)) if req.kind in fa.LAUNCHERS else None
        gid = fa.write_entry(site, req, exe, art=art)
    except Exception as e:
        logger.exception("Faugus add: writing the entry")
        _drop(view, uid)
        view.reload(keep_scroll=True)
        view._update_dock()
        app._card_dialog(app._("tm_install_failed").format(game=req.title), str(e),
                         [(app._("dialog_ok"), Gtk.ResponseType.CLOSE, "ghost")],
                         icon_name="circle-alert-symbolic", transient_for=view.get_toplevel())
        return False
    _drop(view, uid)
    view._toast = app._("tm_toast_aboard").format(game=req.title)
    view._toast_sub = app._("tm_toast_aboard_sub")
    view._toast_until = time.monotonic() + 5
    view.reload(keep_scroll=True)
    view._update_dock()
    if hasattr(app, "reload"):
        app.reload()
    logger.info(f"Faugus add: {req.title} aboard as faugus:{gid}")
    return False
