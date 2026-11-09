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

"""The welcome window, a guided tour: the logo rising from the sea with
who PlunderHub is, then a page per main feature (a picture and a few
words), the keys worth knowing last.

The first time (forced) it is watched through: no way out but Next; Set
sail! at the end is what marks it seen. Opened again from the sidebar's logo it is free: the dots
jump, Esc and Close leave.

Nothing repaints once a page has settled: the logo, the splash and the
Next fill run on tick callbacks that stop."""

import math
import random
import webbrowser
from pathlib import Path
from typing import Callable, Dict, List, Optional

import cairo

from plunderhub.constants import APP_VERSION, KOFI_URL
from plunderhub.gi_setup import Gdk, GdkPixbuf, GLib, Gtk, Pango
from plunderhub.icons import get_icon_pixbuf
from plunderhub.logging_setup import logger
from plunderhub.theme import logo_pixbuf
from plunderhub.ui.home_mixin import _accent_rgb, _rounded
from plunderhub.ui.progress_card import card_dialog

GITHUB_URL = "https://github.com/IkrajaDev/PlunderHub"
LICENSE_URL = "https://github.com/IkrajaDev/PlunderHub/blob/main/LICENSE"
WIDTH = 900                        # the card's column

# (title key, text key, picture in assets/tour/ - made by PlunderHubDemos/tour_pictures.py)
PAGES = [
    ("wt_library_t", "wt_library_s", "library"),
    ("wt_sail_t", "wt_sail_s", "sail"),
    ("wt_map_t", "wt_map_s", "map"),
    ("wt_tune_t", "wt_tune_s", "tune"),
    ("wt_shipwright_t", "wt_shipwright_s", "shipwright"),
    ("wt_bilge_t", "wt_bilge_s", "bilge"),
    ("wt_dash_t", "wt_dash_s", "dash"),
    ("wt_console_t", "wt_console_s", "console"),
    ("wt_yours_t", "wt_yours_s", "yours"),
]
PICTURES = Path(__file__).resolve().parent.parent / "assets" / "tour"

def show(app, forced: bool = False):
    """The tour as a modal card over the main window; returns once closed.
    forced: the first start's (watched through, Set sail! marks it seen)."""
    Tour(app, forced).run()


