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
"""Which games a newer Proton release mentions (core/proton_mentions.py),
worked out off the GTK thread for the whole library and kept here: the
grid card's badge, the game page's strip, the Launch options line and the
Proton page's pill all ask it.

Read again when the library's games change, a game's Proton is changed
here, a hint is dismissed or a release list is fetched (refresh()). Only
the release lists as last fetched are read - no GitHub calls of its own.
"""

import threading
from typing import Dict, Optional, Tuple

from plunderhub.core.persistence import save_settings
from plunderhub.gi_setup import GLib, Gtk
from plunderhub.logging_setup import logger

DISMISSED_KEY = "proton_hint_dismissed"     # uid -> the release tag it was dismissed for


class ProtonHints:
    def __init__(self, app):
        self.app = app
        # uid -> (Mention, GameProton)
        self.hints: Dict[str, Tuple[object, object]] = {}
        self._library: Optional[frozenset] = None
        self._busy = False
        self._again = False
        # the library may be in before this exists: a first look shortly after
        GLib.timeout_add(3000, lambda: self.library_changed() and False)

    # --- asking -----------------------------------------------------------------------

    def get(self, uid: str, soft: bool = False) -> Optional[Tuple[object, object]]:
        """The game's hint: (Mention, GameProton) - only a hard one (a
        pinned older build) unless soft; never one dismissed for that
        release."""
        found = self.hints.get(uid)
        if found is None:
            return None
        mention = found[0]
        if mention.soft and not soft:
            return None
        if not mention.soft and self.app.settings.get(DISMISSED_KEY, {}).get(uid) == mention.tag:
            return None
        return found

    # --- working it out ------------------------------------------------------------------

    def library_changed(self):
        """The library's games may have changed: read again if they did."""
        uids = frozenset(self.app.game_index)
        if uids != self._library:
            self._library = uids
            self.refresh()

    def refresh(self):
        if self._busy:
            self._again = True
            return
        self._busy = True
        games = [g for g in self.app.game_index.values()
                 if not self.app.scanner.is_hidden(g.launcher, g.uid)]

        def work():
            from plunderhub.core import proton_mentions, proton_store
            from plunderhub.ui.proton_page import _game_proton
            found = {}
            try:
                releases = {f: proton_store.cached_releases(f) for f in proton_store.FAMILIES}
                builds = proton_store.installed()
                for game in games:
                    entry = _game_proton(game)
                    if entry is None or entry.native:
                        continue
                    mention = proton_mentions.find(game.title, entry.proton_name(), releases, builds)
                    if mention is not None:
                        found[game.uid] = (mention, entry)
            except Exception as e:
                logger.warning(f"Proton hints: {e}")
            GLib.idle_add(done, found)

        def done(found):
            self._busy = False
            changed = {u: m.tag for u, (m, _e) in found.items()} != {u: m.tag for u, (m, _e) in self.hints.items()}
            self.hints = found
            if changed:
                hard = sum(1 for m, _e in found.values() if not m.soft)
                logger.info(f"Proton hints: {hard} game(s) a newer release mentions, {len(found) - hard} soft")
                self._redraw()
            if self._again:
                self._again = False
                self.refresh()
            return False
        threading.Thread(target=work, daemon=True, name="plunderhub-proton-hints").start()

    def _redraw(self):
        from plunderhub.ui.home_mixin import GameCard
        for card in list(GameCard.live):
            card.deco.queue_draw()
        listener = getattr(self.app, "_on_proton_hints_changed", None)
        if listener is not None:
            listener()

    # --- doing ---------------------------------------------------------------------------

    def dismiss(self, uid: str):
        found = self.hints.get(uid)
        if found is None:
            return
        dismissed = dict(self.app.settings.get(DISMISSED_KEY, {}))
        dismissed[uid] = found[0].tag
        self.app.settings[DISMISSED_KEY] = dismissed
        save_settings(self.app.settings)
        self._redraw()

    def latest_choice(self, uid: str) -> Optional[str]:
        """The game's Proton choice that is its family's Latest, if its
        launcher lists it."""
        from plunderhub.core import proton_store
        from plunderhub.ui.proton_page import _proton_name
        found = self.hints.get(uid)
        if found is None:
            return None
        mention, entry = found
        latest = proton_store.FAMILIES[mention.family].latest_name
        for choice, label in entry.choices:
            if _proton_name(choice if "/" in choice else label) == latest or label.startswith(latest):
                return choice
        return None

    def switch(self, uid: str, parent: Gtk.Window) -> bool:
        """The game onto its family's Latest (it carries every newer fix and
        keeps moving on); False when its launcher doesn't list it."""
        from plunderhub.ui.proton_page import RealProtonData
        found = self.hints.get(uid)
        choice = self.latest_choice(uid)
        if found is None or choice is None:
            return False
        if not RealProtonData(self.app).set_proton(found[1], choice, parent):
            return False
        self.hints.pop(uid, None)
        self._redraw()
        self.refresh()
        return True

    def act(self, uid: str, parent: Gtk.Window, on_status, on_done):
        """Gets the game the release that mentions it - see Mention.action:
        the family's Latest downloaded or updated first when it isn't there
        or is behind, then the game switched to it. on_status(text) while
        that runs; on_done(ok, choice or None, error text or None)."""
        found = self.hints.get(uid)
        if found is None:
            on_done(False, None, None)
            return
        mention, entry = found
        action = mention.action
        if action == "switch":
            choice = self.latest_choice(uid)
            ok = self.switch(uid, parent)
            on_done(ok, choice if ok else None, None)
            return

        def latest_ready(ok: bool, error: Optional[str]):
            if not ok:
                on_done(False, None, error)
                return
            if action == "update":
                self.hints.pop(uid, None)
                self._redraw()
                self.refresh()
                on_done(True, None, None)
                return
            # the launcher lists the Latest now: its choices read again
            from plunderhub.ui.proton_page import _game_proton
            fresh = _game_proton(entry.game) if entry.game is not None else None
            if fresh is not None:
                self.hints[uid] = (mention, fresh)
            choice = self.latest_choice(uid)
            ok = choice is not None and self.switch(uid, parent)
            on_done(ok, choice if ok else None, None if ok else self.app._("ph_switch_failed"))

        if not self._get_latest(mention.family, on_status, latest_ready):
            on_done(False, None, self.app._("ph_busy"))

    def _get_latest(self, family: str, on_status, on_finished) -> bool:
        """The family's Latest updated (or downloaded) by the Proton manager's
        own job - the sidebar's card shows it too; False when another job
        runs. on_status(text) as it goes, on_finished(ok, error)."""
        from plunderhub.ui import proton_jobs
        from plunderhub.ui.proton_page import S, problem_text
        jobs = proton_jobs.jobs(self.app)
        if not jobs.update_latest(family, install_missing=True):
            return False
        mine = jobs.job          # set by update_latest before its thread starts

        def changed():
            job = jobs.job
            if job is mine:
                if job.stage == "download" and job.total:
                    on_status(S["st_downloading"].format(tag=job.tag or job.name, pct=job.percent(),
                                                         done=job.done >> 20, total=job.total >> 20))
                else:
                    on_status(S["st_checking"])
                return
            if jobs.last is mine:
                stop()
                if mine.error and mine.error != "cancelled":
                    on_finished(False, problem_text(mine.problem) if mine.problem is not None else mine.error)
                else:
                    on_finished(not mine.error, None)
        stop = jobs.listen(changed)
        changed()
        return True

    def read(self, uid: str, parent: Gtk.Window):
        """The release notes window on the release that mentions the game."""
        from plunderhub.core import proton_store
        from plunderhub.ui.release_notes_window import open_notes
        found = self.hints.get(uid)
        if found is None:
            return
        mention = found[0]
        open_notes(self.app, parent, mention.family, mention.tag,
                   proton_store.cached_releases(mention.family), proton_store.installed())


