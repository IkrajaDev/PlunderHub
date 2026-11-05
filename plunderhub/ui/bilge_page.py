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

"""Shipwright's Bilge cleaning page: what launchers left behind
(core/bilge.py), each folder with the game it belonged to, its full path
(opens on a click), its size, when it last changed and - for a prefix -
whether saves may be in it. Nothing is picked but what's only a cache;
deleting asks first with the whole list, and the person picks the Trash
or for good. What was cleaned is kept in "Recently cleaned"."""

import os
import shutil
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from plunderhub.core import bilge
from plunderhub.core.persistence import save_settings
from plunderhub.core.utils import format_bytes
from plunderhub.gi_setup import GLib, Gtk, Pango
from plunderhub.logging_setup import logger

LOG_KEEP = 30


# --- settings that come back after the game's first start ---------------------------------

WAITING_KEY = "bilge_waiting"       # settings: backup folders whose game settings still wait


def remember_waiting(app, backup: bilge.Backup) -> None:
    waiting = [p for p in app.settings.get(WAITING_KEY) or [] if p != str(backup.path)]
    app.settings[WAITING_KEY] = waiting + [str(backup.path)]
    save_settings(app.settings)


def _undo_quietly(aside) -> None:
    undo = bilge._read_backup(aside) if aside is not None else None
    if undo is not None:
        try:
            bilge.undo_restore(undo)
        except Exception as e:
            logger.warning(f"Bilge: undoing {aside}: {e}")


def check_waiting(app) -> bool:
    """Finishes the restores that waited for a game's first start (called
    when PlunderHub starts, when a game quits, when the page opens). A
    worker thread; a toast with Undo for each one done. False (a GLib
    source that runs once)."""
    waiting = list(app.settings.get(WAITING_KEY) or [])
    if not waiting or getattr(app, "_bilge_checking", False):
        return False
    app._bilge_checking = True

    def work():
        done, keep = [], []
        for folder in waiting:
            backup = next((b for b in bilge.backups() if str(b.path) == folder), None)
            if backup is None:
                continue                    # the backup was deleted: nothing to wait for
            try:
                result = bilge.finish_waiting(backup)
            except Exception as e:
                logger.warning(f"Bilge: finishing {folder}: {e}")
                result = None
            if result is None:
                keep.append(folder)
            elif result[1]:
                done.append((backup.title, *result))
        GLib.idle_add(finish, done, keep)

    def finish(done, keep):
        app._bilge_checking = False
        app.settings[WAITING_KEY] = keep
        save_settings(app.settings)
        page = getattr(app, "_bilge_page", None)
        for title, aside, targets in done:
            logger.info(f"Bilge: {title}: the game's settings merged in after its first start")
            text = app._("bg_settings_back").format(title=title)
            if page is not None and not page._gone and page.get_mapped():
                # the page's window would hide the main window's toast
                page.flash = text
                page.backups = bilge.backups()
                page._render()
            else:
                app._show_undo_toast(text, lambda a=aside: _undo_quietly(a), timeout_ms=10000)
        return False
    threading.Thread(target=work, daemon=True, name="plunderhub-bilge-waiting").start()
    return False


# --- what the page reads and does (the demo swaps these) ----------------------------------

def real_scan(app) -> bilge.Found:
    """Reads every launcher's games (also the ones switched off in
    Settings - their prefixes are just as much in use), then looks."""
    from plunderhub.core.scan_paths import (LAUNCHER_FLATPAK_IDS, faugus_games_path, faugus_umu_path,
                                            invocation_for_data, lutris_db_path, resolve_launcher_invocation)
    from plunderhub.core.scanner import Scanner
    from plunderhub.core.steam_heroic import heroic_roots
    from plunderhub.core.persistence import load_json
    scanner = Scanner(set())
    games, healthy = [], set()
    for launcher, method in (("Steam", "scan_steam"), ("Heroic", "scan_heroic"),
                             ("Lutris", "scan_lutris"), ("Faugus", "scan_faugus"), ("Bottles", "scan_bottles")):
        try:
            found = getattr(scanner, method)()
        except Exception as e:
            logger.warning(f"Bilge: reading {launcher}'s games: {e}")
            continue
        games += found
        if launcher in scanner.scan_errors:
            continue
        # a launcher whose games may not all have been read isn't looked at
        if launcher == "Heroic":
            ok = all(invocation_for_data(p, "heroic", LAUNCHER_FLATPAK_IDS["Heroic"]) is not None
                     for p in heroic_roots())
        elif launcher == "Lutris":
            ok = lutris_db_path() is None or resolve_launcher_invocation(
                "lutris", LAUNCHER_FLATPAK_IDS["Lutris"]) is not None
        elif launcher == "Faugus":
            ok = faugus_games_path() is None or faugus_umu_path() is not None
        else:
            ok = True
        if ok:
            healthy.add(launcher)
    kept: List[Path] = []
    for game in games:
        if game.install_dir:
            kept.append(Path(game.install_dir))
        try:
            prefix = app._resolve_wine_prefix(game)
        except Exception:
            prefix = None
        if prefix is not None:
            kept += [prefix, prefix.parent] if prefix.name == "pfx" else [prefix]
    # Faugus' own list, as written: every game there has its prefix
    games_json = faugus_games_path()
    if games_json is not None:
        for entry in load_json(games_json) or []:
            if isinstance(entry, dict) and entry.get("prefix"):
                kept.append(Path(os.path.expanduser(str(entry["prefix"]))))
    used_protons: Set[str] = set()
    try:
        from plunderhub.ui.proton_page import _game_proton
        for game in games:
            entry = _game_proton(game)
            if entry is not None and entry.proton_name():
                used_protons.add(entry.proton_name())
    except Exception as e:
        logger.warning(f"Bilge: the games' Protons: {e}")
        used_protons = None
    found = bilge.scan(kept, healthy, used_protons or ())
    if used_protons is None:
        found.items = [i for i in found.items if i.kind != "proton"]
    known = _known_steam_titles()
    for item in found.items:
        if item.kind in ("steam_prefix", "shader_cache") and item.appid:
            item.title = bilge.steam_name(item.appid, known)
        bilge.measure(item)
        bilge.look_for_saves(item)
    return found


