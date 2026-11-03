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
"""The release notes window: a Proton family's releases on the left, the
picked one's notes on the right (core/release_notes.py turns their
Markdown into labels) - read in the app, not in a browser.

On top of the list, when newer releases came out than the one installed,
"Since your version" shows all their notes one after the other. Games of
the user's library the notes mention are picked out: chips under the
title, a click scrolls to the first mention.

open_notes() is the way in (the Proton page's rows, its status card and
the sidebar's "Proton-GE updated" card).
"""

import webbrowser
from typing import Dict, List, Optional, Sequence

from plunderhub.core import release_notes as rn
from plunderhub.gi_setup import Gdk, GLib, Gtk, Pango
from plunderhub.icons import get_icon_pixbuf
from plunderhub.logging_setup import logger
from plunderhub.perf_guard import allow_hover_relayout
from plunderhub.ui.home_mixin import _accent_rgb

SINCE = "\x00since"          # the "Since your version" row's key
SINCE_MAX = 12               # releases shown at most under it
CHIPS_MAX = 5                # game chips under the title (the rest: "+N")


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(round(c * 255) for c in rgb)


def _icon(name: str, px: int, colour: str) -> Gtk.Widget:
    pb = get_icon_pixbuf(name, px, colour)
    return Gtk.Image.new_from_pixbuf(pb) if pb is not None else Gtk.Label(label="")


def open_link(_label, uri: str) -> bool:
    try:
        webbrowser.open(uri)
    except Exception as e:
        logger.warning(f"Couldn't open {uri}: {e}")
    return True


class NotesView:
    """Notes (core/release_notes.py blocks) as labels in a box - the
    release notes window and the Captain's Log share it."""

    SIZES = {1: "x-large", 2: "large", 3: "medium"}
    # airy lines (Pango 1.50+ knows line_height; older ones get the plain spacing)
    LINE_HEIGHT = "1.3" if Pango.version_check(1, 50, 0) is None else None

    def __init__(self, box: Gtk.Box, accent: str, none_text: str):
        self.box, self.accent, self.none_text = box, accent, none_text
        self.first_mention: Dict[str, Gtk.Widget] = {}
        # room between the points, more between the sections
        box.set_spacing(10)

    def clear(self):
        for child in self.box.get_children():
            child.destroy()
        self.first_mention = {}

    def note(self, text: str):
        label = Gtk.Label(label=text, xalign=0)
        label.set_line_wrap(True)
        label.get_style_context().add_class("settings-row-sub")
        self.box.pack_start(label, False, False, 0)
        label.show()

    def release_head(self, tag: str, date: str, first: bool):
        head = Gtk.Box(spacing=10)
        head.set_margin_top(0 if first else 22)
        title = Gtk.Label(xalign=0)
        title.set_markup(f'<span size="large" weight="heavy">{GLib.markup_escape_text(tag)}</span>')
        head.pack_start(title, False, False, 0)
        when = Gtk.Label(label=date, xalign=0)
        when.get_style_context().add_class("settings-row-sub")
        head.pack_start(when, False, False, 0)
        self.box.pack_start(head, False, False, 0)
        head.show_all()

    def label(self, markup: str) -> Gtk.Label:
        label = Gtk.Label(xalign=0)
        label.set_line_wrap(True)
        label.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        label.set_max_width_chars(110)
        label.set_track_visited_links(False)
        if self.LINE_HEIGHT:
            markup = f'<span line_height="{self.LINE_HEIGHT}">{markup}</span>'
        try:
            label.set_markup(markup)
        except Exception:
            label.set_text(GLib.markup_escape_text(markup))
        label.connect("activate-link", open_link)
        return label

    def render(self, blocks: List[rn.Block]):
        if not blocks:
            self.note(self.none_text)
            return
        for index, block in enumerate(blocks):
            if block.kind == "h":
                widget = self.label(f'<span size="{self.SIZES.get(block.level, "medium")}" weight="bold" '
                                    f'foreground="{self.accent}">{block.markup}</span>')
                widget.set_margin_top(4 if index == 0 else 18)
                widget.set_margin_bottom(4)
            elif block.kind == "li":
                widget = Gtk.Box(spacing=10)
                widget.set_margin_start(4 + 24 * block.level)
                bullet = Gtk.Label(xalign=1)
                # on the same line height as its text, so it sits on its first line
                bullet.set_markup(f'<span line_height="{self.LINE_HEIGHT}">{block.bullet}</span>'
                                  if self.LINE_HEIGHT else block.bullet)
                bullet.set_valign(Gtk.Align.START)
                bullet.set_width_chars(2)
                bullet.get_style_context().add_class("notes-bullet" if block.level == 0 else "settings-row-sub")
                widget.pack_start(bullet, False, False, 0)
                widget.pack_start(self.label(block.markup), True, True, 0)
            elif block.kind == "code":
                widget = Gtk.Box()
                widget.get_style_context().add_class("notes-code")
                code = Gtk.Label(label=block.markup, xalign=0)
                code.set_line_wrap(True)
                code.set_line_wrap_mode(Pango.WrapMode.CHAR)
                code.get_style_context().add_class("notes-code-text")
                widget.pack_start(code, True, True, 0)
            elif block.kind == "hr":
                widget = Gtk.Box()
                widget.get_style_context().add_class("settings-row-sep")
                widget.set_margin_top(8)
                widget.set_margin_bottom(8)
            else:
                widget = self.label(block.markup)
                widget.set_margin_top(2)
                widget.set_margin_bottom(4)
            for game in block.games:
                self.first_mention.setdefault(game, widget)
            self.box.pack_start(widget, False, False, 0)
            widget.show_all()



