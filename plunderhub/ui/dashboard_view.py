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

"""The Captain's Dashboard: sail time (a hero total, KPI tiles, a column
chart, the most-played games) and the cargo hold (disk use per drive, the
biggest games). Drawn by hand, like Console Mode."""

import calendar
import math
import threading
import time
from datetime import date, timedelta
from typing import Callable, Dict, List, Optional, Tuple

import cairo

from plunderhub.core.persistence import save_playtime_history
from plunderhub.core.utils import format_bytes
from plunderhub.gi_setup import Gdk, GdkPixbuf, GLib, Gtk, image_surface, logical_size, ui_scale
from plunderhub.icons import get_icon_pixbuf
from plunderhub.logging_setup import logger
from plunderhub.theme import THEME_PALETTES
from plunderhub.ui.home_mixin import _accent_rgb, _on_accent_rgb, _pango_text, _rounded

PERIODS = ("week", "month", "year")
ROW_H = 52
TOP_GAMES = 5
BIGGEST_GAMES = 6


def _hex(value: str) -> Tuple[float, float, float]:
    h = value.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def _column(cr, x, y, w, h, r):
    """A column: 4px rounded data-end, square at the baseline."""
    r = max(0.0, min(r, w / 2, h))
    cr.new_sub_path()
    cr.move_to(x, y + h)
    cr.line_to(x, y + r)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.line_to(x + w, y + h)
    cr.close_path()


class Bucket:
    """One column: a day, or a month in the Year view."""

    def __init__(self, tick: str, label: str, dates: List[str]):
        self.tick, self.label, self.dates = tick, label, dates
        self.minutes = 0
        self.per_game: Dict[str, int] = {}