def _known_steam_titles() -> Dict[str, str]:
    from plunderhub.constants import CONFIG_DIR
    from plunderhub.core.persistence import load_json
    data = load_json(CONFIG_DIR / "known_titles.json") or {}
    return {uid.split(":", 1)[1]: str(v.get("title", "")) for uid, v in data.items()
            if uid.startswith("steam:") and isinstance(v, dict) and v.get("title")}


def real_delete(app, item: bilge.Item, to_trash: bool) -> Optional[str]:
    if item.kind == "proton":
        # PlunderHub's own build: out of every launcher too, and a game
        # still set to it moves to the Latest
        from plunderhub.core import proton_store
        from plunderhub.ui.proton_upkeep import move_games_off
        try:
            proton_store.remove(item.title)
            move_games_off(app, item.title)
            return None
        except Exception as e:
            return str(e)
    return bilge.delete(item.path, to_trash)


SCAN = real_scan
DELETE = real_delete
BACK_UP = bilge.back_up
RESTORE = bilge.restore


def _date(t: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(t)) if t else "-"


TABS = ("steam_prefix", "shader_cache", "prefix", "proton", "backups", "recent")
TAB_ICONS = {"steam_prefix": "wine-symbolic", "shader_cache": "sparkles-symbolic", "prefix": "folder-symbolic",
             "proton": "rocket-symbolic", "backups": "document-save-symbolic", "recent": "clock-symbolic"}


def bright_scroller(child: Gtk.Widget, max_height: int) -> Gtk.ScrolledWindow:
    """A scrolled list with the app's bright, always-there scrollbar."""
    scroller = Gtk.ScrolledWindow()
    scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    scroller.set_overlay_scrolling(False)
    scroller.get_style_context().add_class("settings-scroll")
    scroller.set_propagate_natural_height(True)
    scroller.set_max_content_height(max_height)
    scroller.add(child)
    # from the first line: a dialog's focus would scroll it to its end
    scroller.connect("map", lambda sc: GLib.idle_add(lambda: (sc.get_vadjustment().set_value(0), False)[1]))
    return scroller


