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

"""The main window's library view: sidebar, top bar, the "Continue
playing" hero, the Recently played row and the game cards/list rows."""

import colorsys
import math
import queue
import threading
import time
import webbrowser
import zlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cairo
import gi

gi.require_version("PangoCairo", "1.0")
from gi.repository import PangoCairo  # noqa: E402

from plunderhub.constants import (  # noqa: E402
    APP_CHANNEL, APP_VERSION, KOFI_URL, REVEAL_STAGGER_MAX_MS, REVEAL_STAGGER_MS,
)
from plunderhub.core.desktop import is_tiling_window_manager  # noqa: E402
from plunderhub.core.models import HEROIC_STORES, GameItem  # noqa: E402
from plunderhub.core.persistence import save_settings  # noqa: E402
from plunderhub.gi_setup import Gdk, GdkPixbuf, GLib, Gtk, Pango, image_surface  # noqa: E402
from plunderhub.icons import get_icon_pixbuf  # noqa: E402
from plunderhub.logging_setup import logger  # noqa: E402
from plunderhub.theme import _app_icon_file  # noqa: E402

DANGER_RGB = (0xe5 / 255, 0x53 / 255, 0x4b / 255)
WARNING_RGB = (0xe8 / 255, 0xa3 / 255, 0x3d / 255)
# The round badge of a card dialog: its icon, and whether it takes the
# accent (None) or a colour of its own.
_DIALOG_TONES = {
    "info": ("dialog-information-symbolic", None),
    "question": ("dialog-question-symbolic", None),
    "success": ("emblem-ok-symbolic", None),
    "warning": ("triangle-alert-symbolic", WARNING_RGB),
    "danger": ("triangle-alert-symbolic", DANGER_RGB),
    "error": ("circle-alert-symbolic", DANGER_RGB),
}
STAR_RGB = (0xe3 / 255, 0xb3 / 255, 0x41 / 255)
HERO_HEIGHT = 300
LIST_COLUMN_WIDTH = 980