class ReleaseNotesWindow:
    def __init__(self, app, parent: Optional[Gtk.Window], family: str,
                 releases: Dict[str, Sequence], tag: Optional[str] = None, builds: Sequence = ()):
        """releases: family -> its releases, as far as known (the other
        family's are fetched when switched to). builds: what's installed
        (anything with .family, .tag and .latest)."""
        self.app = app
        self._ = lambda key: app._(f"pm_{key}")
        self.builds = list(builds)
        self.lists: Dict[str, List] = {f: [r for r in rs if getattr(r, "tag", "")]
                                       for f, rs in releases.items() if rs}
        titles = [g.title for g in getattr(app, "game_index", {}).values()
                  if not app.scanner.is_hidden(g.launcher, g.uid)] if hasattr(app, "scanner") else []
        self.games = rn.game_pattern(titles)
        self._parsed: Dict[str, List[rn.Block]] = {}
        self.family = family
        self.releases: List = []
        self.rows: Dict[str, Gtk.ListBoxRow] = {}
        self.current: Optional[str] = None

        self.dialog = dlg = Gtk.Dialog(transient_for=parent, modal=True)
        dlg.set_title(self._("notes_heading"))
        dlg.set_decorated(False)
        visual = dlg.get_screen().get_rgba_visual()
        if visual is not None:
            dlg.set_visual(visual)
        dlg.get_style_context().add_class("home-launch-dialog")
        dlg.get_style_context().add_class("notes-window")
        area = dlg.get_action_area()
        if area is not None:
            area.set_no_show_all(True)
            area.hide()
        dlg.get_content_area().set_border_width(0)
        dlg.set_default_size(*self._size(parent))
        dlg.connect("key-press-event", self._on_key)
        dlg.connect("response", lambda *_: dlg.destroy())

        self.accent = _hex(_accent_rgb(dlg))
        self.view = NotesView(self.content_box(), self.accent, self._("notes_none"))
        body = Gtk.Box()
        dlg.get_content_area().pack_start(body, True, True, 0)
        body.pack_start(self._sidebar(), False, False, 0)
        line = Gtk.Box()
        line.get_style_context().add_class("notes-divider")
        body.pack_start(line, False, False, 0)
        body.pack_start(self._page(), True, True, 0)
        dlg.show_all()
        self._set_family(family, tag)
        # nothing focused (the list's first row would wear a focus ring
        # next to the picked one): the keyboard's arrows work anyway
        GLib.idle_add(lambda: dlg.get_window() is not None and dlg.set_focus(None))
        dlg.connect("map", lambda *_: GLib.idle_add(lambda: dlg.get_window() is not None and dlg.set_focus(None)))

    def content_box(self) -> Gtk.Box:
        self.content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.content.set_margin_end(14)
        self.content.set_margin_bottom(10)
        return self.content

    # --- the family -------------------------------------------------------------------

    def _set_family(self, family: str, tag: Optional[str] = None):
        """Shows the family's releases: its list, and on tag (None:
        "Since your version" when newer ones are out, else the newest)."""
        from plunderhub.core import proton_store
        self.family = family
        self.repo = proton_store.FAMILIES[family].repo
        for key, chip in self.family_chips.items():
            if chip.get_active() != (key == family):
                chip.handler_block(chip.plunderhub_handler)
                chip.set_active(key == family)
                chip.handler_unblock(chip.plunderhub_handler)
        if family not in self.lists:
            self._load(family, tag)
            return
        self.releases = self.lists[family]
        tags = [r.tag for r in self.releases]
        mine = [b for b in self.builds if getattr(b, "family", "") == family]
        self.installed = {b.tag for b in mine if getattr(b, "tag", "")}
        # since the Latest build's release - or else the newest one installed
        since = next((b.tag for b in mine if getattr(b, "latest", False) and b.tag in tags), None)
        if since is None:
            known = [t for t in self.installed if t in tags]
            since = min(known, key=tags.index) if known else None
        self.newer = tags.index(since) if since in tags else 0
        self.since_tag = since if self.newer > 0 else None
        self._fill_list()
        start = SINCE if (tag is None and self.since_tag) else (tag or (tags[0] if tags else None))
        row = self.rows.get(start) or next(iter(self.rows.values()), None)
        if row is None:
            self._show(None)
        elif self.listbox.get_selected_row() is row:
            self._show(row.notes_key)
        else:
            self.listbox.select_row(row)

    def _load(self, family: str, tag: Optional[str]):
        """The family's releases from GitHub (or the saved list), off the
        GTK thread; "loading" meanwhile."""
        import threading
        self._clear_list()
        self.title.set_text(self._("loading"))
        self.sub.set_text("")
        self.pills.foreach(lambda w: w.destroy())
        self.view.clear()
        self.mention_box.set_visible(False)
        self.github_btn.set_visible(False)

        def fetch():
            from plunderhub.core import proton_store
            try:
                found = proton_store.releases(family)
            except Exception as e:
                logger.info(f"Release notes: {family}'s releases: {e}")
                found = proton_store.cached_releases(family)
            GLib.idle_add(done, found)

        def done(found):
            if self.dialog.get_window() is None:
                return False
            self.lists[family] = list(found)
            if self.family == family:
                self._set_family(family, tag)
            return False
        threading.Thread(target=fetch, daemon=True, name="plunderhub-notes").start()

    def _clear_list(self):
        for row in self.listbox.get_children():
            row.destroy()
        self.rows = {}

    def _fill_list(self):
        self._clear_list()
        if self.since_tag:
            self.rows[SINCE] = self._nav_row(
                SINCE, self._("notes_since"),
                self._("notes_since_sub").format(n=self.newer, tag=self.since_tag), special=True)
        for index, release in enumerate(self.releases):
            self.rows[release.tag] = self._nav_row(release.tag, release.tag, release.date,
                                                   dot=release.tag in self.installed, newest=index == 0)
        for row in self.rows.values():
            self.listbox.add(row)
        self.listbox.show_all()

    # --- layout -------------------------------------------------------------------------

    @staticmethod
    def _size(parent):
        width, height = 1120, 780
        try:
            display = Gdk.Display.get_default()
            gdk_window = parent.get_window() if parent is not None else None
            monitor = (display.get_monitor_at_window(gdk_window) if gdk_window is not None else None) \
                or display.get_primary_monitor() or display.get_monitor(0)
            work = monitor.get_workarea()
            width = min(width, int(work.width * 0.92))
            height = min(height, int(work.height * 0.88))
        except Exception:
            pass
        return max(width, 560), max(height, 380)

    def _sidebar(self) -> Gtk.Widget:
        from plunderhub.core import proton_store
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        side.set_size_request(292, -1)
        side.get_style_context().add_class("notes-sidebar")
        head = Gtk.Label(label=self._("notes_heading").upper(), xalign=0)
        head.get_style_context().add_class("settings-group-title")
        head.set_margin_start(22)
        head.set_margin_top(22)
        side.pack_start(head, False, False, 0)
        # Proton-GE or Proton-CachyOS: switched here
        chips = Gtk.Box(spacing=6)
        chips.set_margin_start(16)
        chips.set_margin_end(16)
        chips.set_margin_top(6)
        chips.set_margin_bottom(10)
        self.family_chips: Dict[str, Gtk.ToggleButton] = {}
        for key in proton_store.FAMILIES:
            chip = Gtk.ToggleButton(label=proton_store.FAMILIES[key].latest_name.replace(" Latest", ""))
            chip.get_style_context().add_class("sw-chip")
            # as the Proton page's chips: a small label, lit on purpose
            allow_hover_relayout(chip, "its dimmed text lights up under the pointer")
            chip.set_focus_on_click(False)
            chip.plunderhub_handler = chip.connect(
                "toggled", lambda b, k=key: self._set_family(k) if b.get_active() else
                (k == self.family and b.set_active(True)))
            self.family_chips[key] = chip
            chips.pack_start(chip, True, True, 0)
        side.pack_start(chips, False, False, 0)

        self.listbox = Gtk.ListBox()
        self.listbox.get_style_context().add_class("settings-nav")
        self.listbox.set_selection_mode(Gtk.SelectionMode.BROWSE)
        self.listbox.connect("row-selected", lambda _lb, row: row is not None and self._show(row.notes_key))
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.add(self.listbox)
        scroll.get_style_context().add_class("settings-scroll")
        # always shown, as in Ship's Controls (not GTK's overlay bar)
        scroll.set_overlay_scrolling(False)
        side.pack_start(scroll, True, True, 0)
        hint = Gtk.Label(label="↑ ↓", xalign=0.5)
        hint.get_style_context().add_class("settings-row-sub")
        hint.set_margin_bottom(14)
        hint.set_margin_top(6)
        side.pack_start(hint, False, False, 0)
        return side

    def _nav_row(self, key: str, title: str, sub: str, special: bool = False,
                 dot: bool = False, newest: bool = False) -> Gtk.ListBoxRow:
        row = Gtk.ListBoxRow()
        row.notes_key = key
        box = Gtk.Box(spacing=10)
        if special:
            box.pack_start(_icon("sparkles-symbolic", 18, self.accent), False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        top = Gtk.Box(spacing=6)
        label = Gtk.Label(label=title, xalign=0)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.get_style_context().add_class("notes-nav-title")
        if special:
            label.get_style_context().add_class("sw-nav-accent")
        top.pack_start(label, False, False, 0)
        texts.pack_start(top, False, False, 0)
        bottom = Gtk.Box(spacing=6)
        line = Gtk.Label(label=sub, xalign=0)
        line.set_ellipsize(Pango.EllipsizeMode.END)
        line.get_style_context().add_class("sw-nav-count")
        bottom.pack_start(line, False, False, 0)
        if newest:
            bottom.pack_start(self._pill(self._("notes_newest"), True), False, False, 0)
        texts.pack_start(bottom, False, False, 0)
        box.pack_start(texts, True, True, 0)
        if dot:
            mark = _icon("emblem-ok-symbolic", 16, self.accent)
            mark.set_tooltip_text(self._("notes_installed"))
            box.pack_end(mark, False, False, 0)
        row.add(box)
        return row

    @staticmethod
    def _pill(text: str, accent: bool = False) -> Gtk.Label:
        pill = Gtk.Label(label=text)
        pill.get_style_context().add_class("proton-pill")
        pill.get_style_context().add_class("proton-pill-accent" if accent else "proton-pill-quiet")
        pill.set_valign(Gtk.Align.CENTER)
        return pill

    def _page(self) -> Gtk.Widget:
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        page.set_margin_start(30)
        page.set_margin_end(22)
        page.set_margin_top(20)
        page.set_margin_bottom(18)

        head = Gtk.Box(spacing=12)
        self.title = Gtk.Label(xalign=0)
        self.title.get_style_context().add_class("settings-page-title")
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        self.title.set_selectable(False)
        head.pack_start(self.title, False, False, 0)
        self.pills = Gtk.Box(spacing=6)
        head.pack_start(self.pills, False, False, 0)
        close = Gtk.Button()
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.get_style_context().add_class("notes-close")
        close.add(_icon("window-close-symbolic", 18, _hex(self._fg())))
        close.set_tooltip_text(self.app._("close_btn") + "  (Esc)")
        close.set_valign(Gtk.Align.START)
        close.set_focus_on_click(False)
        close.connect("clicked", lambda *_: self.dialog.destroy())
        head.pack_end(close, False, False, 0)
        page.pack_start(head, False, False, 0)
        self.sub = Gtk.Label(xalign=0)
        self.sub.get_style_context().add_class("settings-page-sub")
        page.pack_start(self.sub, False, False, 0)

        # the library's games the notes mention
        self.mention_box = Gtk.Box(spacing=8)
        self.mention_box.set_margin_top(6)
        self.mention_box.set_no_show_all(False)
        label = Gtk.Label(label=self._("notes_games"), xalign=0)
        label.get_style_context().add_class("settings-row-sub")
        self.mention_box.pack_start(label, False, False, 0)
        self.mention_flow = Gtk.Box(spacing=6)
        self.mention_box.pack_start(self.mention_flow, False, False, 0)
        page.pack_start(self.mention_box, False, False, 0)

        self.scroll = Gtk.ScrolledWindow()
        self.scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.scroll.get_style_context().add_class("notes-scroll")
        self.scroll.get_style_context().add_class("settings-scroll")
        # always shown, as in Ship's Controls (not GTK's overlay bar)
        self.scroll.set_overlay_scrolling(False)
        self.scroll.set_margin_top(8)
        self.scroll.add(self.content)
        page.pack_start(self.scroll, True, True, 0)

        foot = Gtk.Box(spacing=10)
        foot.set_margin_top(10)
        self.github_btn = self.app._dialog_button(self._("notes_github"), "ghost")
        self.github_btn.connect("clicked", lambda *_: self._open_github())
        self.github_btn.set_focus_on_click(False)
        foot.pack_start(self.github_btn, False, False, 0)
        done = self.app._dialog_button(self.app._("close_btn"), "accent", "cancel", "Esc")
        done.set_focus_on_click(False)
        done.connect("clicked", lambda *_: self.dialog.destroy())
        foot.pack_end(done, False, False, 0)
        page.pack_end(foot, False, False, 0)
        return page

    def _fg(self):
        ctx = self.dialog.get_style_context()
        c = ctx.get_color(ctx.get_state())
        return (c.red, c.green, c.blue)

    # --- showing one -------------------------------------------------------------------

    def _blocks(self, release) -> List[rn.Block]:
        key = f"{self.family}/{release.tag}"
        if key not in self._parsed:
            self._parsed[key] = rn.drop_title(
                rn.parse(getattr(release, "notes", ""), self.accent, self.repo, self.games), release.tag)
        return self._parsed[key]

    def _show(self, key: Optional[str]):
        self.current = key
        self.view.clear()
        for child in self.pills.get_children():
            child.destroy()
        for child in self.mention_flow.get_children():
            child.destroy()
        by_tag = {r.tag: r for r in self.releases}
        mentioned: List[str] = []
        if key == SINCE:
            shown = self.releases[:min(self.newer, SINCE_MAX)]
            self.title.set_text(self._("notes_since_title").format(tag=self.since_tag))
            self.sub.set_text(self._("notes_since_line").format(n=self.newer, newest=shown[0].tag))
            for index, release in enumerate(shown):
                self.view.release_head(release.tag, release.date, first=index == 0)
                blocks = self._blocks(release)
                self.view.render(blocks)
                mentioned += [g for g in rn.mentions(blocks) if g not in mentioned]
        elif key in by_tag:
            release = by_tag[key]
            self.title.set_text(release.tag)
            size = getattr(release, "size", 0)
            parts = [release.date] + ([f"{size / (1 << 20):.0f} MB"] if size else [])
            self.sub.set_text("  ·  ".join(p for p in parts if p))
            if release.tag in self.installed:
                self.pills.pack_start(self._pill(self._("notes_installed"), True), False, False, 0)
            if rn.is_hotfix(getattr(release, "notes", "")):
                self.pills.pack_start(self._pill(self._("notes_hotfix")), False, False, 0)
            blocks = self._blocks(release)
            self.view.render(blocks)
            mentioned = rn.mentions(blocks)
        else:
            self.title.set_text(self._("notes_heading"))
            self.sub.set_text("")
            self.view.note(self._("notes_unavailable"))
        self.github_btn.set_visible(key in by_tag or key == SINCE)
        for game in mentioned[:CHIPS_MAX]:
            chip = Gtk.Button()
            label = Gtk.Label(label=game)
            label.set_ellipsize(Pango.EllipsizeMode.END)
            label.set_max_width_chars(24)
            chip.add(label)
            chip.set_tooltip_text(game)
            chip.get_style_context().add_class("notes-game-chip")
            chip.set_focus_on_click(False)
            chip.set_valign(Gtk.Align.CENTER)
            chip.connect("clicked", lambda _b, g=game: self._scroll_to(g))
            self.mention_flow.pack_start(chip, False, False, 0)
        if len(mentioned) > CHIPS_MAX:
            more = Gtk.Label(label=f"+{len(mentioned) - CHIPS_MAX}")
            more.get_style_context().add_class("settings-row-sub")
            more.set_tooltip_text(", ".join(mentioned[CHIPS_MAX:]))
            self.mention_flow.pack_start(more, False, False, 0)
        self.pills.show_all()
        self.content.show_all()
        self.mention_flow.show_all()
        self.mention_box.set_visible(bool(mentioned))
        self.scroll.get_vadjustment().set_value(0)

    # --- doing ---------------------------------------------------------------------------

    def _scroll_to(self, game: str):
        widget = self.view.first_mention.get(game)
        if widget is None:
            return
        coords = widget.translate_coordinates(self.content, 0, 0)
        if coords is not None:
            adj = self.scroll.get_vadjustment()
            adj.set_value(min(max(coords[1] - 12, 0), adj.get_upper() - adj.get_page_size()))

    def _on_link(self, _label, uri: str) -> bool:
        return open_link(_label, uri)

    def _open_github(self):
        tag = self.current if self.current != SINCE else (self.releases[0].tag if self.releases else "")
        self._on_link(None, f"https://github.com/{self.repo}/releases/tag/{tag}")

    def _step(self, delta: int):
        rows = self.listbox.get_children()
        row = self.listbox.get_selected_row()
        index = rows.index(row) + delta if row in rows else 0
        if 0 <= index < len(rows):
            self.listbox.select_row(rows[index])
            rows[index].grab_focus()

    def _on_key(self, _w, event) -> bool:
        key = event.keyval
        if key == Gdk.KEY_Escape:
            self.dialog.destroy()
            return True
        # the list moves on its own arrow keys when it has the focus
        if self.listbox.has_focus() or any(r.has_focus() for r in self.listbox.get_children()):
            return False
        step = {Gdk.KEY_Down: 1, Gdk.KEY_Right: 1, Gdk.KEY_Up: -1, Gdk.KEY_Left: -1}.get(key)
        if step is not None and not event.state & Gdk.ModifierType.CONTROL_MASK:
            self._step(step)
            return True
        return False


def open_notes(app, parent: Optional[Gtk.Window], family: str, tag: Optional[str] = None,
               releases: Optional[Sequence] = None, builds: Sequence = ()) -> ReleaseNotesWindow:
    """The notes, on family (switchable to the other one in the window),
    opened on tag (None: "Since your version" when newer releases are
    out, else the newest). releases: family's list the caller already
    has - else (and the other family's when switched to) fetched in the
    window, the saved list when GitHub can't be reached. builds: what's installed (anything with
    .family, .tag and .latest)."""
    # without a list: fetched in the window (GitHub, else the saved one)
    return ReleaseNotesWindow(app, parent, family, {family: list(releases)} if releases else {}, tag, builds)