class BilgePage(Gtk.Box):
    """One summary card (what can be freed, what's picked, Delete), a row
    of tiles - one per kind of leftover, and Recently cleaned - and the
    list of the tile picked: one kind at a time."""

    def __init__(self, sw):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.sw, self.app, self._ = sw, sw.app, sw.app._
        self.found: Optional[bilge.Found] = None
        self.picked: Set[str] = set()
        self.busy = False
        self.tab: Optional[str] = None
        self.backups: List[bilge.Backup] = []
        self.flash: Optional[str] = None       # said once above the list (a backup made...)
        self.sel_label: Optional[Gtk.Label] = None
        self.sel_button: Optional[Gtk.Button] = None
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.pack_start(self.body, False, False, 0)
        self._gone = False
        self.connect("destroy", lambda *_a: setattr(self, "_gone", True))
        self.app._bilge_page = self            # check_waiting says it here while it's open
        self.scan()

    @property
    def TITLE(self) -> str:
        return self._("bg_title")

    @property
    def SUB(self) -> str:
        return self._("bg_sub")

    # --- looking ---------------------------------------------------------------------------

    def scan(self):
        if self.busy:
            return
        check_waiting(self.app)
        self.busy = True
        self.found = None
        self._render()

        def work():
            try:
                found = SCAN(self.app)
            except Exception as e:
                logger.warning(f"Bilge: looking failed: {e}")
                found = bilge.Found()
            GLib.idle_add(done, found, bilge.backups())

        def done(found, backups):
            self.busy = False
            if self._gone:
                return False
            self.found, self.backups = found, backups
            # nothing picked to start with: the person picks what goes
            self.picked = set()
            self._render()
            return False
        threading.Thread(target=work, daemon=True, name="plunderhub-bilge").start()

    # --- drawing ---------------------------------------------------------------------------

    def _items(self, tab: str) -> List[bilge.Item]:
        items = [i for i in (self.found.items if self.found else []) if i.kind == tab]
        return sorted(items, key=lambda i: -i.size)

    def _tabs(self) -> List[str]:
        """The tiles that have something in them."""
        log = self.app.settings.get("bilge_log") or []
        return [t for t in TABS if (t == "recent" and log) or (t == "backups" and self.backups)
                or (t not in ("recent", "backups") and self._items(t))]

    def _render(self):
        _ = self._
        for child in self.body.get_children():
            child.destroy()
        self.sel_label = self.sel_button = None
        self.body.pack_start(self._status(), False, False, 0)
        undo = self._undo_bar()
        if undo is not None:
            self.body.pack_start(undo, False, False, 0)
        if self.found is not None and (self.backups or any(i.saves for i in self.found.items)):
            self.body.pack_start(self._beta_note(), False, False, 0)
        if self.found is not None:
            for launcher, why in self.found.skipped:
                if launcher in ("Lutris", "Faugus") and why == "unreadable" and not self._launcher_there(launcher):
                    continue        # not installed at all: nothing to say
                text = (_("bg_skipped_unmounted").format(path=why.split(":", 1)[1]) if why.startswith("unmounted:")
                        else _("bg_skipped_unreadable").format(launcher=launcher))
                self.body.pack_start(self._notice(text), False, False, 0)
            tabs = self._tabs()
            if tabs:
                if self.tab not in tabs:
                    self.tab = tabs[0]
                self.body.pack_start(self._tiles(tabs), False, False, 0)
                self.list_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
                self.body.pack_start(self.list_box, False, False, 0)
                self._show_tab()
        self.body.show_all()
        self._update_selection()

    def _launcher_there(self, launcher: str) -> bool:
        return any(p.exists() for p in bilge.PREFIX_FOLDERS[launcher]()[:-1])

    def _status(self) -> Gtk.Widget:
        _ = self._
        card = Gtk.Box(spacing=16)
        card.get_style_context().add_class("settings-card")
        card.get_style_context().add_class("bg-status")
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        texts.set_valign(Gtk.Align.CENTER)
        if self.found is None:
            head, sub = _("bg_scanning"), _("bg_scanning_sub")
        elif self.found.items:
            total = sum(max(i.size, 0) for i in self.found.items)
            head = _("bg_free").format(size=format_bytes(total))
            sub = None
        else:
            head, sub = _("bg_clean"), _("bg_clean_sub")
        title = Gtk.Label(label=head, xalign=0)
        title.get_style_context().add_class("bg-status-title")
        texts.pack_start(title, False, False, 0)
        line = Gtk.Label(label=sub or "", xalign=0)
        line.set_line_wrap(True)
        line.get_style_context().add_class("settings-row-sub")
        texts.pack_start(line, False, False, 0)
        if sub is None:
            self.sel_label = line       # what's picked, kept up to date
        card.pack_start(texts, True, True, 0)
        if self.found is None:
            spinner = Gtk.Spinner()
            spinner.start()
            card.pack_end(spinner, False, False, 0)
            return card
        if self.found.items:
            self.sel_button = self.app._dialog_button(_("bg_delete_btn"), "danger")
            self.sel_button.set_valign(Gtk.Align.CENTER)
            self.sel_button.connect("clicked", lambda *_a: self._delete_picked())
            card.pack_end(self.sel_button, False, False, 0)
        again = self.app._dialog_button(_("bg_rescan"), "ghost")
        again.set_valign(Gtk.Align.CENTER)
        again.connect("clicked", lambda *_a: self.scan())
        card.pack_end(again, False, False, 0)
        return card

    def _notice(self, text: str) -> Gtk.Widget:
        box = Gtk.Box(spacing=12)
        box.get_style_context().add_class("gp-notice")
        lab = Gtk.Label(label=text, xalign=0)
        lab.set_line_wrap(True)
        box.pack_start(lab, True, True, 0)
        return box

    def _tiles(self, tabs: List[str]) -> Gtk.Widget:
        self.tile_subs: Dict[str, Gtk.Label] = {}
        row = Gtk.Box(spacing=12, homogeneous=True)
        first = None
        for tab in tabs:
            tile = Gtk.RadioButton.new_from_widget(first)
            first = first or tile
            tile.set_mode(False)
            tile.set_focus_on_click(False)
            tile.get_style_context().add_class("bg-tile")
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            icon = Gtk.Image.new_from_pixbuf(self.app._icon_pixbuf(TAB_ICONS[tab], 26))
            icon.set_margin_bottom(4)
            inner.pack_start(icon, False, False, 0)
            name = Gtk.Label(label=self._tab_title(tab), xalign=0.5, justify=Gtk.Justification.CENTER)
            name.set_line_wrap(True)
            name.get_style_context().add_class("bg-tile-title")
            inner.pack_start(name, False, False, 0)
            sub = Gtk.Label(xalign=0.5)
            sub.set_markup(self._tab_sub_markup(tab))
            sub.get_style_context().add_class("bg-tile-sub")
            self.tile_subs[tab] = sub
            inner.pack_start(sub, False, False, 0)
            tile.add(inner)
            tile.set_active(tab == self.tab)
            tile.connect("toggled", lambda b, t=tab: b.get_active() and self._pick_tab(t))
            row.pack_start(tile, True, True, 0)
        # always four tiles wide: one or two of them don't stretch over the page
        for _n in range(max(0, 4 - len(tabs))):
            row.pack_start(Gtk.Box(), True, True, 0)
        return row

    def _tab_title(self, tab: str) -> str:
        return self._("bg_recent" if tab == "recent" else "bg_group_prefixes" if tab == "prefix"
                      else "bg_tab_backups" if tab == "backups" else f"bg_group_{tab}")

    def _tab_sub(self, tab: str) -> str:
        if tab == "recent":
            return str(len(self.app.settings.get("bilge_log") or []))
        if tab == "backups":
            return f"{len(self.backups)} · {format_bytes(sum(b.size for b in self.backups))}"
        items = self._items(tab)
        return f"{len(items)} · {format_bytes(sum(max(i.size, 0) for i in items))}"

    def _tab_sub_markup(self, tab: str) -> str:
        """count · size, and how many of them are picked - in the accent."""
        text = GLib.markup_escape_text(self._tab_sub(tab))
        if tab in ("recent", "backups"):
            return text
        n = sum(1 for i in self._items(tab) if str(i.path) in self.picked)
        return text + (f'  <span foreground="{self._accent_hex()}" weight="bold">✓ {n}</span>' if n else "")

    def _accent_hex(self) -> str:
        from plunderhub.ui.home_mixin import _accent_rgb
        r, g, b = _accent_rgb(self)
        return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))

    def _pick_tab(self, tab: str):
        if tab != self.tab:
            self.tab = tab
            self._show_tab()

    def _show_tab(self):
        _ = self._
        for child in self.list_box.get_children():
            child.destroy()
        self.row_checks: Dict[bilge.Item, Gtk.CheckButton] = {}  # noqa (Item is eq=False below)
        tab = self.tab
        head = Gtk.Box(spacing=12)
        about = Gtk.Label(label=_("bg_about_prefixes" if tab == "prefix" else f"bg_about_{tab}")
                          if tab != "recent" else "", xalign=0)
        if self.flash:
            flash = Gtk.Label(label="✓ " + self.flash, xalign=0)
            flash.set_line_wrap(True)
            flash.get_style_context().add_class("bg-flash")
            self.list_box.pack_start(flash, False, False, 0)
            self.flash = None
        about.set_line_wrap(True)
        about.get_style_context().add_class("settings-row-sub")
        head.pack_start(about, True, True, 0)
        items = self._items(tab) if tab not in ("recent", "backups") else []
        self.head_check = None
        # the right-hand columns line up from row to row
        self.size_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        self.button_group = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)
        if tab == "recent":
            # the list only: Clear, like the Dashboard's playtime history
            clear = self._icon_button("user-trash-symbolic", _("bg_recent_clear"), lambda b: self._clear_recent())
            head.pack_end(clear, False, False, 0)
        self.list_box.pack_start(head, False, False, 0)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.get_style_context().add_class("settings-card")
        rows = (self._recent_rows() if tab == "recent" else [self._backup_row(b) for b in self.backups]
                if tab == "backups" else [self._row(i) for i in items])
        if items:
            # "all of these" in the checkboxes' own column: plainly this list only
            head_row = Gtk.Box(spacing=12)
            head_row.get_style_context().add_class("settings-row")
            head_row.get_style_context().add_class("bg-list-head")
            self.head_check = Gtk.CheckButton(label=_("bg_select_these").format(n=len(items)))
            self.head_check.set_focus_on_click(False)
            self.head_check_handler = self.head_check.connect(
                "toggled", lambda b: self._pick_all(items, b.get_active() or b.get_inconsistent()))
            head_row.pack_start(self.head_check, False, False, 0)
            rows.insert(0, head_row)
            self._mark_head(items)
        for n, row in enumerate(rows):
            if n:
                sep = Gtk.Box()
                sep.get_style_context().add_class("settings-row-sep")
                sep.set_margin_start(16)
                sep.set_margin_end(16)
                card.pack_start(sep, False, False, 0)
            card.pack_start(row, False, False, 0)
        self.list_box.pack_start(card, False, False, 0)
        self.list_box.show_all()

    def _pick_all(self, items: List[bilge.Item], on: bool):
        # a mixed list picks all first; all picked, it clears
        on = not all(str(i.path) in self.picked for i in items)
        for item in items:
            (self.picked.add if on else self.picked.discard)(str(item.path))
        for item, check in self.row_checks.items():
            if check.get_active() != (str(item.path) in self.picked):
                check.set_active(str(item.path) in self.picked)
        self._update_selection()

    def _mark_head(self, items: List[bilge.Item]):
        """The list's own checkbox: on, off, or in between."""
        check = self.head_check
        if check is None:
            return
        n = sum(1 for i in items if str(i.path) in self.picked)
        check.handler_block(self.head_check_handler)
        check.set_inconsistent(0 < n < len(items))
        check.set_active(n == len(items))
        check.handler_unblock(self.head_check_handler)

    def _name(self, item: bilge.Item) -> str:
        if item.title:
            return item.title
        return self._("bg_unknown_game").format(appid=item.appid)

    def _row(self, item: bilge.Item) -> Gtk.Widget:
        """One line a glance reads: the game, what's in it (a pill), its
        size; the folder and the backup behind two small buttons, the
        whole path on the name's tooltip."""
        _ = self._
        row = Gtk.Box(spacing=12)
        row.get_style_context().add_class("settings-row")
        check = Gtk.CheckButton()
        check.set_valign(Gtk.Align.CENTER)
        check.set_active(str(item.path) in self.picked)
        check.connect("toggled", lambda b: self._pick(item, b.get_active()))
        self.row_checks[item] = check
        row.pack_start(check, False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        texts.set_valign(Gtk.Align.CENTER)
        top = Gtk.Box(spacing=8)
        name = Gtk.Label(label=self._name(item), xalign=0)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.get_style_context().add_class("settings-row-title")
        name.set_tooltip_text(str(item.path))
        top.pack_start(name, False, False, 0)
        if item.kind == "prefix":
            top.pack_start(self._pill(item.launcher, "bg-pill-quiet"), False, False, 0)
        if item.saves:
            top.pack_start(self._pill(_("bg_pill_player"), "bg-pill-warn", "triangle-alert-symbolic"),
                           False, False, 0)
            if str(item.path) in self._backed_up():
                top.pack_start(self._pill(_("bg_pill_backed"), "bg-pill-ok", "object-select-symbolic"),
                               False, False, 0)
        texts.pack_start(top, False, False, 0)
        meta = Gtk.Label(label=_("bg_changed").format(date=_date(item.changed)), xalign=0)
        meta.get_style_context().add_class("settings-row-sub")
        texts.pack_start(meta, False, False, 0)
        row.pack_start(texts, True, True, 0)
        # left to right: Back up, the size, the folder
        row.pack_end(self._icon_button("folder-symbolic", _("bg_open_folder"),
                                       lambda b: self.app._show_folder(item.path, self.sw.dialog)), False, False, 0)
        size = Gtk.Label(label=format_bytes(max(item.size, 0)), xalign=1)
        size.get_style_context().add_class("bg-size")
        size.set_valign(Gtk.Align.CENTER)
        self.size_group.add_widget(size)
        row.pack_end(size, False, False, 0)
        if item.saves:
            backup = self._icon_button("document-save-symbolic", _("bg_backup"),
                                       lambda b: self._backup(item, b), label=_("bg_backup"))
        else:
            backup = Gtk.Box()          # keeps the column: the sizes stay in one line
        self.button_group.add_widget(backup)
        row.pack_end(backup, False, False, 0)
        return row

    def _files(self, n: int) -> str:
        return self._("bg_files_1") if n == 1 else self._("bg_files_n").format(n=n)

    def _backed_up(self) -> Set[str]:
        return {str(b.source) for b in self.backups}

    # --- small pieces ----------------------------------------------------------------------

    def _image(self, icon: str, px: int = 16) -> Gtk.Image:
        return Gtk.Image.new_from_pixbuf(self.app._icon_pixbuf(icon, px))

    def _pill(self, text: str, css: str, icon: Optional[str] = None) -> Gtk.Widget:
        pill = Gtk.Box(spacing=5)
        pill.get_style_context().add_class("bg-pill")
        pill.get_style_context().add_class(css)
        pill.set_valign(Gtk.Align.CENTER)
        if icon:
            pill.pack_start(self._image(icon, 12), False, False, 0)
        pill.pack_start(Gtk.Label(label=text), False, False, 0)
        return pill

    def _icon_button(self, icon: str, tip: str, on_click, label: Optional[str] = None) -> Gtk.Button:
        button = Gtk.Button()
        inner = Gtk.Box(spacing=6)
        inner.pack_start(self._image(icon, 16), False, False, 0)
        if label:
            inner.pack_start(Gtk.Label(label=label), False, False, 0)
        button.add(inner)
        button.set_tooltip_text(tip)
        button.set_valign(Gtk.Align.CENTER)
        button.set_focus_on_click(False)
        button.get_style_context().add_class("bg-icon-btn")
        button.connect("clicked", on_click)
        return button

    def _panel(self, icon: str, title: str, right: str, css: str, lines: List[Tuple[str, str]]) -> Gtk.Widget:
        """A small card: an icon and a title (with a total on the right),
        then short lines - a name and a number."""
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.get_style_context().add_class("bg-panel")
        card.get_style_context().add_class(css)
        head = Gtk.Box(spacing=8)
        head.pack_start(self._image(icon, 18), False, False, 0)
        t = Gtk.Label(label=title, xalign=0)
        t.get_style_context().add_class("bg-panel-title")
        head.pack_start(t, True, True, 0)
        if right:
            r = Gtk.Label(label=right)
            r.get_style_context().add_class("bg-panel-total")
            head.pack_end(r, False, False, 0)
        card.pack_start(head, False, False, 0)
        for text, num in lines:
            line = Gtk.Box(spacing=8)
            line.set_margin_start(26)
            a = Gtk.Label(label=text, xalign=0)
            a.set_ellipsize(Pango.EllipsizeMode.END)
            line.pack_start(a, True, True, 0)
            if num:
                b = Gtk.Label(label=num)
                b.get_style_context().add_class("settings-row-sub")
                line.pack_end(b, False, False, 0)
            card.pack_start(line, False, False, 0)
        return card

    def _stat(self, number: str, label: str, css: str = "") -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.get_style_context().add_class("bg-stat")
        if css:
            box.get_style_context().add_class(css)
        num = Gtk.Label(label=number)
        num.get_style_context().add_class("bg-stat-num")
        box.pack_start(num, False, False, 0)
        lab = Gtk.Label(label=label)
        lab.get_style_context().add_class("settings-row-sub")
        box.pack_start(lab, False, False, 0)
        return box

    def _line(self, text: str, css: str = "settings-row-sub", icon: Optional[str] = None) -> Gtk.Widget:
        box = Gtk.Box(spacing=8)
        if icon:
            img = self._image(icon, 16)
            img.set_valign(Gtk.Align.START)
            box.pack_start(img, False, False, 0)
        lab = Gtk.Label(label=text, xalign=0)
        lab.set_line_wrap(True)
        lab.set_max_width_chars(44)
        lab.get_style_context().add_class(css)
        box.pack_start(lab, True, True, 0)
        return box

    def _picked_items(self) -> List[bilge.Item]:
        return [i for i in (self.found.items if self.found else []) if str(i.path) in self.picked]

    def _pick(self, item: bilge.Item, on: bool):
        (self.picked.add if on else self.picked.discard)(str(item.path))
        self._update_selection()

    def _update_selection(self):
        if getattr(self, "head_check", None) is not None and self.tab:
            self._mark_head(self._items(self.tab))
        for tab, label in getattr(self, "tile_subs", {}).items():
            label.set_markup(self._tab_sub_markup(tab))
        if self.sel_label is None:
            return
        items = self._picked_items()
        self.sel_label.set_text(
            self._("bg_selected").format(n=len(items), size=format_bytes(sum(max(i.size, 0) for i in items)))
            if items else self._("bg_none_selected"))
        if self.sel_button is not None:
            # the button only once something is picked
            self.sel_button.set_visible(bool(items) and not self.busy)

    def _beta_note(self) -> Gtk.Widget:
        """Backups are new: said plainly, once, where they can be made."""
        box = Gtk.Box(spacing=10)
        box.get_style_context().add_class("bg-beta")
        box.pack_start(self._pill(self._("bg_beta_pill"), "bg-pill-warn"), False, False, 0)
        text = Gtk.Label(label=self._("bg_beta_text"), xalign=0)
        text.set_line_wrap(True)
        box.pack_start(text, True, True, 0)
        return box

    def _clear_recent(self):
        app = self.app
        log = list(app.settings.get("bilge_log") or [])
        if not log or not app._confirm_yes_no(
                self._("bg_recent_clear_text"), transient_for=self.sw.dialog,
                heading=self._("bg_recent_clear_head"), yes_label=self._("dialog_sink_btn"),
                no_label=self._("dialog_keep_btn"), destructive=True):
            return
        app.settings["bilge_log"] = []
        save_settings(app.settings)
        self._undo_log = (log, time.monotonic() + 6)
        GLib.timeout_add(6100, lambda: (self._gone or self._render(), False)[1])
        self._render()

    def _undo_bar(self) -> Optional[Gtk.Widget]:
        undo = getattr(self, "_undo_log", None)
        if undo is None or time.monotonic() > undo[1]:
            return None
        bar = Gtk.Box(spacing=12)
        bar.get_style_context().add_class("bg-undo")
        text = Gtk.Label(label=self._("bg_recent_cleared"), xalign=0)
        bar.pack_start(text, True, True, 0)
        button = self.app._dialog_button(self._("undo_btn"), "ghost")
        button.connect("clicked", lambda *_a: self._undo_clear())
        bar.pack_end(button, False, False, 0)
        return bar

    def _undo_clear(self):
        undo = getattr(self, "_undo_log", None)
        if undo is None:
            return
        # anything cleaned since Clear stays, on top
        self.app.settings["bilge_log"] = (list(self.app.settings.get("bilge_log") or []) + undo[0])[:LOG_KEEP]
        save_settings(self.app.settings)
        self._undo_log = None
        self._render()

    def _recent_rows(self) -> List[Gtk.Widget]:
        rows = []
        for entry in (self.app.settings.get("bilge_log") or [])[:LOG_KEEP]:
            row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            row.get_style_context().add_class("settings-row")
            name = Gtk.Label(label=entry.get("title", ""), xalign=0)
            name.get_style_context().add_class("settings-row-title")
            row.pack_start(name, False, False, 0)
            how = self._("bg_how_trash" if entry.get("trash") else "bg_how_deleted")
            line = Gtk.Label(label=f"{_date(entry.get('when', 0))} · {format_bytes(entry.get('size', 0))} · {how}"
                                   f" · {entry.get('path', '')}", xalign=0)
            line.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            line.get_style_context().add_class("settings-row-sub")
            row.pack_start(line, False, False, 0)
            rows.append(row)
        return rows

    # --- saves -----------------------------------------------------------------------------

    def _categories(self, item: bilge.Item) -> List[Tuple[str, int, int]]:
        """The player files as a person thinks of them: (what, files, bytes)."""
        drive = bilge._drive(item.path).relative_to(item.path).as_posix() + "/"
        sums: Dict[str, List[int]] = {}
        for path, rel in item.player:
            low = rel.lower()
            name = rel.rsplit("/", 1)[-1].lower()
            if name in bilge.SYSTEM_FILES:
                key = "bg_cat_registry"
            elif low.startswith((drive + "users/").lower()) or low.startswith((drive + "programdata/").lower()):
                key = "bg_cat_saves"
            else:
                key = "bg_cat_programs"
            entry = sums.setdefault(key, [0, 0])
            entry[0] += 1
            entry[1] += bilge._size_of_file(path)
        order = ("bg_cat_saves", "bg_cat_programs", "bg_cat_registry")
        return [(k, *sums[k]) for k in order if k in sums]

    def _backup(self, item: bilge.Item, button: Gtk.Button):
        """Asks first, in two cards: what's copied, what's left out."""
        _ = self._
        cats = self._categories(item)
        files = sum(n for _k, n, _s in cats)
        total = sum(s for _k, _n, s in cats)
        extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        extra.pack_start(self._panel("object-select-symbolic", _("bg_backup_copied"),
                                     format_bytes(total), "bg-panel-ok",
                                     [(_(k), self._files(n)) for k, n, _s in cats]), False, False, 0)
        left = []
        for name, size in item.left_out:
            key = "bg_left_windows_s" if name.lower() == "windows" else "bg_left_programs_s"
            left.append((_(key), format_bytes(size)))
        left.append((_("bg_left_caches_s"), ""))
        extra.pack_start(self._panel("window-close-symbolic", _("bg_backup_left"), "", "bg-panel-quiet", left),
                         False, False, 0)
        extra.show_all()
        answer = self.app._card_dialog(_("bg_backup_head").format(title=self._name(item)), _("bg_backup_text"),
                                       [(_("dialog_cancel"), Gtk.ResponseType.NO, "ghost"),
                                        (_("bg_backup_ok").format(n=files), Gtk.ResponseType.YES, "accent")],
                                       icon_name="document-save-symbolic", default=Gtk.ResponseType.YES,
                                       transient_for=self.sw.dialog, extra=extra)
        if answer != Gtk.ResponseType.YES:
            return
        button.set_sensitive(False)
        name = self._name(item)

        def work():
            try:
                made, error = BACK_UP(item, name), None
            except Exception as e:
                logger.warning(f"Bilge: backing up {item.path}: {e}")
                made, error = None, self._why(e)
            GLib.idle_add(done, made, error)

        def done(made, error):
            if self._gone:
                return False
            if error:
                button.set_sensitive(True)
                self.app._show_info_dialog(_("bg_backup_failed").format(error=error),
                                           message_type=Gtk.MessageType.WARNING, transient_for=self.sw.dialog)
                return False
            self.backups = bilge.backups()
            self.flash = _("bg_backed_up").format(files=self._files(len(made.files)))
            self._render()
            return False
        threading.Thread(target=work, daemon=True, name="plunderhub-bilge-backup").start()

    def _backup_row(self, backup: bilge.Backup) -> Gtk.Widget:
        _ = self._
        row = Gtk.Box(spacing=12)
        row.get_style_context().add_class("settings-row")
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        texts.set_valign(Gtk.Align.CENTER)
        top = Gtk.Box(spacing=8)
        name = Gtk.Label(label=backup.title, xalign=0)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.get_style_context().add_class("settings-row-title")
        source = bilge.restore_source(backup)
        name.set_tooltip_text(str(source))
        top.pack_start(name, False, False, 0)
        there = bilge.is_prefix(source)
        top.pack_start(self._pill(_("bg_state_there_s") if there else _("bg_state_gone_s"),
                                  "bg-pill-quiet" if there else "bg-pill-warn"), False, False, 0)
        texts.pack_start(top, False, False, 0)
        meta = Gtk.Label(label=f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(backup.created))}  ·  "
                               f"{self._files(len(backup.files))}  ·  {format_bytes(backup.size)}",
                         xalign=0)
        meta.get_style_context().add_class("settings-row-sub")
        texts.pack_start(meta, False, False, 0)
        row.pack_start(texts, True, True, 0)
        row.pack_end(self._icon_button("user-trash-symbolic", _("bg_delete_backup"),
                                       lambda b: self._delete_backup(backup)), False, False, 0)
        restore = self.app._dialog_button(_("bg_restore"), "accent")
        restore.set_valign(Gtk.Align.CENTER)
        restore.connect("clicked", lambda *_a: self._restore(backup))
        row.pack_end(restore, False, False, 0)
        if backup.undos:
            undo = self.app._dialog_button(_("bg_undo_restore"), "ghost")
            undo.set_valign(Gtk.Align.CENTER)
            undo.connect("clicked", lambda *_a: self._undo(backup))
            row.pack_end(undo, False, False, 0)
        row.pack_end(self._icon_button("folder-symbolic", _("bg_open_folder"),
                                       lambda b: self.app._show_folder(backup.path, self.sw.dialog)), False, False, 0)
        return row

    def _why(self, error: Exception) -> str:
        """An error as the person reads it: no room, a drive not there."""
        if isinstance(error, bilge.NoSpace):
            return self._("bg_no_space").format(need=format_bytes(error.need), free=format_bytes(error.free))
        if isinstance(error, bilge.NotThere):
            return self._("bg_not_there").format(path=self.app._home_path(error.where))
        return str(error)

    def _restore(self, backup: bilge.Backup):
        _ = self._
        try:
            plan = bilge.plan_restore(backup)
        except Exception as e:
            self.app._show_info_dialog(_("bg_restore_failed").format(error=self._why(e)),
                                       message_type=Gtk.MessageType.WARNING,
                                       transient_for=self.sw.dialog)
            return
        extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        stats = Gtk.Box(spacing=10, homogeneous=True)
        stats.pack_start(self._stat(str(len(plan.new)), _("bg_stat_new"), "bg-stat-ok"), True, True, 0)
        stats.pack_start(self._stat(str(len(plan.replace)), _("bg_stat_replaced"),
                                    "bg-stat-warn" if plan.replace else ""), True, True, 0)
        stats.pack_start(self._stat(str(len(plan.same)), _("bg_stat_same")), True, True, 0)
        extra.pack_start(stats, False, False, 0)
        if plan.replace:
            extra.pack_start(self._line(_("bg_restore_aside_short"), "bg-saves", "triangle-alert-symbolic"),
                             False, False, 0)
        if plan.registry:
            n = sum(len(k) for k in plan.registry.values())
            extra.pack_start(self._line(_("bg_restore_registry").format(n=n), "bg-flash", "object-select-symbolic"),
                             False, False, 0)
        waiting = bilge.waits(plan)
        if waiting:
            extra.pack_start(self._line(_("bg_restore_later"), "bg-flash", "object-select-symbolic"),
                             False, False, 0)
        if plan.kept and not plan.registry and not waiting:
            extra.pack_start(self._line(_("bg_restore_kept_short"), icon="circle-alert-symbolic"), False, False, 0)
        extra.show_all()
        nothing = not plan.new and not plan.replace and not plan.registry
        buttons = [(_("dialog_cancel"), Gtk.ResponseType.NO, "ghost")]
        if not nothing:
            buttons.append((_("bg_restore_ok"), Gtk.ResponseType.YES, "accent"))
        text = (_("bg_restore_start_first") if nothing and waiting
                else _("bg_restore_nothing") if nothing
                else _("bg_restore_there") if plan.folder_exists else _("bg_restore_gone"))
        answer = self.app._card_dialog(_("bg_restore_head").format(title=backup.title), text, buttons,
                                       icon_name="document-save-symbolic", default=Gtk.ResponseType.NO,
                                       transient_for=self.sw.dialog, extra=extra)
        if answer != Gtk.ResponseType.YES:
            return
        if bilge.prefix_in_use(plan.source):
            self.app._show_info_dialog(_("bg_in_use"), message_type=Gtk.MessageType.WARNING,
                                       transient_for=self.sw.dialog)
            return

        def work():
            try:
                done_n, aside = RESTORE(plan)
                result = (done_n, aside, None)
            except Exception as e:
                logger.warning(f"Bilge: restoring {backup.path}: {e}")
                result = (0, None, self._why(e))
            GLib.idle_add(finish, *result)

        def finish(done_n, aside, error):
            if self._gone:
                return False
            if error:
                self.app._show_info_dialog(_("bg_restore_failed").format(error=error),
                                           message_type=Gtk.MessageType.WARNING, transient_for=self.sw.dialog)
            else:
                if bilge.waits(plan):
                    remember_waiting(self.app, backup)
                text = _("bg_restored").format(files=self._files(done_n))
                if aside is not None:
                    text += "  ·  " + _("bg_restored_aside")
                self.flash = text
            # a folder made again is a leftover again (its game isn't
            # installed): look afresh, as after deleting - the Backups tile stays
            self.scan()
            return False
        threading.Thread(target=work, daemon=True, name="plunderhub-bilge-restore").start()

    def _undo(self, backup: bilge.Backup):
        """Puts back what the last restore of this backup set aside."""
        _ = self._
        undo = backup.undos[0]
        when = time.strftime('%Y-%m-%d %H:%M', time.localtime(undo.created))
        if not self.app._confirm_yes_no(_("bg_undo_q").format(when=when), transient_for=self.sw.dialog,
                                        title=_("bg_undo_q_head").format(title=backup.title),
                                        yes_label=_("bg_undo_restore")):
            return
        if bilge.prefix_in_use(bilge.restore_source(backup)):
            self.app._show_info_dialog(_("bg_in_use"), message_type=Gtk.MessageType.WARNING,
                                       transient_for=self.sw.dialog)
            return
        try:
            n = bilge.undo_restore(undo)
            self.flash = _("bg_undone").format(files=self._files(n))
        except Exception as e:
            logger.warning(f"Bilge: undoing {undo.path}: {e}")
            self.app._show_info_dialog(_("bg_restore_failed").format(error=self._why(e)),
                                       message_type=Gtk.MessageType.WARNING, transient_for=self.sw.dialog)
        self.backups = bilge.backups()
        self._render()

    def _delete_backup(self, backup: bilge.Backup):
        _ = self._
        if not self.app._confirm_yes_no(_("bg_delete_backup_q").format(title=backup.title,
                                                                       size=format_bytes(backup.size)),
                                        transient_for=self.sw.dialog, title=_("bg_delete_backup"),
                                        yes_label=_("bg_delete"), destructive=True):
            return
        try:
            bilge.delete_backup(backup)
        except OSError as e:
            self.app._show_info_dialog(str(e), message_type=Gtk.MessageType.WARNING, transient_for=self.sw.dialog)
        self.backups = bilge.backups()
        self._render()

    # --- deleting --------------------------------------------------------------------------

    def _delete_picked(self):
        _ = self._
        items = self._picked_items()
        if not items or self.busy:
            return
        total = format_bytes(sum(max(i.size, 0) for i in items))
        heading = (_("bg_confirm_head_1") if len(items) == 1 else _("bg_confirm_head").format(n=len(items)))
        extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        extra.pack_start(self._stat(total, _("bg_frees"), "bg-stat-big"), False, False, 0)
        # every folder by name (its whole path on the tooltip), what's in it
        listing = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        backed = self._backed_up()
        unsafe = 0
        for item in items:
            line = Gtk.Box(spacing=8)
            line.get_style_context().add_class("bg-list-line")
            name = Gtk.Label(label=self._name(item), xalign=0)
            name.set_ellipsize(Pango.EllipsizeMode.END)
            name.set_tooltip_text(str(item.path))
            line.pack_start(name, False, False, 0)
            # what the folder is: a game can have a prefix and a shader cache
            line.pack_start(self._pill(_({"steam_prefix": "bg_kind_prefix", "prefix": "bg_kind_prefix",
                                           "shader_cache": "bg_kind_cache"}.get(item.kind, "bg_kind_proton")),
                                       "bg-pill-quiet"), False, False, 0)
            if item.saves and str(item.path) in backed:
                line.pack_start(self._pill(_("bg_pill_backed"), "bg-pill-ok", "object-select-symbolic"),
                                False, False, 0)
            elif item.saves:
                unsafe += 1
                line.pack_start(self._pill(_("bg_pill_player"), "bg-pill-warn", "triangle-alert-symbolic"),
                                False, False, 0)
            size = Gtk.Label(label=format_bytes(max(item.size, 0)))
            size.get_style_context().add_class("settings-row-sub")
            line.pack_end(size, False, False, 0)
            listing.pack_start(line, False, False, 0)
        scroller = bright_scroller(listing, 170)
        scroller.set_min_content_width(420)
        extra.pack_start(scroller, False, False, 0)
        if unsafe:
            extra.pack_start(self._line(_("bg_nobackup_warn").format(n=unsafe), "bg-saves",
                                        "triangle-alert-symbolic"), False, False, 0)
        folders = [i for i in items if i.kind != "proton"]
        choice = {"trash": True}
        if folders:
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
                inner.pack_start(self._image(icon, 22), False, False, 0)
                t = Gtk.Label(label=_(title))
                t.get_style_context().add_class("bg-tile-title")
                inner.pack_start(t, False, False, 0)
                s_ = Gtk.Label(label=_(sub), justify=Gtk.Justification.CENTER)
                s_.set_line_wrap(True)
                s_.set_max_width_chars(20)
                s_.get_style_context().add_class("bg-tile-sub")
                inner.pack_start(s_, False, False, 0)
                card.add(inner)
                card.connect("toggled", lambda b, k=key: b.get_active() and choice.update(trash=k == "trash"))
                cards.pack_start(card, True, True, 0)
            extra.pack_start(cards, False, False, 0)
        if len(folders) < len(items):
            extra.pack_start(self._line(_("bg_proton_short"), icon="rocket-symbolic"), False, False, 0)
        extra.show_all()
        answer = self.app._card_dialog(heading, None,
                                       [(_("dialog_cancel"), Gtk.ResponseType.NO, "ghost"),
                                        (_("bg_delete"), Gtk.ResponseType.YES, "danger")],
                                       tone="danger", icon_name="user-trash-symbolic",
                                       default=Gtk.ResponseType.NO, transient_for=self.sw.dialog, extra=extra)
        if answer != Gtk.ResponseType.YES:
            return
        self._run_delete(items, choice["trash"])

    def _run_delete(self, items: List[bilge.Item], to_trash: bool):
        self.busy = True
        if self.sel_label is not None:
            self.sel_label.set_text(self._("bg_deleting"))
        if self.sel_button is not None:
            self.sel_button.set_visible(False)

        def work():
            results = [(item, DELETE(self.app, item, to_trash)) for item in items]
            GLib.idle_add(done, results)

        def done(results):
            self.busy = False
            if self._gone:
                return False
            gone = [i for i, err in results if err is None]
            no_trash = [i for i, err in results if err == "trash"]
            failed = [(i, err) for i, err in results if err not in (None, "trash")]
            self._log(gone, to_trash)
            if failed:
                self.app._show_info_dialog("\n".join(self._("bg_failed").format(path=str(i.path), error=err)
                                                     for i, err in failed),
                                           message_type=Gtk.MessageType.WARNING, transient_for=self.sw.dialog)
            if no_trash and self._ask_permanent(no_trash):
                self._run_delete(no_trash, False)
                return False
            self.scan()
            return False
        threading.Thread(target=work, daemon=True, name="plunderhub-bilge-delete").start()

    def _ask_permanent(self, items: List[bilge.Item]) -> bool:
        _ = self._
        listing = Gtk.Label(xalign=0, selectable=True)
        listing.set_markup('<span insert_hyphens="false">'
                           + GLib.markup_escape_text("\n".join(str(i.path) for i in items)) + '</span>')
        listing.set_line_wrap(True)
        listing.set_line_wrap_mode(Pango.WrapMode.CHAR)
        listing.get_style_context().add_class("settings-row-sub")
        listing.show()
        answer = self.app._card_dialog(_("bg_no_trash_head"), _("bg_no_trash"),
                                       [(_("dialog_cancel"), Gtk.ResponseType.NO, "ghost"),
                                        (_("bg_permanent"), Gtk.ResponseType.YES, "danger")],
                                       tone="danger", icon_name="user-trash-symbolic",
                                       default=Gtk.ResponseType.NO, transient_for=self.sw.dialog, extra=listing)
        return answer == Gtk.ResponseType.YES

    def _log(self, items: List[bilge.Item], to_trash: bool):
        if not items:
            return
        log = list(self.app.settings.get("bilge_log") or [])
        now = time.time()
        for item in items:
            log.insert(0, {"title": self._name(item), "path": str(item.path), "size": max(item.size, 0),
                           "when": now, "trash": bool(to_trash and item.kind != "proton")})
        self.app.settings["bilge_log"] = log[:LOG_KEEP]
        save_settings(self.app.settings)
