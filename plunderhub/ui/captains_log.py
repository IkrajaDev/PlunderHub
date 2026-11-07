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
"""The Captain's Log: what changed in this PlunderHub version - the text
of its GitHub release - in a card like the welcome card, with
"Got it, Captain!".

after_start() shows it by itself ONCE, on the first start after an
update - never after a first install (the welcome card is that one's
greeting; App._maybe_show_first_run_tip marks the version seen then).
Ship's Controls -> System -> About opens it any time (show()).
"""

import re
import threading
import webbrowser
from typing import Optional

from plunderhub.constants import APP_VERSION
from plunderhub.core import release_notes as rn
from plunderhub.core.persistence import save_settings
from plunderhub.core.update_check import GITHUB_REPO, fetch_release_notes, is_newer_version
from plunderhub.gi_setup import Gdk, GLib, Gtk
from plunderhub.logging_setup import logger
from plunderhub.ui.progress_card import card_dialog
from plunderhub.ui.release_notes_window import NotesView

SEEN_KEY = "log_seen_version"
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases"
MAX_NOTES_PX = 500           # the notes panel's height at most (then it scrolls)


def after_start(app):
    """Once per update: this version's log, when its notes can be had
    (else tried again at the next start)."""
    seen = app.settings.get(SEEN_KEY)
    if seen == APP_VERSION:
        return
    if seen and not is_newer_version(APP_VERSION, seen):
        # an older version than the last one seen (went back): nothing new
        mark_seen(app)
        return

    def fetch():
        info = fetch_release_notes(APP_VERSION)
        GLib.idle_add(ready, info)

    def ready(info):
        if info is None:
            logger.info(f"Captain's Log: no notes for {APP_VERSION} yet - asked again at the next start")
            return False
        mark_seen(app)
        show(app, app, info)
        return False
    threading.Thread(target=fetch, daemon=True, name="plunderhub-captains-log").start()


def mark_seen(app):
    app.settings[SEEN_KEY] = APP_VERSION
    save_settings(app.settings)


def _drop_version_heading(blocks, version: str):
    """Without a first heading that only names the version ("PlunderHub
    1.0.2") - the card says it already."""
    if blocks and blocks[0].kind == "h":
        text = re.sub(r"<[^>]+>", "", blocks[0].markup)
        if version.lstrip("v") in text and len(text) < 40:
            # kept when an older version's own section follows (a hotfix
            # repeating the release before it): both read alike then
            later = [re.sub(r"<[^>]+>", "", b.markup) for b in blocks[1:]
                     if b.kind == "h" and b.level == blocks[0].level]
            if any(re.search(r"\d+\.\d+\.\d+", t) and len(t) < 40 for t in later):
                return blocks
            return blocks[1:]
    return blocks


