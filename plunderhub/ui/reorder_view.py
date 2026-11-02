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

"""Edit order mode: a launcher's covers laid out by hand so they can be
dragged and slide out of each other's way, and the sidebar's launcher
list, reordered the same way."""

import colorsys
import math
from typing import Callable, Dict, List, Optional

import cairo

from plunderhub.core.models import GameItem
from plunderhub.gi_setup import Gdk, GdkPixbuf, GLib, Gtk, logical_size
from plunderhub.logging_setup import logger
from plunderhub.ui.home_mixin import (
    _accent_rgb, _on_accent_rgb, _paint_name_plate, _pango_text, _rounded, _title_hue,
)

GAP_X, GAP_Y = 22, 24
PAD_TOP = 18
DRAG_THRESHOLD = 4
SLIDE = 0.22
AUTOSCROLL_EDGE = 48     # px from the visible top/bottom where dragging scrolls
AUTOSCROLL_MAX = 18      # px per tick at the very edge


def _set_cursor(widget: Gtk.Widget, name: Optional[str]):
    window = widget.get_window()
    if window is not None:
        window.set_cursor(Gdk.Cursor.new_from_name(widget.get_display(), name) if name else None)


class ReorderGrid(Gtk.DrawingArea):
    """Covers in their custom order. Drag one and the others slide out of
    the way; or click one and move it with the arrow keys. With only a
    keyboard or a controller (see handle): a cursor goes over the covers,
    Enter / A picks one up, the arrows move it, Enter drops it, Esc / B
    puts it back. on_change gets the new uid order after every drop or
    move."""

    def __init__(self, app, games: List[GameItem], card_w: int, card_h: int,
                 on_change: Callable[[List[str]], None]):
        super().__init__()
        self.app = app
        self.order = list(games)
        self.cw, self.ch = card_w, card_h
        self.on_change = on_change
        self.pos: Dict[str, List[float]] = {}
        self.hover_uid: Optional[str] = None
        # the picked-up cover (a click picks one up too), and the order it
        # was picked up from - Esc / B puts it back there
        self.selected_uid: Optional[str] = None
        self._held_from: Optional[List[GameItem]] = None
        # where the keyboard / controller is, nothing picked up
        self.cursor_uid: Optional[str] = games[0].uid if games else None
        self.drag: Optional[Dict] = None
        self._covers: Dict[str, Optional[GdkPixbuf.Pixbuf]] = {}
        self._tick_id: Optional[int] = None
        self.set_can_focus(True)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.connect("draw", self._draw)
        self.connect("button-press-event", self._press)
        self.connect("button-release-event", self._release)
        self.connect("motion-notify-event", self._motion)
        self.connect("leave-notify-event", self._leave)
        self.connect("focus-in-event", lambda *_: self.queue_draw())
        self.connect("focus-out-event", self._focus_out)
        self._height_idle: Optional[int] = None
        self.set_size_request(-1, PAD_TOP + card_h + self.GAP_Y)
        self.connect("size-allocate", lambda *_: self._relayout())
        self.connect("destroy", lambda *_: self._stop_tick())

    # --- layout -----------------------------------------------------------

    # the gaps between covers, and room at the sides (the list's rows
    # need some for their lift)
    GAP_X, GAP_Y, PAD_X = GAP_X, GAP_Y, 0

    def _cols(self) -> int:
        w = self.get_allocated_width()
        return max(1, int((w + self.GAP_X) // (self.cw + self.GAP_X)))

    def _slot_xy(self, i: int):
        c = self._cols()
        return self.PAD_X + (i % c) * (self.cw + self.GAP_X), PAD_TOP + (i // c) * (self.ch + self.GAP_Y)

    def _index_of(self, uid: str) -> int:
        return next(i for i, g in enumerate(self.order) if g.uid == uid)

    def _relayout(self):
        rows = max(1, math.ceil(len(self.order) / self._cols()))
        height = PAD_TOP + rows * (self.ch + self.GAP_Y) + 12
        if self.get_size_request()[1] != height and self._height_idle is None:
            # Not from inside size-allocate - GTK drops resizes queued there.
            def apply():
                self._height_idle = None
                self.set_size_request(-1, height)
                return False
            self._height_idle = GLib.idle_add(apply)
        for i, g in enumerate(self.order):
            if g.uid not in self.pos or not self._tick_id:
                self.pos[g.uid] = list(self._slot_xy(i))
        self.queue_draw()

    def _slot_at(self, x: float, y: float) -> int:
        c = self._cols()
        col = max(0, min(c - 1, int((x - self.PAD_X + self.GAP_X / 2) // (self.cw + self.GAP_X))))
        row = max(0, int((y - PAD_TOP + self.GAP_Y / 2) // (self.ch + self.GAP_Y)))
        return max(0, min(len(self.order) - 1, row * c + col))

    def _card_at(self, x: float, y: float) -> Optional[GameItem]:
        for i, g in enumerate(self.order):
            sx, sy = self._slot_xy(i)
            if sx <= x <= sx + self.cw and sy <= y <= sy + self.ch:
                return g
        return None

    # --- animation ---------------------------------------------------------

    def _start_tick(self):
        if self._tick_id is None:
            self._tick_id = GLib.timeout_add(16, self._tick)

    def _stop_tick(self):
        if self._tick_id is not None:
            GLib.source_remove(self._tick_id)
            self._tick_id = None

    def _tick(self) -> bool:
        busy = False
        if self.drag and self.drag["moved"]:
            self._autoscroll()
        for i, g in enumerate(self.order):
            if self.drag and self.drag["moved"] and self.drag["uid"] == g.uid:
                continue
            tx, ty = self._slot_xy(i)
            p = self.pos.setdefault(g.uid, [tx, ty])
            if abs(p[0] - tx) > 0.5 or abs(p[1] - ty) > 0.5:
                p[0] += (tx - p[0]) * SLIDE
                p[1] += (ty - p[1]) * SLIDE
                busy = True
            else:
                p[0], p[1] = tx, ty
        self.queue_draw()
        if busy or self.drag:
            return True
        self._tick_id = None
        return False

    # --- input -------------------------------------------------------------

    def _leave(self, _w, _e):
        if self.hover_uid is not None and not self.drag:
            self.hover_uid = None
            self.queue_draw()
        return False

    def _press(self, _w, e) -> bool:
        self.grab_focus()
        if e.button != 1 or e.type != Gdk.EventType.BUTTON_PRESS:
            return True
        g = self._card_at(e.x, e.y)
        if g is None:
            self.selected_uid = None
            self.queue_draw()
            return True
        was_held = self.selected_uid == g.uid
        if self.selected_uid is not None and not was_held:
            self._drop()                # another one picked up is set down first
        p = self.pos.get(g.uid) or list(self._slot_xy(self._index_of(g.uid)))
        self.drag = {"uid": g.uid, "dx": e.x - p[0], "dy": e.y - p[1], "x0": e.x, "y0": e.y,
                     "moved": False, "start": self._index_of(g.uid), "was_held": was_held}
        self.cursor_uid = g.uid
        _set_cursor(self, "grabbing")
        self.queue_draw()
        return True

    def _motion(self, _w, e) -> bool:
        d = self.drag
        if d is None:
            g = self._card_at(e.x, e.y)
            uid = g.uid if g else None
            if uid != self.hover_uid:
                self.hover_uid = uid
                self.queue_draw()
            _set_cursor(self, "grab" if g else None)
            return True
        if not d["moved"] and abs(e.x - d["x0"]) + abs(e.y - d["y0"]) < DRAG_THRESHOLD:
            return True
        d["moved"] = True
        self._drag_to(e.x, e.y)
        self._start_tick()
        return True

    def _scroller(self) -> Optional[Gtk.ScrolledWindow]:
        w = self.get_parent()
        while w is not None and not isinstance(w, Gtk.ScrolledWindow):
            w = w.get_parent()
        return w

    def _visible_rows(self):
        """The part of the grid the scrolled window shows, top and bottom in
        the grid's own coordinates."""
        h = self.get_allocated_height()
        sw = self._scroller()
        if sw is None:
            return 0, h
        coords = self.translate_coordinates(sw, 0, 0)
        if coords is None:
            return 0, h
        top = max(0, -coords[1])
        return top, min(h, top + sw.get_allocated_height())

    def _drag_to(self, px: float, py: float):
        """Moves the lifted card under the pointer - kept inside the grid and
        the visible part of it, so its tilt and shadow are never cut off."""
        d = self.drag
        d["px"], d["py"] = px, py
        m = self._lift_margin()
        top, bottom = self._visible_rows()
        w = self.get_allocated_width()
        x = max(m, min(w - self.cw - m, px - d["dx"]))
        y = max(top + m, min(bottom - self.ch - m - 10, py - d["dy"]))
        self.pos[d["uid"]] = [x, y]
        target = self._slot_at(x + self.cw / 2, y + self.ch / 2)
        cur = self._index_of(d["uid"])
        if target != cur:
            self.order.insert(target, self.order.pop(cur))

    def _lift_margin(self) -> int:
        """How far a lifted card's tilt, scale, ring and shadow reach past it."""
        return math.ceil(max(self.cw, self.ch) * 0.04) + 4

    def _autoscroll(self):
        """Scrolls while a dragged card is held near the visible top/bottom."""
        d, sw = self.drag, self._scroller()
        if sw is None or "py" not in d:
            return
        top, bottom = self._visible_rows()
        py = d["py"]
        if py < top + AUTOSCROLL_EDGE:
            speed = -(top + AUTOSCROLL_EDGE - py) / AUTOSCROLL_EDGE
        elif py > bottom - AUTOSCROLL_EDGE:
            speed = (py - bottom + AUTOSCROLL_EDGE) / AUTOSCROLL_EDGE
        else:
            return
        adj = sw.get_vadjustment()
        old = adj.get_value()
        adj.set_value(max(adj.get_lower(), min(adj.get_upper() - adj.get_page_size(),
                                               old + max(-1, min(1, speed)) * AUTOSCROLL_MAX)))
        moved = adj.get_value() - old
        if moved:
            # the pointer stays put on screen, so over the grid it moved too
            self._drag_to(d["px"], py + moved)

    def _release(self, _w, _e) -> bool:
        d = self.drag
        if d is None:
            return True
        self.drag = None
        _set_cursor(self, "grab")
        self._start_tick()
        if d["moved"]:
            # dropped where it was let go
            self._drop()
            if self._index_of(d["uid"]) != d["start"]:
                self._changed()
        elif d["was_held"]:
            self._drop()                # a second click sets it down
        else:
            # a click picks it up, for the arrow keys
            self.selected_uid = d["uid"]
            self._held_from = list(self.order)
        self.queue_draw()
        return True

    def handle(self, action: str) -> Optional[str]:
        """A key or controller action: up / down / left / right, activate
        (Enter, A), back (Esc, B), home / end. None when it's dealt with
        here - else the way focus should go on from the grid's edge, or
        "back" for leaving Edit order."""
        if not self.order:
            return None if action == "activate" else action
        held = self.selected_uid
        if action == "activate":
            if held is None:
                self.selected_uid = self.cursor_uid
                self._held_from = list(self.order)
            else:
                self._drop()
            self.queue_draw()
            return None
        if action == "back":
            if held is None:
                return "back"
            # put it back where it was picked up from
            if self._held_from is not None and [g.uid for g in self._held_from] != [g.uid for g in self.order]:
                self.order = self._held_from
                self._start_tick()
                self._changed()
            self._drop()
            self._scroll_to(self._index_of(held))
            self.queue_draw()
            return None
        c, last = self._cols(), len(self.order) - 1
        uid = held or self.cursor_uid
        cur = self._index_of(uid) if uid in {g.uid for g in self.order} else 0
        if action in ("home", "end"):
            new = 0 if action == "home" else last
        else:
            step = {"left": -1, "right": 1, "up": -c, "down": c}.get(action)
            if step is None:
                return None
            new = cur + step
            # off the edge: a held cover stays, the cursor moves on out
            off = (new < 0 or new > last or (action == "left" and cur % c == 0)
                   or (action == "right" and (cur % c == c - 1 or cur == last)))
            if off and action == "down" and cur // c < last // c:
                new, off = last, False      # the shorter last row
            if off:
                return None if held else action
        if held is not None:
            if new != cur:
                self.order.insert(new, self.order.pop(cur))
                self._start_tick()
                self._changed()
        else:
            self.cursor_uid = self.order[new].uid
        self._scroll_to(new)
        self.queue_draw()
        return None

    def _drop(self):
        if self.selected_uid is not None:
            self.cursor_uid = self.selected_uid
        self.selected_uid = None
        self._held_from = None

    def _focus_out(self, *_):
        # a picked-up cover is set down where it is
        self._drop()
        self.queue_draw()
        return False

    def _scroll_to(self, index: int):
        """Keeps the cover at index in view."""
        sw = self._scroller()
        coords = self.translate_coordinates(sw, 0, 0) if sw is not None else None
        if coords is None:
            return
        adj = sw.get_vadjustment()
        _x, y = self._slot_xy(index)
        top = adj.get_value() + coords[1] + y - 12
        bottom = top + self.ch + 36
        if top < adj.get_value():
            adj.set_value(max(adj.get_lower(), top))
        elif bottom > adj.get_value() + adj.get_page_size():
            adj.set_value(min(adj.get_upper() - adj.get_page_size(), bottom - adj.get_page_size()))

    def _changed(self):
        self.on_change([g.uid for g in self.order])

    # --- drawing -----------------------------------------------------------

    def _cover(self, game: GameItem) -> Optional[GdkPixbuf.Pixbuf]:
        if game.uid not in self._covers:
            pb = None
            path = self.app._find_cached_artwork(self.app.covers_dir, game.uid) if self.app.show_covers else None
            if path is not None:
                try:
                    src = GdkPixbuf.PixbufAnimation.new_from_file(str(path)).get_static_image()
                    s = max(self.cw / src.get_width(), self.ch / src.get_height())
                    sw, sh = int(src.get_width() * s) + 1, int(src.get_height() * s) + 1
                    pb = src.scale_simple(sw, sh, GdkPixbuf.InterpType.BILINEAR).new_subpixbuf(
                        (sw - self.cw) // 2, (sh - self.ch) // 2, self.cw, self.ch)
                except Exception as e:
                    logger.debug(f"Couldn't load cover for edit order ({game.uid}): {e}")
            self._covers[game.uid] = pb
        return self._covers[game.uid]

    def _draw(self, widget, cr):
        accent = _accent_rgb(widget)
        dragging = self.drag["uid"] if self.drag and self.drag["moved"] else None
        # a cover picked up with the keyboard / controller rises the same way
        held = self.selected_uid if not dragging and widget.has_focus() else None
        if dragging:
            sx, sy = self._slot_xy(self._index_of(dragging))
            cr.save()
            _rounded(cr, sx + 1, sy + 1, self.cw - 2, self.ch - 2, 10)
            cr.set_source_rgba(*accent, 0.08)
            cr.fill_preserve()
            cr.set_source_rgba(*accent, 0.7)
            cr.set_line_width(2)
            cr.set_dash([7, 5])
            cr.stroke()
            cr.restore()
        # only the cards in view (the scrolled window clips to it): the rest
        # would load every cover of a big library and lay out its texts,
        # every frame of a drag
        x1, y1, x2, y2 = cr.clip_extents()
        pad = 24          # shadow and hover lift reach past a card's edge
        for i, g in enumerate(self.order):
            if g.uid in (dragging, held):
                continue
            x, y = self.pos.get(g.uid) or self._slot_xy(i)
            if x + self.cw + pad < x1 or x - pad > x2 or y + self.ch + pad < y1 or y - pad > y2:
                continue
            self._draw_card(cr, widget, g, i, accent)
        for uid in (dragging, held):
            if uid:
                i = self._index_of(uid)
                self._draw_card(cr, widget, self.order[i], i, accent, lifted=True)
        return False

    def _draw_card(self, cr, widget, g: GameItem, index: int, accent, lifted: bool = False):
        cw, ch = self.cw, self.ch
        x, y = self.pos.get(g.uid) or self._slot_xy(index)
        if lifted:
            # kept inside the grid, so the tilt and shadow aren't cut off at
            # its edges (the first column sits right against the sidebar)
            m = self._lift_margin()
            x = max(m, min(widget.get_allocated_width() - cw - m, x))
            y = max(m, min(widget.get_allocated_height() - ch - m - 10, y))
        selected = g.uid == self.selected_uid and widget.has_focus()
        cursor = (g.uid == self.cursor_uid and self.selected_uid is None and widget.has_focus()
                  and not self.drag)
        hover = (g.uid == self.hover_uid and not self.drag) or cursor
        cr.save()
        cr.translate(x, y - (4 if hover else 0))
        if lifted:
            cr.translate(cw / 2, ch / 2)
            cr.rotate(-0.03)
            cr.scale(1.05, 1.05)
            cr.translate(-cw / 2, -ch / 2)
        for k, a in ((10, 0.10), (6, 0.16), (3, 0.22)):
            _rounded(cr, -k / 2 + 2, k / 2 + (8 if lifted else 3), cw + k - 4, ch, 12)
            cr.set_source_rgba(0, 0, 0, a if (lifted or hover) else a * 0.5)
            cr.fill()

        plate = not self.app.show_covers
        _rounded(cr, 0, 0, cw, ch, 10)
        cr.save()
        cr.clip()
        pb = self._cover(g)
        if plate:
            # covers switched off: the library's name plate
            _paint_name_plate(cr, widget, g.title, None, cw, ch)
        elif pb is not None:
            Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
            cr.paint()
        else:
            hue = _title_hue(g.title)
            grad = cairo.LinearGradient(0, 0, cw, ch)
            grad.add_color_stop_rgb(0, *colorsys.hsv_to_rgb(hue, 0.55, 0.55))
            grad.add_color_stop_rgb(1, *colorsys.hsv_to_rgb((hue + 0.12) % 1, 0.7, 0.22))
            cr.set_source(grad)
            cr.paint()
        if not plate:
            fade = cairo.LinearGradient(0, ch * 0.6, 0, ch)
            fade.add_color_stop_rgba(0, 0, 0, 0, 0)
            fade.add_color_stop_rgba(1, 0, 0, 0, 0.85)
            cr.set_source(fade)
            cr.paint()
            _pango_text(cr, widget, g.title, 12, ch - 30, cw - 24, 10.5, True, (1, 1, 1, 0.97))
        cr.restore()

        # position badge
        active = selected or lifted or cursor
        bw = 30 if index < 9 else 38
        _rounded(cr, 8, 8, bw, 24, 12)
        if active:
            cr.set_source_rgba(*accent, 0.95)
        else:
            cr.set_source_rgba(0, 0, 0, 0.62)
        cr.fill()
        _pango_text(cr, widget, str(index + 1), 8, 11, bw, 9.5, True,
                    (*_on_accent_rgb(accent), 1) if active else (1, 1, 1, 0.95), align_center=True)

        # grip handle
        cr.set_source_rgba(0, 0, 0, 0.55)
        _rounded(cr, cw - 34, 8, 26, 26, 8)
        cr.fill()
        cr.set_source_rgba(1, 1, 1, 0.9)
        for dx in (0, 6):
            for dy in (0, 5.5, 11):
                cr.arc(cw - 24 + dx, 15.5 + dy, 1.5, 0, 2 * math.pi)
                cr.fill()

        if active:
            # inside the card's edge: the first column sits at the grid's
            # very left, where a ring around it would be cut off
            cr.set_source_rgba(*accent, 1)
            cr.set_line_width(3)
            _rounded(cr, 1.5, 1.5, cw - 3, ch - 3, 9)
            cr.stroke()
        if selected or cursor:
            # what Enter / A does: pick it up, or set it down
            hint = "← → ↑ ↓   ↵" if selected else "↵  " + self.app._("reorder_pick_up")
            pw = min(cw - 16, 30 + 7.5 * len(hint))
            # on a name plate, below the name rather than over it
            hy = ch - 40 if plate else ch / 2 - 15
            _rounded(cr, cw / 2 - pw / 2, hy, pw, 30, 15)
            cr.set_source_rgba(0, 0, 0, 0.7)
            cr.fill()
            _pango_text(cr, widget, hint, cw / 2 - pw / 2, hy + 5, pw, 11, True,
                        (1, 1, 1, 0.95), align_center=True)
        cr.restore()


class ReorderList(ReorderGrid):
    """Edit order in the list view: the library's rows, one under the
    other, moved the same ways as the grid's covers - dragged up and
    down, or picked up and moved with the keyboard or a controller."""

    GAP_X, GAP_Y, PAD_X = 0, 8, 10

    def __init__(self, app, games: List[GameItem], width: int, on_change: Callable[[List[str]], None]):
        self.icon_size = app.game_icon_size
        super().__init__(app, games, width - 2 * self.PAD_X, max(56, self.icon_size + 16), on_change)
        self._icons: Dict[str, Optional[GdkPixbuf.Pixbuf]] = {}
        self._badges: Dict[str, Optional[GdkPixbuf.Pixbuf]] = {}

    def _cols(self) -> int:
        return 1

    def _relayout(self):
        # as wide as it's given, the side room left for a lifted row
        self.cw = max(200, self.get_allocated_width() - 2 * self.PAD_X)
        super()._relayout()

    def _lift_margin(self) -> int:
        return self.PAD_X

    def _icon(self, game: GameItem) -> Optional[GdkPixbuf.Pixbuf]:
        if game.uid not in self._icons:
            app = self.app
            cached = app._find_cached_artwork(app.game_icons_dir, game.uid)
            try:
                self._icons[game.uid] = app._resolve_icon_pixbuf(self.icon_size, app._load_static_icon_source(cached),
                                                                 game.title)
            except Exception as e:
                logger.debug(f"Couldn't load icon for edit order ({game.uid}): {e}")
                self._icons[game.uid] = None
        return self._icons[game.uid]

    def _badge(self, launcher: str) -> Optional[GdkPixbuf.Pixbuf]:
        if launcher not in self._badges:
            self._badges[launcher] = self.app._home_launcher_badge(launcher, 24)
        return self._badges[launcher]

    def _draw_card(self, cr, widget, g: GameItem, index: int, accent, lifted: bool = False):
        app = self.app
        cw, ch = self.cw, self.ch
        x, y = self.pos.get(g.uid) or self._slot_xy(index)
        if lifted:
            y = max(4, min(widget.get_allocated_height() - ch - 12, y))
        selected = g.uid == self.selected_uid and widget.has_focus()
        cursor = (g.uid == self.cursor_uid and self.selected_uid is None and widget.has_focus()
                  and not self.drag)
        hover = (g.uid == self.hover_uid and not self.drag) or cursor
        active = selected or lifted or cursor
        ctx = widget.get_style_context()
        fg = ctx.get_color(ctx.get_state())
        fg_rgb = (fg.red, fg.green, fg.blue)
        found, raised = ctx.lookup_color("plunderhub_raised")
        row_rgb = (raised.red, raised.green, raised.blue) if found else (0.06, 0.18, 0.19)
        cr.save()
        cr.translate(x, y)
        if lifted:
            for k, a in ((10, 0.10), (6, 0.16), (3, 0.22)):
                _rounded(cr, -k / 2, k / 2 + 4, cw + k, ch, 16)
                cr.set_source_rgba(0, 0, 0, a)
                cr.fill()
        # the row, as in the list view - a touch lighter pointed at or held
        _rounded(cr, 0, 0, cw, ch, 14)
        cr.set_source_rgba(*row_rgb, 1)
        cr.fill_preserve()
        cr.set_source_rgba(*fg_rgb, 0.10 if lifted else 0.07 if hover else 0.04)
        cr.fill_preserve()
        if active:
            cr.set_source_rgba(*accent, 1)
            cr.set_line_width(2)
            cr.stroke()
        else:
            cr.new_path()

        # position
        bw = 30 if index < 9 else 38
        _rounded(cr, 12, ch / 2 - 12, bw, 24, 12)
        if active:
            cr.set_source_rgba(*accent, 0.95)
        else:
            cr.set_source_rgba(*fg_rgb, 0.12)
        cr.fill()
        _pango_text(cr, widget, str(index + 1), 12, ch / 2 - 9, bw, 9.5, True,
                    (*_on_accent_rgb(accent), 1) if active else (*fg_rgb, 0.9), align_center=True)
        left = 12 + bw + 14
        icon = self._icon(g)
        if icon is not None:
            Gdk.cairo_set_source_pixbuf(cr, icon, left, (ch - logical_size(icon)[1]) / 2)
            cr.paint()
            left += logical_size(icon)[0] + 16

        # the right-hand side: grip, playtime, launcher - laid out from the edge
        right = cw - 16
        cr.set_source_rgba(*fg_rgb, 0.55)
        for dx in (0, 6):
            for dy in (0, 5.5, 11):
                cr.arc(right - 9 + dx, ch / 2 - 5.5 + dy, 1.5, 0, 2 * math.pi)
                cr.fill()
        right -= 34
        if app.show_playtime:
            _pango_text(cr, widget, app._home_playtime_text(g.uid), right - 120, ch / 2 - 10, 120, 10.5, False,
                        (*fg_rgb, 0.65))
            right -= 136
        badge = self._badge(g.launcher)
        _pango_text(cr, widget, g.launcher, right - 110, ch / 2 - 10, 110, 10.5, False, (*fg_rgb, 0.65))
        if badge is not None:
            Gdk.cairo_set_source_pixbuf(cr, badge, right - 110 - logical_size(badge)[0] - 8,
                                        (ch - logical_size(badge)[1]) / 2)
            cr.paint()
        right -= 150

        title = ("★ " if g.uid in app.favorites else "") + g.title
        if selected or cursor:
            hint = "↑ ↓   ↵" if selected else "↵  " + app._("reorder_pick_up")
            pw = 30 + 7.5 * len(hint)
            _rounded(cr, right - pw, ch / 2 - 14, pw, 28, 14)
            cr.set_source_rgba(0, 0, 0, 0.55)
            cr.fill()
            _pango_text(cr, widget, hint, right - pw, ch / 2 - 9, pw, 10, True, (1, 1, 1, 0.95),
                        align_center=True)
            right -= pw + 12
        _pango_text(cr, widget, title, left, ch / 2 - 11, max(40, right - left), 12, True, (*fg_rgb, 1))
        cr.restore()


class LauncherOrderList(Gtk.DrawingArea):
    """The sidebar's launchers while editing order: drag a row to move
    it, click one to edit that launcher's order. With a keyboard or a
    controller (see handle): up / down go over them, Enter / A picks one
    up to move, right edits its games."""

    ROW_H = 54
    LOGO = 30           # as on the sidebar's rows (home_mixin.LauncherLogo.SIZE)

    def __init__(self, app, on_reorder: Callable[[List[str]], None], on_pick: Callable[[str], None]):
        super().__init__()
        self.app = app
        self.items: List[str] = []
        self.current: Optional[str] = None
        self.y: Dict[str, float] = {}
        self.drag: Optional[Dict] = None
        self.on_reorder = on_reorder
        self.on_pick = on_pick
        self._icons: Dict[str, Optional[GdkPixbuf.Pixbuf]] = {}
        self._tick_id: Optional[int] = None
        self.cursor: Optional[str] = None
        self.held: Optional[str] = None
        self._held_from: Optional[List[str]] = None
        self.set_can_focus(True)
        self.connect("focus-in-event", self._focus_in)
        self.connect("focus-out-event", self._focus_out)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.BUTTON_RELEASE_MASK
                        | Gdk.EventMask.POINTER_MOTION_MASK)
        self.connect("draw", self._draw)
        self.connect("button-press-event", self._press)
        self.connect("button-release-event", self._release)
        self.connect("motion-notify-event", self._motion)
        self.connect("destroy", lambda *_: self._tick_id and GLib.source_remove(self._tick_id))

    def set_items(self, names: List[str], current: Optional[str]):
        if self.drag is not None or self.held is not None:
            if set(names) == set(self.items):
                # a re-render under a drag (or a row held from the keys): the same launchers, the order
                # being dragged into stays
                self.current = current
                self.queue_draw()
                return
            # a launcher came or went: the drag ends here
            self.drag = None
            self.held = self._held_from = None
            _set_cursor(self, None)
        self.items = list(names)
        self.current = current
        self.y = {n: i * self.ROW_H for i, n in enumerate(self.items)}
        self._icons.clear()
        self.set_size_request(-1, len(self.items) * self.ROW_H + 4)
        self.queue_draw()

    def handle(self, action: str) -> Optional[str]:
        """Like ReorderGrid.handle: None when dealt with here, else where
        focus goes on (up / down past the ends, right into the games), or
        "back"."""
        if not self.items:
            return action
        if self.cursor not in self.items:
            self.cursor = self.current if self.current in self.items else self.items[0]
        if action == "activate":
            if self.held is None:
                self.held, self._held_from = self.cursor, list(self.items)
            else:
                self._set_down()
            self.queue_draw()
            return None
        if action == "back":
            if self.held is None:
                return "back"
            if self._held_from is not None and self._held_from != self.items:
                self.items = self._held_from
                self._start_tick()
                self.on_reorder(list(self.items))
            self._set_down()
            self.queue_draw()
            return None
        if action == "right" and self.held is None:
            if self.cursor != self.current:
                self.on_pick(self.cursor)
            return "right"
        step = {"up": -1, "down": 1}.get(action)
        if step is None:
            return None if self.held else action
        i = self.items.index(self.held or self.cursor)
        j = i + step
        if not 0 <= j < len(self.items):
            return None if self.held else action
        if self.held is not None:
            self.items.insert(j, self.items.pop(i))
            self._start_tick()
            self.on_reorder(list(self.items))
        else:
            self.cursor = self.items[j]
        self.queue_draw()
        return None

    def focus_end(self, last: bool):
        """Focus coming in from below (last) or above."""
        self.cursor = self.items[-1 if last else 0] if self.items else None
        self.grab_focus()

    def _set_down(self):
        if self.held is not None:
            self.cursor = self.held
        self.held = self._held_from = None

    def _focus_in(self, *_):
        if self.cursor not in self.items:
            self.cursor = self.current if self.current in self.items else (self.items[0] if self.items else None)
        self.queue_draw()
        return False

    def _focus_out(self, *_):
        self._set_down()
        self.queue_draw()
        return False

    def _icon(self, name: str) -> Optional[GdkPixbuf.Pixbuf]:
        if name not in self._icons:
            self._icons[name] = self.app._home_launcher_badge(name, self.LOGO)
        return self._icons[name]

    def _start_tick(self):
        if self._tick_id is None:
            self._tick_id = GLib.timeout_add(16, self._tick)

    def _tick(self) -> bool:
        busy = False
        for i, n in enumerate(self.items):
            if self.drag and self.drag["moved"] and self.drag["name"] == n:
                continue
            t = i * self.ROW_H
            if abs(self.y[n] - t) > 0.5:
                self.y[n] += (t - self.y[n]) * 0.25
                busy = True
            else:
                self.y[n] = t
        self.queue_draw()
        if busy or self.drag:
            return True
        self._tick_id = None
        return False

    def _press(self, _w, e) -> bool:
        if e.button != 1 or e.type != Gdk.EventType.BUTTON_PRESS:
            return True
        i = int(e.y // self.ROW_H)
        if 0 <= i < len(self.items):
            n = self.items[i]
            self.drag = {"name": n, "dy": e.y - self.y[n], "y0": e.y, "moved": False, "start": i}
            _set_cursor(self, "grabbing")
        return True

    def _motion(self, _w, e) -> bool:
        d = self.drag
        if d is None:
            _set_cursor(self, "grab" if 0 <= e.y < len(self.items) * self.ROW_H else None)
            return True
        if not d["moved"] and abs(e.y - d["y0"]) < DRAG_THRESHOLD:
            return True
        d["moved"] = True
        n = d["name"]
        self.y[n] = max(0, min((len(self.items) - 1) * self.ROW_H, e.y - d["dy"]))
        target = int((self.y[n] + self.ROW_H / 2) // self.ROW_H)
        cur = self.items.index(n)
        if target != cur:
            self.items.insert(target, self.items.pop(cur))
        self._start_tick()
        return True

    def _release(self, _w, _e) -> bool:
        d = self.drag
        if d is None:
            return True
        self.drag = None
        _set_cursor(self, "grab")
        self._start_tick()
        if not d["moved"]:
            self.on_pick(d["name"])
        elif self.items.index(d["name"]) != d["start"]:
            self.on_reorder(list(self.items))
        return True

    def _draw(self, widget, cr):
        accent = _accent_rgb(widget)
        ctx = widget.get_style_context()
        fg = ctx.get_color(ctx.get_state())
        w = widget.get_allocated_width()
        lifted_name = self.drag["name"] if self.drag and self.drag["moved"] else None
        if lifted_name is None and widget.has_focus():
            lifted_name = self.held
        cursor = self.cursor if widget.has_focus() and self.held is None else None
        for n in sorted(self.items, key=lambda name: name == lifted_name):
            lifted = n == lifted_name
            cr.save()
            cr.translate(8, self.y.get(n, 0) + 2)
            _rounded(cr, 0, 0, w - 16, self.ROW_H - 4, 10)
            if lifted:
                cr.set_source_rgba(0, 0, 0, 0.35)
                cr.fill_preserve()
                cr.set_source_rgba(*accent, 0.28)
                cr.fill_preserve()
                cr.set_source_rgba(*accent, 0.9)
                cr.set_line_width(1.5)
                cr.stroke()
            else:
                if n == self.current:
                    cr.set_source_rgba(*accent, 0.22)
                    cr.fill_preserve()
                if n == cursor:
                    # where the keyboard / controller is
                    cr.set_source_rgba(*accent, 1)
                    cr.set_line_width(2)
                    cr.stroke_preserve()
                cr.new_path()
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.55)
            for dx in (0, 5):
                for dy in (0, 5, 10):
                    cr.arc(14 + dx, (self.ROW_H - 4) / 2 - 5 + dy, 1.4, 0, 2 * math.pi)
                    cr.fill()
            # the logo, as big as in the sidebar
            ty = (self.ROW_H - 4 - 40) / 2
            icon = self._icon(n)
            if icon is not None:
                Gdk.cairo_set_source_pixbuf(cr, icon, 30 + (40 - logical_size(icon)[0]) / 2,
                                            ty + (40 - logical_size(icon)[1]) / 2)
                cr.paint()
            _pango_text(cr, widget, n, 82, (self.ROW_H - 4) / 2 - 10, w - 112, None, n == self.current or lifted,
                        (fg.red, fg.green, fg.blue, 1))
            cr.restore()
        return False