def _rounded(cr, x: float, y: float, w: float, h: float, r: float):
    r = max(0.0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def _windowless_area() -> Gtk.DrawingArea:
    """A DrawingArea drawn into its parent's window, without one of its
    own. With one, in a button, it sat over the button's input window: a
    click on the icon - the button's middle - went past the button
    (Shipwright's sidebar button only took clicks round its edge)."""
    area = Gtk.DrawingArea()
    area.set_has_window(False)
    return area


def _pixbuf_area(pb: GdkPixbuf.Pixbuf) -> Gtk.DrawingArea:
    """A picture drawn, not a Gtk.Image: inside a button or a list row, an
    image re-laid the whole window out whenever the pointer came onto it or
    left it (GTK3 drops an image's icon on every change of state)."""
    area = _windowless_area()
    area.set_size_request(pb.get_width(), pb.get_height())

    def draw(_widget, cr):
        Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
        cr.paint()
        return False
    area.connect("draw", draw)
    return area


def _title_hue(uid: str) -> float:
    return (zlib.crc32(uid.encode()) % 1000) / 1000


def _accent_rgb(widget: Gtk.Widget) -> Tuple[float, float, float]:
    found, color = widget.get_style_context().lookup_color("accent_color")
    if not found:
        found, color = widget.get_style_context().lookup_color("theme_selected_bg_color")
    if not found:
        return (0x22 / 255, 0xd3 / 255, 0xac / 255)
    return (color.red, color.green, color.blue)


def _on_accent_rgb(rgb: Tuple[float, float, float]) -> Tuple[float, float, float]:
    luminance = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
    return (0.02, 0.06, 0.05) if luminance > 0.5 else (1.0, 1.0, 1.0)


def _pango_text(cr, widget: Gtk.Widget, text: str, x: float, y: float, width: float,
                size_pt: Optional[float], bold: bool, rgba: Tuple[float, float, float, float],
                lines: int = 1, align_center: bool = False) -> float:
    """Draws text with the widget's own font (Inter), ellipsized to
    `width` - at size_pt, or the widget's own size when None. Returns
    the height it took."""
    layout = _pango_layout(cr, widget, text, width, size_pt, bold, lines, align_center)
    cr.set_source_rgba(*rgba)
    cr.move_to(x, y)
    PangoCairo.show_layout(cr, layout)
    return layout.get_pixel_size()[1]


def _pango_layout(cr, widget: Gtk.Widget, text: str, width: float, size_pt: Optional[float], bold: bool,
                  lines: int = 1, align_center: bool = False):
    """_pango_text's layout, to measure before drawing."""
    layout = PangoCairo.create_layout(cr)
    desc = widget.get_pango_context().get_font_description().copy()
    if size_pt is not None:
        desc.set_size(int(size_pt * Pango.SCALE))
    desc.set_weight(Pango.Weight.BOLD if bold else Pango.Weight.NORMAL)
    layout.set_font_description(desc)
    layout.set_width(int(max(1, width) * Pango.SCALE))
    layout.set_ellipsize(Pango.EllipsizeMode.END)
    if lines > 1:
        layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        layout.set_height(-lines)
    if align_center:
        layout.set_alignment(Pango.Alignment.CENTER)
    layout.set_text(text, -1)
    return layout


def _title_gradient(cr, title: str, w: float, h: float, alpha: float = 1.0):
    """The game's own colours, picked from its title - the no-cover
    card, Console Mode's plate and the hero without art all share them."""
    hue = _title_hue(title)
    grad = cairo.LinearGradient(0, 0, w, h)
    grad.add_color_stop_rgba(0, *colorsys.hsv_to_rgb(hue, 0.50, 0.46), alpha)
    grad.add_color_stop_rgba(1, *colorsys.hsv_to_rgb((hue + 0.08) % 1, 0.65, 0.17), alpha)
    cr.set_source(grad)
    cr.paint()


def _paint_name_plate(cr, widget, title: str, meta: Optional[str], w: float, h: float,
                      alpha: float = 1.0):
    """A game with covers switched off: its colours, a soft light from the
    top left, its initial as a faint watermark, the name large on the
    left (under the launcher badge) and meta - the playtime - below.
    Nothing moves on hover: the name sits above the play button, and
    meta always leaves it room."""
    _rounded(cr, 0, 0, w, h, 8)
    cr.save()
    cr.clip()
    _title_gradient(cr, title, w, h, alpha)
    glow = cairo.RadialGradient(w * 0.15, 0, 0, w * 0.15, 0, w * 0.9)
    glow.add_color_stop_rgba(0, 1, 1, 1, 0.10 * alpha)
    glow.add_color_stop_rgba(1, 1, 1, 1, 0)
    cr.set_source(glow)
    cr.paint()
    scale = min(1.6, max(0.8, w / 300))
    initial = next((c for c in title if c.isalnum()), "").upper()
    if initial:
        size = h * 0.95
        _pango_text(cr, widget, initial, w - size * 0.85, h - size * 1.05, size,
                    size * 0.75, True, (1, 1, 1, 0.07 * alpha))
    pad = 16 * scale
    top = 46 * scale            # under the launcher badge
    room = h - top - (26 * scale if meta else 12 * scale)
    title_size = 15 * scale
    lines = max(1, min(3, int(room // (title_size * 1.75))))
    _pango_text(cr, widget, title, pad, top, w - 2 * pad, title_size, True,
                (1, 1, 1, 0.96 * alpha), lines=lines)
    if meta:
        _pango_text(cr, widget, meta, pad, h - 24 * scale, w - 2 * pad - 40, 9 * scale,
                    False, (1, 1, 1, 0.72 * alpha))
    cr.restore()


def _lighter(rgb, amount: float) -> Tuple[float, float, float]:
    """rgb mixed towards white by amount (0..1)."""
    return tuple(c + (1 - c) * amount for c in rgb[:3])


def _cubic_bezier(x1: float, y1: float, x2: float, y2: float):
    """CSS's cubic-bezier() timing function, as a function of 0..1."""
    def at(t, a, b):
        return 3 * a * t * (1 - t) ** 2 + 3 * b * t * t * (1 - t) + t ** 3

    def ease(x: float) -> float:
        if x <= 0 or x >= 1:
            return min(1.0, max(0.0, x))
        t = x
        for _ in range(8):      # Newton: the t whose curve x is x
            slope = 3 * x1 * (1 - t) ** 2 + 6 * (x2 - x1) * t * (1 - t) + 3 * (1 - x2) * t * t
            if abs(slope) < 1e-6:
                break
            t -= (at(t, x1, x2) - x) / slope
        return at(t, y1, y2)
    return ease


# the lift's curve - the margin transition it used to be
_LIFT_EASE = _cubic_bezier(0.2, 0.9, 0.3, 1.2)
LIFT_SECONDS = 0.22
# blurs of the lifted card's shadow, made once per size and accent
_card_shadows: Dict[Tuple, cairo.ImageSurface] = {}


def _queue_area(widget: Gtk.Widget, x: float, y: float, w: float, h: float):
    """Redraws x, y, w, h of widget's coordinates - through its window:
    the widget's own queue_draw_area() is cut to its clip, which leaves out
    a shadow drawn round it."""
    top = widget.get_toplevel()
    coords = widget.translate_coordinates(top, int(x), int(y)) if top is not None else None
    if coords is None:
        widget.queue_draw()
        return
    top.queue_draw_area(coords[0] - 1, coords[1] - 1, int(w) + 3, int(h) + 3)


def _blurred_box(w: int, h: int, radius: float, blur: float, spread: float) -> Tuple[cairo.ImageSurface, int]:
    """A rounded box's soft shadow as an alpha mask, blurred about like a
    CSS box-shadow of `blur` px - box-blurred three times at a quarter of
    the size, drawn back up smooth. Returns it and its margin round the box."""
    k = 4
    pad = int(math.ceil(blur + spread)) + k
    sw, sh = (w + 2 * pad) // k + 1, (h + 2 * pad) // k + 1
    src = cairo.ImageSurface(cairo.FORMAT_A8, sw, sh)
    cr = cairo.Context(src)
    cr.scale(1 / k, 1 / k)
    _rounded(cr, pad - spread, pad - spread, w + 2 * spread, h + 2 * spread, radius + spread)
    cr.fill()
    # three box blurs ≈ a gaussian of sigma blur / 2
    sigma = blur / 2 / k
    r = max(1, int(round((math.sqrt(4 * sigma * sigma + 1) - 1) / 2)))
    for _ in range(3):
        for dx, dy in ((1, 0), (0, 1)):
            dst = cairo.ImageSurface(cairo.FORMAT_A8, sw, sh)
            dcr = cairo.Context(dst)
            dcr.set_operator(cairo.OPERATOR_ADD)
            for i in range(-r, r + 1):
                dcr.set_source_surface(src, i * dx, i * dy)
                dcr.paint_with_alpha(1 / (2 * r + 1))
            src = dst
    return src, pad


def _soft_shadow(w: int, h: int, radius: float, layers: Tuple) -> Tuple[cairo.ImageSurface, int]:
    """A rounded box's CSS-like box-shadows - layers of (blur px, y offset,
    rgba) - drawn once and kept, with none under the box itself. Returns
    the image and its margin round the box."""
    key = (w, h, radius, layers)
    cached = _card_shadows.get(key)
    if cached is not None:
        return cached
    masks = [(_blurred_box(w, h, radius, blur, 0), dy, rgba) for blur, dy, rgba in layers]
    pad = max(mpad for (_m, mpad), _dy, _c in masks) + max(dy for _m, dy, _c in masks)
    out = cairo.ImageSurface(cairo.FORMAT_ARGB32, w + 2 * pad, h + 2 * pad)
    cr = cairo.Context(out)
    for (mask, mpad), dy, rgba in masks:
        cr.save()
        cr.translate(pad - mpad, pad - mpad + dy)
        cr.scale(4, 4)
        cr.set_source_rgba(*rgba)
        pattern = cairo.SurfacePattern(mask)
        pattern.set_filter(cairo.FILTER_GOOD)
        cr.mask(pattern)
        cr.restore()
    # like a CSS shadow, none under the box itself (its rounded corners)
    cr.set_operator(cairo.OPERATOR_CLEAR)
    _rounded(cr, pad, pad, w, h, radius)
    cr.fill()
    if len(_card_shadows) > 16:
        _card_shadows.clear()
    _card_shadows[key] = (out, pad)
    return out, pad


_hero_lights: Dict[Tuple, cairo.ImageSurface] = {}


def _paint_hero_light(cr, w: float, h: float, accent: Tuple[float, float, float], strength: float,
                      cx: float = 0.08, cy: float = 1.05, radius: float = 0.75):
    """The accent's light rising from behind a hero's text (centred at
    cx, cy of w x h; bottom left by default) - made once at a quarter of
    the size (it's all soft) and scaled up: a radial gradient over the
    whole hero cost a frame's worth of time on every frame of an animated
    one."""
    sw, sh = max(1, int(w / 4)), max(1, int(h / 4))
    key = (sw, sh, accent, strength, cx, cy, radius)
    light = _hero_lights.get(key)
    if light is None:
        light = cairo.ImageSurface(cairo.FORMAT_ARGB32, sw, sh)
        lc = cairo.Context(light)
        lc.scale(sw, sh)
        glow = cairo.RadialGradient(cx / 0.6, cy, 0, cx / 0.6, cy, radius)
        glow.add_color_stop_rgba(0, *accent, strength)
        glow.add_color_stop_rgba(0.45, *accent, strength * 0.4)
        glow.add_color_stop_rgba(1, *accent, 0)
        lc.scale(0.6, 1)
        lc.set_source(glow)
        lc.paint()
        if len(_hero_lights) > 8:
            _hero_lights.clear()
        _hero_lights[key] = light
    cr.save()
    cr.scale(w / sw, h / sh)
    pattern = cairo.SurfacePattern(light)
    pattern.set_filter(cairo.FILTER_BILINEAR)
    cr.set_source(pattern)
    cr.paint()
    cr.restore()


def _draw_hero_glyph(cr, kind: str, x: float, y: float, size: float, rgba: Tuple[float, float, float, float]):
    """The hero's small line icons, drawn rather than taken from the icon
    theme (which differs from desktop to desktop): "clock", "calendar",
    "play"."""
    cr.save()
    cr.new_path()               # or the first arc joins whatever was drawn last
    cr.translate(x, y)
    cr.scale(size / 16, size / 16)
    cr.set_source_rgba(*rgba)
    cr.set_line_width(1.6)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    if kind == "clock":
        cr.arc(8, 8, 6.5, 0, 2 * math.pi)
        cr.move_to(8, 4.2)
        cr.line_to(8, 8)
        cr.line_to(10.8, 9.6)
        cr.stroke()
    elif kind == "calendar":
        _rounded(cr, 1.8, 3, 12.4, 11.2, 2.2)
        cr.move_to(1.8, 6.6)
        cr.line_to(14.2, 6.6)
        cr.move_to(5.2, 1.4)
        cr.line_to(5.2, 4.2)
        cr.move_to(10.8, 1.4)
        cr.line_to(10.8, 4.2)
        cr.stroke()
        cr.rectangle(4.4, 9, 2.4, 2.4)
        cr.fill()
    elif kind == "play":
        cr.move_to(4, 2.2)
        cr.line_to(13.6, 8)
        cr.line_to(4, 13.8)
        cr.close_path()
        cr.fill()
    cr.restore()


def _hero_glyph_area(kind: str, size: int, rgba=None) -> Gtk.DrawingArea:
    """_draw_hero_glyph as a widget; without rgba in the label colour."""
    area = _windowless_area()
    area.set_size_request(size, size)
    area.set_valign(Gtk.Align.CENTER)

    def draw(widget, cr):
        color = rgba(widget) if callable(rgba) else rgba
        if color is None:
            c = widget.get_style_context().get_color(widget.get_style_context().get_state())
            color = (c.red, c.green, c.blue, c.alpha)
        _draw_hero_glyph(cr, kind, 0, 0, size, color)
        return False
    area.connect("draw", draw)
    return area


def _card_shadow(w: int, h: int, accent: Tuple[float, float, float]) -> Tuple[cairo.ImageSurface, int]:
    """The lifted card's shadow and glow - the box-shadow it used to have,
    0 16px 30px black 55% and 0 0 24px of the accent at 30%."""
    return _soft_shadow(w, h, 8, ((24, 0, (*accent, 0.30)), (30, 16, (0, 0, 0, 0.55))))


# a list row's lift (the CSS ease-out its margins moved by) and shadow
_ROW_EASE = _cubic_bezier(0, 0, 0.58, 1)
ROW_LIFT_SECONDS = 0.2


def _row_shadow(w: int, h: int, accent: Tuple[float, float, float], focused: bool):
    """0 12px 26px black 50% and 0 0 22px of the accent - a touch more of
    it stood on - as the row's box-shadow was."""
    return _soft_shadow(w, h, 14, ((22, 0, (*accent, 0.32 if focused else 0.28)), (26, 12, (0, 0, 0, 0.5))))


class GameCard(Gtk.Box):
    """One cover in the grid: lifts on hover, shows the title, playtime
    and a play button over a dark fade, carries a launcher badge and a
    favourite star. A running game's cover is dimmed - the hero up top
    has its timer and Stop button."""

    def __init__(self, app, game: GameItem, cover_w: int, cover_h: int):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.game = game
        self.hover = 0.0
        self._target = 0.0
        self._anim_id: Optional[int] = None
        self._menu_open = False
        # while the pointer may have left: see _on_leave
        self._watch_id: Optional[int] = None
        # the button the pointer is over (fav, menu, play), and how lit
        # each one is - eased like the lift
        self._hot: Optional[str] = None
        self._hot_level: Dict[str, float] = {"fav": 0.0, "menu": 0.0, "play": 0.0}
        # the lift, 0..1 (a touch over while it overshoots), and its tween:
        # drawn, not laid out - see _draw_lift
        self._lift = 0.0
        self._lift_from = 0.0
        self._lift_t0: Optional[int] = None
        self._last_us: Optional[int] = None
        self.plunderhub_uid = game.uid

        btn = Gtk.Button()
        btn.set_relief(Gtk.ReliefStyle.NONE)
        # Hugs the cover rather than the whole grid cell.
        btn.set_halign(Gtk.Align.CENTER)
        ctx = btn.get_style_context()
        ctx.add_class("game-cover")
        ctx.add_class("home-card")
        if game.uid in app.selected_uids:
            ctx.add_class("game-cover-selected")
        self.banner_btn = btn
        self.plunderhub_banner_btn = btn

        overlay = Gtk.Overlay()
        overlay.set_halign(Gtk.Align.CENTER)
        overlay.set_valign(Gtk.Align.START)
        if app.show_covers:
            base = app._create_cover_image(game, (cover_w, cover_h))
        else:
            # A windowless box, not a DrawingArea: a child with a GdkWindow
            # of its own can end up above the button's input window and
            # swallow the pointer (hover flickered off right away).
            # Without covers the card is a wide name plate, not an empty
            # portrait: as wide as a cover, about half as tall.
            base = Gtk.Box()
            base.set_size_request(cover_w, round(cover_w * 0.56))
            base.connect("draw", self._draw_placeholder)
        overlay.add(base)
        # The badge, star, fade and buttons are painted over the cover
        # after the overlay's own children, for the same reason.
        self.deco = overlay
        overlay.connect_after("draw", self._draw_deco)
        btn.add(overlay)
        btn.connect("draw", self._draw_lift)
        self.pack_start(btn, False, False, 0)

        btn.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK
                       | Gdk.EventMask.POINTER_MOTION_MASK)
        btn.connect("motion-notify-event", self._on_motion)
        btn.set_has_tooltip(True)
        btn.connect("query-tooltip", self._on_query_tooltip)
        btn.connect("enter-notify-event", lambda *_: (app._clear_new_game(game.uid), self._set_hover(1.0))[1])
        btn.connect("leave-notify-event", self._on_leave)
        # the cursor sits on the window every card shares: a card that goes
        # away under the pointer (unfavourited, stopped) puts it back
        btn.connect("unrealize", self._on_unrealize)
        btn.connect("button-press-event", self._on_press)
        btn.connect("clicked", lambda b: app._on_game_card_clicked(b, game))
        # After the launch dialog: the pointer may have left meanwhile,
        # without a leave event ever reaching the card.
        btn.connect_after("clicked", lambda *_: GLib.idle_add(self._settle_hover))
        btn.connect("focus-in-event", lambda *_: (app._clear_new_game(game.uid), self._queue_card(), False)[2])
        btn.connect("focus-out-event", lambda *_: self._queue_card())
        btn.connect("destroy", lambda *_: (self._stop_anim(), self._stop_watch()))
        app._grab_focus_on_hover(btn)
        app.selection_widgets.setdefault(game.uid, []).append((btn, None))
        if self._phase() in ("launching", "syncing"):
            self._ensure_anim()
        if game.uid in app._home_fade_uids:
            app._reveal_card(btn, 0, app._reveal_epoch)    # just found by this scan

    # --- state ---------------------------------------------------------

    def _phase(self) -> Optional[str]:
        if self.game.uid in self.app._cloud_syncs or self.game.uid in self.app._proton_waits:
            return "syncing"
        if self.game.uid in self.app.launching_uids:
            return "launching"
        if self.app._is_game_running(self.game.uid):
            return "running"
        return None

    def _on_leave(self, _w, event):
        if event.detail != Gdk.NotifyType.INFERIOR:
            self._set_hot(None)
            # Lifting moves the card up from under the pointer: it only
            # drops once the pointer is off where it stood and where it
            # rose to, or it would bob up and down at its edge.
            self._watch_pointer()
        return False

    # how far a card rises (button.home-card's top padding in theme.py)
    LIFT_PX = 10

    def _watch_pointer(self):
        if self._watch_id is None:
            self._watch_id = GLib.timeout_add(40, self._watch_tick)

    def _watch_tick(self) -> bool:
        if self._menu_open:
            return True     # an open card menu grabs the pointer - it stays lifted
        if self._pointer_near():
            return True
        self._watch_id = None
        self._set_hover(0.0)
        return False

    def _pointer_near(self) -> bool:
        """The pointer over the card or where it rises to and from."""
        if self.banner_btn.get_window() is None:
            return False
        x, y = self.banner_btn.get_pointer()
        alloc = self.banner_btn.get_allocation()
        return 0 <= x < alloc.width and -self.LIFT_PX <= y < alloc.height + self.LIFT_PX

    def _set_hover(self, target: float):
        if target != self._target:
            self._lift_from = self._lift
            self._lift_t0 = None       # the next frame starts the tween
        self._target = target
        self._ensure_anim()
        return False

    def _popup_menu(self, event):
        menu = self.app._game_menu_at(self.banner_btn, event, self.game)
        self._menu_open = True
        self._set_hover(1.0)
        menu.connect("closed", lambda *_: GLib.idle_add(self._on_menu_closed))

    def _on_menu_closed(self) -> bool:
        self._menu_open = False
        return self._settle_hover()

    def _settle_hover(self) -> bool:
        """Drops the lift unless the pointer really is over the card."""
        if self._menu_open or self.banner_btn.get_window() is None:
            return False
        if not self._pointer_near():
            self._set_hover(0.0)
        return False

    def _ensure_anim(self):
        if self._anim_id is None:
            self._last_us = None
            self._anim_id = self.banner_btn.add_tick_callback(self._tick)

    def _stop_watch(self):
        if self._watch_id is not None:
            GLib.source_remove(self._watch_id)
            self._watch_id = None

    def _stop_anim(self):
        if self._anim_id is not None:
            self.banner_btn.remove_tick_callback(self._anim_id)
            self._anim_id = None

    def _tick(self, _widget, frame_clock) -> bool:
        """While something eases: on the bubbles' steady beat of about
        60 fps (App._bubble_beat_frames) - a 10 px lift looks no different
        at 165, and costs three times the repaints - by elapsed time, the
        same pace at any refresh rate."""
        if (self._last_us is not None
                and frame_clock.get_frame_counter() % self.app._bubble_beat_frames(frame_clock)):
            return True
        now = frame_clock.get_frame_time()
        frames = min(4.0, (now - self._last_us) / 1_000_000 * 60) if self._last_us is not None else 1.0
        self._last_us = now
        self.hover += (self._target - self.hover) * (1 - 0.75 ** frames)
        if abs(self.hover - self._target) < 0.01:
            self.hover = self._target
        settled = True
        for zone, level in self._hot_level.items():
            goal = 1.0 if zone == self._hot else 0.0
            level += (goal - level) * (1 - 0.7 ** frames)
            if abs(goal - level) < 0.01:
                level = goal
            settled &= level == goal
            self._hot_level[zone] = level
        lift = self._lift
        if self._lift_t0 is None:
            self._lift_t0 = now
        progress = min(1.0, (now - self._lift_t0) / 1_000_000 / LIFT_SECONDS)
        self._lift = self._lift_from + (self._target - self._lift_from) * _LIFT_EASE(progress)
        if self._lift != lift:
            self._queue_lift_area()
        else:
            self._queue_card()
        if (self.hover == self._target and progress >= 1 and settled
                and self._phase() not in ("launching", "syncing")):
            self._anim_id = None
            return False
        return True

    def _queue_lift_area(self):
        """The card and all its shadow can reach, to be drawn again: GTK
        doesn't count the transparent box-shadow that makes room for it,
        so a plain queue_draw() left a strip of the glow behind above the
        card once it had settled (see _queue_area)."""
        btn = self.banner_btn
        btn.queue_draw()
        coords = self.deco.translate_coordinates(btn, 0, 0)
        w, h = self.deco.get_allocated_width(), self.deco.get_allocated_height()
        if coords and w > 0 and h > 0:
            ox, oy = coords
            pad = _card_shadow(w, h, _accent_rgb(btn))[1]
            _queue_area(btn, ox - pad, oy - pad - self.LIFT_PX * 1.3, w + 2 * pad, h + 2 * pad + self.LIFT_PX * 1.3)

    def _queue_card(self):
        """The card drawn again where it is now - lifted, its top is above
        the cover's own place, and a plain queue_draw() of that left the
        top of a lit button (the menu's ring) as it was."""
        lift = self._lift_px()
        if lift <= 0.01:
            self.deco.queue_draw()
            return
        coords = self.deco.translate_coordinates(self.banner_btn, 0, 0)
        if coords is None:
            self.deco.queue_draw()
            return
        ox, oy = coords
        # the button's own redraw (the card is drawn in it), and the band
        # above it the lifted card reaches into
        self.banner_btn.queue_draw()
        _queue_area(self.banner_btn, ox, oy - lift - 1, self.deco.get_allocated_width(),
                    self.deco.get_allocated_height() + lift + 2)

    def _lift_px(self) -> float:
        """How far up the card is drawn now. A selected card stays down,
        as it always has."""
        if self.game.uid in self.app.selected_uids:
            return 0.0
        return self.LIFT_PX * self._lift

    def _draw_lift(self, widget, cr) -> bool:
        """The lift, drawn: the card's content moved up within the button
        (its top padding is the room), the shadow under it from a blur made
        once - no relayout of the grid on every frame, as moving it by its
        margins did."""
        lift = self._lift_px()
        if lift <= 0.01:
            return False
        coords = self.deco.translate_coordinates(widget, 0, 0)
        ox, oy = coords if coords else (0, 0)
        w, h = self.deco.get_allocated_width(), self.deco.get_allocated_height()
        if w > 0 and h > 0:
            shadow, pad = _card_shadow(w, h, _accent_rgb(widget))
            cr.set_source_surface(shadow, ox - pad, oy - pad - lift)
            cr.paint_with_alpha(min(1.0, self._lift))
        cr.translate(0, -lift)
        return False

    # --- clicks ----------------------------------------------------------

    def _deco_point(self, event) -> Tuple[float, float, float, float]:
        coords = self.deco.translate_coordinates(self.banner_btn, 0, 0)
        ox, oy = coords if coords else (0, 0)
        # the buttons are painted where the lifted card is
        return (event.x - ox, event.y - oy + self._lift_px(),
                self.deco.get_allocated_width(), self.deco.get_allocated_height())

    def _on_press(self, _w, event) -> bool:
        if event.button == 3 and event.type == Gdk.EventType.BUTTON_PRESS:
            self._popup_menu(event)
            return True
        if event.button != 1 or event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        zone = self._zone_at(event)
        if zone == "fav":
            self.app._toggle_favorite(self.game)
            return True
        if zone == "menu":
            self._popup_menu(event)
            return True
        if zone == "play" and self._phase() == "running":
            self.app._on_stop_game_clicked(None, self.game)
            return True
        # Set sail is the card's own click
        return False

    def _zone_at(self, event) -> Optional[str]:
        """Which of the painted buttons the pointer is on, if any."""
        x, y, w, h = self._deco_point(event)
        if math.hypot(x - (w - 20), y - 20) <= 15:
            return "fav"
        if self.app.selection_mode:
            return None
        if self.app.show_card_menu_button and self.hover > 0.3 and math.hypot(x - (w - 52), y - 20) <= 15:
            return "menu"
        if self._phase() != "launching" and math.hypot(x - w / 2, y - h / 2) <= self._play_radius(w) + 4:
            return "play"
        return None

    @staticmethod
    def _play_radius(w: float) -> float:
        """Set sail's size: with the cover, within limits."""
        return max(20.0, min(34.0, w * 0.1))

    def _on_motion(self, _w, event) -> bool:
        self._set_hot(self._zone_at(event))
        return False

    def _on_unrealize(self, btn):
        if self._hot is not None:
            self._hot = None
            window = btn.get_window()
            if window is not None:
                window.set_cursor(None)

    def _set_hot(self, zone: Optional[str]):
        if zone == self._hot:
            return
        self._hot = zone
        window = self.banner_btn.get_window()
        if window is not None:
            cursor = Gdk.Cursor.new_from_name(window.get_display(), "pointer") if zone else None
            window.set_cursor(cursor)
        self.banner_btn.trigger_tooltip_query()
        self._ensure_anim()

    def _on_query_tooltip(self, _w, _x, _y, _keyboard, tooltip) -> bool:
        if self._hot is None:
            return False
        _ = self.app._
        running = self._phase() == "running"
        tooltip.set_text({"fav": _("gm_unfavorite") if self.game.uid in self.app.favorites else _("gm_favorite"),
                          "menu": _("lm_more_tooltip"),
                          "play": _("stop_game_tooltip") if running else _("launch_label")}[self._hot])
        return True

    # --- drawing ---------------------------------------------------------

    def _draw_placeholder(self, widget, cr):
        meta = self.app._home_playtime_text(self.game.uid) if self.app.show_playtime else None
        _paint_name_plate(cr, widget, self.game.title, meta, widget.get_allocated_width(),
                          widget.get_allocated_height())
        return False

    def _draw_deco(self, widget, cr):
        app = self.app
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        hv = self.hover
        phase = self._phase()
        accent = _accent_rgb(widget)
        _rounded(cr, 0, 0, w, h, 8)
        cr.clip()

        if phase == "running":
            cr.set_source_rgba(0, 0, 0, 0.5)
            cr.paint()
        elif hv > 0.02:
            # lifted: a soft sheen across the top of the cover
            sheen = cairo.LinearGradient(0, 0, w * 0.6, h * 0.55)
            sheen.add_color_stop_rgba(0, 1, 1, 1, 0.16 * hv)
            sheen.add_color_stop_rgba(1, 1, 1, 1, 0)
            cr.set_source(sheen)
            cr.paint()

        self._paint_static_layer(widget, cr, w, h, phase, accent)

        if hv > 0.02 and not app.selection_mode and phase != "launching":
            # in the middle of the cover
            cx, cy = w / 2, h / 2
            lit = self._hot_level["play"]
            base = self._play_radius(w)
            # pointed at: a little bigger, lighter, with a soft ring
            r = base * (1 + 0.15 * lit)
            if lit > 0.01:
                cr.new_path()   # the title's text left a current point behind
                cr.set_source_rgba(*(DANGER_RGB if phase == "running" else accent), 0.45 * lit * hv)
                cr.set_line_width(2)
                cr.arc(cx, cy, r + 4, 0, 2 * math.pi)
                cr.stroke()
            if phase == "running":
                cr.set_source_rgba(*_lighter(DANGER_RGB, 0.18 * lit), hv)
                cr.arc(cx, cy, r, 0, 2 * math.pi)
                cr.fill()
                cr.set_source_rgba(0.1, 0.02, 0.01, hv)
                side = r * 0.62
                _rounded(cr, cx - side / 2, cy - side / 2, side, side, side * 0.18)
                cr.fill()
            else:
                cr.set_source_rgba(*_lighter(accent, 0.18 * lit), hv)
                cr.arc(cx, cy, r, 0, 2 * math.pi)
                cr.fill()
                cr.set_source_rgba(*_on_accent_rgb(accent), hv)
                # the triangle's middle a little right of the circle's, so it looks centred
                t = r * 0.5
                cr.move_to(cx - t * 0.62, cy - t)
                cr.line_to(cx - t * 0.62, cy + t)
                cr.line_to(cx + t * 0.98, cy)
                cr.close_path()
                cr.fill()

        # favourite star
        fav = self.game.uid in app.favorites
        lit = self._hot_level["fav"]
        if fav or hv > 0.02 or lit > 0.01:
            alpha = 1.0 if fav else max(hv * 0.85, lit)
            grow = 1 + 0.15 * lit
            cr.set_source_rgba(0, 0, 0, (0.55 + 0.25 * lit) * alpha)
            cr.arc(w - 20, 20, 13 * grow, 0, 2 * math.pi)
            cr.fill()
            cr.new_path()
            for i in range(10):
                ang = -math.pi / 2 + i * math.pi / 5
                rr = (8 if i % 2 == 0 else 3.4) * grow
                cr.line_to(w - 20 + rr * math.cos(ang), 20.5 + rr * math.sin(ang))
            cr.close_path()
            if fav:
                cr.set_source_rgba(*_lighter(STAR_RGB, 0.2 * lit), 1)
                cr.fill()
            else:
                # pointed at: a hint of the gold it would turn
                cr.set_source_rgba(*STAR_RGB, 0.7 * lit)
                cr.fill_preserve()
                cr.set_source_rgba(1, 1, 1, alpha)
                cr.set_line_width(1.4)
                cr.stroke()

        # card menu, next to the star - unless switched off in Settings
        if hv > 0.02 and app.show_card_menu_button:
            lit = self._hot_level["menu"]
            r = 13 * (1 + 0.15 * lit)
            cr.set_source_rgba(0, 0, 0, (0.55 + 0.25 * lit) * hv)
            cr.arc(w - 52, 20, r, 0, 2 * math.pi)
            cr.fill()
            if lit > 0.01:
                cr.set_source_rgba(*accent, lit * hv)
                cr.set_line_width(1.5)
                cr.arc(w - 52, 20, r - 0.75, 0, 2 * math.pi)
                cr.stroke()
            cr.set_source_rgba(1, 1, 1, hv)
            for dx in (-5, 0, 5):
                cr.arc(w - 52 + dx, 20, 1.6 + 0.3 * lit, 0, 2 * math.pi)
                cr.fill()

        if phase in ("launching", "syncing"):
            cr.set_source_rgba(0, 0, 0, 0.62)
            cr.paint()
            spin = time.monotonic() * 5
            cx, cy = w / 2, h / 2 - 10
            cr.set_line_width(4)
            cr.set_source_rgba(1, 1, 1, 0.15)
            cr.arc(cx, cy, 22, 0, 2 * math.pi)
            cr.stroke()
            cr.set_source_rgba(*accent, 1)
            cr.arc(cx, cy, 22, spin, spin + 1.6)
            cr.stroke()
            busy = app.launch_status(self.game.uid) if phase == "syncing" else app._("game_launching_badge")
            _pango_text(cr, widget, busy or "", 8, cy + 34, w - 16, 10, True,
                        (1, 1, 1, 0.95), align_center=True)

        # Selection and keyboard focus are drawn on the cover itself - a
        # ring outside it gets clipped in the scrolling Recently played row.
        if self.banner_btn.has_visible_focus() or (app.selection_mode and self.game.uid in app.selected_uids):
            cr.reset_clip()
            cr.set_source_rgba(*accent, 1)
            cr.set_line_width(3)
            _rounded(cr, 1.5, 1.5, w - 3, h - 3, 8)
            cr.stroke()
        return False


    def _paint_static_layer(self, widget, cr, w: int, h: int, phase: Optional[str], accent):
        """The parts of the card that don't move - the fade, title and
        playtime, the launcher badge or selection check, the New tag -
        painted from images made once, not laid out again on every frame
        of a lift or an animated cover. Made again when any of it changes."""
        app = self.app
        title = self.game.title if app.show_game_titles and app.show_covers else None
        meta = app._home_playtime_text(self.game.uid) if app.show_playtime and app.show_covers else None
        selected = self.game.uid in app.selected_uids
        is_new = app._is_new_game(self.game.uid)
        key = (w, h, phase == "launching", title, meta, app._text_factor(), app.selection_mode, selected,
               is_new, is_new and app._("home_new_badge"), accent, self.game.launcher)
        cached = getattr(self, "_static_cache", None)
        if cached is None or cached[0] != key:
            window = widget.get_window()
            if window is None:
                return
            scale = window.get_scale_factor()
            options = PangoCairo.context_get_font_options(widget.get_pango_context())

            def render(top: int, height: int):
                surface = image_surface(window, cairo.FORMAT_ARGB32, w, max(1, height), scale)
                scr = cairo.Context(surface)
                if options is not None:
                    scr.set_font_options(options)
                scr.translate(0, -top)
                fade_top = self._paint_static(widget, scr, w, h, phase, accent, title, meta)
                return surface, fade_top
            # the badge's strip on top, the fade's part at the bottom -
            # not the whole cover's worth of pixels
            strip = 56
            top_surface, fade_top = render(0, strip)
            fade_top = max(strip, int(fade_top))
            parts = [(top_surface, 0)]
            if fade_top < h:
                parts.append((render(fade_top, h - fade_top)[0], fade_top))
            cached = self._static_cache = (key, parts)
        for surface, y in cached[1]:
            cr.set_source_surface(surface, 0, y)
            cr.paint()

    def _paint_static(self, widget, cr, w: int, h: int, phase: Optional[str], accent,
                      title: Optional[str], meta: Optional[str]) -> float:
        """Draws _paint_static_layer's parts; returns where the fade starts
        (h without one)."""
        app = self.app
        fade_top = float(h)
        # Each shows exactly when its setting is on - hovering doesn't
        # bring a switched-off title back, and the playtime doesn't wait
        # for the pointer.
        # Without covers the name plate draws its own name and playtime.
        if (title or meta) and phase != "launching":
            # The text grows and shrinks with the covers - as designed at
            # 300 px wide, within limits that stay readable - and with the
            # font size picked in Settings; never past what the cover's
            # width takes (a big text size on small covers).
            scale = min(1.6, max(0.8, w / 300)) * app._text_factor()
            # readable however small the text size, never bigger than the cover takes
            title_pt = min(max(12.5 * scale, 9.5), w / 13)
            meta_pt = min(max(10 * scale, 8.5), w / 16)
            pad = min(12 * scale, w * 0.06)
            gap = 10 * scale
            inner = w - 2 * pad
            meta_w = meta_h = 0
            if meta:
                meta_layout = _pango_layout(cr, widget, meta, inner, meta_pt, False)
                meta_w, meta_h = meta_layout.get_pixel_size()
            # The name bottom left, the playtime bottom right - side by side
            # while the name keeps most of the row, else the name over it,
            # so the two never run into each other.
            beside = not meta or inner - meta_w - gap >= inner * 0.5
            layout = None
            if title:
                title_w = inner - meta_w - gap if meta and beside else inner
                layout = _pango_layout(cr, widget, title, title_w, title_pt, True, lines=3)
            title_h = layout.get_pixel_size()[1] if layout is not None else 0
            bottom = h - 10 * scale
            meta_y = bottom - meta_h
            title_y = bottom - title_h if beside else meta_y - title_h - 2 * scale
            text_top = min(title_y if layout is not None else meta_y, meta_y)
            fade_top = min(h * 0.45, text_top - 40 * scale)
            fade = cairo.LinearGradient(0, fade_top, 0, h)
            fade.add_color_stop_rgba(0, 0, 0, 0, 0)
            fade.add_color_stop_rgba(1, 0, 0, 0, 0.88)
            cr.set_source(fade)
            cr.paint()
            if layout is not None:
                cr.set_source_rgba(1, 1, 1, 1)
                cr.move_to(pad, title_y)
                PangoCairo.show_layout(cr, layout)
            if meta:
                # on the title's last line when beside it
                if beside and layout is not None:
                    last = layout.get_line_readonly(layout.get_line_count() - 1)
                    _ink, logical = last.get_pixel_extents()
                    meta_y = bottom - meta_h - max(0, (logical.height - meta_h) / 2)
                cr.set_source_rgba(1, 1, 1, 0.82)
                cr.move_to(w - pad - meta_w, meta_y)
                PangoCairo.show_layout(cr, meta_layout)


        # launcher badge, or the selection check in selection mode
        if app.selection_mode:
            selected = self.game.uid in app.selected_uids
            cr.new_path()   # the title's text left a current point behind
            cr.arc(26, 26, 13, 0, 2 * math.pi)
            if selected:
                cr.set_source_rgba(*accent, 1)
                cr.fill()
                cr.set_source_rgba(*_on_accent_rgb(accent), 1)
                cr.set_line_width(2.4)
                cr.move_to(20, 26)
                cr.line_to(24.5, 30.5)
                cr.line_to(32, 21.5)
                cr.stroke()
            else:
                cr.set_source_rgba(0, 0, 0, 0.55)
                cr.fill_preserve()
                cr.set_source_rgba(1, 1, 1, 0.85)
                cr.set_line_width(1.6)
                cr.stroke()
        else:
            # No plate behind it - just a soft shadow so it reads on light covers.
            badge = app._home_launcher_badge(self.game.launcher, 32)
            if badge is not None:
                bx, by = 26 - badge.get_width() / 2, 26 - badge.get_height() / 2
                shadow = Gdk.cairo_surface_create_from_pixbuf(badge, 1, None)
                cr.set_source_rgba(0, 0, 0, 0.55)
                cr.mask_surface(shadow, bx + 1, by + 1.5)
                Gdk.cairo_set_source_pixbuf(cr, badge, bx, by)
                cr.paint()

        if app._is_new_game(self.game.uid):
            label = app._("home_new_badge")
            cr.save()
            # As tall as the 32 px launcher badge next to it.
            layout_w = 22 + 10.5 * len(label)
            _rounded(cr, 48, 11, layout_w, 30, 15)
            cr.set_source_rgba(*accent, 0.95)
            cr.fill()
            _pango_text(cr, widget, label, 48, 16, layout_w, 11.5, True,
                        (*_on_accent_rgb(accent), 1), align_center=True)
            cr.restore()
        return fade_top


class LauncherLogo(Gtk.DrawingArea):
    """A sidebar launcher's logo, drawn: sharp at any display scale, on a
    soft shadow (no tile round it), with a dot at its corner while the
    launcher runs. Chosen, it pops - a short grow and settle - and the
    accent's light comes up behind it; that moves on the frame clock, on
    the app's beat, only while it moves."""
    SIZE = 30
    POP_SECONDS = 0.38

    def __init__(self, app, name: str, running: bool):
        super().__init__()
        self.set_has_window(False)      # see _windowless_area
        self.app = app
        self.running = running
        self.active = False
        self._pop = 1.0         # 0..1 through the pop; 1 = settled
        self._light = 0.0       # the accent's light behind it, 0..1
        self._t0 = None
        self._tick = 0
        self.set_size_request(40, 40)
        self.set_valign(Gtk.Align.CENTER)
        scale = max(1, app.get_scale_factor())
        pb = app._load_launcher_icon(name, self.SIZE * scale).get_pixbuf()
        self._logo = Gdk.cairo_surface_create_from_pixbuf(pb, scale, None) if pb is not None else None
        self._shadow = self._blurred(pb) if pb is not None else None
        self.connect("draw", self._draw)
        self.connect("unrealize", lambda *_: self._stop())

    @staticmethod
    def _blurred(pb: GdkPixbuf.Pixbuf) -> cairo.ImageSurface:
        """The logo's shape, small: scaled back up it's a soft shadow."""
        small = pb.scale_simple(max(1, pb.get_width() // 4), max(1, pb.get_height() // 4),
                                GdkPixbuf.InterpType.BILINEAR)
        from plunderhub.ui.animated_hero import argb_surface
        return argb_surface(small)

    def set_active(self, on: bool, animate: bool = True):
        if on == self.active:
            return
        self.active = on
        settings = Gtk.Settings.get_default()
        if not animate or not self.get_mapped() or (settings is not None and not settings.props.gtk_enable_animations):
            self._stop()
            self._pop, self._light = 1.0, (1.0 if on else 0.0)
            self.queue_draw()
            return
        self._t0 = None
        self._from_light = self._light
        if not self._tick:
            self._tick = self.add_tick_callback(self._step)

    def _stop(self):
        if self._tick:
            self.remove_tick_callback(self._tick)
            self._tick = 0

    def _step(self, _widget, clock) -> bool:
        if self._t0 is not None and clock.get_frame_counter() % self.app._bubble_beat_frames(clock):
            return True
        now = clock.get_frame_time() / 1_000_000
        if self._t0 is None:
            self._t0 = now
        t = min(1.0, (now - self._t0) / self.POP_SECONDS)
        self._pop = t if self.active else 1.0
        goal = 1.0 if self.active else 0.0
        self._light = self._from_light + (goal - self._from_light) * (1 - (1 - t) ** 3)
        self.queue_draw()
        if t >= 1.0:
            self._tick = 0
            return False
        return True

    def _draw(self, widget, cr) -> bool:
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        cx, cy = w / 2, h / 2
        accent = _accent_rgb(widget)
        if self._light > 0:
            light = cairo.RadialGradient(cx, cy, 0, cx, cy, w / 2)
            light.add_color_stop_rgba(0, *accent, 0.42 * self._light)
            light.add_color_stop_rgba(1, *accent, 0)
            cr.set_source(light)
            cr.paint()
        # a grow past full size and back: up 18 % at its height
        grow = 1 + 0.18 * math.sin(math.pi * self._pop) * (1 - self._pop * 0.35) if self._pop < 1 else 1.0
        size = self.SIZE * grow
        x, y = cx - size / 2, cy - size / 2
        if self._shadow is not None:
            cr.save()
            cr.translate(x, y + 2)
            sw = self._shadow.get_width()
            cr.scale(size / sw, size / self._shadow.get_height())
            pattern = cairo.SurfacePattern(self._shadow)
            pattern.set_filter(cairo.FILTER_BILINEAR)
            cr.set_source_rgba(0, 0, 0, 0.5)
            cr.mask(pattern)
            cr.restore()
        if self._logo is not None:
            cr.save()
            cr.translate(x, y)
            cr.scale(grow, grow)
            cr.set_source_surface(self._logo, 0, 0)
            cr.get_source().set_filter(cairo.FILTER_GOOD if grow != 1 else cairo.FILTER_FAST)
            cr.paint()
            cr.restore()
        if self.running:
            # the dot at its corner, cut out of what's behind by a dark ring
            dx, dy = cx + self.SIZE / 2 - 3, cy + self.SIZE / 2 - 3
            cr.arc(dx, dy, 6, 0, 2 * math.pi)
            cr.set_source_rgba(0.04, 0.09, 0.08, 1)
            cr.fill()
            cr.arc(dx, dy, 4, 0, 2 * math.pi)
            cr.set_source_rgba(*accent, 1)
            cr.fill()
        return False


class HomeMixin:
    # --- chrome: sidebar ------------------------------------------------

    def _build_home_sidebar(self) -> Gtk.Box:
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        side.get_style_context().add_class("home-sidebar")
        side.set_size_request(230, -1)
        self.home_nav = self.settings.get("home_nav", "all")
        self._home_nav_updating = False
        self._home_sidebar_signature = None
        self._home_count_labels: Dict[str, Gtk.Label] = {}
        self._home_nav_text_labels: Dict[str, Tuple[Gtk.Label, str]] = {}
        self._home_badge_cache: Dict[Tuple[str, int], Optional[GdkPixbuf.Pixbuf]] = {}

        brand_btn = Gtk.Button()
        brand_btn.set_relief(Gtk.ReliefStyle.NONE)
        brand_btn.set_can_focus(False)
        brand_btn.get_style_context().add_class("home-brand-btn")
        brand = Gtk.Box(spacing=10)
        logo = Gtk.Image()
        logo.set_valign(Gtk.Align.CENTER)
        brand.pack_start(logo, False, False, 0)
        # Two-tone wordmark, as in Console Mode: "Plunder" + accent "Hub".
        word = Gtk.Box()
        for part, accent in (("Plunder", False), ("Hub", True)):
            label = Gtk.Label(label=part)
            label.get_style_context().add_class("home-brand")
            if accent:
                label.get_style_context().add_class("home-brand-accent")
            word.pack_start(label, False, False, 0)
        word.set_valign(Gtk.Align.CENTER)
        brand.pack_start(word, False, False, 0)
        if APP_CHANNEL:
            beta = Gtk.Label(label=self._("beta_badge").upper())
            beta.get_style_context().add_class("beta-pill")
            beta.set_valign(Gtk.Align.CENTER)
            brand.pack_start(beta, False, False, 0)
            self._home_beta_label = beta

        def size_logo(label: Gtk.Label):
            # The logo in the same proportion to the wordmark as in
            # Console Mode (64 px next to 26 pt text): ~1.85x its height.
            icon_file = _app_icon_file()
            if icon_file is None:
                return
            pt = label.get_pango_context().get_font_description().get_size() / Pango.SCALE
            px = max(32, round(pt * 96 / 72 * 1.85))
            # every hover restyles the label: only a new font size counts
            if getattr(logo, "plunderhub_px", None) == px:
                return
            logo.plunderhub_px = px
            try:
                logo.set_from_pixbuf(GdkPixbuf.Pixbuf.new_from_file_at_size(str(icon_file), px, px))
            except Exception as e:
                logger.debug(f"Couldn't load the app icon for the sidebar: {e}")
        first = word.get_children()[0]
        first.connect("style-updated", size_logo)
        first.connect("realize", size_logo)
        brand_btn.add(brand)
        # As wide as the nav rows under it, the same 8 px in from both
        # sides - its hover no longer runs into the sidebar's edge.
        brand.set_halign(Gtk.Align.START)
        brand_btn.set_margin_top(10)
        brand_btn.set_margin_bottom(6)
        brand_btn.set_margin_start(8)
        brand_btn.set_margin_end(8)
        brand_btn.connect("clicked", lambda *_: self._show_welcome())
        self.home_brand_btn = brand_btn
        side.pack_start(brand_btn, False, False, 0)

        self.home_nav_main = Gtk.ListBox()
        self.home_nav_main.get_style_context().add_class("home-nav")
        for key, icon, text_key in (("all", "view-grid-symbolic", "all_games_section"),
                                    ("fav", "starred-symbolic", "home_nav_favorites"),
                                    ("recent", "clock-symbolic", "home_nav_recent")):
            self.home_nav_main.add(self._home_nav_row(key, self._icon_image_at_size(icon, 18), text_key))
        side.pack_start(self.home_nav_main, False, False, 0)

        self.home_nav_head = Gtk.Label(label=self._("sources_header_launchers").upper(), xalign=0)
        self.home_nav_head.set_line_wrap(True)
        self.home_nav_head.set_max_width_chars(24)
        self.home_nav_head.get_style_context().add_class("home-nav-head")
        side.pack_start(self.home_nav_head, False, False, 0)

        self.home_nav_launchers = Gtk.ListBox()
        self.home_nav_launchers.get_style_context().add_class("home-nav")
        side.pack_start(self.home_nav_launchers, False, False, 0)

        from plunderhub.ui.reorder_view import LauncherOrderList
        self.home_launcher_order = LauncherOrderList(
            self, on_reorder=self._on_launchers_reordered,
            on_pick=lambda name: self._set_home_nav(f"l:{name}"))
        self.home_launcher_order.set_no_show_all(True)
        side.pack_start(self.home_launcher_order, False, False, 0)

        self.home_nav_main.connect("row-selected", self._on_home_nav_selected, self.home_nav_launchers)
        self.home_nav_launchers.connect("row-selected", self._on_home_nav_selected, self.home_nav_main)

        side.pack_start(Gtk.Box(), True, True, 0)
        # while a Proton build downloads (ui/proton_page.py)
        from plunderhub.ui import proton_page
        self._proton_card = proton_page.HomeDownloadCard(
            self, proton_page.RealProtonData(self), lambda: self.show_proton_page())
        side.pack_start(self._proton_card, False, False, 0)
        side.pack_start(self._build_update_card(), False, False, 0)

        bottom = Gtk.ListBox()
        bottom.get_style_context().add_class("home-nav")
        bottom.set_selection_mode(Gtk.SelectionMode.NONE)
        self._home_bottom_actions = {}
        # inspection problems, the missing part of the Recommended kit and
        # updates, side by side when more than one waits
        self._shipwright_badges = Gtk.Box(spacing=4)
        self._shipwright_badges.set_valign(Gtk.Align.CENTER)
        self._shipwright_warn_badge, self._shipwright_rec_badge, self._shipwright_badge = \
            Gtk.Label(), Gtk.Label(), Gtk.Label()
        self._shipwright_attention = {"updates": 0, "warnings": 0, "recommended": []}
        for badge, extra_class in ((self._shipwright_warn_badge, "sw-attention-warn"),
                                   (self._shipwright_rec_badge, "sw-attention-rec"),
                                   (self._shipwright_badge, None)):
            ctx = badge.get_style_context()
            ctx.add_class("sw-attention-badge")
            if extra_class:
                ctx.add_class(extra_class)
            badge.set_no_show_all(True)
            self._shipwright_badges.pack_start(badge, False, False, 0)
        for key, icon, text_key, action in (
                ("shipwright", "hammer-symbolic", "sw_title", lambda: self.show_shipwright()),
                ("dashboard", "office-chart-bar-symbolic", "dashboard_title", lambda: self.show_dashboard()),
                ("console", "input-gaming-symbolic", "console_mode_tooltip", lambda: self._toggle_console_mode()),
                ("settings", "preferences-system-symbolic", "options", lambda: self.show_options())):
            row = self._home_nav_row(key, self._icon_image_at_size(icon, 18), text_key, with_count=False,
                                     extra=self._shipwright_badges if key == "shipwright" else None)
            self._home_bottom_actions[row] = action
            bottom.add(row)
        bottom.connect("row-activated", self._on_home_bottom_action)
        # a quiet look for updates, inspection problems and the Recommended kit, once settled in
        GLib.timeout_add_seconds(40, self._check_shipwright_attention)
        self.home_nav_bottom = bottom
        bottom.set_margin_bottom(12)
        side.pack_start(bottom, False, False, 0)
        self.home_sidebar = side
        return side

    # --- Shipwright: opening it, and the badge on its sidebar row -------------------

    def _set_shipwright_attention(self, updates: Optional[int] = None, warnings: Optional[int] = None,
                                  recommended: Optional[List[str]] = None):
        """The badges on the sidebar's Shipwright row: amber, how many Ship
        inspection problems wait; outlined, how much of the Recommended
        kit is missing (its names in the tooltip); filled, how many
        updates - all at once when all wait. None keeps what's shown."""
        badge = getattr(self, "_shipwright_badge", None)
        if badge is None:
            return
        att = self._shipwright_attention
        for key, value in (("updates", updates), ("warnings", warnings), ("recommended", recommended)):
            if value is not None:
                att[key] = value
        warnings, updates, missing = att["warnings"], att["updates"], att["recommended"]
        warn = self._shipwright_warn_badge
        warn.set_text(f"⚠ {warnings}")
        warn.set_tooltip_text(self._("insp_summary_warn").format(n=warnings) if warnings else None)
        warn.set_visible(warnings > 0)
        rec = self._shipwright_rec_badge
        rec.set_text(f"✦ {len(missing)}")
        rec.set_tooltip_text(f"{self._('sw_recommended')}: " + self._("sw_recommended_missing").format(
            n=len(missing), names=", ".join(missing)) if missing else None)
        rec.set_visible(bool(missing))
        badge.set_text(f"⟳ {updates}")
        badge.set_tooltip_text(self._("sw_updates_waiting").format(n=updates) if updates else None)
        badge.set_visible(updates > 0)

    def _check_shipwright_attention(self) -> bool:
        """Once, a while after start: whether the badge should show. Quiet -
        nothing pops up - and off with the update check."""
        if not self.update_check_enabled:
            return False

        def work():
            from plunderhub.core import flatpak_layers, inspection
            from plunderhub.core.tools import TOOLS, TOOLS_BY_ID, pending_updates
            from plunderhub.core.utils import detect_linux_distro_family
            from plunderhub.launchers.registry import LAUNCHER_REGISTRY
            from plunderhub.ui import launcher_tasks
            from plunderhub.ui.shipwright import FLATPAK_PARTS, RECOMMENDED
            try:
                packages, flatpaks, here = {}, {}, set()
                for name in [t.name for t in TOOLS] + [l.name for l in LAUNCHER_REGISTRY]:
                    st = launcher_tasks.launcher_state(name)
                    if st.installed and not st.partial:
                        here.add(name)
                    if st.installed and st.package:
                        packages[name] = st.package
                    elif st.installed and st.flatpak_id:
                        flatpaks[name] = st.flatpak_id
                updates, _system = pending_updates(detect_linux_distro_family(), packages, flatpaks)
                warnings = sum(1 for c in inspection.run_checks() if c.status == inspection.WARN)
                # as Shipwright counts it: the tool, and a Flatpak launcher's copy of it
                launchers = flatpak_layers.flatpak_launchers()
                have = flatpak_layers.installed_refs() if launchers else set()
                missing = []
                for key in RECOMMENDED:
                    tool = TOOLS_BY_ID[key]
                    ok = tool.name in here
                    if ok and key in FLATPAK_PARTS and launchers:
                        layer = flatpak_layers.LAYERS_BY_ID[FLATPAK_PARTS[key].split(":", 1)[1]]
                        ok = all(r in have for r in flatpak_layers.needed_refs(layer, launchers))
                    if not ok:
                        missing.append(self._(f"sw_name_{key}") if f"sw_name_{key}" in self.strings else tool.name)
            except Exception as e:
                logger.debug(f"Shipwright check: {e}")
                return
            GLib.idle_add(lambda: (self._set_shipwright_attention(len(updates), warnings, missing), False)[1])
        threading.Thread(target=work, daemon=True).start()
        return False

    def show_shipwright(self, focus=None, parent=None, on_closed=None):
        """Shipwright, the tool installer - focus is a tool id to scroll to."""
        from plunderhub.ui import shipwright
        shipwright.show(self, parent, focus, on_closed)

    # --- the "Update available" card ------------------------------------------------

    def _build_update_card(self) -> Gtk.Revealer:
        """A card above the bottom rows while a newer PlunderHub is out:
        a softly glowing badge, the versions, Install and Later."""
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.get_style_context().add_class("home-update-card")
        top = Gtk.Box(spacing=10)
        badge = Gtk.DrawingArea()
        badge.set_size_request(36, 36)
        badge.set_valign(Gtk.Align.CENTER)
        self._update_glow_t = 0.0

        def draw_badge(widget, cr):
            accent = _accent_rgb(widget)
            pulse = 0.5 + 0.5 * math.sin(self._update_glow_t)
            glow = cairo.RadialGradient(18, 18, 8, 18, 18, 18)
            glow.add_color_stop_rgba(0, *accent, 0.30 + 0.25 * pulse)
            glow.add_color_stop_rgba(1, *accent, 0)
            cr.set_source(glow)
            cr.paint()
            cr.arc(18, 18, 12, 0, 2 * math.pi)
            cr.set_source_rgb(*accent)
            cr.fill()
            pb = get_icon_pixbuf("system-software-install-symbolic", 14,
                                 "#%02x%02x%02x" % tuple(round(c * 255) for c in _on_accent_rgb(accent)))
            if pb is not None:
                Gdk.cairo_set_source_pixbuf(cr, pb, 11, 11)
                cr.paint()
            return False
        badge.connect("draw", draw_badge)
        self._update_card_badge = badge
        top.pack_start(badge, False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        texts.set_valign(Gtk.Align.CENTER)
        self._update_card_title = Gtk.Label(xalign=0)
        self._update_card_title.get_style_context().add_class("home-update-title")
        self._update_card_title.set_ellipsize(Pango.EllipsizeMode.END)
        self._update_card_versions = Gtk.Label(xalign=0)
        self._update_card_versions.get_style_context().add_class("home-update-versions")
        texts.pack_start(self._update_card_title, False, False, 0)
        texts.pack_start(self._update_card_versions, False, False, 0)
        top.pack_start(texts, True, True, 0)
        card.pack_start(top, False, False, 0)
        buttons = Gtk.Box(spacing=6, homogeneous=True)
        self._update_card_install = Gtk.Button()
        self._update_card_later = Gtk.Button()
        for btn, css in ((self._update_card_later, "home-update-later"), (self._update_card_install, "home-update-install")):
            btn.get_style_context().add_class(css)
            btn.set_can_focus(False)
            buttons.pack_start(btn, True, True, 0)
        self._update_card_install.connect("clicked", lambda *_: self._show_update_install_choice_dialog())
        self._update_card_later.connect("clicked", lambda *_: self._dismiss_update_banner())
        card.pack_start(buttons, False, False, 0)
        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        revealer.set_transition_duration(260)
        revealer.add(card)
        revealer.set_no_show_all(True)
        self._update_card = revealer
        self._update_glow_id = None
        return revealer

    def _set_update_card(self, tag: Optional[str]):
        """Shows the card for tag - or, with None, hides it."""
        card = getattr(self, "_update_card", None)
        if card is None:
            return
        if tag:
            self._update_card_tag = tag
            self._refresh_update_card_text()
            card.set_no_show_all(False)
            card.show_all()
            card.set_reveal_child(True)
            if self._update_glow_id is None:
                self._update_glow_id = GLib.timeout_add(50, self._update_glow_tick)
        else:
            card.set_reveal_child(False)
            if self._update_glow_id is not None:
                GLib.source_remove(self._update_glow_id)
                self._update_glow_id = None

    def _refresh_update_card_text(self):
        tag = getattr(self, "_update_card_tag", None)
        if tag is None or getattr(self, "_update_card", None) is None:
            return
        self._update_card_title.set_text(self._("update_card_title"))
        current = f"v{APP_VERSION}"
        new = tag if tag.startswith("v") else f"v{tag}"
        self._update_card_versions.set_text(f"{current}  →  {new}")
        self._update_card_install.set_label(self._("update_install_btn"))
        self._update_card_later.set_label(self._("update_later_btn"))

    def _update_glow_tick(self) -> bool:
        # Breathes slowly; holds still whenever the decorative
        # animations do (window out of focus, a game running).
        # Twenty breaths (about a minute), then it glows still: a window
        # left alone must not keep waking up and repainting.
        if not self._should_pause_decorative_animations():
            self._update_glow_t += 0.12
            if self._update_glow_t >= 40 * math.pi:
                self._update_glow_t = math.pi / 2       # the brightest point
                self._update_glow_id = None
                self._update_card_badge.queue_draw()
                return False
            self._update_card_badge.queue_draw()
        return True

    def _on_home_bottom_action(self, _listbox, row):
        self._home_bottom_actions[row]()
        # Settings and the dashboard are dialogs: closing one gave focus
        # back to the row, which then stayed highlighted.
        if row.get_window() is not None:
            self._release_card_after_dialog(row)

    def _home_nav_row(self, key: str, icon: Gtk.Widget, text_key: Optional[str], text: Optional[str] = None,
                      with_count: bool = True, indent: bool = False,
                      extra: Optional[Gtk.Widget] = None) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.home_key = key
        box = Gtk.Box(spacing=12)
        if indent:
            box.set_margin_start(26)
        box.pack_start(icon, False, False, 0)
        label = Gtk.Label(label=self._(text_key) if text_key else text, xalign=0)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_valign(Gtk.Align.BASELINE)
        box.pack_start(label, False, False, 0)
        if text_key:
            self._home_nav_text_labels[key] = (label, text_key)
        if with_count:
            # right after the name, small - it belongs to it
            box.pack_start(self._home_count_label(key), False, False, 0)
        if extra is not None:
            box.pack_end(extra, False, False, 0)
        row.add(box)
        return row

    # how long after its actions appear a click on them still counts as
    # the row's: the second click of a quick double-click isn't a launch
    LAUNCHER_ACTIONS_GRACE = 0.4

    def _home_count_label(self, key: str) -> Gtk.Label:
        count = Gtk.Label()
        count.set_valign(Gtk.Align.BASELINE)
        count.get_style_context().add_class("home-count")
        self._apply_tabular_figures(count)
        self._home_count_labels[key] = count
        return count

    def _home_launcher_row(self, name: str, cmd: Optional[List[str]]) -> Gtk.ListBoxRow:
        """A launcher in the sidebar: its logo, name and games. Open or
        Close and the menu open under the row once it's chosen (or stood
        on with the keyboard or a controller) - never under the pointer
        that's choosing it, so a click picks the launcher's games and
        nothing starts by mistake."""
        running = self._launcher_is_running(name)
        logo = LauncherLogo(self, name, running)
        row = Gtk.ListBoxRow()
        row.home_key = f"l:{name}"
        row.get_style_context().add_class("home-launcher-row")
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        top = Gtk.Box(spacing=10)
        top.pack_start(logo, False, False, 0)
        names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        names.set_valign(Gtk.Align.CENTER)
        label = Gtk.Label(label=name, xalign=0)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_valign(Gtk.Align.BASELINE)
        name_line = Gtk.Box(spacing=12)  # the gap of the other rows' counts
        name_line.pack_start(label, False, False, 0)
        name_line.pack_start(self._home_count_label(row.home_key), False, False, 0)
        names.pack_start(name_line, False, False, 0)
        if running:
            # on the name's line, after its games: the sidebar has the room
            status = Gtk.Label(label=self._("lm_running"), xalign=0)
            status.set_valign(Gtk.Align.BASELINE)
            status.set_margin_start(4)
            status.get_style_context().add_class("home-launcher-status")
            name_line.pack_start(status, False, False, 0)
        top.pack_start(names, True, True, 0)
        column.pack_start(top, False, False, 0)

        from plunderhub.ui.game_page import _icon
        actions = Gtk.Box(spacing=6)
        actions.set_margin_start(50)
        actions.set_margin_top(6)
        actions.set_margin_bottom(2)
        state = {"shown_at": 0.0}

        def guarded(handler):
            def on_click(button):
                if time.monotonic() - state["shown_at"] >= self.LAUNCHER_ACTIONS_GRACE:
                    handler(button)
            return on_click
        action = Gtk.Button()
        action.set_can_focus(True)
        inner = Gtk.Box(spacing=6)
        # in the pill's own text colour, hover included
        inner.pack_start(_icon("media-playback-stop-symbolic" if running else "media-playback-start-symbolic", 12),
                         False, False, 0)
        inner.pack_start(Gtk.Label(label=self._("launcher_close" if running else "launcher_open")), False, False, 0)
        action.add(inner)
        action.get_style_context().add_class("home-launcher-close" if running else "home-launcher-open")
        if running:
            action.connect("clicked", guarded(lambda b: self._on_close_launcher_clicked(name)))
        elif cmd:
            action.connect("clicked", guarded(lambda b: self.request_launch_launcher(cmd, name, b)))
        else:
            action.set_sensitive(False)
        actions.pack_start(action, False, False, 0)
        more = None
        if self.show_card_menu_button:
            more = Gtk.Button()
            more.set_can_focus(True)
            more.add(_icon("view-more-symbolic", 12))
            more.set_tooltip_text(self._("lm_more_tooltip"))
            more.get_style_context().add_class("home-launcher-more")
            more.connect("clicked", guarded(lambda b: self._launcher_menu_at(b, name)))
            actions.pack_start(more, False, False, 0)
        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        revealer.set_transition_duration(180)
        revealer.add(actions)
        column.pack_start(revealer, False, False, 0)

        ev = Gtk.EventBox()
        ev.add(column)
        row.add(ev)
        ev.connect("button-press-event", self._show_launcher_context_menu, name)
        # what the right arrow steps onto, in order
        row.launcher_actions = [w for w in (action if action.get_sensitive() else None, more) if w is not None]

        def update(animate: bool = True):
            chosen = self.home_nav == row.home_key or (name == "Heroic" and self.home_nav.startswith("s:"))
            stood_on = row.has_focus() or any(w.has_focus() for w in row.launcher_actions)
            show = chosen or stood_on
            if show and not revealer.get_reveal_child():
                state["shown_at"] = time.monotonic()
            revealer.set_transition_duration(180 if animate else 0)
            revealer.set_reveal_child(show)
            logo.set_active(chosen, animate)
            return False
        row.plunderhub_update = update

        def pick():
            # the controller's A stood on the row: pick it, then its menu
            # (Open first), like a game's cover
            self._set_home_nav(row.home_key)
            self._home_select_nav_row()
            again = next((r for r in self.home_nav_launchers.get_children()
                          if getattr(r, "home_key", None) == row.home_key), None)
            if again is not None and again is not row:
                self._grab_keyboard_focus(again)
        row.plunderhub_pick = pick

        def launch() -> bool:
            # Enter stood on the row: pick it, and open the launcher - or
            # close it, while it runs
            self._set_home_nav(row.home_key)
            self._home_select_nav_row()
            # picking Heroic rebuilds the rows (its store chips): stay on it
            again = next((r for r in self.home_nav_launchers.get_children()
                          if getattr(r, "home_key", None) == row.home_key), None)
            if again is not None and again is not row:
                self._grab_keyboard_focus(again)
            if running:
                self._on_close_launcher_clicked(name)
            elif cmd:
                self.request_launch_launcher(cmd, name, action)
            return True
        row.plunderhub_launch = launch
        update(animate=False)
        for widget in [row] + row.launcher_actions:
            widget.connect("focus-in-event", lambda *_: GLib.idle_add(update) and False)
            widget.connect("focus-out-event", lambda *_: GLib.idle_add(update) and False)
        return row

    def _refresh_home_sidebar(self, force: bool = False):
        launchers = [s for s in self._ordered_launcher_sections(self._cached_launchers)]
        names = [s["name"] for s in launchers]
        stores = []
        if "Heroic" in names and (self.home_nav == "l:Heroic" or self.home_nav.startswith("s:")):
            heroic = next(s for s in launchers if s["name"] == "Heroic")
            present = {(g.store_platform or "epic") for g in heroic["games"]
                       if not self.scanner.is_hidden(g.launcher, g.uid)}
            stores = [k for k in HEROIC_STORES if k in present]
            if len(stores) < 2 and not self.home_nav.startswith("s:"):
                stores = []
        valid = {"all", "fav", "recent"} | {f"l:{n}" for n in names} | {f"s:{k}" for k in stores}
        if self.home_nav not in valid:
            self.home_nav = "all"
        editing = self.reorder_mode
        self.home_launcher_order.set_visible(editing)
        self.home_nav_launchers.set_visible(not editing)
        self.home_nav_head.set_text(self._("reorder_launchers_head" if editing else "sources_header_launchers")
                                    .upper())
        if editing:
            self.home_launcher_order.set_items(names, self.home_nav[2:] if self.home_nav.startswith("l:") else None)
        signature = (tuple(names), tuple(stores), tuple(sorted(self.running_launcher_procs)),
                     tuple(sorted(self._launchers_running)), self.lang, self.show_card_menu_button)
        if force or signature != self._home_sidebar_signature:
            self._home_sidebar_signature = signature
            self._home_nav_updating = True
            try:
                for row in list(self.home_nav_launchers.get_children()):
                    key = getattr(row, "home_key", "")
                    self._home_count_labels.pop(key, None)
                    self.home_nav_launchers.remove(row)
                # Heroic's stores are chips over its games (_home_store_chips)
                for section in launchers:
                    self.home_nav_launchers.add(self._home_launcher_row(section["name"], section["cmd"]))
                self.home_nav_launchers.show_all()
                self.home_nav_head.set_visible(bool(names))
            finally:
                self._home_nav_updating = False
        self._home_select_nav_row()
        self._refresh_home_counts()

    def _home_select_nav_row(self):
        for row in self.home_nav_launchers.get_children():
            update = getattr(row, "plunderhub_update", None)
            if update is not None:
                update()
        self._home_nav_updating = True
        # one of Heroic's stores is Heroic in the sidebar
        key = "l:Heroic" if self.home_nav.startswith("s:") else self.home_nav
        try:
            for listbox in (self.home_nav_main, self.home_nav_launchers):
                target = next((r for r in listbox.get_children()
                               if getattr(r, "home_key", None) == key), None)
                if target is not None:
                    listbox.select_row(target)
                else:
                    listbox.unselect_all()
        finally:
            self._home_nav_updating = False

    def _refresh_home_counts(self):
        visible = self._home_all_visible_games()
        counts: Dict[str, int] = {
            "all": len(visible),
            "fav": sum(1 for g in visible if g.uid in self.favorites),
            "recent": sum(1 for g in visible if self.recent.get(g.uid)),
        }
        for g in visible:
            counts[f"l:{g.launcher}"] = counts.get(f"l:{g.launcher}", 0) + 1
            if g.launcher == "Heroic":
                key = f"s:{g.store_platform or 'epic'}"
                counts[key] = counts.get(key, 0) + 1
        for key, label in self._home_count_labels.items():
            n = counts.get(key, 0)
            label.set_text(str(n) if n else "")

    def _on_home_nav_selected(self, _listbox, row, other):
        if self._home_nav_updating or row is None:
            return
        self._home_nav_updating = True
        try:
            other.unselect_all()
        finally:
            self._home_nav_updating = False
        self._set_home_nav(row.home_key)

    def _set_home_nav(self, key: str):
        if key == self.home_nav:
            return
        self.home_nav = key
        self.settings["home_nav"] = key
        save_settings(self.settings)
        if self.reorder_mode and not key.startswith("l:"):
            self._exit_reorder_mode()
            return
        self._render_launchers(animate=True)
        self.scroller.get_vadjustment().set_value(0)

    # --- keyboard / controller: sidebar and hero --------------------------

    def _home_sidebar_rows(self) -> List[Gtk.ListBoxRow]:
        rows = []
        for listbox in (self.home_nav_main, self.home_nav_launchers, self.home_nav_bottom):
            if listbox.get_visible():
                rows.extend(r for r in listbox.get_children() if r.get_visible())
        return rows

    def _home_focus_sidebar(self) -> bool:
        rows = self._home_sidebar_rows()
        target = next((r for r in rows if getattr(r, "home_key", None) == self.home_nav), rows[0] if rows else None)
        if target is None:
            return False
        self._grab_keyboard_focus(target)
        return True

    def _home_focus_content(self) -> bool:
        grid = getattr(self, "_reorder_grid", None)
        if self.reorder_mode and grid is not None and grid.get_mapped():
            self._grab_keyboard_focus(grid)
            return True
        hero = getattr(self, "_home_hero_play", None)
        if hero is not None and hero.get_mapped():
            self._grab_keyboard_focus(hero)
            return True
        for group in self._nav_groups:
            if self._focus_first_child_in_group(group):
                return True
        return False

    def _reorder_input(self, action: str) -> bool:
        """Edit order by keyboard or controller: the covers, the
        sidebar's launcher list and the banner's Reset / Done, and
        between them. True when it was dealt with."""
        if not self.reorder_mode:
            return False
        focused = self.get_focus()
        grid = getattr(self, "_reorder_grid", None)
        grid = grid if grid is not None and grid.get_mapped() else None
        order_list = self.home_launcher_order if self.home_launcher_order.get_visible() else None
        buttons = [b for b in getattr(self, "_reorder_buttons", []) if b.get_mapped()]
        main_rows = [r for r in self.home_nav_main.get_children() if r.get_visible()]
        bottom_rows = [r for r in self.home_nav_bottom.get_children() if r.get_visible()]
        if focused is not None and focused in (grid, order_list):
            leave = focused.handle(action)
            if leave is None:
                return True
            if leave == "back":
                self._exit_reorder_mode()
            elif focused is grid:
                if leave == "up" and buttons:
                    self._grab_keyboard_focus(buttons[-1])
                elif leave == "left":
                    if order_list is not None and self.home_nav.startswith("l:"):
                        order_list.grab_focus()
                    else:
                        self._home_focus_sidebar()
            elif leave == "right":
                grid = getattr(self, "_reorder_grid", None)     # a new one, for another launcher
                if grid is not None:
                    self._grab_keyboard_focus(grid)
            elif leave == "up" and main_rows:
                self._grab_keyboard_focus(main_rows[-1])
            elif leave == "down" and bottom_rows:
                self._grab_keyboard_focus(bottom_rows[0])
            return True
        if focused is not None and focused in buttons:
            i = buttons.index(focused)
            if action == "down" and grid is not None:
                self._grab_keyboard_focus(grid)
            elif action == "left":
                if i > 0:
                    self._grab_keyboard_focus(buttons[i - 1])
                else:
                    self._home_focus_sidebar()
            elif action == "right" and i + 1 < len(buttons):
                self._grab_keyboard_focus(buttons[i + 1])
            elif action == "back":
                self._exit_reorder_mode()
            elif action == "activate":
                return False        # the button's own click
            return True
        if action == "back":
            self._exit_reorder_mode()
            return True
        # the sidebar: the launcher list sits between its two groups of rows
        if order_list is not None:
            if action == "down" and main_rows and focused is main_rows[-1]:
                order_list.focus_end(last=False)
                return True
            if action == "up" and bottom_rows and focused is bottom_rows[0]:
                order_list.focus_end(last=True)
                return True
        if action == "right" and focused is not None and any(focused is r for r in main_rows + bottom_rows) \
                and grid is not None:
            self._grab_keyboard_focus(grid)
            return True
        return False

    def _home_move_focus(self, focused: Optional[Gtk.Widget], direction: str) -> bool:
        """Arrow keys / d-pad outside the card groups: up and down the
        sidebar, across to the content, and onto the hero's button.
        Returns False when the card-group navigation should handle it."""
        rows = self._home_sidebar_rows()
        row = next((r for r in rows if focused is r or (focused is not None and focused.is_ancestor(r))), None)
        if row is not None:
            i = rows.index(row)
            # A launcher's Open / Close and menu sit between its row and
            # the games: right onto them one by one, then across; left back.
            stops = [row] + getattr(row, "launcher_actions", [])
            at = next((k for k, w in enumerate(stops) if w is focused), 0)
            if direction in ("up", "down"):
                j = i + (-1 if direction == "up" else 1)
                if 0 <= j < len(rows):
                    self._grab_keyboard_focus(rows[j])
            elif direction == "right":
                if at + 1 < len(stops):
                    self._grab_keyboard_focus(stops[at + 1])
                else:
                    self._home_focus_content()
            elif direction == "left" and at > 0:
                self._grab_keyboard_focus(stops[at - 1])
            return True
        hero = getattr(self, "_home_hero_play", None)
        if hero is not None and focused is hero:
            if direction == "down":
                for group in self._nav_groups:
                    if self._focus_first_child_in_group(group):
                        break
            elif direction == "left":
                self._home_focus_sidebar()
            return True
        return False

    def _home_launcher_badge(self, launcher: str, size: int = 16) -> Optional[GdkPixbuf.Pixbuf]:
        key = (launcher, size)
        if key not in self._home_badge_cache:
            pb = None
            try:
                pb = self._load_launcher_icon(launcher, size).get_pixbuf()
            except Exception as e:
                logger.debug(f"No badge icon for {launcher}: {e}")
            self._home_badge_cache[key] = pb
        return self._home_badge_cache[key]

    # --- chrome: top bar ------------------------------------------------

    def _build_home_topbar(self) -> Gtk.Widget:
        drag = Gtk.EventBox()
        drag.get_style_context().add_class("home-topbar")
        drag.connect("button-press-event", self._on_home_topbar_press)
        bar = Gtk.Box(spacing=8)
        bar.set_margin_top(12)
        bar.set_margin_bottom(8)
        bar.set_margin_start(24)
        bar.set_margin_end(12)
        drag.add(bar)

        self._search_debounce_id: Optional[int] = None
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text(self._("filter_placeholder"))
        self.search_entry.get_style_context().add_class("home-search")
        self.search_entry.set_size_request(360, -1)
        self.search_entry.set_icon_sensitive(Gtk.EntryIconPosition.SECONDARY, True)
        self.search_entry.connect(
            "icon-release",
            lambda entry, pos, _ev: entry.set_text("") if pos == Gtk.EntryIconPosition.SECONDARY else None)
        self.search_entry.connect("search-changed", lambda *_: self._on_search_changed())
        self.search_entry.connect("activate", lambda *_: self._launch_first_search_hit())
        self.search_entry.connect("key-press-event", self._on_search_entry_key_press)
        self.search_entry.connect("focus-out-event", self._on_search_entry_focus_out)
        bar.pack_start(self.search_entry, False, False, 0)

        self.minimize_btn = self._make_icon_button("window-minimize-symbolic", self._("window_minimize_tooltip"))
        self.minimize_btn.get_style_context().add_class("window-control-btn")
        self.minimize_btn.connect("clicked", lambda *_: self.iconify())
        self._is_maximized = False
        self.maximize_btn = self._make_icon_button("window-maximize-symbolic", self._("window_maximize_tooltip"))
        self.maximize_btn.get_style_context().add_class("window-control-btn")
        self.maximize_btn.connect("clicked", lambda *_: self._toggle_maximize())
        self.close_btn_header = self._make_icon_button("window-close-symbolic", self._("window_close_tooltip"))
        self.close_btn_header.get_style_context().add_class("window-control-btn")
        self.close_btn_header.get_style_context().add_class("window-close-btn")
        self.close_btn_header.connect("clicked", lambda *_: self._close_to_tray_or_quit())
        # A tiling compositor places and closes windows itself.
        if not is_tiling_window_manager():
            window_controls = Gtk.Box(spacing=2)
            window_controls.get_style_context().add_class("home-winctl")
            window_controls.set_valign(Gtk.Align.CENTER)
            window_controls.set_margin_start(10)
            for btn in (self.minimize_btn, self.maximize_btn, self.close_btn_header):
                window_controls.pack_start(btn, False, False, 0)
            bar.pack_end(window_controls, False, False, 0)

        # View, sort and the library tools share one pill, like the search.
        toolbar = Gtk.Box(spacing=2)
        toolbar.get_style_context().add_class("home-toolbar")
        toolbar.set_valign(Gtk.Align.CENTER)
        bar.pack_end(toolbar, False, False, 0)

        # Report bug says so in words, on a pill of its own beside them
        self.report_bug_btn = Gtk.Button()
        self.report_bug_btn.get_style_context().add_class("home-sort-btn")
        self.report_bug_btn.set_can_focus(False)
        # words only, no icon
        self.report_bug_label = Gtk.Label(label=self._("about_report_bug"))
        self.report_bug_btn.add(self.report_bug_label)
        self.report_bug_btn.connect("clicked", lambda b: self._show_bug_report(b))
        report_pill = Gtk.Box()
        report_pill.get_style_context().add_class("home-toolbar")
        report_pill.set_valign(Gtk.Align.CENTER)
        report_pill.set_margin_end(6)
        report_pill.pack_start(self.report_bug_btn, False, False, 0)
        bar.pack_end(report_pill, False, False, 0)

        # Ko-fi beside it, in the same kind of pill, words only too
        kofi_btn = Gtk.Button()
        kofi_btn.get_style_context().add_class("home-sort-btn")
        kofi_btn.set_can_focus(False)
        kofi_btn.set_tooltip_text(self._("about_support_kofi"))
        kofi_btn.add(Gtk.Label(label="Ko-fi"))
        kofi_btn.connect("clicked", lambda *_: webbrowser.open(KOFI_URL))
        self.kofi_btn = kofi_btn
        kofi_pill = Gtk.Box()
        kofi_pill.get_style_context().add_class("home-toolbar")
        kofi_pill.set_valign(Gtk.Align.CENTER)
        kofi_pill.set_margin_end(6)
        kofi_pill.pack_start(kofi_btn, False, False, 0)
        bar.pack_end(kofi_pill, False, False, 0)

        # icons only - their names show on hover
        self.select_btn = self._make_icon_button("circle-dashed-check-symbolic", self._("selection_mode_btn"))
        self.select_btn.connect("clicked", lambda *_: self._exit_selection_mode() if self.selection_mode
                                else self._enter_selection_mode())
        self.reorder_btn = self._make_icon_button("view-sort-descending-symbolic", self._("reorder_menu"))
        self.reorder_btn.connect("clicked", lambda *_: self._exit_reorder_mode() if self.reorder_mode
                                 else self._cached_launchers and self._enter_reorder_mode())
        self.shortcuts_btn = self._make_icon_button("input-keyboard-symbolic", self._("help_btn_tooltip"))
        self.shortcuts_btn.connect("clicked", lambda *_: self._show_shortcuts_dialog())
        for btn in (self.select_btn, self.reorder_btn, self.shortcuts_btn):
            btn.get_style_context().add_class("view-toggle-btn")


        self.sort_btn = Gtk.MenuButton()
        self.sort_btn.get_style_context().add_class("home-sort-btn")
        self.sort_btn.set_tooltip_text(self._("sort_by"))
        self.sort_btn.set_can_focus(False)
        self.sort_btn.set_valign(Gtk.Align.CENTER)
        sort_box = Gtk.Box(spacing=6)
        self.sort_btn_label = Gtk.Label()
        sort_box.pack_start(self.sort_btn_label, False, False, 0)
        sort_box.pack_start(self._icon_image_at_size("pan-down-symbolic", 16), False, False, 0)
        self.sort_btn.add(sort_box)
        self.sort_popover = Gtk.Popover()
        self.sort_popover.connect("show", lambda *_: self._populate_sort_popover())
        self.sort_popover.connect("show", lambda *_: self._enter_dimmed_dialog())
        self.sort_popover.connect("closed", lambda *_: self._exit_dimmed_dialog())
        self.sort_btn.set_popover(self.sort_popover)
        self._update_sort_btn_label()

        self.view_grid_btn = Gtk.RadioButton()
        self.view_grid_btn.set_mode(False)
        self.view_grid_btn.set_image(self._icon_image("view-grid-symbolic", Gtk.IconSize.BUTTON))
        self.view_grid_btn.set_tooltip_text(self._("view_grid_tooltip"))
        self.view_grid_btn.get_accessible().set_name(self._("view_grid_tooltip"))
        self.view_grid_btn.get_style_context().add_class("view-toggle-btn")
        self.view_list_btn = Gtk.RadioButton.new_from_widget(self.view_grid_btn)
        self.view_list_btn.set_mode(False)
        self.view_list_btn.set_image(self._icon_image("view-list-symbolic", Gtk.IconSize.BUTTON))
        self.view_list_btn.set_tooltip_text(self._("view_list_tooltip"))
        self.view_list_btn.get_accessible().set_name(self._("view_list_tooltip"))
        self.view_list_btn.get_style_context().add_class("view-toggle-btn")
        (self.view_list_btn if self.card_view_mode == "list" else self.view_grid_btn).set_active(True)
        self.view_grid_btn.connect("toggled", self._on_card_view_mode_toggled, "grid")
        self.view_list_btn.connect("toggled", self._on_card_view_mode_toggled, "list")

        def divider() -> Gtk.Separator:
            sep = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
            sep.get_style_context().add_class("home-toolbar-sep")
            return sep
        for widget in (self.view_grid_btn, self.view_list_btn, divider(), self.sort_btn, divider(),
                       self.select_btn, self.reorder_btn, self.shortcuts_btn):
            widget.set_valign(Gtk.Align.CENTER)
            toolbar.pack_start(widget, False, False, 0)

        self.home_topbar = drag
        self._home_topbar = drag
        return drag

    def _on_home_topbar_press(self, _w, event) -> bool:
        if event.button != 1:
            return False
        if event.type == Gdk.EventType._2BUTTON_PRESS:
            self._toggle_maximize()
            return True
        if event.type == Gdk.EventType.BUTTON_PRESS:
            self.begin_move_drag(event.button, int(event.x_root), int(event.y_root), event.time)
            return True
        return False

    def _update_sort_btn_label(self):
        if hasattr(self, "sort_btn_label"):
            self.sort_btn_label.set_text(self._(f"sort_{self.sort_mode}"))

    def _launch_first_search_hit(self):
        if not self.search_entry.get_text().strip():
            return
        games = self._home_filtered_games()
        if games:
            game = games[0]
            self.request_launch(game.launch_cmd, game.title, game.env, game.uid, game.install_dir)

    def _show_shortcuts_dialog(self):
        """Keyboard and controller shortcuts, a page each (the switch on
        top; Left / Right, Tab or LB / RB), in groups, in the card
        dialogs' look. Esc, the controller's B or Close closes it."""
        dlg = Gtk.Dialog(transient_for=self._dialog_transient_parent(), modal=True)
        dlg.set_decorated(False)
        dlg.set_resizable(False)
        visual = dlg.get_screen().get_rgba_visual()
        if visual is not None:
            dlg.set_visual(visual)
        dlg.get_style_context().add_class("home-launch-dialog")
        area = dlg.get_content_area()
        area.set_border_width(0)
        action_area = dlg.get_action_area()
        if action_area is not None:
            action_area.set_no_show_all(True)
            action_area.hide()

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        outer.set_border_width(24)
        header = Gtk.Box(spacing=16)
        badge = Gtk.DrawingArea()
        badge.set_size_request(48, 48)

        def draw_badge(widget, cr):
            rgb = _accent_rgb(widget)
            cr.arc(24, 24, 24, 0, 2 * math.pi)
            cr.set_source_rgba(*rgb, 0.16)
            cr.fill()
            pb = get_icon_pixbuf("input-keyboard-symbolic", 24,
                                 "#%02x%02x%02x" % tuple(round(c * 255) for c in rgb))
            if pb is not None:
                Gdk.cairo_set_source_pixbuf(cr, pb, 12, 12)
                cr.paint()
            return False
        badge.connect("draw", draw_badge)
        header.pack_start(badge, False, False, 0)
        title = Gtk.Label(label=self._("help_btn_tooltip"), xalign=0)
        title.get_style_context().add_class("home-dialog-title")
        header.pack_start(title, True, True, 0)
        close = self._dialog_button(self._("close_btn"), "ghost", "back", "Esc")
        close.set_valign(Gtk.Align.CENTER)
        close.connect("clicked", lambda *_: dlg.response(Gtk.ResponseType.CLOSE))
        header.pack_end(close, False, False, 0)
        outer.pack_start(header, False, False, 0)

        # Scrolls only when the window is too short for it.
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_height(True)
        scroller.set_max_content_height(max(300, self.get_size()[1] - 180))
        scroller.get_style_context().add_class("settings-scroll")
        panels = self._build_shortcuts_panels()
        scroller.add(panels)
        outer.pack_start(scroller, True, True, 0)
        area.pack_start(outer, True, True, 0)
        tabs = panels.shortcuts_tabs
        # the page last looked at (this run)
        tabs[getattr(self, "_shortcuts_page", "keyboard")].set_active(True)

        def switch(page: Optional[str] = None):
            name = page or ("controller" if tabs["keyboard"].get_active() else "keyboard")
            tabs[name].set_active(True)
            self._shortcuts_page = name

        def scroll(delta: float):
            adj = scroller.get_vadjustment()
            adj.set_value(max(adj.get_lower(), min(adj.get_upper() - adj.get_page_size(), adj.get_value() + delta)))

        def nav(action: str):
            """The pad: LB / RB or Left / Right switch the page, Up / Down
            and the stick scroll, B closes."""
            if action == "back":
                dlg.response(Gtk.ResponseType.CLOSE)
            elif action in ("prev_tab", "next_tab"):
                switch()
            elif action in ("left", "right"):
                switch("keyboard" if action == "left" else "controller")
            elif action in ("up", "down"):
                scroll(-60 if action == "up" else 60)

        def on_key(_w, event) -> bool:
            keys = {Gdk.KEY_Escape: "back", Gdk.KEY_Left: "left", Gdk.KEY_Right: "right",
                    Gdk.KEY_Up: "up", Gdk.KEY_Down: "down", Gdk.KEY_Tab: "next_tab", Gdk.KEY_ISO_Left_Tab: "prev_tab"}
            if event.keyval in keys:
                nav(keys[event.keyval])
                return True
            return False
        dlg.connect("key-press-event", on_key)
        dlg.plunderhub_nav = nav
        dlg.plunderhub_scroll = scroll

        dlg.show_all()
        dlg.set_focus(None)
        self._enter_dimmed_dialog()
        try:
            dlg.run()
        finally:
            dlg.destroy()
            self._exit_dimmed_dialog()

    # --- resupply (rescan) feedback ------------------------------------------

    _home_fade_uids: set = set()

    def _diff_library(self, launchers: List[Dict], installed: set) -> Tuple[set, set]:
        """What this scan found and lost compared to the last one (kept in
        settings, so games installed while PlunderHub was closed count
        too). A launcher that wasn't there last time - the first run, or
        one just turned on - brings no NEW tags: all of its games would
        be "new". Found games keep their NEW tag until they're pointed
        at (mouse or keyboard/controller focus)."""
        launcher_of = {g.uid: s["name"] for s in launchers for g in s["games"]}
        names = {s["name"] for s in launchers}
        previous = self.settings.get("home_last_installed")
        previous_launchers = set(self.settings.get("home_last_launchers", []))
        added, removed = set(), set()
        if previous is not None:
            previous = set(previous)
            added = {uid for uid in installed - previous if launcher_of.get(uid) in previous_launchers}
            removed = {uid for uid in previous - installed
                       if self.known_titles.get(uid, {}).get("launcher") in names & previous_launchers}
        new_marks = (self._home_new_marks() | added) & installed
        changed = (previous != installed or previous_launchers != names
                   or new_marks != self._home_new_marks())
        self._home_new_uids = new_marks
        self._home_fade_uids = added
        if changed:
            self.settings["home_last_installed"] = sorted(installed)
            self.settings["home_last_launchers"] = sorted(names)
            self.settings["home_new_games"] = sorted(new_marks)
            save_settings(self.settings)
        return added, removed

    def _home_new_marks(self) -> set:
        if not hasattr(self, "_home_new_uids"):
            self._home_new_uids = set(self.settings.get("home_new_games", []))
        return self._home_new_uids

    def _is_new_game(self, uid: str) -> bool:
        return uid in self._home_new_marks()

    def _clear_new_game(self, uid: str):
        marks = self._home_new_marks()
        if uid not in marks:
            return
        marks.discard(uid)
        self.settings["home_new_games"] = sorted(marks)
        save_settings(self.settings)
        for btn, _check in self.selection_widgets.get(uid, []):
            btn.queue_draw()
        for tag in getattr(self, "_home_new_tags", {}).pop(uid, []):
            tag.hide()

    def _new_game_tag(self, uid: str, console: bool = False) -> Gtk.Label:
        """The NEW pill as a widget (list rows, Console Mode); cleared
        together with the one painted on grid cards."""
        tag = Gtk.Label(label=self._("home_new_badge"))
        tag.get_style_context().add_class("home-new-pill")
        if console:
            tag.get_style_context().add_class("console")
        tag.set_valign(Gtk.Align.CENTER)
        tag.set_no_show_all(True)
        tag.show()
        if not hasattr(self, "_home_new_tags"):
            self._home_new_tags = {}
        self._home_new_tags.setdefault(uid, []).append(tag)
        tag.connect("destroy", lambda t: t in self._home_new_tags.get(uid, []) and self._home_new_tags[uid].remove(t))
        return tag

    def _build_resupply_pill(self) -> Gtk.Widget:
        """A floating pill under the top bar: spinner + "Resupplying…"
        while a rescan runs, then what it found, then it fades out."""
        holder = Gtk.EventBox()
        holder.set_visible_window(True)   # above the scroller's own window
        holder.get_style_context().add_class("home-floating")
        holder.set_halign(Gtk.Align.CENTER)
        holder.set_valign(Gtk.Align.START)
        holder.set_margin_top(6)
        pill = Gtk.Box(spacing=10)
        pill.get_style_context().add_class("home-resupply-pill")
        self._resupply_spinner = Gtk.Spinner()
        self._resupply_spinner.get_style_context().add_class("accent-spinner")
        self._resupply_check = Gtk.Image()
        self._resupply_label = Gtk.Label()
        for widget in (self._resupply_spinner, self._resupply_check, self._resupply_label):
            widget.set_valign(Gtk.Align.CENTER)
            pill.pack_start(widget, False, False, 0)
        holder.add(pill)
        holder.show_all()
        holder.set_no_show_all(True)
        holder.hide()
        self._resupply_pill = holder
        self._resupply_timer: Optional[int] = None
        return holder

    def _show_resupply_pill(self):
        if self._resupply_timer is not None:
            GLib.source_remove(self._resupply_timer)
            self._resupply_timer = None
        self._resupply_label.set_text(self._("home_resupplying"))
        self._resupply_check.hide()
        self._resupply_spinner.show()
        self._resupply_spinner.start()
        self._resupply_pill.set_opacity(1.0)
        self._resupply_pill.show()

    def _finish_resupply_pill(self, added: int, removed: int):
        self._show_resupply_pill()
        self._resupply_spinner.stop()
        self._resupply_spinner.hide()
        self._resupply_check.set_from_pixbuf(get_icon_pixbuf("object-select-symbolic", 18, self._home_accent_hex()))
        self._resupply_check.show()
        parts = []
        if added:
            parts.append(self._("home_resupply_added").format(n=added))
        if removed:
            parts.append(self._("home_resupply_removed").format(n=removed))
        self._resupply_label.set_text("  ·  ".join(parts) or self._("home_resupply_uptodate"))

        def fade() -> bool:
            opacity = self._resupply_pill.get_opacity() - 0.08
            if opacity <= 0:
                self._resupply_pill.hide()
                self._resupply_timer = None
                return False
            self._resupply_pill.set_opacity(opacity)
            return True

        def start_fade() -> bool:
            self._resupply_timer = GLib.timeout_add(30, fade)
            return False
        self._resupply_timer = GLib.timeout_add(2600, start_fade)

    # --- launch confirmation ------------------------------------------------

    def _dialog_key_hint(self, role: str, key: str) -> Gtk.Widget:
        """The key that presses a dialog button - or, with a controller
        in use (pressed last), just its button's symbol, big."""
        if self._controller_family != "keyboard" and self._joystick_manager.is_connected():
            glyph = Gtk.Label(label=self._controller_glyph_text(role))
            glyph.get_style_context().add_class("home-padglyph")
            glyph.set_valign(Gtk.Align.CENTER)
            return glyph
        cap = Gtk.Label(label=key)
        cap.get_style_context().add_class("home-keycap")
        cap.set_valign(Gtk.Align.CENTER)
        return cap

    def _dialog_button(self, text: str, kind: str, role: Optional[str] = None,
                       key: Optional[str] = None) -> Gtk.Button:
        """A pill button for the card dialogs: kind is "accent", "danger"
        or "ghost"."""
        btn = Gtk.Button()
        ctx = btn.get_style_context()
        ctx.add_class("home-play")
        ctx.add_class({"accent": "suggested-action", "danger": "destructive-action"}.get(kind, "home-pill-ghost"))
        inner = Gtk.Box(spacing=10)
        inner.pack_start(Gtk.Label(label=text), False, False, 0)
        if role is not None and key is not None:
            inner.pack_start(self._dialog_key_hint(role, key), False, False, 0)
        btn.add(inner)
        return btn

    def _card_dialog(self, heading: str, text: Optional[str], buttons: List[Tuple[str, Gtk.ResponseType, str]],
                     tone: str = "info", icon_name: Optional[str] = None,
                     default: Optional[Gtk.ResponseType] = None,
                     transient_for: Optional[Gtk.Window] = None,
                     extra: Optional[Gtk.Widget] = None,
                     other_responses: Tuple[Gtk.ResponseType, ...] = ()) -> Gtk.ResponseType:
        """The app's own message and question dialog, in the look of
        "Ready to set sail?": a round badge, a heading, a line or two of
        text and pill buttons. buttons are (label, response, kind), the
        first one the way out (Esc / the controller's B); Enter and A
        press default, else the last one. extra goes under the text;
        other_responses are ones the caller gives the dialog itself."""
        dlg = Gtk.Dialog(transient_for=transient_for or self._dialog_transient_parent(), modal=True)
        dlg.set_decorated(False)
        dlg.set_resizable(False)
        visual = dlg.get_screen().get_rgba_visual()
        if visual is not None:
            dlg.set_visual(visual)
        dlg.get_style_context().add_class("home-launch-dialog")
        area = dlg.get_content_area()
        area.set_border_width(0)
        action_area = dlg.get_action_area()
        if action_area is not None:
            action_area.set_no_show_all(True)
            action_area.hide()

        default_icon, colour = _DIALOG_TONES.get(tone, _DIALOG_TONES["info"])
        icon_name = icon_name or default_icon
        card = Gtk.Box(spacing=20)
        card.set_border_width(24)
        badge = Gtk.DrawingArea()
        badge.set_size_request(56, 56)
        badge.set_valign(Gtk.Align.START)

        def draw_badge(widget, cr):
            rgb = colour or _accent_rgb(widget)
            cr.arc(28, 28, 28, 0, 2 * math.pi)
            cr.set_source_rgba(*rgb, 0.16)
            cr.fill()
            hex_colour = "#%02x%02x%02x" % tuple(round(c * 255) for c in rgb)
            pb = get_icon_pixbuf(icon_name, 28, hex_colour)
            if pb is not None:
                Gdk.cairo_set_source_pixbuf(cr, pb, 14, 14)
                cr.paint()
            return False
        badge.connect("draw", draw_badge)
        card.pack_start(badge, False, False, 0)

        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        title = Gtk.Label(label=heading, xalign=0)
        title.set_line_wrap(True)
        title.set_max_width_chars(34)
        title.get_style_context().add_class("home-dialog-title")
        column.pack_start(title, False, False, 0)
        if text:
            body = Gtk.Label(label=text, xalign=0)
            body.set_line_wrap(True)
            body.set_max_width_chars(46)
            body.set_selectable(False)
            body.get_style_context().add_class("dim-label")
            column.pack_start(body, False, False, 0)
        if extra is not None:
            extra.set_margin_top(8)
            column.pack_start(extra, False, False, 0)

        responses = [r for _label, r, _kind in buttons]
        # no buttons: a card waiting on work, closed by its caller
        back = responses[0] if responses else Gtk.ResponseType.NONE
        go = default if default in responses else (responses[-1] if responses else back)
        row = Gtk.Box(spacing=10)
        row.set_halign(Gtk.Align.END)
        row.set_margin_top(18)
        for label, response, kind in buttons:
            # The way out is always marked Esc, even when Enter also
            # picks it (a "Sink it" question defaults to keeping).
            if response == back and (len(buttons) > 1 or kind == "ghost"):
                btn = self._dialog_button(label, kind, "back", "Esc")
            elif response == go:
                btn = self._dialog_button(label, kind, "activate", "Enter")
            else:
                btn = self._dialog_button(label, kind)
            btn.connect("clicked", lambda _b, r=response: dlg.response(r))
            row.pack_start(btn, False, False, 0)
        if buttons:
            column.pack_start(row, False, False, 0)
        card.pack_start(column, True, True, 0)
        area.pack_start(card, True, True, 0)

        def on_key(_w, event) -> bool:
            if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
                dlg.response(go)
                return True
            if event.keyval == Gdk.KEY_Escape:
                dlg.response(back)
                return True
            return False
        dlg.connect("key-press-event", on_key)

        def on_controller(action: str):
            if action == "activate":
                dlg.response(go)
            elif action == "back":
                dlg.response(back)
        dlg.plunderhub_nav = on_controller

        dlg.show_all()
        dlg.set_focus(None)
        self._enter_dimmed_dialog()
        try:
            response = dlg.run()
        finally:
            dlg.destroy()
            self._exit_dimmed_dialog()
        # Closed some other way (the window manager): the way out.
        return response if response in responses or response in other_responses else back

    def _confirm_launch(self, title: str, uid: Optional[str], launcher: bool = False) -> bool:
        """"Ready to set sail?" with the game's cover - or, for a launcher
        (title its name), its logo. Enter / the controller's A (✕) sets
        sail, Esc / B (○) doesn't - whichever button has focus - and the
        buttons show those keys."""
        dlg = Gtk.Dialog(transient_for=self._dialog_transient_parent(), modal=True)
        dlg.set_decorated(False)
        dlg.set_resizable(False)
        visual = dlg.get_screen().get_rgba_visual()
        if visual is not None:
            dlg.set_visual(visual)
        dlg.get_style_context().add_class("home-launch-dialog")
        area = dlg.get_content_area()
        area.set_border_width(0)
        action_area = dlg.get_action_area()
        if action_area is not None:
            action_area.set_no_show_all(True)
            action_area.hide()

        card = Gtk.Box(spacing=24)
        card.set_border_width(24)
        game = self.game_index.get(uid) if uid else None
        # the game's hero behind it all (else its cover), shaded so the
        # text reads on any picture and in any theme
        art = None
        if game is not None and self.show_covers:
            art = self.hero_path(game.uid) or self._find_cached_artwork(self.covers_dir, game.uid)
        if art is not None:
            dlg.get_style_context().add_class("home-launch-hero")
            card.set_size_request(580, -1)
            art_state = {}
            motion = self._art_animation(art)
            if motion is not None:
                # it moves: played on the dialog itself, so a frame redraws
                # all of it, its padding included
                from plunderhub.ui.animated_hero import HeroAnimation
                art_state["anim"], art_state["motion"] = HeroAnimation(self, dlg), motion
            card.connect("draw", self._draw_launch_dialog_art, art, art_state)
        if game is not None:
            cached = self._find_cached_artwork(self.game_icons_dir, game.uid)
            icon = Gtk.Image.new_from_pixbuf(
                self._resolve_icon_pixbuf(96, self._load_static_icon_source(cached), game.title))
            icon.set_valign(Gtk.Align.START)
            icon.get_style_context().add_class("home-launch-icon")
            card.pack_start(icon, False, False, 0)
        elif launcher:
            # the launcher's logo, as big as a game's icon - no plate under it
            logo = self._load_launcher_icon(title, 96)
            logo.set_valign(Gtk.Align.START)
            logo.get_style_context().add_class("home-launch-icon")
            card.pack_start(logo, False, False, 0)

        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        kicker = Gtk.Label(label=self._("launch_confirm_kicker").upper(), xalign=0)
        kicker.get_style_context().add_class("home-hero-kicker")
        name = Gtk.Label(label=title, xalign=0)
        name.set_line_wrap(True)
        name.set_max_width_chars(26)
        name.get_style_context().add_class("home-launch-title")
        column.pack_start(kicker, False, False, 0)
        column.pack_start(name, False, False, 0)
        if game is not None:
            meta = Gtk.Label(label=f"{game.launcher}  ·  {self._home_playtime_text(game.uid)}", xalign=0)
            meta.get_style_context().add_class("home-launch-meta" if art is not None else "dim-label")
            column.pack_start(meta, False, False, 0)
        elif launcher:
            # as its menu counts them: the hidden ones aren't on board
            count = sum(1 for g in self.game_index.values()
                        if g.launcher == title and not self.scanner.is_hidden(g.launcher, g.uid))
            meta = Gtk.Label(label=self._("games_count").format(count=count), xalign=0)
            meta.get_style_context().add_class("dim-label")
            column.pack_start(meta, False, False, 0)
        column.pack_start(Gtk.Box(), True, True, 0)

        buttons = Gtk.Box(spacing=10)
        buttons.set_halign(Gtk.Align.END)
        buttons.set_margin_top(18)
        cancel = self._dialog_button(self._("dialog_cancel"), "ghost", "back", "Esc")
        go = self._dialog_button(self._("launch_label"), "accent", "activate", "Enter")
        cancel.connect("clicked", lambda *_: dlg.response(Gtk.ResponseType.NO))
        go.connect("clicked", lambda *_: dlg.response(Gtk.ResponseType.YES))
        buttons.pack_start(cancel, False, False, 0)
        buttons.pack_start(go, False, False, 0)
        column.pack_start(buttons, False, False, 0)
        card.pack_start(column, True, True, 0)
        area.pack_start(card, True, True, 0)

        def on_key(_w, event) -> bool:
            if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter):
                dlg.response(Gtk.ResponseType.YES)
                return True
            if event.keyval == Gdk.KEY_Escape:
                dlg.response(Gtk.ResponseType.NO)
                return True
            return False
        dlg.connect("key-press-event", on_key)

        def on_controller(action: str):
            if action == "activate":
                dlg.response(Gtk.ResponseType.YES)
            elif action == "back":
                dlg.response(Gtk.ResponseType.NO)
        dlg.plunderhub_nav = on_controller

        dlg.show_all()
        dlg.set_focus(None)
        self._enter_dimmed_dialog()
        try:
            response = dlg.run()
        finally:
            dlg.destroy()
            self._exit_dimmed_dialog()
        return response == Gtk.ResponseType.YES

    def _draw_launch_dialog_art(self, widget, cr, art: Path, cache: Dict) -> bool:
        """Set sail's picture: covering the whole dialog (the card sits in
        its padding), cut to its rounded corners, under a shade that's
        darkest behind the icon and text and along the bottom, where the
        buttons are."""
        alloc = widget.get_allocation()
        win_w, win_h = widget.get_toplevel().get_size()
        # inside the dialog's 1 px border
        w, h = win_w - 2, win_h - 2
        cr.translate(1 - alloc.x, 1 - alloc.y)
        if cache.get("size") != (w, h):
            cache["size"], cache["pb"] = (w, h), None
            try:
                src = GdkPixbuf.PixbufAnimation.new_from_file(str(art)).get_static_image()
                scale = max(w / src.get_width(), h / src.get_height())
                cache["pb"] = src.scale_simple(max(1, int(src.get_width() * scale) + 1),
                                               max(1, int(src.get_height() * scale) + 1),
                                               GdkPixbuf.InterpType.BILINEAR)
            except Exception as e:
                logger.debug(f"Couldn't load {art} for the launch dialog: {e}")
        pb = cache["pb"]
        cr.save()
        # the dialog's corners (the compositor's, where it rounds windows
        # itself - see _match_compositor_corners), inside its border
        _rounded(cr, 0, 0, w, h, max(0, self.dialog_corner_radius - 1))
        cr.clip()
        anim = cache.get("anim")
        if anim is not None:
            anim.set(cache["motion"], w, h)
        if anim is not None and anim.paint(cr, w, h):
            pass
        elif pb is not None:
            Gdk.cairo_set_source_pixbuf(cr, pb, (w - pb.get_width()) / 2, (h - pb.get_height()) * 0.35)
            cr.paint()
        across = cairo.LinearGradient(0, 0, w, 0)
        across.add_color_stop_rgba(0, 0, 0, 0, 0.84)
        across.add_color_stop_rgba(0.55, 0, 0, 0, 0.64)
        across.add_color_stop_rgba(1, 0, 0, 0, 0.5)
        cr.set_source(across)
        cr.paint()
        down = cairo.LinearGradient(0, h * 0.45, 0, h)
        down.add_color_stop_rgba(0, 0, 0, 0, 0)
        down.add_color_stop_rgba(1, 0, 0, 0, 0.45)
        cr.set_source(down)
        cr.paint()
        cr.restore()
        cr.translate(alloc.x - 1, alloc.y - 1)
        return False

    # --- top bar menus ------------------------------------------------------

    def _home_menu_box(self, popover: Gtk.Popover, heading: Optional[str] = None) -> Gtk.Box:
        """Empties `popover` and gives it the modern menu look."""
        old = popover.get_child()
        if old is not None:
            popover.remove(old)
        popover.get_style_context().add_class("home-menu")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        if heading:
            label = Gtk.Label(label=heading.upper(), xalign=0)
            label.get_style_context().add_class("home-menu-heading")
            box.pack_start(label, False, False, 0)
        popover.add(box)
        return box

    def _home_menu_row(self, icon_name: Optional[str], label: str,
                       checked: Optional[bool] = None, accel: Optional[str] = None) -> Gtk.Button:
        """A full-width menu row: icon, label, and at the end an accent
        check when it's an option that's currently on, or its shortcut."""
        btn = Gtk.Button()
        btn.set_relief(Gtk.ReliefStyle.NONE)
        btn.get_style_context().add_class("home-menu-row")
        row = Gtk.Box(spacing=12)
        if icon_name:
            pb = self._icon_pixbuf(icon_name, 18)
            icon = _pixbuf_area(pb) if pb is not None else self._icon_image_at_size(icon_name, 18)
            icon.set_valign(Gtk.Align.CENTER)
            row.pack_start(icon, False, False, 0)
        text = Gtk.Label(label=label, xalign=0)
        row.pack_start(text, True, True, 0)
        if accel:
            cap = Gtk.Label(label=accel)
            cap.get_style_context().add_class("home-keycap")
            cap.set_valign(Gtk.Align.CENTER)
            row.pack_end(cap, False, False, 0)
        else:
            tick = get_icon_pixbuf("object-select-symbolic", 18, self._home_accent_hex())
            check = _pixbuf_area(tick) if tick is not None else Gtk.Image()
            check.set_valign(Gtk.Align.CENTER)
            check.set_opacity(1.0 if checked else 0.0)
            row.pack_end(check, False, False, 0)
        btn.add(row)
        return btn

    def _home_menu_separator(self) -> Gtk.Separator:
        sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
        sep.get_style_context().add_class("home-menu-sep")
        return sep

    def _home_accent_hex(self) -> str:
        r, g, b = _accent_rgb(self)
        return "#{:02x}{:02x}{:02x}".format(round(r * 255), round(g * 255), round(b * 255))

    # --- data -------------------------------------------------------------

    def _home_all_visible_games(self) -> List[GameItem]:
        games = []
        for section in self._cached_launchers:
            for game in section["games"]:
                if not self.scanner.is_hidden(game.launcher, game.uid):
                    games.append(game)
        return games

    def _home_filtered_games(self) -> List[GameItem]:
        nav = self.home_nav
        filter_text = self.search_entry.get_text().strip().lower()
        games = self._home_all_visible_games()
        if nav == "fav":
            games = [g for g in games if g.uid in self.favorites]
        elif nav == "recent":
            games = [g for g in games if self.recent.get(g.uid)]
        elif nav.startswith("l:"):
            games = [g for g in games if g.launcher == nav[2:]]
        elif nav.startswith("s:"):
            games = [g for g in games if g.launcher == "Heroic" and (g.store_platform or "epic") == nav[2:]]
        if filter_text:
            games = [g for g in games if filter_text in g.title.lower()]
        if nav == "recent" and self.sort_mode not in ("recent", "playtime"):
            return sorted(games, key=lambda g: (-self.recent.get(g.uid, 0), g.title.lower()))
        return self._sort_games(games)

    def _home_nav_title(self) -> str:
        nav = self.home_nav
        if nav == "fav":
            return self._("home_nav_favorites")
        if nav == "recent":
            return self._("home_nav_recent")
        if nav.startswith("l:"):
            return nav[2:]
        if nav.startswith("s:"):
            return "Heroic"     # the store is the lit chip beside it
        return self._("all_games_section")

    def _home_store_chips(self) -> Optional[Gtk.Widget]:
        """On Heroic: All and each store it has games from (two at least),
        with how many - the one shown lit; a click shows just its games."""
        if self.home_nav != "l:Heroic" and not self.home_nav.startswith("s:"):
            return None
        counts: Dict[str, int] = {}
        for g in self._home_all_visible_games():
            if g.launcher == "Heroic":
                store = g.store_platform or "epic"
                counts[store] = counts.get(store, 0) + 1
        stores = [k for k in HEROIC_STORES if counts.get(k)]
        if len(stores) < 2:
            return None
        box = Gtk.Box(spacing=8)
        box.set_valign(Gtk.Align.CENTER)
        box.set_margin_start(6)
        for key, text, n in [("l:Heroic", self._("sw_filter_all"), sum(counts.values()))] + \
                [(f"s:{k}", self._(f"store_{k}"), counts[k]) for k in stores]:
            chip = Gtk.ToggleButton()
            chip.get_style_context().add_class("sw-chip")
            inner = Gtk.Box(spacing=7)
            inner.pack_start(Gtk.Label(label=text), False, False, 0)
            count = Gtk.Label(label=str(n))
            count.get_style_context().add_class("home-chip-count")
            inner.pack_start(count, False, False, 0)
            chip.add(inner)
            chip.set_focus_on_click(False)
            chip.set_active(self.home_nav == key)
            chip.connect("toggled", lambda b, k=key: self._set_home_nav(k) if self.home_nav != k
                         else b.set_active(True))
            box.pack_start(chip, False, False, 0)
        return box

    def _home_playtime_text(self, uid: str) -> str:
        minutes = self.playtime.get(uid) or 0
        if not minutes:
            return self._("home_not_played")
        hours, mins = divmod(minutes, 60)
        return (self._("playtime_hm").format(h=hours, m=mins) if hours
                else self._("playtime_m").format(m=mins))

    def _home_ago_text(self, ts: float) -> str:
        days = max(0, int((time.time() - ts) // 86400))
        if days == 0:
            return self._("home_today")
        if days == 1:
            return self._("home_yesterday")
        return self._("home_days_ago").format(n=days)

    def _busy_badge(self, uid: str, phase: Optional[str]) -> str:
        """What a busy game's button says: launching, syncing its saves,
        or waiting for its Proton."""
        if phase == "launching":
            return self._("game_launching_badge")
        if uid in self._proton_waits:
            from plunderhub.ui.proton_page import S
            return S["wait_badge"]
        return self._("cloud_syncing_badge")

    def _home_phase(self, uid: str) -> Optional[str]:
        if uid in self._cloud_syncs or uid in self._proton_waits:
            return "syncing"
        if uid in self.launching_uids:
            return "launching"
        if self._is_game_running(uid):
            return "running"
        return None

    # --- rendering --------------------------------------------------------

    def _render_launchers(self, animate: bool = False):
        if getattr(self, "_library_dropped", False) and not self.get_visible():
            # in the tray with its cards let go (App._drop_hidden_library):
            # the games are noted, the cards wait till the window's back
            self.game_index = {g.uid: g for section in self._cached_launchers for g in section["games"]}
            return
        self._reveal_epoch += 1
        self._reveal_index = 0
        intro = getattr(self, "_startup_intro", None)
        if intro is not None:
            # the cards it moves are about to go
            intro.finish()
            self._startup_intro = None
        for child in list(self.content.get_children()):
            self.content.remove(child)
        self.game_index = {}
        # The animated covers keep their decoded loops: the same covers
        # coming back carry on, and the ones that don't are dropped by the
        # player once their widgets are gone.
        self.cover_widgets.clear()
        self.game_icon_widgets.clear()
        self.selection_widgets.clear()
        self.running_time_widgets.clear()
        self._nav_groups.clear()
        self._home_hero_play: Optional[Gtk.Button] = None
        self._home_badge_cache.clear()
        self._list_name_sizegroup = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        self._list_time_sizegroup = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        self._update_sort_btn_label()

        launchers = self._cached_launchers
        for section in launchers:
            for game in section["games"]:
                self.game_index[game.uid] = game
        self._refresh_home_sidebar()
        self.info.set_text("")

        if not launchers:
            key = "no_sources_enabled" if not self.enabled_launchers else "no_games_found"
            empty_box = self._build_getting_started_box(self._(key))
            self.content.pack_start(Gtk.Box(), True, True, 0)
            self.content.pack_start(empty_box, True, False, 0)
            self.content.pack_start(Gtk.Box(), True, True, 0)
            self._finish_home_render()
            return

        cover_w, cover_h = self.cover_size, int(self.cover_size * 4 / 3)

        if self.reorder_mode:
            self._render_reorder_view(cover_h)
            self._finish_home_render()
            return

        games = self._home_filtered_games()
        browsing = self.home_nav == "all" and not self.search_entry.get_text().strip()
        if browsing:
            recent = sorted((g for g in self._home_all_visible_games() if self.recent.get(g.uid)),
                            key=lambda g: -self.recent.get(g.uid, 0))
            # the rest of them are in the sidebar's Recently sailed
            if recent:
                self.content.pack_start(self._build_home_hero(recent[0]), False, False, 0)

        heading = self._home_section_heading(self._home_nav_title())
        chips = self._home_store_chips()
        if chips is not None:
            heading.pack_start(chips, False, False, 0)
        if not games:
            self.content.pack_start(heading, False, False, 0)
            self.content.pack_start(self._build_home_empty(), False, False, 30)
        elif self.card_view_mode == "list":
            column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            column.set_halign(Gtk.Align.CENTER)
            column.set_size_request(LIST_COLUMN_WIDTH, -1)
            column.pack_start(heading, False, False, 0)
            rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
            self._home_list_cols = {k: Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
                                    for k in ("title", "launcher", "time", "button")}
            for game in games:
                row = self._build_home_list_row(game)
                rows_box.pack_start(row, False, False, 0)
                self._maybe_reveal(row.plunderhub_row_btn, animate)
            column.pack_start(rows_box, False, False, 0)
            self.content.pack_start(column, False, False, 0)
            self._nav_groups.append({"launcher_widget": None, "container": rows_box, "is_grid": False})
        else:
            self.content.pack_start(heading, False, False, 0)
            flow = self._home_flow()
            for game in games:
                card = GameCard(self, game, cover_w, cover_h)
                flow.add(card)
                self._maybe_reveal(card.banner_btn, animate)
            self.content.pack_start(flow, False, False, 0)
            self._nav_groups.append({"launcher_widget": None, "container": flow, "is_grid": True})
        self._finish_home_render()

    def _render_reorder_view(self, cover_h: int):
        """Edit order: one launcher's covers - or All Plunder's (just the
        favourites on Favorites), in an order of its own - dragged or
        moved with the arrow keys; the sidebar's launchers are dragged
        the same way."""
        from plunderhub.ui.reorder_view import ReorderGrid
        name = self.home_nav[2:] if self.home_nav.startswith("l:") else None
        if name is not None:
            section = next((s for s in self._cached_launchers if s["name"] == name), None)
            if section is None:
                return
            games = sorted((g for g in section["games"] if not self.scanner.is_hidden(g.launcher, g.uid)),
                           key=self._launcher_sort_key)

            def store(order):
                self._store_game_order(name, order)
        else:
            games = self._home_all_visible_games()
            if self.home_nav == "fav":
                games = [g for g in games if g.uid in self.favorites]
            games = sorted(games, key=self._all_sort_key)
            store = self._store_all_order

        banner = Gtk.Box(spacing=16)
        banner.get_style_context().add_class("home-reorder-banner")
        banner.set_margin_top(6)
        banner.pack_start(self._icon_image_at_size("view-sort-descending-symbolic", 26), False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        kicker = Gtk.Label(label=self._("reorder_kicker").upper(), xalign=0)
        kicker.get_style_context().add_class("home-hero-kicker")
        title = Gtk.Label(label=name or self._home_nav_title(), xalign=0)
        title.get_style_context().add_class("home-reorder-title")
        sub = Gtk.Label(label=self._("reorder_sub"), xalign=0)
        sub.set_line_wrap(True)
        sub.get_style_context().add_class("dim-label")
        for widget in (kicker, title, sub):
            texts.pack_start(widget, False, False, 0)
        banner.pack_start(texts, True, True, 0)
        done = Gtk.Button(label=self._("bulk_done_btn"))
        done.get_style_context().add_class("suggested-action")
        done.get_style_context().add_class("home-play")
        done.set_valign(Gtk.Align.CENTER)
        done.connect("clicked", lambda *_: self._exit_reorder_mode())
        banner.pack_end(done, False, False, 0)
        reset = Gtk.Button(label=self._("reorder_reset_alpha"))
        reset.get_style_context().add_class("home-pill-ghost")
        reset.set_valign(Gtk.Align.CENTER)
        banner.pack_end(reset, False, False, 0)
        self.content.pack_start(banner, False, False, 0)

        card_h = cover_h
        card_w = max(1, round(card_h * 2 / 3))
        if not self.show_covers:
            # the library's name plates: a cover's width, about half as tall
            card_w = round(cover_h * 3 / 4)
            card_h = round(card_w * 0.56)
        if self.card_view_mode == "list":
            # the list view's rows, in its column
            from plunderhub.ui.reorder_view import ReorderList
            grid = ReorderList(self, games, LIST_COLUMN_WIDTH + 2 * ReorderList.PAD_X, on_change=store)
            holder = Gtk.Box()
            holder.set_halign(Gtk.Align.CENTER)
            holder.set_size_request(LIST_COLUMN_WIDTH + 2 * ReorderList.PAD_X, -1)
            holder.pack_start(grid, True, True, 0)
        else:
            grid = holder = ReorderGrid(self, games, card_w, card_h, on_change=store)
        # for the keyboard / controller (see _reorder_input)
        self._reorder_grid = grid
        self._reorder_buttons = [reset, done]

        def reset_alpha(_b):
            grid.order.sort(key=lambda g: g.title.lower())
            grid._start_tick()
            store([g.uid for g in grid.order])
        reset.connect("clicked", reset_alpha)
        self.content.pack_start(holder, False, False, 0)
        GLib.idle_add(lambda: (grid.grab_focus(), False)[1])

    def _on_launchers_reordered(self, names: List[str]):
        self.launcher_order = names
        self.settings["launcher_order"] = names
        save_settings(self.settings)

    def _finish_home_render(self):
        self.content.show_all()
        if self.console_mode_active:
            self._populate_console_mode(steal_focus=self.get_visible())
        self._update_cover_hint_bar_visibility()

    def _maybe_reveal(self, widget: Gtk.Widget, animate: bool):
        if animate:
            delay_ms = min(self._reveal_index * REVEAL_STAGGER_MS, REVEAL_STAGGER_MAX_MS)
            self._reveal_card(widget, delay_ms, self._reveal_epoch)
            self._reveal_index += 1

    def _home_flow(self) -> Gtk.FlowBox:
        flow = Gtk.FlowBox()
        flow.set_selection_mode(Gtk.SelectionMode.NONE)
        flow.set_max_children_per_line(30)
        flow.set_min_children_per_line(1)
        flow.set_column_spacing(18)
        flow.set_row_spacing(14)
        flow.set_homogeneous(True)
        flow.set_valign(Gtk.Align.START)
        return flow

    def _home_section_heading(self, title: str) -> Gtk.Box:
        outer = Gtk.Box(spacing=12)
        outer.set_margin_top(14)
        outer.set_margin_bottom(2)
        label = Gtk.Label(label=title, xalign=0)
        label.get_style_context().add_class("home-section")
        outer.pack_start(label, False, False, 0)
        return outer

    def _build_home_hero(self, game: GameItem) -> Gtk.Widget:
        overlay = Gtk.Overlay()
        area = Gtk.DrawingArea()
        area.set_size_request(-1, HERO_HEIGHT)
        area.connect("draw", self._draw_home_hero, game)
        self._home_hero_area = area
        from plunderhub.ui.animated_hero import HeroAnimation
        self._home_hero_anim = HeroAnimation(self, area)
        overlay.add(area)

        phase = self._home_phase(game.uid)
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        text.set_valign(Gtk.Align.CENTER)
        text.set_halign(Gtk.Align.START)
        text.set_margin_start(40)
        text.set_margin_end(40)
        # setting sail is said on the button alone
        kicker = None
        if phase not in ("launching", "syncing"):
            kicker = Gtk.Box(spacing=10)
            if phase == "running":
                kicker.pack_start(self._home_hero_marker(), False, False, 0)
            kicker_label = Gtk.Label(label=self._("home_hero_now_playing" if phase == "running" else "home_hero_continue"
                                                  ).upper(), xalign=0)
            kicker_label.get_style_context().add_class("home-hero-kicker")
            kicker.pack_start(kicker_label, False, False, 0)
        title = Gtk.Label(label=game.title, xalign=0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_max_width_chars(40)
        title.get_style_context().add_class("home-hero-title")
        playtime = self._home_playtime_text(game.uid)
        # the launcher's logo and name, then what's known of the playing
        meta = Gtk.Box(spacing=8)
        meta.get_style_context().add_class("home-hero-meta")
        meta.pack_start(self._home_hero_logo(game.launcher, 20), False, False, 0)
        meta.pack_start(self._home_hero_meta_label(game.launcher), False, False, 0)
        timer_box = None
        if phase == "running":
            meta.pack_start(self._home_hero_meta_label(
                self._("home_played_total").format(time=playtime) if self.playtime.get(game.uid) else playtime,
                "clock"), False, False, 0)
            # the session's clock, big in the accent, and plainly what it is
            timer_box = Gtk.Box(spacing=12)
            timer_box.set_margin_top(4)
            timer = Gtk.Label(xalign=0)
            timer.get_style_context().add_class("home-hero-timer")
            self._apply_tabular_figures(timer)
            caption = Gtk.Label(label=self._("home_hero_session"), xalign=0)
            caption.set_valign(Gtk.Align.CENTER)
            caption.get_style_context().add_class("home-hero-timer-caption")
            timer_box.pack_start(timer, False, False, 0)
            timer_box.pack_start(caption, False, False, 0)

            def update_timer() -> bool:
                if timer.get_parent() is None or not self._is_game_running(game.uid):
                    return False
                started = self.running_since.get(game.uid)
                timer.set_text(self._format_elapsed_time(time.monotonic() - started)
                               if started is not None else "00:00")
                return True
            update_timer()
            GLib.timeout_add(1000, update_timer)
        else:
            meta.pack_start(self._home_hero_meta_label(
                self._("home_played").format(time=playtime) if self.playtime.get(game.uid) else playtime, "clock"),
                False, False, 0)
            last = self.recent.get(game.uid)
            if last:
                meta.pack_start(self._home_hero_meta_label(
                    self._("home_last_played").format(when=self._home_ago_text(last)), "calendar"), False, False, 0)
        play = self._home_play_button(game)
        play.set_halign(Gtk.Align.START)
        play.set_margin_top(10)
        self._home_hero_play = play
        play.plunderhub_uid = game.uid      # its game, for the keyboard (App._focused_game)
        # Reaching the hero with the keyboard or a controller brings the
        # whole page back to the top, not just the button into view.
        play.connect("focus-in-event", lambda *_: (GLib.idle_add(self._home_scroll_to_top), False)[1])
        status = None
        if phase in ("launching", "syncing"):
            # what Steam is up to, so a long launch says why
            status = Gtk.Label(xalign=0)
            status.set_no_show_all(True)
            status.get_style_context().add_class("home-hero-status")

            def update_status() -> bool:
                if status.get_parent() is None or self._home_phase(game.uid) not in ("launching", "syncing"):
                    return False
                line = self.launch_status(game.uid)
                status.set_text(line or "")
                status.set_visible(bool(line))
                return True
            update_status()
            GLib.timeout_add(500, update_status)
        for widget in (kicker, title, meta, timer_box, play, status):
            if widget is not None:
                text.pack_start(widget, False, False, 0)
        overlay.add_overlay(text)
        # right-click: the game's menu, as on its card
        box = Gtk.EventBox()
        box.set_visible_window(False)
        box.set_margin_top(4)
        box.add(overlay)
        box.connect("button-press-event", self._on_home_hero_press, game, box)
        # The picture has a window of its own, which takes no clicks unless
        # asked to - without this only Set sail's right-click got here.
        area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        area.connect("button-press-event", self._on_home_hero_press, game, box)
        return box

    def _home_hero_marker(self) -> Gtk.DrawingArea:
        """Before a running game's kicker: a dot in a halo."""
        area = _windowless_area()
        area.set_size_request(18, 18)
        area.set_valign(Gtk.Align.CENTER)

        def draw(widget, cr):
            accent = _accent_rgb(widget)
            for radius, alpha in ((9, 0.18), (6.5, 0.3), (4.5, 1)):
                cr.arc(9, 9, radius, 0, 2 * math.pi)
                cr.set_source_rgba(*accent, alpha)
                cr.fill()
            return False
        area.connect("draw", draw)
        return area

    def _home_hero_logo(self, launcher: str, px: int) -> Gtk.Widget:
        """The launcher's logo, bare and sharp at any display scale."""
        scale = max(1, self.get_scale_factor())
        pb = self._load_launcher_icon(launcher, px * scale).get_pixbuf()
        area = _windowless_area()
        area.set_size_request(px, px)
        area.set_valign(Gtk.Align.CENTER)
        if pb is not None:
            surface = Gdk.cairo_surface_create_from_pixbuf(pb, scale, None)

            def draw(_widget, cr):
                cr.set_source_surface(surface, (px - pb.get_width() / scale) / 2, (px - pb.get_height() / scale) / 2)
                cr.paint()
                return False
            area.connect("draw", draw)
        return area

    def _home_hero_meta_label(self, text: str, glyph: Optional[str] = None) -> Gtk.Widget:
        """One item of the hero's line under the title, with its icon."""
        box = Gtk.Box(spacing=7)
        if glyph is not None:
            box.set_margin_start(14)
            box.pack_start(_hero_glyph_area(glyph, 15, (1, 1, 1, 0.7)), False, False, 0)
        box.pack_start(Gtk.Label(label=text, xalign=0), False, False, 0)
        return box

    def _on_home_hero_press(self, _widget, event, game: GameItem, box: Gtk.Widget) -> bool:
        if event.button != 3 or event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        from plunderhub.ui.game_page import GameMenu
        menu = GameMenu(self, game, box)
        # where it was clicked, whichever part of the hero got the click
        x, y = box.get_pointer()
        rect = Gdk.Rectangle()
        rect.x, rect.y, rect.width, rect.height = x, y, 1, 1
        menu.set_pointing_to(rect)
        menu.popup()
        return True

    def _draw_home_hero(self, widget, cr, game: GameItem):
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        # Picture and shade made whole first, then cut to the rounded
        # corners in one go: clipped one by one, the corners' half-covered
        # edge pixels got the picture more than the shade, a light rim
        # round the dark left corners of a bright hero.
        cr.push_group()
        self._home_hero_widget = widget
        pb, moving = self._home_hero_pixbuf(game.uid, w, h) if self.show_covers else (None, None)
        anim = getattr(self, "_home_hero_anim", None)
        playing = False
        if anim is not None and anim.widget is widget:
            anim.set(moving, w, h)
            playing = anim.paint(cr, w, h)
        if not playing and pb is not None:
            # made a surface once: from the pixbuf, the whole picture was
            # converted again for every bubble passing the hero's edge
            surface = getattr(self, "_home_hero_surface", None)
            if surface is None or surface[0] is not pb:
                from plunderhub.ui.animated_hero import argb_surface
                surface = self._home_hero_surface = (pb, argb_surface(pb))
            pw, ph = pb.get_width(), pb.get_height()
            # one made for another size (a resize, its own still being
            # scaled off the GTK thread): stretched to cover meanwhile
            s = max(w / pw, h / ph)
            cr.save()
            if abs(s - 1) > 0.01:
                cr.scale(s, s)
            cr.set_source_surface(surface[1], (w / s - pw) / 2, (h / s - ph) * 0.3)
            cr.paint()
            cr.restore()
        elif not playing:
            # the same colours as the game's no-cover card in the library
            _title_gradient(cr, game.title, w, h)
        fade = cairo.LinearGradient(0, 0, w, 0)
        fade.add_color_stop_rgba(0, 0, 0, 0, 0.92)
        fade.add_color_stop_rgba(0.55, 0, 0, 0, 0.55)
        fade.add_color_stop_rgba(1, 0, 0, 0, 0.1)
        cr.set_source(fade)
        cr.paint()
        accent = _accent_rgb(widget)
        live = self._home_phase(game.uid) == "running"
        _paint_hero_light(cr, w, h, accent, 0.4 if live else 0.26)
        cr.pop_group_to_source()
        _rounded(cr, 0, 0, w, h, 18)
        cr.fill()
        return False

    def _home_hero_pixbuf(self, uid: str, w: int, h: int) -> Tuple[Optional[GdkPixbuf.Pixbuf], Optional[Path]]:
        """The game's hero banner - or, without one, its cover - scaled to
        cover the hero's width, and the file it moves from if it's
        animated (see animated_hero). Cached per size: a playing hero
        draws every frame, and new art clears the cache
        (update_hero_image, update_cover_image)."""
        cache = getattr(self, "_home_hero_cache", None)
        if cache is None:
            cache = self._home_hero_cache = {}
        key = (uid, w, h)
        if key in cache:
            self._home_hero_last = (uid,) + cache[key]
            return cache[key]
        # Decoded and scaled on a worker (a 3840 px hero at every width of
        # a resize stalled the GTK thread); meanwhile the last one for this
        # game, stretched by the draw.
        if getattr(self, "_home_hero_wanted", None) != key:
            self._home_hero_wanted = key
            cached = self.hero_path(uid) or self._find_cached_artwork(self.covers_dir, uid)
            threading.Thread(target=self._home_hero_job, args=(key, cached), daemon=True,
                             name="plunderhub-home-hero").start()
        last = getattr(self, "_home_hero_last", None)
        if last is not None and last[0] == uid:
            return last[1], last[2]
        return None, None

    def _home_hero_job(self, key, cached: Optional[Path]):
        uid, w, h = key
        pb, moving = None, None
        if cached is not None and w > 0 and h > 0:
            try:
                src = GdkPixbuf.PixbufAnimation.new_from_file(str(cached)).get_static_image()
                scale = max(w / src.get_width(), h / src.get_height())
                pb = src.scale_simple(max(1, int(src.get_width() * scale) + 1),
                                      max(1, int(src.get_height() * scale) + 1),
                                      GdkPixbuf.InterpType.BILINEAR)
            except Exception as e:
                logger.debug(f"Couldn't load hero art for {uid}: {e}")
            try:
                moving = self._art_animation(cached)
            except Exception as e:
                logger.debug(f"Couldn't check hero animation for {uid}: {e}")
        GLib.idle_add(self._home_hero_done, key, pb, moving)

    def _home_hero_done(self, key, pb, moving) -> bool:
        if getattr(self, "_home_hero_wanted", None) != key:
            return False        # a newer size or game came meanwhile
        self._home_hero_wanted = None
        cache = self._home_hero_cache
        cache.clear()
        cache[key] = (pb, moving)
        self._home_hero_last = (key[0], pb, moving)
        widget = getattr(self, "_home_hero_widget", None)
        if widget is not None and widget.get_window() is not None:
            widget.queue_draw()
        return False

    def _home_scroll_to_top(self) -> bool:
        adjustment = self.scroller.get_vadjustment()
        if adjustment.get_value() > adjustment.get_lower():
            self._on_scroll_top_clicked(None)
        return False

    def _home_play_button(self, game: GameItem) -> Gtk.Button:
        phase = self._home_phase(game.uid)
        btn = Gtk.Button()
        ctx = btn.get_style_context()
        ctx.add_class("home-play")
        if phase == "running":
            inner = Gtk.Box(spacing=8)
            inner.pack_start(self._icon_image_at_size("media-playback-stop-symbolic", 16), False, False, 0)
            inner.pack_start(Gtk.Label(label=self._("stop_game_tooltip")), False, False, 0)
            btn.add(inner)
            ctx.add_class("destructive-action")
            btn.connect("clicked", lambda b: self._on_stop_game_clicked(b, game))
        elif phase in ("launching", "syncing"):
            # Busy, not disabled: a greyed-out button was unreadable over
            # the hero. Nothing happens on a click until the game is up.
            inner = Gtk.Box(spacing=10)
            spinner = Gtk.Spinner()
            spinner.start()
            inner.pack_start(spinner, False, False, 0)
            inner.pack_start(Gtk.Label(label=self._busy_badge(game.uid, phase)), False, False, 0)
            btn.add(inner)
            ctx.add_class("home-play-busy")
        else:
            inner = Gtk.Box(spacing=9)
            inner.pack_start(_hero_glyph_area("play", 13, lambda w: (*_on_accent_rgb(_accent_rgb(w)), 1)),
                             False, False, 0)
            inner.pack_start(Gtk.Label(label=self._("launch_label")), False, False, 0)
            btn.add(inner)
            ctx.add_class("suggested-action")
            # its glow from a blur made once (see _draw_play_glow)
            ctx.add_class("home-play-glow")
            btn.connect("draw", self._draw_play_glow)
            btn.connect("clicked", lambda b: self.request_launch(
                game.launch_cmd, game.title, game.env, game.uid, game.install_dir))
        return btn

    @staticmethod
    def _draw_play_glow(widget, cr) -> bool:
        """Set sail's glow - the suggested-action box-shadow, 0 2px 10px of
        the accent at 35% - under the button, from a blur made once. GTK
        blurred it again on every repaint, and over an animated hero or a
        list of them that was a fifth of the app's CPU. Focused, the CSS
        draws its own shadow as before."""
        if widget.has_visible_focus():
            return False
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        if w > 0 and h > 0:
            shadow, pad = _soft_shadow(w, h, h / 2, ((10, 2, (*_accent_rgb(widget), 0.35)),))
            cr.set_source_surface(shadow, -pad, -pad)
            cr.paint()
        return False

    def _build_home_list_row(self, game: GameItem) -> Gtk.Box:
        row = Gtk.Box(spacing=12)
        row.get_style_context().add_class("home-list-row")
        row.plunderhub_uid = game.uid
        # before the hero's: everything after it is drawn lifted
        row.connect("draw", self._draw_list_row_lift)
        hero = self.hero_path(game.uid) if self.show_covers else None
        if hero is not None:
            # the game's hero behind the row, shaded so the text reads
            row.get_style_context().add_class("home-list-row-hero")
            row.connect("draw", self._draw_list_row_hero, game.uid)
            motion = self._art_animation(hero) if self.animated_covers else None
            if motion is not None:
                # moving, as on the grid's cards and the Continue playing
                # card - streamed (a row's loop is small, a list's many are
                # not), and only while the row's in view
                from plunderhub.ui.animated_hero import HeroAnimation
                row.plunderhub_hero_motion = motion
                row.plunderhub_hero_anim = HeroAnimation(self, row, visible=lambda: self._widget_on_screen(row),
                                                         stream=True)

        row_btn = Gtk.Button()
        row_btn.set_relief(Gtk.ReliefStyle.NONE)
        row_btn.get_style_context().add_class("game-cover")
        row_btn.get_style_context().add_class("home-list-row-btn")
        if game.uid in self.selected_uids:
            row_btn.get_style_context().add_class("game-cover-selected")
        hbox = Gtk.Box(spacing=18)
        check_icon = None
        if self.selection_mode:
            check_icon = self._icon_image("object-select-symbolic", Gtk.IconSize.BUTTON)
            check_icon.set_opacity(1.0 if game.uid in self.selected_uids else 0.35)
            hbox.pack_start(check_icon, False, False, 0)
        icon = self._create_game_icon_image(game)
        icon.set_valign(Gtk.Align.CENTER)
        hbox.pack_start(icon, False, False, 0)

        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        texts.set_valign(Gtk.Align.CENTER)
        text = ("★ " if game.uid in self.favorites else "") + game.title
        title = Gtk.Label(label=text, xalign=0)
        # the whole title, onto another line where the row's too narrow
        title.set_line_wrap(True)
        title.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        title.set_max_width_chars(len(text))
        title.get_style_context().add_class("settings-row-title")
        title_row = Gtk.Box(spacing=10)
        # Every title as wide as the widest, so the NEW tags line up.
        self._home_list_cols["title"].add_widget(title)
        title_row.pack_start(title, False, True, 0)
        if self._is_new_game(game.uid):
            title_row.pack_start(self._new_game_tag(game.uid), False, False, 0)
        texts.pack_start(title_row, False, False, 0)
        phase = self._home_phase(game.uid)
        if phase is not None:
            status = Gtk.Label(label="● " + ((self.launch_status(game.uid) or "") if phase == "syncing" else
                                             self._("game_launching_badge" if phase == "launching"
                                                    else "game_running_badge")), xalign=0)
            status.get_style_context().add_class("settings-status-on")
            texts.pack_start(status, False, False, 0)
        hbox.pack_start(texts, True, True, 0)

        def column(key: str, text: str, icon_widget: Optional[Gtk.Widget]) -> Gtk.Box:
            cell = Gtk.Box(spacing=6)
            cell.set_valign(Gtk.Align.CENTER)
            if icon_widget is not None:
                cell.pack_start(icon_widget, False, False, 0)
            lbl = Gtk.Label(label=text, xalign=0)
            lbl.get_style_context().add_class("settings-row-sub")
            cell.pack_start(lbl, False, False, 0)
            self._home_list_cols[key].add_widget(cell)
            return cell
        # The same badge as on the grid's cards, at the same size.
        badge = self._home_launcher_badge(game.launcher, 32)
        badge_image = (Gtk.Image.new_from_pixbuf(badge) if badge is not None
                       else self._load_launcher_icon(game.launcher, 32))
        hbox.pack_start(column("launcher", game.launcher, badge_image), False, False, 0)
        if self.show_playtime:
            if row.get_style_context().has_class("home-list-row-hero"):
                # light over the shaded hero, whatever the theme's text is
                from plunderhub.ui.game_page import _icon
                clock = _icon("clock-symbolic", 14, "#ffffff")
            else:
                clock = self._icon_image_at_size("clock-symbolic", 14)
            hbox.pack_start(column("time", self._home_playtime_text(game.uid), clock), False, False, 0)
        row_btn.add(hbox)
        row_btn.connect("clicked", lambda b: self._on_game_card_clicked(b, game))
        row_btn.connect("button-press-event", self._show_game_context_menu, game)
        row_btn.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK)
        row_btn.connect("enter-notify-event", lambda *_: (self._clear_new_game(game.uid), False)[1])
        row_btn.connect("focus-in-event", lambda *_: (self._clear_new_game(game.uid), False)[1])
        self._grab_focus_on_hover(row_btn)
        self.selection_widgets.setdefault(game.uid, []).append((row_btn, check_icon))
        row.pack_start(row_btn, True, True, 0)

        play = self._home_play_button(game)
        play.set_valign(Gtk.Align.CENTER)
        play.set_can_focus(False)
        self._home_list_cols["button"].add_widget(play)
        row.pack_end(play, False, False, 0)
        row.plunderhub_row_btn = row_btn
        self._lift_list_row_on_hover(row, row_btn, play)
        return row

    # how far a list row rises (.home-list-row's top margin in theme.py)
    LIST_LIFT_PX = 5

    def _lift_list_row_on_hover(self, row: Gtk.Box, row_btn: Gtk.Button, play: Gtk.Widget):
        """The row rises like a grid card while it's pointed at (its own
        button or its Set sail) or stood on, the hero behind it eased in
        closer - drawn (_draw_list_row_lift, _draw_list_row_hero), on the
        common beat, with no relayout: moved by its margins, it re-laid the
        whole window out on every frame."""
        state = {"hover": False, "anim": None, "watch": None, "from": 0.0, "t0": None}
        row.plunderhub_lift = 0.0
        row.plunderhub_target = 0.0
        row.plunderhub_focused = False

        def tick(_widget, frame_clock):
            if state["t0"] is not None and frame_clock.get_frame_counter() % self._bubble_beat_frames(frame_clock):
                return True
            now = frame_clock.get_frame_time()
            if state["t0"] is None:
                state["t0"] = now
            progress = min(1.0, (now - state["t0"]) / 1_000_000 / ROW_LIFT_SECONDS)
            target = row.plunderhub_target
            row.plunderhub_lift = state["from"] + (target - state["from"]) * _ROW_EASE(progress)
            self._queue_list_row_lift(row)
            if progress >= 1:
                state["anim"] = None
                return False
            return True

        def update():
            focused = row_btn.has_focus() and not row_btn.get_style_context().has_class("game-cover-focus-suppressed")
            target = 1.0 if (state["hover"] or focused) else 0.0
            if focused != row.plunderhub_focused:
                row.plunderhub_focused = focused
                self._queue_list_row_lift(row)
            if target != row.plunderhub_target:
                row.plunderhub_target = target
                state["from"], state["t0"] = row.plunderhub_lift, None
                if state["anim"] is None:
                    state["anim"] = row.add_tick_callback(tick)
            return False

        def near() -> bool:
            # over the row, or where it rises to and from - lifting moves
            # it up from under the pointer, which would otherwise drop it
            # again at once and set it bobbing
            if row.get_window() is None:
                return False
            x, y = row.get_pointer()
            alloc = row.get_allocation()
            return 0 <= x < alloc.width and -self.LIST_LIFT_PX <= y < alloc.height + self.LIST_LIFT_PX

        def watch():
            if near():
                return True
            state["watch"] = None
            state["hover"] = False
            update()
            return False

        def entered(*_):
            if not state["hover"]:
                state["hover"] = True
                update()
            if state["watch"] is None:
                state["watch"] = GLib.timeout_add(40, watch)
            return False
        for widget in (row_btn, play):
            widget.add_events(Gdk.EventMask.ENTER_NOTIFY_MASK)
            widget.connect("enter-notify-event", entered)
        row_btn.connect("focus-in-event", lambda *_: GLib.idle_add(update) and False)
        row_btn.connect("focus-out-event", lambda *_: GLib.idle_add(update) and False)

        def stop(*_):
            if state["anim"] is not None:
                row.remove_tick_callback(state["anim"])
                state["anim"] = None
            if state["watch"] is not None:
                GLib.source_remove(state["watch"])
                state["watch"] = None
        row.connect("destroy", stop)

    def _queue_list_row_lift(self, row: Gtk.Box):
        """The row and all its shadow can reach (see GameCard._queue_lift_area)."""
        row.queue_draw()
        ctx = row.get_style_context()
        margin = ctx.get_margin(ctx.get_state())
        w = row.get_allocated_width() - margin.left - margin.right
        h = row.get_allocated_height() - margin.top - margin.bottom
        if w > 0 and h > 0:
            pad = _row_shadow(w, h, _accent_rgb(row), True)[1]
            _queue_area(row, margin.left - pad, margin.top - pad - self.LIST_LIFT_PX,
                        w + 2 * pad, h + 2 * pad + self.LIST_LIFT_PX)

    def _draw_list_row_lift(self, widget, cr) -> bool:
        """Under everything of the row: its shadow and, stood on, the
        accent ring round it - then the rest drawn up to LIST_LIFT_PX higher,
        into its top margin."""
        intro = getattr(widget, "plunderhub_intro", None)
        if intro:
            # rising into place at startup (StartupIntro)
            cr.translate(*intro)
        lift = getattr(widget, "plunderhub_lift", 0.0)
        focused = getattr(widget, "plunderhub_focused", False)
        if lift <= 0.005 and not focused:
            return False
        ctx = widget.get_style_context()
        margin = ctx.get_margin(ctx.get_state())
        w = widget.get_allocated_width() - margin.left - margin.right
        h = widget.get_allocated_height() - margin.top - margin.bottom
        up = self.LIST_LIFT_PX * lift
        accent = _accent_rgb(widget)
        if w > 0 and h > 0:
            shadow, pad = _row_shadow(w, h, accent, focused)
            cr.set_source_surface(shadow, margin.left - pad, margin.top - pad - up)
            alpha = 1.0 if focused else min(1.0, lift)
            if alpha >= 1:
                cr.paint()
            else:
                cr.paint_with_alpha(alpha)
            if focused:
                cr.set_source_rgba(*accent, 1)
                cr.set_line_width(2)
                _rounded(cr, margin.left - 1, margin.top - 1 - up, w + 2, h + 2, 15)
                cr.stroke()
        cr.translate(0, -up)
        return False

    def _draw_list_row_hero(self, widget, cr, uid: str) -> bool:
        """Under the row's own drawing: its hero (dark until it's loaded)
        and a shade - strongest behind the title, lighter in the middle,
        a little stronger again behind the launcher and playtime."""
        # In the row's box, not its allocation (its CSS margins round it);
        # drawn lifted already, see _draw_list_row_lift.
        ctx = widget.get_style_context()
        margin = ctx.get_margin(ctx.get_state())
        w = widget.get_allocated_width() - margin.left - margin.right
        full_h = widget.get_allocated_height()
        h = full_h - margin.top - margin.bottom
        # the picture keeps one size whatever the margins are mid-lift
        pb = self._list_hero_pixbuf(uid, w, full_h - self.LIST_LIFT_PX, widget)
        # lifted (see _lift_list_row_on_hover): the picture eases in a
        # little closer and its shade lightens
        lift = getattr(widget, "plunderhub_lift", 0.0)
        window = widget.get_window()
        anim = getattr(widget, "plunderhub_hero_anim", None)
        if anim is not None:
            anim.set(widget.plunderhub_hero_motion, w, h)
            if anim.frame() is None:
                anim = None                   # nothing decoded yet: the still
        if anim is not None or lift > 0 or pb is None or window is None or w <= 0 or h <= 0:
            cr.save()
            cr.translate(margin.left, margin.top)
            self._paint_list_row_hero(cr, uid, pb, w, h, lift, anim)
            cr.restore()
            return False
        # At rest, from an image made once: a lifting row's shadow reaches
        # its neighbours, and they were all drawn again from the picture,
        # frame by frame.
        key = (w, h, pb)
        cached = getattr(widget, "_plunderhub_rest", None)
        if cached is None or cached[0] != key:
            surface = image_surface(window, cairo.FORMAT_ARGB32, w, h)
            self._paint_list_row_hero(cairo.Context(surface), uid, pb, w, h, 0.0)
            cached = widget._plunderhub_rest = (key, surface)
        cr.set_source_surface(cached[1], margin.left, margin.top)
        cr.paint()
        return False

    def _paint_list_row_hero(self, cr, uid: str, pb: Optional[GdkPixbuf.Pixbuf], w: int, h: int, lift: float,
                             anim=None):
        """_draw_list_row_hero's picture - the animation's frame due, with
        anim - and shade, in a w x h box at 0, 0."""
        cr.save()
        _rounded(cr, 0, 0, w, h, 14)
        cr.clip()
        if anim is not None or pb is not None:
            cr.save()
            # the lift's ease-in, round the middle
            zoom = 1 + 0.05 * lift
            cr.translate(w / 2, h / 2)
            cr.scale(zoom, zoom)
            cr.translate(-w / 2, -h / 2)
            if (anim is None or not anim.paint(cr, w, h)) and pb is not None:
                # covering the box - what's cached may be a pixel or so off;
                # cut 0.3 down, as the animation's frames are (animated_hero._fill)
                fit = max(w / pb.get_width(), h / pb.get_height(), 1.0)
                pw, ph = pb.get_width() * fit, pb.get_height() * fit
                cr.translate((w - pw) / 2, (h - ph) * 0.3)
                cr.scale(fit, fit)
                cr.set_source_surface(self._list_hero_surface(uid, pb), 0, 0)
                cr.paint()
            cr.restore()
        else:
            cr.set_source_rgba(0.05, 0.07, 0.08, 1)
            cr.paint()
        ease = 0.14 * lift
        fade = cairo.LinearGradient(0, 0, w, 0)
        fade.add_color_stop_rgba(0, 0, 0, 0, 0.86 - ease / 2)
        fade.add_color_stop_rgba(0.42, 0, 0, 0, 0.58 - ease)
        fade.add_color_stop_rgba(0.7, 0, 0, 0, 0.5 - ease)
        fade.add_color_stop_rgba(1, 0, 0, 0, 0.72 - ease / 2)
        cr.set_source(fade)
        cr.paint()
        cr.restore()

    def _list_hero_pixbuf(self, uid: str, w: int, h: int, widget: Gtk.Widget) -> Optional[GdkPixbuf.Pixbuf]:
        """uid's hero cut for a w x h row - None until it's first loaded,
        off the GTK thread one at a time (a big PNG takes a while to
        decode, and a library can have hundreds of rows). Kept per game,
        not per size: mid-lift the row's height wavers by a pixel, and
        the drawing scales what's here to fit. Only a real resize loads
        it again - the old one shown meanwhile."""
        cache = getattr(self, "_list_hero_cache", None)
        if cache is None:
            cache = self._list_hero_cache = {}
            self._list_hero_pending = set()
            self._list_hero_jobs = queue.Queue()
            threading.Thread(target=self._list_hero_worker, args=(self._list_hero_jobs,),
                             name="plunderhub-list-heroes", daemon=True).start()
        have = cache.get(uid)       # (w, h, pixbuf)
        if have is not None and abs(have[0] - w) <= 24 and abs(have[1] - h) <= 12:
            return have[2]
        if w > 1 and h > 1 and uid not in self._list_hero_pending:
            path = self.hero_path(uid)
            if path is not None:
                self._list_hero_pending.add(uid)
                self._list_hero_jobs.put((uid, w, h, path, widget))
        return have[2] if have is not None else None

    def _list_hero_surface(self, uid: str, pb: GdkPixbuf.Pixbuf) -> cairo.ImageSurface:
        """The row's picture made a surface once - converted from the
        pixbuf on every repaint, each frame of a lift cost a full copy."""
        surfaces = getattr(self, "_list_hero_surfaces", None)
        if surfaces is None:
            surfaces = self._list_hero_surfaces = {}
        have = surfaces.get(uid)
        if have is None or have[0] is not pb:
            # 32-bit: it's drawn scaled, and RGB24 scales the slow way (argb_surface)
            from plunderhub.ui.animated_hero import argb_surface
            have = surfaces[uid] = (pb, argb_surface(pb))
        return have[1]

    def _list_hero_worker(self, jobs):
        while True:
            uid, w, h, path, widget = jobs.get()
            pb = None
            try:
                src = GdkPixbuf.PixbufAnimation.new_from_file(str(path)).get_static_image()
                scale = max(w / src.get_width(), h / src.get_height())
                pb = src.scale_simple(max(1, int(src.get_width() * scale) + 1),
                                      max(1, int(src.get_height() * scale) + 1), GdkPixbuf.InterpType.BILINEAR)
            except Exception as e:
                logger.debug(f"Couldn't load the hero for a list row ({path}): {e}")
            GLib.idle_add(self._list_hero_loaded, uid, w, h, pb, widget)

    def _list_hero_loaded(self, uid, w, h, pb, widget) -> bool:
        self._list_hero_pending.discard(uid)
        if pb is not None or uid not in self._list_hero_cache:
            self._list_hero_cache[uid] = (w, h, pb)
        if widget.get_window() is not None:
            widget.queue_draw()
        return False

    def _build_home_empty(self) -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_halign(Gtk.Align.CENTER)
        box.pack_start(self._icon_image_at_size("skull-symbolic", 72), False, False, 0)
        title = Gtk.Label(label=self._("no_games"))
        title.get_style_context().add_class("home-empty-title")
        box.pack_start(title, False, False, 0)
        # The search hint only when there's a search to change
        if self.search_entry.get_text().strip():
            key = "home_empty_sub"
        elif self.home_nav == "fav":
            key = "home_empty_fav_sub"
        else:
            return box
        sub = Gtk.Label(label=self._(key))
        sub.get_style_context().add_class("dim-label")
        box.pack_start(sub, False, False, 0)
        return box

    def _refresh_home_language(self):
        self._refresh_update_card_text()
        beta = getattr(self, "_home_beta_label", None)
        if beta is not None:
            beta.set_text(self._("beta_badge").upper())
        for label, key in self._home_nav_text_labels.values():
            label.set_text(self._(key))
        self._update_sort_btn_label()
        self._home_sidebar_signature = None
