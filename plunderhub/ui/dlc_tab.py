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

"""The game page's DLC tab (core/dlc.py does the work).

On top three tiles: how many DLCs, how many are in, what they take on
disk. Under them a row per DLC - a tick, its name and size, and a pill:
installed, not installed, or "no files of its own" (only unlocks
something: no tick). At the bottom Install and Uninstall, each for the
ticked ones it applies to; while they work a progress bar takes the
buttons' place.

The tab button shows only once the list came back with something in it
(Steam, and Heroic's Epic and GOG games)."""

import math
import threading
from typing import Dict, List, Optional

from plunderhub.core import dlc as core
from plunderhub.gi_setup import GLib, Gtk, Pango
from plunderhub.icons import get_icon_pixbuf
from plunderhub.logging_setup import logger

# uid -> the work going on for that game: {"text", "frac", "running"} -
# kept here so the tab finds it again when the page is opened anew
JOBS: Dict[str, Dict] = {}


def _fmt(n: int) -> str:
    if n >= 1e9:
        return f"{n / 1e9:.1f} GB"
    if n >= 1e6:
        return f"{n / 1e6:.0f} MB"
    return f"{max(1, round(n / 1e3))} KB" if n else ""


def _hex_rgb(hex_colour: str):
    hex_colour = hex_colour.lstrip("#")
    return tuple(int(hex_colour[i:i + 2], 16) / 255 for i in (0, 2, 4))


class _Tick(Gtk.DrawingArea):
    """The row's tick: a rounded box, filled in the accent with a check
    when the row is picked. Drawn - an image swap re-laid the row out."""
    SIZE = 22

    def __init__(self, row: Gtk.ToggleButton, accent: str):
        super().__init__()
        self.row, self.accent = row, _hex_rgb(accent)
        self.set_size_request(self.SIZE, self.SIZE)
        self.set_valign(Gtk.Align.CENTER)
        self.connect("draw", self._draw)

    def _draw(self, widget, cr):
        s = self.SIZE
        x = y = 1.5
        w = h = s - 3
        r = 6
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()
        if self.row.get_active():
            cr.set_source_rgb(*self.accent)
            cr.fill()
            lum = 0.299 * self.accent[0] + 0.587 * self.accent[1] + 0.114 * self.accent[2]
            cr.set_source_rgb(*((0.05, 0.08, 0.09) if lum > 0.55 else (1, 1, 1)))
            cr.set_line_width(2.4)
            cr.set_line_cap(1)
            cr.set_line_join(1)
            cr.move_to(s * 0.28, s * 0.52)
            cr.line_to(s * 0.44, s * 0.68)
            cr.line_to(s * 0.73, s * 0.34)
            cr.stroke()
        else:
            ctx = widget.get_style_context()
            fg = ctx.get_color(ctx.get_state())
            cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.38)
            cr.set_line_width(1.6)
            cr.stroke()


