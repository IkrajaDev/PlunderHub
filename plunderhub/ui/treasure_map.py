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

"""Treasure Map: every game the player owns on Steam, Epic, GOG and
Amazon on one sidebar page of the main window. "Aboard" = installed (full
colour, always first), "Buried" = owned but not installed (faded; Dig up
opens a card about the game and installs it - Steam through its own files,
Heroic's stores with Heroic's own tools - and follows the download).

Smooth with hundreds of games:
- the grid is drawn by hand with its own scrolling: the area is only as
  tall as the window (X11 caps a window at 32767 px), only what is on
  screen is painted;
- every tile (shadow, cover, badge, name) is rendered ONCE into its own
  surface; a scroll frame is a few dozen surface blits, no text layout,
  no scaling. A tile is re-rendered only when it changes (cover arrived,
  hover, download step) - at most a few per frame, the rest next frame;
- covers are decoded off the main thread straight at the tile's size, a
  screen ahead of the scroll; pixbufs and tiles live in small LRU caches
  (a few hundred, not all 700+)."""

import math
import os
import re
import threading
import weakref
import time
from collections import OrderedDict
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import cairo

from plunderhub.logging_setup import logger
from plunderhub.perf_guard import allow_hover_relayout
from plunderhub.gi_setup import Gdk, GdkPixbuf, GLib, Gtk, Pango, logical_size, pixbuf_surface, ui_scale
from plunderhub.theme import THEME_PALETTES
from plunderhub.ui.home_mixin import _accent_rgb, _on_accent_rgb, _pango_layout, _pango_text, _rounded, _title_gradient

from plunderhub.core import treasure as hold_data
from plunderhub.core.treasure import OwnedGame, StoreState
from plunderhub.ui.game_page import TIER_COLOURS

TABS = ("all", "steam", "epic", "gog", "amazon", "faugus")
STORE_NAMES = {"steam": "Steam", "epic": "Epic", "gog": "GOG", "amazon": "Amazon", "faugus": "Faugus"}
LAUNCHER_OF = {"steam": "Steam", "epic": "Heroic", "gog": "Heroic", "amazon": "Heroic", "faugus": "Faugus"}
KIND_TAG = {"demo": "tm_kind_demo", "application": "tm_kind_software", "beta": "tm_kind_beta", "mod": "tm_kind_mod"}
SORTS = (("alpha", "sort_alpha"), ("playtime", "sort_playtime"),
         ("newest", "tm_sort_newest"), ("oldest", "tm_sort_oldest"))
ASPECT = 4 / 3          # the library's cards (home_mixin: cover_size x cover_size * 4 / 3)
SAT_BURIED = 0.22       # how much colour a buried cover keeps
TILE_RENDERS_PER_FRAME = 10
WORKERS = 4
PIXBUF_CACHE = 260
TILE_CACHE = 140

CSS = b"""
.tm-title { font-size: 24px; font-weight: 800; letter-spacing: -0.3px; }
.tm-sub { opacity: 0.68; }
.tm-dock { padding: 10px 16px; }
.tm-dock-title { font-weight: 700; }
.tm-dock-sub { opacity: 0.7; font-size: 92%; }
.tm-state-title { font-size: 20px; font-weight: 800; }
.tm-state-text { opacity: 0.75; }
.tm-card-desc { opacity: 0.8; }
.tm-card-hint { opacity: 0.6; font-size: 92%; }
/* the app gives dialog, .dialog-vbox and stack a big 160 % gradient each - three
   gradient fills under the grid on every scroll frame (~15 ms); the window paints
   its own flat background instead */
dialog.tm-window, dialog.tm-window .dialog-vbox, dialog.tm-window stack { background-image: none; }
stack.tm-body { background-image: none; background-color: transparent; }
"""




def hold_data_faugus():
    from plunderhub.core import faugus_add
    return faugus_add


def faugus_ui():
    """plunderhub.ui.faugus_aboard, imported when first needed (it imports this module)."""
    from plunderhub.ui import faugus_aboard
    return faugus_aboard


def _hex(value: str) -> Tuple[float, float, float]:
    h = value.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _fmt_gb(n: float) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def _sort_key(g: OwnedGame):
    return re.sub(r"^(the|a|an)\s+", "", g.title.lower())


def _load_cover_at(path: Path, tw: int, th: int) -> GdkPixbuf.Pixbuf:
    """path decoded straight at tw x th device px, cropped to fill
    (the top kept: titles sit there)."""
    _fmt, w, h = GdkPixbuf.Pixbuf.get_file_info(str(path))
    if not w or not h:
        pb = GdkPixbuf.Pixbuf.new_from_file(str(path))
        w, h = pb.get_width(), pb.get_height()
    k = max(tw / w, th / h)
    sw, sh = max(tw, int(math.ceil(w * k))), max(th, int(math.ceil(h * k)))
    pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(path), sw, sh, False)
    x, y = (pb.get_width() - tw) // 2, (pb.get_height() - th) // 4
    return pb.new_subpixbuf(max(0, x), max(0, y), tw, th).copy()


# --- covers: decoded off the main thread, a screen ahead ---------------------------

class CoverLoader:
    library_art = None      # game -> the library's cover of it, if it has one

    """Newest request first (what was just scrolled to), 3 workers, at
    the tile's own pixel size. A small LRU keeps them: one evicted is
    read again from the disk cache (fast) if it comes back on screen."""

    def __init__(self, cache_dir: Path, on_ready: Callable[[str], None]):
        self.cache_dir = cache_dir
        self.on_ready = on_ready
        self.size = (160, 240)
        self.pixbufs: "OrderedDict[str, Optional[GdkPixbuf.Pixbuf]]" = OrderedDict()
        self.files: Dict[str, Optional[Path]] = {}
        self._pending: "OrderedDict[str, OwnedGame]" = OrderedDict()
        self._lock = threading.Lock()
        self._wake = threading.Semaphore(0)
        self._stop = False
        for _ in range(WORKERS):
            threading.Thread(target=self._work, daemon=True).start()

    def set_size(self, w: int, h: int):
        if (w, h) != self.size:
            self.size = (w, h)
            self.pixbufs.clear()

    def get(self, uid: str):
        pb = self.pixbufs.get(uid)
        if uid in self.pixbufs:
            self.pixbufs.move_to_end(uid)
        return pb

    def known(self, uid: str) -> bool:
        return uid in self.pixbufs

    def want(self, game: OwnedGame):
        if game.uid in self.pixbufs:
            return
        with self._lock:
            if game.uid in self._pending:
                self._pending.move_to_end(game.uid)
                return
            self._pending[game.uid] = game
        self._wake.release()

    def _path_of(self, game):
        """The cover the library shows for an Aboard game, else the store's."""
        return (self.library_art(game) if self.library_art is not None else None) \
            or hold_data.cover_file(game, self.cache_dir)

    def forget(self, uid: str):
        self.files.pop(uid, None)
        self.pixbufs.pop(uid, None)

    def stop(self):
        self._stop = True
        for _ in range(WORKERS):
            self._wake.release()

    def _work(self):
        while True:
            self._wake.acquire()
            if self._stop:
                return
            with self._lock:
                if not self._pending:
                    continue
                uid, game = self._pending.popitem(last=True)
                size = self.size
            result = None
            try:
                path = self.files.get(uid) if uid in self.files else self._path_of(game)
                self.files[uid] = path
                if path is not None:
                    result = _load_cover_at(path, *size)
            except Exception:
                result = None
                try:            # the library's own art may be a format only it reads
                    path = hold_data.cover_file(game, self.cache_dir)
                    self.files[uid] = path
                    result = _load_cover_at(path, *size) if path is not None else None
                except Exception:
                    result = None
            # above the redraw's priority: at the default idle one, covers only
            # landed once a scroll stopped (frames kept the loop busy)
            GLib.idle_add(self._done, uid, result, size, priority=GLib.PRIORITY_HIGH_IDLE)

    def _done(self, uid, result, size):
        if size != self.size:
            return False
        self.pixbufs[uid] = result
        while len(self.pixbufs) > PIXBUF_CACHE:
            self.pixbufs.popitem(last=False)
        self.on_ready(uid)
        return False


# --- the grid -------------------------------------------------------------------------

class Item:
    __slots__ = ("game", "x", "y", "w", "h")

    def __init__(self, game, x, y, w, h):
        self.game, self.x, self.y, self.w, self.h = game, x, y, w, h


class Header:
    __slots__ = ("text", "count", "note", "y")

    def __init__(self, text, count, note, y):
        self.text, self.count, self.note, self.y = text, count, note, y


