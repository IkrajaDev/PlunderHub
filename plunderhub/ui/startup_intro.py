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

"""The main window building itself up at startup: the loading scrim fades
away while the sidebar's rows slide in one by one, the top bar settles and
the library rises into place in a wave from the top left.

About a second and a half, stepped on the frame clock at the app's common
beat. Only what is on screen moves, and nothing is laid out again: each
piece gets an opacity and a translated draw (its own "draw" handler, or
the attribute plunderhub_intro read by a draw handler that must run first,
as the list rows' _draw_list_row_lift). Nothing is left running after."""

import math
from typing import List, Optional

import cairo

from plunderhub.gi_setup import Gdk, GdkPixbuf, GLib, Gtk
from plunderhub.logging_setup import logger
from plunderhub.theme import _app_icon_file

UNIT_US = 450_000           # one piece fading in and moving into place
SCRIM_US = 380_000          # the loading scrim fading away
SIDE_STAGGER_US = 35_000    # sidebar rows, one after the other
TOPBAR_AT_US = 120_000
CONTENT_AT_US = 160_000     # the library's wave starts...
WAVE_US = 650_000           # ...and reaches the bottom right this much later
SIDE_DX = -18
TOPBAR_DY = -10
RISE_DY = 24


def _ease_out(k: float) -> float:
    return 1 - (1 - k) ** 3


class LoadingMark(Gtk.DrawingArea):
    """The loading screen's mark: the app's logo breathing in a pulsing
    accent glow, a comet circling it. When the library is ready it swells
    a little as the scrim fades (leave, 0..1, set by StartupIntro).
    Repaints only itself, on the app's common beat, and only between
    start() and stop() - App._sync_startup_spinner stops it under a dialog
    or with the window in the background."""

    LOGO_PX = 112
    SIZE = 240
    TURN_US = 1_300_000      # one turn of the comet
    BREATH_US = 2_400_000    # one breath of the logo and its glow
    FADE_IN_US = 350_000

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.set_size_request(self.SIZE, self.SIZE)
        self.leave = 0.0
        self._t0: Optional[int] = None
        self._now = 0
        self._tick_id: Optional[int] = None
        self._logo = None
        self.connect("draw", self._draw)

    def start(self):
        if self._tick_id is None:
            self._tick_id = self.add_tick_callback(self._tick)

    def stop(self):
        if self._tick_id is not None:
            self.remove_tick_callback(self._tick_id)
            self._tick_id = None

    def _tick(self, _widget, frame_clock) -> bool:
        now = frame_clock.get_frame_time()
        if self._t0 is None:
            self._t0 = now
        elif frame_clock.get_frame_counter() % self.app._bubble_beat_frames(frame_clock):
            return True
        self._now = now - self._t0
        self.queue_draw()
        return True

    def _logo_pixbuf(self, scale: int):
        if self._logo is None or self._logo[0] != scale:
            pb = None
            icon_file = _app_icon_file()
            if icon_file is not None:
                try:
                    pb = GdkPixbuf.Pixbuf.new_from_file_at_size(str(icon_file), self.LOGO_PX * scale,
                                                                self.LOGO_PX * scale)
                except GLib.Error as e:
                    logger.debug(f"Couldn't load the app icon: {e}")
            self._logo = (scale, pb)
        return self._logo[1]

    def _draw(self, widget, cr) -> bool:
        from plunderhub.ui.home_mixin import _accent_rgb
        t = self._now
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        cx, cy = w / 2, h / 2
        accent = _accent_rgb(widget)
        appear = min(1.0, t / self.FADE_IN_US)
        breath = 0.5 - 0.5 * math.cos(2 * math.pi * t / self.BREATH_US)
        leave = _ease_out(self.leave)
        grow = 1 + 0.035 * breath + 0.18 * leave
        cr.push_group()

        # the glow, breathing with the logo
        reach = min(w, h) / 2 - 1
        halo = cairo.RadialGradient(cx, cy, self.LOGO_PX * 0.2, cx, cy, reach)
        halo.add_color_stop_rgba(0, *accent, 0.26 + 0.14 * breath)
        halo.add_color_stop_rgba(0.55, *accent, 0.07 + 0.05 * breath)
        halo.add_color_stop_rgba(1, *accent, 0)
        cr.set_source(halo)
        cr.paint()

        # the orbit, and a comet going round it with a fading tail
        radius = self.LOGO_PX * 0.5 + 26
        cr.set_line_width(2)
        cr.set_source_rgba(*accent, 0.14 * (1 - leave))
        cr.arc(cx, cy, radius, 0, 2 * math.pi)
        cr.stroke()
        head = 2 * math.pi * t / self.TURN_US - math.pi / 2
        tail, pieces = math.radians(120), 24
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        for i in range(pieces):
            k = (i + 1) / pieces
            a0 = head - tail * (1 - (i / pieces))
            a1 = head - tail * (1 - k)
            cr.set_source_rgba(*accent, 0.9 * k ** 1.6 * (1 - leave))
            cr.set_line_width(1.2 + 2.6 * k)
            cr.arc(cx, cy, radius, a0, a1)
            cr.stroke()
        cr.set_source_rgba(*accent, 1 - leave)
        cr.arc(cx + radius * math.cos(head), cy + radius * math.sin(head), 3.2, 0, 2 * math.pi)
        cr.fill()

        scale = widget.get_scale_factor()
        pb = self._logo_pixbuf(scale)
        if pb is not None:
            cr.save()
            cr.translate(cx, cy)
            cr.scale(grow / scale, grow / scale)
            Gdk.cairo_set_source_pixbuf(cr, pb, -pb.get_width() / 2, -pb.get_height() / 2)
            cr.paint()
            cr.restore()
        cr.pop_group_to_source()
        cr.paint_with_alpha(appear)
        return False


