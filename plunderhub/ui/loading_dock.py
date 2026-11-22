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


def _speed(v) -> str:
    if not v:
        return "–"
    return f"{v / 1e6:.1f} MB/s" if v < 1e8 else f"{v / 1e6:.0f} MB/s"


def _took(seconds) -> str:
    """h:mm:ss"""
    if not seconds:
        return "–"
    s = int(round(seconds))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def _when(at: float) -> str:
    """The time, with the day before it when it wasn't today."""
    t, now = time.localtime(at), time.localtime()
    if t[:3] == now[:3]:
        return time.strftime("%H:%M", t)
    return time.strftime("%Y-%m-%d %H:%M", t)


# hero pictures, decoded once (path -> pixbuf)
_HERO_CACHE: Dict[str, object] = {}


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
        # every install at once - in the first section's header
        self.all_btn = Gtk.Button(label=self._("ld_pause_all"))
        self.all_btn.get_style_context().add_class("bg-icon-btn")
        self.all_btn.set_valign(Gtk.Align.CENTER)
        self.all_btn.connect("clicked", lambda *_a: self._pause_all())
        self.list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.pack_start(self.list, False, False, 0)
        self.rows: Dict[str, Dict] = {}
        self.steam_state: Optional[Dict] = None
        self._poll_id = 0
        self._asking = False
        self._order: list = []
        self._groups: Dict[str, List[str]] = {}
        # the cards' layout for the room there is: 0 wide (all in a row), 1 the
        # graph under the numbers, 2 narrow (numbers 2x2, buttons at the foot);
        # what a wider one needed, measured when it didn't fit
        self._mode = 0
        self._need: Dict[int, int] = {}
        self._mode_pending = False
        self.connect("size-allocate", lambda *_a: self._fit())
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
        return self.view.dock_game(uid)

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
        done = [e for e in self.view.aboard() if e["uid"] not in uids]
        layout = [(k, tuple(groups[k])) for k in ("now", "next", "paused")] + [("done", tuple(e["uid"] for e in done))]
        if layout != self._order:
            self._order = layout
            self._groups = groups
            for child in self.list.get_children():
                self.list.remove(child)
            self.rows.clear()
            # every card the same columns: the buttons, each number tile and the
            # speed right now - lined up page-wide
            H = Gtk.SizeGroupMode.HORIZONTAL
            self._sizes = {k: Gtk.SizeGroup(mode=H) for k in ("right", "tile", "now")}
            if not uids:
                self.list.pack_start(self._empty(), False, False, 0)
            if self.all_btn.get_parent() is not None:
                self.all_btn.get_parent().remove(self.all_btn)
            first = True
            for key, title in (("now", "ld_sec_now"), ("next", "ld_sec_next"), ("paused", "ld_sec_paused")):
                if not groups[key]:
                    continue
                head = Gtk.Box(spacing=10)
                head.set_margin_top(6)
                lab = Gtk.Label(label=self._(title).upper(), xalign=0)
                lab.get_style_context().add_class("gp-card-label")
                head.pack_start(lab, True, True, 0)
                if first:
                    head.pack_end(self.all_btn, False, False, 0)
                    first = False
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

    # fitting the room (1080p at 150 %, laptops at 200 %, big text) ------------------------

    def _room(self) -> int:
        """The width the cards get: what's shown of the page (the page itself
        is laid out at least as wide as its widest card, so its parent's)."""
        parent = self.get_parent()
        width = parent.get_allocated_width() if parent is not None else self.get_allocated_width()
        return width - 20 - 24 - 14          # the page's margins and the scrollbar

    def _fit(self):
        """A layout that fits: a narrower one when the cards are wider than the
        room, back to a wider one once there's room for what it needed."""
        if self._mode_pending or not self.list.get_children():
            return
        room = self._room()
        if room < 50:
            return
        need = self.list.get_preferred_width()[0]
        mode = self._mode
        if need > room and mode < 2:
            self._need[mode] = need
            mode += 1
        elif mode > 0 and room >= self._need.get(mode - 1, 1 << 30) + 8:
            mode -= 1
        if mode == self._mode:
            return
        self._mode = mode
        self._mode_pending = True

        def rebuild():
            self._mode_pending = False
            self._order = []
            self.refresh()
            return False
        GLib.idle_add(rebuild)

    def _empty(self) -> Gtk.Widget:
        """Nothing coming aboard: the quiet dock, and the way to the map."""
        from plunderhub.gi_setup import Gdk
        from plunderhub.icons import get_icon_pixbuf
        _ = self._
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.get_style_context().add_class("gp-card")
        box.get_style_context().add_class("ld-empty")
        scale = self.get_scale_factor() or 1
        pb = get_icon_pixbuf("anchor-symbolic", 56, self.app._home_accent_hex())
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

    def _shell(self, uid: str, hero: Optional[str] = None):
        """A Loading Dock card: the game's hero picture faint behind it
        (darkening to the left, where the words are), the content on top."""
        card = Gtk.Box()
        card.get_style_context().add_class("gp-card")
        card.get_style_context().add_class("ld-card")
        back = HeroBack()
        content = Gtk.Box(spacing=16)
        content.set_margin_start(16)
        content.set_margin_end(16)
        content.set_margin_top(14)
        content.set_margin_bottom(14)
        back.pack_start(content, True, True, 0)
        card.pack_start(back, True, True, 0)
        self._find_hero(uid, hero, back)
        return card, content

    def _find_hero(self, uid: str, hero: Optional[str], back: "HeroBack"):
        """The hero picture: the one the Dig up card showed, the library's,
        Steam's own - read off the main thread."""
        paths = [hero] if hero else []
        got = (self.view.__dict__.get("_card_cache") or {}).get(uid)
        if got and got[0].get("hero"):
            paths.append(str(got[0]["hero"]))
        try:
            lib = self.app.hero_path(uid)
            if lib is not None:
                paths.append(str(lib))
        except Exception:
            pass
        appid = uid.split(":", 1)[1] if uid.startswith("steam:") else None
        own = self.app.game_index.get(uid)
        if getattr(own, "store_platform", None) == "store-launcher" and getattr(self.view, "opts", None):
            # a store launcher in Faugus: PlunderHub's own banner for it, last
            from pathlib import Path
            from plunderhub.core.faugus_add import launcher_kind
            from plunderhub.ui.faugus_aboard import launcher_banner
            kind = launcher_kind(own.exe_path or "")
            banner = launcher_banner(kind, Path(self.view.opts["cache"]), self) if kind else None
            if banner:
                paths.append(banner)
        scale = self.get_scale_factor() or 1
        game = self._game(uid)
        # the name to look Steam's picture up by: the game's, or its Aboard entry's
        title = game.title if game is not None else next(
            (e.get("title") for e in self.view.aboard() if e.get("uid") == uid), None)
        cache_dir = (self.view.opts or {}).get("cache") if hasattr(self.view, "opts") else None

        def work():
            from pathlib import Path
            from plunderhub.gi_setup import GdkPixbuf
            more = list(paths)
            steam_id = appid
            # not a store launcher itself ("Battle.net" would find some game of that name)
            launcher_entry = getattr(self.app.game_index.get(uid), "store_platform", None) == "store-launcher"
            from plunderhub.core.faugus_add import LAUNCHERS
            is_launcher = launcher_entry or (title or "") in {v.get("name") for v in LAUNCHERS.values()}
            by_name = uid.startswith(("storedl:", "faugus:")) and bool(title) and not is_launcher
            if steam_id is None and by_name and not more:
                # a game a store launcher downloads (no card of its own yet, its old art
                # cleared with its uninstall): Steam's picture of the same game, by its name
                try:
                    from plunderhub.core.protondb import steam_appid_by_title
                    steam_id = steam_appid_by_title(title)
                except Exception as e:
                    logger.debug(f"Loading Dock: no Steam id for {title}: {e}")
            if not steam_id and by_name and not more and getattr(self.app, "api_key", None):
                # not on Steam (Wargaming's Steel Hunters...): SteamGridDB's, as its card gets
                try:
                    gid = self.app._steamgrid_search(title, strict=True)
                    page, _n = self.app._steamgrid_page("hero", gid, 0) if gid is not None else ([], 0)
                    stills = [i for i in page if not i.get("animated")]
                    picks = [i for i in stills if i.get("style") == "official"] + stills
                    if picks and cache_dir:
                        from plunderhub.core import treasure
                        h = treasure._download(picks[0]["url"], Path(cache_dir))
                        if h:
                            more.append(str(h))
                            dl = self.view.downloads.get(uid)
                            if dl is not None and not dl.get("hero"):
                                dl["hero"] = str(h)
                except Exception as e:
                    logger.debug(f"Loading Dock: no SteamGridDB hero for {title}: {e}")
            if steam_id:
                from plunderhub.core import launcher_art
                h = launcher_art._steam(steam_id).get("hero")
                if not h and by_name and cache_dir:
                    from plunderhub.core import treasure
                    h = treasure._download("https://shared.akamai.steamstatic.com/store_item_assets/steam/apps/"
                                           f"{steam_id}/library_hero.jpg", Path(cache_dir))
                if h:
                    more.append(str(h))
                    dl = self.view.downloads.get(uid)
                    if dl is not None and not dl.get("hero"):
                        dl["hero"] = str(h)         # kept for its Aboard card
            if game is not None and cache_dir and getattr(game, "store", "") != "faugus":
                # last: the one the Dig up card would show (fetched once, kept in the map's cache)
                try:
                    from plunderhub.core import treasure
                    h = treasure.details(game, Path(cache_dir), slow=False).get("hero")
                    if h:
                        more.append(str(h))
                except Exception as e:
                    logger.debug(f"Loading Dock: no hero for {uid}: {e}")
            for path in more:
                pb = _HERO_CACHE.get(path)
                if pb is None and Path(path).is_file():
                    try:
                        pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 1100 * scale, -1, True)
                    except Exception:
                        pb = None
                    if pb is not None:
                        _HERO_CACHE[path] = pb
                        while len(_HERO_CACHE) > 24:
                            _HERO_CACHE.pop(next(iter(_HERO_CACHE)))
                if pb is not None:
                    GLib.idle_add(lambda p=pb: back.set_picture(p) and False)
                    return
        threading.Thread(target=work, name="plunderhub-dock-hero", daemon=True).start()

    def _launcher_line(self, launcher: str, parts: List[str], launcher_uid: Optional[str] = None) -> Gtk.Widget:
        line = Gtk.Box(spacing=7)
        # a store launcher in Faugus (Battle.net...): its own icon, as its card has
        own = self.app.game_index.get(launcher_uid) if launcher_uid else None
        icon = self.app._create_game_icon_image(own, 16) if own is not None \
            else self.app._load_launcher_icon(launcher, 16)
        icon.set_valign(Gtk.Align.CENTER)
        line.pack_start(icon, False, False, 0)
        lab = Gtk.Label(label="   ·   ".join([launcher] + parts), xalign=0)
        lab.set_ellipsize(Pango.EllipsizeMode.END)
        lab.get_style_context().add_class("gp-card-sub")
        line.pack_start(lab, True, True, 0)
        line.label = lab
        return line

    def _stat(self, value: str, caption: str):
        tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        tile.get_style_context().add_class("ld-stat")
        tile.set_valign(Gtk.Align.CENTER)
        cap = Gtk.Label(label=caption.upper(), xalign=0)
        cap.get_style_context().add_class("ld-stat-cap")
        tile.pack_start(cap, False, False, 0)
        val = Gtk.Label(label=value, xalign=0)
        val.get_style_context().add_class("ld-stat-value")
        self.app._apply_tabular_figures(val)
        tile.pack_start(val, False, False, 0)
        tile.value = val
        return tile

    STAT_KEYS = ["ld_stat_time", "ld_stat_max", "ld_stat_min", "ld_stat_avg"]

    def _stats_row(self, live: bool = False):
        """The numbers (time, max, min, average), the speed graph and how far
        it is with the speed right now: in a row when there's room, the
        graph under the numbers when less, the numbers 2x2 when narrow."""
        mode = self._mode
        row = Gtk.Grid()
        row.set_column_spacing(4)
        row.set_row_spacing(8)
        tiles = {}
        for i, key in enumerate(self.STAT_KEYS):
            tiles[key] = self._stat("–", self._(key))
            self._sizes["tile"].add_widget(tiles[key])
            if mode == 2:
                row.attach(tiles[key], i % 2, i // 2, 1, 1)
            else:
                row.attach(tiles[key], i, 0, 1, 1)
        graph = MiniGraph(self.app, wide=mode == 0)
        graph.set_hexpand(True)
        if mode == 0:
            graph.set_margin_start(10)
            row.attach(graph, 4, 0, 1, 1)
        elif mode == 1:
            row.attach(graph, 0, 1, 5, 1)
        # narrow: no graph - only what matters (it's back when there's room)
        # how far it is, the speed right now under it (the place kept on every
        # card: the graphs end in line)
        side = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        side.set_valign(Gtk.Align.CENTER)
        pct = Gtk.Label(label="", xalign=1)
        pct.get_style_context().add_class("ld-now-pct")
        self.app._apply_tabular_figures(pct)
        side.pack_start(pct, False, False, 0)
        now = Gtk.Label(label="", xalign=1)
        now.get_style_context().add_class("ld-now-rate")
        now.set_width_chars(10)
        self.app._apply_tabular_figures(now)
        side.pack_start(now, False, False, 0)
        self._sizes["now"].add_widget(side)
        if mode == 0:
            row.attach(side, 5, 0, 1, 1)
        else:
            side.set_hexpand(True)
            side.set_halign(Gtk.Align.END)
            row.attach(side, 2 if mode == 2 else 4, 0, 1, 1)
        tiles["now"], tiles["pct"] = now, pct
        return row, tiles, graph

    def _done_row(self, e: Dict) -> Gtk.Widget:
        """A finished install: its cover with the launcher's mark, how long
        it took and how fast it came, its speed graph."""
        _ = self._
        card, content = self._shell(e["uid"], e.get("hero"))
        mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        mid.set_valign(Gtk.Align.CENTER)
        top = Gtk.Box(spacing=10)
        title = Gtk.Label(label=e["title"], xalign=0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.get_style_context().add_class("ld-title")
        top.pack_start(title, True, True, 0)
        pill = Gtk.Label(label="✓  " + _("ld_state_aboard"))
        pill.set_valign(Gtk.Align.CENTER)
        for c in ("bg-pill", "bg-pill-ok"):
            pill.get_style_context().add_class(c)
        drop = self._icon_btn("window-close-symbolic", _("ld_remove"), lambda u=e["uid"]: self._drop_done(u))
        if self._mode == 2:
            top.pack_end(drop, False, False, 0)
        top.pack_end(pill, False, False, 0)
        mid.pack_start(top, False, False, 0)
        mid.pack_start(self._launcher_line(e["launcher"], ([_fmt(e["size"])] if e.get("size") else [])
                                           + [_("ld_done_at").format(time=_when(e["at"]))],
                                           e.get("launcher_uid")), False, False, 0)
        bar = Gtk.ProgressBar()
        bar.get_style_context().add_class("ld-bar")
        bar.set_margin_top(2)
        bar.set_fraction(1.0)
        mid.pack_start(bar, False, False, 0)
        row, tiles, graph = self._stats_row()
        row.set_margin_top(2)
        tiles["ld_stat_time"].value.set_text(_took(e.get("took")))
        for key, field in (("ld_stat_avg", "avg"), ("ld_stat_max", "max"), ("ld_stat_min", "min")):
            tiles[key].value.set_text(_speed(e.get(field)))
        graph.set_samples(e.get("curve") or [], e.get("avg"))
        mid.pack_start(row, False, False, 0)
        content.pack_start(mid, True, True, 0)
        if self._mode < 2:
            right = Gtk.Box()
            right.set_valign(Gtk.Align.CENTER)
            right.pack_end(drop, False, False, 0)
            self._sizes["right"].add_widget(right)
            content.pack_end(right, False, False, 0)
        return card

    def _drop_done(self, uid: str):
        """One finished install out of the Aboard list."""
        log = self.view.aboard()
        log[:] = [e for e in log if e["uid"] != uid]
        self.view.save_aboard()
        self.refresh()

    def _clear_done(self):
        self.view.aboard().clear()
        self.view.save_aboard()
        self.refresh()

    def _set_icon(self, button: Gtk.Button, icon: str, colour: Optional[str], px: int):
        from plunderhub.gi_setup import Gdk
        from plunderhub.icons import get_icon_pixbuf
        scale = self.get_scale_factor() or 1
        pb = get_icon_pixbuf(icon, px, colour or self.app._home_accent_hex())
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
        card, content = self._shell(uid, dl.get("hero"))
        mid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        mid.set_valign(Gtk.Align.CENTER)
        line = Gtk.Box(spacing=10)
        # its place in the line, before the name
        num = Gtk.Label(label=f"#{index + 1}")
        num.get_style_context().add_class("ld-num")
        num.set_valign(Gtk.Align.CENTER)
        num.set_no_show_all(kind != "next")
        line.pack_start(num, False, False, 0)
        title = Gtk.Label(label=game.title if game else uid, xalign=0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.get_style_context().add_class("ld-title")
        line.pack_start(title, True, True, 0)
        pill = Gtk.Label()
        pill.get_style_context().add_class("bg-pill")
        pill.set_valign(Gtk.Align.CENTER)
        line.pack_end(pill, False, False, 0)
        mid.pack_start(line, False, False, 0)
        launcher = self._launcher_line(dl.get("launcher") or "Steam", [], dl.get("launcher_uid"))
        mid.pack_start(launcher, False, False, 0)
        bar = Gtk.ProgressBar()
        bar.get_style_context().add_class("ld-bar")
        bar.set_margin_top(2)
        mid.pack_start(bar, False, False, 0)
        stats, tiles, graph = self._stats_row(live=True)
        stats.set_margin_top(2)
        mid.pack_start(stats, False, False, 0)
        content.pack_start(mid, True, True, 0)
        buttons = Gtk.Box(spacing=8)
        buttons.set_valign(Gtk.Align.CENTER)
        # moving it in the line: a small pair before the other buttons
        narrow = self._mode == 2
        arrows = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL if narrow else Gtk.Orientation.VERTICAL,
                         spacing=8 if narrow else 6)
        arrows.set_valign(Gtk.Align.CENTER)
        up = self._icon_btn("go-up-symbolic", _("ld_up"), lambda: self._move(uid, -1))
        down = self._icon_btn("go-down-symbolic", _("ld_down"), lambda: self._move(uid, 1))
        arrows.pack_start(up, False, False, 0)
        arrows.pack_start(down, False, False, 0)
        buttons.pack_start(arrows, False, False, 0)
        pause = self._icon_btn("pause-symbolic", _("ld_pause"), lambda: self._pause(uid))
        stop = self._icon_btn("media-playback-stop-symbolic", _("tm_stop"), lambda: self._stop(uid), colour="#e05252")
        steam = self._icon_btn("external-link-symbolic", _("ld_open_steam"),
                               lambda: self.view._steam_quietly("steam://open/downloads"))
        for b in (pause, stop, steam):
            buttons.pack_start(b, False, False, 0)
        if narrow:
            # under how far it is, beside the numbers' second row
            buttons.set_halign(Gtk.Align.END)
            stats.attach(buttons, 2, 1, 1, 1)
        else:
            self._sizes["right"].add_widget(buttons)
            content.pack_end(buttons, False, False, 0)
        self.rows[uid] = {"pill": pill, "bar": bar, "detail": launcher.label, "up": up, "down": down,
                          "pause": pause, "stop": stop, "steam": steam, "kind": kind, "arrows": arrows,
                          "tiles": tiles, "graph": graph, "stats": stats, "launcher": dl.get("launcher") or "Steam"}
        return card

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
        parts = []
        size = dl.get("size") or 0
        if size:
            parts.append(_("tm_of").format(done=_fmt(dl.get("got") or frac * size), total=_fmt(size)))
        rate = dl.get("rate") or dl.get("speed") or 0
        if rate > 1e5 and not paused and key == "ld_state_downloading":
            left = max(0, (size - (dl.get("got") or frac * size)) / rate) if size else 0
            mins, secs = divmod(int(left), 60)
            parts += [_("tm_left").format(m=mins, s=f"{secs:02d}")] if left else []
        act = activity_text(_, dl)
        r["detail"].set_text("   ·   ".join([r["launcher"], act] if act else [r["launcher"]] + parts))
        samples = dl.get("samples") or []
        tiles = r["tiles"]
        # a launcher that keeps its download's size to itself (Wargaming, Amazon): no made-up 0 %
        unknown = bool(dl.get("store_dl")) and not dl.get("size") and state != "done"
        tiles["pct"].set_text("–" if unknown else f"{int(frac * 100)} %")
        began = dl.get("began")
        tiles["ld_stat_time"].value.set_text(_took(time.time() - began) if began else "–")
        tiles["now"].set_text(_speed(rate) if rate > 1e5 and not paused and key == "ld_state_downloading" else "")
        from plunderhub.ui.treasure_map import rate_stats
        st = rate_stats(samples) if samples else {}
        for k, field in (("ld_stat_avg", "avg"), ("ld_stat_max", "max"), ("ld_stat_min", "min")):
            tiles[k].value.set_text(_speed(st.get(field)))
        if len(samples) != r["graph"].count:
            r["graph"].set_samples(st.get("curve") or [], st.get("avg"), count=len(samples))
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
        # the Pause all / Resume all button
        everything_paused = bool(self._uids()) and not self._groups.get("now") and not self._groups.get("next")
        self.all_btn.set_label(_("ld_resume_all") if everything_paused else _("ld_pause_all"))

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


class HeroBack(Gtk.Box):
    """A card's background: the game's hero picture, faint, filling it
    (cropped), under a wash of the card's own colour that's thick on the
    left where the words sit. Drawn once per size into a surface."""
    RADIUS = 13

    def __init__(self):
        super().__init__()
        self.picture = None
        self._surface = None
        self._for = None
        self.connect("draw", self._draw)

    def set_picture(self, pb):
        self.picture = pb
        self._surface = None
        self.queue_draw()

    def _draw(self, widget, cr):
        if self.picture is None:
            return False
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        if w < 2 or h < 2:
            return False
        scale = widget.get_scale_factor() or 1
        if self._surface is None or self._for != (w, h, scale):
            self._surface = self._paint(widget, w, h, scale)
            self._for = (w, h, scale)
        cr.set_source_surface(self._surface, 0, 0)
        cr.paint()
        return False

    def _paint(self, widget, w, h, scale):
        import cairo
        from plunderhub.gi_setup import Gdk
        surf = widget.get_window().create_similar_image_surface(cairo.FORMAT_ARGB32, w * scale, h * scale, scale) \
            if widget.get_window() is not None else cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
        cr = cairo.Context(surf)
        r = self.RADIUS
        cr.new_sub_path()
        cr.arc(w - r, r, r, -math.pi / 2, 0)
        cr.arc(w - r, h - r, r, 0, math.pi / 2)
        cr.arc(r, h - r, r, math.pi / 2, math.pi)
        cr.arc(r, r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()
        cr.clip()
        pb = self.picture
        pw, ph = pb.get_width() / scale, pb.get_height() / scale
        k = max(w / pw, h / ph)
        cr.save()
        # filling the card's width, the picture's upper middle shown (faces, titles)
        cr.translate((w - pw * k) / 2, (h - ph * k) * 0.35)
        cr.scale(k / scale, k / scale)
        Gdk.cairo_set_source_pixbuf(cr, pb, 0, 0)
        cr.paint_with_alpha(0.5)
        cr.restore()
        ctx = widget.get_parent().get_style_context()
        bg = ctx.get_property("background-color", ctx.get_state())
        grad = cairo.LinearGradient(0, 0, w, 0)
        grad.add_color_stop_rgba(0.0, bg.red, bg.green, bg.blue, 0.97)
        grad.add_color_stop_rgba(0.45, bg.red, bg.green, bg.blue, 0.86)
        grad.add_color_stop_rgba(0.8, bg.red, bg.green, bg.blue, 0.66)
        grad.add_color_stop_rgba(1.0, bg.red, bg.green, bg.blue, 0.8)
        cr.set_source(grad)
        cr.paint()
        return surf


class MiniGraph(Gtk.DrawingArea):
    """An install's speed over its whole run, small: the area and line in
    the accent, its average as a dashed line."""

    def __init__(self, app, wide: bool = True):
        super().__init__()
        self.app = app
        self.values: List[float] = []
        self.avg = None
        self.count = -1
        # beside the numbers: as tall as they are; under them: a flatter strip
        self.set_size_request(140 if wide else 60, 50 if wide else 34)
        self.set_valign(Gtk.Align.CENTER)
        self.connect("draw", self._draw)

    def set_samples(self, values, avg=None, count=None):
        self.values = [float(v) for v in values or []]
        self.avg = avg
        self.count = len(self.values) if count is None else count
        self.queue_draw()

    def _draw(self, widget, cr):
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        hexs = self.app._home_accent_hex().lstrip("#")
        r, g, b = (int(hexs[i:i + 2], 16) / 255 for i in (0, 2, 4))
        cr.set_source_rgba(r, g, b, 0.22)
        cr.set_line_width(1)
        cr.move_to(0, h - 0.5)
        cr.line_to(w, h - 0.5)
        cr.stroke()
        vals = self.values
        if len(vals) < 2:
            return False
        top = max(max(vals), 1.0) * 1.1
        step = w / (len(vals) - 1)
        pts = [(i * step, h - 2 - (h - 6) * v / top) for i, v in enumerate(vals)]

        def curve():
            cr.move_to(*pts[0])
            for (xa, ya), (xb, yb) in zip(pts, pts[1:]):
                mid = (xa + xb) / 2
                cr.curve_to(mid, ya, mid, yb, xb, yb)
        curve()
        cr.line_to(pts[-1][0], h)
        cr.line_to(0, h)
        cr.close_path()
        cr.set_source_rgba(r, g, b, 0.22)
        cr.fill()
        curve()
        cr.set_source_rgba(r, g, b, 0.95)
        cr.set_line_width(1.6)
        cr.stroke()
        if self.avg:
            y = round(h - 2 - (h - 6) * self.avg / top) + 0.5
            cr.set_dash([3, 3])
            cr.set_source_rgba(r, g, b, 0.55)
            cr.set_line_width(1)
            cr.move_to(0, y)
            cr.line_to(w, y)
            cr.stroke()
        return False


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

    def set_compact(self, on: bool):
        """A short sidebar: the title, state and bar only."""
        self._compact = on
        self.detail.set_visible(not on)
        if on:
            self.more.set_visible(False)

    def update(self, view):
        _ = self.app._
        uids = [u for u in view.queue if view.downloads.get(u, {}).get("state") != "done"]
        now = next((u for u in uids if view.downloads[u].get("state") in ("downloading", "installing")), None)
        uid = now or (uids[0] if uids else None)
        self.set_reveal_child(uid is not None)
        if uid is None:
            return
        dl = view.downloads[uid]
        game = view.dock_game(uid)
        self.title.set_text(game.title if game else uid)
        state = "paused" if dl.get("steam_paused") else dl.get("state")
        key = {"installing": "ld_state_installing", "downloading": "ld_state_downloading",
               "paused": "ld_state_paused"}.get(state, "ld_state_waiting")
        self.state.set_text(_(key))
        frac = max(0.0, min(1.0, float(dl.get("frac") or 0)))
        self.bar.set_fraction(frac)
        unknown = bool(dl.get("store_dl")) and not dl.get("size")
        self.pct.set_text("–" if unknown else f"{int(frac * 100)} %")
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
        self.more.set_visible(len(uids) > 1 and not getattr(self, "_compact", False))


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
    app._sync_topbar_tools()
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