def hint_strip(app, game, window_of, on_switched=None, soft: bool = False) -> Gtk.Widget:
    """The game's hint as a strip: what release mentions it and the
    sentence itself, Read / Switch to Proton-GE Latest / dismiss. Empty
    (an invisible box) without a hint. window_of() gives the window the
    buttons open over; on_switched(choice) runs after a switch (the Launch
    options tab keeps its picker in step). soft: also a hint for a game on
    another Proton (Launch options), which has no Switch or dismiss."""
    from plunderhub.icons import get_icon_pixbuf
    from plunderhub.ui.home_mixin import _accent_rgb
    _ = app._
    holder = Gtk.Box()
    hints = getattr(app, "proton_hints", None)
    found = hints.get(game.uid, soft=soft) if hints is not None else None
    if found is None:
        holder.set_no_show_all(True)
        return holder
    mention = found[0]
    strip = Gtk.Box(spacing=14)
    strip.get_style_context().add_class("ph-strip")
    accent = "#%02x%02x%02x" % tuple(round(c * 255) for c in _accent_rgb(strip))
    pb = get_icon_pixbuf("go-up-symbolic", 22, accent)
    icon = Gtk.Image.new_from_pixbuf(pb) if pb is not None else Gtk.Box()
    icon.set_valign(Gtk.Align.START)
    icon.set_margin_top(2)
    strip.pack_start(icon, False, False, 0)

    texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
    if mention.soft:
        head = _("ph_soft_title").format(tag=mention.tag)
    else:
        head = _("ph_fix_title" if mention.fix else "ph_title").format(tag=mention.tag)
    title = Gtk.Label(label=head, xalign=0)
    title.set_line_wrap(True)
    title.get_style_context().add_class("ph-title")
    texts.pack_start(title, False, False, 0)
    from plunderhub.core import proton_store
    latest = proton_store.FAMILIES[mention.family].latest_name
    if mention.on_latest:
        line = _("ph_latest_behind").format(latest=latest, held=mention.latest_tag)
    elif mention.current:
        line = _("ph_current").format(current=mention.current)
        if mention.action == "download_switch":
            line += "  ·  " + _("ph_latest_missing").format(latest=latest)
        elif mention.action == "update_switch":
            line += "  ·  " + _("ph_latest_behind").format(latest=latest, held=mention.latest_tag)
    else:
        line = ""
    sub = None
    if line:
        sub = Gtk.Label(label=line, xalign=0)
        sub.set_line_wrap(True)
        sub.get_style_context().add_class("settings-row-sub")
        texts.pack_start(sub, False, False, 0)
    quote = Gtk.Label(label=f"“{mention.excerpt}”", xalign=0)
    quote.set_line_wrap(True)
    quote.set_max_width_chars(90)
    quote.get_style_context().add_class("ph-quote")
    texts.pack_start(quote, False, False, 0)

    buttons = Gtk.Box(spacing=8)
    buttons.set_margin_top(6)
    read = app._dialog_button(_("ph_read"), "ghost")
    read.set_focus_on_click(False)
    read.connect("clicked", lambda *_a: hints.read(game.uid, window_of()))
    buttons.pack_start(read, False, False, 0)
    done = Gtk.Label(label="", xalign=0)
    done.set_line_wrap(True)
    done.get_style_context().add_class("settings-status-on")
    action = mention.action
    if action is not None:
        key = {"switch": "ph_switch", "update_switch": "ph_update_switch",
               "download_switch": "ph_download_switch", "update": "ph_update"}[action]
        go = app._dialog_button(_(key).format(latest=latest), "accent")
        go.set_focus_on_click(False)

        def status(text: str):
            done.get_style_context().remove_class("ph-error")
            done.set_text(text)
            done.show()

        def finished(ok: bool, choice, error):
            if ok:
                if sub is not None:
                    sub.hide()      # what it said about the builds is past
                done.set_text("✓ " + (_("ph_updated") if action == "update" else _("ph_switched")).format(
                    latest=latest))
                if choice is not None and on_switched is not None:
                    on_switched(choice)
            else:
                buttons.foreach(lambda w: w is not done and w.show())
                done.get_style_context().add_class("ph-error")
                done.set_text(error or _("ph_switch_failed"))
            done.show()

        def on_go(*_a):
            buttons.foreach(lambda w: w is not done and w.hide())
            hints.act(game.uid, window_of(), status, finished)
        go.connect("clicked", on_go)
        buttons.pack_start(go, False, False, 0)
    buttons.pack_start(done, False, False, 0)
    done.set_no_show_all(True)
    texts.pack_start(buttons, False, False, 0)
    strip.pack_start(texts, True, True, 0)

    if not mention.soft:
        close = Gtk.Button()
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.get_style_context().add_class("notes-close")
        pbx = get_icon_pixbuf("window-close-symbolic", 14, "#ffffff")
        if pbx is not None:
            close.add(Gtk.Image.new_from_pixbuf(pbx))
        close.set_tooltip_text(_("ph_dismiss"))
        close.set_valign(Gtk.Align.START)
        close.set_focus_on_click(False)

        def on_close(*_a):
            hints.dismiss(game.uid)
            holder.hide()
        close.connect("clicked", on_close)
        strip.pack_end(close, False, False, 0)
    holder.pack_start(strip, True, True, 0)
    return holder
