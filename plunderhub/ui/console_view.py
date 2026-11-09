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

"""Console Mode's screen: the selected game's cover as a poster behind a
big info block, shelves of covers below, the controller legend at the
bottom. Drawn by hand so the selection, the shelves and the backdrop can
all glide; driven by ConsoleModeMixin (controller, keyboard) and the mouse.

Everything over the backdrop - top bar, clock, info block, shelves,
legend - is kept as one image while nothing of it moves, so an animated
hero's frames redraw only the backdrop and lay that image over it. Any
queue_draw() (a move, the clock's tick, a change in the library) makes
it again; the hero's own frames come in by queue_draw_area() and don't.
The glides step on the app's common beat (App._bubble_beat_frames), by
elapsed time."""

import colorsys
import math
import time
from typing import Callable, Dict, List, Optional, Tuple

import cairo

from plunderhub.constants import CONSOLE_UI_SCALE_CSS_PERCENT, CONTROLLER_GLYPH_TEXT
from plunderhub.core.models import GameItem
from plunderhub.gi_setup import Gdk, GdkPixbuf, GLib, Gtk, image_surface
from plunderhub.logging_setup import logger
from plunderhub.ui.animated_hero import argb_surface
from plunderhub.theme import THEME_PALETTES, _app_icon_file, logo_accent, logo_pixbuf
from plunderhub.ui.home_mixin import (
    _accent_rgb, _draw_hero_glyph, _on_accent_rgb, _paint_hero_light, _paint_name_plate, _pango_text, _rounded,
    _title_gradient, _title_hue,
)

DANGER = (0.90, 0.33, 0.29)
STAR = (0.89, 0.70, 0.25)
# Face-button colours, as on the pads themselves.
FACE_COLORS = {
    "xbox": {"activate": (0.42, 0.75, 0.27), "back": (0.90, 0.33, 0.29),
             "favorite": (0.25, 0.55, 0.95), "sort": (0.95, 0.77, 0.20)},
    "sony": {"activate": (0.45, 0.62, 0.98), "back": (0.93, 0.36, 0.40),
             "favorite": (0.85, 0.50, 0.85), "sort": (0.30, 0.80, 0.68)},
    # keys: keycaps, drawn like the shoulder buttons
    "keyboard": {},
}
GROW = 0.12      # how much the selected cover grows
RECENT_SHELF = 10


