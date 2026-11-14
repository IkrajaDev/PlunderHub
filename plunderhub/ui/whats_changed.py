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

""""What changed?" - a game that didn't start or crashed, next to the last
launch that went well (core/launch_history.py keeps them).

It opens by itself only on a sure sign:
- the launcher said the launch failed, or a game PlunderHub ran itself
  ended with an error code (a crash);
- a game handed to its launcher (Steam, Heroic, a store launcher, Lutris,
  Bottles) was gone within NO_START_SECONDS of running - those give no
  exit code, and nobody quits a game in the first seconds.
Never for a game closed from PlunderHub, or one that ended by itself
without an error (quit in the game). The game's menu opens it any time.

The way back: the Proton of the last good launch - downloaded first when
it isn't there any more (the exact release a "Latest" held then)."""

import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from plunderhub.core import launch_history as history
from plunderhub.core.treasure import open_uri
from plunderhub.gi_setup import Gdk, GLib, Gtk, Pango
from plunderhub.icons import get_icon_pixbuf
from plunderhub.logging_setup import logger

NO_START_SECONDS = 10.0
SETTING = "whats_changed_popup"         # opens by itself (Settings), on by default
MUTED = "whats_changed_muted"           # games it doesn't open by itself for

_ICONS = {"proton": "wine-symbolic", "options": "preferences-other-symbolic", "game": "package-symbolic",
          "driver": "monitor-symbolic", "kernel": "cpu-symbolic", "plunderhub": "anchor-symbolic"}


def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(round(c * 255) for c in rgb)


def _icon(name: str, px: int, colour: str) -> Gtk.Widget:
    pb = get_icon_pixbuf(name, px, colour)
    return Gtk.Image.new_from_pixbuf(pb) if pb is not None else Gtk.Label(label="")


def _state(app) -> Dict:
    state = getattr(app, "_wc_state", None)
    if state is None:
        state = app._wc_state = {"at": {}, "threads": {}, "showing": set()}
    return state


# --- following the launches ------------------------------------------------------------------

def launch_started(app, uid: str):
    game = app.game_index.get(uid)
    # a store launcher in Faugus isn't a game: nothing to follow
    if game is None or "--self-test" in sys.argv or game.store_platform == "store-launcher":
        return
    state = _state(app)
    at = time.time()
    state["at"][uid] = at

    def work():
        try:
            history.record_start(uid, at, history.take(app, game))
        except Exception as e:
            logger.warning(f"Launch snapshot of {game.title} failed: {e}")
    thread = threading.Thread(target=work, name="plunderhub-launch-snapshot", daemon=True)
    state["threads"][uid] = thread
    thread.start()


def _ran(app, uid: str) -> float:
    since = app.running_since.get(uid)
    if since is None:
        return 0.0
    end = app._gone_since.get(uid, time.monotonic())
    return max(0.0, end - since)


def _handed_over(app, uid: str) -> bool:
    """Its launcher starts it: no exit code reaches PlunderHub."""
    return uid.startswith(("steam:", "heroic:", "lutris:", "bottles:")) or app._is_launcher_game(uid)


def launch_closed(app, uid: str):
    """Closed from PlunderHub (Stop): ended ok, whenever it was."""
    _end(app, uid, _ran(app, uid), "ok", "")


def launch_ended(app, uid: str, failed: bool, crashed: bool, error: str = ""):
    """The game's session is over (App._poll_running_games, before it
    forgets the session)."""
    if uid not in _state(app)["at"]:
        return
    ran = _ran(app, uid)
    if failed:
        result = "failed"
        error = app._launch_failed_text(uid, error)     # what the launcher said
    elif crashed:
        result = "crashed"
    elif _handed_over(app, uid) and uid in app._directory_confirmed_running and ran < NO_START_SECONDS:
        result = "failed"       # it was there, and gone at once
    else:
        result = "ok"
    _end(app, uid, ran, result, error)


def takes_over(app, uid: str) -> bool:
    """Whether the window tells about this failed launch (instead of the
    plain "couldn't start" dialog)."""
    return uid in _state(app)["showing"]


def _wanted(app, uid: str) -> bool:
    if "--self-test" in sys.argv or not app.settings.get(SETTING, True):
        return False
    return uid not in (app.settings.get(MUTED) or [])