class Tour:
    def __init__(self, app, forced: bool):
        self.app, self.forced = app, forced
        self.t = app._
        self.page = 0
        self.finished = False
        self.dlg, column = card_dialog(app._dialog_transient_parent(), self.t("wt_ahoy"))
        self.dlg.get_style_context().add_class("welcome-tour")
        column.set_size_request(WIDTH, -1)
        column.set_spacing(14)
        self.pic_w, self.pic_h = self._picture_size()

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.SLIDE_LEFT_RIGHT)
        self.stack.set_transition_duration(320)
        self.stack.set_vhomogeneous(True)
        self.pages: List[Gtk.Widget] = [self._welcome_page()]
        self.pages += [self._feature_page(*p) for p in PAGES]
        self.pages.append(self._ready_page())
        for i, page in enumerate(self.pages):
            self.stack.add_named(page, str(i))
        column.pack_start(self.stack, True, True, 0)
        column.pack_start(self._footer(), False, False, 0)

        self.dlg.connect("key-press-event", self._on_key)
        self.dlg.connect("delete-event", lambda *_: self.forced and not self.finished)
        self.dlg.plunderhub_nav = self._nav

    # --- sizes ---------------------------------------------------------------

    def _picture_size(self):
        """16:9 as wide as the card, lower on a short screen (1366x768
        must fit: the words and the buttons take about 300 px)."""
        w = WIDTH - 2 * Picture.SHADOW         # the shadow round it stays inside the card's width
        h = round(w * 9 / 16)
        try:
            display = Gdk.Display.get_default()
            mon = display.get_monitor_at_window(self.app.get_window()) if self.app.get_window() else \
                display.get_primary_monitor() or display.get_monitor(0)
            room = mon.get_workarea().height - 320
            if room < h:
                h = max(240, room)
                w = round(h * 16 / 9)
        except Exception as e:
            logger.debug(f"Welcome tour: no monitor size ({e})")
        return w, h

    # --- pages -----------------------------------------------------------------

    def _welcome_page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_valign(Gtk.Align.CENTER)
        accent = self.app._home_accent_hex()
        words = []

        title = Gtk.Label()
        heading = self.t("wt_ahoy")
        if "PlunderHub" in heading:
            # the wordmark as in the sidebar: "Hub" in the theme's accent
            before, _sep, after = heading.partition("PlunderHub")
            title.set_markup(GLib.markup_escape_text(before) + "Plunder"
                             + f"<span foreground='{accent}'>Hub</span>" + GLib.markup_escape_text(after))
        else:
            title.set_text(heading)
        title.get_style_context().add_class("home-dialog-title")
        title.get_style_context().add_class("wt-ahoy")
        words.append(title)

        slogan = Gtk.Label(label=self.t("wt_slogan"))
        slogan.get_style_context().add_class("wt-slogan")
        words.append(slogan)
        tagline = Gtk.Label(label=self.t("wt_tagline"))
        tagline.set_line_wrap(True)
        tagline.set_justify(Gtk.Justification.CENTER)
        tagline.set_max_width_chars(60)
        tagline.get_style_context().add_class("dim-label")
        words.append(tagline)

        pills = Gtk.Box(spacing=10)
        pills.set_halign(Gtk.Align.CENTER)
        pills.set_margin_top(10)
        for css, icon_name, glyph, text, url in (
                ("github-btn", "github-symbolic", None, self.app._("about_view_github"), GITHUB_URL),
                ("kofi-btn", None, "🍻", self.app._("about_support_kofi"), KOFI_URL)):
            btn = Gtk.Button()
            for c in (css, "about-link", "welcome-link"):
                btn.get_style_context().add_class(c)
            btn.set_can_focus(False)
            btn.set_tooltip_text(url)
            row = Gtk.Box(spacing=7)
            pb = get_icon_pixbuf(icon_name, 15, "#ffffff") if icon_name else None
            if pb is not None:
                row.pack_start(Gtk.Image.new_from_pixbuf(pb), False, False, 0)
            elif glyph:
                row.pack_start(Gtk.Label(label=glyph), False, False, 0)
            row.pack_start(Gtk.Label(label=text), False, False, 0)
            btn.add(row)
            btn.connect("clicked", lambda _b, u=url: webbrowser.open(u))
            pills.pack_start(btn, False, False, 0)
        words.append(pills)

        quiet = Gtk.Box(spacing=4)
        quiet.set_halign(Gtk.Align.CENTER)
        version = Gtk.Label(label=self.app._("about_version").format(version=APP_VERSION))
        version.get_style_context().add_class("dim-label")
        version.get_style_context().add_class("welcome-small")
        quiet.pack_start(version, False, False, 0)
        dot = Gtk.Label(label="·")
        dot.get_style_context().add_class("dim-label")
        quiet.pack_start(dot, False, False, 0)
        license_btn = Gtk.Button(label=self.app._("about_license"))
        license_btn.get_style_context().add_class("about-quiet")
        license_btn.get_style_context().add_class("welcome-quiet")
        license_btn.set_can_focus(False)
        license_btn.set_tooltip_text(LICENSE_URL)
        license_btn.connect("clicked", lambda *_a: webbrowser.open(LICENSE_URL))
        quiet.pack_start(license_btn, False, False, 0)
        words.append(quiet)

        page.pack_start(RisingLogo(self.app, 250, then=words), False, False, 0)
        for w in words:
            page.pack_start(w, False, False, 0)
        return page

    def _feature_page(self, title_key: str, text_key: str, picture: str) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        path = PICTURES / f"{picture}.jpg"
        pic = Picture(path, self.pic_w, self.pic_h) if path.is_file() else Placeholder(picture, self.pic_w, self.pic_h)
        pic.set_halign(Gtk.Align.CENTER)
        page.pack_start(pic, False, False, 6)
        title = Gtk.Label(label=self.t(title_key))
        title.get_style_context().add_class("home-dialog-title")
        title.get_style_context().add_class("wt-title")
        title.set_margin_top(8)
        page.pack_start(title, False, False, 0)
        text = Gtk.Label(label=self.t(text_key))
        text.set_line_wrap(True)
        text.set_justify(Gtk.Justification.CENTER)
        text.set_max_width_chars(64)
        text.set_halign(Gtk.Align.CENTER)
        text.get_style_context().add_class("dim-label")
        text.get_style_context().add_class("wt-text")
        page.pack_start(text, False, False, 0)
        return page

    def _ready_page(self) -> Gtk.Widget:
        app = self.app
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        page.set_valign(Gtk.Align.CENTER)
        # already up here; Set sail! takes it back under the water
        self.ready_logo = RisingLogo(app, 200, risen=True)
        page.pack_start(self.ready_logo, False, False, 0)
        title = Gtk.Label(label=self.t("wt_ready_t"))
        title.get_style_context().add_class("home-dialog-title")
        title.get_style_context().add_class("wt-title")
        page.pack_start(title, False, False, 0)
        sub = Gtk.Label(label=self.t("wt_ready_s"))
        sub.get_style_context().add_class("dim-label")
        page.pack_start(sub, False, False, 0)

        keys = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        keys.set_halign(Gtk.Align.CENTER)
        keys.set_margin_top(14)
        rows = [Gtk.Box(spacing=12), Gtk.Box(spacing=12)]
        for r in rows:
            r.set_halign(Gtk.Align.CENTER)
            keys.pack_start(r, False, False, 0)
        # their names as the app says them elsewhere (already translated)
        for i, (cap, key) in enumerate((("A", "all_games_section"), ("T", "tm_title"), ("S", "sw_title"),
                                        ("D", "dashboard_title"), ("C", "settings_console_group"),
                                        ("O", "options"), ("F1", "wt_key_all"))):
            row = Gtk.Box(spacing=12)
            row.get_style_context().add_class("welcome-feature")
            row.pack_start(app._shortcut_key(cap), False, False, 0)
            label = Gtk.Label(label=self.t(key), xalign=0)
            row.pack_start(label, True, True, 0)
            rows[i // 4].pack_start(row, False, False, 0)
        page.pack_start(keys, False, False, 0)

        # the ship is still being built: the BETA note and Report bug
        leaks = Gtk.Box(spacing=12)
        leaks.get_style_context().add_class("welcome-note")
        leaks.set_margin_top(18)
        leaks.set_halign(Gtk.Align.CENTER)
        beta = Gtk.Label(label=app._("beta_badge").upper())
        beta.get_style_context().add_class("beta-pill")
        beta.get_style_context().add_class("welcome-beta")
        beta.set_valign(Gtk.Align.CENTER)
        leaks.pack_start(beta, False, False, 0)
        leaks_text = Gtk.Label(label=app._("welcome_leaks"), xalign=0)
        leaks_text.set_line_wrap(True)
        leaks_text.set_max_width_chars(56)
        leaks.pack_start(leaks_text, True, True, 0)
        bug = Gtk.Button(label=app._("about_report_bug"))
        bug.set_relief(Gtk.ReliefStyle.NONE)
        bug.set_can_focus(False)
        bug.set_valign(Gtk.Align.CENTER)
        bug.get_style_context().add_class("welcome-bug")
        bug.connect("clicked", lambda b: app._show_bug_report(b))
        leaks.pack_end(bug, False, False, 0)
        page.pack_start(leaks, False, False, 0)
        return page

    # --- the way along ---------------------------------------------------------

    def _footer(self) -> Gtk.Widget:
        app = self.app
        foot = Gtk.Box(spacing=10)
        foot.set_margin_top(6)
        self.close_btn = app._dialog_button(self.t("wt_close"), "ghost", "back", "Esc")
        self.close_btn.connect("clicked", lambda *_: self._close())
        foot.pack_start(self.close_btn, False, False, 0)
        self.back_btn = app._dialog_button("←  " + self.t("wt_back"), "ghost")
        self.back_btn.connect("clicked", lambda *_: self.go(self.page - 1))
        foot.pack_start(self.back_btn, False, False, 0)

        self.dots = Dots(len(self.pages), self._dot_clicked)
        foot.set_center_widget(self.dots)

        self.next_btn = app._dialog_button(self.t("wt_show_around") + "  →", "accent", "activate", "Enter")
        self.next_btn.connect("clicked", lambda *_: self.next())
        foot.pack_end(self.next_btn, False, False, 0)
        return foot

    def _update_footer(self):
        last = self.page == len(self.pages) - 1
        label = (self.t("wt_set_sail") if last
                 else self.t("wt_show_around") + "  →" if self.page == 0 else self.t("wt_next") + "  →")
        inner = self.next_btn.get_child()
        inner.get_children()[0].set_text(label)
        self.back_btn.set_visible(self.page > 0)
        self.close_btn.set_visible(not self.forced)
        self.dots.set_page(self.page)
        self.seen.add(self.page)

    def go(self, i: int):
        if not 0 <= i < len(self.pages) or i == self.page:
            return
        self.page = i
        self.stack.set_visible_child_name(str(i))
        self._update_footer()

    def next(self):
        if self.page == len(self.pages) - 1:
            if self.finished:
                return                  # already sinking
            self.finished = True
            self.ready_logo.sink(lambda: self.dlg.response(Gtk.ResponseType.OK))
        else:
            self.go(self.page + 1)

    def _dot_clicked(self, i: int):
        # forced: only back to a page already seen, or on to the next one
        if not self.forced or i in self.seen:
            self.go(i)
        elif i == self.page + 1:
            self.next()

    def _close(self):
        if not self.forced or self.finished:
            self.dlg.response(Gtk.ResponseType.CANCEL)

    def _on_key(self, _w, ev) -> bool:
        k = ev.keyval
        if k in (Gdk.KEY_Right, Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_space, Gdk.KEY_Page_Down):
            self.next()
        elif k in (Gdk.KEY_Left, Gdk.KEY_BackSpace, Gdk.KEY_Page_Up):
            self.go(self.page - 1)
        elif k == Gdk.KEY_Escape:
            self._close()
        else:
            return False
        return True

    def _nav(self, action: str):
        """The controller: A / right / RB on, left / LB back, B closes (when free)."""
        if action in ("activate", "right", "next_tab"):
            self.next()
        elif action in ("left", "prev_tab"):
            self.go(self.page - 1)
        elif action == "back":
            if self.page > 0 and self.forced:
                self.go(self.page - 1)
            else:
                self._close()

    def run(self):
        self.seen = set()
        self.dlg.get_content_area().show_all()
        self._update_footer()
        self.app._enter_dimmed_dialog()
        try:
            self.dlg.run()
        finally:
            self.app._exit_dimmed_dialog()
        self.dlg.destroy()
        return self.finished


# --- the pieces ------------------------------------------------------------------

class Placeholder(Gtk.DrawingArea):
    """Where the page's picture goes: a dashed frame saying what it will show."""

    def __init__(self, what: str, w: int, h: int):
        super().__init__()
        self.what = what
        self.set_size_request(w, h)
        self.connect("draw", self._draw)

    def _draw(self, widget, cr):
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        ctx = widget.get_style_context()
        fg = ctx.get_color(ctx.get_state())
        _rounded(cr, 1, 1, w - 2, h - 2, 16)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.05)
        cr.fill_preserve()
        cr.set_dash([8, 6])
        cr.set_line_width(1.5)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.28)
        cr.stroke()
        cr.set_dash([])
        layout = widget.create_pango_layout("PICTURE\n" + self.what)
        layout.set_alignment(Pango.Alignment.CENTER)
        layout.set_width(int((w - 80) * Pango.SCALE))
        tw, th = layout.get_pixel_size()
        cr.move_to((w - (w - 80)) / 2, (h - th) / 2)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.45)
        from gi.repository import PangoCairo
        PangoCairo.show_layout(cr, layout)
        return False