class MapGrid(Gtk.EventBox):
    # the library's grid: content margin 16, FlowBox spacing 18 / 14; the
    # name sits on the cover like on the library's cards (TEXT_H 0)
    PAD, GAP_X, GAP_Y, TEXT_H, HEAD_H, M = 20, 18, 14, 0, 52, 14

    def __init__(self, view: "TreasureMap"):
        super().__init__()
        self.view = view
        self.app = view.app
        self.sections = []
        self.items: List[Item] = []
        self.headers: List[Header] = []
        self.content_h = 0
        self.tile_w, self.tile_h = 150, 225
        self.hover: Optional[Item] = None
        self.adj = Gtk.Adjustment(value=0, lower=0, upper=1, step_increment=60, page_increment=400, page_size=1)
        self.adj.connect("value-changed", lambda *_: self.queue_draw())
        self._tiles: "OrderedDict[str, Tuple[tuple, cairo.Surface]]" = OrderedDict()
        self._heads: Dict[tuple, cairo.Surface] = {}
        self._badges: Dict[str, Optional[GdkPixbuf.Pixbuf]] = {}
        self._more_pending = False
        self._scroll_anim: Optional[int] = None
        self._scroll_to = 0.0
        # no GdkWindow of its own to paint into (only an input one): a child
        # window inside the main window's RGBA one was composited on every
        # scroll frame - the drawing took 5 ms, the frame 24
        self.set_visible_window(False)
        self.set_app_paintable(True)
        self.set_can_focus(True)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.POINTER_MOTION_MASK
                        | Gdk.EventMask.LEAVE_NOTIFY_MASK | Gdk.EventMask.SCROLL_MASK
                        | Gdk.EventMask.SMOOTH_SCROLL_MASK)
        self.connect("draw", self._draw)
        self.connect("size-allocate", lambda *_: self._relayout())
        self.connect("motion-notify-event", self._on_motion)
        self.connect("leave-notify-event", lambda *_: self._set_hover(None))
        self.connect("button-press-event", self._on_click)
        self.connect("scroll-event", self._on_scroll)
        # the keyboard's cursor (the hover) goes with the focus
        self.connect("focus-out-event", lambda *_: self._set_hover(None) and False)

    # layout
    def set_sections(self, sections, keep_scroll: bool = False):
        self.sections = sections
        if not keep_scroll:
            self.adj.set_value(0)
            self._scroll_to = 0
        self._relayout()

    def _relayout(self):
        W, H = self.get_allocated_width(), self.get_allocated_height()
        if W <= 1:
            return
        f = self.app._text_factor()
        pad, gx = self.PAD, self.GAP_X
        tw = int(self.app.cover_size)                  # Ship's Controls > Covers: the cover size
        cols = max(1, int((W - 2 * pad + gx) // (tw + gx)))
        th = int(tw * ASPECT)
        if (tw, th) != (self.tile_w, self.tile_h):
            self._tiles.clear()
            d = self.get_scale_factor()
            self.view.loader.set_size(tw * d, th * d)
        self.tile_w, self.tile_h = tw, th
        row_h = th + self.TEXT_H * f + self.GAP_Y
        self.items, self.headers = [], []
        y = 6.0
        for title, note, games in self.sections:
            if title:
                self.headers.append(Header(title, len(games), note, y))
                y += self.HEAD_H * f
                if title in self.view.collapsed:
                    y += 6
                    continue
            for i, g in enumerate(games):
                r, c = divmod(i, cols)
                self.items.append(Item(g, pad + c * (tw + gx), y + r * row_h, tw, th))
            y += ((len(games) + cols - 1) // cols) * row_h + 14
        self.content_h = y + 16
        self.adj.configure(min(self.adj.get_value(), max(0, self.content_h - H)), 0, max(self.content_h, H),
                           60, H * 0.9, H)
        self.queue_draw()

    # scrolling: eased, our own
    def _on_scroll(self, _w, ev):
        ok, _dx, dy = ev.get_scroll_deltas()
        if not ok:
            dy = {Gdk.ScrollDirection.UP: -1, Gdk.ScrollDirection.DOWN: 1}.get(ev.direction, 0)
        dev = ev.get_source_device()
        touchpad = dev is not None and dev.get_source() == Gdk.InputSource.TOUCHPAD
        if touchpad:                        # follows the fingers, no easing
            self._stop_anim()
            self.adj.set_value(min(self.adj.get_upper() - self.adj.get_page_size(),
                                   max(0, self.adj.get_value() + dy * 28)))
        else:
            # GTK's own wheel step (what the library's scroller moves): page^(2/3)
            self.scroll_by(dy * self.adj.get_page_size() ** (2 / 3))
        if self.hover is not None:
            self._set_hover(None)
        return True

    def _stop_anim(self):
        if self._scroll_anim is not None:
            self.remove_tick_callback(self._scroll_anim)
            self._scroll_anim = None

    def scroll_by(self, delta: float):
        top = max(0, self.adj.get_upper() - self.adj.get_page_size())
        base = self._scroll_to if self._scroll_anim is not None else self.adj.get_value()
        self._scroll_to = min(top, max(0, base + delta))
        self._anim_from, self._anim_t0 = self.adj.get_value(), None
        if self._scroll_anim is None:
            self._scroll_anim = self.add_tick_callback(self._scroll_tick)

    def _scroll_tick(self, _w, clock):
        now = clock.get_frame_time() / 1e6
        if self._anim_t0 is None:
            self._anim_t0 = now
        t = min(1.0, (now - self._anim_t0) / 0.22)
        ease = 1 - (1 - t) ** 3
        self.adj.set_value(self._anim_from + (self._scroll_to - self._anim_from) * ease)
        if t >= 1.0:
            self._scroll_anim = None
            return False
        return True

    # hover / click
    def _hit(self, x, y) -> Optional[Item]:
        yy = y + self.adj.get_value()
        for it in self._visible():
            if it.x <= x <= it.x + it.w and it.y <= yy <= it.y + it.h + self.TEXT_H:
                return it
        return None

    def _on_motion(self, _w, ev):
        if self._scroll_anim is None:
            self._set_hover(self._hit(ev.x, ev.y))

    def _set_hover(self, it):
        if it is self.hover:
            return
        old, self.hover = self.hover, it
        for x in (old, it):
            if x is not None:
                self.queue_item(x)
        # a card about to be opened: its art and words fetched meanwhile
        if getattr(self, "_prefetch_id", None):
            GLib.source_remove(self._prefetch_id)
            self._prefetch_id = None
        if it is not None:
            def go(game=it.game):
                self._prefetch_id = None
                self.view.prefetch(game)
                return False
            self._prefetch_id = GLib.timeout_add(120, go)
        win = self.get_window()
        if win is not None:
            win.set_cursor(Gdk.Cursor.new_from_name(self.get_display(), "pointer") if it else None)

    def queue_item(self, it):
        top = self.adj.get_value()
        m = self.M
        self.queue_draw_area(int(it.x) - m, int(it.y - top) - m, int(it.w) + 2 * m,
                             int(it.h + self.TEXT_H) + 2 * m)

    # keyboard / controller: the hover is the cursor too
    def kb_enter(self) -> bool:
        """Onto the tiles: the one stood on last, else the first in sight."""
        self.grab_focus()
        if not self.items:
            return True
        uid = getattr(self, "_kb_uid", None)
        it = next((i for i in self.items if i.game.uid == uid), None)
        if it is None:
            vis = self._visible()
            it = vis[0] if vis else self.items[0]
        self._kb_to(it)
        return True

    def _kb_to(self, it: "Item"):
        self._kb_uid = it.game.uid
        self._set_hover(it)
        top, H = self.adj.get_value(), self.get_allocated_height()
        if it.y < top + 12:
            # the section's heading in sight too when it's the first row
            first_row = not any(i.y < it.y for i in self.items if i.y > it.y - it.h - self.GAP_Y - 1)
            head = self.HEAD_H * self.app._text_factor() if first_row else 0
            self.scroll_by(it.y - head - 12 - top)
        elif it.y + it.h > top + H - 12:
            self.scroll_by(it.y + it.h + 16 - top - H)

    def kb_move(self, direction: str) -> bool:
        """An arrow / the d-pad. False off the left edge (to the sidebar)."""
        cur = self.hover if self.hover in self.items else None
        if cur is None:
            return self.kb_enter()
        if direction in ("left", "right"):
            i = self.items.index(cur) + (1 if direction == "right" else -1)
            if i < 0 or (direction == "left" and self.items[i].y != cur.y):
                return False
            if i < len(self.items):
                self._kb_to(self.items[i])
            return True
        if direction == "down":
            rows = sorted({i.y for i in self.items if i.y > cur.y})
        else:
            rows = sorted({i.y for i in self.items if i.y < cur.y}, reverse=True)
        if not rows:
            if direction == "up":
                self.scroll_by(-self.adj.get_value())
            return True
        row = [i for i in self.items if i.y == rows[0]]
        self._kb_to(min(row, key=lambda i: abs(i.x - cur.x)))
        return True

    def kb_activate(self) -> bool:
        if self.hover in self.items:
            self.view.on_tile(self.hover.game)
            return True
        return False

    def _on_click(self, _w, ev):
        if ev.button == 3:
            self._context_menu(ev)
            return True
        if ev.button != 1:
            return False
        f = self.app._text_factor()
        yy = ev.y + self.adj.get_value()
        for hd in self.headers:
            if hd.y <= yy <= hd.y + self.HEAD_H * f:
                self.view.toggle_section(hd.text)
                return True
        it = self._hit(ev.x, ev.y)
        if it is not None:
            self.view.on_tile(it.game)
        return True

    def _context_menu(self, ev):
        """An Aboard game's right-click: the library's own menu."""
        it = self._hit(ev.x, ev.y)
        if it is None or not it.game.installed or it.game.uid in self.view.downloads:
            return
        game = self.view.library_game(it.game)
        if game is not None:
            self._set_hover(None)
            self.app._game_menu_at(self, ev, game)

    def _visible(self, margin: float = 0):
        top = self.adj.get_value() - margin
        bottom = self.adj.get_value() + self.get_allocated_height() + margin
        th = self.TEXT_H
        return [it for it in self.items if it.y + it.h + th >= top and it.y <= bottom]

    def cover_ready(self, uid):
        self.refresh_game(uid)

    def refresh_game(self, uid):
        self._tiles.pop(uid, None)
        for it in self._visible():
            if it.game.uid == uid:
                self.queue_item(it)

    # drawing
    def _draw(self, _w, cr):
        c = self.view.colors()
        accent = _accent_rgb(self)
        W, H = self.get_allocated_width(), self.get_allocated_height()
        top = self.adj.get_value()
        if not self.view.embedded:              # embedded: the page under it painted the sea already
            self.view.paint_bg(cr, self, W, H)
        f = self.app._text_factor()
        for hd in self.headers:
            y = hd.y - top
            if y + self.HEAD_H * f < 0 or y > H:
                continue
            cr.set_source_surface(self._head_surface(hd, c, accent, W, f), 0, y)
            cr.paint()
        clip = cr.clip_extents()
        budget = TILE_RENDERS_PER_FRAME
        m = self.M
        for it in self._visible(margin=H):
            y = it.y - top
            if not self.view.loader.known(it.game.uid):
                self.view.loader.want(it.game)          # a screen ahead too
            if y + it.h + self.TEXT_H + m < clip[1] or y - m > clip[3] \
                    or it.x + it.w + m < clip[0] or it.x - m > clip[2]:
                continue
            surf, budget = self._tile(it, c, accent, budget)
            if surf is not None:
                cr.set_source_surface(surf, it.x - m, y - m)
                cr.paint()
            else:                                       # next frame: a quiet placeholder till then
                _rounded(cr, it.x, y, it.w, it.h, 14)
                cr.set_source_rgba(*c["raised"], 1)
                cr.fill()
                self._render_more()
        return False

    def _render_more(self):
        if not self._more_pending:
            self._more_pending = True

            def again():
                self._more_pending = False
                self.queue_draw()
                return False
            GLib.idle_add(again, priority=GLib.PRIORITY_HIGH_IDLE)

    def _head_surface(self, hd, c, accent, W, f):
        collapsed = hd.text in self.view.collapsed
        key = (hd.text, hd.count, hd.note, W, f, self.app.theme, collapsed)
        surf = self._heads.get(key)
        if surf is not None:
            return surf
        d = self.get_scale_factor()
        h = int(self.HEAD_H * f)
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, W * d, h * d)
        surf.set_device_scale(d, d)
        cr = cairo.Context(surf)
        x = self.PAD
        # a chevron: the section folds (click anywhere on its heading)
        cy = 14 * f + 11 * f
        cr.new_path()
        cr.set_line_width(2 * f)
        cr.set_line_cap(cairo.LINE_CAP_ROUND)
        cr.set_line_join(cairo.LINE_JOIN_ROUND)
        k = 4.5 * f
        if collapsed:
            cr.move_to(x + k * 0.6, cy - k)
            cr.line_to(x + k * 1.6, cy)
            cr.line_to(x + k * 0.6, cy + k)
        else:
            cr.move_to(x, cy - k * 0.5)
            cr.line_to(x + k, cy + k * 0.5)
            cr.line_to(x + 2 * k, cy - k * 0.5)
        cr.set_source_rgba(*c["fg"], 0.7)
        cr.stroke()
        x += 2 * k + 10 * f
        shown = self.app._({"Aboard": "tm_aboard", "Buried": "tm_buried", "Harbors": "section_harbors"}.get(hd.text, hd.text))
        used = _text_w(self, cr, shown, 18 * f, True)
        _pango_text(cr, self, shown, x, 10 * f, W, 18 * f, True, (*c["fg"], 1))
        x += used + 12 * f
        cnt = str(hd.count)
        cw = _text_w(self, cr, cnt, 10.5 * f, True) + 18 * f
        _rounded(cr, x, 13 * f, cw, 24 * f, 12 * f)
        cr.set_source_rgba(*accent, 0.18)
        cr.fill()
        _pango_text(cr, self, cnt, x, 16 * f, cw, 10.5 * f, True, (*accent, 1), align_center=True)
        x += cw + 14 * f
        _pango_text(cr, self, hd.note, x, 16 * f, W - x - self.PAD, 10.5 * f, False, (*c["fg"], 0.55))
        if len(self._heads) > 12:
            self._heads.clear()
        self._heads[key] = surf
        return surf

    def _badge(self, launcher):
        if launcher not in self._badges:
            self._badges[launcher] = self.app._home_launcher_badge(launcher, 32)
        return self._badges[launcher]

    def _tile(self, it, c, accent, budget):
        g = it.game
        dl = self.view.downloads.get(g.uid)
        if dl is not None and dl["state"] == "done":
            dl = None
        hover = it is self.hover
        sig = (self.tile_w, g.installed, hover, g.uid in self.view.removing, self.app._text_factor(),
               getattr(self.app, "show_game_titles", True), getattr(self.app, "show_playtime", True), self.view.loader.known(g.uid), self.view.tab == "all",
               self.app.theme, None if dl is None else (dl["state"], int(dl["frac"] * 100), dl.get("place"), dl.get("phase")),
               g.uid in self.view.faugus_running)
        hit = self._tiles.get(g.uid)
        if hit is not None and hit[0] == sig:
            self._tiles.move_to_end(g.uid)
            return hit[1], budget
        if budget <= 0:
            return (hit[1] if hit is not None else None), budget
        surf = self._render_tile(it, c, accent, hover, dl)
        self._tiles[g.uid] = (sig, surf)
        while len(self._tiles) > TILE_CACHE:
            self._tiles.popitem(last=False)
        return surf, budget - 1

    def _render_tile(self, it, c, accent, hover, dl):
        g = it.game
        if g.uid == faugus_ui().ADD_UID:
            return faugus_ui().render_add_tile(self, it, c, accent, hover)
        m, w, h = self.M, it.w, it.h
        d = self.get_scale_factor()
        tw, th = int(w + 2 * m), int(h + self.TEXT_H + 2 * m)
        surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, tw * d, th * d)
        surf.set_device_scale(d, d)
        cr = cairo.Context(surf)
        lit = g.installed or hover or dl is not None
        x, y = m, m - (4 if hover else 0)
        for i, a in ((10, 0.05), (6, 0.08), (3, 0.12)) if lit else ((4, 0.06),):
            _rounded(cr, x - i * 0.3, y + i * 0.9, w + i * 0.6, h + i * 0.4, 14 + i * 0.4)
            cr.set_source_rgba(0, 0, 0, a * (1.6 if hover else 1))
            cr.fill()
        cr.save()
        _rounded(cr, x, y, w, h, 14)
        cr.clip()
        # solid under the faded art: the bubbles swim behind the cards, as in the library
        cr.set_source_rgba(*c["bg"], 1)
        cr.paint()
        pb = self.view.loader.get(g.uid)
        if pb is not None:
            if not lit:
                dim = pb.copy()
                pb.saturate_and_pixelate(dim, SAT_BURIED, False)
                pb = dim
            cr.set_source_surface(Gdk.cairo_surface_create_from_pixbuf(pb, d, None), x, y)
            cr.paint_with_alpha(1.0 if lit else 0.62)
        else:
            cr.translate(x, y)
            if self.view.loader.known(g.uid):              # no cover anywhere: its own colours
                _title_gradient(cr, g.title, w, h, 1.0 if lit else 0.45)
                _pango_text(cr, self, g.title, 14, h * 0.36, w - 28, 12, True,
                            (1, 1, 1, 0.95 if lit else 0.55), lines=4, align_center=True)
            else:                                          # still loading: its name, quietly
                cr.set_source_rgba(*c["raised"], 1)
                cr.paint()
                _pango_text(cr, self, g.title, 14, h * 0.4, w - 28, 11, True,
                            (*c["fg"], 0.35), lines=3, align_center=True)
            cr.translate(-x, -y)
        if not lit:
            cr.set_source_rgba(*c["bg"], 0.22)
            cr.paint()
        f = self.app._text_factor()
        titles = getattr(self.app, "show_game_titles", True)
        if titles or hover or dl is not None:
            # the name on the cover's foot, like the library's cards
            grad = cairo.LinearGradient(0, y + h * 0.55, 0, y + h)
            grad.add_color_stop_rgba(0, 0, 0, 0, 0)
            grad.add_color_stop_rgba(1, 0, 0, 0, 0.82 if lit else 0.6)
            cr.set_source(grad)
            cr.rectangle(x, y, w, h)
            cr.fill()
        cr.restore()
        # the launcher's badge, top left, like the library's cards
        badge = self.app._game_badge(LAUNCHER_OF[g.store], g.extra.get("store_icon"), 32) \
            if g.extra.get("store_icon") else self._badge(LAUNCHER_OF[g.store])
        if badge is not None:
            bw, bh = logical_size(badge)
            bx, by = x + 10, y + 10
            cr.set_source_rgba(0, 0, 0, 0.55)
            cr.mask_surface(pixbuf_surface(badge), bx + 1, by + 1.5)
            cr.save()
            cr.rectangle(bx, by, bw, bh)
            cr.clip()
            cr.set_source_surface(pixbuf_surface(badge), bx, by)
            cr.paint_with_alpha(1.0 if lit else 0.7)
            cr.restore()
        if titles:
            meta = []
            tag = KIND_TAG.get(g.extra.get("kind", ""))
            tag = self.app._(tag) if tag else None
            if tag:
                meta.append(tag)
            if g.playtime and getattr(self.app, "show_playtime", True):
                hh, mm = divmod(g.playtime, 60)
                meta.append(f"{hh}h {mm}m" if hh else f"{mm}m")
            elif self.view.tab == "all" and g.store != "steam":    # Steam's badge says it already
                meta.append(STORE_NAMES[g.store])
            meta_text = "  ·  ".join(meta)
            # the library cards' own sizes (home_mixin._paint_static): grown
            # with the cover from 300 px wide, and with the text size
            k = min(1.6, max(0.8, w / 300)) * f
            title_pt = min(max(12.5 * k, 9.5), w / 13)
            meta_pt = min(max(10 * k, 8.5), w / 16)
            pad = min(12 * k, w * 0.06)
            mw = _text_w(self, cr, meta_text, meta_pt, False) + 10 * k if meta_text else 0
            ty = y + h - 10 * k - title_pt * 1.4
            _pango_text(cr, self, g.title, x + pad, ty, w - 2 * pad - mw, title_pt, True,
                        (1, 1, 1, 0.97 if lit else 0.72))
            if meta_text:
                _pango_text(cr, self, meta_text, x + w - pad - mw + 10 * k,
                            ty + (title_pt - meta_pt) * 0.75, mw, meta_pt, False,
                            (1, 1, 1, 0.82 if lit else 0.55))
        if g.uid in self.view.removing:
            self._scrim(cr, x, y, w, h)
            px, py, pw, ph = self._panel(cr, x, y, w, h, 64 * f)
            _pango_text(cr, self, self.app._("tm_going_overboard"), px + 12, py + (ph - 18 * f) / 2, pw - 24,
                        13 * f, True, (1, 1, 1, 0.97), align_center=True)
        elif dl is not None and dl["state"] != "done" and g.store == "faugus":
            faugus_ui().draw_phase_progress(self, cr, x, y, w, h, dl, accent, f)
        elif dl is not None and dl["state"] != "done":
            self._draw_progress(cr, x, y, w, h, dl, accent, f)
        if g.uid in self.view.faugus_running and not (dl is not None and dl["state"] != "done"):
            faugus_ui().draw_running_chip(self, cr, x, y, w, accent, f)
        if (dl is not None and dl["state"] != "done") or g.uid in self.view.removing:
            pass
        elif hover:
            if g.uid in self.view.faugus_running:
                label = "■  " + self.app._("tm_fg_close")
            else:
                label = ("▶  " + self.app._("launch_label")) if g.installed else ("⤓  " + self.app._("tm_dig_up"))
            pw = _text_w(self, cr, label, 14 * f, True) + 44 * f
            ph = 44 * f
            px, py = x + (w - pw) / 2, y + (h - ph) / 2
            for i, a in ((8, 0.10), (4, 0.16)):
                _rounded(cr, px - i * 0.2, py + i * 0.6, pw + i * 0.4, ph + i * 0.2, ph / 2)
                cr.set_source_rgba(0, 0, 0, a)
                cr.fill()
            _rounded(cr, px, py, pw, ph, ph / 2)
            cr.set_source_rgba(*accent, 1)
            cr.fill()
            _pango_text(cr, self, label, px, py + (ph - 19 * f) / 2, pw, 14 * f, True,
                        (*_on_accent_rgb(accent), 1), align_center=True)
        return surf

    def _scrim(self, cr, x, y, w, h):
        """Darkens the whole cover so what's said over it reads on any art."""
        cr.save()
        _rounded(cr, x, y, w, h, 14)
        cr.clip()
        cr.set_source_rgba(0, 0, 0, 0.58)
        cr.paint()
        cr.restore()

    def _panel(self, cr, x, y, w, h, ph):
        """A dark rounded plate in the cover's middle; its (x, y, w, h)."""
        pw = w - 2 * max(14, w * 0.07)
        px, py = x + (w - pw) / 2, y + (h - ph) / 2
        _rounded(cr, px, py + 3, pw, ph, 14)
        cr.set_source_rgba(0, 0, 0, 0.25)
        cr.fill()
        _rounded(cr, px, py, pw, ph, 14)
        cr.set_source_rgba(0.04, 0.07, 0.08, 0.9)
        cr.fill_preserve()
        cr.set_source_rgba(1, 1, 1, 0.12)
        cr.set_line_width(1)
        cr.stroke()
        return px, py, pw, ph

    def reveal(self, cr, x, y, w, h, frac, accent, f=1.0):
        """Any install on its tile: the cover comes to light from the bottom
        up as it goes on."""
        level = y + h * (1 - frac)
        cr.save()
        _rounded(cr, x, y, w, h, 14)
        cr.clip()
        cr.rectangle(x, y, w, level - y)
        cr.set_source_rgba(0.02, 0.05, 0.06, 0.80)
        cr.fill()
        if frac > 0:
            glow = cairo.LinearGradient(0, level - 34 * f, 0, level)
            glow.add_color_stop_rgba(0, *accent, 0)
            glow.add_color_stop_rgba(1, *accent, 0.5)
            cr.rectangle(x, level - 34 * f, w, 34 * f)
            cr.set_source(glow)
            cr.fill()
            cr.rectangle(x, level - 1.5, w, 3)
            cr.set_source_rgba(*accent, 1)
            cr.fill()
        cr.restore()

    def _draw_progress(self, cr, x, y, w, h, dl, accent, f=1.0):
        frac, state = max(0.0, min(1.0, dl["frac"])), dl["state"]
        big = state not in ("queued", "asked")
        if state == "asked":
            label = self.app._("tm_opening").format(launcher=dl["launcher"])
        elif state == "queued":
            label = self.app._("tm_waiting_n").format(n=dl["place"]) if dl.get("place") else self.app._("tm_waiting")
        else:
            label = f"{int(frac * 100)} %"
        self.reveal(cr, x, y, w, h, frac if big else 0.0, accent, f)
        tsize = (20 if big else 13) * f
        px, py, pw, ph = self._panel(cr, x, y, w, h, tsize * 1.5 + 24 * f)
        _pango_text(cr, self, label, px + 12, py + 12 * f, pw - 24, tsize, True,
                    (1, 1, 1, 0.98), align_center=True, lines=2)


class InstallingCard(Gtk.EventBox):
    """A game being dug up, in the library's grid where it will land: the
    Treasure Map's own tile (cover, name, progress plate), redrawn as the
    install moves. A click opens the map."""

    live: "weakref.WeakSet[InstallingCard]" = None   # set below
    LIFT = 10

    def __init__(self, app, view: "TreasureMap", game: OwnedGame, w: int, h: int):
        super().__init__()
        self.app, self.view, self.game, self.w, self.h = app, view, game, w, h
        self._surf = None
        self._sig = None
        area = Gtk.DrawingArea()
        # the library's cards keep 10 px on top to rise into (theme.py
        # button.home-card): the same, so the covers line up
        area.set_size_request(w, h + self.LIFT)
        area.connect("draw", self._draw)
        self.area = area
        self.add(area)
        self.set_halign(Gtk.Align.CENTER)
        self.set_valign(Gtk.Align.START)
        self.set_visible_window(False)
        self.set_tooltip_text(app._("tm_installing_tooltip"))
        self.connect("button-release-event", self._click)
        self.connect("realize", lambda w: w.get_window() and w.get_window().set_cursor(
            Gdk.Cursor.new_from_name(w.get_display(), "pointer")))
        InstallingCard.live.add(self)

    def _click(self, _w, ev):
        if ev.button == 1:
            self.app._set_home_nav("map")
        return True

    def _draw(self, widget, cr):
        grid = self.view.grid
        dl = self.view.downloads.get(self.game.uid)
        if dl is None:
            return False
        if not self.view.loader.known(self.game.uid):
            self.view.loader.want(self.game)
        sig = (self.w, self.h, dl["state"], int(dl["frac"] * 100), dl.get("place"),
               self.view.loader.get(self.game.uid) is not None, self.app.theme, self.app._text_factor())
        if sig != self._sig or self._surf is None:
            self._sig = sig
            self._surf = grid._render_tile(Item(self.game, 0, 0, self.w, self.h), self.view.colors(),
                                           _accent_rgb(widget), False, dl)
        m = grid.M
        cr.set_source_surface(self._surf, -m, self.LIFT - m)
        cr.paint()
        return False


InstallingCard.live = weakref.WeakSet()


class InstallingRow(Gtk.Box):
    """The list view's InstallingCard: a row like the library's (launcher
    badge, name) with the install's state and a bar under the name. A
    click opens the map."""

    def __init__(self, app, view: "TreasureMap", game: OwnedGame):
        super().__init__(spacing=12)
        self.app, self.view, self.game = app, view, game
        self.get_style_context().add_class("home-list-row")
        btn = Gtk.Button()
        btn.set_relief(Gtk.ReliefStyle.NONE)
        btn.get_style_context().add_class("game-cover")
        btn.get_style_context().add_class("home-list-row-btn")
        btn.set_tooltip_text(app._("tm_installing_tooltip"))
        btn.connect("clicked", lambda *_: app._set_home_nav("map"))
        self.plunderhub_row_btn = btn
        hbox = Gtk.Box(spacing=18)
        launcher = LAUNCHER_OF[game.store]
        badge = app._home_launcher_badge(launcher, 32)
        icon = Gtk.Image.new_from_pixbuf(badge) if badge is not None else app._load_launcher_icon(launcher, 32)
        icon.set_valign(Gtk.Align.CENTER)
        hbox.pack_start(icon, False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        texts.set_valign(Gtk.Align.CENTER)
        title = Gtk.Label(label=game.title, xalign=0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.get_style_context().add_class("settings-row-title")
        texts.pack_start(title, False, False, 0)
        self.status = Gtk.Label(xalign=0)
        self.status.set_width_chars(24)               # its text changes every second: no width jumps
        self.status.get_style_context().add_class("settings-status-on")
        texts.pack_start(self.status, False, False, 0)
        self.area = Gtk.DrawingArea()
        self.area.set_size_request(-1, 6)
        self.area.connect("draw", self._draw)
        texts.pack_start(self.area, False, False, 0)
        hbox.pack_start(texts, True, True, 0)
        btn.add(hbox)
        self.pack_start(btn, True, True, 0)
        self.update()
        InstallingCard.live.add(self)

    def update(self):
        dl = self.view.downloads.get(self.game.uid)
        if dl is not None:
            self.status.set_text("● " + _progress_text(self.app, dl))

    def _draw(self, widget, cr):
        dl = self.view.downloads.get(self.game.uid)
        if dl is None:
            return False
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        ctx = widget.get_style_context()
        fg = ctx.get_color(ctx.get_state())
        _rounded(cr, 0, 0, w, h, h / 2)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.15)
        cr.fill()
        if dl["frac"] > 0:
            _rounded(cr, 0, 0, max(h, w * dl["frac"]), h, h / 2)
            cr.set_source_rgb(*_accent_rgb(widget))
            cr.fill()
        return False


def _progress_text(app, dl) -> str:
    """An install's state in words, as on the tile's plate."""
    state = dl["state"]
    if state == "asked":
        return app._("tm_opening").format(launcher=dl["launcher"])
    if state == "queued":
        return app._("tm_waiting_n").format(n=dl["place"]) if dl.get("place") else app._("tm_waiting")
    text = f"{int(dl['frac'] * 100)} %"
    if dl.get("size"):
        text += "   ·   " + app._("tm_of").format(done=_fmt_gb(dl.get("got") or dl["frac"] * dl["size"]),
                                                  total=_fmt_gb(dl["size"]))
    return text


def _installing(app, nav: str) -> List[OwnedGame]:
    """The games being dug up through the map that belong on library page nav."""
    view = getattr(app, "_tm_view", None)
    if view is None:
        return []
    out = []
    for uid in list(view.queue):
        dl = view.downloads.get(uid)
        game = view.games_by_uid.get(uid)
        if dl is None or game is None or dl.get("state") == "done":
            continue
        if nav in ("all", f"l:{LAUNCHER_OF[game.store]}"):
            out.append(game)
    return out


def installing_rows(app, nav: str) -> List[Gtk.Widget]:
    """installing_cards for the list view."""
    rows = [InstallingRow(app, app._tm_view, game) for game in _installing(app, nav)]
    app._tm_lib_shown = bool(rows)
    return rows


def installing_cards(app, nav: str, w: int, h: int) -> List[Gtk.Widget]:
    """Cards for the games being installed through the Treasure Map, for the
    library page nav (All Plunder, or their launcher's own page)."""
    view = getattr(app, "_tm_view", None)
    if view is None:
        return []
    out = []
    if view.queue and (view.grid.tile_w, view.grid.tile_h) != (w, h):
        # the map not laid out yet: its covers at the library's size (the same
        # as its own - Ship's Controls' cover size)
        d = ui_scale()
        view.grid._tiles.clear()
        view.grid.tile_w, view.grid.tile_h = w, h
        view.loader.set_size(w * d, h * d)
    out = [InstallingCard(app, view, game, w, h) for game in _installing(app, nav)]
    app._tm_lib_shown = bool(out)
    return out


def _corner(app) -> float:
    """The card windows' corner: the compositor's own (Hyprland's rounding,
    KDE's...), as App._match_compositor_corners found it - a fixed radius
    left see-through corners inside its frame, or the frame cut ours."""
    return float(getattr(app, "dialog_corner_radius", None) or getattr(app, "DIALOG_CORNER_RADIUS", 20))


def _text_w(widget, cr, s, size, bold=True):
    from gi.repository import PangoCairo
    layout = PangoCairo.create_layout(cr)
    desc = widget.get_pango_context().get_font_description().copy()
    desc.set_size(int(size * Pango.SCALE))
    desc.set_weight(Pango.Weight.BOLD if bold else Pango.Weight.NORMAL)
    layout.set_font_description(desc)
    layout.set_text(s, -1)
    return layout.get_pixel_size()[0]


# --- the Dig up card ------------------------------------------------------------------

class GameCard:
    """A big card about one game. Buried: its art, name, maker, year,
    reviews, ProtonDB, a few lines about it, the room it needs against
    the room there is (in numbers), and Dig up. Aboard: the same, with
    what it takes on disk, Set sail and Uninstall. Facts that can't be
    found are left out, not guessed. run() -> "dig", "sail", "uninstall"
    or None."""

    W, HERO_H = 700, 250

    def __init__(self, view: "TreasureMap", game: OwnedGame):
        self.view, self.app, self.game = view, view.app, game
        # Ship's Controls > text size: the whole card scales with it
        self.f = max(0.7, self.app._text_factor())
        self.Wf = int(self.W * self.f)
        self.info: Optional[Dict] = None
        self.hero_pb: Optional[GdkPixbuf.Pixbuf] = None
        self.dlg: Optional[Gtk.Dialog] = None
        self.pdb = None                     # protondb.Rating, False = none, None = still asking
        self._pdb_rect = None
        self._pdb_hover = False
        self.poster_pb: Optional[GdkPixbuf.Pixbuf] = None

    def _fit_hero(self, dlg):
        """A low screen (a laptop at 125-200 %): the hero art gives up what
        the card lacks, so its buttons stay on the screen."""
        from plunderhub.gi_setup import screen_room
        room = screen_room(self.view)
        if room is None:
            return
        need = dlg.get_preferred_height_for_width(self.Wf)[1]
        if need <= room[1]:
            return
        self.HERO_H = max(110, self.HERO_H - (need - room[1]) / self.f)
        self.top.set_size_request(self.Wf, int((self.HERO_H + 22 + self.VERDICT_H + 12) * self.f))

    def run(self) -> Optional[str]:
        app, game = self.app, self.game
        dlg = self.dlg = Gtk.Dialog(transient_for=self.view.get_toplevel(), modal=True)
        dlg.set_decorated(False)
        dlg.set_resizable(False)
        visual = dlg.get_screen().get_rgba_visual()
        if visual is not None:
            dlg.set_visual(visual)
        dlg.set_app_paintable(True)
        dlg.connect("draw", self._paint_bg)
        area = dlg.get_content_area()
        area.set_border_width(0)
        aa = dlg.get_action_area()
        if aa is not None:
            aa.set_no_show_all(True)
            aa.hide()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.set_size_request(self.Wf, -1)
        self.top = Gtk.DrawingArea()
        self.top.set_size_request(self.Wf, int((self.HERO_H + 22 + self.VERDICT_H + 12) * self.f))
        self.top.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.POINTER_MOTION_MASK
                            | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.top.connect("draw", self._draw_top)
        self.top.connect("button-press-event", self._on_top_click)
        self.top.connect("motion-notify-event", self._on_top_motion)
        box.pack_start(self.top, False, False, 0)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        body.set_margin_start(32)
        body.set_margin_end(32)
        body.set_margin_top(4)
        body.set_margin_bottom(24)
        self.desc = Gtk.Label(xalign=0, yalign=0)
        self.desc.set_line_wrap(True)
        self.desc.set_max_width_chars(10)          # the card's width decides, not the text
        self.desc.set_size_request(self.Wf - 64 - 16, -1)
        self.desc.get_style_context().add_class("tm-card-desc")
        # a long one scrolls in place (about five lines show)
        self.desc_scroll = Gtk.ScrolledWindow()
        self.desc_scroll.get_style_context().add_class("settings-scroll")
        self.desc_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.desc_scroll.set_overlay_scrolling(False)
        self.desc_scroll.set_propagate_natural_height(True)
        self.desc_scroll.set_max_content_height(int(96 * self.f))
        self.desc_scroll.add(self.desc)
        self.desc_scroll.set_no_show_all(True)
        body.pack_start(self.desc_scroll, False, False, 0)
        self.room = Gtk.DrawingArea()
        self.room.set_size_request(-1, int(self.ROOM_H * self.f))
        self.room.connect("draw", self._draw_room)
        body.pack_start(self.room, False, False, 0)
        launcher = LAUNCHER_OF[game.store]
        self.choice: Optional[Dict] = None
        if not game.installed and game.store == "steam":
            body.pack_start(self._steam_options(), False, False, 0)
        if not game.installed and game.store != "steam":
            body.pack_start(self._heroic_options(), False, False, 0)
        row = Gtk.Box(spacing=10)
        answers = {1: "dig", 2: "sail", 3: "uninstall"}
        if game.installed:
            out = app._dialog_button(app._("tm_uninstall"), "danger")
            out.connect("clicked", lambda *_: dlg.response(3))
            out.set_focus_on_click(False)
            row.pack_start(out, False, False, 0)
            buttons = [app._dialog_button(app._("close_btn"), "ghost", "back", "Esc"),
                       app._dialog_button("▶  " + app._("launch_label"), "accent", "activate", "Enter")]
            codes = (Gtk.ResponseType.CANCEL, 2)
        else:
            dig = app._dialog_button(app._("tm_dig_up_with").format(launcher=launcher), "accent", "activate", "Enter")
            logo = Gtk.Image.new_from_pixbuf(app._home_launcher_badge(launcher, 20))
            inner = dig.get_child()
            inner.pack_start(logo, False, False, 0)
            inner.reorder_child(logo, 0)
            if game.store == "steam":
                dig.set_tooltip_text(app._("tm_dig_tooltip_steam"))
            else:
                dig.set_tooltip_text(app._("tm_dig_tooltip_heroic"))
            buttons = [app._dialog_button(app._("tm_not_now"), "ghost", "back", "Esc"), dig]
            codes = (Gtk.ResponseType.CANCEL, 1)
        for b, code in zip(buttons, codes):
            b.connect("clicked", lambda *_a, c=code: dlg.response(c))
            b.set_focus_on_click(False)
            b.set_valign(Gtk.Align.CENTER)
        right = Gtk.Box(spacing=10)
        for b in buttons:
            right.pack_start(b, False, False, 0)
        row.pack_end(right, False, False, 0)
        self._go_code = codes[1]
        body.pack_start(row, False, False, 0)
        box.pack_start(body, False, False, 0)
        area.pack_start(box, False, False, 0)
        dlg.connect("key-press-event", self._on_key)
        cached = self.view.card_cached(game.uid)
        if cached is not None:
            self.hero_pb = cached[1]
            GLib.idle_add(self._got, cached[0], cached[1])
        threading.Thread(target=self._fetch, daemon=True).start()
        if app.settings.get("protondb_enabled", True):
            threading.Thread(target=self._fetch_pdb, daemon=True).start()
        else:
            self.pdb = False
        self._fit_hero(dlg)
        dlg.show_all()
        buttons[1].grab_focus()
        r = dlg.run()
        dlg.destroy()
        self.dlg = None
        return answers.get(int(r)) if int(r) in answers else None

    ROOM_H = 96

    def _heroic_options(self) -> Gtk.Widget:
        """Heroic's install window's choices, filled in the way it fills
        them: the install folder, the Wine prefix, the Wine / Proton build."""
        app = self.app
        d = hold_data.heroic_defaults(self.game.title)
        versions = hold_data.heroic_wine_versions()
        default_wine = d["wine"] or hold_data.preferred_wine(versions)
        if default_wine and not any(v.get("name") == default_wine.get("name") for v in versions):
            versions.insert(0, default_wine)
        self.choice = {"path": d["install_path"], "prefix": d["prefix"], "wine": default_wine}
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)

        def label(text):
            lab = Gtk.Label(label=text, xalign=0)
            lab.get_style_context().add_class("tm-card-hint")
            return lab

        # a Flatpak Heroic sees only some folders: its own ones stay, as Heroic
        # set them (changed in Heroic's settings) - never one it can't open
        fixed = hold_data.heroic_flatpak_id() is not None

        def path_row(r, title, key, chooser_title):
            grid.attach(label(title), 0, r, 1, 1)
            if fixed:
                text = Gtk.Label(label=self.choice[key], xalign=0)
                text.set_ellipsize(Pango.EllipsizeMode.START)
                text.set_hexpand(True)
                text.set_tooltip_text(self.choice[key])
                grid.attach(text, 1, r, 1, 1)
                return
            btn = Gtk.Button()
            btn.get_style_context().add_class("sw-chip")
            btn.set_focus_on_click(False)
            inner = Gtk.Box(spacing=8)
            text = Gtk.Label(label=self.choice[key], xalign=0)
            text.set_ellipsize(Pango.EllipsizeMode.START)
            text.set_hexpand(True)
            inner.pack_start(text, True, True, 0)
            inner.pack_start(app._icon_image_at_size("folder-symbolic", 16), False, False, 0)
            btn.add(inner)
            btn.set_hexpand(True)

            def pick(*_a):
                # the portal's folder picker where there is one (Flatpak, any desktop)
                dlg = Gtk.FileChooserNative.new(chooser_title, self.dlg, Gtk.FileChooserAction.SELECT_FOLDER,
                                                None, None)
                start = Path(self.choice[key].rstrip("/"))
                while not start.exists() and start != start.parent:
                    start = start.parent
                dlg.set_current_folder(str(start))
                if dlg.run() == Gtk.ResponseType.ACCEPT and dlg.get_filename():
                    picked = dlg.get_filename()
                    if key == "prefix":
                        self.choice[key] = picked
                    else:
                        self.choice[key] = picked.rstrip("/") + "/"
                        self._refresh_room()
                    text.set_text(self.choice[key])
                dlg.destroy()
            btn.connect("clicked", pick)
            grid.attach(btn, 1, r, 1, 1)
        path_row(0, app._("tm_install_to"), "path", app._("tm_install_to"))
        path_row(1, app._("tm_wine_prefix"), "prefix", app._("tm_wine_prefix"))
        grid.attach(label(app._("tm_wine_version")), 0, 2, 1, 1)
        combo = Gtk.ComboBoxText()
        for v in versions:
            combo.append_text(v.get("name") or v.get("bin") or "?")
        idx = next((i for i, v in enumerate(versions) if v.get("name") == default_wine.get("name")), 0)
        if versions:
            combo.set_active(idx)
        combo.connect("changed", lambda c: self.choice.__setitem__("wine", versions[c.get_active()])
                      if c.get_active() >= 0 else None)
        combo.set_hexpand(True)
        grid.attach(combo, 1, 2, 1, 1)
        if fixed:
            note = label(app._("tm_flatpak_fixed_paths"))
            note.set_line_wrap(True)
            note.set_max_width_chars(60)
            grid.attach(note, 0, 3, 2, 1)
        return grid

    def _steam_options(self) -> Gtk.Widget:
        """Steam's install window's one choice: the library - every one
        Steam has, Steam's own default (its star) picked first."""
        app = self.app
        libs = hold_data.steam_library_choices()
        pick = next((c for c in libs if c["default"]), libs[0] if libs else None)
        self.choice = {"path": pick["path"] if pick else ""}
        grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        lab = Gtk.Label(label=app._("tm_install_to"), xalign=0)
        lab.get_style_context().add_class("tm-card-hint")
        grid.attach(lab, 0, 0, 1, 1)
        combo = Gtk.ComboBoxText()
        for c in libs:
            star = "  ★" if c["default"] else ""
            combo.append_text(f"{c['label']}  ·  {app._('tm_free_space').format(size=_fmt_gb(c['free']))}{star}")
        if pick is not None:
            combo.set_active(libs.index(pick))
        combo.set_hexpand(True)

        def changed(c):
            i = c.get_active()
            if 0 <= i < len(libs):
                self.choice["path"] = libs[i]["path"]
                c.set_tooltip_text(libs[i]["path"])
                self._refresh_room()
        combo.connect("changed", changed)
        if pick is not None:
            combo.set_tooltip_text(pick["path"])
        grid.attach(combo, 1, 0, 1, 1)
        return grid

    def _refresh_room(self):
        """A new install folder: its drive's room."""
        if self.info is None:
            return
        free, disk, where = hold_data._free_space([Path(self.choice["path"])])
        self.info.update(free=free, disk=disk, where=where)
        self.room.queue_draw()

    def _on_key(self, dlg, ev):
        if ev.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            dlg.response(self._go_code)
            return True
        return False

    def _fetch(self):
        cached = self.view.card_cached(self.game.uid)
        if cached is None:
            # the fast part first (art, words), then the numbers
            try:
                fast = hold_data.details(self.game, Path(self.view.opts["cache"]), slow=False)
            except Exception:
                fast = {}
            hero = None
            if fast.get("hero"):
                try:
                    hero = _load_cover_at(Path(fast["hero"]), *self.view.hero_size())
                except Exception:
                    hero = None
            GLib.idle_add(self._got, fast, hero)
            cached = (fast, hero)
        try:
            info = hold_data.details(self.game, Path(self.view.opts["cache"]))
        except Exception as e:
            logger.info(f"Treasure Map: details failed: {e}")
            info = {}
        pb = cached[1]
        # the poster: the grid's copy when it has one, else its own (the game
        # may not be on screen in the grid - a search, a scroll)
        if self.view.loader.get(self.game.uid) is None:
            try:
                path = self.view.loader._path_of(self.game)
                pw = int(132 * self.f) * d
                self.poster_pb = _load_cover_at(path, pw, int(pw * ASPECT)) if path else None
            except Exception:
                self.poster_pb = None
        GLib.idle_add(self._got, info, pb)

    def _blurred_poster(self):
        """The cover, shrunk to a few pixels: scaled up it's a soft blur."""
        if getattr(self, "_blur", None) is None:
            self._blur = None
            pb = self.view.loader.get(self.game.uid) or self.poster_pb
            if pb is not None:
                small = pb.scale_simple(12, max(1, int(12 * pb.get_height() / pb.get_width())),
                                        GdkPixbuf.InterpType.BILINEAR)
                self._blur = small.scale_simple(48, max(1, int(48 * pb.get_height() / pb.get_width())),
                                                GdkPixbuf.InterpType.BILINEAR)
        return self._blur

    def _fetch_pdb(self):
        try:
            from plunderhub.core import protondb
            found = protondb.rating(self.game.uid, self.game.title)
        except Exception as e:
            logger.info(f"Treasure Map: ProtonDB: {e}")
            found = None
        GLib.idle_add(self._got_pdb, found or False)

    def _got_pdb(self, found):
        if self.dlg is not None:
            self.pdb = found
            self.top.queue_draw()
        return False

    def _got(self, info, pb):
        if self.dlg is None:
            return False
        self.info, self.hero_pb = info, pb or self.hero_pb
        if getattr(self, "choice", None):
            self._refresh_room()
        desc = info.get("description") or ""
        self.desc.set_text(desc)
        self.desc.show()
        self.desc_scroll.set_visible(bool(desc))
        for w in (self.top, self.room):
            w.queue_draw()
        self._fit_hero(self.dlg)            # its text came in: still on the screen
        return False

    def _on_top_click(self, _w, ev):
        r = self._pdb_rect
        x, y = ev.x / self.f, ev.y / self.f
        if ev.button == 1 and r and r[0] <= x <= r[0] + r[2] and r[1] <= y <= r[1] + r[3]:
            import webbrowser
            webbrowser.open(self.pdb.url)
            return True
        return False

    def _on_top_motion(self, w, ev):
        r = self._pdb_rect
        x, y = ev.x / self.f, ev.y / self.f
        hover = bool(r and r[0] <= x <= r[0] + r[2] and r[1] <= y <= r[1] + r[3])
        if hover != self._pdb_hover:
            self._pdb_hover = hover
            win = w.get_window()
            if win is not None:
                win.set_cursor(Gdk.Cursor.new_from_name(w.get_display(), "pointer") if hover else None)
            w.queue_draw()

    def _paint_bg(self, widget, cr):
        a = widget.get_allocation()
        c = self.view.colors()
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        _rounded(cr, 0, 0, a.width, a.height, _corner(self.app))
        cr.set_source_rgba(*c["alt"], 1)
        cr.fill()
        return False

    def _pill(self, widget, cr, x, y, label, col, bold_part=None, chevron=False, hover=False):
        text = label + ("  ›" if chevron else "")
        w = _text_w(widget, cr, text, 9.5) + 24
        _rounded(cr, x, y, w, 26, 13)
        cr.set_source_rgba(*col, 0.28 if hover else 0.16)
        cr.fill()
        _pango_text(cr, widget, text, x, y + 5, w, 9.5, True, (*col, 1), align_center=True)
        return w

    def _draw_top(self, widget, cr):
        c = self.view.colors()
        accent = _accent_rgb(widget)
        f = self.f
        cr.scale(f, f)                                 # drawn at the Large size, scaled to the text size
        W = widget.get_allocated_width() / f
        hh = self.HERO_H
        d = widget.get_scale_factor()
        # the hero art, fading into the card
        cr.save()
        cr.new_path()
        cr.move_to(0, hh)
        rr = _corner(self.app) / f                   # the window's corner, in the drawing's units
        cr.line_to(0, rr)
        cr.arc(rr, rr, rr, math.pi, 1.5 * math.pi)
        cr.arc(W - rr, rr, rr, 1.5 * math.pi, 0)
        cr.line_to(W, hh)
        cr.close_path()
        cr.clip()
        if self.hero_pb is not None:
            sc = W / (self.hero_pb.get_width() / d)
            cr.scale(sc, sc)
            cr.set_source_surface(Gdk.cairo_surface_create_from_pixbuf(self.hero_pb, d, None), 0, 0)
            cr.paint()
            cr.scale(1 / sc, 1 / sc)
        elif self._blurred_poster() is not None:
            # the art still on its way: the cover, soft, in its place
            bp = self._blurred_poster()
            sc = max(W / bp.get_width(), hh / bp.get_height())
            cr.save()
            cr.translate(W / 2, hh / 2)
            cr.scale(sc, sc)
            Gdk.cairo_set_source_pixbuf(cr, bp, -bp.get_width() / 2, -bp.get_height() / 2)
            cr.get_source().set_filter(cairo.FILTER_BILINEAR)
            cr.paint()
            cr.restore()
        else:
            _title_gradient(cr, self.game.title, W, hh, 0.9)
        grad = cairo.LinearGradient(0, hh * 0.35, 0, hh)
        grad.add_color_stop_rgba(0, *c["alt"], 0)
        grad.add_color_stop_rgba(1, *c["alt"], 1)
        cr.set_source(grad)
        cr.rectangle(0, 0, W, hh)
        cr.fill()
        cr.restore()
        # the poster, over the art's edge
        px, py, pw, ph = 32, hh - 118, 132, 176
        for i, a in ((12, 0.06), (7, 0.1), (3, 0.16)):
            _rounded(cr, px - i * 0.3, py + i * 0.9, pw + i * 0.6, ph + i * 0.4, 14)
            cr.set_source_rgba(0, 0, 0, a)
            cr.fill()
        cr.save()
        _rounded(cr, px, py, pw, ph, 14)
        cr.clip()
        cr.translate(px, py)
        pb = self.view.loader.get(self.game.uid) or self.poster_pb
        if pb is not None:
            sc = pw / (pb.get_width() / d)
            cr.scale(sc, sc)
            cr.set_source_surface(Gdk.cairo_surface_create_from_pixbuf(pb, d, None), 0, 0)
            cr.paint()
        else:
            _title_gradient(cr, self.game.title, pw, ph)
        cr.restore()
        badge = self.app._home_launcher_badge(LAUNCHER_OF[self.game.store], 30)
        if badge is not None:
            cr.set_source_rgba(0, 0, 0, 0.55)
            cr.mask_surface(pixbuf_surface(badge), px + 11, py + 11.5)
            cr.set_source_surface(pixbuf_surface(badge), px + 10, py + 10)
            cr.paint()
        # name and maker, right of the poster
        tx = px + pw + 26
        tw = W - tx - 28
        # a name on two lines starts higher: the line under it stays clear of the verdicts
        title_h = _pango_layout(cr, widget, self.game.title, tw, 22, True, 2).get_pixel_size()[1]
        line_h = _pango_layout(cr, widget, "Ag", tw, 22, True).get_pixel_size()[1]
        y = hh - 46 - max(0, title_h - line_h)
        used = _pango_text(cr, widget, self.game.title, tx, y, tw, 22, True, (*c["fg"], 1), lines=2)
        y += used + 4
        info = self.info or {}
        facts = [str(info[k]) for k in ("developer", "year") if info.get(k)]
        if self.game.store != "steam":
            facts.append(self.app._("tm_via_heroic").format(store=STORE_NAMES[self.game.store]))
        if facts:
            _pango_text(cr, widget, "  ·  ".join(facts), tx, y, tw, 11.5, False, (*c["fg"], 0.62))
        # the verdicts, as pictures: Steam's reviews on a ring, ProtonDB as a medal
        y = hh + 22
        x = tx
        self._pdb_rect = None
        if self.info is None and self.pdb is None:
            _pango_text(cr, widget, self.app._("tm_looking"), tx, y + 18, tw, 11, False, (*accent, 0.8))
            return False
        if info.get("review"):
            r = int(info["review"])
            col = (0.38, 0.78, 0.98) if r >= 70 else (0.98, 0.76, 0.33) if r >= 40 else (0.96, 0.45, 0.40)
            x += self._verdict(widget, cr, x, y, col, r / 100, f"{r}%", self.app._("tm_positive"), self.app._("tm_steam_reviews")) + 12
        if self.pdb:
            tier = self.pdb.tier
            col = _hex(TIER_COLOURS.get(tier, "#7fd4c1" if tier == "native" else "#9aa7b0"))
            name = {"pending": self.app._("tm_pdb_pending"), "native": self.app._("tm_native")}.get(tier, tier.capitalize())
            w = self._verdict(widget, cr, x, y, col, None, "", name, "ProtonDB  ›", medal=True,
                              hover=self._pdb_hover)
            self._pdb_rect = (x, y, w, self.VERDICT_H)
        elif self.pdb is None:
            _pango_text(cr, widget, "ProtonDB…", x, y + 20, 140, 10.5, False, (*c["fg"], 0.45))
        return False

    VERDICT_H = 60

    def _verdict(self, widget, cr, x, y, col, frac, inner, big, small, medal=False, hover=False):
        """A tile: a ring (a share) or a medal (a tier) on the left, the word
        big in its colour, what it is under it. Returns its width."""
        c = self.view.colors()
        h = self.VERDICT_H
        w = max(_text_w(widget, cr, big, 15), _text_w(widget, cr, small, 9.5, False)) + h + 22
        _rounded(cr, x, y, w, h, 16)
        cr.set_source_rgba(*c["raised"], 1)
        cr.fill()
        if hover:
            _rounded(cr, x, y, w, h, 16)
            cr.set_source_rgba(*col, 0.12)
            cr.fill()
        cx, cy, r = x + h / 2 + 2, y + h / 2, 19
        if medal:
            glow = cairo.RadialGradient(cx, cy, r * 0.4, cx, cy, r * 1.5)
            glow.add_color_stop_rgba(0, *col, 0.45)
            glow.add_color_stop_rgba(1, *col, 0)
            cr.set_source(glow)
            cr.arc(cx, cy, r * 1.5, 0, 2 * math.pi)
            cr.fill()
            shade = cairo.LinearGradient(cx, cy - r, cx, cy + r)
            shade.add_color_stop_rgb(0, *[min(1, v * 1.25 + 0.08) for v in col])
            shade.add_color_stop_rgb(1, *[v * 0.62 for v in col])
            cr.set_source(shade)
            cr.arc(cx, cy, r, 0, 2 * math.pi)
            cr.fill()
            # a five-point star struck on it
            cr.new_path()
            for i in range(10):
                a = -math.pi / 2 + i * math.pi / 5
                rr = 9 if i % 2 == 0 else 4
                cr.line_to(cx + rr * math.cos(a), cy + rr * math.sin(a))
            cr.close_path()
            cr.set_source_rgba(1, 1, 1, 0.85)
            cr.fill()
        else:
            cr.new_path()
            cr.set_line_width(5)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.arc(cx, cy, r, 0, 2 * math.pi)
            cr.set_source_rgba(*c["fg"], 0.12)
            cr.stroke()
            cr.new_path()
            cr.arc(cx, cy, r, -math.pi / 2, -math.pi / 2 + 2 * math.pi * max(0.02, frac))
            cr.set_source_rgba(*col, 1)
            cr.stroke()
            _pango_text(cr, widget, inner, cx - r, cy - 8, 2 * r, 10, True, (*c["fg"], 1), align_center=True)
        tx = x + h + 8
        _pango_text(cr, widget, big, tx, y + 10, w - h - 12, 15, True, (*col, 1))
        _pango_text(cr, widget, small, tx, y + 33, w - h - 12, 9.5, False, (*c["fg"], 0.6))
        return w

    def _draw_room(self, widget, cr):
        cr.scale(self.f, self.f)
        return self._draw_room_at(widget, cr, widget.get_allocated_width() / self.f)

    def _draw_room_at(self, widget, cr, W):
        """Three tiles - an icon, the number big, a word: what it needs /
        what's free / what's left after (aboard: on disk / played / free) -
        and a bar of the drive: used, the game (accent; red when it
        doesn't fit), free."""
        from plunderhub.icons import get_icon_pixbuf
        c = self.view.colors()
        accent = _accent_rgb(widget)
        red = (0.96, 0.45, 0.40)
        H = self.ROOM_H
        info = self.info
        if info is None:
            _rounded(cr, 0, 0, W, H, 18)
            cr.set_source_rgba(*c["raised"], 1)
            cr.fill()
            _pango_text(cr, widget, self.app._("tm_measuring"), 20, H / 2 - 9, W - 40, 11, False, (*c["fg"], 0.6))
            return False
        g = self.game
        size, free, disk, where = (info.get("size") or 0, info.get("free") or 0, info.get("disk") or 0,
                                   info.get("where") or "")
        fg = c["fg"]
        if g.installed:
            size = g.size or size
            h, m = divmod(g.playtime, 60)
            _ = self.app._
            played = (_("playtime_hm").format(h=h, m=m) if h else _("playtime_m").format(m=m)) if g.playtime \
                else _("tm_not_yet")
            stats = [("drive-harddisk-symbolic", _("tm_on_disk"), _fmt_gb(size) if size else "–", fg),
                     ("clock-symbolic", _("tm_played"), played, fg),
                     ("drive-multidisk-symbolic", _("tm_free"), _fmt_gb(free) if disk else "–", accent)]
        else:
            fits = not size or size <= free
            _ = self.app._
            stats = [("cloud-download-symbolic", _("tm_needs"), _fmt_gb(size) if size else
                      _("tm_in_launcher").format(launcher=LAUNCHER_OF[g.store]),
                      red if not fits else fg),
                     ("drive-harddisk-symbolic", _("tm_free_now"), _fmt_gb(free) if disk else "–", fg),
                     ("drive-multidisk-symbolic", _("tm_left_after"),
                      (_fmt_gb(free - size) if fits else _("tm_wont_fit")) if size and disk else "–",
                      red if not fits else accent)]
        gap = 10
        tile_w = (W - 2 * gap) / 3
        tile_h = 62
        d = widget.get_scale_factor()
        for i, (icon, label, value, col) in enumerate(stats):
            x = i * (tile_w + gap)
            _rounded(cr, x, 0, tile_w, tile_h, 16)
            cr.set_source_rgba(*c["raised"], 1)
            cr.fill()
            cx, cy = x + 31, tile_h / 2
            cr.arc(cx, cy, 18, 0, 2 * math.pi)
            cr.set_source_rgba(*col, 0.16)
            cr.fill()
            pb = get_icon_pixbuf(icon, 18, "#%02x%02x%02x" % tuple(int(v * 255) for v in col))
            if pb is not None:
                cr.set_source_surface(pixbuf_surface(pb), cx - 9, cy - 9)
                cr.paint()
            _pango_text(cr, widget, value, x + 58, 11, tile_w - 64, 17 if len(value) < 11 else 13, True, (*col, 1))
            _pango_text(cr, widget, label, x + 58, 37, tile_w - 64, 9.5, False, (*fg, 0.6))
        if disk:
            by, bh = tile_h + 16, 10
            label_w = _text_w(widget, cr, where, 9.5, False) + 4
            bw = W - label_w - 14
            _rounded(cr, 0, by, bw, bh, bh / 2)
            cr.set_source_rgba(*fg, 0.10)
            cr.fill()
            used = (disk - free) / disk
            cr.save()
            _rounded(cr, 0, by, bw, bh, bh / 2)
            cr.clip()
            cr.rectangle(0, by, bw * used, bh)
            cr.set_source_rgba(*fg, 0.32)
            cr.fill()
            if size and not g.installed:
                grad = cairo.LinearGradient(bw * used, 0, bw * (used + size / disk), 0)
                col = red if size > free else accent
                grad.add_color_stop_rgba(0, *col, 1)
                grad.add_color_stop_rgba(1, *col, 0.75)
                cr.rectangle(bw * used, by, max(4, bw * min(1 - used, size / disk)), bh)
                cr.set_source(grad)
                cr.fill()
            cr.restore()
            _pango_text(cr, widget, where, W - label_w, by - 4, label_w, 9.5, False, (*fg, 0.55))
        return False


class UninstallCard:
    """Before a game goes overboard: its art, a red badge, the question
    short, and what happens as pictures - the room it frees, the saves
    that stay, the launcher that asks once more (Steam). The library's
    game menu asks the same."""

    W, HERO_H = 540, 150

    def __init__(self, app, parent, title: str, launcher: str, size: int, hero_pb=None, cover_pb=None,
                 game: Optional[OwnedGame] = None, cache: Optional[str] = None):
        self.app, self.parent, self.title, self.launcher, self.size = app, parent, title, launcher, size
        self.hero_pb, self.cover_pb, self.game, self.cache = hero_pb, cover_pb, game, cache
        self.f = max(0.7, app._text_factor())
        self.dlg = None

    def colors(self):
        palette = THEME_PALETTES.get(self.app.theme) or THEME_PALETTES["plunderhub"]
        return {"bg": _hex(palette["bg"]), "alt": _hex(palette["bg_alt"]), "raised": _hex(palette["bg_raised"]),
                "fg": _hex(palette["fg"])}

    def run(self) -> bool:
        app, f = self.app, self.f
        dlg = self.dlg = Gtk.Dialog(transient_for=self.parent, modal=True)
        dlg.set_decorated(False)
        dlg.set_resizable(False)
        visual = dlg.get_screen().get_rgba_visual()
        if visual is not None:
            dlg.set_visual(visual)
        dlg.set_app_paintable(True)
        dlg.connect("draw", self._paint_bg)
        area = dlg.get_content_area()
        area.set_border_width(0)
        aa = dlg.get_action_area()
        if aa is not None:
            aa.set_no_show_all(True)
            aa.hide()
        self.top = Gtk.DrawingArea()
        self.top.set_size_request(int(self.W * f), int((self.HERO_H + 150) * f))
        self.top.connect("draw", self._draw)
        area.pack_start(self.top, False, False, 0)
        row = Gtk.Box(spacing=10)
        row.set_halign(Gtk.Align.CENTER)
        row.set_margin_bottom(26)
        row.set_margin_top(4)
        keep = app._dialog_button(app._("tm_keep_it"), "ghost", "back", "Esc")
        out = app._dialog_button(app._("tm_uninstall"), "danger", "activate", "Enter")
        keep.connect("clicked", lambda *_: dlg.response(Gtk.ResponseType.CANCEL))
        out.connect("clicked", lambda *_: dlg.response(Gtk.ResponseType.OK))
        for b in (keep, out):
            b.set_focus_on_click(False)
            row.pack_start(b, False, False, 0)
        area.pack_start(row, False, False, 0)
        if self.hero_pb is None and self.game is not None and self.cache:
            threading.Thread(target=self._fetch_hero, daemon=True).start()
        dlg.show_all()
        keep.grab_focus()
        r = dlg.run()
        dlg.destroy()
        self.dlg = None
        return r == Gtk.ResponseType.OK

    def _fetch_hero(self):
        pb = None
        try:
            info = hold_data.details(self.game, Path(self.cache), slow=False)
            if info.get("hero"):
                d = ui_scale()
                pb = _load_cover_at(Path(info["hero"]), int(self.W * self.f) * d, int(self.HERO_H * self.f) * d)
        except Exception:
            pb = None

        def got():
            if self.dlg is not None and pb is not None:
                self.hero_pb = pb
                self.top.queue_draw()
            return False
        GLib.idle_add(got)

    def _paint_bg(self, widget, cr):
        a = widget.get_allocation()
        cr.set_operator(cairo.OPERATOR_SOURCE)
        cr.set_source_rgba(0, 0, 0, 0)
        cr.paint()
        cr.set_operator(cairo.OPERATOR_OVER)
        _rounded(cr, 0, 0, a.width, a.height, _corner(self.app))
        cr.set_source_rgba(*self.colors()["alt"], 1)
        cr.fill()
        return False

    def _draw(self, widget, cr):
        from plunderhub.icons import get_icon_pixbuf
        c = self.colors()
        f = self.f
        cr.scale(f, f)
        W = widget.get_allocated_width() / f
        hh = self.HERO_H
        d = widget.get_scale_factor()
        red = (0.96, 0.42, 0.40)
        accent = _accent_rgb(widget)
        cr.save()
        cr.new_path()
        cr.move_to(0, hh)
        rr = _corner(self.app) / f                   # the window's corner, in the drawing's units
        cr.line_to(0, rr)
        cr.arc(rr, rr, rr, math.pi, 1.5 * math.pi)
        cr.arc(W - rr, rr, rr, 1.5 * math.pi, 0)
        cr.line_to(W, hh)
        cr.close_path()
        cr.clip()
        if self.hero_pb is not None:
            sc = W / (self.hero_pb.get_width() / d)
            cr.scale(sc, sc)
            cr.set_source_surface(Gdk.cairo_surface_create_from_pixbuf(self.hero_pb, d, None), 0, 0)
            cr.paint()
            cr.scale(1 / sc, 1 / sc)
        else:
            _title_gradient(cr, self.title, W, hh, 0.9)
        # the sea takes it: cold, darker toward the card
        grad = cairo.LinearGradient(0, 0, 0, hh)
        grad.add_color_stop_rgba(0, *c["alt"], 0.25)
        grad.add_color_stop_rgba(1, *c["alt"], 1)
        cr.set_source(grad)
        cr.rectangle(0, 0, W, hh)
        cr.fill()
        cr.restore()
        # the red badge, half over the art
        cx, cy, r = W / 2, hh - 6, 34
        glow = cairo.RadialGradient(cx, cy, r * 0.6, cx, cy, r * 2.0)
        glow.add_color_stop_rgba(0, *red, 0.45)
        glow.add_color_stop_rgba(1, *red, 0)
        cr.set_source(glow)
        cr.arc(cx, cy, r * 2, 0, 2 * math.pi)
        cr.fill()
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.set_source_rgba(*[v * 0.55 for v in red], 1)
        cr.fill_preserve()
        cr.set_source_rgba(*red, 1)
        cr.set_line_width(2)
        cr.stroke()
        pb = get_icon_pixbuf("trash-symbolic", 30, "#ffffff")
        if pb is not None:
            cr.set_source_surface(pixbuf_surface(pb), cx - 15, cy - 15)
            cr.paint()
        y = cy + r + 16
        _pango_text(cr, widget, self.app._("tm_overboard_q"), 20, y, W - 40, 20, True, (*c["fg"], 1), align_center=True)
        y += 32
        _pango_text(cr, widget, self.title, 20, y, W - 40, 12, False, (*c["fg"], 0.65), align_center=True)
        y += 34
        chips = []
        if self.size:
            chips.append(("drive-harddisk-symbolic", self.app._("tm_frees").format(size=_fmt_gb(self.size)), accent))
        # Steam's too: its Proton prefix (compatdata) is kept (core/treasure.steam_remove_files)
        chips.append((None, self.app._("tm_keeps_prefix"), c["fg"]))
        widths = [_text_w(widget, cr, t, 11) + 54 for _i, t, _c in chips]
        x = (W - sum(widths) - 10 * (len(chips) - 1)) / 2
        for (icon, text, col), w in zip(chips, widths):
            _rounded(cr, x, y, w, 36, 18)
            cr.set_source_rgba(*col, 0.14)
            cr.fill()
            if icon:
                ipb = get_icon_pixbuf(icon, 16, "#%02x%02x%02x" % tuple(int(v * 255) for v in col))
            else:
                ipb = self.app._home_launcher_badge(self.launcher, 18)
            if ipb is not None:
                iw = logical_size(ipb)[0]
                cr.set_source_surface(pixbuf_surface(ipb), x + 16, y + 18 - iw / 2)
                cr.paint()
            _pango_text(cr, widget, text, x + 40, y + 9, w - 46, 11, True, (*col, 1))
            x += w + 10
        return False


class LeftoversCard(UninstallCard):
    """After a game went overboard, what it left in the bilge (Bilge
    cleaning found it): a row per leftover - what it is, its size, saves
    in it - and the way to Bilge cleaning. run() -> True: open it."""

    ROW_H = 46
    ICONS = {"steam_prefix": "wine-symbolic", "shader_cache": "sparkles-symbolic", "prefix": "folder-symbolic"}

    def __init__(self, app, parent, title: str, launcher: str, items, **kw):
        super().__init__(app, parent, title, launcher, 0, **kw)
        self.items = items

    def run(self) -> bool:
        app, f = self.app, self.f
        dlg = self.dlg = Gtk.Dialog(transient_for=self.parent, modal=True)
        dlg.set_decorated(False)
        dlg.set_resizable(False)
        visual = dlg.get_screen().get_rgba_visual()
        if visual is not None:
            dlg.set_visual(visual)
        dlg.set_app_paintable(True)
        dlg.connect("draw", self._paint_bg)
        area = dlg.get_content_area()
        area.set_border_width(0)
        aa = dlg.get_action_area()
        if aa is not None:
            aa.set_no_show_all(True)
            aa.hide()
        self.top = Gtk.DrawingArea()
        rows = len(self.items)
        saves = any(i.saves for i in self.items)
        self.top.set_size_request(int(self.W * f),
                                  int((self.HERO_H + 116 + rows * (self.ROW_H + 8) + (34 if saves else 0)) * f))
        self.top.connect("draw", self._draw)
        area.pack_start(self.top, False, False, 0)
        row = Gtk.Box(spacing=10)
        row.set_halign(Gtk.Align.CENTER)
        row.set_margin_bottom(26)
        row.set_margin_top(4)
        row.set_margin_start(24)
        row.set_margin_end(24)
        no = app._dialog_button(app._("tm_left_no"), "ghost", "back", "Esc")
        go = app._dialog_button(app._("tm_left_go"), "accent", "activate", "Enter")
        no.connect("clicked", lambda *_: dlg.response(Gtk.ResponseType.CANCEL))
        go.connect("clicked", lambda *_: dlg.response(Gtk.ResponseType.OK))
        for b in (no, go):
            b.set_focus_on_click(False)
            row.pack_start(b, False, False, 0)
        area.pack_start(row, False, False, 0)
        if self.hero_pb is None and self.game is not None and self.cache:
            threading.Thread(target=self._fetch_hero, daemon=True).start()
        dlg.show_all()
        go.grab_focus()
        r = dlg.run()
        dlg.destroy()
        self.dlg = None
        return r == Gtk.ResponseType.OK

    def _draw(self, widget, cr):
        from plunderhub.icons import get_icon_pixbuf
        from plunderhub.core.utils import format_bytes
        c = self.colors()
        f = self.f
        cr.scale(f, f)
        W = widget.get_allocated_width() / f
        hh = self.HERO_H
        d = widget.get_scale_factor()
        amber = (0.98, 0.74, 0.33)
        accent = _accent_rgb(widget)
        cr.save()
        cr.new_path()
        cr.move_to(0, hh)
        rr = _corner(self.app) / f
        cr.line_to(0, rr)
        cr.arc(rr, rr, rr, math.pi, 1.5 * math.pi)
        cr.arc(W - rr, rr, rr, 1.5 * math.pi, 0)
        cr.line_to(W, hh)
        cr.close_path()
        cr.clip()
        if self.hero_pb is not None:
            sc = W / (self.hero_pb.get_width() / d)
            cr.scale(sc, sc)
            cr.set_source_surface(Gdk.cairo_surface_create_from_pixbuf(self.hero_pb, d, None), 0, 0)
            cr.paint()
            cr.scale(1 / sc, 1 / sc)
        else:
            _title_gradient(cr, self.title, W, hh, 0.9)
        grad = cairo.LinearGradient(0, 0, 0, hh)
        grad.add_color_stop_rgba(0, *c["alt"], 0.35)
        grad.add_color_stop_rgba(1, *c["alt"], 1)
        cr.set_source(grad)
        cr.rectangle(0, 0, W, hh)
        cr.fill()
        cr.restore()
        # the amber badge: the bilge's own icon
        cx, cy, r = W / 2, hh - 6, 34
        glow = cairo.RadialGradient(cx, cy, r * 0.6, cx, cy, r * 2.0)
        glow.add_color_stop_rgba(0, *amber, 0.4)
        glow.add_color_stop_rgba(1, *amber, 0)
        cr.set_source(glow)
        cr.arc(cx, cy, r * 2, 0, 2 * math.pi)
        cr.fill()
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.set_source_rgba(*[v * 0.5 for v in amber], 1)
        cr.fill_preserve()
        cr.set_source_rgba(*amber, 1)
        cr.set_line_width(2)
        cr.stroke()
        pb = get_icon_pixbuf("user-trash-symbolic", 30, "#ffffff")
        if pb is not None:
            cr.set_source_surface(pixbuf_surface(pb), cx - 15, cy - 15)
            cr.paint()
        y = cy + r + 16
        _pango_text(cr, widget, self.app._("tm_left_t"), 20, y, W - 40, 20, True, (*c["fg"], 1), align_center=True)
        y += 32
        total = sum(max(i.size, 0) for i in self.items)
        _pango_text(cr, widget, self.app._("tm_left_s").format(game=self.title, size=format_bytes(total)),
                    20, y, W - 40, 12, False, (*c["fg"], 0.65), align_center=True)
        y += 36
        x0, w = 28, W - 56
        for item in self.items:
            _rounded(cr, x0, y, w, self.ROW_H, 14)
            cr.set_source_rgba(*c["raised"], 0.9)
            cr.fill()
            ipb = get_icon_pixbuf(self.ICONS.get(item.kind, "folder-symbolic"), 18,
                                  "#%02x%02x%02x" % tuple(int(v * 255) for v in accent))
            if ipb is not None:
                cr.set_source_surface(pixbuf_surface(ipb), x0 + 16, y + self.ROW_H / 2 - 9)
                cr.paint()
            label = self.app._(f"tm_left_kind_{item.kind}")
            size = format_bytes(max(item.size, 0))
            sw = _text_w(widget, cr, size, 11.5, True)
            pill = ""
            if item.saves:
                pill = self.app._("bg_pill_player")
            pw = _text_w(widget, cr, pill, 9.5, True) + 20 if pill else 0
            _pango_text(cr, widget, label, x0 + 46, y + 13, w - 46 - sw - pw - 40, 11.5, True, (*c["fg"], 0.95))
            if pill:
                px = x0 + w - 16 - sw - 14 - pw
                _rounded(cr, px, y + 12, pw, 22, 11)
                cr.set_source_rgba(*amber, 0.18)
                cr.fill()
                _pango_text(cr, widget, pill, px, y + 15.5, pw, 9.5, True, (*amber, 1), align_center=True)
            _pango_text(cr, widget, size, x0 + w - 16 - sw, y + 13, sw + 2, 11.5, True, (*accent, 1))
            y += self.ROW_H + 8
        if any(i.saves for i in self.items):
            _pango_text(cr, widget, self.app._("tm_left_saves"), 28, y + 4, W - 56, 10.5, False,
                        (*amber, 0.95), align_center=True)
        return False


def confirm_uninstall(app, parent, title: str, launcher: str, size: int, **kw) -> bool:
    return UninstallCard(app, parent, title, launcher, size, **kw).run()


# --- the window ------------------------------------------------------------------------

class TreasureMap(Gtk.Box):
    def __init__(self, app, close: Optional[Callable[[], None]], opts: Dict, embedded: bool = False):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self.embedded = embedded
        self._bg = None
        self.close = close
        self.opts = opts
        self.tab = opts.get("tab", "all")
        self.show = "all"               # all, aboard, buried
        self.collapsed = set(app.settings.get("treasure_collapsed") or [])
        self.sort = "alpha"             # alpha, recent, playtime - Aboard always first
        self.query = ""
        self.states: Dict[str, StoreState] = {}
        self.downloads: Dict[str, Dict] = {}
        self.removing: set = set()
        self._removing_since: Dict[str, Tuple[float, float]] = {}
        self.queue: List[str] = []
        self.games_by_uid: Dict[str, OwnedGame] = {}
        self.faugus_pending: Dict[str, OwnedGame] = {}     # being brought aboard Faugus, not written yet
        self.faugus_running: set = set()                   # Faugus games with a program up in their prefix
        self._fg_poll_id: Optional[int] = None
        self._signature = None
        self._tick_id: Optional[int] = None
        self._toast_until = 0.0
        self._toast = ""
        self._steam_ran_before = True       # see _steam_stop_for_work
        self._toast_sub = ""
        self._dock_uid: Optional[str] = None
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        Gtk.StyleContext.add_provider_for_screen(Gdk.Screen.get_default(), provider,
                                                 Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)
        self.loader = CoverLoader(Path(opts["cache"]), self._cover_ready)
        self.loader.library_art = self._library_cover
        self._build()
        # embedded: see-through - the library's sea and its bubbles show under it
        self.reload()
        self._watch_id = None
        self._gog_dates_asked = False

    def activate(self):
        """Shown (the sidebar row picked): read the launchers' files again,
        and watch them while it's on screen."""
        self.reload()
        if self._watch_id is None:
            self._watch_id = GLib.timeout_add_seconds(3, self._watch)
        if not self._gog_dates_asked:
            self._gog_dates_asked = True
            threading.Thread(target=self._gog_dates, daemon=True).start()

    def deactivate(self):
        if self._watch_id is not None:
            GLib.source_remove(self._watch_id)
            self._watch_id = None

    def _gog_dates(self):
        ids = [g.app_id for g in self.states.get("gog", StoreState("gog", "")).games]
        try:
            dates = hold_data.gog_release_dates(ids, Path(self.opts["cache"]))
        except Exception:
            dates = {}
        GLib.idle_add(self._got_gog_dates, dates)

    def _got_gog_dates(self, dates):
        self._gog_dates_cache = dates
        for g in self.states.get("gog", StoreState("gog", "")).games:
            g.released = dates.get(g.app_id, g.released)
        if self.sort in ("newest", "oldest"):
            self._refresh(keep_scroll=True)
        return False

    _bg_times: List[float] = []

    def _draw_page_bg(self, w, cr):
        t0 = time.perf_counter()
        self.paint_bg(cr, w, 0, 0)
        TreasureMap._bg_times.append(time.perf_counter() - t0)
        return False

    def paint_bg(self, cr, widget, W, H):
        """The library's own sea (the bubble canvas' gradient), drawn once
        per size into a surface and blitted - a gradient fill per scroll
        frame cost ~5 ms. Outside the main window: flat."""
        canvas = getattr(self.app, "bubble_canvas", None)
        if not self.embedded or canvas is None:
            cr.set_source_rgb(*self.colors()["bg"])
            cr.paint()
            return
        cw, ch = self.get_allocated_width(), self.get_allocated_height()
        pos = widget.translate_coordinates(self, 0, 0) or (0, 0)
        key = (cw, ch, self.app.theme, widget.get_scale_factor())
        if self._bg is None or self._bg[0] != key:
            d = widget.get_scale_factor()
            surf = cairo.ImageSurface(cairo.FORMAT_RGB24, cw * d, ch * d)
            surf.set_device_scale(d, d)
            c2 = cairo.Context(surf)
            c2.set_source_rgb(*self.colors()["bg"])
            c2.paint()
            Gtk.render_background(canvas.get_style_context(), c2, 0, 0, cw, ch)
            self._bg = (key, surf)
        cr.set_source_surface(self._bg[1], -pos[0], -pos[1])
        cr.paint()

    def stop(self):
        self.loader.stop()
        for src in (self._watch_id, self._tick_id):
            if src:
                GLib.source_remove(src)
        self._watch_id = self._tick_id = None

    def colors(self):
        palette = THEME_PALETTES.get(self.app.theme) or THEME_PALETTES["plunderhub"]
        return {"bg": _hex(palette["bg"]), "alt": _hex(palette["bg_alt"]), "raised": _hex(palette["bg_raised"]),
                "fg": _hex(palette["fg"]), "dim": _hex(palette["fg_dim"])}

    # building
    def _build(self):
        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        head.set_margin_start(20)
        head.set_margin_end(24)
        head.set_margin_top(4)
        title_row = Gtk.Box(spacing=12)
        title = Gtk.Label(label=self.app._("tm_title"), xalign=0)
        title.get_style_context().add_class("home-section")       # the library's own heading
        title_row.pack_start(title, False, False, 0)
        if not self.embedded:
            close_x = Gtk.Button()
            close_x.add(self.app._icon_image_at_size("window-close-symbolic", 16))
            close_x.get_style_context().add_class("about-close")
            close_x.set_valign(Gtk.Align.CENTER)
            close_x.set_focus_on_click(False)
            close_x.connect("clicked", lambda *_: self.close())
            title_row.pack_end(close_x, False, False, 0)
        head.pack_start(title_row, False, False, 0)
        sub = Gtk.Label(xalign=0)
        sub.set_line_wrap(True)
        sub.set_markup(self.app._("tm_sub"))
        sub.get_style_context().add_class("tm-sub")
        head.pack_start(sub, False, False, 0)

        bar = Gtk.Box(spacing=8)
        bar.set_margin_top(12)
        self.tab_buttons: Dict[str, Tuple[Gtk.ToggleButton, Gtk.Label]] = {}
        for key in TABS:
            chip = Gtk.ToggleButton()
            chip.get_style_context().add_class("sw-chip")
            chip.set_focus_on_click(False)
            allow_hover_relayout(chip, "its dimmed text lights up under the pointer")
            inner = Gtk.Box(spacing=7)
            if key != "all":
                inner.pack_start(self.app._load_launcher_icon(LAUNCHER_OF[key], 16), False, False, 0)
            inner.pack_start(Gtk.Label(label=self.app._("sw_filter_all") if key == "all" else STORE_NAMES[key]),
                             False, False, 0)
            count = Gtk.Label()
            count.get_style_context().add_class("home-chip-count")
            inner.pack_start(count, False, False, 0)
            chip.add(inner)
            chip.connect("toggled", self._on_tab, key)
            self.tab_buttons[key] = (chip, count)
            bar.pack_start(chip, False, False, 0)
        head.pack_start(bar, False, False, 0)
        self.notice = Gtk.Label(xalign=0)          # only when Steam's files can't be read
        self.notice.get_style_context().add_class("tm-sub")
        self.notice.set_no_show_all(True)
        self.notice.set_margin_top(8)
        head.pack_start(self.notice, False, False, 0)
        self.pack_start(head, False, False, 0)

        self.body = Gtk.Stack()
        self.body.get_style_context().add_class("tm-body")
        # no CROSSFADE: a GtkStack set to it costs ~14 ms every frame the grid scrolls
        # (PlunderHubNotes/gtk3-performance.md)
        self.body.set_transition_type(Gtk.StackTransitionType.NONE)
        self.body.set_margin_top(6)
        grid_box = Gtk.Box()
        self.grid = MapGrid(self)
        grid_box.pack_start(self.grid, True, True, 0)
        sb_wrap = Gtk.ScrolledWindow()      # only to wear the app's bright scrollbar look
        sb_wrap.get_style_context().add_class("settings-scroll")
        sb_wrap.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.NEVER)
        sb_wrap.add(Gtk.Scrollbar(orientation=Gtk.Orientation.VERTICAL, adjustment=self.grid.adj))
        grid_box.pack_start(sb_wrap, False, False, 0)
        self.body.add_named(grid_box, "grid")
        self.state_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.state_card.set_valign(Gtk.Align.CENTER)
        self.state_card.set_halign(Gtk.Align.CENTER)
        self.body.add_named(self.state_card, "state")
        self.pack_start(self.body, True, True, 0)

        # the download dock: only while something downloads
        self.dock_rev = Gtk.Revealer()
        self.dock_rev.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.dock_rev.set_transition_duration(220)
        dock = Gtk.Box(spacing=14)
        dock.get_style_context().add_class("tm-dock")
        dock.set_margin_start(26)
        dock.set_margin_end(26)
        dock.set_margin_bottom(14)
        dock.set_margin_top(6)
        dock.set_app_paintable(True)
        self.dock_art = Gtk.DrawingArea()
        self.dock_art.set_size_request(38, 57)
        self.dock_art.connect("draw", self._draw_dock_art)
        dock.pack_start(self.dock_art, False, False, 0)
        mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5)
        mid.set_valign(Gtk.Align.CENTER)
        self.dock_title = Gtk.Label(xalign=0)
        self.dock_title.get_style_context().add_class("tm-dock-title")
        self.dock_title.set_ellipsize(Pango.EllipsizeMode.END)
        self.dock_sub = Gtk.Label(xalign=0)
        self.dock_sub.get_style_context().add_class("tm-dock-sub")
        self.dock_sub.set_ellipsize(Pango.EllipsizeMode.END)
        self.dock_bar = Gtk.DrawingArea()
        self.dock_bar.set_size_request(-1, 8)
        self.dock_bar.connect("draw", self._draw_dock_bar)
        mid.pack_start(self.dock_title, False, False, 0)
        mid.pack_start(self.dock_bar, False, False, 0)
        mid.pack_start(self.dock_sub, False, False, 0)
        dock.pack_start(mid, True, True, 0)
        self.dock_next = Gtk.Label(xalign=1)
        self.dock_next.get_style_context().add_class("tm-dock-sub")
        self.dock_next.set_valign(Gtk.Align.CENTER)
        self.dock_next.set_max_width_chars(30)
        self.dock_next.set_ellipsize(Pango.EllipsizeMode.END)
        dock.pack_start(self.dock_next, False, False, 0)
        self.dock_open = Gtk.Button()
        self.dock_open.get_style_context().add_class("home-pill-ghost")
        self.dock_open.set_valign(Gtk.Align.CENTER)
        self.dock_open.set_focus_on_click(False)
        self.dock_open.connect("clicked", lambda *_: self.stop_direct(self._dock_uid)
                               if getattr(self, "_dock_direct", False) else self._open_launcher(self._dock_launcher))
        dock.pack_start(self.dock_open, False, False, 0)
        self._dock_launcher = "Heroic"
        dock.connect("draw", self._paint_dock)
        self.dock_rev.add(dock)
        self.pack_start(self.dock_rev, False, False, 0)

        self.tab_buttons[self.tab][0].set_active(True)

    def sort_label(self) -> str:
        return self.app._(dict(SORTS)[self.sort])

    def fill_sort_menu(self, box: Gtk.Box, popover: Gtk.Popover):
        """The main window's Sort by menu while the map shows: its own sorts."""
        for value, label in SORTS:
            row = self.app._home_menu_row(None, self.app._(label), checked=self.sort == value)
            row.connect("clicked", lambda _b, v=value: (popover.popdown(), self.set_sort(v)))
            box.pack_start(row, False, False, 0)

    def set_sort(self, value):
        if value != self.sort:
            self.sort = value
            self.app._update_sort_btn_label()
            self._refresh()

    def set_query(self, text: str):
        if text != self.query:
            self.query = text
            self._refresh()

    def toggle_section(self, name: str):
        """Aboard / Buried fold (open by default); kept in the settings."""
        if name in self.collapsed:
            self.collapsed.discard(name)
        else:
            self.collapsed.add(name)
        self.app.settings["treasure_collapsed"] = sorted(self.collapsed)
        try:
            from plunderhub.core.persistence import save_settings
            save_settings(self.app.settings)
        except Exception:
            pass
        self.grid._relayout()

    def open_first(self):
        """Enter in the search: the first match's card."""
        if self.grid.items:
            self.on_tile(self.grid.items[0].game)

    def _paint_dock(self, widget, cr):
        a = widget.get_allocation()
        _rounded(cr, 0, 0, a.width, a.height, 18)
        cr.set_source_rgba(*self.colors()["raised"], 1)
        cr.fill_preserve()
        cr.set_source_rgba(*_accent_rgb(widget), 0.35)
        cr.set_line_width(1)
        cr.stroke()
        return False

    # data
    def reload(self, keep_scroll: bool = False):
        hold_data.OWN_CACHE[0] = Path(self.opts["cache"])
        self._maybe_fetch_gog()
        states = hold_data.all_states()
        fg = states.get("faugus")
        if fg is not None and fg.status == "ok":
            fg.games.extend(self.faugus_pending.values())
            for g in fg.games:
                kind = g.extra.get("launcher")
                if kind and not g.cover:
                    g.cover = faugus_ui().launcher_cover(kind, Path(self.opts["cache"]), self)
        self.states = states
        self.games_by_uid = {g.uid: g for st in states.values() for g in st.games}
        self._index_library()
        # a finished download: the launchers' files decide from now on
        for uid in [u for u, dl in self.downloads.items() if dl.get("state") == "done"]:
            del self.downloads[uid]
        for key, (chip, count) in self.tab_buttons.items():
            n = sum(len(s.games) for s in states.values()) if key == "all" else len(states[key].games)
            count.set_text(str(n) if n else "")
            count.set_visible(bool(n))
        for g in states.get("gog", StoreState("gog", "")).games:
            g.released = getattr(self, "_gog_dates_cache", {}).get(g.app_id, g.released)
        self._refresh(keep_scroll=keep_scroll)

    def _maybe_fetch_gog(self):
        """Signed in to GOG but Heroic's list not written yet: GOG's own
        list, in the background (once a minute at most when it fails)."""
        if getattr(self, "_gog_fetching", False) or time.monotonic() < getattr(self, "_gog_next_try", 0):
            return
        try:
            needed = hold_data.gog_needs_owned()
        except Exception:
            needed = False
        if not needed:
            return
        self._gog_fetching = True
        self._gog_next_try = time.monotonic() + 60

        def work():
            try:
                ok = hold_data.gog_fetch_owned()
            except Exception as e:
                logger.info(f"Treasure Map: GOG list: {e}")
                ok = False
            logger.info(f"Treasure Map: GOG's own list {'fetched' if ok else 'not fetched'} (Heroic's not written yet)")

            def done():
                self._gog_fetching = False
                if ok:
                    self.reload(keep_scroll=True)
                return False
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()

    def _watch(self):
        """The launchers' files changed (a purchase, a sign-in, a finished
        install): read them again - cheap, 0.1 s."""
        paths = []
        root = hold_data.heroic_root()
        if root is not None:
            paths += [root / "store_cache" / f for f in ("legendary_library.json", "gog_library.json",
                                                          "nile_library.json")]
            paths += [root / a for st in hold_data._HEROIC.values() for a in st[3]]
            paths += [root / a for files in hold_data._INSTALLED_FILES.values() for a in files]
            # what a sign-in writes first: legendary's metadata, nile's library
            paths += [root / "legendaryConfig" / "legendary" / "metadata",
                      root / "nile_config" / "nile" / "library.json", root / "nile_store" / "library.json"]
        for r in hold_data.steam_roots():
            # appinfo too: after a sign-in Steam writes packageinfo first and
            # appinfo a moment later - read in between, it looked unreadable
            paths += [r / "appcache" / "packageinfo.vdf", r / "appcache" / "appinfo.vdf",
                      r / "config" / "loginusers.vdf", r / "steamapps"]
        sig = []
        for p in paths:
            try:
                sig.append(p.stat().st_mtime)
            except OSError:
                sig.append(0)
        sig = tuple(sig)
        unreadable = any(st.status == "unreadable" for st in self.states.values())
        if self._signature is not None and (sig != self._signature or unreadable):
            # unreadable (a file caught half written): tried again until it reads
            logger.info("Treasure Map: a launcher's files changed - reading them again")
            self.reload(keep_scroll=True)
        self._signature = sig
        return True

    def _refresh(self, keep_scroll: bool = False):
        if self.tab == "faugus":
            return self._refresh_faugus(keep_scroll)
        store = None if self.tab == "all" else self.tab
        unreadable = [s for s, st in self.states.items() if st.status == "unreadable" and store in (None, s)]
        self.notice.set_text(self.app._("tm_unreadable") if unreadable else "")
        self.notice.set_visible(bool(unreadable))
        if store is not None and self.states[store].status in ("no_launcher", "signed_out"):
            self._show_state(self.states[store])
            return
        games = [g for s, st in self.states.items() if store in (None, s) for g in st.games]
        q = self.query.strip().lower()
        if q:
            games = [g for g in games if q in g.title.lower()]
        if self.sort == "recent":
            key = lambda g: (-g.last_played, _sort_key(g))
        elif self.sort == "playtime":
            key = lambda g: (-g.playtime, _sort_key(g))
        elif self.sort == "newest":                      # not known: at the end
            key = lambda g: (not g.released, -g.released, _sort_key(g))
        elif self.sort == "oldest":
            key = lambda g: (not g.released, g.released, _sort_key(g))
        else:
            key = _sort_key
        # being dug up: aboard already, first, in the order they were started
        order = {u: i for i, u in enumerate(self.queue)
                 if self.downloads.get(u, {}).get("state") != "done"}
        coming = sorted([g for g in games if not g.installed and g.uid in order], key=lambda g: order[g.uid])
        aboard = coming + sorted([g for g in games if g.installed], key=key)
        buried = sorted([g for g in games if not g.installed and g.uid not in order], key=key)
        sections = []
        harbors = [g for g in aboard if g.extra.get("launcher")]
        aboard = [g for g in aboard if not g.extra.get("launcher")]
        if self.show in ("all", "aboard") and aboard:
            sections.append(("Aboard", self.app._("tm_aboard_note"), aboard))
        if self.show in ("all", "aboard") and harbors:
            sections.append(("Harbors", self.app._("section_harbors_note"), harbors))
        if self.show in ("all", "buried") and buried:
            sections.append(("Buried", self.app._("tm_buried_note"), buried))
        if not sections:
            self._show_empty()
            return
        self.grid.set_sections(sections, keep_scroll)
        self.body.set_visible_child_name("grid")

    def _refresh_faugus(self, keep_scroll: bool = False):
        """Faugus: its games Aboard, under them the Bring aboard card; none
        yet: the card alone, big; Faugus never opened: open it once."""
        self.notice.set_visible(False)
        st = self.states.get("faugus") or StoreState("faugus", "no_launcher")
        if st.status in ("no_launcher", "unreadable"):
            self._show_state(StoreState("faugus", "no_launcher"))
            return
        games = list(st.games)
        q = self.query.strip().lower()
        if q:
            games = [g for g in games if q in g.title.lower()]
        order = {u: i for i, u in enumerate(self.queue)}
        coming = sorted([g for g in games if not g.installed and g.uid in order], key=lambda g: order[g.uid])
        aboard = coming + sorted([g for g in games if g.installed], key=self._sort_fn())
        if not aboard and not q:
            faugus_ui().show_empty(self)
            return
        if not aboard:
            self._show_empty()
            return
        add = OwnedGame(faugus_ui().ADD_UID, "faugus", self.app._("tm_fg_bring"), True, None, "+add")
        if self._fg_poll_id is None:
            self._fg_poll_id = GLib.timeout_add_seconds(2, self._fg_poll)
            self._fg_poll()
        harbors = [g for g in aboard if g.extra.get("launcher")]
        played = [g for g in aboard if not g.extra.get("launcher")]
        sections = [("Aboard", self.app._("tm_aboard_note"), played)] if played else []
        if harbors:
            sections.append(("Harbors", self.app._("section_harbors_note"), harbors))
        self.grid.set_sections(sections + [("", "", [add])], keep_scroll)
        self.body.set_visible_child_name("grid")

    def _fg_poll(self) -> bool:
        """While the Faugus tab shows: which of its games has a program
        running in its prefix (a launcher left open after its install, a
        game) - their tiles say so, and close them on a click."""
        if self.tab != "faugus" or not self.get_mapped():
            self._fg_poll_id = None
            if self.faugus_running:
                self.faugus_running = set()
            return False
        st = self.states.get("faugus")
        games = [g for g in (st.games if st else []) if g.installed and g.extra.get("prefix")]
        by_prefix = {}
        for g in games:
            by_prefix.setdefault(os.path.normpath(g.extra["prefix"]), []).append(g.uid)
        try:
            up = hold_data_faugus().running_prefixes(set(by_prefix))
        except Exception:
            up = set()
        now = {uid for p in up for uid in by_prefix.get(p, [])}
        if now != self.faugus_running:
            changed = now ^ self.faugus_running
            self.faugus_running = now
            for uid in changed:
                self.grid.refresh_game(uid)
        return True

    def close_faugus_game(self, game: OwnedGame):
        """A running Faugus game / launcher: closed (asked first) - all that
        runs in its prefix."""
        r = self.app._card_dialog(
            self.app._("tm_fg_close_game_t").format(name=game.title), self.app._("tm_fg_close_game_s"),
            [(self.app._("tm_not_now"), Gtk.ResponseType.CANCEL, "ghost"),
             (self.app._("tm_fg_close"), Gtk.ResponseType.OK, "danger")],
            icon_name="circle-alert-symbolic", default=Gtk.ResponseType.OK, transient_for=self.get_toplevel())
        if r != Gtk.ResponseType.OK:
            return
        prefix = game.extra.get("prefix")
        self.app.closed_on_purpose.add(game.uid)
        threading.Thread(target=lambda: hold_data_faugus().stop_prefix(prefix), daemon=True).start()

    def _sort_fn(self):
        if self.sort == "playtime":
            return lambda g: (-g.playtime, _sort_key(g))
        return _sort_key

    def _show_state(self, st: StoreState):
        for child in self.state_card.get_children():
            self.state_card.remove(child)
        launcher = LAUNCHER_OF[st.store]
        name = STORE_NAMES[st.store]
        logo = self.app._load_launcher_icon(launcher, 72)
        logo.set_margin_bottom(6)
        self.state_card.pack_start(logo, False, False, 0)
        waiting = False
        if st.status == "no_launcher":
            heading = self.app._("tm_no_launcher_t").format(launcher=launcher)
            text = self.app._("tm_fg_no_launcher_s" if st.store == "faugus" else "tm_no_launcher_s").format(
                store=name, launcher=launcher)
            label = self.app._("tm_install_launcher").format(launcher=launcher)
            action = (lambda: self.app.show_shipwright(focus=launcher))
        elif _launcher_running(launcher):
            # open already: signing in, or the launcher writing its lists - the games
            # come in by themselves (the files are watched)
            waiting = True
            heading = self.app._("tm_waiting_t").format(launcher=launcher)
            text = self.app._("tm_waiting_s").format(launcher=launcher)
            label, action = None, None
        else:
            # Steam's store is Steam itself: "Sign in to Steam", not "... in Steam"
            same = name == launcher
            heading = self.app._("tm_signin_self_t" if same else "tm_signin_t").format(store=name, launcher=launcher)
            text = self.app._("tm_signin_self_s" if same else "tm_signin_s").format(store=name, launcher=launcher)
            label = self.app._("tm_open_launcher").format(launcher=launcher)
            action = (lambda: self._open_launcher(launcher))
        h = Gtk.Label(label=heading)
        h.get_style_context().add_class("tm-state-title")
        t = Gtk.Label(label=text)
        t.set_justify(Gtk.Justification.CENTER)
        t.get_style_context().add_class("tm-state-text")
        self.state_card.pack_start(h, False, False, 0)
        self.state_card.pack_start(t, False, False, 0)
        if waiting:
            spinner = Gtk.Spinner()
            spinner.set_size_request(28, 28)
            spinner.set_margin_top(12)
            spinner.start()
            self.state_card.pack_start(spinner, False, False, 0)
            # the launcher closed again without a sign-in: back to its Open button
            GLib.timeout_add_seconds(3, self._recheck_waiting, st.store)
        else:
            b = Gtk.Button(label=label)
            b.get_style_context().add_class("suggested-action")
            b.set_focus_on_click(False)
            b.set_halign(Gtk.Align.CENTER)
            b.set_margin_top(10)
            b.connect("clicked", lambda *_: action())
            self.state_card.pack_start(b, False, False, 0)
        self._state_shown = (st.store, waiting)
        self.state_card.show_all()
        self.body.set_visible_child_name("state")

    def _show_empty(self):
        for child in self.state_card.get_children():
            self.state_card.remove(child)
        q = self.query.strip()
        h = Gtk.Label(label=self.app._("tm_no_match").format(q=q) if q else self.app._("tm_nothing"))
        h.get_style_context().add_class("tm-state-title")
        self.state_card.pack_start(h, False, False, 0)
        self.state_card.show_all()
        self.body.set_visible_child_name("state")

    # controls
    def _select_tab(self, key):
        self.tab_buttons[key][0].set_active(True)

    def _on_tab(self, chip, key):
        if not chip.get_active():
            if self.tab == key:
                chip.set_active(True)
            return
        self.tab = key
        for k, (other, _c) in self.tab_buttons.items():
            if k != key and other.get_active():
                other.set_active(False)
        if self.states:
            self._refresh()

    def on_key(self, ev) -> bool:
        """Page Up/Down scroll; on the tiles Enter / Space open the card,
        Home / End go to the first / last."""
        if self.grid.has_focus():
            if ev.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter, Gdk.KEY_space):
                return self.grid.kb_activate()
            if ev.keyval in (Gdk.KEY_Home, Gdk.KEY_End) and self.grid.items:
                self.grid._kb_to(self.grid.items[-1 if ev.keyval == Gdk.KEY_End else 0])
                return True
        if ev.keyval in (Gdk.KEY_Page_Down, Gdk.KEY_Page_Up):
            self.grid.scroll_by(self.grid.adj.get_page_size() * 0.9 * (1 if ev.keyval == Gdk.KEY_Page_Down else -1))
            return True
        return False

    def _recheck_waiting(self, store: str) -> bool:
        """While "Waiting for <launcher>" shows: if the launcher was closed
        without signing in, the card goes back to its Open button."""
        shown = getattr(self, "_state_shown", None)
        if shown != (store, True) or not self.get_mapped():
            return False
        if _launcher_running(LAUNCHER_OF[store]):
            return True
        self._refresh(keep_scroll=True)
        return False

    def _open_launcher(self, launcher):
        section = next((s for s in getattr(self.app, "_cached_launchers", []) or [] if s.get("name") == launcher), None)
        cmd = section.get("cmd") if section else None
        if not cmd:
            # not switched on in Ship's Controls > Library: found the scanner's way,
            # switched on, then opened; not installed at all: Shipwright offers it
            from plunderhub.core.scan_paths import LAUNCHER_FLATPAK_IDS, resolve_launcher_invocation
            from plunderhub.launchers.registry import LAUNCHER_REGISTRY
            ldef = next((d for d in LAUNCHER_REGISTRY if d.name == launcher), None)
            cmd = resolve_launcher_invocation(ldef.binary, LAUNCHER_FLATPAK_IDS.get(launcher, [])) if ldef else None
            if not cmd:
                logger.info(f"Treasure Map: {launcher} isn't installed - Shipwright offers it")
                self.app.show_shipwright(focus=launcher)
                return
            if launcher not in self.app.enabled_launchers:
                from plunderhub.ui.launcher_tasks import _set_enabled
                logger.info(f"Treasure Map: {launcher} switched on in the library, to open it")
                _set_enabled(self.app, launcher, True)
        logger.info(f"Treasure Map: opening {launcher}")
        self.app.request_launch_launcher(cmd, launcher)

    def _cover_ready(self, uid: str):
        self.grid.cover_ready(uid)
        for card in list(InstallingCard.live):          # the library's installing cards
            if card.game.uid == uid and card.get_mapped():
                card.area.queue_draw()

    # --- the cards' art and words, fetched before they're opened ---------------------

    def hero_size(self):
        f = max(0.7, self.app._text_factor())
        d = ui_scale()
        return int(GameCard.W * f) * d, int(GameCard.HERO_H * f) * d

    def prefetch(self, game: OwnedGame, done: Optional[Callable[[], None]] = None):
        """game's card art (hero) and words, in the background, kept for
        when its card opens; done() on the main thread when they're in."""
        cache = self.__dict__.setdefault("_card_cache", OrderedDict())
        busy = self.__dict__.setdefault("_card_busy", {})
        if game.uid in cache:
            if done:
                done()
            return
        if game.uid in busy:
            if done:
                busy[game.uid].append(done)
            return
        busy[game.uid] = [done] if done else []
        size = self.hero_size()

        def work():
            try:
                info = hold_data.details(game, Path(self.opts["cache"]), slow=False)
            except Exception as e:
                logger.info(f"Treasure Map: details failed: {e}")
                info = {}
            pb = None
            if info.get("hero"):
                try:
                    pb = _load_cover_at(Path(info["hero"]), *size)
                except Exception:
                    pb = None

            def put():
                cache[game.uid] = (info, pb, size)
                while len(cache) > 24:
                    cache.popitem(last=False)
                for cb in busy.pop(game.uid, []):
                    cb()
                return False
            GLib.idle_add(put)
        threading.Thread(target=work, daemon=True).start()

    def card_cached(self, uid: str):
        """(info, hero pixbuf) fetched before at the card's size, or None."""
        got = self.__dict__.get("_card_cache", {}).get(uid)
        if got is None or got[2] != self.hero_size():
            return None
        return got[0], got[1]

    # tiles
    def on_tile(self, game: OwnedGame):
        if game.uid == faugus_ui().ADD_UID:
            faugus_ui().open_add(self)
            return
        if game.uid in self.downloads and self.downloads[game.uid]["state"] != "done":
            win = self.downloads[game.uid].get("window")
            if win is not None and not win.closed:
                win.show()          # a launcher's install window, hidden: back
            return
        if game.store == "faugus":
            if game.installed:
                self.grid._set_hover(None)
                if game.uid in self.faugus_running:
                    self.close_faugus_game(game)
                else:
                    self.set_sail(game)
            return
        if game.uid in self.removing:
            return
        self.grid._set_hover(None)
        if self.card_cached(game.uid) is None:
            # opened at once it showed its art popping in a moment later:
            # a short wait for it (a slow network: opened anyway)
            state = {"open": False}

            def open_now():
                if not state["open"]:
                    state["open"] = True
                    self._open_card(game)
                return False
            self.prefetch(game, done=lambda: GLib.idle_add(open_now))
            GLib.timeout_add(600, open_now)
            return
        self._open_card(game)

    def _open_card(self, game: OwnedGame):
        card = GameCard(self, game)
        answer = card.run()
        if answer == "dig" and card.choice and game.store != "steam":
            self.start_heroic_install(game, card.choice, card.info or {})
        elif answer == "dig" and card.choice and game.store == "steam":
            self.start_steam_install(game, card.choice["path"], card.info or {})
        elif answer == "dig":
            self.start_download(game)
        elif answer == "sail":
            self.set_sail(game)
        elif answer == "uninstall":
            if confirm_uninstall(self.app, self.get_toplevel(), game.title, LAUNCHER_OF[game.store], game.size,
                                 hero_pb=card.hero_pb, game=game, cache=self.opts["cache"]):
                self.start_uninstall(game)

    # --- hand it to the launcher, then follow its own files --------------------------

    def start_uninstall(self, game: OwnedGame):
        if game.store == "steam":
            # Steam's own steps without its window: closed, the files gone,
            # started again (in the tray) only if it ran before or more of
            # our Steam work is waiting for it
            if not self._steam_stop_for_work():
                return
            logger.info(f"Treasure Map: uninstall {game.title}: Steam's files ({game.app_id})")
            self.removing.add(game.uid)
            self._removing_since[game.uid] = (time.monotonic(), 3600)
            self.grid.refresh_game(game.uid)

            def work():
                ok, why = hold_data.steam_remove_files(game.app_id)

                def done():
                    others = any(self.games_by_uid.get(u) is not None and self.games_by_uid[u].store == "steam"
                                 and self.downloads.get(u, {}).get("state") != "done" for u in self.queue)
                    if self._steam_ran_before or others:
                        self._steam_start_silent()
                    if not ok:
                        self.removing.discard(game.uid)
                        self.grid.refresh_game(game.uid)
                        self.app._card_dialog(
                            self.app._("tm_uninstall_failed").format(game=game.title), why,
                            [(self.app._("dialog_ok"), Gtk.ResponseType.CLOSE, "ghost")],
                            icon_name="circle-alert-symbolic", transient_for=self.get_toplevel())
                    return False
                GLib.idle_add(done)
            threading.Thread(target=work, daemon=True).start()
            self._ensure_tick()
            return
        else:
            # Heroic takes no uninstall from outside: its own steps, done here
            # (core/treasure.heroic_uninstall) - with Heroic closed meanwhile
            if self.app._launcher_is_running("Heroic"):
                r = self.app._card_dialog(
                    self.app._("tm_heroic_close_t"), self.app._("tm_heroic_close_s"),
                    [(self.app._("tm_not_now"), Gtk.ResponseType.CANCEL, "ghost"),
                     (self.app._("tm_close_heroic"), Gtk.ResponseType.OK, "accent")],
                    icon_name="circle-alert-symbolic", default=Gtk.ResponseType.OK,
                    transient_for=self.get_toplevel())
                if r != Gtk.ResponseType.OK or not self.app.close_launcher_asking("Heroic", self.get_toplevel()):
                    return
            logger.info(f"Treasure Map: uninstall {game.title}: Heroic's steps ({game.store})")
            self.removing.add(game.uid)
            self._removing_since[game.uid] = (time.monotonic(), 3600)
            self.grid.refresh_game(game.uid)

            def work():
                ok, why = hold_data.heroic_uninstall(game.store, game.app_id, game.title)

                def done():
                    if not ok:
                        self.removing.discard(game.uid)
                        self.grid.refresh_game(game.uid)
                        self.app._card_dialog(
                            self.app._("tm_uninstall_failed").format(game=game.title), why,
                            [(self.app._("dialog_ok"), Gtk.ResponseType.CLOSE, "ghost")],
                            icon_name="circle-alert-symbolic", transient_for=self.get_toplevel())
                    return False
                GLib.idle_add(done)
            threading.Thread(target=work, daemon=True).start()
            self._ensure_tick()
            return
        self.removing.add(game.uid)
        self._removing_since[game.uid] = (time.monotonic(), limit)
        self.grid.refresh_game(game.uid)
        self._ensure_tick()

    def start_heroic_install(self, game: OwnedGame, choice: Dict, info: Dict):
        """Installs a Heroic game the way Heroic does, without Heroic
        (core/treasure.heroic_install) - Heroic closed meanwhile: it would
        write its old state over the new entries."""
        if self.app._launcher_is_running("Heroic"):
            r = self.app._card_dialog(
                self.app._("tm_heroic_close_t"), self.app._("tm_heroic_close_s"),
                [(self.app._("tm_not_now"), Gtk.ResponseType.CANCEL, "ghost"),
                 (self.app._("tm_close_heroic"), Gtk.ResponseType.OK, "accent")],
                icon_name="circle-alert-symbolic", default=Gtk.ResponseType.OK,
                transient_for=self.get_toplevel())
            if r != Gtk.ResponseType.OK or not self.app.close_launcher_asking("Heroic", self.get_toplevel()):
                return
        size = int(info.get("download") or info.get("size") or 0)
        dl = {"frac": 0.0, "state": "downloading", "size": size, "speed": 0, "rate": 0, "place": 0,
              "launcher": "Heroic", "t0": time.monotonic(), "wall0": time.time(), "last": None, "bytes": 0,
              "direct": True, "got": 0}
        self.downloads[game.uid] = dl
        self.queue.append(game.uid)
        self._refresh(keep_scroll=True)                 # up into Aboard
        logger.info(f"Treasure Map: install {game.title}: Heroic's steps -> {choice['path']}")

        def progress(pct, got, speed):
            def put():
                dl["frac"] = max(dl["frac"], min(1.0, pct / 100))
                if got:
                    dl["got"] = got
                if speed:
                    dl["rate"] = speed
                if not dl["size"] and pct > 1 and got:
                    dl["size"] = int(got * 100 / pct)
                return False
            GLib.idle_add(put)

        def work():
            ok, why = hold_data.heroic_install(
                game.store, game.app_id, game.title, choice["path"], choice["prefix"], choice["wine"],
                on_progress=progress, on_proc=lambda p: dl.__setitem__("proc", p),
                lang=info.get("lang") or "en-US")

            def done():
                if game.uid in self.queue:
                    self.queue.remove(game.uid)
                if ok:
                    dl["state"] = "done"
                    self._toast = self.app._("tm_toast_aboard").format(game=game.title)
                    self._toast_sub = self.app._("tm_toast_aboard_sub")
                    self._toast_until = time.monotonic() + 5
                    self.reload(keep_scroll=True)
                    if hasattr(self.app, "reload"):
                        self.app.reload()           # the library shows it too
                else:
                    self.downloads.pop(game.uid, None)
                    self._refresh(keep_scroll=True)     # back to Buried
                    if not dl.get("stopped"):
                        self.app._card_dialog(
                            self.app._("tm_install_failed").format(game=game.title), why,
                            [(self.app._("dialog_ok"), Gtk.ResponseType.CLOSE, "ghost")],
                            icon_name="circle-alert-symbolic", transient_for=self.get_toplevel())
                self._update_dock()
                return False
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()
        self._update_dock()
        self._ensure_tick()

    def stop_direct(self, uid: str):
        """Stop on the dock: the tool's whole process group goes."""
        dl = self.downloads.get(uid)
        if dl is not None and dl.get("job") is not None:      # a Faugus install
            job = dl["job"]
            if dl.get("phase") in ("signin", "close_it"):
                # its window is what it waits for: closed, the install finishes as it should
                threading.Thread(target=lambda: hold_data_faugus().stop_prefix(job.req.prefix), daemon=True).start()
                return
            dl["stopped"] = True
            job.cancel()
            return
        proc = dl.get("proc") if dl else None
        if proc is not None and proc.poll() is None:
            import os as _os
            import signal
            dl["stopped"] = True
            try:
                _os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                proc.terminate()

    def _index_library(self):
        """Owned game -> its library uid: the same card art everywhere."""
        steam, heroic, faugus = set(), {}, set()
        for section in getattr(self.app, "_cached_launchers", []) or []:
            for g in section.get("games") or []:
                if g.uid.startswith("steam:"):
                    steam.add(g.uid)
                elif g.uid.startswith("heroic:"):
                    heroic[g.uid.split(":", 2)[-1]] = g.uid
                elif g.uid.startswith("faugus:"):
                    faugus.add(g.uid)
        index = {}
        for g in self.games_by_uid.values():
            if not g.installed:
                continue
            uid = f"steam:{g.app_id}" if g.store == "steam" else g.uid if g.store == "faugus" else heroic.get(g.app_id)
            if uid in steam or uid in faugus or (uid and uid.startswith("heroic:")):
                index[g.uid] = uid
        self._library_uid = index

    def _library_cover(self, game: OwnedGame) -> Optional[Path]:
        uid = getattr(self, "_library_uid", {}).get(game.uid)
        return self.app._find_cached_artwork(self.app.covers_dir, uid) if uid else None

    def library_art_changed(self, library_uid: str):
        """A cover changed in the library, here too at once."""
        self._index_library()
        for uid in [o for o, u in self._library_uid.items() if u == library_uid]:
            self.loader.forget(uid)
            self._cover_ready(uid)

    def library_game(self, game: OwnedGame):
        """The library's own entry for an owned game (Steam: steam:<appid>,
        Heroic: heroic:...:<app_name>), None before a scan has it."""
        launcher = LAUNCHER_OF[game.store]
        for section in getattr(self.app, "_cached_launchers", []) or []:
            if section.get("name") != launcher:
                continue
            for g in section.get("games") or []:
                if g.uid == f"steam:{game.app_id}" or (launcher == "Heroic" and g.uid.endswith(":" + game.app_id)) \
                        or (launcher == "Faugus" and g.uid == game.uid):
                    return g
        return None

    def set_sail(self, game: OwnedGame):
        """Set sail on an Aboard game: the library's own launch (its launch
        options, the ask-first setting, playtime - all as on its card)."""
        g = self.library_game(game)
        if g is None:
            self.app._card_dialog(self.app._("tm_not_in_library").format(game=game.title), None,
                                  [(self.app._("dialog_ok"), Gtk.ResponseType.CLOSE, "ghost")],
                                  icon_name="clock-symbolic", transient_for=self.get_toplevel())
            return
        self.app.request_launch(g.launch_cmd, g.title, g.env, g.uid, g.install_dir)

    def _steam_quietly(self, uri: str):
        """Steam not running: start it in the tray first (-silent), the link
        once it's up - a link alone opened Steam's whole window."""
        if self.app._launcher_is_running("Steam"):
            hold_data.open_uri(uri)
            return
        section = next((s for s in getattr(self.app, "_cached_launchers", []) or [] if s.get("name") == "Steam"), None)
        cmd = list(section["cmd"]) + ["-silent"] if section and section.get("cmd") else None
        if not cmd:
            hold_data.open_uri(uri)
            return
        import subprocess
        try:
            subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            hold_data.open_uri(uri)
            return
        state = {"n": 0, "up": 0}

        def wait():
            state["n"] += 1
            if self.app._launcher_is_running("Steam"):
                state["up"] += 1
            if state["up"] >= 6 or state["n"] > 90:      # a few seconds after it's up: it takes links then
                hold_data.open_uri(uri)
                return False
            return True
        GLib.timeout_add_seconds(1, wait)

    # --- Steam, driven from here (in the tray, its windows never shown) ---------------
    # Steam reads the manifests only while it starts: it's closed, the
    # manifest written (or the game's files removed), then started again in
    # the tray. Steam is left as found: started again only if it ran before,
    # and closed at the end if PlunderHub started it (the user's wish).

    def _steam_busy(self) -> bool:
        """Steam work of ours still going: installs being followed, removals."""
        return any(self.games_by_uid.get(u) is not None and self.games_by_uid[u].store == "steam"
                   and self.downloads.get(u, {}).get("state") != "done" for u in self.queue) \
            or any(u.startswith("steam:") for u in self.removing)

    def _steam_stop_for_work(self) -> bool:
        """Notes whether Steam ran before our work began, then closes it
        (asked first when one of its games runs). False: it stays open."""
        running = self.app._launcher_is_running("Steam")
        if not self._steam_busy():
            self._steam_ran_before = running
        if running and not self.app.close_launcher_asking("Steam", self.get_toplevel()):
            return False
        return True

    def _steam_start_silent(self) -> bool:
        section = next((s for s in getattr(self.app, "_cached_launchers", []) or [] if s.get("name") == "Steam"), None)
        cmd = list(section["cmd"]) + ["-silent"] if section and section.get("cmd") else ["steam", "-silent"]
        import subprocess
        try:
            subprocess.Popen(cmd, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as e:
            logger.info(f"Treasure Map: Steam didn't start: {e}")
            return False
        return True

    def steam_held(self) -> bool:
        """Steam is up only for our work (or being closed after it): the
        new game question waits, Steam needs no restart of its own then."""
        return self._steam_busy() or getattr(self, "_steam_closing", False)

    def _ask_after_steam_work(self):
        self.app._ask_about_new_games(list(self.app.game_index.values()))

    def _watch_steam_closing(self) -> bool:
        self._steam_close_wait = getattr(self, "_steam_close_wait", 0) + 1
        if self.app._launcher_is_running("Steam") and self._steam_close_wait < 60:
            return True
        self._steam_closing = False
        self._steam_close_wait = 0
        self._ask_after_steam_work()
        return False

    def _steam_work_ended(self):
        """The last Steam install / removal of ours ended: a Steam we started
        is closed again (quietly - unless one of its games runs now)."""
        if self._steam_busy():
            return
        if getattr(self, "_steam_ran_before", True):
            self._ask_after_steam_work()
            return
        self._steam_ran_before = True            # once
        app = self.app
        playing = any(g is not None and g.launcher == "Steam" for g in
                      (app.game_index.get(u) for u in set(app.running_procs) | set(app.detected_running)))
        if playing or not app._launcher_is_running("Steam"):
            self._ask_after_steam_work()
            return
        self._steam_closing = True
        GLib.timeout_add_seconds(1, self._watch_steam_closing)
        from plunderhub.core.launch_options import steam_shutdown_invocations
        import subprocess
        for inv in steam_shutdown_invocations() or [["steam"]]:
            try:
                subprocess.Popen([*inv, "-shutdown"], start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                pass
        logger.info("Treasure Map: Steam closed again (it wasn't running before)")

    def start_steam_install(self, game: OwnedGame, library: str, info: Dict):
        if not self._steam_stop_for_work():
            return
        ok, why = hold_data.steam_queue_install(game.app_id, library)
        logger.info(f"Treasure Map: install {game.title}: manifest in {library} ({'ok' if ok else why})")
        if not ok:
            self.app._card_dialog(self.app._("tm_install_failed").format(game=game.title), why,
                                  [(self.app._("dialog_ok"), Gtk.ResponseType.CLOSE, "ghost")],
                                  icon_name="circle-alert-symbolic", transient_for=self.get_toplevel())
            if self._steam_ran_before:
                self._steam_start_silent()
            return
        self.downloads[game.uid] = {"frac": 0.0, "state": "asked", "size": int(info.get("size") or 0),
                                    "speed": 0, "rate": 0, "place": 0, "launcher": "Steam",
                                    "t0": time.monotonic(), "wall0": time.time(), "last": None, "bytes": 0}
        self.queue.append(game.uid)
        self._steam_start_silent()
        self._refresh(keep_scroll=True)                 # up into Aboard
        self._update_dock()
        self._redraw_downloads()
        self._ensure_tick()

    def start_download(self, game: OwnedGame):
        """Without the card's options: the launcher's own install window."""
        launcher = LAUNCHER_OF[game.store]
        uri = ("steam://install/" + game.app_id if game.store == "steam"
               else f"heroic://launch?appName={game.app_id}&runner={game.runner}")
        logger.info(f"Treasure Map: install {game.title}: {uri}")
        if not hold_data.open_uri(uri):
            self.app._card_dialog(self.app._("tm_no_answer").format(launcher=launcher), self.app._("tm_no_answer_sub"),
                                  [("OK", Gtk.ResponseType.CLOSE, "ghost")], icon_name="circle-alert-symbolic",
                                  transient_for=self.get_toplevel())
            return
        self.downloads[game.uid] = {"frac": 0.0, "state": "asked", "size": 0, "speed": 0, "rate": 0,
                                    "place": 0, "launcher": launcher, "t0": time.monotonic(),
                                    "wall0": time.time(), "last": None, "bytes": 0}
        self.queue.append(game.uid)
        self._refresh(keep_scroll=True)                 # up into Aboard
        self._update_dock()
        self._redraw_downloads()
        self._ensure_tick()

    def _ensure_tick(self):
        if self._tick_id is None:
            self._tick_id = GLib.timeout_add(1000, self._tick)

    def _tick(self):
        now = time.monotonic()
        changed = False
        for uid in list(self.queue):
            game = self.games_by_uid.get(uid)
            dl = self.downloads[uid]
            if game is None:
                self.queue.remove(uid)
                continue
            if dl.get("direct"):
                continue
            if game.store == "steam":
                m = hold_data.steam_manifest(game.app_id)
                if m is None:
                    if now - dl["t0"] > 300:         # the install window was closed: nothing came
                        self._give_up(uid, self.app._("tm_gave_up").format(game=game.title, launcher="Steam"))
                        changed = True
                    continue
                # Steam keeps the manifest's byte counts at 0 while it downloads;
                # its content_log says which part runs (shader pre-caching first,
                # then the game's files, then the commit) and how big it is, and
                # the part's own folder shows how much has landed
                apps = Path(m["lib"]) / "steamapps"
                p = hold_data.steam_phase(game.app_id, dl["wall0"]) or {}
                phase = p.get("phase") or "game"
                if phase != dl.get("phase"):
                    dl["last"], dl["rate"], dl["bytes"] = None, 0, 0     # a new part: its own count
                dl["phase"] = phase
                if phase == "shaders":
                    folders = [apps / "shadercache" / game.app_id / "downloads"]
                else:
                    folders = [apps / "downloading" / game.app_id] + \
                        ([apps / "common" / m["installdir"]] if m.get("installdir") else [])
                self._measure_folder(uid, folders)
                size = p.get("download") or m["BytesToDownload"] or m["SizeOnDisk"]
                stage = p.get("stage") or m["BytesToStage"] or size
                got = m["BytesDownloaded"] if phase == "game" and m["BytesToDownload"] == size else 0
                if stage and dl["bytes"]:
                    got = max(got, int(size * min(1.0, dl["bytes"] / stage)))
                dl["size"] = size
                self._step(dl, got, now)
                if m["StateFlags"] == 4 and phase in ("game", "between", "commit") and \
                        m["BytesStaged"] >= m["BytesToStage"] and (phase != "game" or got >= size):
                    self._landed(uid, game, m["SizeOnDisk"])
                    changed = True
                else:
                    dl["state"] = "installing" if phase == "commit" else "downloading"
            else:
                h = hold_data.heroic_download(game.app_id)
                if h is None or h["added"] < dl["wall0"] - 10:
                    if now - dl["t0"] > 600:
                        self._give_up(uid, self.app._("tm_gave_up").format(game=game.title, launcher="Heroic"))
                        changed = True
                    continue
                if h["where"] == "finished":
                    if h["status"] == "done":
                        self._landed(uid, game, h["size"])
                    else:
                        self._give_up(uid, self.app._("tm_heroic_stopped").format(game=game.title,
                                                                                  why=h["status"] or "cancelled"))
                    changed = True
                    continue
                dl["size"] = h["size"]
                if h["status"] in ("queued", "") and dl["state"] == "asked":
                    dl["state"] = "queued"
                else:
                    dl["state"] = "downloading"
                    self._measure_folder(uid, h["target"])
                    self._step(dl, dl["bytes"], now)
        for uid in list(self.removing):
            game = self.games_by_uid.get(uid)
            since, limit = self._removing_since.get(uid, (now, 0))
            gone = False
            if game is not None and game.store == "steam":
                gone = hold_data.steam_manifest(game.app_id) is None
            elif game is not None:
                st = hold_data.heroic_state(game.store)
                gone = not any(g.app_id == game.app_id and g.installed for g in st.games)
            if gone:
                self.removing.discard(uid)
                self.downloads.pop(uid, None)
                if game.store == "steam":
                    GLib.idle_add(lambda: self._steam_work_ended() and False)
                self._look_for_leftovers(game)
                self._toast = self.app._("tm_toast_overboard").format(game=game.title)
                self._toast_sub = self.app._("tm_toast_overboard_sub")
                self._toast_until = now + 5
                self.reload(keep_scroll=True)
                if hasattr(self.app, "reload"):
                    self.app.reload()           # gone from the library too
                changed = True
            elif now - since > limit:
                self.removing.discard(uid)        # cancelled in the launcher
                self.grid.refresh_game(uid)
        self._update_dock()
        self._redraw_downloads()
        if not self.queue and not self.removing and now > self._toast_until:
            self.dock_rev.set_reveal_child(False)
            self._tick_id = None
            return False
        return True

    def _step(self, dl, got, now):
        if dl["size"]:
            dl["frac"] = max(0.0, min(1.0, got / dl["size"]))
        last = dl.get("last")
        if last is not None and now > last[0]:
            inst = max(0.0, (got - last[1]) / (now - last[0]))
            dl["rate"] = inst if not dl["rate"] else dl["rate"] * 0.6 + inst * 0.4
        dl["last"] = (now, got)
        dl["got"] = got

    def _measure_folder(self, uid, target):
        """Heroic says nothing about bytes: the game's folder, growing - read
        off the main thread, used on the next tick."""
        dl = self.downloads.get(uid)
        if dl is None or dl.get("measuring"):
            return
        dl["measuring"] = True

        def work():
            targets = target if isinstance(target, list) else [target]
            n = max([hold_data.folder_bytes(t) for t in targets] or [0])

            def put():
                dl["bytes"] = n
                dl["measuring"] = False
                return False
            GLib.idle_add(put)
        threading.Thread(target=work, daemon=True).start()

    def _look_for_leftovers(self, game: OwnedGame):
        """Bilge cleaning looks again after an uninstall; what this game left
        (its Proton/Wine prefix, shader cache - never a Proton build) is
        shown, with the way to Bilge cleaning."""
        app = self.app
        hero = (self.card_cached(game.uid) or (None, None))[1]

        def work():
            from plunderhub.ui import bilge_page
            try:
                found = bilge_page.real_scan(app)
            except Exception as e:
                logger.info(f"Treasure Map: bilge look: {e}")
                return
            if game.store == "steam":
                mine = [i for i in found.items if i.kind in ("steam_prefix", "shader_cache")
                        and i.appid == game.app_id]
            else:
                prefix = hold_data.heroic_game_prefix(game.app_id)
                real = None
                try:
                    real = prefix.resolve() if prefix else None
                except OSError:
                    pass
                names = {hold_data._sanitize(game.title).lower()}
                mine = []
                for i in found.items:
                    if i.kind != "prefix" or i.launcher != "Heroic":
                        continue
                    try:
                        ip = i.path.resolve()
                    except OSError:
                        ip = i.path
                    if (real is not None and ip == real) or i.path.name.lower() in names:
                        mine.append(i)
            if mine:
                GLib.idle_add(show, mine)

        def show(items):
            parent = self.get_toplevel()
            if LeftoversCard(app, parent, game.title, LAUNCHER_OF[game.store], items, hero_pb=hero,
                             game=game, cache=self.opts.get("cache")).run():
                app.show_shipwright(focus="bilge", parent=parent)
            return False
        threading.Thread(target=work, daemon=True).start()

    def _library_changed(self):
        """The library's page shows the games being installed: brought up
        to date (a finished one is a real card after the launchers' rescan)."""
        if hasattr(self.app, "reload"):
            self.app.reload()

    def _landed(self, uid, game, size):
        GLib.idle_add(lambda: self._library_changed() and False)
        if game.store == "steam":
            GLib.idle_add(lambda: self._steam_work_ended() and False)
        self.queue.remove(uid)
        self.downloads[uid]["state"] = "done"
        game.installed = True
        game.size = size or game.size
        self._toast = self.app._("tm_toast_aboard").format(game=game.title)
        self._toast_sub = self.app._("tm_toast_aboard_sub")
        self._toast_until = time.monotonic() + 5
        self.reload(keep_scroll=True)

    def _give_up(self, uid, why):
        if uid.startswith("steam:"):
            GLib.idle_add(lambda: self._steam_work_ended() and False)
        GLib.idle_add(lambda: self._refresh(keep_scroll=True) and False)
        if getattr(self.app, "_tm_lib_shown", False) and not getattr(self.app, "_tm_active", False):
            GLib.idle_add(lambda: self.app._render_launchers() and False)
        self.queue.remove(uid)
        self.downloads.pop(uid, None)
        self._toast = why
        self._toast_sub = ""
        self._toast_until = time.monotonic() + 5
        self.grid.refresh_game(uid)

    def _redraw_downloads(self):
        for card in list(InstallingCard.live):
            if card.get_mapped():
                if isinstance(card, InstallingRow):
                    card.update()
                card.area.queue_draw()
        for it in self.grid._visible():
            if it.game.uid in self.downloads:
                self.grid.queue_item(it)

    def _update_dock(self):
        current = next((u for u in self.queue if self.downloads[u]["state"] in ("downloading", "installing")), None)
        if current is None and self.queue:
            current = self.queue[0]
        if current is None:
            if time.monotonic() < self._toast_until:
                self.dock_title.set_text("✓  " + self._toast)
                self.dock_sub.set_text(self._toast_sub)
                self.dock_next.set_text("")
                self._dock_uid = None
                self.dock_bar.hide()
                self.dock_open.hide()
                self.dock_art.queue_draw()
            self.dock_rev.set_reveal_child(time.monotonic() < self._toast_until or bool(self.queue))
            return
        dl = self.downloads[current]
        game = self.games_by_uid.get(current)
        self._dock_uid = current
        self._dock_launcher = dl["launcher"]
        name = game.title if game else current
        if dl["state"] == "asked":
            self.dock_title.set_text(self.app._("tm_dock_asked").format(launcher=dl["launcher"], game=name))
            self.dock_sub.set_text(self.app._("tm_dock_asked_sub"))
        elif dl["state"] == "queued":
            self.dock_title.set_text(self.app._("tm_dock_queued").format(launcher=dl["launcher"], game=name))
            self.dock_sub.set_text(self.app._("tm_dock_queued_sub"))
        else:
            self.dock_title.set_text(self.app._("tm_dock_unpacking" if dl["state"] == "installing" else "tm_dock_digging")
                                     .format(launcher=dl["launcher"], game=name))
            done = dl.get("got", dl["frac"] * dl["size"])
            rate = dl.get("rate") or dl.get("speed") or 0
            parts = [f"{int(dl['frac'] * 100)} %"]
            part = {"shaders": "tm_phase_shaders", "game": "tm_phase_game", "commit": "tm_phase_commit"}.get(
                dl.get("phase") or "")
            if dl["launcher"] == "Faugus":
                part = f"tm_fg_phase_{dl.get('phase') or 'prepare'}"
            if part:
                parts.insert(0, self.app._(part))
            if dl["size"]:
                parts.append(self.app._("tm_of").format(done=_fmt_gb(done), total=_fmt_gb(dl["size"])))
            if rate > 1e5:
                left = max(0, (dl["size"] - done) / rate)
                mins, secs = divmod(int(left), 60)
                parts += [f"{rate / 1e6:.0f} MB/s", self.app._("tm_left").format(m=mins, s=f"{secs:02d}")]
            self.dock_sub.set_text("   ·   ".join(parts))
        waiting = [self.games_by_uid[u].title for u in self.queue if self.downloads[u]["state"] == "queued"
                   and u in self.games_by_uid]
        self.dock_next.set_text(self.app._("tm_next").format(names=", ".join(waiting)) if waiting else "")
        # only Stop (an install of ours); the launcher's own window isn't needed
        self.dock_open.set_label(self.app._("tm_fg_close_it") if dl["launcher"] == "Faugus"
                                 and dl.get("phase") in ("signin", "close_it") else self.app._("tm_stop"))
        self._dock_direct = bool(dl.get("direct"))
        self.dock_open.set_visible(self._dock_direct)
        self.dock_bar.show()
        self.dock_bar.queue_draw()
        self.dock_art.queue_draw()
        self.dock_rev.set_reveal_child(True)

    def _draw_dock_bar(self, widget, cr):
        uid = self._dock_uid
        frac = self.downloads[uid]["frac"] if uid in self.downloads else 1.0
        a = widget.get_allocation()
        accent = _accent_rgb(widget)
        _rounded(cr, 0, 1, a.width, 6, 3)
        cr.set_source_rgba(*self.colors()["fg"], 0.12)
        cr.fill()
        _rounded(cr, 0, 1, max(6, a.width * frac), 6, 3)
        grad = cairo.LinearGradient(0, 0, a.width, 0)
        grad.add_color_stop_rgba(0, *accent, 0.7)
        grad.add_color_stop_rgba(1, *accent, 1)
        cr.set_source(grad)
        cr.fill()
        return False

    def _draw_dock_art(self, widget, cr):
        uid = self._dock_uid
        a = widget.get_allocation()
        _rounded(cr, 0, 0, a.width, a.height, 8)
        cr.clip()
        pb = self.loader.get(uid) if uid else None
        if pb is not None:
            d = widget.get_scale_factor()
            sc = a.width / (pb.get_width() / d)
            cr.scale(sc, sc)
            cr.set_source_surface(Gdk.cairo_surface_create_from_pixbuf(pb, d, None), 0, 0)
            cr.paint()
        else:
            accent = _accent_rgb(widget)
            cr.set_source_rgba(*accent, 0.25)
            cr.paint()
            _pango_text(cr, widget, "✓", 0, a.height / 2 - 14, a.width, 16, True, (*accent, 1), align_center=True)
        return False


def _fit_size(app) -> Tuple[int, int]:
    """Big enough to browse, never bigger than the screen it opens on
    (a 1080p one included): 88 % of the monitor's work area at most."""
    f = max(1.0, app._text_factor())
    w, h = int(1240 * f), int(860 * f)
    try:
        display = Gdk.Display.get_default()
        win = app.get_window()
        mon = display.get_monitor_at_window(win) if win is not None else display.get_primary_monitor()
        area = mon.get_workarea()
        w, h = min(w, int(area.width * 0.88)), min(h, int(area.height * 0.88))
    except Exception:
        pass
    return max(720, w), max(520, h)


def open_treasure_map(app, opts: Dict):
    dlg = Gtk.Dialog(title="Treasure Map", transient_for=app._dialog_transient_parent(), flags=0)
    dlg.set_modal(True)
    dlg.set_decorated(False)
    dlg.get_style_context().add_class("tm-window")
    dlg.set_default_size(*_fit_size(app))
    area = dlg.get_content_area()
    area.set_border_width(0)
    action_area = dlg.get_action_area()
    if action_area is not None:
        action_area.set_no_show_all(True)
        action_area.hide()
    view = TreasureMap(app, lambda: dlg.response(Gtk.ResponseType.CLOSE), opts)

    def paint(widget, cr):
        cr.set_source_rgb(*view.colors()["bg"])
        cr.paint()
        return False
    area.set_app_paintable(True)
    area.connect("draw", paint)
    area.pack_start(view, True, True, 0)
    dlg.connect("key-press-event", lambda _d, ev: view.on_key(ev))
    dlg.show_all()
    view.activate()
    view.dock_rev.set_reveal_child(False)
    view.grid.grab_focus()
    if opts.get("on_view"):
        opts["on_view"](view)
    app._enter_dimmed_dialog()
    try:
        dlg.run()
    finally:
        app._exit_dimmed_dialog()
        view.stop()
        dlg.destroy()


def draw_map_icon(cr, size, rgb, fill=False, alpha=1.0):
    """A folded map with a dashed route to an X - drawn (40 px design)."""
    cr.save()
    cr.scale(size / 40, size / 40)
    cr.new_path()
    cr.set_line_width(2.2)
    cr.set_line_join(cairo.LINE_JOIN_ROUND)
    cr.set_line_cap(cairo.LINE_CAP_ROUND)
    pts = [(4, 9), (14, 5), (26, 9), (36, 5), (36, 31), (26, 35), (14, 31), (4, 35)]
    cr.move_to(*pts[0])
    for p in pts[1:]:
        cr.line_to(*p)
    cr.close_path()
    if fill:
        cr.set_source_rgba(*rgb, 0.16 * alpha)
        cr.fill_preserve()
    cr.set_source_rgba(*rgb, alpha)
    cr.stroke()
    cr.set_line_width(1.6)
    cr.set_dash([2.2, 3.0])
    cr.move_to(9, 27)
    cr.curve_to(13, 18, 20, 26, 24, 18)
    cr.stroke()
    cr.set_dash([])
    cr.set_line_width(2.4)
    for dx, dy in ((1, 1), (1, -1)):
        cr.move_to(29 - 3.2 * dx, 16 - 3.2 * dy)
        cr.line_to(29 + 3.2 * dx, 16 + 3.2 * dy)
    cr.stroke()
    cr.restore()


# --- in the main window: a sidebar page above All Plunder ---------------------------------

def _launcher_running(launcher: str) -> bool:
    try:
        from plunderhub.core.launch_options import launcher_pids
        return bool(launcher_pids(launcher))
    except Exception:  # noqa: BLE001 - a process list that can't be read: not running
        return False


def shown(app) -> Optional["TreasureMap"]:
    """The map, while it is the page the main window shows."""
    return getattr(app, "_tm_view", None) if getattr(app, "_tm_active", False) else None


def map_count(app) -> int:
    view = getattr(app, "_tm_view", None)
    return sum(len(st.games) for st in view.states.values()) if view is not None else 0


def follow_library_look(app):
    """Ship's Controls changed the covers / the text: the tiles again."""
    view = getattr(app, "_tm_view", None)
    if view is not None:
        view.grid._tiles.clear()
        view.grid._heads.clear()
        view.grid._badges.clear()
        view.grid._relayout()


def add_uninstall_row(menu, page: Gtk.Box):
    """A Steam / Heroic / Faugus game's menu in the library: Uninstall,
    above the last divider."""
    game, app = menu.game, menu.app
    if game.launcher not in ("Steam", "Heroic", "Faugus"):
        return
    seps = [c for c in page.get_children() if isinstance(c, Gtk.Separator)]
    row = menu._row("trash-symbolic", app._("tm_uninstall"), app._("tm_uninstall_sub").format(launcher=game.launcher),
                    action=lambda: (_uninstall_from_faugus if game.launcher == "Faugus"
                                    else _uninstall_from_library)(app, game))
    page.pack_start(row, False, False, 0)
    if seps and not getattr(app, "_tm_active", False):      # on the map there's nothing under it
        page.reorder_child(row, page.get_children().index(seps[-1]))
    row.show_all()


def attach(app, opts):
    """The page, over the library's scroller in the main overlay (hidden
    until picked), and its sidebar row first in the main list - the app
    still starts on All Plunder (home_nav is never "map")."""
    view = TreasureMap(app, None, opts, embedded=True)
    view.set_no_show_all(True)
    # over the bubbles, like the library's scroller - right above it, under
    # the app's own overlays (toasts, the Animations paused pill...)
    app.main_overlay.add_overlay(view)
    app.main_overlay.reorder_overlay(view, 1)
    app._tm_view, app._tm_active = view, False
    icon = Gtk.DrawingArea()
    icon.set_size_request(18, 18)
    icon.set_valign(Gtk.Align.CENTER)

    def draw(widget, cr):
        ctx = widget.get_style_context()
        fg = ctx.get_color(ctx.get_state())
        draw_map_icon(cr, 18, (fg.red, fg.green, fg.blue), alpha=fg.alpha)
        return False
    icon.connect("draw", draw)
    row = app._home_nav_row("map", icon, "tm_title", with_count=True)
    app.home_nav_main.insert(row, 3)          # under Recently sailed
    row.show_all()
    app._tm_row = row
    app._refresh_home_counts()
    return view


def show_map(app, on: bool):
    view = app._tm_view
    if on == app._tm_active:
        return
    app._tm_active = on
    # the same top bar as the library's, without what only the library has:
    # grid / list, their divider, Select and Edit order
    toolbar = app.view_grid_btn.get_parent()
    kids = toolbar.get_children()
    divider = kids[kids.index(app.view_list_btn) + 1]
    for w in (app.view_grid_btn, app.view_list_btn, divider, app.select_btn, app.reorder_btn):
        w.set_visible(not on)
    app._update_sort_btn_label()
    if on:
        view.set_query(app.search_entry.get_text())
    else:
        # a search typed on the map is the map's: leaving it clears it
        view.set_query("")
        if app.search_entry.get_text():
            app.search_entry.set_text("")
        # installs started (or ended) on the map: the library's page shows them
        if view.queue or getattr(app, "_tm_lib_shown", False):
            GLib.idle_add(lambda: app._render_launchers() and False)
    app.scroller.set_visible(not on)
    # the library's Back to top pill belongs to the library: left up from a scrolled
    # library it sat over the map's dock (its Stop button) and scrolled the hidden page
    if on:
        app._set_scroll_top_visible(False)
    else:
        app._on_main_scroll_value_changed(app.scroller.get_vadjustment())
    if on:
        view.set_no_show_all(False)
        view.show_all()
        view.dock_rev.set_reveal_child(bool(view.queue))
        view.activate()
        view.grid.grab_focus()
    else:
        view.deactivate()
        view.hide()
        app._on_search_changed()                   # the library catches up with the search
    app._update_decorative_animation_pause_state()
    app._home_select_nav_row()                     # the sidebar says which page this is


def _uninstall_from_faugus(app, game):
    """Asked first; the prefix goes too unless it's unticked (a store
    launcher's games live in it) or another Faugus entry uses it. A running
    Faugus is closed for it and started again."""
    fa = hold_data_faugus()
    _status, site = fa.faugus_status()
    if site is None:
        return
    gid = game.uid.split(":", 1)[1]
    _ = app._
    from plunderhub.core.faugus_store import known as store_games
    owner = store_games().get(gid)
    if owner:
        # its launcher's own job: the launcher opens, the card goes when the game does
        launcher = app.game_index.get(f"faugus:{owner.get('launcher')}")
        if launcher is not None:
            app._show_info_dialog(_("tm_fg_uninstall_in_launcher").format(game=game.title, launcher=launcher.title),
                                  Gtk.MessageType.INFO)
            app.launch(launcher.launch_cmd, launcher.title, launcher.env, launcher.uid, launcher.install_dir)
        return
    extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
    tile = how = None
    choice = {"trash": True}
    if fa.own_prefix(site, gid):
        tile = app._new_game_tile("trash-symbolic", _("tm_fg_rm_prefix"), _("tm_fg_rm_prefix_sub"))
        extra.pack_start(tile, False, False, 0)
        how = _trash_or_delete(app, choice)
        extra.pack_start(how, False, False, 0)
    running = app._launcher_is_running("Faugus")
    if running:
        note = Gtk.Label(label=_("lo_new_restart_note").format(launcher="Faugus"), xalign=0)
        note.set_line_wrap(True)
        note.set_max_width_chars(46)
        note.get_style_context().add_class("newgame-restart")
        extra.pack_start(note, False, False, 0)
    extra.show_all()
    if tile is not None:
        tile.connect("toggled", lambda t: how.set_visible(t.get_active()))
    answer = app._card_dialog(_("tm_fg_rm_t").format(game=game.title), _("tm_fg_rm_s"),
                              [(_("tm_not_now"), Gtk.ResponseType.CANCEL, "ghost"),
                               (_("tm_uninstall"), Gtk.ResponseType.OK, "danger")],
                              default=Gtk.ResponseType.CANCEL, extra=extra, art=app._new_game_art(game))
    if answer != Gtk.ResponseType.OK or (running and not app.close_launcher_asking("Faugus", app)):
        return
    with_prefix = tile is not None and tile.get_active()
    command = next((list(s["cmd"]) for s in getattr(app, "_cached_launchers", []) or []
                    if s.get("name") == "Faugus" and s.get("cmd")), None) if running else None

    from plunderhub.ui.progress_card import card_dialog, status_label
    busy = {"dlg": None}

    def run(to_trash: bool):
        # a big prefix takes a while: said, not silent
        dlg, column = card_dialog(app._dialog_transient_parent(), game.title)
        column.pack_start(status_label(_("tm_going_overboard")), False, False, 0)
        dlg.show_all()
        busy["dlg"] = dlg

        def work():
            GLib.idle_add(done, *fa.remove_game(site, gid, with_prefix, to_trash))
        threading.Thread(target=work, daemon=True, name="plunderhub-faugus-remove").start()

    def restart():
        if command:
            import subprocess
            try:
                subprocess.Popen(command, start_new_session=True, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except OSError as e:
                logger.warning(f"Couldn't start Faugus again: {e}")

    def done(ok, why) -> bool:
        if busy["dlg"] is not None:
            busy["dlg"].destroy()
            busy["dlg"] = None
        if why == "trash":
            if app._card_dialog(_("bg_no_trash_head"), _("bg_no_trash"),
                                [(_("dialog_cancel"), Gtk.ResponseType.NO, "ghost"),
                                 (_("bg_permanent"), Gtk.ResponseType.YES, "danger")],
                                tone="danger", icon_name="user-trash-symbolic",
                                default=Gtk.ResponseType.NO) == Gtk.ResponseType.YES:
                run(False)
            else:
                restart()
            return False
        restart()
        if ok:
            app.forget_art(game.uid)
        else:
            app._card_dialog(_("tm_uninstall_failed").format(game=game.title), why,
                             [(_("dialog_ok"), Gtk.ResponseType.CLOSE, "ghost")], icon_name="circle-alert-symbolic")
        app.reload()
        view = getattr(app, "_tm_view", None)
        if view is not None:
            view.reload(keep_scroll=True)       # its own list: the library's reload doesn't touch it
        return False
    run(choice["trash"])


def _trash_or_delete(app, choice: Dict) -> Gtk.Box:
    """Bilge cleaning's two tiles: to the Trash (picked first) or for good."""
    from plunderhub.icons import get_icon_pixbuf
    from plunderhub.ui.home_mixin import _accent_rgb
    colour = "#%02x%02x%02x" % tuple(round(c * 255) for c in _accent_rgb(app))
    cards = Gtk.Box(spacing=10, homogeneous=True)
    first = None
    for key, icon, title, sub in (("trash", "user-trash-symbolic", "bg_trash", "bg_trash_short"),
                                  ("forever", "zap-symbolic", "bg_permanent", "bg_permanent_short")):
        card = Gtk.RadioButton.new_from_widget(first)
        first = first or card
        card.set_mode(False)
        card.set_focus_on_click(False)
        card.get_style_context().add_class("bg-tile")
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        inner.pack_start(Gtk.Image.new_from_pixbuf(get_icon_pixbuf(icon, 22, colour)), False, False, 0)
        name = Gtk.Label(label=app._(title))
        name.get_style_context().add_class("bg-tile-title")
        inner.pack_start(name, False, False, 0)
        line = Gtk.Label(label=app._(sub), justify=Gtk.Justification.CENTER)
        line.set_line_wrap(True)
        line.set_max_width_chars(20)
        line.get_style_context().add_class("bg-tile-sub")
        inner.pack_start(line, False, False, 0)
        card.add(inner)
        card.connect("toggled", lambda b, k=key: b.get_active() and choice.update(trash=k == "trash"))
        cards.pack_start(card, True, True, 0)
    return cards


def _uninstall_from_library(app, game):
    view = getattr(app, "_tm_view", None)
    app_id = game.uid.split(":")[-1]
    owned = None
    if view is not None:
        owned = next((g for st in view.states.values() for g in st.games if g.app_id == app_id), None)
    if not confirm_uninstall(app, app, game.title, game.launcher, owned.size if owned else 0,
                             game=owned, cache=view.opts["cache"] if view is not None else None):
        return
    if view is not None and owned is not None:
        view.start_uninstall(owned)


# --- the library's Select: several games uninstalled at once ------------------------------

def bulk_uninstall(app, uids) -> bool:
    """The selected games that can go from here (Steam, Heroic, a Faugus
    entry of its own), asked once; Steam and Heroic closed once for all and
    their games removed one after the other (they share the launcher's
    files), Faugus' alike. True: it started."""
    from plunderhub.core.utils import format_bytes
    _ = app._
    view = getattr(app, "_tm_view", None)
    fa = hold_data_faugus()
    from plunderhub.core.faugus_store import known as store_games
    inside = store_games()
    steam, heroic, faugus, skipped = [], [], [], []
    for uid in uids:
        g = app.game_index.get(uid)
        if g is None:
            continue
        if g.launcher in ("Steam", "Heroic") and view is not None:
            app_id = uid.split(":")[-1]
            owned = next((o for st in view.states.values() for o in st.games
                          if o.app_id == app_id and o.installed and LAUNCHER_OF.get(o.store) == g.launcher), None)
            if owned is None or owned.uid in view.removing:
                skipped.append(g.title)
            else:
                (steam if owned.store == "steam" else heroic).append(owned)
        elif g.launcher == "Faugus" and uid.split(":", 1)[1] not in inside:
            faugus.append(g)
        else:
            skipped.append(g.title)
    titles = [o.title for o in steam + heroic] + [g.title for g in faugus]
    if not titles:
        app._card_dialog(_("tm_bulk_none_t"), _("tm_bulk_none_s"),
                         [(_("dialog_ok"), Gtk.ResponseType.CLOSE, "ghost")], icon_name="trash-symbolic")
        return False
    lines = ["• " + t for t in titles[:8]]
    if len(titles) > 8:
        lines.append(_("tm_bulk_more").format(count=len(titles) - 8))
    size = sum(o.size for o in steam + heroic)
    text = "\n".join(lines)
    if size:
        text += "\n\n" + _("tm_bulk_free").format(size=format_bytes(size))
    if skipped:
        text += "\n\n" + _("tm_bulk_skipped").format(names=", ".join(skipped[:5]) + ("…" if len(skipped) > 5 else ""))
    extra = None
    tile = how = None
    choice = {"trash": True}
    _status, site = fa.faugus_status()
    backup_tile = None
    with_saves: Dict[str, str] = {}             # Faugus uid -> its own prefix, when it holds player files
    if faugus and site is not None and any(fa.own_prefix(site, g.uid.split(":", 1)[1]) for g in faugus):
        from plunderhub.core import bilge
        for g in faugus:
            prefix = fa.own_prefix(site, g.uid.split(":", 1)[1])
            if prefix:
                item = bilge.Item("prefix", "Faugus", Path(prefix), g.title)
                bilge.look_for_saves(item)
                if item.saves:
                    with_saves[g.uid] = prefix
        extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        tile = app._new_game_tile("trash-symbolic", _("tm_bulk_rm_prefix"), _("tm_fg_rm_prefix_sub"))
        extra.pack_start(tile, False, False, 0)
        if with_saves:
            # the saves in the prefixes that go: copied to Bilge cleaning's backups first
            backup_tile = app._new_game_tile("document-save-symbolic", _("tm_bulk_backup"),
                                             _("tm_bulk_backup_sub").format(count=len(with_saves)))
            extra.pack_start(backup_tile, False, False, 0)
        how = _trash_or_delete(app, choice)
        extra.pack_start(how, False, False, 0)
        extra.show_all()

        def prefix_toggled(t):
            how.set_visible(t.get_active())
            if backup_tile is not None:
                backup_tile.set_visible(t.get_active())
        tile.connect("toggled", prefix_toggled)
    answer = app._card_dialog(_("tm_bulk_rm_t").format(count=len(titles)), text,
                              [(_("tm_keep_it"), Gtk.ResponseType.CANCEL, "ghost"),
                               (_("tm_uninstall"), Gtk.ResponseType.OK, "danger")],
                              tone="danger", icon_name="trash-symbolic", default=Gtk.ResponseType.CANCEL, extra=extra)
    if answer != Gtk.ResponseType.OK:
        return False
    # the launchers closed first, once (each asks when one of its games runs)
    if steam and not view._steam_stop_for_work():
        steam = []
    if heroic and app._launcher_is_running("Heroic") and not app.close_launcher_asking("Heroic", app):
        heroic = []
    faugus_cmd = None
    if faugus:
        if app._launcher_is_running("Faugus"):
            faugus_cmd = next((list(s["cmd"]) for s in getattr(app, "_cached_launchers", []) or []
                               if s.get("name") == "Faugus" and s.get("cmd")), None)
            if not app.close_launcher_asking("Faugus", app):
                faugus, faugus_cmd = [], None
    if not (steam or heroic or faugus):
        return False
    logger.info(f"Bulk uninstall: {len(steam)} Steam, {len(heroic)} Heroic, {len(faugus)} Faugus")
    for o in steam + heroic:
        view.removing.add(o.uid)
        view._removing_since[o.uid] = (time.monotonic(), 3600)
        view.grid.refresh_game(o.uid)
    if steam or heroic:
        view._ensure_tick()
    failed: List[Tuple[str, str]] = []
    left = {"n": (1 if steam else 0) + (1 if heroic else 0) + (1 if faugus else 0)}

    def all_done():
        left["n"] -= 1
        if left["n"]:
            return
        if failed:
            app._card_dialog(_("tm_uninstall_failed").format(game=", ".join(t for t, _w in failed)),
                             "\n".join(f"{t}: {w}" for t, w in failed),
                             [(_("dialog_ok"), Gtk.ResponseType.CLOSE, "ghost")], icon_name="circle-alert-symbolic")
        app.reload()
        if view is not None:
            view.reload(keep_scroll=True)       # its own list: the library's reload doesn't touch it

    def steam_work():
        for o in steam:
            ok, why = hold_data.steam_remove_files(o.app_id)
            if not ok:
                failed.append((o.title, why))
                GLib.idle_add(lambda u=o.uid: (view.removing.discard(u), view.grid.refresh_game(u), False)[2])

        def done():
            if view._steam_ran_before:
                view._steam_start_silent()
            all_done()
            return False
        GLib.idle_add(done)

    def heroic_work():
        for o in heroic:
            ok, why = hold_data.heroic_uninstall(o.store, o.app_id, o.title)
            if not ok:
                failed.append((o.title, why))
                GLib.idle_add(lambda u=o.uid: (view.removing.discard(u), view.grid.refresh_game(u), False)[2])
        GLib.idle_add(lambda: all_done() and False)

    with_prefix = tile is not None and tile.get_active()
    back_up_first = with_prefix and backup_tile is not None and backup_tile.get_active()
    backed: set = set()

    def faugus_work(to_trash: bool):
        no_trash = []
        for g in faugus:
            gid = g.uid.split(":", 1)[1]
            if back_up_first and g.uid in with_saves and g.uid not in backed:
                from plunderhub.core import bilge
                try:
                    bilge.back_up(bilge.Item("prefix", "Faugus", Path(with_saves[g.uid]), g.title), g.title)
                    backed.add(g.uid)
                    logger.info(f"Bulk uninstall: {g.title}'s saves backed up")
                except Exception as e:      # no copy of its saves: it stays as it is
                    logger.warning(f"Bulk uninstall: {g.title}'s saves not backed up: {e}")
                    failed.append((g.title, _("tm_bulk_backup_failed").format(why=e)))
                    continue
            ok, why = fa.remove_game(site, gid, with_prefix and bool(fa.own_prefix(site, gid)), to_trash)
            if why == "trash":
                no_trash.append(g)
            elif not ok:
                failed.append((g.title, why))
            else:
                GLib.idle_add(lambda u=g.uid: (app.forget_art(u), False)[1])
        GLib.idle_add(faugus_done, no_trash)

    def faugus_done(no_trash) -> bool:
        if no_trash and app._card_dialog(_("bg_no_trash_head"), _("bg_no_trash"),
                                         [(_("dialog_cancel"), Gtk.ResponseType.NO, "ghost"),
                                          (_("bg_permanent"), Gtk.ResponseType.YES, "danger")],
                                         tone="danger", icon_name="user-trash-symbolic",
                                         default=Gtk.ResponseType.NO) == Gtk.ResponseType.YES:
            faugus[:] = no_trash
            threading.Thread(target=faugus_work, args=(False,), daemon=True).start()
            return False
        if faugus_cmd:
            import subprocess
            try:
                subprocess.Popen(faugus_cmd, start_new_session=True, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL)
            except OSError as e:
                logger.warning(f"Couldn't start Faugus again: {e}")
        all_done()
        return False

    if steam:
        threading.Thread(target=steam_work, daemon=True, name="plunderhub-bulk-steam").start()
    if heroic:
        threading.Thread(target=heroic_work, daemon=True, name="plunderhub-bulk-heroic").start()
    if faugus:
        threading.Thread(target=faugus_work, args=(choice["trash"],), daemon=True,
                         name="plunderhub-bulk-faugus").start()
    return True
