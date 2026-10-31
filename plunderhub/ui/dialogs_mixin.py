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


import math
import os
import re
import shutil
import webbrowser
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cairo

from plunderhub.constants import CONTROLLER_GLYPH_TEXT
from plunderhub.core.models import GameItem
from plunderhub.gi_setup import Gdk, GdkPixbuf, GLib, Gtk, Pango
from plunderhub.icons import get_icon_pixbuf
from plunderhub.logging_setup import logger
from plunderhub.theme import _app_icon_file
from plunderhub.ui.home_mixin import _accent_rgb


def _read_mounts() -> List[Tuple[str, str, str]]:
    """(mount point, fstype, source) per /proc/self/mountinfo line."""
    mounts = []
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as f:
            for line in f:
                left, sep, right = line.partition(" - ")
                fields, tail = left.split(), right.split()
                if not sep or len(fields) < 5 or len(tail) < 2:
                    continue
                # The kernel octal-escapes only space, tab, newline and
                # backslash here; non-ASCII names come through as UTF-8.
                mount_point = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), fields[4])
                mounts.append((mount_point, tail[0], tail[1]))
    except OSError:
        pass
    return mounts


def _filesystem_key(real_path: str, st_dev: int, mounts: List[Tuple[str, str, str]]) -> tuple:
    """Identifies the filesystem holding real_path by its mount source
    (e.g. /dev/nvme1n1p2), which every btrfs subvolume of one disk
    shares. Pseudo sources like "tmpfs" aren't unique, and without
    mountinfo there's nothing better - st_dev is the fallback then."""
    best = None
    for mount_point, fstype, source in mounts:
        prefix = mount_point.rstrip("/") + "/"
        if (real_path == mount_point or real_path.startswith(prefix)) and \
                (best is None or len(mount_point) > len(best[0])):
            best = (mount_point, fstype, source)
    if best is None or not best[2].startswith("/"):
        return ("dev", st_dev)
    return ("source", best[1], best[2])


def _mount_point_of(real_path: str, mounts: List[Tuple[str, str, str]]) -> str:
    """The deepest mount point holding real_path ("/" if none matches)."""
    best = "/"
    for mount_point, _fstype, _source in mounts:
        prefix = mount_point.rstrip("/") + "/"
        if (real_path == mount_point or real_path.startswith(prefix)) and len(mount_point) > len(best):
            best = mount_point
    return best