class Picture(Gtk.DrawingArea):
    """A page's picture: rounded, on a soft shadow, read once at the
    screen's own pixels (sharp on HiDPI) and kept as a surface - a draw is
    one paint."""
    R, SHADOW = 14, 10

    def __init__(self, path: Path, w: int, h: int):
        super().__init__()
        self.path, self.w, self.h = path, w, h
        self._surf = None
        self.set_size_request(w + 2 * self.SHADOW, h + 2 * self.SHADOW)
        self.connect("draw", self._draw)

    def _surface(self, scale: int):
        if self._surf is None or self._surf[0] != scale:
            surf = None
            try:
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(str(self.path), self.w * scale, self.h * scale, False)
                surf = Gdk.cairo_surface_create_from_pixbuf(pb, scale, None)
            except GLib.Error as e:
                logger.warning(f"Welcome tour: couldn't read {self.path.name}: {e}")
            self._surf = (scale, surf)
        return self._surf[1]

    def _draw(self, widget, cr):
        m, r = self.SHADOW, self.R
        x, y, w, h = m, m * 0.6, self.w, self.h
        for i, a in ((8, 0.05), (5, 0.08), (2, 0.12)):       # the shadow, a little lower than the picture
            _rounded(cr, x - i * 0.5, y + i * 0.7, w + i, h + i * 0.5, r + i * 0.5)
            cr.set_source_rgba(0, 0, 0, a)
            cr.fill()
        surf = self._surface(widget.get_scale_factor())
        cr.save()
        _rounded(cr, x, y, w, h, r)
        cr.clip()
        if surf is not None:
            cr.set_source_surface(surf, x, y)
            cr.paint()
        cr.restore()
        ctx = widget.get_style_context()
        fg = ctx.get_color(ctx.get_state())
        _rounded(cr, x + 0.5, y + 0.5, w - 1, h - 1, r)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.12)
        cr.set_line_width(1)
        cr.stroke()
        return False


