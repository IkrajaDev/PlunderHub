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

"""The Treasure Map's Loading Dock tab: every install it started, in order.

Ours (Heroic's tools, Faugus) run one at a time: they can be moved up and
down, Heroic's paused and resumed, any stopped. Steam keeps its own queue;
with "Steer Steam's downloads" on (core/steam_cef.py - opt-in, Steam's
local port) its downloads pause, resume and move from here too, and the
switch goes off by itself once they're done. Open in Steam always shows
Steam's own Downloads page."""

import math
import threading
import time
from typing import Dict, List, Optional

from plunderhub.core import steam_cef
from plunderhub.core.persistence import save_settings
from plunderhub.gi_setup import GLib, Gtk, Pango
import gi  # noqa: E402
gi.require_version("PangoCairo", "1.0")
from plunderhub.logging_setup import logger

SETTING = "steam_control_on"
STEAM_POLL_S = 1


def activity_text(_, dl) -> str:
    """What Steam does while the bar still stands at 0 (core/treasure.steam_activity)."""
    act = dl.get("activity")
    if not act:
        return ""
    key, arg = act
    # waiting behind something or stopped by Steam: said at any percent;
    # the rest only while nothing has landed yet
    if key not in ("other", "suspended", "dialog") and float(dl.get("frac") or 0) >= 0.005:
        return ""
    if key == "other":
        return _("ld_act_other").format(name=arg) if arg else _("ld_act_other_any")
    return _(f"ld_act_{key}")


def _fmt(n: float) -> str:
    return f"{n / 1e9:.1f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