def show(app, parent: Optional[Gtk.Window] = None, info: Optional[dict] = None,
         version: str = APP_VERSION):
    """The log card for version; without info, fetched while it shows."""
    _ = app._
    dlg, column = card_dialog(parent or app._dialog_transient_parent(), _("log_title"))
    dlg.get_style_context().add_class("captains-log")
    column.set_size_request(720, -1)
    column.set_spacing(8)
    accent = app._home_accent_hex()

    title = Gtk.Label(label=_("log_title"), xalign=0)
    title.get_style_context().add_class("home-dialog-title")
    title.get_style_context().add_class("welcome-title")
    sub = Gtk.Label(label=_("log_sub").format(version=version), xalign=0)
    sub.set_line_wrap(True)
    sub.set_max_width_chars(48)
    sub.get_style_context().add_class("dim-label")
    when = Gtk.Label(label="", xalign=0)
    when.get_style_context().add_class("dim-label")
    when.get_style_context().add_class("welcome-small")
    words = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    words.set_valign(Gtk.Align.CENTER)
    for widget in (title, sub, when):
        words.pack_start(widget, False, False, 0)

    # the notes: as tall as they are, up to a scroll
    scroll = Gtk.ScrolledWindow()
    scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroll.set_propagate_natural_height(True)
    scroll.set_max_content_height(MAX_NOTES_PX)
    # GTK 3 measures the wrapped labels before their width is known (too
    # short): a floor that fits a usual release's notes without scrolling
    scroll.set_min_content_height(330)
    scroll.get_style_context().add_class("log-notes")
    # the bright accent scrollbar, as in Ship's Controls and Shipwright
    scroll.get_style_context().add_class("settings-scroll")
    # always shown, as in Ship's Controls (not GTK's overlay bar)
    scroll.set_overlay_scrolling(False)
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    box.set_margin_start(18)
    box.set_margin_end(18)
    box.set_margin_top(14)
    box.set_margin_bottom(14)
    scroll.add(box)
    view = NotesView(box, accent, _("log_empty"))

    foot = Gtk.Box(spacing=12)
    foot.set_margin_top(10)
    every = Gtk.Button(label=_("log_all"))
    every.get_style_context().add_class("about-quiet")
    every.get_style_context().add_class("welcome-quiet")
    every.set_can_focus(False)
    every.set_valign(Gtk.Align.CENTER)
    every.set_tooltip_text(RELEASES_URL)
    every.connect("clicked", lambda *_a: webbrowser.open(RELEASES_URL))
    foot.pack_start(every, False, False, 0)
    got_it = app._dialog_button(_("tip_got_it"), "accent", "activate", "Enter")
    got_it.set_valign(Gtk.Align.CENTER)
    got_it.connect("clicked", lambda *_a: dlg.response(Gtk.ResponseType.OK))
    foot.pack_end(got_it, False, False, 0)

    def fill(found: Optional[dict]):
        view.clear()
        if found is None:
            view.note(_("log_failed"))
            return False
        when.set_text(found.get("date", ""))
        blocks = _drop_version_heading(rn.parse(found.get("notes", ""), accent, GITHUB_REPO), version)
        view.render(blocks)
        return False

    # GTK 3 asks the wrapped labels for their height before it knows their
    # width (too short): once the width is known, the panel is made as
    # tall as the notes really are - up to MAX_NOTES_PX, then it scrolls
    def fit(_box, allocation):
        if allocation.width <= 1:
            return
        # the size requests count the box's margins, its allocation doesn't:
        # asked for the width with them, the answer has them already
        width = allocation.width + box.get_margin_start() + box.get_margin_end()
        _minimum, natural = box.get_preferred_height_for_width(width)
        wanted = min(MAX_NOTES_PX, natural)
        if abs(wanted - scroll.get_min_content_height()) > 2:
            GLib.idle_add(lambda: scroll.set_min_content_height(wanted) and False)
    box.connect("size-allocate", fit)

    top = Gtk.Box(spacing=8)
    top.pack_start(app._popping_logo(96, glow=True, then=[words, scroll, foot], glow_room=(48, 32)),
                   False, False, 0)
    top.pack_start(words, True, True, 0)
    for widget in (top, scroll, foot):
        column.pack_start(widget, False, False, 0)

    if info is not None:
        fill(info)
    else:
        view.note(_("log_loading"))

        def fetch():
            found = fetch_release_notes(version)
            GLib.idle_add(lambda: dlg.get_window() is not None and fill(found))
        threading.Thread(target=fetch, daemon=True, name="plunderhub-captains-log").start()

    def on_key(_w, event) -> bool:
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter, Gdk.KEY_Escape):
            dlg.response(Gtk.ResponseType.OK)
            return True
        return False
    dlg.connect("key-press-event", on_key)
    dlg.plunderhub_nav = lambda action: action in ("activate", "back") and dlg.response(Gtk.ResponseType.OK)
    dlg.get_content_area().show_all()
    app._enter_dimmed_dialog()
    try:
        dlg.run()
    finally:
        app._exit_dimmed_dialog()
    dlg.destroy()