class _Piece:
    __slots__ = ("widget", "delay", "dx", "dy", "handler", "done")

    def __init__(self, widget: Gtk.Widget, delay: int, dx: float, dy: float, handler: Optional[int]):
        self.widget, self.delay, self.dx, self.dy = widget, delay, dx, dy
        self.handler, self.done = handler, False


class StartupIntro:
    def __init__(self, app, scrim: Optional[Gtk.Widget]):
        self.app = app
        self.scrim = scrim
        self.pieces: List[_Piece] = []
        self.start: Optional[int] = None
        self.tick_id: Optional[int] = None

    # --- what moves --------------------------------------------------------

    def add(self, widget: Gtk.Widget, delay_us: int, dx: float = 0, dy: float = 0,
            own_draw: bool = True):
        """own_draw=False: the widget's first draw handler reads
        plunderhub_intro itself (it has handlers that must run before)."""
        handler = None
        if (dx or dy) and own_draw:
            handler = widget.connect("draw", self._draw)
        widget.plunderhub_intro = (dx, dy)
        widget.set_opacity(0)
        self.pieces.append(_Piece(widget, delay_us, dx, dy, handler))

    @staticmethod
    def _draw(widget, cr) -> bool:
        dx, dy = getattr(widget, "plunderhub_intro", (0, 0))
        cr.translate(dx, dy)
        return False

    def collect(self):
        app = self.app
        sidebar = getattr(app, "home_sidebar", None)
        if sidebar is not None:
            # its panel fades in under the rows sliding in
            self.add(sidebar, 0)
        side = []
        if getattr(app, "home_brand_btn", None) is not None:
            side.append(app.home_brand_btn)
        for box in (getattr(app, "home_nav_main", None), getattr(app, "home_nav_head", None),
                    getattr(app, "home_nav_launchers", None), getattr(app, "home_nav_bottom", None)):
            if box is None or not box.get_visible():
                continue
            if isinstance(box, Gtk.ListBox):
                side.extend(row for row in box.get_children() if row.get_visible())
            else:
                side.append(box)
        for i, widget in enumerate(side):
            self.add(widget, i * SIDE_STAGGER_US, dx=SIDE_DX)
        topbar = getattr(app, "_home_topbar", None)
        if topbar is not None:
            self.add(topbar, TOPBAR_AT_US, dy=TOPBAR_DY)

        content = app.content
        alloc = content.get_allocation()
        units: List[Gtk.Widget] = []
        self._units(content, units)
        view_w = max(1, alloc.width)
        scroller = content.get_ancestor(Gtk.ScrolledWindow)
        view_h = max(1, scroller.get_allocated_height() if scroller is not None else alloc.height)
        for widget in units:
            if not app._widget_on_screen(widget):
                continue
            coords = widget.translate_coordinates(content, 0, 0)
            if coords is None:
                continue
            x, y = coords
            # a wave from the top left: down counts more than across
            where = 0.65 * min(1.0, max(0.0, y / view_h)) + 0.35 * min(1.0, max(0.0, x / view_w))
            delay = CONTENT_AT_US + int(WAVE_US * where)
            list_row = "home-list-row" in widget.get_style_context().list_classes()
            self.add(widget, delay, dy=RISE_DY, own_draw=not list_row)

    def _units(self, widget: Gtk.Widget, out: List[Gtk.Widget]):
        """The pieces of the library that come in on their own: cards, list
        rows, and whatever else is there (headings, the hero) as a whole."""
        from plunderhub.ui.home_mixin import GameCard
        if not widget.get_visible():
            return
        if isinstance(widget, GameCard) or "home-list-row" in widget.get_style_context().list_classes():
            out.append(widget)
            return
        if isinstance(widget, Gtk.FlowBoxChild):
            child = widget.get_child()
            if child is not None:
                self._units(child, out)
            return
        if isinstance(widget, Gtk.Container) and self._holds_cards(widget):
            for child in widget.get_children():
                self._units(child, out)
            return
        out.append(widget)

    def _holds_cards(self, widget: Gtk.Widget) -> bool:
        from plunderhub.ui.home_mixin import GameCard
        found = []

        def look(w):
            if found:
                return
            if isinstance(w, GameCard) or "home-list-row" in w.get_style_context().list_classes():
                found.append(w)
            elif isinstance(w, Gtk.Container):
                w.forall(look)
        if isinstance(widget, Gtk.Container):
            widget.forall(look)
        return bool(found)

    # --- running ------------------------------------------------------------

    def run(self):
        if not self.pieces and self.scrim is None:
            return
        self.tick_id = self.app.add_tick_callback(self._tick)

    def _tick(self, _widget, frame_clock) -> bool:
        now = frame_clock.get_frame_time()
        if self.start is None:
            self.start = now
        elif frame_clock.get_frame_counter() % self.app._bubble_beat_frames(frame_clock):
            return True
        t = now - self.start
        busy = False
        if self.scrim is not None:
            k = min(1.0, t / SCRIM_US)
            if k >= 1.0:
                self.scrim.destroy()
                self.scrim = None
            else:
                self.scrim.set_opacity(1 - k)
                mark = getattr(self.app, "startup_spinner", None)
                if isinstance(mark, LoadingMark):
                    mark.leave = k
                busy = True
        for piece in self.pieces:
            if piece.done:
                continue
            k = max(0.0, min(1.0, (t - piece.delay) / UNIT_US))
            if k >= 1.0:
                self._settle(piece)
                continue
            busy = True
            if k <= 0.0:
                continue
            eased = _ease_out(k)
            piece.widget.plunderhub_intro = (piece.dx * (1 - eased), piece.dy * (1 - eased))
            piece.widget.set_opacity(1 - (1 - k) ** 2)
            self._redraw(piece)
        if not busy:
            self.tick_id = None
        return busy

    def _settle(self, piece: _Piece):
        piece.done = True
        widget = piece.widget
        widget.plunderhub_intro = (0, 0)
        widget.set_opacity(1.0)
        if piece.handler is not None:
            widget.disconnect(piece.handler)
        self._redraw(piece)

    @staticmethod
    def _redraw(piece: _Piece):
        """The piece's whole way, from where it started to where it ends:
        its own queue_draw() is cut to its clip and left a ghost of a
        card's bottom below it."""
        from plunderhub.ui.home_mixin import _queue_area
        widget = piece.widget
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        # a card's shadow and lift reach a little beyond it too
        pad = 16
        _queue_area(widget, min(0, piece.dx) - pad, min(0, piece.dy) - pad,
                    w + abs(piece.dx) + 2 * pad, h + abs(piece.dy) + 2 * pad)

    def finish(self):
        """Everything in place at once (the window hid, the library was
        rebuilt meanwhile)."""
        if self.tick_id is not None:
            self.app.remove_tick_callback(self.tick_id)
            self.tick_id = None
        for piece in self.pieces:
            if not piece.done:
                self._settle(piece)
        if self.scrim is not None:
            self.scrim.destroy()
            self.scrim = None