class LoadingDock(Gtk.ScrolledWindow):
    def __init__(self, view):
        super().__init__()
        self.view, self.app = view, view.app
        self._ = view.app._
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.set_overlay_scrolling(False)
        self.get_style_context().add_class("settings-scroll")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_start(20)
        box.set_margin_end(24)
        box.set_margin_top(4)
        box.set_margin_bottom(28)
        self.add(box)
        title = Gtk.Label(label=self._("ld_tab"), xalign=0)
        title.get_style_context().add_class("home-section")
        box.pack_start(title, False, False, 0)
        sub = Gtk.Label(label=self._("ld_page_sub"), xalign=0)
        sub.get_style_context().add_class("tm-sub")
        box.pack_start(sub, False, False, 0)
        # the Steam switch first, then the speed over the last minutes
        self.steam_card = self._steam_card()
        box.pack_start(self.steam_card, False, False, 0)
        self.graph_card = self._graph_card()
        self.graph_card.set_no_show_all(True)
        box.pack_start(self.graph_card, False, False, 0)
        self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.pack_start(self.list, False, False, 0)
        self.rows: Dict[str, Dict] = {}
        self.steam_state: Optional[Dict] = None
        self._poll_id = 0
        self._asking = False
        self._order: list = []
        self._groups: Dict[str, List[str]] = {}
        self.connect("map", lambda *_a: self._start_poll())
        # not stopped when the page goes: the sidebar's foot follows Steam's state too

        self.show_all()

    # Steam's switch -------------------------------------------------------------------------

    def _steam_card(self) -> Gtk.Widget:
        _ = self._
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        card.get_style_context().add_class("gp-card")
        top = Gtk.Box(spacing=14)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        title = Gtk.Label(label=_("ld_steam_title"), xalign=0)
        title.get_style_context().add_class("pf-record-title")
        texts.pack_start(title, False, False, 0)
        sub = Gtk.Label(label=_("ld_steam_sub"), xalign=0)
        sub.set_line_wrap(True)
        sub.get_style_context().add_class("gp-card-sub")
        texts.pack_start(sub, False, False, 0)
        top.pack_start(texts, True, True, 0)
        self.switch = Gtk.Switch()
        self.switch.set_valign(Gtk.Align.CENTER)
        self.switch.set_active(steam_cef.enabled())
        # after the switch's own click (a question may follow - gtk3-pitfalls.md)
        self.switch.connect("notify::active", lambda *_a: self._asking or GLib.idle_add(
            lambda: self._switched(self.switch.get_active()) and False))
        top.pack_end(self.switch, False, False, 0)
        card.pack_start(top, False, False, 0)
        self.steam_note = Gtk.Label(xalign=0)
        self.steam_note.set_line_wrap(True)
        self.steam_note.get_style_context().add_class("gp-card-sub")
        self.steam_note.set_no_show_all(True)
        card.pack_start(self.steam_note, False, False, 0)
        return card

    # the speed, over the last minutes (as Heroic's Downloads page shows it) ------------------

    def _graph_card(self) -> Gtk.Widget:
        _ = self._
        card = Gtk.Box(spacing=20)
        card.get_style_context().add_class("gp-card")
        self.graph = SpeedGraph(self.app)
        card.pack_start(self.graph, True, True, 0)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        side.set_valign(Gtk.Align.CENTER)
        self.rate_label = Gtk.Label(label="0 MB/s", xalign=0)
        self.rate_label.get_style_context().add_class("ld-rate")
        self.app._apply_tabular_figures(self.rate_label)
        side.pack_start(self.rate_label, False, False, 0)
        cap = Gtk.Label(label=_("ld_rate_caption"), xalign=0)
        cap.get_style_context().add_class("gp-card-sub")
        side.pack_start(cap, False, False, 0)
        card.pack_start(side, False, False, 0)
        self.all_btn = self._icon_btn("pause-symbolic", _("ld_pause_all"), self._pause_all)
        card.pack_end(self.all_btn, False, False, 0)
        return card

    def _pause_all(self):
        """Every install at once: ours paused, Steam's too while it's steered;
        again: all resumed."""
        own = [u for u in self._uids() if self.view.downloads[u].get("direct")
               and self.view.downloads[u].get("pausable")]
        any_running = any(self.view.downloads[u].get("state") in ("downloading", "installing", "waiting")
                          for u in own) or (self.steam_state is not None and not self.steam_state.get("paused"))
        for uid in own:
            (self.view.pause_own if any_running else self.view.resume_own)(uid)
        if self.steam_state is not None:
            self._steam_action(steam_cef.pause_all, any_running)
        self.refresh()

    def _set_switch(self, on: bool):
        self._asking = True
        self.switch.set_active(on)
        self._asking = False

    def _switched(self, on: bool):
        _ = self._
        if not on:
            steam_cef.disable()
            self.app.settings[SETTING] = False
            save_settings(self.app.settings)
            self.refresh()
            return
        if not steam_cef.enable():
            self._set_switch(False)
            return
        self.app.settings[SETTING] = True
        save_settings(self.app.settings)
        if self.app._launcher_is_running("Steam") and not steam_cef.available():
            # the port opens when Steam starts: once, now (it goes on downloading after)
            r = self.app._card_dialog(_("ld_restart_t"), _("ld_restart_s"),
                                      [(_("tm_not_now"), Gtk.ResponseType.CANCEL, "ghost"),
                                       (_("ld_restart_btn"), Gtk.ResponseType.OK, "accent")],
                                      icon_name="view-refresh-symbolic", default=Gtk.ResponseType.OK,
                                      transient_for=self.get_toplevel())
            windowed = self.app.launcher_windowed("Steam")
            if r == Gtk.ResponseType.OK and self.app.close_launcher_asking("Steam", self.get_toplevel()):
                self.view._steam_windowed = windowed is True
                self.view._steam_start_silent()
                self._start_poll()
        self.refresh()

    def auto_off(self):
        """Switched on here and the Steam installs it was on for are done:
        off again (switched on with none yet: it waits for them)."""
        if not self.app.settings.get(SETTING):
            return
        if self._steam_uids():
            self._had_steam = True
            return
        if not getattr(self, "_had_steam", False):
            return
        self._had_steam = False
        steam_cef.disable()
        self.app.settings[SETTING] = False
        save_settings(self.app.settings)
        self._set_switch(False)
        logger.info("Loading Dock: Steam's installs are done - its control switched off again")

    # the list -------------------------------------------------------------------------------

    def _uids(self) -> List[str]:
        return [u for u in self.view.queue if self.view.downloads.get(u, {}).get("state") != "done"]

    def _steam_uids(self) -> List[str]:
        return [u for u in self._uids() if self.view.downloads[u].get("launcher") == "Steam"]

    def _game(self, uid):
        return self.view.games_by_uid.get(uid) or getattr(self.view, "faugus_pending", {}).get(uid)

    def _kind(self, uid: str) -> str:
        """now (downloading / installing), next (waiting) or paused."""
        dl = self.view.downloads[uid]
        item = self._steam_item(uid) if dl.get("launcher") == "Steam" else None
        if dl.get("state") == "paused" or (item and (item["paused"] or (self.steam_state or {}).get("paused"))):
            return "paused"
        if item is not None:
            return "now" if item["active"] else "next"
        return "now" if dl.get("state") in ("downloading", "installing") else "next"

    def refresh(self):
        """Now loading on top, then what's next (numbered, in order), then
        what's paused; rows built anew only when that changes."""
        uids = self._uids()
        steam = self._steam_info()
        if steam:
            # Steam's own order among its items
            index = {str(x["appid"]): x["queue"] for x in steam.get("queued", [])}
            st = [u for u in uids if self.view.downloads[u].get("launcher") == "Steam"]
            st.sort(key=lambda u: index.get(u.split(":", 1)[1], 999))
            it = iter(st)
            uids = [next(it) if self.view.downloads[u].get("launcher") == "Steam" else u for u in uids]
        groups = {k: [u for u in uids if self._kind(u) == k] for k in ("now", "next", "paused")}
        done = [e for e in getattr(self.view, "aboard_log", []) if e["uid"] not in uids]
        layout = [(k, tuple(groups[k])) for k in ("now", "next", "paused")] + [("done", tuple(e["uid"] for e in done))]
        if layout != self._order:
            self._order = layout
            self._groups = groups
            for child in self.list.get_children():
                self.list.remove(child)
            self.rows.clear()
            if not uids:
                self.list.pack_start(self._empty(), False, False, 0)
            # no_show_all keeps it out of the page's show_all while idle - and makes
            # its own show_all a no-op: lifted before showing it (else an empty frame)
            self.graph_card.set_no_show_all(not uids)
            if uids:
                self.graph_card.show_all()
            else:
                self.graph_card.hide()
            for key, title in (("now", "ld_sec_now"), ("next", "ld_sec_next"), ("paused", "ld_sec_paused")):
                if not groups[key]:
                    continue
                head = Gtk.Label(label=self._(title).upper(), xalign=0)
                head.get_style_context().add_class("gp-card-label")
                head.set_margin_top(6)
                self.list.pack_start(head, False, False, 0)
                for i, uid in enumerate(groups[key]):
                    self.list.pack_start(self._row(uid, key, i), False, False, 0)
            if done:
                head = Gtk.Box(spacing=10)
                head.set_margin_top(6)
                lab = Gtk.Label(label=self._("ld_sec_done").upper(), xalign=0)
                lab.get_style_context().add_class("gp-card-label")
                head.pack_start(lab, True, True, 0)
                clear = Gtk.Button(label=self._("ld_clear"))
                clear.get_style_context().add_class("bg-icon-btn")
                clear.connect("clicked", lambda *_a: self._clear_done())
                head.pack_end(clear, False, False, 0)
                self.list.pack_start(head, False, False, 0)
                for e in done:
                    self.list.pack_start(self._done_row(e), False, False, 0)
            self.list.show_all()
        for uid in uids:
            self._update_row(uid)
        self._update_steam_card()
        self.auto_off()

    def _empty(self) -> Gtk.Widget:
        """Nothing coming aboard: the quiet dock, and the way to the map."""
        from plunderhub.gi_setup import Gdk
        from plunderhub.icons import get_icon_pixbuf
        _ = self._
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.get_style_context().add_class("gp-card")
        box.get_style_context().add_class("ld-empty")
        scale = self.get_scale_factor() or 1
        pb = get_icon_pixbuf("anchor-symbolic", 56 * scale, self.app._home_accent_hex())
        if pb is not None:
            box.pack_start(Gtk.Image.new_from_surface(Gdk.cairo_surface_create_from_pixbuf(pb, scale, None)),
                           False, False, 0)
        t = Gtk.Label(label=_("ld_empty"))
        t.get_style_context().add_class("ld-empty-title")
        box.pack_start(t, False, False, 0)
        sub = Gtk.Label(label=_("ld_empty_sub"))
        sub.set_line_wrap(True)
        sub.set_justify(Gtk.Justification.CENTER)
        sub.set_max_width_chars(52)
        sub.get_style_context().add_class("gp-card-sub")
        box.pack_start(sub, False, False, 0)
        go = self.app._dialog_button(_("ld_to_map"), "accent")
        go.set_halign(Gtk.Align.CENTER)
        go.set_margin_top(6)
        go.connect("clicked", lambda *_a: self.app._set_home_nav("map"))
        box.pack_start(go, False, False, 0)
        return box

    def _done_row(self, e: Dict) -> Gtk.Widget:
        """A finished install: its cover, name, launcher, size and when."""
        _ = self._
        card = Gtk.Box(spacing=14)
        card.get_style_context().add_class("gp-card")
        card.get_style_context().add_class("ld-done")
        game = self._game(e["uid"])
        card.pack_start(self._art(game, {"launcher": e["launcher"]}), False, False, 0)
        mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        mid.set_valign(Gtk.Align.CENTER)
        title = Gtk.Label(label=e["title"], xalign=0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.get_style_context().add_class("dlc-row-title")
        mid.pack_start(title, False, False, 0)
        parts = [e["launcher"]] + ([_fmt(e["size"])] if e.get("size") else []) + \
            [_("ld_done_at").format(time=time.strftime("%H:%M", time.localtime(e["at"])))]
        sub = Gtk.Label(label="   ·   ".join(parts), xalign=0)
        sub.get_style_context().add_class("gp-card-sub")
        mid.pack_start(sub, False, False, 0)
        card.pack_start(mid, True, True, 0)
        pill = Gtk.Label(label=_("ld_state_aboard"))
        pill.set_valign(Gtk.Align.CENTER)
        for c in ("bg-pill", "bg-pill-ok"):
            pill.get_style_context().add_class(c)
        card.pack_end(pill, False, False, 0)
        return card

    def _clear_done(self):
        log = getattr(self.view, "aboard_log", None)
        if log is not None:
            log.clear()
        self.refresh()

    def _set_icon(self, button: Gtk.Button, icon: str, colour: Optional[str], px: int):
        from plunderhub.gi_setup import Gdk
        from plunderhub.icons import get_icon_pixbuf
        scale = self.get_scale_factor() or 1
        pb = get_icon_pixbuf(icon, px * scale, colour or self.app._home_accent_hex())
        if pb is not None:
            button.set_image(Gtk.Image.new_from_surface(Gdk.cairo_surface_create_from_pixbuf(pb, scale, None)))
        button._icon = icon

    def _icon_btn(self, icon: str, tip: str, cb, colour: Optional[str] = None, mini: bool = False) -> Gtk.Button:
        """A round icon button, all of a kind the same size - the rows' columns line up."""
        b = Gtk.Button()
        b.get_style_context().add_class("ld-mini-btn" if mini else "ld-icon-btn")
        self._set_icon(b, icon, colour, 12 if mini else 16)
        b.set_tooltip_text(tip)
        b.set_valign(Gtk.Align.CENTER)
        b.connect("clicked", lambda *_a: cb())
        return b

    @staticmethod
    def _hold(widget: Gtk.Widget, on: bool):
        """Shown or not, its place kept (the columns stay in line)."""
        widget.set_opacity(1.0 if on else 0.0)
        widget.set_sensitive(on)

    def _row(self, uid: str, kind: str, index: int) -> Gtk.Widget:
        _ = self._
        dl = self.view.downloads[uid]
        game = self._game(uid)
        card = Gtk.Box(spacing=14)
        card.get_style_context().add_class("gp-card")
        # the order: its number, and moving it right beside it
        order = Gtk.Box(spacing=6)
        order.set_valign(Gtk.Align.CENTER)
        num = Gtk.Label(label=str(index + 1) if kind == "next" else "")
        num.get_style_context().add_class("ld-num")
        num.set_valign(Gtk.Align.CENTER)
        self._hold(num, kind == "next")
        order.pack_start(num, False, False, 0)
        arrows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        arrows.set_valign(Gtk.Align.CENTER)
        up = self._icon_btn("go-up-symbolic", _("ld_up"), lambda: self._move(uid, -1), mini=True)
        down = self._icon_btn("go-down-symbolic", _("ld_down"), lambda: self._move(uid, 1), mini=True)
        arrows.pack_start(up, False, False, 0)
        arrows.pack_start(down, False, False, 0)
        order.pack_start(arrows, False, False, 0)
        card.pack_start(order, False, False, 0)
        art = self._art(game, dl)
        card.pack_start(art, False, False, 0)
        mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        mid.set_valign(Gtk.Align.CENTER)
        line = Gtk.Box(spacing=10)
        title = Gtk.Label(label=game.title if game else uid, xalign=0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.get_style_context().add_class("dlc-row-title")
        line.pack_start(title, True, True, 0)
        pill = Gtk.Label()
        pill.get_style_context().add_class("bg-pill")
        line.pack_end(pill, False, False, 0)
        mid.pack_start(line, False, False, 0)
        bar = Gtk.ProgressBar()
        bar.get_style_context().add_class("ld-bar")
        mid.pack_start(bar, False, False, 0)
        detail = Gtk.Label(xalign=0)
        detail.get_style_context().add_class("gp-card-sub")
        mid.pack_start(detail, False, False, 0)
        card.pack_start(mid, True, True, 0)
        buttons = Gtk.Box(spacing=8)
        buttons.set_valign(Gtk.Align.CENTER)
        pause = self._icon_btn("pause-symbolic", _("ld_pause"), lambda: self._pause(uid))
        stop = self._icon_btn("media-playback-stop-symbolic", _("tm_stop"), lambda: self._stop(uid), colour="#e05252")
        steam = self._icon_btn("external-link-symbolic", _("ld_open_steam"),
                               lambda: self.view._steam_quietly("steam://open/downloads"))
        for b in (pause, stop, steam):
            buttons.pack_start(b, False, False, 0)
        card.pack_end(buttons, False, False, 0)
        self.rows[uid] = {"art": art, "pill": pill, "bar": bar, "detail": detail, "up": up, "down": down,
                          "pause": pause, "stop": stop, "steam": steam, "kind": kind, "arrows": arrows}
        return card

    ART_W, ART_H = 42, 56

    def _art(self, game, dl) -> Gtk.Widget:
        """A slot for the game's cover, small - from the map's own cover
        loader (never a download here); its launcher's logo meanwhile."""
        slot = Gtk.Box()
        slot.set_size_request(self.ART_W, self.ART_H)
        slot.set_valign(Gtk.Align.CENTER)
        slot.dl_launcher = dl.get("launcher") or "Steam"
        self._fill_art(slot, game)
        return slot

    def _fill_art(self, slot: Gtk.Box, game):
        for child in slot.get_children():
            slot.remove(child)
        pb = self.view.loader.get(game.uid) if game is not None else None
        if pb is None and game is not None and not self.view.loader.known(game.uid):
            self.view.loader.want(game)
        if pb is not None:
            scale = self.get_scale_factor() or 1
            from plunderhub.gi_setup import Gdk, GdkPixbuf
            small = pb.scale_simple(self.ART_W * scale, self.ART_H * scale, GdkPixbuf.InterpType.BILINEAR)
            img = Gtk.Image.new_from_surface(Gdk.cairo_surface_create_from_pixbuf(small, scale, None))
            img.get_style_context().add_class("ld-cover")
        else:
            img = self.app._load_launcher_icon(slot.dl_launcher, 32)
        img.set_valign(Gtk.Align.CENTER)
        img.set_halign(Gtk.Align.CENTER)
        slot.pack_start(img, True, True, 0)
        slot.show_all()

    def cover_ready(self, uid: str):
        """The map's loader has the cover now: into the row's slot."""
        row = self.rows.get(uid)
        if row is not None and row.get("art") is not None:
            self._fill_art(row["art"], self._game(uid))

    def _steam_item(self, uid: str) -> Optional[Dict]:
        s = self.steam_state
        if not s:
            return None
        appid = uid.split(":", 1)[1]
        return next((x for x in s.get("queued", []) + s.get("unqueued", []) if str(x["appid"]) == appid), None)

    def _update_row(self, uid: str):
        _ = self._
        r, dl = self.rows.get(uid), self.view.downloads.get(uid)
        if r is None or dl is None:
            return
        is_steam = dl.get("launcher") == "Steam"
        item = self._steam_item(uid) if is_steam else None
        state = dl.get("state")
        paused = state == "paused" or bool(item and (item["paused"] or (self.steam_state or {}).get("paused")))
        if paused:
            key, kind = "ld_state_paused", "bg-pill-warn"
        elif state in ("waiting", "queued", "asked") or (item and not item["active"]):
            key, kind = "ld_state_waiting", "bg-pill-quiet"
        elif state == "installing":
            key, kind = "ld_state_installing", "bg-pill-ok"
        else:
            key, kind = "ld_state_downloading", "bg-pill-ok"
        pill = r["pill"]
        pill.set_text(_(key))
        ctx = pill.get_style_context()
        for c in ("bg-pill-warn", "bg-pill-quiet", "bg-pill-ok"):
            (ctx.add_class if c == kind else ctx.remove_class)(c)
        frac = max(0.0, min(1.0, float(dl.get("frac") or 0)))
        r["bar"].set_fraction(frac)
        parts = [f"{int(frac * 100)} %"]
        size = dl.get("size") or 0
        if size:
            parts.append(_("tm_of").format(done=_fmt(dl.get("got") or frac * size), total=_fmt(size)))
        rate = dl.get("rate") or dl.get("speed") or 0
        if rate > 1e5 and not paused and key == "ld_state_downloading":
            left = max(0, (size - (dl.get("got") or frac * size)) / rate) if size else 0
            mins, secs = divmod(int(left), 60)
            parts += [f"{rate / 1e6:.0f} MB/s"] + ([_("tm_left").format(m=mins, s=f"{secs:02d}")] if left else [])
        act = activity_text(_, dl)
        r["detail"].set_text(act if act else "   ·   ".join(parts))
        steer = is_steam and item is not None          # Steam's control on and answering
        own = bool(dl.get("direct"))
        kind = r["kind"]
        # moving: among what's next, ours among ours, Steam's among Steam's
        group = [u for u in self._groups.get("next", []) if bool(self.view.downloads[u].get("direct")) == own]
        pos = group.index(uid) if uid in group else -1
        movable = kind == "next" and (own or steer)
        self._hold(r["arrows"], movable)
        self._hold(r["up"], movable and pos > 0)
        self._hold(r["down"], movable and 0 <= pos < len(group) - 1)
        self._hold(r["pause"], bool((own and dl.get("pausable")) or steer))
        icon = "media-playback-start-symbolic" if paused else "pause-symbolic"
        if getattr(r["pause"], "_icon", None) != icon:
            self._set_icon(r["pause"], icon, None, 16)
            r["pause"].set_tooltip_text(_("ld_resume") if paused else _("ld_pause"))
        self._hold(r["stop"], own or is_steam)
        self._hold(r["steam"], is_steam)

    # actions --------------------------------------------------------------------------------

    def _move(self, uid: str, step: int):
        dl = self.view.downloads.get(uid) or {}
        if dl.get("direct"):
            # swapped with its neighbour among what's next of ours
            group = [u for u in self._groups.get("next", []) if self.view.downloads.get(u, {}).get("direct")]
            if uid not in group:
                return
            j = group.index(uid) + step
            if 0 <= j < len(group):
                q = self.view.queue
                a, b = q.index(uid), q.index(group[j])
                q[a], q[b] = q[b], q[a]
                self.view._update_dock()
            return
        item = self._steam_item(uid)
        if item is not None and item.get("queue", -1) >= 0:
            self._steam_action(steam_cef.move, uid.split(":", 1)[1], max(0, item["queue"] + step))

    def _pause(self, uid: str):
        dl = self.view.downloads.get(uid) or {}
        if dl.get("direct"):
            if dl.get("state") == "paused":
                self.view.resume_own(uid)
            else:
                self.view.pause_own(uid)
            self.refresh()
            return
        item = self._steam_item(uid)
        if item is None:
            return
        appid = uid.split(":", 1)[1]
        if (self.steam_state or {}).get("paused"):
            self._steam_action(steam_cef.pause_all, False)
        elif item["paused"]:
            self._steam_action(steam_cef.resume, appid)
        else:
            self._steam_action(steam_cef.pause, appid)

    def _stop(self, uid: str):
        _ = self._
        dl = self.view.downloads.get(uid) or {}
        game = self._game(uid)
        name = game.title if game else uid
        r = self.app._card_dialog(_("ld_stop_t").format(game=name), _("ld_stop_s"),
                                  [(_("dialog_cancel"), Gtk.ResponseType.CANCEL, "ghost"),
                                   (_("tm_stop"), Gtk.ResponseType.OK, "danger")],
                                  icon_name="circle-alert-symbolic", default=Gtk.ResponseType.OK,
                                  transient_for=self.get_toplevel())
        if r != Gtk.ResponseType.OK:
            return
        if dl.get("direct"):
            self.view.stop_direct(uid)
        elif game is not None and dl.get("launcher") == "Steam":
            # what's there of it goes, as an uninstall (Steam closed meanwhile)
            if uid in self.view.queue:
                self.view.queue.remove(uid)
            self.view.downloads.pop(uid, None)
            self.view.start_uninstall(game)
        self.refresh()

    def _steam_action(self, fn, *args):
        def work():
            fn(*args)
            time.sleep(0.6)
            state = steam_cef.downloads()
            GLib.idle_add(lambda: self._got_steam(state) and False)
        threading.Thread(target=work, name="plunderhub-steam-control", daemon=True).start()

    # Steam's state, followed while the tab is shown ------------------------------------------

    def _steam_info(self) -> Optional[Dict]:
        return self.steam_state if steam_cef.enabled() else None

    def _update_steam_card(self):
        _ = self._
        on = steam_cef.enabled()
        if self.switch.get_active() != on:
            self._set_switch(on)
        s = self.steam_state
        if not on:
            note = ""
        elif s is None:
            port = getattr(self, "_port_open", False)      # from the poll's worker thread
            note = "" if port else _("ld_steam_next_start")
            if self._steam_uids() and port:
                note = _("ld_steam_waiting")
        elif s.get("paused"):
            note = _("ld_steam_all_paused")
        else:
            note = ""           # on and answering: the switch says it
        self.steam_note.set_text(note)
        self.steam_note.set_visible(bool(note))
        # the graph's numbers and the Pause all / Resume all button
        rate = sum(self._rate(u) for u in (self._groups.get("now") or []))
        self.graph.add(rate)
        self.rate_label.set_text(f"{rate / 1e6:.1f} MB/s")
        everything_paused = bool(self._uids()) and not self._groups.get("now") and not self._groups.get("next")
        icon = "media-playback-start-symbolic" if everything_paused else "pause-symbolic"
        if getattr(self.all_btn, "_icon", None) != icon:
            self._set_icon(self.all_btn, icon, None, 16)
            self.all_btn.set_tooltip_text(_("ld_resume_all") if everything_paused else _("ld_pause_all"))
        self._hold(self.all_btn, bool(self._uids()))

    def _rate(self, uid: str) -> float:
        dl = self.view.downloads.get(uid) or {}
        if dl.get("launcher") == "Steam" and self.steam_state:
            return float(self.steam_state.get("rate") or 0)
        return float(dl.get("rate") or dl.get("speed") or 0)

    def _start_poll(self):
        self.refresh()
        if not self._poll_id:
            self._poll_id = GLib.timeout_add_seconds(STEAM_POLL_S, self._poll)
            self._poll()

    def _stop_poll(self):
        if self._poll_id:
            GLib.source_remove(self._poll_id)
            self._poll_id = 0

    def _poll(self) -> bool:
        if not self.get_mapped() and not (steam_cef.enabled() and self._steam_uids()):
            self._poll_id = 0           # nothing to follow: started again by changed()
            return False
        if steam_cef.enabled() and not getattr(self, "_polling", False):
            self._polling = True
            want_state = bool(self._steam_uids())

            def work():
                port = steam_cef.available()
                state = steam_cef.downloads() if port and want_state else None
                GLib.idle_add(lambda: self._got_steam(state, port) and False)
            threading.Thread(target=work, name="plunderhub-steam-state", daemon=True).start()
        elif not steam_cef.enabled():
            self.steam_state = None
            self._port_open = False
            self.refresh()
        return True

    def _got_steam(self, state, port: bool = True):
        self._polling = False
        self._port_open = port
        self.steam_state = state
        # Steam's own word on each of its installs: paused or not (the bar and
        # the sidebar's foot count from its files, which can't tell)
        for uid in self._steam_uids():
            dl = self.view.downloads.get(uid)
            item = self._steam_item(uid)
            if dl is not None:
                dl["steam_paused"] = bool(item and (item["paused"] or (state or {}).get("paused")))
        if self.get_mapped():
            self.refresh()
        self.app._ld_card.update(self.view)
        return False


class SpeedGraph(Gtk.DrawingArea):
    """The download speed over the last few minutes: a line and the area
    under it, in the accent. A sample a second (from the page's refresh);
    drawn again only when one comes - or under the pointer: a marker and
    that moment's speed and time."""
    SAMPLES = 180

    def __init__(self, app):
        super().__init__()
        from plunderhub.gi_setup import Gdk
        self.app = app
        self.samples: List[float] = []
        self.times: List[float] = []
        self._last = 0.0
        self.hover_x: Optional[float] = None
        self.set_size_request(-1, 86)
        self.add_events(Gdk.EventMask.POINTER_MOTION_MASK | Gdk.EventMask.LEAVE_NOTIFY_MASK)
        self.connect("draw", self._draw)
        self.connect("motion-notify-event", self._motion)
        self.connect("leave-notify-event", lambda *_a: self._set_hover(None))

    def add(self, rate: float):
        now = time.monotonic()
        if now - self._last < 0.9:
            return
        self._last = now
        self.samples = (self.samples + [max(0.0, rate)])[-self.SAMPLES:]
        self.times = (self.times + [time.time()])[-self.SAMPLES:]
        self.queue_draw()

    def _motion(self, _w, ev):
        self._set_hover(ev.x)
        return False

    def _set_hover(self, x):
        if x != self.hover_x:
            self.hover_x = x
            self.queue_draw()             # only the graph: the page isn't laid out again
        return False

    def _values(self) -> List[float]:
        # a short running mean: one odd reading (a manifest's late jump) doesn't spike it
        return [sum(self.samples[max(0, i - 2):i + 1]) / len(self.samples[max(0, i - 2):i + 1])
                for i in range(len(self.samples))]

    def _draw(self, widget, cr):
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        hexs = self.app._home_accent_hex().lstrip("#")
        r, g, b = (int(hexs[i:i + 2], 16) / 255 for i in (0, 2, 4))
        cr.set_source_rgba(r, g, b, 0.18)
        cr.set_line_width(1)
        cr.move_to(0, h - 0.5)
        cr.line_to(w, h - 0.5)
        cr.stroke()
        if len(self.samples) < 2:
            return
        vals = self._values()
        # scaled to the usual speed, not to the one highest reading; above it: clipped
        ordered = sorted(vals)
        top = max(ordered[int(0.95 * (len(ordered) - 1))] * 1.15, 1.0)
        step = w / (self.SAMPLES - 1)
        x0 = w - step * (len(vals) - 1)
        pts = [(x0 + i * step, h - 4 - (h - 12) * min(v, top) / top) for i, v in enumerate(vals)]

        def curve():
            cr.move_to(*pts[0])
            for (xa, ya), (xb, yb) in zip(pts, pts[1:]):
                mid = (xa + xb) / 2
                cr.curve_to(mid, ya, mid, yb, xb, yb)
        curve()
        cr.line_to(pts[-1][0], h)
        cr.line_to(pts[0][0], h)
        cr.close_path()
        cr.set_source_rgba(r, g, b, 0.20)
        cr.fill()
        curve()
        cr.set_source_rgba(r, g, b, 0.95)
        cr.set_line_width(2)
        cr.set_line_join(1)
        cr.stroke()
        if self.hover_x is None or self.hover_x < pts[0][0] - step:
            return
        i = max(0, min(len(pts) - 1, round((self.hover_x - x0) / step)))
        x, y = pts[i]
        cr.set_source_rgba(r, g, b, 0.45)
        cr.set_line_width(1)
        cr.move_to(round(x) + 0.5, 0)
        cr.line_to(round(x) + 0.5, h)
        cr.stroke()
        cr.arc(x, y, 4, 0, 2 * math.pi)
        cr.set_source_rgba(r, g, b, 1)
        cr.fill()
        # the moment's speed and time, in a small box beside the marker
        from gi.repository import PangoCairo
        text = f"{vals[i] / 1e6:.1f} MB/s  ·  {time.strftime('%H:%M:%S', time.localtime(self.times[i]))}"
        layout = PangoCairo.create_layout(cr)
        ctx = widget.get_style_context()
        font = ctx.get_font(ctx.get_state()).copy()
        font.set_size(int(font.get_size() * 0.85))
        font.set_weight(Pango.Weight.BOLD)
        layout.set_font_description(font)
        layout.set_text(text, -1)
        tw, th = (v / Pango.SCALE for v in layout.get_size())
        pad = 6
        bx = x + 10 if x + 10 + tw + 2 * pad < w else x - 10 - tw - 2 * pad
        by = 4
        cr.new_sub_path()
        rad = 6
        cr.arc(bx + tw + 2 * pad - rad, by + rad, rad, -math.pi / 2, 0)
        cr.arc(bx + tw + 2 * pad - rad, by + th + 2 * pad - rad, rad, 0, math.pi / 2)
        cr.arc(bx + rad, by + th + 2 * pad - rad, rad, math.pi / 2, math.pi)
        cr.arc(bx + rad, by + rad, rad, math.pi, 3 * math.pi / 2)
        cr.close_path()
        ok, base = ctx.lookup_color("theme_base_color")
        if ok:
            cr.set_source_rgba(base.red, base.green, base.blue, 0.94)
        else:
            cr.set_source_rgba(0.05, 0.09, 0.10, 0.94)
        cr.fill_preserve()
        cr.set_source_rgba(r, g, b, 0.6)
        cr.set_line_width(1)
        cr.stroke()
        fg = ctx.get_color(ctx.get_state())
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 1)
        cr.move_to(bx + pad, by + pad)
        PangoCairo.show_layout(cr, layout)


class InstallCard(Gtk.Revealer):
    """At the foot of the main window's sidebar, as Heroic shows it: the
    game being dug up, its state, a bar with the percent, and the numbers
    in short (what doesn't fit is cut from the end). A click opens the
    Loading Dock."""

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.set_transition_duration(260)
        button = Gtk.Button()
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.set_can_focus(False)
        button.get_style_context().add_class("ld-side")
        button.connect("clicked", lambda *_a: app._set_home_nav("dock"))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)

        def label(cls, xalign=0.0):
            lab = Gtk.Label(xalign=xalign)
            lab.get_style_context().add_class(cls)
            lab.set_ellipsize(Pango.EllipsizeMode.END)
            lab.set_max_width_chars(1)          # the sidebar's width stays put
            app._apply_tabular_figures(lab)
            return lab
        self.title = label("ld-side-title")
        box.pack_start(self.title, False, False, 0)
        state_line = Gtk.Box(spacing=8)
        self.state = label("ld-side-state")
        self.left = label("ld-side-num", 1.0)
        self.left.set_max_width_chars(-1)
        self.left.set_ellipsize(Pango.EllipsizeMode.NONE)
        state_line.pack_start(self.state, True, True, 0)
        state_line.pack_end(self.left, False, False, 0)
        box.pack_start(state_line, False, False, 0)
        bar_line = Gtk.Box(spacing=8)
        self.bar = Gtk.ProgressBar()
        self.bar.get_style_context().add_class("ld-bar")
        self.bar.set_valign(Gtk.Align.CENTER)
        bar_line.pack_start(self.bar, True, True, 0)
        self.pct = label("ld-side-num", 1.0)
        self.pct.set_max_width_chars(-1)
        self.pct.set_ellipsize(Pango.EllipsizeMode.NONE)
        bar_line.pack_end(self.pct, False, False, 0)
        box.pack_start(bar_line, False, False, 0)
        self.detail = label("ld-side-detail")
        box.pack_start(self.detail, False, False, 0)
        self.more = label("ld-side-detail")
        self.more.set_no_show_all(True)
        box.pack_start(self.more, False, False, 0)
        button.add(box)
        self.add(button)
        self.set_margin_start(12)
        self.set_margin_end(12)
        self.set_margin_bottom(8)
        self.show_all()
        self.set_reveal_child(False)

    def update(self, view):
        _ = self.app._
        uids = [u for u in view.queue if view.downloads.get(u, {}).get("state") != "done"]
        now = next((u for u in uids if view.downloads[u].get("state") in ("downloading", "installing")), None)
        uid = now or (uids[0] if uids else None)
        self.set_reveal_child(uid is not None)
        if uid is None:
            return
        dl = view.downloads[uid]
        game = view.games_by_uid.get(uid) or getattr(view, "faugus_pending", {}).get(uid)
        self.title.set_text(game.title if game else uid)
        state = "paused" if dl.get("steam_paused") else dl.get("state")
        key = {"installing": "ld_state_installing", "downloading": "ld_state_downloading",
               "paused": "ld_state_paused"}.get(state, "ld_state_waiting")
        self.state.set_text(_(key))
        frac = max(0.0, min(1.0, float(dl.get("frac") or 0)))
        self.bar.set_fraction(frac)
        self.pct.set_text(f"{int(frac * 100)} %")
        size = float(dl.get("size") or 0)
        got = float(dl.get("got") or frac * size)
        rate = float(dl.get("rate") or dl.get("speed") or 0)
        left = ""
        if state == "downloading" and rate > 1e5 and size:
            mins, secs = divmod(int(max(0, (size - got) / rate)), 60)
            left = _("ld_left_short").format(m=mins, s=f"{secs:02d}")
        self.left.set_text(left)
        parts = []
        if size:
            parts.append(f"{got / 1e9:.1f}/{size / 1e9:.1f} GB")
        if rate > 1e5 and state == "downloading":
            parts.append(f"{rate / 1e6:.0f} MB/s")
        self.detail.set_text(activity_text(_, dl) or "  ·  ".join(parts))
        # the others waiting behind it, in words
        self.more.set_text(_("ld_card_more").format(n=len(uids) - 1))
        self.more.set_visible(len(uids) > 1)