class Dots(Gtk.DrawingArea):
    """Where we are: a dot per page, the current one a longer accent pill."""
    R, GAP = 4, 10

    def __init__(self, n: int, clicked: Callable[[int], None]):
        super().__init__()
        self.n, self.page, self.clicked = n, 0, clicked
        self.set_size_request(n * (2 * self.R + self.GAP) + 20, 24)
        self.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.connect("draw", self._draw)
        self.connect("button-press-event", self._press)

    def set_page(self, i: int):
        self.page = i
        self.queue_draw()

    def _xs(self):
        w = self.get_allocated_width()
        total = (self.n - 1) * (2 * self.R + self.GAP) + 4 * self.R
        x = (w - total) / 2
        out = []
        for i in range(self.n):
            dw = 6 * self.R if i == self.page else 2 * self.R
            out.append((x, dw))
            x += dw + self.GAP
        return out

    def _draw(self, widget, cr):
        h = widget.get_allocated_height()
        ctx = widget.get_style_context()
        fg = ctx.get_color(ctx.get_state())
        accent = _accent_rgb(widget)
        for i, (x, dw) in enumerate(self._xs()):
            _rounded(cr, x, h / 2 - self.R, dw, 2 * self.R, self.R)
            if i == self.page:
                cr.set_source_rgb(*accent)
            else:
                cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.25)
            cr.fill()
        return False

    def _press(self, _w, ev):
        for i, (x, dw) in enumerate(self._xs()):
            if x - self.GAP / 2 <= ev.x <= x + dw + self.GAP / 2:
                self.clicked(i)
                return True
        return False


def _on_beat(widget: Gtk.Widget, clock) -> bool:
    """True on the frames an animation here steps: the whole number of the
    monitor's frames nearest 60 fps, as the app's other animations
    (App._bubble_beat_frames) - 165 Hz paints a third of its frames."""
    now = clock.get_frame_time()
    cached = getattr(widget, "_wt_beat", None)
    if cached is None or now - cached[1] > 3_000_000:
        rate = 0.0
        win = widget.get_window()
        if win is not None:
            monitor = win.get_display().get_monitor_at_window(win)
            if monitor is not None:
                rate = monitor.get_refresh_rate() / 1000
        cached = widget._wt_beat = (max(1, round(rate / 60)) if rate > 0 else 1, now)
    return clock.get_frame_counter() % cached[0] == 0