def _end(app, uid: str, ran: float, result: str, error: str):
    state = _state(app)
    at = state["at"].pop(uid, None)
    if at is None:
        return
    thread = state["threads"].pop(uid, None)
    show = result in ("failed", "crashed") and _wanted(app, uid)
    if show:
        state["showing"].add(uid)

    def work():
        if thread is not None:
            thread.join(10)
        session = history.record_end(uid, at, ran, result)
        if show:
            GLib.idle_add(_open_after, app, uid, session, error)
    threading.Thread(target=work, name="plunderhub-launch-end", daemon=True).start()


def _open_after(app, uid: str, session: Optional[Dict], error: str) -> bool:
    _state(app)["showing"].discard(uid)
    game = app.game_index.get(uid)
    if game is None:
        return False
    if session is None:
        # nothing to compare: the plain dialog
        app._show_info_dialog(error or app._launch_failed_text(uid, ""), Gtk.MessageType.ERROR,
                              heading=app._("wc_failed").format(name=game.title))
        return False
    WhatsChanged(app, game, session=session, error=error, by_itself=True)
    return False


def open_for(app, game):
    """The game menu's "What changed?": the last launch, whatever it was."""
    WhatsChanged(app, game)


# --- the window ------------------------------------------------------------------------------

class WhatsChanged:
    def __init__(self, app, game, session: Optional[Dict] = None, error: str = "", by_itself: bool = False,
                 good: Optional[Dict] = None):
        self.app, self.game, self._ = app, game, app._
        self.error, self.by_itself = error, by_itself
        sessions = history.load(game.uid)
        self.session = session or (sessions[-1] if sessions else None)
        before = self.session.get("at") if self.session else None
        # by hand after a good launch: compared with the good one before it
        self.good = good or (history.last_good(sessions, before=before) if self.session else None)
        self.sessions = sessions
        self.busy = False
        self.undo_wanted = False

        self.dialog = dlg = Gtk.Dialog(transient_for=app._dialog_transient_parent(), modal=True)
        dlg.set_title(self._("wc_title"))
        dlg.set_decorated(False)
        visual = dlg.get_screen().get_rgba_visual()
        if visual is not None:
            dlg.set_visual(visual)
        ctx = dlg.get_style_context()
        ctx.add_class("home-launch-dialog")
        ctx.add_class("wc-window")
        area = dlg.get_action_area()
        if area is not None:
            area.set_no_show_all(True)
            area.hide()
        dlg.get_content_area().set_border_width(0)
        _w, h = app.get_size()
        dlg.set_default_size(820, -1)
        from plunderhub.ui.home_mixin import _accent_rgb
        self.accent = _hex(_accent_rgb(dlg))

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_propagate_natural_height(True)
        scroller.set_max_content_height(max(420, min(h, dlg.get_screen().get_height()) - 140))
        scroller.set_overlay_scrolling(False)
        scroller.get_style_context().add_class("settings-scroll")
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.body.set_border_width(26)
        scroller.add(self.body)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        footer = self._footer()     # first: it says whether the options card offers Undo
        outer.pack_start(self._head(), False, False, 0)
        outer.pack_start(scroller, True, True, 0)
        outer.pack_start(footer, False, False, 0)
        dlg.get_content_area().pack_start(outer, True, True, 0)
        self._fill()
        dlg.connect("key-press-event", self._on_key)
        dlg.connect("response", lambda *_: self._close())
        app._enable_keyboard_focus_ring_detection(dlg)
        dlg.show_all()
        GLib.idle_add(lambda: dlg.get_window() is not None and dlg.set_focus(None))

    # the top: the game, what happened -----------------------------------------------------

    def _head(self) -> Gtk.Widget:
        _ = self._
        head = Gtk.Box(spacing=16)
        head.get_style_context().add_class("wc-head")
        result = (self.session or {}).get("result")
        tone = "wc-bad" if result in ("failed", "crashed") else "wc-calm"
        head.get_style_context().add_class(tone)
        icon = self.app._create_game_icon_image(self.game, 52)
        icon.set_valign(Gtk.Align.CENTER)
        head.pack_start(icon, False, False, 0)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        titles.set_valign(Gtk.Align.CENTER)
        kicker = Gtk.Label(label=_("wc_title").upper(), xalign=0)
        kicker.get_style_context().add_class("wc-kicker")
        title = Gtk.Label(label=self._headline(), xalign=0)
        title.set_line_wrap(True)
        title.set_max_width_chars(44)
        title.get_style_context().add_class("wc-headline")
        titles.pack_start(kicker, False, False, 0)
        titles.pack_start(title, False, False, 0)
        sub = self._subline()
        if sub:
            line = Gtk.Label(label=sub, xalign=0)
            line.set_line_wrap(True)
            line.get_style_context().add_class("dim-label")
            titles.pack_start(line, False, False, 0)
        head.pack_start(titles, True, True, 0)
        close = Gtk.Button()
        close.get_style_context().add_class("notes-close")
        close.add(self.app._icon_image_at_size("window-close-symbolic", 18))   # the theme's colour: light themes too
        close.set_tooltip_text(_("close_btn"))
        close.set_valign(Gtk.Align.START)
        close.connect("clicked", lambda *_a: self.dialog.response(Gtk.ResponseType.CLOSE))
        head.pack_start(close, False, False, 0)
        return head

    def _headline(self) -> str:
        _, title = self._, self.game.title
        s = self.session or {}
        if s.get("result") == "failed":
            return _("wc_failed").format(name=title)
        if s.get("result") == "crashed":
            return _("wc_crashed").format(name=title, time=self._duration(s.get("ran") or 0))
        return _("wc_by_hand").format(name=title)

    def _subline(self) -> str:
        _ = self._
        if self.session is None:
            return _("wc_no_history")
        if self.good is None:
            return _("wc_no_good")
        return _("wc_since").format(when=self._when(self.good.get("at")),
                                    time=self._duration(self.good.get("ran") or 0))

    def _duration(self, seconds: float) -> str:
        seconds = int(seconds)
        if seconds < 60:
            return self._("wc_seconds").format(n=seconds)
        hours, rem = divmod(seconds // 60, 60)
        return f"{hours}:{rem:02d} h" if hours else self._("wc_minutes").format(n=rem)

    def _when(self, stamp: Optional[float]) -> str:
        if not stamp:
            return ""
        days = (time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1)) - stamp) / 86400
        clock = time.strftime("%H:%M", time.localtime(stamp))
        if days <= 0:
            return self._("wc_today").format(time=clock)
        if days <= 1:
            return self._("wc_yesterday").format(time=clock)
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))

    # the comparison -------------------------------------------------------------------------

    def _fill(self):
        for child in self.body.get_children():
            child.destroy()
        _ = self._
        if self.error:
            box = Gtk.Box(spacing=10)
            box.get_style_context().add_class("wc-error")
            box.pack_start(_icon("triangle-alert-symbolic", 18, "#f0a04b"), False, False, 0)
            text = Gtk.Label(label=self.error, xalign=0)
            text.set_line_wrap(True)
            text.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
            text.set_selectable(True)
            box.pack_start(text, True, True, 0)
            self.body.pack_start(box, False, False, 0)
        if self.session is None:
            self.body.pack_start(self._note(_("wc_no_history_long")), False, False, 0)
            self.body.show_all()
            return
        rows = history.diff(self.good["snap"] if self.good else None, self.session["snap"])
        changed = [r for r in rows if r[3]]
        same = [r for r in rows if not r[3]]
        rescue = self._rescue_card()
        if rescue is not None:
            self.body.pack_start(rescue, False, False, 0)
        if self.good is not None:
            heading = _("wc_changed_n").format(n=len(changed)) if changed else _("wc_nothing_changed")
            self.body.pack_start(self._section(heading), False, False, 0)
            if changed:
                self.body.pack_start(self._grid(changed, compare=True), False, False, 0)
            else:
                self.body.pack_start(self._note(_("wc_nothing_changed_long")), False, False, 0)
            if same:
                expander = Gtk.Expander(label=_("wc_same_n").format(n=len(same)))
                expander.get_style_context().add_class("wc-expander")
                expander.add(self._grid(same, compare=False))
                self.body.pack_start(expander, False, False, 0)
        else:
            self.body.pack_start(self._section(_("wc_runs_with")), False, False, 0)
            self.body.pack_start(self._grid(rows, compare=False), False, False, 0)
        log = self._log_box()
        if log is not None:
            self.body.pack_start(log, False, False, 0)
        self.body.show_all()

    def _section(self, text: str) -> Gtk.Widget:
        label = Gtk.Label(label=text.upper(), xalign=0)
        label.get_style_context().add_class("wc-section")
        return label

    def _note(self, text: str) -> Gtk.Widget:
        label = Gtk.Label(label=text, xalign=0)
        label.set_line_wrap(True)
        label.get_style_context().add_class("dim-label")
        return label

    def _value_text(self, name: str, text: str) -> str:
        if name == "proton" and text == "native":
            return self._("wc_native")
        if name == "options" and not text:
            return self._("wc_none")
        return text or "—"

    def _grid(self, rows, compare: bool) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for name, was, now, changed in rows:
            card = Gtk.Box(spacing=14)
            card.get_style_context().add_class("wc-row")
            if changed:
                card.get_style_context().add_class("wc-row-changed")
            badge = Gtk.Box()
            badge.get_style_context().add_class("wc-badge")
            badge.set_valign(Gtk.Align.CENTER)
            badge.pack_start(_icon(_ICONS.get(name, "dialog-information-symbolic"), 18,
                                   self.accent if changed else "#9aa4b2"), True, True, 0)
            card.pack_start(badge, False, False, 0)
            column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            column.set_valign(Gtk.Align.CENTER)
            field = Gtk.Label(label=self._(f"wc_f_{name}"), xalign=0)
            field.get_style_context().add_class("wc-field")
            column.pack_start(field, False, False, 0)
            values = Gtk.Box(spacing=10)
            if compare and changed and name == "options":
                column.pack_start(self._word_diff(was, now), False, False, 0)
                values = None
            elif compare and changed:
                old = Gtk.Label(xalign=0)
                old.set_markup(f"<s>{GLib.markup_escape_text(self._value_text(name, was))}</s>")
                old.get_style_context().add_class("wc-old")
                old.set_line_wrap(True)
                old.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
                old.set_max_width_chars(30)
                values.pack_start(old, False, False, 0)
                values.pack_start(_icon("go-next-symbolic", 16, self.accent), False, False, 0)
            if values is not None:
                new = Gtk.Label(label=self._value_text(name, now), xalign=0)
                new.get_style_context().add_class("wc-new" if changed else "wc-value")
                new.set_line_wrap(True)
                new.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
                new.set_max_width_chars(40)
                new.set_selectable(True)
                values.pack_start(new, False, False, 0)
                column.pack_start(values, False, False, 0)
            card.pack_start(column, True, True, 0)
            if changed and name == "options" and self.undo_wanted:
                undo = Gtk.Button(label=self._("wc_undo_options"))
                undo.get_style_context().add_class("sw-link")
                undo.set_valign(Gtk.Align.CENTER)
                undo.connect("clicked", lambda b: self._undo_options() and (
                    b.set_label(self._("wc_undone")), b.set_sensitive(False)))
                card.pack_end(undo, False, False, 0)
            elif changed:
                pill = Gtk.Label(label=self._("wc_changed"))
                pill.get_style_context().add_class("proton-pill")
                pill.get_style_context().add_class("proton-pill-accent")
                pill.set_valign(Gtk.Align.CENTER)
                card.pack_end(pill, False, False, 0)
            box.pack_start(card, False, False, 0)
        return box

    def _word_diff(self, was: str, now: str) -> Gtk.Widget:
        """Launch options: the words that came and went as chips, the
        whole line as it is now under them."""
        import shlex
        try:
            old_words, new_words = shlex.split(was or ""), shlex.split(now or "")
        except ValueError:
            old_words, new_words = (was or "").split(), (now or "").split()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        chips = Gtk.FlowBox()
        chips.set_selection_mode(Gtk.SelectionMode.NONE)
        chips.set_max_children_per_line(6)
        chips.set_column_spacing(6)
        chips.set_row_spacing(6)
        chips.set_homogeneous(False)
        for word in [w for w in new_words if w not in old_words]:
            chips.add(self._chip(f"+ {word}", "wc-chip-add"))
        for word in [w for w in old_words if w not in new_words]:
            chips.add(self._chip(f"\u2212 {word}", "wc-chip-gone"))
        box.pack_start(chips, False, False, 0)
        line = Gtk.Label(label=now or self._("wc_none"), xalign=0)
        line.set_line_wrap(True)
        line.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        line.set_selectable(True)
        line.get_style_context().add_class("wc-line")
        box.pack_start(line, False, False, 0)
        return box

    @staticmethod
    def _chip(text: str, css: str) -> Gtk.Widget:
        label = Gtk.Label(label=text)
        label.get_style_context().add_class("wc-chip")
        label.get_style_context().add_class(css)
        label.set_halign(Gtk.Align.START)
        return label

    # the way back --------------------------------------------------------------------------

    def _target(self) -> Optional[Dict]:
        """The Proton to go back to: the last good launch's, when the game
        runs on something else now (another build, or a "Latest" that
        moved on to a newer release)."""
        if self.good is None or self.session is None:
            return None
        old = self.good["snap"].get("proton") or {}
        new = self.session["snap"].get("proton") or {}
        if not old or old.get("native") or history.describe(self.good["snap"], "proton") \
                == history.describe(self.session["snap"], "proton"):
            return None
        return old

    def _rescue_card(self) -> Optional[Gtk.Widget]:
        target = self._target()
        if target is None:
            return None
        _ = self._
        card = Gtk.Box(spacing=16)
        card.get_style_context().add_class("wc-rescue")
        card.pack_start(_icon("rotate-ccw-symbolic", 26, self.accent), False, False, 0)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        name = self._back_name(target)
        title = Gtk.Label(label=_("wc_rescue_title").format(proton=name), xalign=0)
        title.set_line_wrap(True)
        title.get_style_context().add_class("wc-rescue-title")
        column.pack_start(title, False, False, 0)
        self.rescue_line = Gtk.Label(label=self._rescue_text(target), xalign=0)
        self.rescue_line.set_line_wrap(True)
        self.rescue_line.get_style_context().add_class("dim-label")
        column.pack_start(self.rescue_line, False, False, 0)
        self.rescue_bar = Gtk.ProgressBar()
        self.rescue_bar.get_style_context().add_class("wc-bar")
        self.rescue_bar.set_no_show_all(True)
        column.pack_start(self.rescue_bar, False, False, 4)
        card.pack_start(column, True, True, 0)
        self.rescue_btn = self.app._dialog_button(_("wc_back_to").format(proton=name), "accent")
        self.rescue_btn.set_valign(Gtk.Align.CENTER)
        self.rescue_btn.connect("clicked", lambda *_a: self._go_back(target))
        card.pack_end(self.rescue_btn, False, False, 0)
        return card

    @staticmethod
    def _back_name(target: Dict) -> str:
        # a Latest that moved on goes back to the release it held then
        name, tag = target.get("name") or "", target.get("tag") or ""
        return tag if tag and "Latest" in name else (name or tag)

    def _rescue_text(self, target: Dict) -> str:
        from plunderhub.ui.proton_page import _on_disk
        name = self._back_name(target)
        choice = target.get("choice") or ""
        there = _on_disk(choice if "/" in choice else name, name) if "Latest" not in (target.get("name") or "") \
            else self._store_has(name)
        return self._("wc_rescue_there" if there else "wc_rescue_download").format(proton=name)

    @staticmethod
    def _store_has(name: str) -> bool:
        from plunderhub.ui.proton_page import _on_disk
        return _on_disk(name, name)

    def _go_back(self, target: Dict):
        if self.busy:
            return
        self.busy = True
        self.rescue_btn.set_sensitive(False)
        name = self._back_name(target)
        if self._store_has(name):
            self._set_by_name(name)
            return
        # the very build it had, still there (a Latest has moved on: not that)
        if "Latest" not in (target.get("name") or "") and self._set_choice(target):
            return
        family = "cachyos" if "cachyos" in name.lower() else "ge" if "ge-proton" in name.lower() else None
        if family is None:
            self._rescue_done(False, self._("wc_rescue_gone").format(proton=name))
            return
        self.rescue_line.set_text(self._("wc_rescue_finding").format(proton=name))

        def find():
            from plunderhub.core import proton_store
            try:
                release = next((r for r in proton_store.releases(family) if r.tag == name
                                or proton_store.build_name(r) == name), None)
            except Exception as e:
                logger.warning(f"What changed: releases of {family}: {e}")
                release = None
            GLib.idle_add(self._download, release, name)
        threading.Thread(target=find, name="plunderhub-wc-find", daemon=True).start()

    def _download(self, release, name: str) -> bool:
        if release is None:
            self._rescue_done(False, self._("wc_rescue_gone").format(proton=name))
            return False
        from plunderhub.core import proton_store
        from plunderhub.ui.proton_jobs import jobs
        queue = jobs(self.app)
        if not queue.install(release, latest=False):
            self._rescue_done(False, self._("wc_rescue_busy"))
            return False
        build = proton_store.build_name(release)
        self.rescue_bar.show()

        def tell():
            # the window may be closed meanwhile: the game is still set once it's there
            job = queue.job
            if job is not None and job.name == build:
                if self.dialog is not None:
                    self.rescue_bar.set_fraction(job.percent() / 100)
                    self.rescue_line.set_text(self._("wc_rescue_downloading").format(
                        proton=build, pct=job.percent()))
                return
            last = queue.last
            stop()
            if self.dialog is not None:
                self.rescue_bar.hide()
            if last is not None and last.name == build and not last.error:
                self._set_by_name(build)
            else:
                from plunderhub.ui.proton_page import problem_text
                why = problem_text(getattr(last, "problem", None), getattr(last, "error", "") or "")
                self._rescue_done(False, self._("wc_rescue_failed").format(why=why))
        stop = queue.listen(tell)
        return False

    def _set_choice(self, target: Dict) -> bool:
        """The very choice the good launch had, when the launcher still has it."""
        from plunderhub.ui.proton_page import _game_proton
        entry = _game_proton(self.game)
        choice = target.get("choice") or ""
        if entry is None or not choice or (choice not in dict(entry.choices)
                                           and not choice.startswith("proton_")):
            return False
        self._save(entry, choice, self._back_name(target))
        return True

    def _set_by_name(self, build: str):
        from plunderhub.ui.proton_page import _game_proton, _proton_name
        entry = _game_proton(self.game)
        if entry is None:
            self._rescue_done(False, self._("wc_rescue_gone").format(proton=build))
            return
        choice = next((i for i, label in entry.choices
                       if _proton_name(i if "/" in i else label) == build or label == build), None)
        if choice is None:
            # a launcher that needs a restart to list a new build (Steam):
            # it's named as the launcher's folder lists it
            choice = build if entry.launcher == "Steam" else None
        if choice is None:
            self._rescue_done(False, self._("wc_rescue_not_listed").format(proton=build,
                                                                           launcher=entry.launcher))
            return
        self._save(entry, choice, build)

    def _save(self, entry, choice: str, name: str):
        try:
            opts = entry.backend.load(self.game)
        except Exception as e:
            self._rescue_done(False, str(e))
            return
        opts.proton = choice or None
        parent = self.dialog or self.app._dialog_transient_parent()
        if self.app._lo_save(self.game, entry.backend, opts, parent):
            logger.info(f"What changed: {self.game.title} back on {name}")
            self._rescue_done(True, self._("wc_rescue_done").format(proton=name))
        else:
            self._rescue_done(False, "")

    def _rescue_done(self, ok: bool, text: str):
        self.busy = False
        if self.dialog is None:
            return
        if text:
            self.rescue_line.set_text(text)
        self.rescue_btn.set_sensitive(not ok)
        if ok:
            self.rescue_btn.hide()
            self.rescue_line.get_style_context().add_class("wc-ok")
            self.launch_btn.grab_focus()

    # Proton's log --------------------------------------------------------------------------

    def _log_box(self) -> Optional[Gtk.Widget]:
        if self.session is None or self.session.get("result") not in ("failed", "crashed"):
            return None
        path = history.proton_log_since(self.session.get("at") or time.time())
        if path is None:
            return None
        lines = history.log_errors(path)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        head = Gtk.Box(spacing=10)
        head.pack_start(self._section(self._("wc_log_title")), False, False, 0)
        open_btn = Gtk.Button(label=self._("wc_log_open"))
        open_btn.get_style_context().add_class("sw-link")
        open_btn.connect("clicked", lambda *_a: open_uri(path.as_uri()))
        head.pack_end(open_btn, False, False, 0)
        box.pack_start(head, False, False, 0)
        code = Gtk.Box()
        code.get_style_context().add_class("notes-code")
        text = Gtk.Label(label="\n".join(lines) or self._("wc_log_clean"), xalign=0)
        text.get_style_context().add_class("notes-code-text")
        text.set_line_wrap(True)
        text.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        text.set_selectable(True)
        code.pack_start(text, True, True, 0)
        box.pack_start(code, False, False, 0)
        return box

    def _proton_log_on(self) -> Optional[bool]:
        """Whether the next launch writes Proton's log (None: can't say)."""
        from plunderhub.core.launch_options import backend_for
        backend = backend_for(self.game)
        if backend is None or "proton_log" in backend.unsupported(self.game):
            return None
        try:
            opts = backend.load(self.game)
        except Exception:
            return None
        return None if opts.native else bool(opts.proton_log)

    def _set_proton_log(self, on: bool) -> bool:
        from plunderhub.core.launch_options import backend_for
        backend = backend_for(self.game)
        try:
            opts = backend.load(self.game)
        except Exception as e:
            logger.warning(f"What changed: Proton log not set: {e}")
            return False
        opts.proton_log = on
        return self.app._lo_save(self.game, backend, opts, self.dialog)

    def _undo_options(self) -> bool:
        from plunderhub.core.launch_options import backend_for, parse_command_line
        backend = backend_for(self.game)
        old = (self.good or {}).get("snap", {}).get("options")
        if backend is None or old is None:
            return False
        try:
            current = backend.load(self.game)
            opts = parse_command_line(old)
            opts.proton, opts.native, opts.target_exe = current.proton, current.native, current.target_exe
        except Exception as e:
            logger.warning(f"What changed: launch options not put back: {e}")
            return False
        return self.app._lo_save(self.game, backend, opts, self.dialog)

    # the bottom ----------------------------------------------------------------------------

    def _footer(self) -> Gtk.Widget:
        _ = self._
        bar = Gtk.Box(spacing=10)
        bar.get_style_context().add_class("wc-footer")
        quiet = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        quiet.set_valign(Gtk.Align.CENTER)
        bar.pack_start(quiet, False, False, 0)
        if self.by_itself:
            mute = Gtk.Button(label=_("wc_mute"))
            mute.get_style_context().add_class("sw-link")
            mute.set_halign(Gtk.Align.START)
            mute.connect("clicked", lambda *_a: self._mute())
            quiet.pack_start(mute, False, False, 0)
        log_on = self._proton_log_on()
        if log_on is not None:
            log = Gtk.CheckButton(label=_("wc_log_next"))
            log.set_active(log_on)
            log.set_tooltip_text(_("wc_log_next_tip"))
            log.connect("toggled", lambda b: self._set_proton_log(b.get_active()))
            log.set_halign(Gtk.Align.START)
            log.set_margin_start(10)
            quiet.pack_start(log, False, False, 0)
        launch = self.app._dialog_button(_("wc_launch_again"), "accent", "activate", "Enter")
        launch.connect("clicked", lambda *_a: self._launch())
        self.launch_btn = launch
        bar.pack_end(launch, False, False, 0)
        close = self.app._dialog_button(_("close_btn"), "ghost", "back", "Esc")
        close.connect("clicked", lambda *_a: self.dialog.response(Gtk.ResponseType.CLOSE))
        bar.pack_end(close, False, False, 0)
        old_options = (self.good or {}).get("snap", {}).get("options")
        now_options = (self.session or {}).get("snap", {}).get("options")
        if old_options is not None and now_options is not None and old_options != now_options:
            self.undo_wanted = True
        return bar

    def _mute(self):
        from plunderhub.core.persistence import save_settings
        muted = list(self.app.settings.get(MUTED) or [])
        if self.game.uid not in muted:
            muted.append(self.game.uid)
            self.app.settings[MUTED] = muted
            save_settings(self.app.settings)
        self._close()

    def _launch(self):
        game = self.game
        self._close()
        GLib.idle_add(lambda: self.app.request_launch(game.launch_cmd, game.title, game.env, game.uid,
                                                      game.install_dir) and False)

    def _on_key(self, _w, event) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            self._close()
            return True
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and not isinstance(
                self.dialog.get_focus(), (Gtk.Button, Gtk.Expander)):
            self._launch()
            return True
        return False

    def _close(self):
        if self.dialog is not None:
            dialog, self.dialog = self.dialog, None
            dialog.destroy()