# --- in the main window: a sidebar row under the Treasure Map's, shown while something installs ---

def attach(app, view):
    page = LoadingDock(view)
    page.set_no_show_all(True)
    page.hide()
    app.main_overlay.add_overlay(page)
    app.main_overlay.reorder_overlay(page, 1)
    app._ld_view, app._ld_active = page, False
    icon = app._icon_image_at_size("anchor-symbolic", 18)
    row = app._home_nav_row("dock", icon, "ld_tab", with_count=True)
    children = app.home_nav_main.get_children()
    after = getattr(app, "_tm_row", None)
    app.home_nav_main.insert(row, children.index(after) + 1 if after in children else -1)
    row.show_all()
    app._ld_row = row
    card = InstallCard(app)
    side = app.home_sidebar
    side.pack_start(card, False, False, 0)
    proton = getattr(app, "_proton_card", None)
    if proton is not None and proton.get_parent() is side:
        side.reorder_child(card, side.get_children().index(proton))
    app._ld_card = card
    return page


def shown(app) -> Optional[LoadingDock]:
    return getattr(app, "_ld_view", None) if getattr(app, "_ld_active", False) else None


def changed(app):
    """The installs changed (or ticked): the sidebar row and card follow,
    the page too while it's shown; nothing left: the row goes."""
    page = getattr(app, "_ld_view", None)
    if page is None:
        return
    view = page.view
    n = len([u for u in view.queue if view.downloads.get(u, {}).get("state") != "done"])
    count = app._home_count_labels.get("dock")
    if count is not None:
        count.set_text(str(n) if n else "")
    if steam_cef.enabled() and page._steam_uids() and not page._poll_id:
        page._start_poll()
    app._ld_card.update(view)
    if app._ld_active:
        page.refresh()
    else:
        page.auto_off()


def show_dock(app, on: bool):
    page = getattr(app, "_ld_view", None)
    if page is None or on == app._ld_active:
        return
    app._ld_active = on
    toolbar = app.view_grid_btn.get_parent()
    kids = toolbar.get_children()
    divider = kids[kids.index(app.view_list_btn) + 1]
    for w in (app.view_grid_btn, app.view_list_btn, divider, app.select_btn, app.reorder_btn, app.sort_btn):
        w.set_visible(not on)
    app.scroller.set_visible(not on)
    if on:
        app._set_scroll_top_visible(False)
        page.set_no_show_all(False)
        page.show_all()
        page.refresh()
    else:
        page.hide()
        app._on_main_scroll_value_changed(app.scroller.get_vadjustment())
    app._update_decorative_animation_pause_state()
    app._home_select_nav_row()