class RisingLogo(Gtk.DrawingArea):
    """The logo rising out of the sea: the surface heaves up as it comes,
    breaks with a crown of water and a spray of drops on both sides, waves
    run off to the edges, water drips off it - then the sea calms and
    nothing is redrawn any more. The glow behind it stays above the water.
    The widgets in then fade in one by one once it's up. Still where
    animations are off."""
    DELAY, RISE, FADE, STAGGER = 0.4, 1.15, 0.3, 0.09
    CALM = 2.4                       # after breaking through, the waves are gone by then
    EYE_AT, EYE_LEN, GLOW_LEN = 1.2, 0.8, 0.5   # the eye lights up once it's up, the glow coming in with it
    # the neon eye starting: (time into it, brightness) - flickers, then stays on
    FLICKER = ((0.0, 0.0), (0.08, 0.85), (0.13, 0.1), (0.24, 0.6), (0.29, 0.05), (0.42, 1.0),
               (0.5, 0.45), (0.56, 1.0))
    EYE_BOX = (596.8, 587.5, 817.8, 741.3)        # the eye and its glow in the 1024px logo
    TOP, BELOW = 30, 62              # room over the logo (its overshoot, the spray), and for the sea

    SINK = 1.0                       # going back under, Set sail! on the last page

    def __init__(self, app, px: int, then=(), splash: bool = True, risen: bool = False):
        super().__init__()
        self.app, self.px, self.splash_on = app, px, splash
        self.set_size_request(-1, px + self.TOP + self.BELOW)
        self.set_hexpand(True)
        self.animate = Gtk.Settings.get_default().props.gtk_enable_animations
        self.t = 0.0 if self.animate and not risen else 99.0
        self.sink_s: Optional[float] = None       # 0..1 while it sinks
        if self.t >= 99:
            self._out_t = -99.0                   # long out of the water: a calm sea
        self._sink_done = None
        self.then = list(then) if self.animate else []
        for w in self.then:
            w.set_opacity(0)
        self._logo = None
        rnd = random.Random(7)
        k = px / 250
        # the spray: (side, start offset from the logo's edge, vx, vy, radius, delay)
        self.drops = [(side, rnd.uniform(-0.08, 0.12) * px, side * rnd.uniform(40, 300) * k,
                       rnd.uniform(240, 560) * k, rnd.uniform(1.6, 4.6) * max(0.7, k), rnd.uniform(0, 0.12))
                      for side in (-1, 1) for _ in range(20)]
        # water running off it once it's up: (x offset, delay, radius)
        self.drips = [(rnd.uniform(-0.28, 0.28) * px, rnd.uniform(0.0, 0.7), rnd.uniform(1.4, 2.8) * max(0.7, k))
                      for _ in range(9)]
        self.break_t = self._break_time()
        self.connect("draw", self._draw)
        if self.animate and not risen:
            self._start = None
            self.connect("map", lambda w: w.add_tick_callback(self._step))

    def sink(self, done: Callable[[], None]):
        """Back under the water (its eye going out, the light fading), then done()."""
        if not self.animate or not self.get_mapped():
            done()
            return
        self._sink_done = done
        self._sink_start = None
        self.sink_s = 0.0
        self.add_tick_callback(self._sink_step)

    def _sink_step(self, _w, clock) -> bool:
        now = clock.get_frame_time() / 1e6
        if self._sink_start is None:
            self._sink_start = now
        self.sink_s = min(1.0, (now - self._sink_start) / self.SINK)
        if self.sink_s >= 1.0 or _on_beat(self, clock):
            self.queue_draw()
        if self.sink_s >= 1.0:
            done, self._sink_done = self._sink_done, None
            if done is not None:
                GLib.idle_add(lambda: (done(), False)[1])
            return False
        return True

    # timing
    def _lift(self, t: float) -> float:
        """How far up the logo is (0 under, 1 in place; a little overshoot)."""
        r = max(0.0, min(1.0, t / self.RISE))
        c = 0.9
        return 1 + (c + 1) * (r - 1) ** 3 + c * (r - 1) ** 2

    def _crossing(self) -> float:
        """0..1..0 while the logo goes through the surface."""
        if self.sink_s is not None:
            s = self.sink_s
            return min(1.0, s / 0.2) * min(1.0, (1 - s) / 0.35)
        if self.t < self.break_t - 0.1:
            return 0.0
        # full while any of it is still under the surface, then dying out
        out = getattr(self, "_out_t", None)
        if out is None:
            return min(1.0, (self.t - self.break_t + 0.1) / 0.1)
        return max(0.0, 1 - (self.t - out) / 0.55) ** 1.5

    def _break_time(self) -> float:
        """When the logo's top breaks the surface."""
        depth = self.px * 1.15                     # how deep it starts
        for i in range(200):
            t = i * self.RISE / 200
            if depth * (1 - self._lift(t)) < self.px + 6:
                return t
        return self.RISE * 0.3

    def _eye(self) -> float:
        """How bright the neon eye is (0 dark, 1 lit)."""
        if self.sink_s is not None:             # going out as it sinks: a last flicker
            s = self.sink_s
            return 1.0 if s < 0.05 else 0.3 if s < 0.09 else 0.8 if s < 0.12 else 0.0
        e = self.t - self.EYE_AT
        if e <= 0:
            return 0.0
        level = 0.0
        for at, v in self.FLICKER:
            if e >= at:
                level = v
        return level

    def _glow(self) -> float:
        """The light behind it: in once the eye is lit."""
        g = max(0.0, min(1.0, (self.t - self.EYE_AT) / self.GLOW_LEN))
        g = g * g * (3 - 2 * g)
        if self.sink_s is None and self.t - self.EYE_AT < self.EYE_LEN:
            g *= 0.2 + 0.8 * self._eye()            # flickering with the eye
        if self.sink_s is not None:
            g *= max(0.0, 1 - self.sink_s / 0.5) ** 2
        return g

    def _pixbuf(self, scale: int):
        """(the logo with its eye dark, the logo as it is)."""
        if self._logo is None or self._logo[0] != scale:
            n = self.px * scale
            # in the theme's colour; the eye unlit = not there at all (nor its glow)
            lit = logo_pixbuf(n)
            dark = logo_pixbuf(n, eye=False)
            def surf(pb):
                return Gdk.cairo_surface_create_from_pixbuf(pb, 1, None) if pb is not None else None
            self._logo = (scale, (surf(dark or lit), surf(lit), lit))
        return self._logo[1]

    def _step(self, _w, clock) -> bool:
        now = clock.get_frame_time() / 1e6
        if self._start is None:
            self._start = now
        self.t = now - self._start - self.DELAY
        busy = self.t < max(self.break_t + self.CALM, self.EYE_AT + max(self.EYE_LEN, self.GLOW_LEN))
        up = self.RISE * 0.7
        for i, w in enumerate(self.then):
            k = max(0.0, min(1.0, (self.t - up - i * self.STAGGER) / self.FADE))
            eased = 1 - (1 - k) ** 2
            if w.get_opacity() != eased:
                w.set_opacity(eased)
            busy = busy or k < 1.0
        if not busy or _on_beat(self, clock):
            self.queue_draw()
        return busy

    # the sea's surface at x (y grows downwards)
    def _surface(self, x: float, cx: float, water: float) -> float:
        px, t = self.px, self.t
        y = water + 1.6 * math.sin(x / 26 + 1.3)          # the calm sea
        tb = t - self.break_t
        d = abs(x - cx)
        # heaving up over the logo as it comes, falling back once through
        heave = math.sin(math.pi * max(0.0, min(1.0, (t + 0.25) / (self.break_t + 0.55))))
        y -= 0.16 * px * heave * math.exp(-(d / (px * 0.45)) ** 2)
        # while it comes through: the water round it is restless - ripples over
        # it, and the water clinging up its sides (no straight line across it)
        c = self._crossing()
        if c > 0:
            win = math.exp(-(d / (px * 0.36)) ** 6)
            y += c * win * px * (0.035 * math.sin(x / (px * 0.07) + t * 12)
                                 + 0.015 * math.sin(x / (px * 0.04) - t * 17))
            y -= c * px * 0.07 * math.exp(-((d - px * 0.31) / (px * 0.05)) ** 2)
        if tb > 0:
            # waves running off to both sides, smaller as they go
            v, wl = 0.85 * px, 0.36 * px
            front = v * tb
            dd = d - px * 0.32
            if 0 < dd < front:
                edge = min(1.0, (front - dd) / wl)
                amp = 0.10 * px * math.exp(-1.2 * tb) * max(0.0, 1 - tb / self.CALM) ** 2 * math.exp(-dd / (px * 2.2)) * edge
                y += amp * math.sin(2 * math.pi * (dd - front) / wl)
        return y

    def _surface_path(self, cr, W, cx, water, below: bool):
        step = 4
        cr.move_to(0, self._surface(0, cx, water))
        for x in range(step, int(W) + step, step):
            cr.line_to(x, self._surface(x, cx, water))
        if below:
            cr.line_to(W, water + 400)
            cr.line_to(0, water + 400)
        else:
            cr.line_to(W, -10)
            cr.line_to(0, -10)
        cr.close_path()

    def _draw(self, widget, cr):
        W, H = widget.get_allocated_width(), widget.get_allocated_height()
        accent = _accent_rgb(widget)
        spray = tuple(0.55 * a + 0.45 for a in accent)        # foam: the accent, lighter
        px, t = self.px, self.t
        cx = W / 2
        water = H - self.BELOW + 12
        rest_cy = water - px / 2 - 4
        cy = rest_cy + px * 1.15 * (1 - self._lift(t))
        if self.sink_s is not None:
            # a little lift first, then down - faster as it goes
            s = self.sink_s
            cy = rest_cy - px * 0.05 * math.sin(math.pi * min(1.0, s / 0.25)) + px * 1.2 * max(0.0, (s - 0.12) / 0.88) ** 2
        # when its chin left the water (the restless water dies out from then)
        if cy + px * 0.42 < water and t > self.break_t:
            if getattr(self, "_out_t", None) is None:
                self._out_t = t
        elif t <= self.break_t:
            self._out_t = None
        appear = max(0.0, min(1.0, (t + self.DELAY) / 0.4))     # the sea fades in first

        # the sea: a soft body under the surface
        cr.save()
        self._surface_path(cr, W, cx, water, below=True)
        body = cairo.LinearGradient(0, water - 6, 0, H)
        body.add_color_stop_rgba(0, *accent, 0.13 * appear)
        body.add_color_stop_rgba(1, *accent, 0)
        side = cairo.LinearGradient(0, 0, W, 0)
        side.add_color_stop_rgba(0, 0, 0, 0, 0)
        side.add_color_stop_rgba(0.2, 0, 0, 0, 1)
        side.add_color_stop_rgba(0.8, 0, 0, 0, 1)
        side.add_color_stop_rgba(1, 0, 0, 0, 0)
        cr.clip()
        cr.push_group()
        cr.set_source(body)
        cr.paint()
        cr.pop_group_to_source()
        cr.mask(side)
        cr.restore()

        scale = widget.get_scale_factor()
        dark, lit, pb = self._pixbuf(scale)
        eye = self._eye()

        def logo(alpha):
            cr.save()
            cr.translate(cx, cy)
            cr.scale(1 / scale, 1 / scale)
            lw, lh = pb.get_width(), pb.get_height()
            cr.set_source_surface(dark if eye < 1 else lit, -lw / 2, -lh / 2)
            cr.paint_with_alpha(alpha)
            if 0 < eye < 1:
                # the lit eye over the dark one, as bright as it is now
                x0, y0, x1, y1 = (v / 1024 * lw for v in self.EYE_BOX)
                cr.rectangle(x0 - lw / 2, y0 - lh / 2, x1 - x0, y1 - y0)
                cr.clip()
                cr.set_source_surface(lit, -lw / 2, -lh / 2)
                cr.paint_with_alpha(alpha * eye)
            cr.restore()

        # above the water: the glow, then the logo
        cr.save()
        self._surface_path(cr, W, cx, water, below=False)
        cr.clip()
        rise = self._glow()
        if rise > 0:
            reach = min(px * 0.95, rest_cy - 2, W / 2 - 2)
            halo = cairo.RadialGradient(cx, rest_cy, px * 0.15, cx, rest_cy, reach)
            halo.add_color_stop_rgba(0, *accent, 0.36 * rise)
            halo.add_color_stop_rgba(0.55, *accent, 0.10 * rise)
            halo.add_color_stop_rgba(1, *accent, 0)
            cr.set_source(halo)
            cr.paint()
        cr.restore()
        if pb is not None and t > 0:
            # the logo: whole above the water, fading out into the deep below it
            # (no hard straight cut where it goes through the surface)
            wl = self._surface(cx, cx, water)
            # only the logo's square takes part (the groups stay small)
            cr.save()
            cr.rectangle(cx - px * 0.6, cy - px * 0.6, px * 1.2, px * 1.2)
            cr.clip()
            low = min(self._surface(x, cx, water) for x in range(int(cx - px * 0.5), int(cx + px * 0.5) + 8, 8))
            if cy + px * 0.5 < low - 2:
                # all of it above the water: just the logo
                logo(1.0)
                cr.restore()
            else:
                self._logo_in_water(cr, W, cx, water, wl, logo)
                cr.restore()
            # its reflection on the sea once it's through, fading downwards
            out = max(0.0, min(1.0, (t - self.break_t) / 0.6))
            if self.sink_s is not None:
                out *= max(0.0, 1 - self.sink_s / 0.4)
            if out > 0:
                cr.save()
                cr.rectangle(cx - px * 0.6, water - 4, px * 1.2, px * 0.46)
                cr.clip()
                self._surface_path(cr, W, cx, water, below=True)
                cr.clip()
                cr.push_group()
                cr.translate(0, 2 * water + 2)
                cr.scale(1, -1)
                logo(1.0)
                refl = cr.pop_group()
                fall = cairo.LinearGradient(0, water, 0, water + px * 0.42)
                fall.add_color_stop_rgba(0, 0, 0, 0, 0.17 * out)
                fall.add_color_stop_rgba(1, 0, 0, 0, 0)
                cr.set_source(refl)
                cr.mask(fall)
                cr.restore()
        self._draw_rest(widget, cr, W, H, cx, cy, water, accent, spray, appear)
        return False

    def _logo_in_water(self, cr, W, cx, water, wl, logo):
        """The logo going through the surface: whole above it, fading out into the deep below."""
        px = self.px
        if True:
            cr.push_group()
            logo(1.0)
            src = cr.pop_group()
            cr.push_group()
            cr.save()
            self._surface_path(cr, W, cx, water, below=False)
            cr.set_source_rgba(0, 0, 0, 1)
            cr.fill()
            cr.restore()
            cr.save()
            self._surface_path(cr, W, cx, water, below=True)
            cr.clip()
            deep = cairo.LinearGradient(0, wl - 3, 0, wl + px * 0.14)
            deep.add_color_stop_rgba(0, 0, 0, 0, 0.9)
            deep.add_color_stop_rgba(0.45, 0, 0, 0, 0.4)
            deep.add_color_stop_rgba(1, 0, 0, 0, 0.1)
            cr.set_source(deep)
            cr.paint()
            cr.restore()
            mask = cr.pop_group()
            cr.set_source(src)
            cr.mask(mask)

    def _draw_rest(self, widget, cr, W, H, cx, cy, water, accent, spray, appear):
        """The surface, the water on the logo, the crown, the spray and the drips."""
        px, t = self.px, self.t
        # the surface line, brightest in the middle
        c = self._crossing()

        def line_at(k):
            line = cairo.LinearGradient(0, 0, W, 0)
            line.add_color_stop_rgba(0, *accent, 0)
            if c > 0:
                # dim where it crosses the logo: the sheet over it is the edge there
                line.add_color_stop_rgba(0.5 - px * 0.42 / W, *accent, k * appear)
                line.add_color_stop_rgba(0.5 - px * 0.30 / W, *accent, k * appear * (1 - 0.8 * c))
            line.add_color_stop_rgba(0.5, *accent, k * appear * (1 - 0.8 * c))
            if c > 0:
                line.add_color_stop_rgba(0.5 + px * 0.30 / W, *accent, k * appear * (1 - 0.8 * c))
                line.add_color_stop_rgba(0.5 + px * 0.42 / W, *accent, k * appear)
            line.add_color_stop_rgba(1, *accent, 0)
            return line
        if c > 0:
            # water running down its face: a thin sheet with a torn top edge
            x0, x1 = int(cx - px * 0.36), int(cx + px * 0.36)
            cr.move_to(x0, self._surface(x0, cx, water))
            for x in range(x0, x1 + 3, 3):
                d = abs(x - cx)
                win = math.exp(-(d / (px * 0.33)) ** 6)
                hgt = c * win * px * (0.06 + 0.035 * math.sin(x / (px * 0.045) + t * 9)
                                      + 0.015 * math.sin(x / (px * 0.025) - t * 15))
                cr.line_to(x, self._surface(x, cx, water) - max(0.0, hgt))
            for x in range(x1, x0 - 3, -3):
                cr.line_to(x, self._surface(x, cx, water) + 2)
            cr.close_path()
            ws = self._surface(cx, cx, water)
            sheet = cairo.LinearGradient(0, ws - px * 0.1, 0, ws + 2)
            sheet.add_color_stop_rgba(0, *spray, 0.7 * c)
            sheet.add_color_stop_rgba(1, *accent, 0.25 * c)
            cr.set_source(sheet)
            cr.fill()
        # the surface: a soft band with a thin bright line on it (not a hard edge)
        cr.move_to(0, self._surface(0, cx, water))
        for x in range(4, int(W) + 4, 4):
            cr.line_to(x, self._surface(x, cx, water))
        cr.set_source(line_at(0.6 * 0.22))
        cr.set_line_width(7)
        cr.stroke_preserve()
        cr.set_source(line_at(0.6 * 0.75))
        cr.set_line_width(1.5)
        cr.stroke()

        tb = t - self.break_t
        if not self.splash_on or tb <= 0:
            return False
        g = 1500 * px / 250
        # the crown: a sheet of water thrown up and outwards on each side as it
        # breaks through, its top torn into fingers with a bead on each tip,
        # rising, leaning out and falling back
        s = tb / 0.75
        if s < 1:
            grow = math.sin(math.pi * min(1.0, s * 1.2)) * (1 - 0.25 * s)
            fade = (1 - s) ** 0.8
            for side in (-1, 1):
                x0 = cx + side * px * 0.36                  # its foot at the logo's side
                spread = px * (0.18 + 0.20 * s)             # how far out its foot reaches
                lean = px * (0.06 + 0.30 * s)               # its top, further out still
                hgt = px * 0.40 * grow
                fingers = ((0.0, 0.70), (0.3, 1.0), (0.62, 0.78), (1.0, 0.48))

                def at(f, h):
                    """A point of the sheet: f across it (0 inner, 1 outer), h up it (0..1)."""
                    return (x0 + side * (spread * f + lean * h * (0.6 + 0.4 * f)), water - hgt * h)
                cr.move_to(*at(0, -0.02))
                x, y = at(0, 0.55)
                cr.curve_to(x0 - side * px * 0.02, water - hgt * 0.25, x, y + hgt * 0.1, x, y)
                prev = None
                tips = []
                for f, h in fingers:
                    tip = at(f, h)
                    tips.append(tip)
                    if prev is not None:
                        # the notch between two fingers
                        nf = (prev + f) / 2
                        nx, ny = at(nf, 0.45 + 0.15 * min(h, 1))
                        cr.line_to(nx, ny)
                    cr.line_to(*tip)
                    prev = f
                x, y = at(1, 0.15)
                cr.curve_to(x + side * px * 0.02, y, x, water - 2, x - side * px * 0.03, water + 2)
                cr.close_path()
                sheet = cairo.LinearGradient(0, water, 0, water - hgt)
                sheet.add_color_stop_rgba(0, *spray, 0.25 * fade)
                sheet.add_color_stop_rgba(0.6, *spray, 0.55 * fade)
                sheet.add_color_stop_rgba(1, *spray, 0.75 * fade)
                cr.set_source(sheet)
                cr.fill()
                # a bead of water on each finger, coming loose as it falls
                for i, (tx, ty) in enumerate(tips):
                    r = px * (0.016 + 0.006 * (i % 2)) * (1 - 0.3 * s)
                    cr.arc(tx + side * lean * 0.15 * s, ty - hgt * 0.12 * s, r, 0, 2 * math.pi)
                    cr.set_source_rgba(*spray, 0.85 * fade)
                    cr.fill()
        # the spray: drops flying out and falling back
        for side, off, vx, vy, r, delay in self.drops:
            tt = tb - delay
            if tt <= 0:
                continue
            x = cx + side * (px * 0.40 + off) + vx * tt
            y = water - (vy * tt - 0.5 * g * tt * tt)
            if y > water + 2 or tt > 1.2:
                continue
            cr.arc(x, y, r, 0, 2 * math.pi)
            cr.set_source_rgba(*spray, 0.85 * max(0.0, 1 - tt / 1.2))
            cr.fill()
        # water running off it, back into the sea
        out = t - self.RISE * 0.75
        if out > 0:
            for dx, delay, r in self.drips:
                tt = out - delay
                if tt <= 0:
                    continue
                x = cx + dx
                y = cy + px * 0.38 + 0.5 * g * 0.6 * tt * tt
                if y > water:
                    continue
                cr.arc(x, y, r, 0, 2 * math.pi)
                cr.set_source_rgba(*spray, 0.8)
                cr.fill()
        return False