class DialogsMixin:

    def _compute_storage_results(self) -> Tuple[List[Tuple[GameItem, int]],
                                                List[Tuple[str, int, int, int]]]:
        """Each installed game's size (biggest first), and per drive
        holding games: (mount point, total, used, used by games)."""
        results: List[Tuple[GameItem, int]] = []
        seen_dirs: set = set()
        # Keyed by the filesystem, not st_dev: btrfs gives every subvolume
        # its own st_dev (e.g. / and /home on one disk), which counted the
        # same disk once per subvolume holding a game.
        drive_stats: Dict[tuple, Tuple[int, int, int]] = {}
        mounts = _read_mounts()
        for section in self._cached_launchers:
            for game in section["games"]:
                if not game.install_dir:
                    continue
                path = Path(game.install_dir)
                if not path.exists():
                    continue
                try:
                    real = str(path.resolve())
                except OSError:
                    real = str(path)
                if real in seen_dirs:
                    continue
                seen_dirs.add(real)
                size = self._dir_size_bytes(path)
                results.append((game, size))
                try:
                    fs_key = _filesystem_key(real, os.stat(path).st_dev, mounts)
                    if fs_key not in drive_stats:
                        usage = shutil.disk_usage(str(path))
                        drive_stats[fs_key] = [_mount_point_of(real, mounts), usage.total, usage.used, 0]
                    drive_stats[fs_key][3] += size
                except OSError:
                    pass
        results.sort(key=lambda t: t[1], reverse=True)
        drives = sorted((tuple(v) for v in drive_stats.values()), key=lambda d: d[0])
        return results, drives

    def _controller_glyph_text(self, role: str) -> str:
        family_table = CONTROLLER_GLYPH_TEXT.get(self._controller_family, CONTROLLER_GLYPH_TEXT["xbox"])
        return family_table[role]

    # --- the shortcuts dialog --------------------------------------------------

    def _shortcut_key(self, text: Optional[str] = None, icon_name: Optional[str] = None) -> Gtk.Widget:
        """A keycap: a label, or an icon for the arrows and Enter."""
        if icon_name is not None:
            box = Gtk.Box()
            box.get_style_context().add_class("home-shortcut-key")
            box.set_valign(Gtk.Align.CENTER)
            image = Gtk.Image()
            image.connect("style-updated", lambda w, n=icon_name: w.set_from_pixbuf(
                get_icon_pixbuf(n, 14, self._widget_fg_hex(w))))
            box.pack_start(image, True, True, 0)
            return box
        cap = Gtk.Label(label=text)
        cap.get_style_context().add_class("home-shortcut-key")
        cap.set_valign(Gtk.Align.CENTER)
        return cap

    def _widget_fg_hex(self, widget: Gtk.Widget) -> str:
        ctx = widget.get_style_context()
        c = ctx.get_color(ctx.get_state())
        return "#%02x%02x%02x" % (round(c.red * 255), round(c.green * 255), round(c.blue * 255))

    def _shortcut_pad(self, text: Optional[str] = None, colour: Optional[str] = None, wide: bool = False,
                      icon_name: Optional[str] = None) -> Gtk.Widget:
        """A controller button: round for the face buttons (in their own
        colour), a pill for the shoulder and menu buttons, or an icon
        for the d-pad and the stick."""
        if icon_name is not None:
            box = Gtk.Box()
            box.get_style_context().add_class("home-shortcut-pad")
            box.set_size_request(34, 34)
            box.set_halign(Gtk.Align.CENTER)
            box.set_valign(Gtk.Align.CENTER)
            image = Gtk.Image()
            image.connect("style-updated", lambda w, n=icon_name: w.set_from_pixbuf(
                get_icon_pixbuf(n, 18, self._widget_fg_hex(w))))
            box.pack_start(image, True, True, 0)
            return box
        glyph = Gtk.Label(label=text)
        ctx = glyph.get_style_context()
        ctx.add_class("home-shortcut-pad")
        if wide:
            ctx.add_class("home-shortcut-pad-wide")
        else:
            glyph.set_size_request(34, 34)
        glyph.set_halign(Gtk.Align.CENTER)
        glyph.set_valign(Gtk.Align.CENTER)
        if colour:
            provider = Gtk.CssProvider()
            provider.load_from_data(f"label {{ color: {colour}; }}".encode())
            # Above the theme, which sets every label's colour.
            ctx.add_provider(provider, Gtk.STYLE_PROVIDER_PRIORITY_USER + 1)
        return glyph

    def _shortcuts_panel(self, icon_name: str, heading: str) -> Tuple[Gtk.Box, Gtk.Grid]:
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        panel.get_style_context().add_class("home-shortcuts-panel")
        head = Gtk.Box(spacing=8)
        icon = Gtk.Image()
        icon.connect("style-updated", lambda w: w.set_from_pixbuf(get_icon_pixbuf(
            icon_name, 16, self._widget_fg_hex(w))))
        icon.get_style_context().add_class("home-hero-kicker")
        head.pack_start(icon, False, False, 0)
        label = Gtk.Label(label=heading.upper(), xalign=0)
        label.get_style_context().add_class("home-hero-kicker")
        head.pack_start(label, False, False, 0)
        panel.pack_start(head, False, False, 0)
        grid = Gtk.Grid()
        grid.set_column_spacing(14)
        panel.pack_start(grid, False, False, 0)
        return panel, grid

    def _shortcuts_row(self, grid: Gtk.Grid, row: int, cells: List[Tuple[Gtk.Widget, int]], desc: str,
                       desc_col: int, rule_first: bool = False) -> int:
        """Puts one shortcut on grid at row (a thin rule above all but
        the first - rule_first: under a heading row, row 1 is the first)
        and returns the next free row."""
        if row > (1 if rule_first else 0):
            rule = Gtk.Box()
            rule.get_style_context().add_class("home-shortcuts-rule")
            rule.set_hexpand(True)
            grid.attach(rule, 0, row, desc_col + 1, 1)
            row += 1
        col = 0
        for widget, span in cells:
            widget.set_margin_top(7)
            widget.set_margin_bottom(7)
            grid.attach(widget, col, row, span, 1)
            col += span
        label = Gtk.Label(label=desc, xalign=0)
        label.set_line_wrap(True)
        # Wide enough that most fit on one line - a wrapping label
        # otherwise asks for next to no width at all.
        label.set_width_chars(26)
        label.set_max_width_chars(34)
        label.set_hexpand(True)
        # room from the rules when it wraps to two lines
        label.set_margin_top(6)
        label.set_margin_bottom(6)
        grid.attach(label, desc_col, row, 1, 1)
        return row + 1

    _KEY_ICONS = {"↑": "go-up-symbolic", "↓": "go-down-symbolic", "←": "go-previous-symbolic",
                  "→": "go-next-symbolic", "Enter": "input-enter-symbolic"}
    _PAD_ICONS = {"D-pad": "input-dpad-symbolic", "Stick": "input-joystick-symbolic"}

    def _shortcut_group_card(self, title_key: str, icon_name: str, rows, caps_col: Gtk.SizeGroup,
                             controller: bool, wide: bool = False) -> Gtk.Box:
        """One group of shortcuts as a card: its name, then a line per
        shortcut - the keys (in one column across the page), what they do.
        wide: the lines in two columns, the first half left."""
        from plunderhub.ui.shortcut_list import FACE_COLOURS
        card, grid = self._shortcuts_panel(icon_name, self._(title_key))
        grid.set_column_spacing(16)
        grids = [grid]
        if wide and len(rows) > 1:
            card.remove(grid)
            halves = Gtk.Box(spacing=32, homogeneous=True)
            grids.append(Gtk.Grid())
            grids[1].set_column_spacing(16)
            for g in grids:
                halves.pack_start(g, True, True, 0)
            card.pack_start(halves, False, False, 0)
        half = (len(rows) + 1) // 2 if wide and len(rows) > 1 else len(rows)
        row = 0
        for n, (keys, desc_key) in enumerate(rows):
            if n == half:
                grid, row = grids[1], 0
            if controller:
                if row == 0:
                    # which column is which controller, over each card's
                    for col, (text, group) in enumerate((("Xbox", caps_col[0]), ("PlayStation", caps_col[1]))):
                        head = Gtk.Label(label=text, xalign=0)
                        head.get_style_context().add_class("home-shortcuts-pad-head")
                        group.add_widget(head)
                        grid.attach(head, col, 0, 1, 1)
                    row = 1
                cells = []
                for chord, group in zip(keys, caps_col):
                    caps = Gtk.Box(spacing=4)
                    caps.set_valign(Gtk.Align.CENTER)
                    for i, k in enumerate(chord.split("+")):
                        if i:
                            plus = Gtk.Label(label="+")
                            plus.get_style_context().add_class("dim-label")
                            caps.pack_start(plus, False, False, 0)
                        if k in self._PAD_ICONS:
                            caps.pack_start(self._shortcut_pad(icon_name=self._PAD_ICONS[k]), False, False, 0)
                            continue
                        text = self._("shortcut_start") if k == "Start" else k
                        caps.pack_start(self._shortcut_pad(text, FACE_COLOURS.get(k), wide=len(text) > 2),
                                        False, False, 0)
                    group.add_widget(caps)
                    cells.append((caps, 1))
                row = self._shortcuts_row(grid, row, cells, self._(desc_key), 2, rule_first=True)
                continue
            caps = Gtk.Box(spacing=4)
            caps.set_valign(Gtk.Align.CENTER)
            for i, k in enumerate(keys):
                if i and keys[0] == "Ctrl":
                    plus = Gtk.Label(label="+")
                    plus.get_style_context().add_class("dim-label")
                    caps.pack_start(plus, False, False, 0)
                caps.pack_start(self._shortcut_key(icon_name=self._KEY_ICONS[k]) if k in self._KEY_ICONS
                                else self._shortcut_key(k), False, False, 0)
            caps_col.add_widget(caps)
            row = self._shortcuts_row(grid, row, [(caps, 1)], self._(desc_key), 1)
        return card

    def _build_shortcuts_panels(self) -> Gtk.Widget:
        """The shortcuts, one page for the keyboard and one for a
        controller, each in groups set out in two columns of cards - the
        page to show picked with the switch above them (a Gtk.Stack:
        .shortcuts_stack, the switch's buttons .shortcuts_tabs)."""
        from plunderhub.ui.shortcut_list import CONTROLLER, KEYBOARD, OWN_KEYS_COLUMN, WIDE
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        tabs = Gtk.Box(spacing=2)
        tabs.get_style_context().add_class("gp-tabs")
        tabs.set_halign(Gtk.Align.START)
        stack = Gtk.Stack()
        stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        stack.set_transition_duration(150)
        # one size for both pages: the window doesn't jump on a switch
        stack.set_vhomogeneous(True)
        first = None
        buttons = {}
        for name, heading, groups, controller in (
                ("keyboard", "shortcuts_keyboard_heading", KEYBOARD, False),
                ("controller", "shortcuts_controller_heading", CONTROLLER, True)):
            # the keys' column - a controller's: an Xbox and a PlayStation one
            caps_col = (tuple(Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL) for _ in range(2))
                        if controller else Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL))
            page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
            columns = Gtk.Box(spacing=16, homogeneous=True)
            page.pack_start(columns, False, False, 0)
            left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
            right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
            columns.pack_start(left, True, True, 0)
            columns.pack_start(right, True, True, 0)
            # the cards in two columns of about the same height
            heights = [0, 0]
            for title_key, icon_name, rows in groups:
                if title_key in WIDE:
                    own = (tuple(Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL) for _ in range(2))
                           if title_key in OWN_KEYS_COLUMN else None)
                    page.pack_start(self._shortcut_group_card(title_key, icon_name, rows, own or caps_col,
                                                              controller, wide=True), False, False, 0)
                    continue
                side = 0 if heights[0] <= heights[1] else 1
                (left if side == 0 else right).pack_start(
                    self._shortcut_group_card(title_key, icon_name, rows, caps_col, controller), False, False, 0)
                heights[side] += len(rows) + 1.5
            stack.add_named(page, name)
            rb = Gtk.RadioButton.new_with_label_from_widget(first, self._(heading))
            rb.set_mode(False)
            rb.set_focus_on_click(False)
            first = first or rb
            rb.connect("toggled", lambda b, n=name: b.get_active() and stack.set_visible_child_name(n))
            tabs.pack_start(rb, False, False, 0)
            buttons[name] = rb
        box.pack_start(tabs, False, False, 0)
        box.pack_start(stack, False, False, 0)
        box.shortcuts_stack, box.shortcuts_tabs = stack, buttons
        return box

    def _popping_logo(self, logo_px: int, glow: bool = False, then=(),
                      glow_room: Tuple[int, int] = (112, 72), picture=None) -> Gtk.DrawingArea:
        """The app's logo, big, popping into place: it fades in as it grows
        from 70 % with a little overshoot - a moment after the card shows,
        since compositors (Hyprland, KWin...) animate a new window themselves
        for about that long and hid it. With glow it sits in a soft accent
        glow that grows with it, and one ring ripples out as it lands. The
        widgets in then fade in after it, one by one; glow_room is the space
        the glow gets beside / above and below the logo. picture(px) gives another
        picture than the app's logo (Shipwright's cards). Still where the system
        has animations off; nothing is redrawn once it has settled."""
        art = Gtk.DrawingArea()
        if glow:
            # roomy, so the glow and the ring fade out well inside it
            art.set_size_request(logo_px + glow_room[0], logo_px + glow_room[1])
        else:
            art.set_size_request(logo_px + 24, logo_px + 12)
        animate = Gtk.Settings.get_default().props.gtk_enable_animations
        state = {"t": 0.0 if animate else 1.0, "ring": 0.0 if animate and glow else 1.0,
                 "start": None, "logo": None}
        delay_us, length_us, ring_us = 500_000, 600_000, 900_000
        fade_at, fade_us, stagger_us = 0.45 * length_us, 280_000, 90_000
        then = list(then) if animate else []
        for widget in then:
            widget.set_opacity(0)

        def logo_pixbuf(scale: int):
            """The logo at the screen's own pixels (sharp on HiDPI too)."""
            have = state["logo"]
            if have is None or have[0] != scale:
                pb = None
                icon_file = _app_icon_file()
                if picture is not None:
                    pb, icon_file = picture(logo_px * scale), None
                if icon_file is not None:
                    try:
                        pb = GdkPixbuf.Pixbuf.new_from_file_at_size(str(icon_file), logo_px * scale, logo_px * scale)
                    except GLib.Error as e:
                        logger.debug(f"Couldn't load the app icon: {e}")
                have = state["logo"] = (scale, pb)
            return have[1]

        def draw(widget, cr):
            t = state["t"]
            if t <= 0:
                return False
            # ease-out with a small overshoot, then settling at full size
            c = 1.6
            grow = 1 + (c + 1) * (t - 1) ** 3 + c * (t - 1) ** 2
            size = 0.7 + 0.3 * grow
            w, h = widget.get_allocated_width(), widget.get_allocated_height()
            cx, cy = w / 2, h / 2
            if glow:
                accent = _accent_rgb(widget)
                strength = min(1.0, t * 1.6)
                halo = cairo.RadialGradient(cx, cy, logo_px * 0.15 * size, cx, cy, (min(w, h) / 2 - 1) * size)
                halo.add_color_stop_rgba(0, *accent, 0.38 * strength)
                halo.add_color_stop_rgba(0.55, *accent, 0.10 * strength)
                halo.add_color_stop_rgba(1, *accent, 0)
                cr.set_source(halo)
                cr.paint()
                r = state["ring"]
                if 0 < r < 1:
                    inner, outer = logo_px * 0.42, min(w, h) / 2 - 2
                    cr.arc(cx, cy, inner + (outer - inner) * (1 - (1 - r) ** 3), 0, 2 * math.pi)
                    cr.set_source_rgba(*accent, 0.5 * (1 - r) ** 2)
                    cr.set_line_width(0.5 + 2.5 * (1 - r))
                    cr.stroke()
            scale = widget.get_scale_factor()
            pb = logo_pixbuf(scale)
            if pb is not None:
                cr.save()
                cr.translate(cx, cy)
                cr.scale(size / scale, size / scale)
                Gdk.cairo_set_source_pixbuf(cr, pb, -pb.get_width() / 2, -pb.get_height() / 2)
                cr.paint_with_alpha(min(1.0, t * 2.5))
                cr.restore()
            return False

        def tick(widget, frame_clock) -> bool:
            now = frame_clock.get_frame_time()
            if state["start"] is None:
                state["start"] = now + delay_us
            since = now - state["start"]
            state["t"] = max(0.0, min(1.0, since / length_us))
            if glow:
                state["ring"] = max(0.0, min(1.0, (since - 0.55 * length_us) / ring_us))
            busy = state["t"] < 1.0 or 0.0 < state["ring"] < 1.0 or (glow and state["ring"] == 0.0)
            widget.queue_draw()
            for i, other in enumerate(then):
                k = max(0.0, min(1.0, (since - fade_at - i * stagger_us) / fade_us))
                eased = 1 - (1 - k) ** 2
                if other.get_opacity() != eased:
                    other.set_opacity(eased)
                busy = busy or k < 1.0
            return busy

        art.connect("draw", draw)
        if animate:
            art.connect("map", lambda w: w.add_tick_callback(tick))
        return art


    # ---------------------------------------------------------------
    # Report a bug: what to write, the system in copyable lines, the log
    # ---------------------------------------------------------------

    def _bug_report_lines(self, gtk: Dict[str, str]) -> List[Tuple[str, str]]:
        """The system, free of personal details; safe off the GTK thread
        (gtk = bug_report.gtk_lines, read on it)."""
        from plunderhub.core import bug_report
        enabled = getattr(getattr(self, "scanner", None), "enabled", None)
        extra = [("Language", str(getattr(self, "lang", "?"))), ("Theme", str(getattr(self, "theme", "?")))]
        return bug_report.system_lines(enabled, extra, gtk=gtk,
                                       settings=dict(getattr(self, "settings", None) or {}))

    def _copy_to_clipboard(self, text: str) -> None:
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(text, -1)
        clipboard.store()

    def _show_bug_report(self, source: Optional[Gtk.Widget] = None) -> None:
        """The Report bug buttons open this card, the welcome card's size:
        three questions a good report answers, a note that nothing personal
        is shared, the system folded into one line (open it to read or copy
        it), the e-mail address for those without a GitHub account, the log,
        and GitHub's bug form with the system filled in. GitHub opens in the
        browser and the card stays open; the X (or Esc) closes it. The card
        keeps its size: the middle scrolls when the system is unfolded.
        source is the button, so the card opens over the window it was in."""
        import threading
        from plunderhub.constants import BUG_REPORT_EMAIL, LOG_FILE
        from plunderhub.core import bug_report
        from plunderhub.ui.progress_card import card_dialog
        _ = self._
        parent = source.get_toplevel() if source is not None else None
        if not isinstance(parent, Gtk.Window) or not parent.get_visible():
            parent = None
        accent_hex = "#%02x%02x%02x" % tuple(round(c * 255) for c in _accent_rgb(self))
        state = {"system": "", "open": False, "alive": True}
        timers: Dict[Gtk.Label, int] = {}

        dlg, column = card_dialog(parent or self._dialog_transient_parent(), _("bug_title"))
        column.set_size_request(760, -1)
        column.set_spacing(12)

        def pill(key: str, kind: str = "ghost", small: bool = False) -> Gtk.Button:
            btn = self._dialog_button(_(key), kind)
            if small:
                btn.get_style_context().add_class("bug-small-pill")
            btn.set_valign(Gtk.Align.CENTER)
            return btn

        def copied(btn: Gtk.Button, key: str) -> None:
            # "Copied" for a moment, then the button's own words again
            label = btn.get_child().get_children()[0]
            label.set_text(_("bug_copied"))
            old = timers.pop(label, None)
            if old:
                GLib.source_remove(old)

            def back() -> bool:
                timers.pop(label, None)
                label.set_text(_(key))
                return False
            timers[label] = GLib.timeout_add(1500, back)

        # --- the heading: the bug in its round badge, the words, the X
        top = Gtk.Box(spacing=18)
        badge = Gtk.DrawingArea()
        badge.set_size_request(56, 56)
        badge.set_valign(Gtk.Align.START)

        def draw_badge(widget, cr):
            rgb = _accent_rgb(widget)
            cr.arc(28, 28, 28, 0, 2 * math.pi)
            cr.set_source_rgba(*rgb, 0.16)
            cr.fill()
            pb = get_icon_pixbuf("bug-symbolic", 28, accent_hex)
            if pb is not None:
                Gdk.cairo_set_source_pixbuf(cr, pb, 14, 14)
                cr.paint()
            return False
        badge.connect("draw", draw_badge)
        top.pack_start(badge, False, False, 0)
        words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        words.set_valign(Gtk.Align.CENTER)
        title = Gtk.Label(label=_("bug_title"), xalign=0)
        title.get_style_context().add_class("home-dialog-title")
        title.get_style_context().add_class("welcome-title")
        words.pack_start(title, False, False, 0)
        intro = Gtk.Label(label=_("bug_intro"), xalign=0)
        intro.set_line_wrap(True)
        intro.set_max_width_chars(52)
        intro.get_style_context().add_class("dim-label")
        words.pack_start(intro, False, False, 0)
        top.pack_start(words, True, True, 0)
        close = Gtk.Button()
        close.get_style_context().add_class("about-close")
        cross_pb = get_icon_pixbuf("window-close-symbolic", 16, self._widget_fg_hex(self))
        cross = Gtk.Image.new_from_pixbuf(cross_pb) if cross_pb is not None else Gtk.Label(label="×")
        close.add(cross)
        close.set_valign(Gtk.Align.START)
        close.set_focus_on_click(False)
        close.set_tooltip_text(_("bug_close"))
        close.connect("clicked", lambda *_a: dlg.response(Gtk.ResponseType.CLOSE))
        top.pack_end(close, False, False, 0)
        column.pack_start(top, False, False, 0)

        # --- what follows scrolls once the system is unfolded
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        col.set_margin_end(4)

        # the three questions, side by side like the welcome card's
        steps = Gtk.Grid(column_spacing=8, column_homogeneous=True)
        for i, (title_key, sub_key) in enumerate((("bug_q1_t", "bug_q1_s"), ("bug_q2_t", "bug_q2_s"),
                                                  ("bug_q3_t", "bug_q3_s"))):
            card = Gtk.Box(spacing=12)
            card.get_style_context().add_class("welcome-feature")
            num = Gtk.Label(label=str(i + 1))
            num.get_style_context().add_class("bug-step-big")
            # all three at the top: the titles stay on one line however
            # many lines a card's words wrap to
            num.set_valign(Gtk.Align.START)
            card.pack_start(num, False, False, 0)
            texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            texts.set_valign(Gtk.Align.START)
            head = Gtk.Label(label=_(title_key), xalign=0)
            head.get_style_context().add_class("home-update-title")
            texts.pack_start(head, False, False, 0)
            sub = Gtk.Label(label=_(sub_key), xalign=0)
            sub.set_line_wrap(True)
            sub.set_max_width_chars(18)
            sub.get_style_context().add_class("dim-label")
            sub.get_style_context().add_class("welcome-small")
            texts.pack_start(sub, False, False, 0)
            card.pack_start(texts, True, True, 0)
            steps.attach(card, i, 0, 1, 1)
        col.pack_start(steps, False, False, 0)

        # said where it can't be missed: nothing personal leaves the computer
        privacy = Gtk.Box(spacing=12)
        privacy.get_style_context().add_class("bug-privacy")
        pb = get_icon_pixbuf("shield-check-symbolic", 26, accent_hex)
        if pb is not None:
            shield = Gtk.Image.new_from_pixbuf(pb)
            shield.set_valign(Gtk.Align.CENTER)
            privacy.pack_start(shield, False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        texts.set_valign(Gtk.Align.CENTER)
        privacy_head = Gtk.Label(label=_("bug_privacy_title"), xalign=0)
        privacy_head.get_style_context().add_class("bug-privacy-title")
        texts.pack_start(privacy_head, False, False, 0)
        privacy_text = Gtk.Label(label=_("bug_privacy"), xalign=0)
        privacy_text.set_line_wrap(True)
        privacy_text.set_max_width_chars(70)
        privacy_text.get_style_context().add_class("dim-label")
        privacy_text.get_style_context().add_class("welcome-small")
        texts.pack_start(privacy_text, False, False, 0)
        privacy.pack_start(texts, True, True, 0)
        col.pack_start(privacy, False, False, 0)

        # "Your system": one line, folded; a click unfolds the full list
        system_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        system_card.get_style_context().add_class("bug-info")
        head_row = Gtk.Box(spacing=8)
        toggle = Gtk.Button()
        toggle.set_relief(Gtk.ReliefStyle.NONE)
        toggle.get_style_context().add_class("bug-toggle")
        toggle_inner = Gtk.Box(spacing=8)
        chevron = Gtk.Image()
        down = get_icon_pixbuf("pan-down-symbolic", 16, accent_hex)
        right = down.rotate_simple(GdkPixbuf.PixbufRotation.COUNTERCLOCKWISE) if down is not None else None
        if right is not None:
            chevron.set_from_pixbuf(right)
        toggle_inner.pack_start(chevron, False, False, 0)
        head = Gtk.Label(label=_("bug_system"), xalign=0)
        head.get_style_context().add_class("bug-section")
        toggle_inner.pack_start(head, False, False, 0)
        brief = Gtk.Label(label="…", xalign=0)
        brief.set_ellipsize(Pango.EllipsizeMode.END)
        brief.set_max_width_chars(48)
        brief.get_style_context().add_class("dim-label")
        brief.get_style_context().add_class("welcome-small")
        toggle_inner.pack_start(brief, True, True, 0)
        toggle.add(toggle_inner)
        head_row.pack_start(toggle, True, True, 0)
        copy_info = pill("bug_copy_info", small=True)
        head_row.pack_start(copy_info, False, False, 0)
        system_card.pack_start(head_row, False, False, 0)

        # name | value, a wrapped value staying in its own column
        info = Gtk.Grid(column_spacing=14, row_spacing=3)
        info.get_style_context().add_class("bug-info-text")
        info.set_margin_top(6)
        info.set_margin_bottom(6)
        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        revealer.set_transition_duration(180)
        revealer.add(info)
        system_card.pack_start(revealer, False, False, 0)
        col.pack_start(system_card, False, False, 0)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_overlay_scrolling(False)
        scroller.set_propagate_natural_width(True)
        scroller.get_style_context().add_class("settings-scroll")
        scroller.get_style_context().add_class("bug-scroll")
        scroller.add(col)
        column.pack_start(scroller, False, False, 0)

        # --- no GitHub account: the same by e-mail, the address to copy
        mail = Gtk.Box(spacing=12)
        mail.get_style_context().add_class("bug-info")
        mail.get_style_context().add_class("bug-mail")
        pb = get_icon_pixbuf("mail-symbolic", 22, accent_hex)
        if pb is not None:
            envelope = Gtk.Image.new_from_pixbuf(pb)
            envelope.set_valign(Gtk.Align.CENTER)
            envelope.set_margin_start(8)
            mail.pack_start(envelope, False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        texts.set_valign(Gtk.Align.CENTER)
        mail_head = Gtk.Label(label=_("bug_email_title"), xalign=0)
        mail_head.get_style_context().add_class("bug-section")
        texts.pack_start(mail_head, False, False, 0)
        mail_sub = Gtk.Label(label=_("bug_email_sub"), xalign=0)
        mail_sub.set_line_wrap(True)
        mail_sub.set_max_width_chars(40)
        mail_sub.get_style_context().add_class("dim-label")
        mail_sub.get_style_context().add_class("welcome-small")
        texts.pack_start(mail_sub, False, False, 0)
        mail.pack_start(texts, True, True, 0)
        address = Gtk.Label(label=BUG_REPORT_EMAIL)
        address.set_selectable(True)
        # the Copy button beside it is the keyboard's stop
        address.set_can_focus(False)
        address.get_style_context().add_class("bug-mail-address")
        address.set_valign(Gtk.Align.CENTER)
        mail.pack_start(address, False, False, 0)
        copy_mail = pill("bug_copy_info", small=True)
        mail.pack_start(copy_mail, False, False, 0)
        column.pack_start(mail, False, False, 0)

        # --- the log on the left, GitHub on the right
        foot = Gtk.Box(spacing=8)
        foot.set_margin_top(4)
        copy_log = pill("bug_copy_log")
        open_log = pill("bug_open_log")
        foot.pack_start(copy_log, False, False, 0)
        foot.pack_start(open_log, False, False, 0)
        github = self._dialog_button(_("bug_open_github"), "accent", "activate", "Enter")
        github.set_valign(Gtk.Align.CENTER)
        foot.pack_end(github, False, False, 0)
        column.pack_start(foot, False, False, 0)

        # --- the scrolling part keeps the height the folded card needs
        try:
            display = Gdk.Display.get_default()
            anchor = (parent or self).get_window()
            monitor = display.get_monitor_at_window(anchor) if anchor is not None else display.get_primary_monitor()
            room = (monitor or display.get_monitor(0)).get_workarea().height - 400
        except Exception:
            room = 0
        scroller.set_propagate_natural_height(True)
        if room > 0:
            scroller.set_max_content_height(max(140, room))

        def freeze(widget) -> None:
            def later() -> bool:
                height = widget.get_allocated_height()
                widget.set_min_content_height(height)
                widget.set_max_content_height(height)
                widget.set_propagate_natural_height(False)
                widget.get_vadjustment().set_value(0)
                return False
            GLib.idle_add(later)
        scroller.connect("map", freeze)

        # --- the system, gathered off the GTK thread (~0.25 s)
        def fill(lines) -> bool:
            if not state["alive"]:
                return False
            state["system"] = bug_report.system_text(lines)
            brief.set_text(bug_report.summary(lines))
            for row, (key, value) in enumerate(lines):
                name = Gtk.Label(label=key, xalign=0, yalign=0)
                name.get_style_context().add_class("dim-label")
                info.attach(name, 0, row, 1, 1)
                text = Gtk.Label(label=value, xalign=0, yalign=0)
                text.set_line_wrap(True)
                text.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
                text.set_max_width_chars(60)
                text.set_hexpand(True)
                info.attach(text, 1, row, 1, 1)
            info.show_all()
            return False

        gtk_part = bug_report.gtk_lines(parent or self)

        def gather():
            try:
                lines = self._bug_report_lines(gtk_part)
            except Exception as e:
                logger.warning(f"Couldn't gather the system info for a bug report: {e}")
                lines = [("PlunderHub", "unknown")]
            GLib.idle_add(fill, lines)
        threading.Thread(target=gather, name="bug-report-info", daemon=True).start()

        # --- what the buttons do
        def on_toggle(_b):
            state["open"] = not state["open"]
            revealer.set_reveal_child(state["open"])
            pix = down if state["open"] else right
            if pix is not None:
                chevron.set_from_pixbuf(pix)
        toggle.connect("clicked", on_toggle)

        def on_revealed(rev, _pspec):
            if rev.get_child_revealed():
                # bring the unfolded list into view, its heading at the top
                scroller.get_vadjustment().set_value(system_card.get_allocation().y)
        revealer.connect("notify::child-revealed", on_revealed)

        def on_copy_info(btn):
            if state["system"]:
                self._copy_to_clipboard(state["system"])
                copied(btn, "bug_copy_info")
        copy_info.connect("clicked", on_copy_info)

        def on_copy_mail(btn):
            self._copy_to_clipboard(BUG_REPORT_EMAIL)
            copied(btn, "bug_copy_info")
        copy_mail.connect("clicked", on_copy_mail)

        def on_copy_log(btn):
            log = bug_report.log_tail()
            if not log:
                self._show_info_dialog(_("bug_no_log"), transient_for=dlg)
                return
            self._copy_to_clipboard(log)
            copied(btn, "bug_copy_log")
        copy_log.connect("clicked", on_copy_log)

        def on_open_log(_btn):
            if not LOG_FILE.is_file():
                self._show_info_dialog(_("bug_no_log"), transient_for=dlg)
                return
            try:
                Gtk.show_uri_on_window(dlg, LOG_FILE.as_uri(), Gdk.CURRENT_TIME)
            except GLib.Error as e:
                # nothing opens .log files: the folder, with the file in it
                logger.info(f"Couldn't open the log file itself: {e}")
                self._show_folder(LOG_FILE.parent, transient_for=dlg)
        open_log.connect("clicked", on_open_log)

        def on_github(*_a):
            # the form, the system filled in; the card stays open (the log
            # can still be copied from it)
            system = state["system"] or bug_report.system_text(self._bug_report_lines(gtk_part))
            webbrowser.open(bug_report.issue_url(system))
        github.connect("clicked", on_github)

        def on_key(_w, event) -> bool:
            if event.keyval == Gdk.KEY_Escape:
                dlg.response(Gtk.ResponseType.CLOSE)
                return True
            if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_ISO_Enter) and dlg.get_focus() is None:
                on_github()
                return True
            return False
        dlg.connect("key-press-event", on_key)

        def on_controller(action: str):
            if action == "back":
                dlg.response(Gtk.ResponseType.CLOSE)
            elif action == "activate":
                on_github()
        dlg.plunderhub_nav = on_controller

        dlg.get_content_area().show_all()
        self._enter_dimmed_dialog()
        try:
            dlg.run()
        finally:
            state["alive"] = False
            for source_id in timers.values():
                GLib.source_remove(source_id)
            self._exit_dimmed_dialog()
            dlg.destroy()