class ConsoleView(Gtk.DrawingArea):
    def __init__(self, app):
        super().__init__()
        self.app = app
        self.tab: Optional[str] = None           # launcher name, None = all
        self.shelf = 0
        self.index: Dict[str, int] = {}          # shelf title -> selected index
        self.scroll_x: Dict[str, float] = {}
        self.scroll_y = 0.0
        self.sel_anim = 1.0
        self.bg_prev: Optional[GameItem] = None
        self.bg_cur: Optional[GameItem] = None
        self.bg_t = 1.0
        self._shelves: List[Tuple[str, List[GameItem]]] = []
        self._art: Dict[tuple, Optional[GdkPixbuf.Pixbuf]] = {}
        self._hits: List[Tuple[Tuple[float, float, float, float], Callable[[], None]]] = []
        self._toast = ("", 0.0)
        self._settled = True
        self._anim_id: Optional[int] = None
        self._anim_last: Optional[int] = None
        # where the glides are heading, worked out as the shelves are drawn
        self._target_y = 0.0
        self._target_x: Dict[str, float] = {}
        # everything over the backdrop, kept while it's still (see above)
        self._overlay: Optional[cairo.ImageSurface] = None
        self._overlay_dirty = True
        # each game's whole backdrop - colour, poster, shade - made once (a
        # playing hero's: once a frame): a glide redraws only what's over
        # it, and a switch fades between two of them (each layer of it was
        # a full-screen paint, four or five a frame)
        self._posters: Dict[tuple, cairo.ImageSurface] = {}
        self._clock_id: Optional[int] = None
        self.set_can_focus(True)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("draw", self._draw)
        self.connect("button-press-event", self._on_click)
        from plunderhub.ui.animated_hero import HeroAnimation
        # the current game's hero, if it moves - only the poster's part
        # of the screen is redrawn for its frames
        self._hero_anim = HeroAnimation(app, self, lambda: (0, 0, self.get_allocated_width(),
                                                           int(self.get_allocated_height() * 0.72)))
        self.connect("map", lambda *_: self._start_clock())
        self.connect("unmap", lambda *_: (self._stop_clock(), self._let_go()))

    # --- data ----------------------------------------------------------------

    def tabs(self) -> List[Optional[str]]:
        return [None] + [s["name"] for s in self.app._cached_launchers if self._visible(s["games"])]

    def _visible(self, games) -> List[GameItem]:
        return [g for g in games if not self.app.scanner.is_hidden(g.launcher, g.uid)]

    def refresh(self, keep_uid: Optional[str] = None):
        """Rebuilds the shelves from the library (after a scan, a launch,
        a favourite or sort change), keeping the selected game selected."""
        if self._clock_id is not None:      # a game started / ended: the clock's beat follows
            self._stop_clock()
            self._start_clock()
        app = self.app
        keep_uid = keep_uid or (self.selected().uid if self.selected() else None)
        if self.tab not in self.tabs():
            self.tab = None
        games = []
        for section in app._cached_launchers:
            if self.tab is None or section["name"] == self.tab:
                games.extend(self._visible(section["games"]))
        shelves = []
        recent = sorted((g for g in games if app.recent.get(g.uid) and g.store_platform != "store-launcher"),
                        key=lambda g: -app.recent.get(g.uid, 0))
        if recent:
            shelves.append((app._("home_nav_recent"), recent[:RECENT_SHELF]))
        # a launcher's tab in its own order, all of them in All Plunder's
        per_launcher = self.tab is not None
        favorites = app._sort_games([g for g in games if g.uid in app.favorites], per_launcher)
        if favorites:
            shelves.append((app._("home_nav_favorites"), favorites))
        harbors = [g for g in games if g.store_platform == "store-launcher"]
        plunder = [g for g in games if g.store_platform != "store-launcher"]
        if plunder:
            shelves.append((self.tab or app._("all_games_section"), app._sort_games(plunder, per_launcher)))
        if harbors:
            shelves.append((app._("section_harbors"), app._sort_games(harbors, per_launcher)))
        self._shelves = shelves
        if keep_uid:
            # stay on the same game - on the shelf the cursor was on, if it's still there
            order = [self.shelf] + [i for i in range(len(shelves)) if i != self.shelf]
            for si in order:
                if si < len(shelves):
                    uids = [g.uid for g in shelves[si][1]]
                    if keep_uid in uids:
                        self.shelf = si
                        self.index[shelves[si][0]] = uids.index(keep_uid)
                        break
        self.shelf = max(0, min(self.shelf, len(shelves) - 1))
        self._selection_changed(user=False)

    def selected(self) -> Optional[GameItem]:
        if not self._shelves:
            return None
        name, games = self._shelves[min(self.shelf, len(self._shelves) - 1)]
        if not games:
            return None
        i = max(0, min(self.index.get(name, 0), len(games) - 1))
        self.index[name] = i
        return games[i]

    # --- actions (controller / keyboard / mouse) ------------------------------

    def move(self, direction: str):
        if not self._shelves:
            return
        name, games = self._shelves[self.shelf]
        i = self.index.get(name, 0)
        if direction == "right" and i < len(games) - 1:
            self.index[name] = i + 1
        elif direction == "left" and i > 0:
            self.index[name] = i - 1
        elif direction == "down" and self.shelf < len(self._shelves) - 1:
            self.shelf += 1
        elif direction == "up" and self.shelf > 0:
            self.shelf -= 1
        else:
            return
        self._selection_changed(user=True)

    def move_to_end(self, last: bool):
        """Home / End: the shelf's first or last game."""
        if not self._shelves:
            return
        name, games = self._shelves[self.shelf]
        i = len(games) - 1 if last else 0
        if games and self.index.get(name, 0) != i:
            self.index[name] = i
            self._selection_changed(user=True)

    def shift_tab(self, step: int):
        tabs = self.tabs()
        try:
            i = tabs.index(self.tab)
        except ValueError:
            i = 0
        self.set_tab(tabs[(i + step) % len(tabs)])

    def set_tab(self, name: Optional[str]):
        if name == self.tab:
            return
        self.tab = name
        self.shelf = 0
        self.index.clear()
        self.scroll_x.clear()
        self.refresh(keep_uid=None)

    def toast(self, text: str):
        self._toast = (text, time.monotonic() + 2.2)
        self._animate()

    def _selection_changed(self, user: bool):
        game = self.selected()
        if game is not self.bg_cur:
            self.bg_prev, self.bg_cur, self.bg_t = self.bg_cur, game, 0.0
            self.sel_anim = 0.0
        if user and game is not None:
            self.app._clear_new_game(game.uid)
        self._animate()

    # --- animation --------------------------------------------------------------

    def queue_draw(self):
        """Everything over the backdrop is drawn again too (see the module's
        notes) - unlike an animated hero's frames, which ask for their part
        by queue_draw_area()."""
        self._overlay_dirty = True
        super().queue_draw()

    def _animate(self):
        if self._anim_id is None:
            self._anim_last = None
            self._anim_id = self.add_tick_callback(self._tick)
        self.queue_draw()

    def _tick(self, _widget, frame_clock) -> bool:
        if (self._anim_last is not None
                and frame_clock.get_frame_counter() % self.app._bubble_beat_frames(frame_clock)):
            return True
        now = frame_clock.get_frame_time()
        # in 60 fps frames of elapsed time - the same pace at any rate
        frames = min(4.0, (now - self._anim_last) / 1_000_000 * 60) if self._anim_last is not None else 1.0
        self._anim_last = now
        self.bg_t = min(1.0, self.bg_t + 0.06 * frames)
        self.sel_anim = min(1.0, self.sel_anim + 0.12 * frames)
        settled = True
        k = 1 - (1 - 0.18) ** frames
        if abs(self._target_y - self.scroll_y) > 0.5:
            self.scroll_y += (self._target_y - self.scroll_y) * k
            settled = False
        else:
            self.scroll_y = self._target_y
        k = 1 - (1 - 0.2) ** frames
        for name, want in self._target_x.items():
            cur = self.scroll_x.get(name, want)
            if abs(want - cur) > 0.5:
                self.scroll_x[name] = cur + (want - cur) * k
                settled = False
            else:
                self.scroll_x[name] = want
        self._settled = settled
        self.queue_draw()
        if (self.bg_t >= 1 and self.sel_anim >= 1 and settled
                and time.monotonic() > self._toast[1]):
            self._anim_id = None
            return False
        return True

    def _let_go(self):
        """Out of Console Mode: its full-screen images go, and its hero's
        stream (started again when it shows)."""
        self._hero_anim.set(None, 0, 0)
        self._posters.clear()
        # all of it: kept, every game browsed here held its full-screen art
        self._art.clear()
        self._overlay = None
        self._overlay_dirty = True
        from plunderhub.ui import memory
        memory.release_soon()

    def _start_clock(self):
        # The clock and a running game's timer; nothing else moves at rest.
        # Every second only while a game runs (its session timer); else at
        # the next minute - the overlay is rebuilt whole at each draw.
        if self._clock_id is None:
            self._clock_tick(first=True)

    def _clock_tick(self, first: bool = False) -> bool:
        if not first:
            self.queue_draw()
        if self.app._any_game_running():
            delay = 1000
        else:
            delay = int((60 - time.time() % 60) * 1000) + 20
        self._clock_id = GLib.timeout_add(delay, self._clock_tick)
        return False

    def _stop_clock(self):
        if self._clock_id is not None:
            GLib.source_remove(self._clock_id)
            self._clock_id = None

    # --- art ---------------------------------------------------------------------

    def _source(self, game: GameItem) -> Optional[GdkPixbuf.Pixbuf]:
        key = ("src", game.uid)
        if key not in self._art:
            pb = None
            path = self.app._find_cached_artwork(self.app.covers_dir, game.uid) if self.app.show_covers else None
            if path is not None:
                try:
                    pb = GdkPixbuf.PixbufAnimation.new_from_file(str(path)).get_static_image()
                except Exception as e:
                    logger.debug(f"Console Mode couldn't load the cover of {game.uid}: {e}")
            self._art[key] = pb
        return self._art[key]

    def _cover(self, game: GameItem, w: int, h: int, d: float = 1.0) -> Optional[cairo.ImageSurface]:
        """The cover cut to w x h (logical), as a surface - made once, not
        converted from a pixbuf on every frame of a glide. d is the pixel
        density it's drawn at (window scale, x 1+GROW on a card): its
        pixels are w*d x h*d, so a 2x screen doesn't stretch a 1x picture."""
        key = ("cover", game.uid, w, h, d)
        if key not in self._art:
            src = self._source(game)
            surface = None
            pw, ph = max(1, math.ceil(w * d)), max(1, math.ceil(h * d))
            if src is not None and w > 0 and h > 0:
                s = max(pw / src.get_width(), ph / src.get_height())
                sw, sh = int(src.get_width() * s) + 1, int(src.get_height() * s) + 1
                surface = argb_surface(src.scale_simple(sw, sh, GdkPixbuf.InterpType.BILINEAR).new_subpixbuf(
                    (sw - pw) // 2, (sh - ph) // 2, pw, ph))
                surface.set_device_scale(d, d)
            self._art[key] = surface
        return self._art[key]

    def forget_art(self, uid: Optional[str] = None):
        """Drops cached art - all of it, or one game's (new cover or hero)."""
        self._hero_anim.set(None, 0, 0)          # picked up again on the next draw
        self._posters.clear()
        if uid is None:
            self._art.clear()
        else:
            for key in [k for k in self._art if k[1] == uid]:
                del self._art[key]
        self.queue_draw()

    def _paint_cover(self, cr, game: GameItem, w: int, h: int, alpha: float = 1.0, d: float = 1.0):
        if not self.app.show_covers:
            # the library's name plate
            meta = (self.app._home_playtime_text(game.uid) or None) if self.app.show_playtime else None
            _paint_name_plate(cr, self, game.title, meta, w, h, alpha)
            return
        surface = self._cover(game, w, h, d)
        if surface is not None:
            cr.set_source_surface(surface, 0, 0)
            if alpha >= 1:
                cr.paint()
            else:
                cr.paint_with_alpha(alpha)
            return
        hue = _title_hue(game.title)     # the library's no-cover card has the same colour
        grad = cairo.LinearGradient(0, 0, w, h)
        grad.add_color_stop_rgba(0, *colorsys.hsv_to_rgb(hue, 0.55, 0.55), alpha)
        grad.add_color_stop_rgba(1, *colorsys.hsv_to_rgb((hue + 0.12) % 1, 0.7, 0.22), alpha)
        cr.set_source(grad)
        cr.paint()
        _pango_text(cr, self, game.title, 14, h * 0.36, w - 28, max(12.0, w / 13), True,
                    (1, 1, 1, 0.94 * alpha), lines=3)

    # --- mouse --------------------------------------------------------------------

    def _on_click(self, _w, event) -> bool:
        if event.button != 1 or event.type != Gdk.EventType.BUTTON_PRESS:
            return False
        self.grab_focus()
        for (x, y, w, h), action in reversed(self._hits):
            if x <= event.x <= x + w and y <= event.y <= y + h:
                action()
                return True
        return False

    def _select_at(self, shelf: int, index: int):
        if shelf >= len(self._shelves) or index >= len(self._shelves[shelf][1]):
            return      # a hit from before a refresh: the next draw has the new ones
        name = self._shelves[shelf][0]
        if shelf == self.shelf and self.index.get(name) == index:
            self.app._console_activate()
            return
        self.shelf = shelf
        self.index[name] = index
        self._selection_changed(user=True)

    # --- drawing ------------------------------------------------------------------

    def _scale(self) -> float:
        """The picked size - smaller on a screen under 1080 logical pixels
        high (a laptop at 150-200 %), where the hero's text would run into
        the shelves; positions are fractions of the height already."""
        fit = min(1.0, max(0.5, self.get_allocated_height() / 1080)) if self.get_allocated_height() > 1 else 1.0
        return CONSOLE_UI_SCALE_CSS_PERCENT.get(self.app.console_ui_scale, 100) / 100 * fit

    def _bg_rgb(self) -> Tuple[float, float, float]:
        palette = THEME_PALETTES.get(self.app.theme)
        if palette is not None:
            h = palette["bg"].lstrip("#")
            return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
        found, color = self.get_style_context().lookup_color("theme_bg_color")
        return (color.red, color.green, color.blue) if found else (0.03, 0.10, 0.11)

    def _draw(self, widget, cr):
        W, H = widget.get_allocated_width(), widget.get_allocated_height()
        window = widget.get_window()
        bg = self._bg_rgb()
        if window is None:
            self._paint_scene(cr, self.bg_cur, W, H, bg)
        else:
            self._draw_backdrop(cr, window, W, H, bg)

        moving = self._anim_id is not None
        overlay = self._overlay
        if moving or window is None:
            # gliding: straight on, as it changes every frame anyway
            self._overlay = None
            self._draw_overlay(cr, W, H)
            return False
        scale = window.get_scale_factor()
        if (self._overlay_dirty or overlay is None
                or (overlay.get_width(), overlay.get_height()) != (W * scale, H * scale)):
            if overlay is None or (overlay.get_width(), overlay.get_height()) != (W * scale, H * scale):
                overlay = self._overlay = image_surface(window, cairo.FORMAT_ARGB32, W * scale, H * scale, scale)
            ocr = cairo.Context(overlay)
            ocr.set_operator(cairo.OPERATOR_CLEAR)
            ocr.paint()
            ocr.set_operator(cairo.OPERATOR_OVER)
            from gi.repository import PangoCairo
            options = PangoCairo.context_get_font_options(widget.get_pango_context())
            if options is not None:
                ocr.set_font_options(options)
            self._draw_overlay(ocr, W, H)
            overlay.flush()
            self._overlay_dirty = False
        cr.set_source_surface(overlay, 0, 0)
        cr.paint()
        return False

    def _scene(self, window, game: GameItem, W: int, H: int, bg) -> cairo.ImageSurface:
        """game's whole backdrop - colour, poster, shade - as one image,
        made once (a playing hero's: once a frame, but not mid-switch)."""
        hh = int(H * 0.72)
        hero = self._hero(game, W, hh)
        frame = self._hero_anim.frame() if game is self.bg_cur and hero is not None else None
        key = (game.uid, W, H, window.get_scale_factor(), bg, self.app.show_covers,
               id(frame) if frame is not None else None)
        scene = self._posters.get(key)
        if scene is None and self.bg_t < 1:
            # Mid-switch, the backdrop each game already has - the outgoing
            # one as it was last shown, the incoming one at the frame it
            # came in on: made again for every hero frame and for the
            # outgoing still, a quick run along a shelf drew three or four
            # full-screen backdrops a step.
            scene = next((v for k, v in self._posters.items() if k[:6] == key[:6]), None)
        if scene is None:
            scale = window.get_scale_factor()
            scene = image_surface(window, cairo.FORMAT_RGB24, W * scale, H * scale, scale)
            self._paint_scene(cairo.Context(scene), game, W, H, bg)
            scene.flush()
            # this game's older ones go (a playing hero's last frame), and
            # all but the last few games'
            for old in [k for k in self._posters if k[0] == game.uid]:
                del self._posters[old]
            while len(self._posters) >= 2:
                del self._posters[next(iter(self._posters))]
            self._posters[key] = scene
        return scene

    def _paint_scene(self, cr, game: Optional[GameItem], W: int, H: int, bg):
        cr.set_source_rgb(*bg)
        cr.paint()
        if game is not None:
            self._paint_poster(cr, game, 1.0, W, H, bg)
        shade = cairo.LinearGradient(0, H * 0.35, 0, H)
        shade.add_color_stop_rgba(0, *bg, 0)
        shade.add_color_stop_rgba(1, *bg, 0.96)
        cr.set_source(shade)
        cr.paint()

    def _veil(self, window, W: int, H: int, bg) -> cairo.ImageSurface:
        """Everything of a hero's backdrop but the picture: its shades over
        it (as _paint_poster draws them), the colour below it, the shade
        over all - see-through where the picture shows."""
        key = ("veil", W, H, window.get_scale_factor(), bg)
        veil = self._art.get(key)
        if veil is None:
            for old in [k for k in self._art if k[0] == "veil"]:
                del self._art[old]
            hh = int(H * 0.72)
            scale = window.get_scale_factor()
            veil = image_surface(window, cairo.FORMAT_ARGB32, W * scale, H * scale, scale)
            cr = cairo.Context(veil)
            cr.rectangle(0, hh, W, H - hh)
            cr.set_source_rgb(*bg)
            cr.fill()
            cr.save()
            cr.rectangle(0, 0, W, hh)
            cr.clip()
            left = cairo.LinearGradient(0, 0, W * 0.7, 0)
            left.add_color_stop_rgba(0, *bg, 0.82)
            left.add_color_stop_rgba(0.55, *bg, 0.45)
            left.add_color_stop_rgba(1, *bg, 0)
            cr.set_source(left)
            cr.paint()
            down = cairo.LinearGradient(0, hh * 0.4, 0, hh)
            down.add_color_stop_rgba(0, *bg, 0)
            down.add_color_stop_rgba(1, *bg, 1)
            cr.set_source(down)
            cr.paint()
            cr.restore()
            shade = cairo.LinearGradient(0, H * 0.35, 0, H)
            shade.add_color_stop_rgba(0, *bg, 0)
            shade.add_color_stop_rgba(1, *bg, 0.96)
            cr.set_source(shade)
            cr.paint()
            veil.flush()
            self._art[key] = veil
        return veil

    def _draw_backdrop(self, cr, window, W: int, H: int, bg):
        """The backdrop: the selected game's - faded over from the last
        one's after a move - from the images kept of them; a playing hero's
        drawn straight (each of its frames a new one anyway)."""
        cur = self.bg_cur
        if cur is not None:
            # the hero takes up its size and file here, if it plays
            hh = int(H * 0.72)
            moving = self._hero(cur, W, hh) is not None
            self._hero_anim.set(self._hero_motion(cur) if moving else None, W, hh)
        if self.bg_t >= 1 and self._hero_anim.frame() is not None:
            # A playing hero: its frame, and over it all the rest of the
            # backdrop from one image kept of it (_veil) - five full-screen
            # layers a frame before.
            hh = int(H * 0.72)
            cr.save()
            cr.rectangle(0, 0, W, hh)
            cr.clip()
            cr.set_source_rgb(*bg)
            cr.paint()
            self._hero_anim.paint(cr, W, hh)
            cr.restore()
            cr.set_source_surface(self._veil(window, W, H, bg), 0, 0)
            cr.paint()
            return
        if cur is None:
            self._paint_scene(cr, None, W, H, bg)
            return
        if self.bg_t < 1:
            prev = self._scene(window, self.bg_prev, W, H, bg) if self.bg_prev is not None else None
            if prev is not None:
                cr.set_source_surface(prev, 0, 0)
            else:
                cr.set_source_rgb(*bg)
            cr.paint()
            cr.set_source_surface(self._scene(window, cur, W, H, bg), 0, 0)
            cr.paint_with_alpha(self.bg_t)
            return
        cr.set_source_surface(self._scene(window, cur, W, H, bg), 0, 0)
        cr.paint()

    def _draw_overlay(self, cr, W: int, H: int):
        """Everything over the backdrop."""
        self._hits = []
        s = self._scale()
        accent = _accent_rgb(self)
        pad = max(48, W * 0.045)
        self._draw_topbar(cr, W, pad, s, accent)
        game = self.selected()
        if game is None:
            _pango_text(cr, self, self.app._("no_games"), pad, H * 0.4, W - 2 * pad, 22 * s, True, (1, 1, 1, 0.8))
        else:
            self._draw_hero(cr, W, H, pad, s, accent, game)
            self._draw_shelves(cr, W, H, pad, s, accent)
            self._draw_legend(cr, W, H, pad, s, game)
        self._draw_toast(cr, W, H, s, accent)

    def _hero(self, game: GameItem, w: int, h: int) -> Optional[cairo.ImageSurface]:
        """The game's SteamGridDB hero, filling w x h (cut to it, a little
        above the middle), or None without one. Animated heroes show their
        first frame."""
        d = self.get_scale_factor()        # w x h logical, the screen's own pixels
        key = ("hero", game.uid, w, h, d)
        if key not in self._art:
            pb = None
            path = self.app.hero_path(game.uid) if self.app.show_covers else None
            if path is not None and w > 0 and h > 0:
                try:
                    src = GdkPixbuf.PixbufAnimation.new_from_file(str(path)).get_static_image()
                    s = max(w * d / src.get_width(), h * d / src.get_height())
                    sw, sh = int(src.get_width() * s) + 1, int(src.get_height() * s) + 1
                    pb = argb_surface(src.scale_simple(sw, sh, GdkPixbuf.InterpType.BILINEAR).new_subpixbuf(
                        (sw - w * d) // 2, int((sh - h * d) * 0.3), w * d, h * d))
                    pb.set_device_scale(d, d)
                except Exception as e:
                    logger.debug(f"Console Mode couldn't load the hero of {game.uid}: {e}")
            # Only one size of a hero is ever on screen: older sizes go -
            # and only the last few games' (one is ~14 MB at 3440x1440)
            for old in [k for k in self._art if k[0] == "hero" and k[1] == game.uid]:
                del self._art[old]
            heroes = [k for k in self._art if k[0] == "hero"]
            for old in heroes[:max(0, len(heroes) - 3)]:
                del self._art[old]
            self._art[key] = pb
        return self._art[key]

    def _hero_motion(self, game: GameItem):
        """The file the game's hero moves from, if it's animated (looked up
        once: a playing hero draws every frame)."""
        key = ("motion", game.uid)
        if key not in self._art:
            self._art[key] = self.app._art_animation(self.app.hero_path(game.uid))
        return self._art[key]

    def _paint_poster(self, cr, game: GameItem, alpha: float, W: int, H: int, bg):
        """The game's hero across the top, darkened behind the text on the
        left and melting into the background downwards. Without a hero,
        its cover, sharp, on the right."""
        hh = int(H * 0.72)
        hero = self._hero(game, W, hh)
        moving = game is self.bg_cur and hero is not None
        if game is self.bg_cur:
            self._hero_anim.set(self._hero_motion(game) if moving else None, W, hh)
        if hero is not None:
            # Made whole first, then faded in as one: fading the picture
            # and its shades separately let the outgoing and incoming
            # heroes show through each other's shade mid-switch.
            cr.save()
            cr.rectangle(0, 0, W, hh)
            cr.clip()
            # Only a switch needs the extra pass: fully shown, the picture
            # and its shades go straight down (an animated hero draws
            # this every frame).
            fading = alpha < 1
            if fading:
                cr.push_group()
            if not (moving and self._hero_anim.paint(cr, W, hh)):
                cr.set_source_surface(hero, 0, 0)
                cr.paint()
            # the title and buttons stay readable over any picture
            left = cairo.LinearGradient(0, 0, W * 0.7, 0)
            left.add_color_stop_rgba(0, *bg, 0.82)
            left.add_color_stop_rgba(0.55, *bg, 0.45)
            left.add_color_stop_rgba(1, *bg, 0)
            cr.set_source(left)
            cr.paint()
            down = cairo.LinearGradient(0, hh * 0.4, 0, hh)
            down.add_color_stop_rgba(0, *bg, 0)
            down.add_color_stop_rgba(1, *bg, 1)
            cr.set_source(down)
            cr.paint()
            if fading:
                cr.pop_group_to_source()
                cr.paint_with_alpha(alpha)
            cr.restore()
            return
        if not self.app.show_covers:
            # no art at all: the game's colours wash in from the top right
            cr.save()
            cr.rectangle(0, 0, W, hh)
            cr.clip()
            cr.push_group()
            _title_gradient(cr, game.title, W, hh)
            left = cairo.LinearGradient(0, 0, W * 0.8, 0)
            left.add_color_stop_rgba(0, *bg, 0.9)
            left.add_color_stop_rgba(1, *bg, 0.2)
            cr.set_source(left)
            cr.paint()
            down = cairo.LinearGradient(0, hh * 0.3, 0, hh)
            down.add_color_stop_rgba(0, *bg, 0)
            down.add_color_stop_rgba(1, *bg, 1)
            cr.set_source(down)
            cr.paint()
            cr.pop_group_to_source()
            cr.paint_with_alpha(alpha)
            cr.restore()
            return
        ph = int(H * 0.95)
        pw = int(ph * 2 / 3)
        x0 = W - pw
        cr.save()
        cr.translate(x0, 0)
        cr.push_group()
        self._paint_cover(cr, game, pw, ph, d=self.get_scale_factor())
        pattern = cr.pop_group()
        fade = cairo.LinearGradient(0, 0, pw * 0.55, 0)
        fade.add_color_stop_rgba(0, 0, 0, 0, 0)
        fade.add_color_stop_rgba(1, 0, 0, 0, alpha)
        cr.set_source(pattern)
        cr.mask(fade)
        down = cairo.LinearGradient(0, ph * 0.45, 0, ph)
        down.add_color_stop_rgba(0, *bg, 0)
        down.add_color_stop_rgba(1, *bg, alpha)
        cr.set_source(down)
        cr.rectangle(0, 0, pw, H)
        cr.fill()
        cr.restore()

    def _glyph(self, cr, role: str, x: float, cy: float, size: float, family: Optional[str] = None) -> float:
        """A controller button: its symbol in the pad's own colour, or a
        rounded bar for a shoulder button or a key. Returns the width it took.
        family: that controller's instead of the one in use."""
        app = self.app
        family = family or app._controller_family
        sym = CONTROLLER_GLYPH_TEXT.get(family, CONTROLLER_GLYPH_TEXT["xbox"])[role]
        color = FACE_COLORS.get(family, FACE_COLORS["xbox"]).get(role)
        if family == "keyboard":
            # a keycap: square for one letter, wider for Enter / Esc
            w, h = self._measure(cr, sym, size * 0.72, True)
            kh = h + 8
            kw = max(w + 16, kh)
            _rounded(cr, x, cy - kh / 2, kw, kh, min(6, kh / 4))
            cr.set_source_rgba(1, 1, 1, 0.14)
            cr.fill_preserve()
            cr.set_source_rgba(1, 1, 1, 0.38)
            cr.set_line_width(1)
            cr.stroke()
            _pango_text(cr, self, sym, x + (kw - w) / 2, cy - h / 2, w + 4, size * 0.72, True, (1, 1, 1, 0.95))
            return kw
        if color is None:
            w, h = self._measure(cr, sym, size * 0.62, True)
            _rounded(cr, x, cy - h / 2 - 3, w + 16, h + 6, (h + 6) / 2)
            cr.set_source_rgba(1, 1, 1, 0.16)
            cr.fill()
            _pango_text(cr, self, sym, x + 8, cy - h / 2, w + 4, size * 0.62, True, (1, 1, 1, 0.95))
            return w + 16
        w, h = self._measure(cr, sym, size, True)
        _pango_text(cr, self, sym, x, cy - h / 2, w + 4, size, True, (*color, 1))
        return w

    def _glyph_width(self, cr, role: str, size: float, family: Optional[str] = None) -> float:
        """How wide _glyph draws role - for laying out around it first."""
        family = family or self.app._controller_family
        sym = CONTROLLER_GLYPH_TEXT.get(family, CONTROLLER_GLYPH_TEXT["xbox"])[role]
        if family == "keyboard":
            w, h = self._measure(cr, sym, size * 0.72, True)
            return max(w + 16, h + 8)
        if FACE_COLORS.get(family, FACE_COLORS["xbox"]).get(role) is None:
            return self._measure(cr, sym, size * 0.62, True)[0] + 16
        return self._measure(cr, sym, size, True)[0]

    def _measure(self, cr, text: str, size: float, bold: bool) -> Tuple[float, float]:
        from gi.repository import Pango, PangoCairo
        layout = PangoCairo.create_layout(cr)
        desc = self.get_pango_context().get_font_description().copy()
        desc.set_size(int(size * Pango.SCALE))
        desc.set_weight(Pango.Weight.BOLD if bold else Pango.Weight.NORMAL)
        layout.set_font_description(desc)
        layout.set_text(text, -1)
        return layout.get_pixel_size()

    def _launcher_logo(self, launcher: str, px: int) -> Optional[cairo.ImageSurface]:
        """A launcher's logo at px, read once per size."""
        key = ("launcher", launcher, px)
        if key not in self._art:
            pb = self.app._load_launcher_icon(launcher, px).get_pixbuf()
            self._art[key] = argb_surface(pb) if pb is not None else None
        return self._art[key]

    def _logo(self, px: int) -> Optional[cairo.ImageSurface]:
        """The app's logo at px, read from its file once per size (the view
        redraws every frame while something on it moves)."""
        d = self.get_scale_factor()
        key = ("logo", px, d, logo_accent())
        if key not in self._art:
            pb = None
            icon_file = _app_icon_file()
            if icon_file is not None:
                try:
                    pb = argb_surface(logo_pixbuf(px * d))
                    pb.set_device_scale(d, d)
                except Exception as e:
                    logger.debug(f"Console Mode couldn't load the logo: {e}")
            self._art[key] = pb
        return self._art[key]

    def _draw_topbar(self, cr, W, pad, s, accent):
        app = self.app
        y = 30 * s
        # The brand, big: the logo and a two-tone "Plunder" / "Hub" wordmark.
        cy = y + 15 * s
        logo_px = int(64 * s)
        logo = self._logo(logo_px)
        if logo is not None:
            cr.set_source_surface(logo, pad, cy - logo_px / 2)
            cr.paint()
        tx = pad + logo_px + 14 * s
        pw, ph = self._measure(cr, "Plunder", 26 * s, True)
        _pango_text(cr, self, "Plunder", tx, cy - ph / 2, pw + 4, 26 * s, True, (1, 1, 1, 1))
        hw, _ = self._measure(cr, "Hub", 26 * s, True)
        _pango_text(cr, self, "Hub", tx + pw, cy - ph / 2, hw + 4, 26 * s, True, (*accent, 1))

        tabs = self.tabs()
        labels = [app._("console_filter_all") if t is None else t for t in tabs]
        widths = [self._measure(cr, t, 12 * s, True)[0] + 32 * s for t in labels]
        lb_w = self._glyph_width(cr, "prev_tab", 15 * s)
        rb_w = self._glyph_width(cr, "next_tab", 15 * s)
        total = sum(widths) + lb_w + rb_w + 44
        x = (W - total) / 2
        _rounded(cr, x, y - 6 * s, total, 42 * s, 21 * s)
        cr.set_source_rgba(0, 0, 0, 0.35)
        cr.fill()
        gx = x + 12
        gx += self._glyph(cr, "prev_tab", gx, y + 15 * s, 15 * s) + 8
        for tab, label, tw in zip(tabs, labels, widths):
            active = tab == self.tab
            if active:
                _rounded(cr, gx, y - 1 * s, tw, 32 * s, 16 * s)
                cr.set_source_rgba(*accent, 1)
                cr.fill()
            _pango_text(cr, self, label, gx, y + 5 * s, tw, 12 * s, True,
                        (*_on_accent_rgb(accent), 1) if active else (1, 1, 1, 0.8), align_center=True)
            self._hits.append(((gx, y - 6 * s, tw, 42 * s), lambda t=tab: self.set_tab(t)))
            gx += tw
        self._glyph(cr, "next_tab", gx + 8, y + 15 * s, 15 * s)
        self._draw_clock(cr, W, pad, y, s, accent)

    def _draw_clock(self, cr, W, pad, y, s, accent):
        """The time, big, and the date beside it, on a pill as dark as the
        tabs' but a touch more solid - readable over any hero."""
        app = self.app
        now = time.localtime()
        hours, minutes = time.strftime("%H", now), time.strftime("%M", now)
        date = app._format_day_label(time.strftime("%Y-%m-%d", now))
        big, small = 19 * s, 11.5 * s
        hw, th = self._measure(cr, hours, big, True)
        colon_w, _ = self._measure(cr, ":", big, True)
        mw, _ = self._measure(cr, minutes, big, True)
        dw, dh = self._measure(cr, date, small, True)
        gap, inner = 14 * s, 18 * s
        time_w = hw + colon_w + mw
        pill_w = inner + time_w + gap + 1 + gap + dw + inner
        pill_h = 42 * s
        px, py = W - pad - pill_w, y - 6 * s
        _rounded(cr, px, py, pill_w, pill_h, pill_h / 2)
        cr.set_source_rgba(0, 0, 0, 0.5)
        cr.fill_preserve()
        cr.set_source_rgba(1, 1, 1, 0.12)
        cr.set_line_width(1)
        cr.stroke()
        ty = py + (pill_h - th) / 2
        x = px + inner

        def text(t, tx, tyy, size, rgba, width):
            # a soft shadow under it, then the text
            _pango_text(cr, self, t, tx + 1, tyy + 1.5, width, size, True, (0, 0, 0, 0.55))
            _pango_text(cr, self, t, tx, tyy, width, size, True, rgba)
        text(hours, x, ty, big, (1, 1, 1, 1), hw + 4)
        text(":", x + hw, ty - 1 * s, big, (*accent, 1), colon_w + 4)
        text(minutes, x + hw + colon_w, ty, big, (1, 1, 1, 1), mw + 4)
        x += time_w + gap
        cr.rectangle(x, py + pill_h * 0.28, 1, pill_h * 0.44)
        cr.set_source_rgba(1, 1, 1, 0.22)
        cr.fill()
        x += 1 + gap
        text(date, x, py + (pill_h - dh) / 2, small, (1, 1, 1, 0.78), dw + 4)

    def _draw_hero(self, cr, W, H, pad, s, accent, game: GameItem):
        app = self.app
        x, y = pad, max(H * 0.13, 120 * s)
        phase = app._home_phase(game.uid)
        latest = max(app.recent.values(), default=0)
        # the accent's light behind the text, stronger while the game runs
        _paint_hero_light(cr, W, H * 0.62, accent, 0.36 if phase == "running" else 0.2,
                          cx=0.05, cy=0.42, radius=0.55)
        # Setting sail is said on the button alone, and the launcher in the
        # line under the title - a kicker only for these two. Its room
        # stays, so the title doesn't jump from game to game.
        kicker = None
        if phase == "running":
            kicker = app._("home_hero_now_playing")
        elif latest and app.recent.get(game.uid) == latest:
            kicker = app._("home_hero_continue")
        if kicker:
            ky = y + 8 * s
            if phase == "running":
                for radius, alpha in ((10, 0.18), (7, 0.3), (5, 1)):
                    cr.arc(x + 10 * s, ky, radius * s, 0, 2 * math.pi)
                    cr.set_source_rgba(*accent, alpha)
                    cr.fill()
                kx = x + 30 * s
            else:
                kx = x
            _pango_text(cr, self, "\u2009".join(kicker.upper()), kx, y, W * 0.5, 12 * s, True, (*accent, 1))
        ty = y + 24 * s
        _pango_text(cr, self, game.title, x + 1, ty + 3 * s, W * 0.55, 42 * s, True, (0, 0, 0, 0.45))
        th = _pango_text(cr, self, game.title, x, ty, W * 0.55, 42 * s, True, (1, 1, 1, 1))
        # the launcher's logo and name, then what's known of the playing
        my, msize = ty + th + 10 * s, 14 * s
        _, mh = self._measure(cr, "Ag", msize, False)
        mx = x
        logo = self._launcher_logo(game.launcher, int(22 * s))
        if logo is not None:
            cr.set_source_surface(logo, mx, my + mh / 2 - 11 * s)
            cr.paint()
            mx += 30 * s
        items = [(None, game.launcher)]
        playtime = app._home_playtime_text(game.uid)
        if phase == "running":
            if playtime:
                items.append(("clock", app._("home_played_total").format(time=playtime)
                              if app.playtime.get(game.uid) else playtime))
        else:
            if playtime:
                items.append(("clock", playtime))
            if app.recent.get(game.uid):
                items.append(("calendar", app._("home_last_played").format(
                    when=app._home_ago_text(app.recent[game.uid]))))
        if game.uid in app.favorites:
            items.append((None, "★ " + app._("favorite_tooltip")))
        for i, (glyph, item) in enumerate(items):
            if i:
                mx += 26 * s
            if glyph is not None:
                _draw_hero_glyph(cr, glyph, mx, my + mh / 2 - 8 * s, 16 * s, (1, 1, 1, 0.7))
                mx += 24 * s
            iw, _ = self._measure(cr, item, msize, False)
            _pango_text(cr, self, item, mx, my, iw + 4, msize, False, (1, 1, 1, 0.86))
            mx += iw
        by = my + mh + 28 * s
        if phase == "running":
            # the session's clock, big in the accent, and plainly what it is
            started = app.running_since.get(game.uid)
            clock = app._format_elapsed_time(time.monotonic() - started) if started is not None else "00:00"
            tw, _ = self._measure(cr, clock, 34 * s, True)
            _pango_text(cr, self, clock, x, by - 16 * s, tw + 8, 34 * s, True, (*accent, 1))
            _pango_text(cr, self, app._("home_hero_session"), x + tw + 14 * s, by + 2 * s, 400, 14 * s,
                        False, (1, 1, 1, 0.82))
            by += 50 * s
        label = (app._("stop_game_tooltip") if phase == "running" else
                 app._busy_badge(game.uid, phase) if phase in ("launching", "syncing") else app._("launch_label"))
        fill = DANGER if phase == "running" else accent
        fg = (1, 1, 1) if phase == "running" else _on_accent_rgb(accent)
        lw, lh = self._measure(cr, label, 15 * s, True)
        sym = app._controller_glyph_text("activate")
        keycap = app._controller_family == "keyboard"
        gsize = 11 * s if keycap else 17 * s
        gw, gh = self._measure(cr, sym, gsize, True)
        if keycap:
            gw += 14 * s
        bw, bh = lw + gw + 64 * s, 50 * s
        _rounded(cr, x, by, bw, bh, bh / 2)
        cr.set_source_rgba(*fill, 0.55 if phase in ("launching", "syncing") else 1)
        cr.fill()
        _pango_text(cr, self, label, x + 26 * s, by + bh / 2 - lh / 2, lw + 4, 15 * s, True, (*fg, 1))
        gx = x + 38 * s + lw
        if keycap:
            # the key, outlined in the button's own text colour
            kh = gh + 8 * s
            _rounded(cr, gx, by + bh / 2 - kh / 2, gw, kh, 5 * s)
            cr.set_source_rgba(*fg, 0.12)
            cr.fill_preserve()
            cr.set_source_rgba(*fg, 0.55)
            cr.set_line_width(1.2)
            cr.stroke()
            _pango_text(cr, self, sym, gx + 7 * s, by + bh / 2 - gh / 2, gw, gsize, True, (*fg, 1))
        else:
            _pango_text(cr, self, sym, gx, by + bh / 2 - lh / 2 - 2 * s, gw + 4, gsize, True, (*fg, 1))
        self._hits.append(((x, by, bw, bh), self.app._console_activate))
        status = app.launch_status(game.uid) if phase in ("launching", "syncing") else None
        if status:
            _, sh = self._measure(cr, status, 14 * s, False)
            _pango_text(cr, self, status, x + bw + 18 * s, by + bh / 2 - sh / 2, W * 0.4, 14 * s,
                        False, (1, 1, 1, 0.8))
            return
        pad_family = app._joystick_manager.pad_family()
        if pad_family is not None:
            # a controller plugged in: beside the button, how to get from a
            # game back here and back - seen before it's needed (under it,
            # it ran into the shelf's title on a 1080p screen)
            hy, size = by + bh / 2, 16 * s
            hx = x + bw + 26 * s
            hx += self._glyph(cr, "settings", hx, hy, size, pad_family) + 6 * s
            pw, ph = self._measure(cr, "+", 14 * s, True)
            _pango_text(cr, self, "+", hx, hy - ph / 2, pw + 4, 14 * s, True, (1, 1, 1, 0.6))
            hx += pw + 6 * s
            hx += self._glyph(cr, "toggle", hx, hy, size, pad_family) + 12 * s
            text = app._("sc_game_switch")
            _, th2 = self._measure(cr, text, 14 * s, False)
            _pango_text(cr, self, text, hx, hy - th2 / 2, W - hx - pad, 14 * s, False, (1, 1, 1, 0.82))

    def _draw_shelves(self, cr, W, H, pad, s, accent):
        """The shelves where their glides are now; where they're heading is
        noted for _tick, which moves them - drawing moved them too, so an
        animated hero's frames sped the glides up."""
        app = self.app
        ch = int(min(H * 0.3, app.console_cover_size * 4 / 3 * 0.8))
        cw = int(ch * 3 / 4)                     # the cards' 3:4, as in the library
        if not app.show_covers:
            # name plates: wide and low, as in the library
            cw = int(ch * 1.05)
            ch = int(cw * 0.56)
        gap = 22 * s
        # the cards' own size against the one picked (a short screen makes
        # them smaller): their badge, star, glow and frame shrink with them
        self._card_k = min(1.0, ch / max(1.0, app.console_cover_size * 4 / 3 * 0.8)) if app.show_covers else 1.0
        top = H * 0.50
        shelf_h = ch + int(ch * GROW) + 78 * s
        target_y = self.shelf * shelf_h
        heading_off = abs(target_y - self._target_y) > 0.5
        self._target_y = target_y
        cr.save()
        cr.rectangle(0, top - ch * GROW - 60 * s, W, H - top + ch * GROW + 30 * s)
        cr.clip()
        for si, (name, games) in enumerate(self._shelves):
            sy = top + si * shelf_h - self.scroll_y
            if sy > H or sy + shelf_h < top - 80:
                continue
            active = si == self.shelf
            _pango_text(cr, self, name, pad, sy - ch * GROW - 40 * s, W - 2 * pad, 15 * s, True,
                        (1, 1, 1, 1 if active else 0.55))
            sel = self.index.get(name, 0)
            want = max(0, sel * (cw + gap) - (cw + gap))
            if abs(self._target_x.get(name, want) - want) > 0.5:
                heading_off = True
            self._target_x[name] = want
            cur = self.scroll_x.setdefault(name, want)
            extra = cw * GROW * self.sel_anim if active else 0
            for gi, game in enumerate(games):
                gx = pad + gi * (cw + gap) - cur + (extra if gi > sel else 0)
                if gx > W or gx + cw < -40:
                    continue
                selected = active and gi == sel
                self._draw_card(cr, game, gx, sy, cw, ch, selected, active, accent, s)
                grow = 1 + GROW * self.sel_anim if selected else 1
                self._hits.append(((gx, sy + ch - ch * grow, cw * grow, ch * grow),
                                   lambda si=si, gi=gi: self._select_at(si, gi)))
        cr.restore()
        if heading_off or abs(target_y - self.scroll_y) > 0.5:
            # somewhere new to glide to (a move, a resize): _tick takes it there
            GLib.idle_add(lambda: (self._animate() if self._anim_id is None else None, False)[1])

    def _draw_card(self, cr, game: GameItem, gx, sy, cw, ch, selected, active_shelf, accent, s):
        app = self.app
        scale = 1 + GROW * self.sel_anim if selected else 1
        cr.save()
        cr.translate(gx, sy + ch)          # grows up and to the right
        cr.scale(scale, scale)
        cr.translate(0, -ch)
        c = getattr(self, "_card_k", 1.0)
        if selected:
            for k, a in ((16, 0.10), (10, 0.16), (5, 0.25)):
                k *= c
                _rounded(cr, -k, -k, cw + 2 * k, ch + 2 * k, 12 * c + k)
                cr.set_source_rgba(*accent, a * self.sel_anim)
                cr.fill()
        cr.set_source_surface(self._card_image(game, cw, ch, active_shelf, accent), 0, 0)
        cr.paint()
        if selected:
            _rounded(cr, -1.5 * c, -1.5 * c, cw + 3 * c, ch + 3 * c, 13 * c)
            cr.set_source_rgba(1, 1, 1, 0.95)
            cr.set_line_width(3 * c)
            cr.stroke()
        cr.restore()

    def _card_image(self, game: GameItem, cw: int, ch: int, active_shelf: bool, accent) -> cairo.ImageSurface:
        """A card as an image - cover, corners, badge, New tag, star - made
        again only when one of them changes, not on every frame of a glide."""
        app = self.app
        running = app._is_game_running(game.uid)
        is_new = app._is_new_game(game.uid)
        key = ("card", game.uid, cw, ch, active_shelf, running, is_new, game.uid in app.favorites,
               self.tab is None, accent, app.show_covers,
               self.get_scale_factor(), getattr(self, "_card_k", 1.0))
        image = self._art.get(key)
        if image is not None:
            return image
        for old in [k for k in self._art if k[0] == "card" and k[1] == game.uid and k[2:5] == key[2:5]]:
            del self._art[old]
        window = self.get_window()
        # pixels for the selected card's full size too (_draw_card grows it
        # by GROW): drawn at its own size it is scaled down a little, never up
        d = (window.get_scale_factor() if window is not None else 1) * (1 + GROW)
        image = cairo.ImageSurface(cairo.FORMAT_ARGB32, max(1, math.ceil(cw * d)), max(1, math.ceil(ch * d)))
        image.set_device_scale(d, d)
        cr = cairo.Context(image)
        from gi.repository import PangoCairo
        options = PangoCairo.context_get_font_options(self.get_pango_context())
        if options is not None:
            cr.set_font_options(options)
        # cover and shade cut to the rounded corners together (see
        # home_mixin._draw_home_hero), so no light rim shows at them
        cr.push_group()
        self._paint_cover(cr, game, cw, ch, 1.0 if active_shelf else 0.55, d)
        if running:
            cr.set_source_rgba(0, 0, 0, 0.5)
            cr.paint()
        cr.pop_group_to_source()
        c = getattr(self, "_card_k", 1.0)
        _rounded(cr, 0, 0, cw, ch, 12 * c)
        cr.fill()
        # badge, New tag and star as on a full-size card, scaled with it
        cr.save()
        cr.scale(c, c)
        cw = cw / c
        badge = self._badge(game.launcher, d * c) if self.tab is None else None
        if badge is not None:
            bx, by = 10, 10
            cr.set_source_rgba(0, 0, 0, 0.55)
            cr.mask_surface(badge, bx + 1, by + 1.5)
            cr.set_source_surface(badge, bx, by)
            cr.paint()
        if is_new:
            label = app._("home_new_badge")
            tx = 44 if self.tab is None else 10
            tw = 22 + 10.5 * len(label)
            _rounded(cr, tx, 9, tw, 30, 15)
            cr.set_source_rgba(*accent, 0.95)
            cr.fill()
            _pango_text(cr, self, label, tx, 14, tw, 11.5, True, (*_on_accent_rgb(accent), 1), align_center=True)
        if game.uid in app.favorites:
            cr.new_path()
            cr.set_source_rgba(0, 0, 0, 0.55)
            cr.arc(cw - 20, 22, 13, 0, 2 * math.pi)
            cr.fill()
            for k in range(10):
                ang = -math.pi / 2 + k * math.pi / 5
                rr = 8 if k % 2 == 0 else 3.4
                cr.line_to(cw - 20 + rr * math.cos(ang), 22.5 + rr * math.sin(ang))
            cr.close_path()
            cr.set_source_rgba(*STAR, 1)
            cr.fill()
        cr.restore()
        image.flush()
        self._art[key] = image
        return image

    def _badge(self, launcher: str, d: float = 1.0) -> Optional[cairo.ImageSurface]:
        """The launcher's 28 px logo, with 28*d pixels (see _cover)."""
        key = ("badge", launcher, d)
        if key not in self._art:
            pb = self.app._home_launcher_badge(launcher, math.ceil(28 * d))
            surface = None
            if pb is not None:
                surface = argb_surface(pb)
                k = pb.get_width() / 28
                surface.set_device_scale(k, k)
            self._art[key] = surface
        return self._art[key]

    def _draw_legend(self, cr, W, H, pad, s, game: GameItem):
        app = self.app
        y = H - 34 * s
        bg = self._bg_rgb()
        shade = cairo.LinearGradient(0, H - 80 * s, 0, H)
        shade.add_color_stop_rgba(0, *bg, 0)
        shade.add_color_stop_rgba(1, *bg, 0.92)
        cr.set_source(shade)
        cr.rectangle(0, H - 80 * s, W, 80 * s)
        cr.fill()
        running = app._is_game_running(game.uid)
        items = [
            ("activate", app._("stop_game_tooltip") if running else app._("launch_label"), app._console_activate),
            ("favorite", app._("unfavorite_menu") if game.uid in app.favorites else app._("favorite_menu"),
             app._console_toggle_favorite),
            ("sort", f"{app._('sort_by')}: {app._console_sort_label()}", app._console_cycle_sort),
            ("back", app._("console_mode_exit"), app._exit_console_mode),
        ]
        size = 15 * s
        widths = []
        for role, label, _action in items:
            gw = self._glyph_width(cr, role, size)
            widths.append(gw + 10 + self._measure(cr, label, 12 * s, True)[0])
        x = W - pad - sum(widths) - 34 * s * (len(items) - 1)
        for (role, label, action), width in zip(items, widths):
            gw = self._glyph(cr, role, x, y, size)
            _pango_text(cr, self, label, x + gw + 10, y - 9 * s, width, 12 * s, True, (1, 1, 1, 0.85))
            self._hits.append(((x - 6, y - 20 * s, width + 12, 40 * s), action))
            x += width + 34 * s

    def _draw_toast(self, cr, W, H, s, accent):
        text, until = self._toast
        if not text or time.monotonic() > until:
            return
        tw, th = self._measure(cr, text, 13 * s, True)
        x, y = (W - tw) / 2 - 22, H * 0.09
        _rounded(cr, x, y, tw + 44, th + 20, (th + 20) / 2)
        cr.set_source_rgba(0.04, 0.12, 0.13, 0.96)
        cr.fill_preserve()
        cr.set_source_rgba(*accent, 0.6)
        cr.set_line_width(1.2)
        cr.stroke()
        _pango_text(cr, self, text, x + 22, y + 10, tw + 8, 13 * s, True, (1, 1, 1, 1))