class DlcTab(Gtk.Box):
    def __init__(self, app, game, page):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.app, self.game, self.page = app, game, page
        self._ = app._
        self.set_border_width(28)
        self.accent = app._home_accent_hex()
        self.list: Optional[core.DlcList] = None
        self.rows: Dict[str, Gtk.ToggleButton] = {}
        self._poll_id = 0
        self._loading = False

        self.tiles = Gtk.Box(spacing=12, homogeneous=True)
        self.pack_start(self.tiles, False, False, 0)
        self.rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.pack_start(self.rows_box, False, False, 0)
        self.pack_start(self._actions(), False, False, 0)
        self.load()

    # loading ----------------------------------------------------------------------------

    def load(self, fresh: bool = False):
        if self._loading:
            return
        self._loading = True

        def work():
            found = core.list_dlcs(self.game, fresh=fresh)

            def done():
                self._loading = False
                self.list = found
                self._fill()
                return False
            GLib.idle_add(done)
        threading.Thread(target=work, name="plunderhub-dlc-list", daemon=True).start()

    def _fill(self):
        _ = self._
        for box in (self.tiles, self.rows_box):
            for child in box.get_children():
                box.remove(child)
        self.rows.clear()
        dlcs = self.list.dlcs if self.list else []
        self.page.set_dlc_tab_visible(bool(dlcs))
        if not dlcs:
            return
        have = [d for d in dlcs if d.installed and d.files]
        disk = sum(d.size for d in have)
        self.tiles.pack_start(self._tile(str(len(dlcs)), _("dlc_tile_all")), True, True, 0)
        self.tiles.pack_start(self._tile(f"{len(have)}/{len(self.list.actionable)}", _("dlc_tile_in")), True, True, 0)
        self.tiles.pack_start(self._tile(_fmt(disk) or "–", _("dlc_tile_disk")), True, True, 0)

        head = Gtk.Box(spacing=10)
        label = Gtk.Label(label=_("dlc_list_head"), xalign=0)
        label.get_style_context().add_class("gp-card-label")
        head.pack_start(label, True, True, 0)
        self.all_btn = Gtk.Button(label=_("dlc_select_all"))
        self.all_btn.get_style_context().add_class("bg-icon-btn")
        self.all_btn.connect("clicked", lambda *_a: self._select_all())
        self.all_btn.set_sensitive(bool(self.list.actionable))
        head.pack_end(self.all_btn, False, False, 0)
        self.rows_box.pack_start(head, False, False, 4)
        for d in dlcs:
            self.rows_box.pack_start(self._row(d), False, False, 0)
        self.tiles.show_all()
        self.rows_box.show_all()
        self._update_actions()
        job = JOBS.get(self.game.uid)
        if job and job.get("running"):
            self._show_job()

    def _tile(self, value: str, name: str) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.get_style_context().add_class("pf-fact")
        v = Gtk.Label(label=value)
        v.get_style_context().add_class("dlc-tile-value")
        # a non-breaking space: with the letter spacing "ON DISK" asked for two lines' height
        n = Gtk.Label(label=name.upper().replace(" ", "\u00a0"))
        n.get_style_context().add_class("pf-tile-name")
        v.set_valign(Gtk.Align.CENTER)
        for label in (v, n):
            label.set_line_wrap(False)
            label.set_single_line_mode(True)
        box.pack_start(v, True, True, 0)
        box.pack_end(n, False, False, 0)      # the names on one line, whatever the number's height
        return box

    def _row(self, d: core.Dlc) -> Gtk.Widget:
        _ = self._
        line = Gtk.Box(spacing=14)
        if d.files:
            row = Gtk.ToggleButton()
            row.get_style_context().add_class("dlc-row")
            tick = _Tick(row, self.accent)
            row.connect("toggled", lambda *_a, t=tick: (t.queue_draw(), self._update_actions()))
            line.pack_start(tick, False, False, 0)
            row.dlc = d
            self.rows[d.id] = row
        else:
            row = Gtk.Box()
            row.get_style_context().add_class("dlc-row-static")
            pb = get_icon_pixbuf("sparkles-symbolic", 20, self.accent)
            if pb is not None:
                img = Gtk.Image.new_from_pixbuf(pb)
                img.set_size_request(_Tick.SIZE, _Tick.SIZE)
                line.pack_start(img, False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        texts.set_valign(Gtk.Align.CENTER)
        title = Gtk.Label(label=d.title, xalign=0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        if len(d.title) > 48:
            title.set_tooltip_text(d.title)       # the cut-off end
        title.get_style_context().add_class("dlc-row-title")
        texts.pack_start(title, False, False, 0)
        sub_text = _fmt(d.size) if d.files else _("dlc_unlock_sub")
        if sub_text:
            sub = Gtk.Label(label=sub_text, xalign=0)
            sub.get_style_context().add_class("gp-card-sub")
            texts.pack_start(sub, False, False, 0)
        line.pack_start(texts, True, True, 0)
        pill_text, kind = ((_("dlc_installed"), "bg-pill-ok") if d.installed and d.files else
                           (_("dlc_missing"), "bg-pill-quiet") if d.files else (_("dlc_no_files"), "bg-pill-quiet"))
        pill = Gtk.Label(label=pill_text)
        pill.set_valign(Gtk.Align.CENTER)
        for c in ("bg-pill", kind):
            pill.get_style_context().add_class(c)
        line.pack_end(pill, False, False, 0)
        if isinstance(row, Gtk.Box):
            row.pack_start(line, True, True, 0)
        else:
            row.add(line)
        return row

    # picking --------------------------------------------------------------------------------

    def _picked(self, installed: bool) -> List[core.Dlc]:
        return [r.dlc for r in self.rows.values() if r.get_active() and r.dlc.installed == installed]

    def _select_all(self):
        on = not all(r.get_active() for r in self.rows.values())
        for r in self.rows.values():
            r.set_active(on)

    def _actions(self) -> Gtk.Widget:
        _ = self._
        bar = Gtk.Box(spacing=12)
        bar.get_style_context().add_class("gp-card")
        bar.get_style_context().add_class("dlc-actions")
        self.actions = bar
        self.note = Gtk.Label(xalign=0)
        self.note.set_line_wrap(True)
        self.note.get_style_context().add_class("gp-card-sub")
        bar.pack_start(self.note, True, True, 0)
        self.progress = Gtk.ProgressBar()
        self.progress.set_valign(Gtk.Align.CENTER)
        self.progress.set_size_request(220, -1)
        self.progress.set_no_show_all(True)
        bar.pack_start(self.progress, False, False, 0)
        self.uninstall_btn = self.app._dialog_button(_("dlc_uninstall"), "danger")
        self.uninstall_btn.connect("clicked", lambda *_a: self._start(remove=self._picked(True), install=[]))
        self.install_btn = self.app._dialog_button(_("dlc_install"), "accent")
        self.install_btn.connect("clicked", lambda *_a: self._start(install=self._picked(False), remove=[]))
        bar.pack_end(self.install_btn, False, False, 0)
        bar.pack_end(self.uninstall_btn, False, False, 0)
        return bar

    @staticmethod
    def _set_btn(btn: Gtk.Button, text: str):
        label = btn.get_child().get_children()[0]
        label.set_text(text)

    def _count_text(self, key: str, picked: List[core.Dlc]) -> str:
        size = sum(d.size for d in picked)
        if not picked:
            return self._(key)
        return self._(key + "_n").format(n=len(picked), size=_fmt(size)) if size else \
            self._(key + "_n_nosize").format(n=len(picked))

    def _update_actions(self):
        if JOBS.get(self.game.uid, {}).get("running"):
            return
        out, add = self._picked(True), self._picked(False)
        self._set_btn(self.uninstall_btn, self._count_text("dlc_uninstall", out))
        self._set_btn(self.install_btn, self._count_text("dlc_install", add))
        self.uninstall_btn.set_sensitive(bool(out))
        self.install_btn.set_sensitive(bool(add))
        self.uninstall_btn.set_visible(True)
        self.install_btn.set_visible(True)
        self.progress.set_visible(False)
        store = self.list.store if self.list else ""
        note = {"steam": "dlc_note_steam", "gog": "dlc_note_heroic", "epic": "dlc_note_epic"}.get(store)
        if out or add:
            self.note.set_text(self._(note) if note else "")
        else:
            self.note.set_text(self._("dlc_pick_hint"))
        if hasattr(self, "all_btn"):
            all_on = bool(self.rows) and all(r.get_active() for r in self.rows.values())
            self.all_btn.set_label(self._("dlc_select_none") if all_on else self._("dlc_select_all"))

    # doing it --------------------------------------------------------------------------------

    def _parent(self) -> Gtk.Window:
        return self.get_toplevel() if isinstance(self.get_toplevel(), Gtk.Window) else self.page

    def _start(self, install: List[core.Dlc], remove: List[core.Dlc]):
        _ = self._
        if not (install or remove) or self.list is None or JOBS.get(self.game.uid, {}).get("running"):
            return
        parent = self._parent()
        if self.app._is_game_running(self.game.uid):
            self.app._card_dialog(_("dlc_game_running_t").format(game=self.game.title), _("dlc_game_running_s"),
                                  [(_("dialog_ok"), Gtk.ResponseType.CLOSE, "ghost")],
                                  icon_name="circle-alert-symbolic", transient_for=parent)
            return
        if remove:
            size = sum(d.size for d in remove)
            names = ", ".join(d.title for d in remove[:3]) + ("…" if len(remove) > 3 else "")
            r = self.app._card_dialog(
                _("dlc_confirm_t").format(n=len(remove)),
                _("dlc_confirm_s").format(names=names, size=_fmt(size) or "?", game=self.game.title),
                [(_("dialog_cancel"), Gtk.ResponseType.CANCEL, "ghost"),
                 (_("dlc_confirm_btn"), Gtk.ResponseType.OK, "danger")],
                icon_name="user-trash-symbolic", default=Gtk.ResponseType.OK, transient_for=parent)
            if r != Gtk.ResponseType.OK:
                return
        store = self.list.store
        ids_in, ids_out = [d.id for d in install], [d.id for d in remove]
        if store == "steam":
            self._steam(ids_in, ids_out, parent)
        else:
            if store == "gog" and self.app._launcher_is_running("Heroic"):
                r = self.app._card_dialog(
                    _("tm_heroic_close_t"), _("dlc_heroic_close_s"),
                    [(_("tm_not_now"), Gtk.ResponseType.CANCEL, "ghost"),
                     (_("tm_close_heroic"), Gtk.ResponseType.OK, "accent")],
                    icon_name="circle-alert-symbolic", default=Gtk.ResponseType.OK, transient_for=parent)
                if r != Gtk.ResponseType.OK or not self.app.close_launcher_asking("Heroic", parent):
                    return
            self._heroic(store, install, remove)

    def _job(self, text: str, frac: Optional[float]):
        job = JOBS.setdefault(self.game.uid, {})
        job.update(text=text, frac=frac, running=True)

    def _show_job(self):
        """The bar shows the work: the buttons give way to a progress bar,
        followed twice a second while it goes."""
        job = JOBS.get(self.game.uid) or {}
        self.uninstall_btn.set_visible(False)
        self.install_btn.set_visible(False)
        self.progress.set_visible(True)
        frac = job.get("frac")
        self.note.set_text((job.get("text") or "") + (f"  ·  {round(frac * 100)} %" if frac is not None else ""))
        if frac is None:
            self.progress.pulse()
        else:
            self.progress.set_fraction(max(0.0, min(1.0, frac)))
        for r in self.rows.values():
            r.set_sensitive(False)
        if hasattr(self, "all_btn"):
            self.all_btn.set_sensitive(False)
        if not self._poll_id:
            self._poll_id = GLib.timeout_add(500, self._poll)
            self.connect("destroy", lambda *_a: self._poll_id and GLib.source_remove(self._poll_id))

    def _poll(self) -> bool:
        job = JOBS.get(self.game.uid) or {}
        if job.get("running"):
            self._show_job()
            return True
        self._poll_id = 0
        why = job.get("error")
        JOBS.pop(self.game.uid, None)
        if why:
            _ = self._
            self.app._card_dialog(_("dlc_failed_t").format(game=self.game.title), why,
                                  [(_("dialog_ok"), Gtk.ResponseType.CLOSE, "ghost")],
                                  icon_name="circle-alert-symbolic", transient_for=self._parent())
        self.load()
        return False

    def _heroic(self, store: str, install: List[core.Dlc], remove: List[core.Dlc]):
        _ = self._
        names = {d.id: d.title for d in install + remove}
        out_ids = {d.id for d in remove}
        self._job(_("dlc_working"), 0.0)
        self._show_job()
        game = self.game

        def step(done: int, total: int, dlc_id: str, pct: float):
            title = names.get(dlc_id, "")
            if store == "gog":
                text = _("dlc_working_gog")
            elif dlc_id in out_ids:
                text = _("dlc_removing").format(dlc=title)
            else:
                text = _("dlc_installing").format(dlc=title)
            frac = (done + pct / 100) / total if total else 1.0
            if total > 1 and store != "gog":
                text += f"  ·  {min(done + 1, total)}/{total}"
            JOBS[game.uid].update(text=text, frac=frac)

        def work():
            try:
                ok, why = core.heroic_change(store, core.target(game)[1], game.title,
                                             [d.id for d in install], [d.id for d in remove], on_step=step)
            except Exception as e:  # noqa: BLE001 - said in the dialog, kept in the log
                logger.exception(f"DLC change of {game.title}")
                ok, why = False, str(e)
            JOBS[game.uid].update(running=False, error=None if ok else (why or "?"))
        threading.Thread(target=work, name="plunderhub-dlc-change", daemon=True).start()

    # Steam: closed here (asked first if one of its games runs), the rest in
    # core.steam_apply - written, Steam in the tray applies it, closed and
    # started again as it was
    def _steam_cmd(self) -> List[str]:
        section = next((s for s in getattr(self.app, "_cached_launchers", []) or [] if s.get("name") == "Steam"), None)
        return list(section["cmd"]) if section and section.get("cmd") else ["steam"]

    def _steam(self, ids_in: List[str], ids_out: List[str], parent):
        _ = self._
        ran_before = self.app._launcher_is_running("Steam")
        if ran_before and not self.app.close_launcher_asking("Steam", parent):
            return
        appid = core.target(self.game)[1]
        self._job(_("dlc_steam_closing"), None)
        self._show_job()
        uid, cmd = self.game.uid, self._steam_cmd()

        def status(what: str):
            JOBS[uid].update(text=_("dlc_steam_applying") if what == "applying" else _("dlc_steam_finishing"))

        def work():
            try:
                ok, why = core.steam_apply(appid, ids_in, ids_out, cmd, ran_before, on_status=status)
            except Exception as e:  # noqa: BLE001 - said in the dialog, kept in the log
                logger.exception(f"DLC change of {appid}")
                ok, why = False, str(e)
            JOBS[uid].update(running=False, error=None if ok else (_("dlc_steam_slow") if why == "timeout" else why))
        threading.Thread(target=work, name="plunderhub-dlc-steam", daemon=True).start()