class DashboardView(Gtk.DrawingArea):
    def __init__(self, app, close: Callable[[], None]):
        super().__init__()
        self.app = app
        self.close = close
        self.tab = 0                    # 0 sail time, 1 cargo hold
        self.period = "week"
        self.offset = 0                 # 0 = this period, -1 = the one before, ...
        self.hover: Optional[int] = None
        self.anim = 1.0
        self.storage = None             # (games [(GameItem, bytes)], drives [...]) once measured
        self.storage_started = False
        self._undo = None               # (history backup, until)
        self._pointer: Optional[Tuple[float, float]] = None
        self._undo_rect: Optional[Tuple[float, float, float, float]] = None
        self._hits: List[Tuple[Tuple[float, float, float, float], Callable[[], None]]] = []
        self._col_hits: List[Tuple[Tuple[float, float, float, float], int]] = []
        self._icons: Dict[Tuple[str, int], Optional[GdkPixbuf.Pixbuf]] = {}
        self._memo: Dict[Tuple[str, int], Tuple[str, List[Bucket]]] = {}
        # Everything but the columns' bars, the tooltip and the undo toast,
        # drawn once: a pointer over the columns repainted the whole page
        # every time it crossed one (45 % CPU sweeping over them).
        # (size key, picture, hits, column hits, plot)
        self._base = None
        self._plot = None               # where the bars go, for the live layer
        self._grow_tick: Optional[int] = None
        self._grow_start: Optional[int] = None
        self._pointer_cursor = False
        # the keyboard's / controller's cursor: the rect of the button it's on
        self.kb_rect: Optional[Tuple[float, float, float, float]] = None
        self.kb_on = False
        self._kb_from: Optional[Tuple[float, float]] = None
        self.set_can_focus(True)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK | Gdk.EventMask.POINTER_MOTION_MASK
                        | Gdk.EventMask.LEAVE_NOTIFY_MASK | Gdk.EventMask.KEY_PRESS_MASK)
        self.connect("draw", self._draw)
        self.connect("button-press-event", self._on_click)
        self.connect("motion-notify-event", self._on_motion)
        self.connect("leave-notify-event", self._on_leave)
        self.connect("key-press-event", self._on_key)
        self._grow()

    # --- data -----------------------------------------------------------------------

    def buckets(self, offset: Optional[int] = None) -> Tuple[str, List[Bucket]]:
        """Worked out once per period shown; _grow() forgets them on any change."""
        offset = self.offset if offset is None else offset
        key = (self.period, offset)
        if key not in self._memo:
            self._memo[key] = self._buckets(offset)
        return self._memo[key]

    def _buckets(self, offset: int) -> Tuple[str, List[Bucket]]:
        app = self.app
        today = date.today()
        weekdays = app._date_names("date_weekdays_short")
        months_short = app._date_names("date_months_short")
        out: List[Bucket] = []
        if self.period == "week":
            start = today - timedelta(days=today.weekday()) + timedelta(weeks=offset)
            days = [start + timedelta(days=i) for i in range(7)]
            for d in days:
                out.append(Bucket(weekdays[d.weekday()], app._format_day_label(d.isoformat()), [d.isoformat()]))
            title = (f"{app._format_day_label(days[0].isoformat())} – "
                     f"{app._format_day_label(days[-1].isoformat())} {days[-1].year}")
        elif self.period == "month":
            y, m = today.year, today.month + offset
            while m < 1:
                y, m = y - 1, m + 12
            title = app._format_month_label(f"{y}-{m:02d}")
            for day in range(1, calendar.monthrange(y, m)[1] + 1):
                d = date(y, m, day)
                out.append(Bucket(str(day) if day in (1, 8, 15, 22, 29) else "",
                                  app._format_day_label(d.isoformat()), [d.isoformat()]))
        else:
            y = today.year + offset
            title = str(y)
            for m in range(1, 13):
                prefix = f"{y}-{m:02d}"
                dates = [k for k in app.playtime_history if k.startswith(prefix)]
                out.append(Bucket(months_short[m - 1], app._format_month_label(prefix), dates))
        for b in out:
            for d in b.dates:
                for uid, minutes in (app.playtime_history.get(d) or {}).items():
                    b.minutes += minutes
                    if uid != "__unknown__":
                        b.per_game[uid] = b.per_game.get(uid, 0) + minutes
        return title, out

    def _title_of(self, uid: str) -> Tuple[str, str]:
        game = self.app.game_index.get(uid)
        if game is not None:
            return game.title, game.launcher
        known = self.app.known_titles.get(uid, {})
        return known.get("title", uid), known.get("launcher", "")

    def _icon(self, uid: str, title: str, px: int) -> GdkPixbuf.Pixbuf:
        """The game's own icon; without one (no API key, or a removed game
        whose art went with it) the same monogram the library list shows."""
        key = (uid, px)
        if key not in self._icons:
            pb = None
            path = self.app._find_cached_artwork(self.app.game_icons_dir, uid)
            if path is not None:
                try:
                    pb = GdkPixbuf.PixbufAnimation.new_from_file(str(path)).get_static_image()
                    pb = pb.scale_simple(px, px, GdkPixbuf.InterpType.BILINEAR)
                except Exception as e:
                    logger.debug(f"Dashboard couldn't load the icon of {uid}: {e}")
                    pb = None
            if pb is None:
                pb = self.app._resolve_icon_pixbuf(px, None, title)
            self._icons[key] = pb
        return self._icons[key]

    # --- actions -------------------------------------------------------------------

    def set_tab(self, tab: int):
        self.tab = tab
        if tab == 1 and not self.storage_started:
            self.storage_started = True
            threading.Thread(target=self._measure_storage, daemon=True).start()
        self._grow()

    def set_period(self, period: str):
        if period != self.period:
            self.period, self.offset = period, 0
            self._grow()

    def step(self, delta: int):
        if self.offset + delta <= 0:
            self.offset += delta
            self._grow()

    def controller(self, action: str):
        """The pad: the d-pad moves the cursor between the page's buttons
        (tabs, periods, ‹ ›, Clear), A presses, LB / RB switch tab, B closes."""
        if action == "back":
            self.close()
        elif action in ("prev_tab", "next_tab"):
            self.set_tab(1 - self.tab)
        elif action in ("left", "right", "up", "down"):
            self.kb_move(action)
        elif action == "activate":
            self.kb_press()

    # --- the keyboard's / controller's cursor --------------------------------------------

    @staticmethod
    def _centre(rect) -> Tuple[float, float]:
        x, y, w, h = rect
        return x + w / 2, y + h / 2

    def kb_move(self, action: str):
        self._kb_from = None
        hits = [rect for rect, _a in self._hits]
        if not hits:
            return
        if not self.kb_on or self.kb_rect is None:
            self.kb_on = True
            if self.kb_rect is None or self._nearest(self.kb_rect) is None:
                self.kb_rect = min(hits, key=lambda r: (r[1], r[0]))
            self.queue_draw()
            return
        # a row is what sits at one height (the tabs; the periods with ‹ ›
        # and Clear): Left / Right along it, Up / Down to the next row's nearest
        rows: List[List[Tuple[float, float, float, float]]] = []
        for rect in sorted(hits, key=lambda r: (self._centre(r)[1], r[0])):
            if rows and abs(self._centre(rows[-1][0])[1] - self._centre(rect)[1]) <= 12:
                rows[-1].append(rect)
            else:
                rows.append([rect])
        current = self._nearest(self.kb_rect)[0]
        r = next(i for i, row in enumerate(rows) if current in row)
        c = rows[r].index(current)
        if action in ("left", "right"):
            j = c + (1 if action == "right" else -1)
            if 0 <= j < len(rows[r]):
                self.kb_rect = rows[r][j]
        else:
            nr = r + (1 if action == "down" else -1)
            if 0 <= nr < len(rows):
                cx = self._centre(current)[0]
                self.kb_rect = min(rows[nr], key=lambda rect: abs(self._centre(rect)[0] - cx))
        self.queue_draw()

    def _nearest(self, rect):
        """The hit at rect now - or the nearest one, the page having been
        laid out again (another period's labels are wider)."""
        if not self._hits:
            return None
        cx, cy = self._centre(rect)
        return min(self._hits, key=lambda h: abs(self._centre(h[0])[0] - cx) + abs(self._centre(h[0])[1] - cy))

    def kb_press(self):
        if not self.kb_on or self.kb_rect is None:
            self.kb_move("down")
            return
        hit = self._nearest(self.kb_rect)
        if hit is not None:
            hit[1]()
            self.queue_draw()

    def _draw_kb(self, cr, c):
        """The cursor's ring, on top of everything."""
        if not self.kb_on or self.kb_rect is None:
            return
        hit = self._nearest(self.kb_rect)
        if hit is None:
            return
        self.kb_rect = hit[0]
        x, y, w, h = hit[0]
        _rounded(cr, x - 3, y - 3, w + 6, h + 6, (h + 6) / 2)
        cr.set_source_rgba(*c["fg"], 0.9)
        cr.set_line_width(2)
        cr.stroke()

    def _clear_history(self):
        app = self.app
        if not app.playtime_history or not app._confirm_yes_no(
                app._("playtime_reset_confirm_text"), transient_for=self.get_toplevel(),
                heading=app._("playtime_reset_confirm_heading"), yes_label=app._("dialog_sink_btn"),
                no_label=app._("dialog_keep_btn"), destructive=True):
            return
        backup = {d: dict(games) for d, games in app.playtime_history.items()}
        app.playtime_history = {}
        save_playtime_history(app.playtime_history)
        self._undo = (backup, time.monotonic() + 6)
        GLib.timeout_add(6100, lambda: (self.queue_draw(), False)[1])
        self._grow()

    def _undo_clear(self):
        if self._undo is None:
            return
        # minutes credited since Clear (a game that ended meanwhile) stay
        merged = self._undo[0]
        for day, games in self.app.playtime_history.items():
            into = merged.setdefault(day, {})
            for uid, minutes in games.items():
                into[uid] = into.get(uid, 0) + minutes
        self.app.playtime_history = merged
        save_playtime_history(self.app.playtime_history)
        self._undo = None
        self._grow()

    def _measure_storage(self):
        try:
            games, drives = self.app._compute_storage_results()
        except Exception as e:
            logger.warning(f"Storage scan failed unexpectedly: {e}")
            games, drives = [], []

        def done():
            self.storage = (games, drives)
            self._grow()
            return False
        GLib.idle_add(done)

    # --- input ------------------------------------------------------------------------

    def _on_key(self, _w, event) -> bool:
        k = event.keyval
        if k == Gdk.KEY_Escape:
            self.close()
        elif k in (Gdk.KEY_1, Gdk.KEY_2, Gdk.KEY_3):
            self.set_period(PERIODS[k - Gdk.KEY_1])
        elif k in (Gdk.KEY_Left, Gdk.KEY_Right, Gdk.KEY_Up, Gdk.KEY_Down):
            self.kb_move({Gdk.KEY_Left: "left", Gdk.KEY_Right: "right", Gdk.KEY_Up: "up", Gdk.KEY_Down: "down"}[k])
        elif k in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space):
            self.kb_press()
        elif k in (Gdk.KEY_Tab, Gdk.KEY_ISO_Left_Tab):
            self.set_tab(1 - self.tab)
        else:
            return False
        return True

    def _at(self, event) -> Tuple[float, float]:
        """Where the pointer is, in the drawing's own (unscaled) units."""
        s = self.app._text_factor()
        return event.x / s, event.y / s

    def _on_click(self, _w, event) -> bool:
        if event.button != 1 or event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        ex, ey = self._at(event)
        for (x, y, w, h), action in reversed(self._hits):
            if x <= ex <= x + w and y <= ey <= y + h:
                action()
                self.queue_draw()
                return True
        return False

    def _on_leave(self, *_args) -> bool:
        self._pointer = None
        self._set_hover(None)
        if self._undo_rect is not None:
            self.queue_draw()
        return False

    def _over_undo(self) -> bool:
        if self._pointer is None or self._undo_rect is None:
            return False
        (px, py), (x, y, w, h) = self._pointer, self._undo_rect
        return x <= px <= x + w and y <= py <= y + h

    def _on_motion(self, _w, event) -> bool:
        if self.kb_on:
            # the pointer takes over once it really moves (see ui/keynav)
            if self._kb_from is None:
                self._kb_from = (event.x_root, event.y_root)
            elif abs(event.x_root - self._kb_from[0]) + abs(event.y_root - self._kb_from[1]) > 12:
                self.kb_on, self._kb_from = False, None
                self.queue_draw()
        was_over_undo = self._over_undo()
        ex, ey = self._pointer = self._at(event)
        if self._over_undo() != was_over_undo:
            self.queue_draw()
        hover = next((i for (x, y, w, h), i in self._col_hits
                      if x <= ex <= x + w and y <= ey <= y + h), None)
        self._set_hover(hover)
        clickable = any(x <= ex <= x + w and y <= ey <= y + h for (x, y, w, h), _a in self._hits)
        window = self.get_window()
        if window is not None and clickable != self._pointer_cursor:
            self._pointer_cursor = clickable
            window.set_cursor(Gdk.Cursor.new_from_name(self.get_display(), "pointer") if clickable else None)
        return False

    def _set_hover(self, index: Optional[int]):
        if index == self.hover:
            return
        self.hover = index
        if self._plot is None:
            self.queue_draw()
            return
        # only the chart (the tooltip stays inside it, give or take its edge)
        x0, y0, w, h = self._plot["area"]
        s = self.app._text_factor()
        self.queue_draw_area(int((x0 - 4) * s), int((y0 - 16) * s), int((w + 8) * s) + 2, int((h + 44) * s) + 2)

    # --- animation ---------------------------------------------------------------------

    _GROW_US = 230_000

    def _grow(self):
        """Columns and bars grow in after any change - which is also when
        the cached page and periods are out of date."""
        self.anim = 0.0
        self._base = None
        self._memo.clear()
        self._grow_start = None
        if self._grow_tick is None:
            self._grow_tick = self.add_tick_callback(self._grow_step)
        self.queue_draw()

    def _grow_step(self, _widget, clock) -> bool:
        now = clock.get_frame_time()
        if self._grow_start is None:
            self._grow_start = now
        self.anim = min(1.0, (now - self._grow_start) / self._GROW_US)
        if self.anim >= 1.0:
            self._grow_tick = None
            self.queue_draw()
            return False
        if clock.get_frame_counter() % self.app._bubble_beat_frames(clock) == 0:
            self.queue_draw()       # ~60 fps like the app's other animations
        return True

    # --- drawing ------------------------------------------------------------------------

    def _colors(self):
        palette = THEME_PALETTES.get(self.app.theme) or THEME_PALETTES["plunderhub"]
        return {"bg": _hex(palette["bg"]), "alt": _hex(palette["bg_alt"]), "raised": _hex(palette["bg_raised"]),
                "fg": _hex(palette["fg"]), "dim": _hex(palette["fg_dim"])}

    def _hm(self, minutes: float) -> str:
        minutes = int(round(minutes))
        h, m = divmod(max(0, minutes), 60)
        return self.app._("playtime_hm").format(h=h, m=m) if h else self.app._("playtime_m").format(m=m)

    def _text(self, cr, s, x, y, size, bold=True, color=(1, 1, 1, 1), width=1000, center=False) -> float:
        return _pango_text(cr, self, s, x, y, width, size, bold, color, align_center=center)

    def _measure(self, cr, s, size, bold=True) -> Tuple[int, int]:
        from gi.repository import Pango, PangoCairo
        layout = PangoCairo.create_layout(cr)
        desc = self.get_pango_context().get_font_description().copy()
        desc.set_size(int(size * Pango.SCALE))
        desc.set_weight(Pango.Weight.BOLD if bold else Pango.Weight.NORMAL)
        layout.set_font_description(desc)
        layout.set_text(s, -1)
        return layout.get_pixel_size()

    def _pill(self, cr, x, y, items, selected, c, accent, on_pick, size=11.5) -> float:
        """A row of options in one rounded pill. Returns its width."""
        widths = [self._measure(cr, label, size)[0] + 34 for label, _v in items]
        _rounded(cr, x, y, sum(widths) + 8, 40, 20)
        cr.set_source_rgba(*c["raised"], 1)
        cr.fill()
        px = x + 4
        for (label, value), w in zip(items, widths):
            active = value == selected
            if active:
                _rounded(cr, px, y + 4, w, 32, 16)
                cr.set_source_rgba(*accent, 1)
                cr.fill()
            self._text(cr, label, px, y + 12, size, True,
                       (*_on_accent_rgb(accent), 1) if active else (*c["fg"], 0.75), width=w, center=True)
            self._hits.append(((px, y + 4, w, 32), lambda v=value: on_pick(v)))
            px += w
        return sum(widths) + 8

    def _round_button(self, cr, x, y, symbol, c, action, enabled=True, size=34):
        _rounded(cr, x, y, size, size, size / 2)
        cr.set_source_rgba(*c["raised"], 1 if enabled else 0.4)
        cr.fill()
        self._text(cr, symbol, x, y + 3, 15, True, (*c["fg"], 0.9 if enabled else 0.3), width=size, center=True)
        if enabled:
            self._hits.append(((x, y, size, size), action))

    def _draw(self, widget, cr):
        c = self._colors()
        accent = _accent_rgb(widget)
        # Laid out at the "Large" font size; the whole drawing follows the
        # one picked in Settings (and _at() maps the pointer back).
        s = self.app._text_factor()
        W, H = widget.get_allocated_width() / s, widget.get_allocated_height() / s
        if self.anim < 1.0 or self.tab != 0 or widget.get_window() is None:
            self._paint_page(cr, s, W, H, c, accent, live=True)
            self._draw_kb(cr, c)
            return False
        size = (widget.get_allocated_width(), widget.get_allocated_height(), widget.get_scale_factor(), s)
        if self._base is None or self._base[0] != size:
            scale = widget.get_scale_factor()
            surface = image_surface(
                widget.get_window(), cairo.FORMAT_ARGB32, size[0] * scale, size[1] * scale, scale)
            self._paint_page(cairo.Context(surface), s, W, H, c, accent, live=False)
            self._base = (size, surface, self._hits, self._col_hits, self._plot)
        _size, surface, hits, col_hits, self._plot = self._base
        self._hits, self._col_hits = list(hits), col_hits
        cr.set_source_surface(surface, 0, 0)
        cr.paint()
        cr.scale(s, s)
        self._draw_bars(cr, c, accent)
        self._draw_undo(cr, W, H, c, accent)
        self._draw_kb(cr, c)
        return False

    def _paint_page(self, cr, s, W, H, c, accent, live: bool):
        """The page; without its live layer (bars, tooltip, undo) when
        `live` is off - that one goes on top of the cached picture."""
        self._hits, self._col_hits, self._plot = [], [], None
        app = self.app
        cr.set_source_rgb(*c["bg"])
        cr.paint()
        cr.scale(s, s)
        pad = 36
        self._text(cr, app._("dashboard_title"), pad, 28, 19, True, (*c["fg"], 1))
        tabs = [(app._("dashboard_tab_playtime"), 0), (app._("dashboard_tab_storage"), 1)]
        tabs_w = sum(self._measure(cr, t, 11.5)[0] + 34 for t, _v in tabs) + 8
        self._pill(cr, W - pad - tabs_w, 25, tabs, self.tab, c, accent, self.set_tab)
        if self.tab == 0:
            self._draw_sail_time(cr, W, H, pad, c, accent)
            if live:
                self._draw_bars(cr, c, accent)
                self._draw_undo(cr, W, H, c, accent)
        else:
            self._draw_cargo(cr, W, H, pad, c, accent)

    # --- sail time -------------------------------------------------------------------------

    def _draw_sail_time(self, cr, W, H, pad, c, accent):
        app = self.app
        title, buckets = self.buckets()
        _t, previous = self.buckets(self.offset - 1)
        total = sum(b.minutes for b in buckets)
        prev_total = sum(b.minutes for b in previous)
        y = 96
        periods = [(app._(f"playtime_period_{p}"), p) for p in PERIODS]
        x = pad + self._pill(cr, pad, y, periods, self.period, c, accent, self.set_period) + 20
        self._round_button(cr, x, y + 3, "‹", c, lambda: self.step(-1))
        self._round_button(cr, x + 40, y + 3, "›", c, lambda: self.step(1), enabled=self.offset < 0)
        self._text(cr, title, x + 92, y + 10, 12.5, True, (*c["fg"], 0.9), width=W - x - 92 - pad - 50)
        self._round_button(cr, W - pad - 34, y + 3, "", c, self._clear_history,
                           enabled=bool(app.playtime_history))
        trash = get_icon_pixbuf("user-trash-symbolic", 18, self.app._theme_fg_hex())
        if trash is not None:
            cr.save()
            Gdk.cairo_set_source_pixbuf(cr, trash, W - pad - 34 + 8, y + 3 + 8)
            cr.paint_with_alpha(0.85 if app.playtime_history else 0.3)
            cr.restore()

        # hero total + change vs the previous period
        y = 160
        caption = app._(f"dash_sailed_{self.period}") if self.offset == 0 else app._("dash_sailed_period")
        self._text(cr, caption, pad, y, 11, True, (*c["dim"], 1))
        hero = self._hm(total)
        hw, hh = self._measure(cr, hero, 38)
        self._text(cr, hero, pad, y + 18, 38, True, (*c["fg"], 1), width=hw + 10)
        if total or prev_total:
            diff = total - prev_total
            arrow = "▲" if diff > 0 else "▼" if diff < 0 else "="
            self._text(cr, f"{arrow} {self._hm(abs(diff))} {app._('dash_vs_' + self.period)}",
                       pad + hw + 18, y + 18 + hh - 26, 12, True, (*c["dim"], 1), width=500)

        # KPI tiles
        per_game: Dict[str, int] = {}
        for b in buckets:
            for uid, minutes in b.per_game.items():
                per_game[uid] = per_game.get(uid, 0) + minutes
        active = sum(1 for b in buckets if b.minutes)
        busiest = max(buckets, key=lambda b: b.minutes)
        months = self.period == "year"
        if not busiest.minutes:
            busiest_label = "–"
        elif self.period == "week":
            busiest_label = busiest.tick
        elif months:
            busiest_label = app._date_names("date_months")[buckets.index(busiest)]
        else:
            busiest_label = busiest.label
        tiles = [(app._("dash_months_at_sea" if months else "dash_days_at_sea"), f"{active} / {len(buckets)}"),
                 (app._("dash_monthly_avg" if months else "dash_daily_avg"), self._hm(total / len(buckets))),
                 (app._("dash_busiest_month" if months else "dash_busiest_day"), busiest_label),
                 (app._("dash_games_played"), str(len(per_game)))]
        ty = y + 96
        tw = (W - 2 * pad - 3 * 14) / 4
        for i, (label, value) in enumerate(tiles):
            tx = pad + i * (tw + 14)
            _rounded(cr, tx, ty, tw, 80, 16)
            cr.set_source_rgba(*c["raised"], 1)
            cr.fill()
            self._text(cr, label, tx + 18, ty + 14, 10.5, True, (*c["dim"], 1), width=tw - 36)
            self._text(cr, value, tx + 18, ty + 36, 17, True, (*c["fg"], 1), width=tw - 36)

        # columns
        cy = ty + 108
        rows_room = TOP_GAMES * ROW_H + 70
        ch = max(150, min(340, H - cy - rows_room - 40))
        self._draw_columns(cr, pad, cy, W - 2 * pad, ch, buckets, c, accent)

        # most-played games
        ly = cy + ch + 48
        self._text(cr, app._("playtime_top_games"), pad, ly, 13, True, (*c["fg"], 1))
        top = sorted(per_game.items(), key=lambda kv: -kv[1])[:TOP_GAMES]
        if not top:
            self._text(cr, app._("dash_no_sailing"), pad, ly + 32, 11.5, False, (*c["dim"], 1))
        else:
            rows = [(uid, minutes, self._hm(minutes)) for uid, minutes in top]
            self._draw_rows(cr, W, H, pad, ly + 34, rows, top[0][1], c, accent)

    def _draw_columns(self, cr, x0, y0, w, h, buckets: List[Bucket], c, accent):
        values = [b.minutes for b in buckets]
        vmax = max(values + [60])
        step = 60 * max(1, math.ceil(vmax / 60 / 4))         # a gridline every n whole hours
        top = step * math.ceil(vmax / step)
        axis_w = 44
        plot_x, plot_w = x0 + axis_w, w - axis_w
        for v in range(0, top + 1, step):
            gy = int(y0 + h - h * v / top) + 0.5
            cr.set_source_rgba(*c["fg"], 0.08)
            cr.set_line_width(1)
            cr.move_to(plot_x, gy)
            cr.line_to(plot_x + plot_w, gy)
            cr.stroke()
            hours = self.app._("playtime_hm").format(h=v // 60, m=0).split()[0]
            self._text(cr, hours, x0, gy - 9, 9.5, False, (*c["dim"], 1), width=axis_w - 10)
        slot = plot_w / len(buckets)
        for i, b in enumerate(buckets):
            cx = plot_x + slot * i + slot / 2
            if b.tick:
                self._text(cr, b.tick, cx - 30, y0 + h + 8, 9.5, False, (*c["dim"], 1), width=60, center=True)
            self._col_hits.append(((plot_x + slot * i, y0, slot, h + 24), i))
        self._plot = {"area": (x0, y0, w, h), "plot_x": plot_x, "slot": slot, "top": top, "buckets": buckets}

    def _draw_bars(self, cr, c, accent):
        """The live layer of the chart: the bars, dimmed beside the one
        under the pointer, and its tooltip."""
        plot = self._plot
        if plot is None:
            return
        x0, y0, w, h = plot["area"]
        buckets, slot = plot["buckets"], plot["slot"]
        bar = min(24, slot * 0.62)
        grow = 1 - (1 - self.anim) ** 3
        for i, b in enumerate(buckets):
            if b.minutes:
                cx = plot["plot_x"] + slot * i + slot / 2
                bh = h * b.minutes / plot["top"] * grow
                _column(cr, cx - bar / 2, y0 + h - bh, bar, bh, 4)
                cr.set_source_rgba(*accent, 1.0 if self.hover in (None, i) else 0.45)
                cr.fill()
        if self.hover is not None and self.hover < len(buckets):
            self._draw_tooltip(cr, buckets[self.hover], plot["plot_x"] + slot * self.hover + slot / 2,
                               x0, y0, w, c, accent)

    def _draw_tooltip(self, cr, b: Bucket, cx, x0, y0, w, c, accent):
        value = self._hm(b.minutes)
        vw, vh = self._measure(cr, value, 13)
        lw, lh = self._measure(cr, b.label, 10, False)
        games = sorted(b.per_game.items(), key=lambda kv: -kv[1])[:3]
        box_w = max(vw, lw, 200) + 28
        box_h = vh + lh + 26 + 18 * len(games)
        bx = min(max(x0, cx - box_w / 2), x0 + w - box_w)
        by = y0 - 10
        _rounded(cr, bx, by, box_w, box_h, 12)
        cr.set_source_rgba(*c["alt"], 0.98)
        cr.fill_preserve()
        cr.set_source_rgba(*accent, 0.4)
        cr.set_line_width(1)
        cr.stroke()
        self._text(cr, value, bx + 14, by + 10, 13, True, (*c["fg"], 1), width=box_w - 28)     # value leads
        self._text(cr, b.label, bx + 14, by + 12 + vh, 10, False, (*c["dim"], 1), width=box_w - 28)
        for k, (uid, minutes) in enumerate(games):
            gy = by + 18 + vh + lh + k * 18
            cr.set_source_rgba(*accent, 1)
            cr.rectangle(bx + 14, gy + 7, 10, 2)          # a line key, not a box
            cr.fill()
            self._text(cr, self._title_of(uid)[0], bx + 30, gy, 9.5, False, (*c["fg"], 0.85), width=box_w - 110)
            self._text(cr, self._hm(minutes), bx + box_w - 84, gy, 9.5, True, (*c["fg"], 1), width=70)

    def _draw_rows(self, cr, W, H, pad, y, rows, best, c, accent):
        """Icon, title, launcher, a bar against the leader, the value."""
        name_w = 280
        for i, (uid, amount, value) in enumerate(rows):
            ry = y + i * ROW_H
            if ry + ROW_H > H - 8:
                break
            title, launcher = self._title_of(uid)
            # Uninstalled since: still counted, but drawn faded, with a tag.
            removed = uid not in self.app.game_index
            fade = 0.5 if removed else 1.0
            # at the drawing's scale, so it stays sharp
            k = self.app._text_factor() * ui_scale()     # and the screen's own pixels
            icon = self._icon(uid, title, round(40 * k))
            iw, ih = logical_size(icon)[0] / k, logical_size(icon)[1] / k
            cr.save()
            cr.translate(pad + (40 - iw) / 2, ry + 2 + (40 - ih) / 2)
            cr.scale(1 / k, 1 / k)
            Gdk.cairo_set_source_pixbuf(cr, icon, 0, 0)
            cr.paint_with_alpha(fade)
            cr.restore()
            self._text(cr, title, pad + 54, ry + 4, 11.5, True, (*c["fg"], 0.6 if removed else 1), width=name_w)
            sub_x = pad + 54
            if removed:
                tag = self.app._("dashboard_removed_game")
                tw, th = self._measure(cr, tag, 8, True)
                _rounded(cr, sub_x, ry + 24, tw + 12, th + 2, (th + 2) / 2)
                cr.set_source_rgba(*c["dim"], 0.22)
                cr.fill()
                self._text(cr, tag, sub_x + 6, ry + 25, 8, True, (*c["fg"], 0.75), width=tw + 4)
                sub_x += tw + 20
            self._text(cr, launcher, sub_x, ry + 24, 10, False, (*c["dim"], 1), width=name_w - (sub_x - pad - 54))
            bx = pad + 54 + name_w + 16
            bw = W - pad - bx - 120
            _rounded(cr, bx, ry + 17, bw, 10, 5)
            cr.set_source_rgba(*accent, 0.14)
            cr.fill()
            _rounded(cr, bx, ry + 17, max(10, bw * amount / max(1, best) * self.anim), 10, 5)
            cr.set_source_rgba(*accent, fade)
            cr.fill()
            vw, _ = self._measure(cr, value, 12)
            self._text(cr, value, W - pad - vw, ry + 12, 12, True, (*c["fg"], 0.6 if removed else 1), width=vw + 4)

    def _draw_undo(self, cr, W, H, c, accent):
        """The main window's undo toast, drawn: a pill with the message
        and an outlined Undo button that fills in under the pointer."""
        self._undo_rect = None
        if self._undo is None or time.monotonic() > self._undo[1]:
            return
        text = self.app._("playtime_reset_toast_text")
        undo = self.app._("undo_btn")
        tw, th = self._measure(cr, text, 11.5)
        uw, _ = self._measure(cr, undo, 11.5)
        bar_h = th + 28
        btn_w, btn_h = uw + 40, bar_h - 12
        box_w = 22 + tw + 18 + btn_w + 6
        x, y = (W - box_w) / 2, H - bar_h - 28
        _rounded(cr, x + 2, y + 5, box_w, bar_h, bar_h / 2)
        cr.set_source_rgba(0, 0, 0, 0.35)
        cr.fill()
        _rounded(cr, x, y, box_w, bar_h, bar_h / 2)
        cr.set_source_rgba(*c["alt"], 0.98)
        cr.fill_preserve()
        cr.set_source_rgba(*accent, 0.35)
        cr.set_line_width(1)
        cr.stroke()
        self._text(cr, text, x + 22, y + (bar_h - th) / 2, 11.5, True, (*c["fg"], 1), width=tw + 4)
        bx, by = x + box_w - 6 - btn_w, y + 6
        self._undo_rect = (bx, by, btn_w, btn_h)
        hovered = self._over_undo()
        _rounded(cr, bx, by, btn_w, btn_h, btn_h / 2)
        if hovered:
            cr.set_source_rgba(*accent, 1)
            cr.fill()
        else:
            cr.set_source_rgba(*accent, 1)
            cr.set_line_width(1.4)
            cr.stroke()
        self._text(cr, undo, bx, by + (btn_h - th) / 2, 11.5, True,
                   (*_on_accent_rgb(accent), 1) if hovered else (*accent, 1), width=btn_w, center=True)
        self._hits.append((self._undo_rect, self._undo_clear))

    # --- cargo hold -----------------------------------------------------------------------------

    def _draw_cargo(self, cr, W, H, pad, c, accent):
        app = self.app
        if self.storage is None:
            self._text(cr, app._("dash_measuring"), pad, 120, 13, True, (*c["dim"], 1), width=W - 2 * pad)
            return
        games, drives = self.storage
        if not games:
            self._text(cr, app._("storage_no_games"), pad, 120, 13, True, (*c["dim"], 1), width=W - 2 * pad)
            return
        total = sum(size for _g, size in games)
        y = 100
        self._text(cr, app._("dash_on_board"), pad, y, 11, True, (*c["dim"], 1))
        hero = format_bytes(total)
        hw, hh = self._measure(cr, hero, 38)
        self._text(cr, hero, pad, y + 18, 38, True, (*c["fg"], 1), width=hw + 10)
        self._text(cr, app._("dash_across_games").format(n=len(games)), pad + hw + 18, y + 18 + hh - 26, 12,
                   True, (*c["dim"], 1), width=400)
        dy = y + 92
        for mount, size, used, games_bytes in drives:
            _rounded(cr, pad, dy, W - 2 * pad, 96, 16)
            cr.set_source_rgba(*c["raised"], 1)
            cr.fill()
            self._text(cr, mount, pad + 20, dy + 16, 12, True, (*c["fg"], 1), width=W / 2)
            free_text = app._("dash_free_of").format(free=format_bytes(size - used), total=format_bytes(size))
            fw, _ = self._measure(cr, free_text, 11)
            self._text(cr, free_text, W - pad - 20 - fw, dy + 16, 11, True, (*c["dim"], 1), width=fw + 4)
            mx, mw = pad + 20, W - 2 * pad - 40
            other = max(0, used - games_bytes)
            cr.save()
            _rounded(cr, mx, dy + 50, mw, 14, 7)
            cr.set_source_rgba(*accent, 0.14)
            cr.fill_preserve()
            cr.clip()
            gw = mw * games_bytes / max(1, size) * self.anim
            ow = mw * other / max(1, size) * self.anim
            cr.set_source_rgba(*accent, 1)
            cr.rectangle(mx, dy + 50, gw, 14)
            cr.fill()
            cr.set_source_rgba(*c["dim"], 0.55)
            cr.rectangle(mx + gw + 2, dy + 50, max(0, ow - 2), 14)      # 2px surface gap between fills
            cr.fill()
            cr.restore()
            lx = mx
            for color, label in (((*accent, 1), app._("dash_games_legend").format(size=format_bytes(games_bytes))),
                                 ((*c["dim"], 0.55), app._("dash_else_legend").format(size=format_bytes(other)))):
                cr.set_source_rgba(*color)
                _rounded(cr, lx, dy + 74, 10, 10, 2)
                cr.fill()
                lw, _ = self._measure(cr, label, 10, False)
                self._text(cr, label, lx + 16, dy + 70, 10, False, (*c["dim"], 1), width=lw + 4)
                lx += lw + 40
            dy += 110
        ly = dy + 16
        self._text(cr, app._("dash_biggest"), pad, ly, 13, True, (*c["fg"], 1))
        top = games[:BIGGEST_GAMES]
        rows = [(game.uid, size, format_bytes(size)) for game, size in top]
        self._draw_rows(cr, W, H, pad, ly + 34, rows, top[0][1], c, accent)
