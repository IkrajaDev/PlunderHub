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

"""Shipwright's Fleet orders: launch options for many games at once.

The page is a game's own Launch options tab (game_page.LaunchOptionsTab),
starting blank: what the player touches there - and only that - is the
order (a touched row is marked, ↺ takes it back). "Apply to all games"
or "Apply to some games..." (picked in a list) shows game by game what
changes and what's already so, saves through the launchers' own backends
(Steam closed once first), and Undo puts back what each game had.
Proton isn't offered: every launcher has a list of its own."""

import copy
from typing import Dict, List, Optional, Set

from plunderhub.gi_setup import GLib, Gtk, Pango
from plunderhub.core import launch_options as lo
from plunderhub.logging_setup import logger


def _attr(name):
    return (lambda o: getattr(o, name)), (lambda o, v: setattr(o, name, v))


def _gs(name):
    return (lambda o: getattr(o.gamescope, name)), (lambda o, v: setattr(o.gamescope, name, v))


def _gs_pair(a, b):
    return ((lambda o: (getattr(o.gamescope, a), getattr(o.gamescope, b))),
            (lambda o, v: (setattr(o.gamescope, a, v[0]), setattr(o.gamescope, b, v[1]))))


# field -> (the ui key its row is found by, getter, setter) on LaunchOptions
FIELDS = {
    "native": ("native",) + _attr("native"),
    "proton": ("proton",) + _attr("proton"),
    "gamemode": ("gamemode",) + _attr("gamemode"),
    "mangohud": ("mangohud",) + _attr("mangohud"),
    "vkbasalt": ("vkbasalt",) + _attr("vkbasalt"),
    "fps_limit": ("fps_check",) + _attr("fps_limit"),
    "esync": ("esync",) + _attr("esync"),
    "fsync": ("fsync",) + _attr("fsync"),
    "wayland": ("wayland",) + _attr("wayland"),
    "hdr": ("hdr",) + _attr("hdr"),
    "fsr": ("fsr",) + _attr("fsr"),
    "nvapi": ("nvapi",) + _attr("nvapi"),
    "proton_log": ("proton_log",) + _attr("proton_log"),
    "battleye": ("battleye",) + _attr("battleye"),
    "eac": ("eac",) + _attr("eac"),
    "args": ("args",) + _attr("args"),
    "gs_enabled": ("gs_enabled",) + _gs("enabled"),
    "gs_game": ("gs_game_w",) + _gs_pair("game_width", "game_height"),
    "gs_out": ("gs_out_w",) + _gs_pair("output_width", "output_height"),
    "gs_upscale": ("gs_upscale",) + _gs("upscale_method"),
    "gs_window": ("gs_window",) + _gs("window_type"),
    "gs_fps": ("gs_fps",) + _gs("fps_limit"),
    "gs_fps_unfocused": ("gs_fps_unfocused",) + _gs("fps_limit_unfocused"),
    "gs_grab": ("gs_grab",) + _gs("force_grab_cursor"),
    "gs_hdr": ("gs_hdr",) + _gs("hdr"),
    "gs_additional": ("gs_additional",) + _gs("additional_options"),
    "env": ("env",) + ((lambda o: dict(o.env)), (lambda o, v: setattr(o, "env", dict(v)))),
}
# what a game's list row shows it has on
BADGES = (("gamemode", "GameMode"), ("mangohud", "MangoHud"), ("hdr", "HDR"), ("gs_enabled", "gamescope"),
          ("vkbasalt", "vkBasalt"))


class _PageStandIn:
    """What the tab asks of the game page it usually sits in."""

    def __init__(self, window):
        self.window = window

    def _proton_switched(self, *_a):
        pass

    def __getattr__(self, name):
        return getattr(self.window, name)


def _ancestor(widget, css):
    w = widget
    while w is not None:
        if w.get_style_context().has_class(css):
            return w
        w = w.get_parent()
    return None


def _title_box(row):
    """The box with a setting row's title, or a card's head."""
    kids = row.get_children()
    if not kids:
        return None
    first = kids[0]
    if row.get_style_context().has_class("gp-setting"):
        inner = first.get_children() if isinstance(first, Gtk.Container) else []
        return inner[0] if inner and isinstance(inner[0], Gtk.Box) else None
    return first if isinstance(first, Gtk.Box) else None


def _relabel(btn, text):
    def walk(w):
        if isinstance(w, Gtk.Label):
            w.set_text(text)
            return True
        return isinstance(w, Gtk.Container) and any(walk(c) for c in w.get_children())
    walk(btn)


class FleetOrdersPage(Gtk.Box):
    def __init__(self, sw):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.sw, self.app = sw, sw.app
        self._ = sw.app._
        self.set_margin_start(28)
        self.set_margin_end(28)
        self.set_margin_bottom(18)
        self.games = []
        self.opts: Dict[str, lo.LaunchOptions] = {}
        for g in self.app._home_all_visible_games():
            backend = lo.backend_for(g)
            if backend is None or g.store_platform == "store-launcher":
                continue
            try:
                self.opts[g.uid] = backend.load(g)
            except Exception as e:
                logger.debug(f"Fleet orders: {g.title!r}'s launch options not read: {e}")
                continue
            self.games.append(g)
        self.games.sort(key=lambda g: g.title.lower())
        self.busy = False
        self._queued = False
        self.undo: Optional[Dict[str, lo.LaunchOptions]] = None
        self.touched: Set[str] = set()      # the order: the fields the player changed
        self._written: Set[str] = set()     # filled in by the form itself, not yet looked at
        self.shown: Dict[str, object] = {}
        self.available: Set[str] = set()
        self.rows: Dict[str, Gtk.Widget] = {}
        self.tab = None
        if not self.games:
            empty = Gtk.Label(label=self._("fo_no_games"), xalign=0)
            empty.get_style_context().add_class("settings-row-sub")
            self.pack_start(empty, False, False, 0)
            return
        # the form is one game's tab: the one whose launcher can store the most
        # (a Steam game's has no anti-cheat switches - Steam does those itself)
        self.template = min(self.games, key=lambda g: (len(lo.backend_for(g).unsupported(g)),
                                                       g.launcher == "Steam", g.title.lower()))
        self.scroller = self._tab_panel()
        self.pack_start(self.scroller, True, True, 0)
        self.pack_start(self._bar(), False, False, 0)
        self._blank()

    # --- the form ---------------------------------------------------------------------------
    def _tab_panel(self) -> Gtk.Widget:
        from plunderhub.ui import game_page
        g = self.template
        tab = game_page.LaunchOptionsTab(self.app, g, lo.backend_for(g), _PageStandIn(self.sw.dialog))
        self.tab = tab
        tab.page = self.sw.dialog       # what its own dialogs (Recommended) open over
        tab.set_border_width(4)
        for child in list(tab.get_children()):
            if child.get_style_context().has_class("gp-notice"):
                tab.remove(child)       # about that one game
        for name in ("prefix", "text"):
            if name in tab.chip_buttons:     # one game's own
                tab.chip_buttons[name].set_no_show_all(True)
                tab.chip_buttons[name].hide()
        hide = [getattr(tab, "hint_strip", None), tab.ui.get("fleet_card")]
        if "target_exe" in tab.ui:
            hide.append(_ancestor(tab.ui["target_exe"], "gp-card"))
        for w in hide:
            if w is not None:
                w.set_no_show_all(True)
                w.hide()
        self._fill_proton_union()
        for field, (key, _g, _s) in FIELDS.items():
            widget = getattr(tab, "env_rows", None) if key == "env" else tab.ui.get(key)
            if widget is None or not widget.get_sensitive():
                continue
            row = _ancestor(widget, "gp-setting") or _ancestor(widget, "gp-card")
            if row is None and field == "args" and tab.ui.get("win_fullscreen") is not None:
                # the arguments' own field is only their store: their card is the window-mode switches'
                row = _ancestor(tab.ui["win_fullscreen"], "gp-card")
            top = _title_box(row) if row is not None else None
            if top is None:
                continue
            self.rows[field] = row
        self.available = set(self.rows)
        self._watch(tab.ui)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_overlay_scrolling(False)
        scroller.get_style_context().add_class("settings-scroll")
        scroller.add(tab)
        return scroller

    # --- Proton: one list for every launcher, by the build's name ---------------------------
    @staticmethod
    def _proton_name(choice_id: str, label: str) -> str:
        """What a Proton build is called whatever the launcher (Heroic stores its
        path, Steam its name): its folder's name, else its label."""
        import os
        raw = os.path.basename(os.path.dirname(choice_id)) if "/" in choice_id else (label or choice_id)
        return raw

    @staticmethod
    def _proton_key(name: str) -> str:
        return "".join(ch for ch in name.lower() if ch.isalnum())

    def _fill_proton_union(self):
        """The tab's Proton list: every build some game's launcher knows,
        once each by its name, the launchers' own default first."""
        combo = self.tab.ui.get("proton")
        if combo is None:
            return
        self.proton_names: Dict[str, str] = {}      # key -> name shown
        self.proton_ids: Dict[str, Dict[str, str]] = {}   # uid -> {key: that launcher's id}
        for g in self.games:
            backend = lo.backend_for(g)
            try:
                choices = backend.proton_choices(g, self.opts[g.uid].proton)
            except Exception:
                choices = []
            ids: Dict[str, str] = {}
            for c in choices:
                name = self._proton_name(c.id, c.label)
                key = self._proton_key(name)
                if not key:
                    continue
                ids.setdefault(key, c.id)       # the launcher's own folder first (Heroic's tools)
                self.proton_names.setdefault(key, name)
            self.proton_ids[g.uid] = ids
        box = combo.get_parent()
        for other in (box.get_children() if box is not None else []):
            if other is not combo:
                # Proton versions: another Shipwright over this one, and the list back to one
                # launcher's after it - the Proton page is a click away in this sidebar
                other.set_no_show_all(True)
                other.hide()
        combo.remove_all()
        combo.append("", self._("lo_proton_default"))
        for key, name in sorted(self.proton_names.items(), key=lambda kv: kv[1].lower()):
            combo.append("fo:" + key, name)
        combo.set_active_id("")
        combo.set_sensitive(True)
        self.tab._proton_alias = None

    def _proton_for(self, g, ordered: str):
        """The game's own id for the ordered build ("" = the launcher's
        default -> None); False when its launcher doesn't have that build."""
        if not ordered:
            return None
        return self.proton_ids.get(g.uid, {}).get(ordered[3:], False)

    # widgets beside a field's own one that set it
    _ALSO = {"fps_entry": "fps_limit", "gs_game_h": "gs_game", "gs_out_h": "gs_out", "env_store": "env",
             "win_fullscreen": "args", "win_borderless": "args", "launcher_skip": "args"}

    def _watch(self, ui):
        field_of = {key: field for field, (key, _g, _s) in FIELDS.items()}
        field_of.update(self._ALSO)
        for key, w in ui.items():
            field = field_of.get(key)
            if isinstance(w, Gtk.Switch):
                w.connect("notify::active", self._changed, field)
            elif isinstance(w, Gtk.ToggleButton):
                w.connect("toggled", self._changed, field)
            elif isinstance(w, (Gtk.Entry, Gtk.ComboBox)):
                w.connect("changed", self._changed, field)
            elif isinstance(w, Gtk.ListStore):
                for sig in ("row-changed", "row-inserted", "row-deleted"):
                    w.connect(sig, lambda *a, f=field: self._changed(f))

    def _changed(self, *args):
        field = args[-1] if args and (args[-1] is None or isinstance(args[-1], str)) else None
        if self.busy:
            return
        if field is not None:
            if getattr(self.app, "_lo_writing", False):
                # the form filled at once (Recommended): ordered only where it differs
                self._written.add(field)
            else:
                self.touched.add(field)
        if not self._queued:
            self._queued = True
            GLib.idle_add(self._recompute)

    def _now(self) -> lo.LaunchOptions:
        return self.app._lo_read_widgets(self.tab.ui, self.opts[self.template.uid])

    def _write(self, opts: lo.LaunchOptions):
        self.busy = True
        try:
            self.app._lo_write_widgets(self.tab.ui, opts)
            self.tab._rebuild_env_rows()
            self.tab.ui["gs_grid"].set_sensitive(True)      # its options can be ordered on their own
        finally:
            self.busy = False

    @staticmethod
    def _blank_options() -> lo.LaunchOptions:
        opts = lo.LaunchOptions(esync=False, fsync=False)
        opts.wrappers = []
        return opts

    def _blank(self):
        """Everything off and empty: nothing ordered."""
        self.touched.clear()
        self.clearing = False
        self._write(self._blank_options())
        now = self._now()
        self.shown = {f: FIELDS[f][1](now) for f in FIELDS}
        self._recompute()

    # what "Reset all options" leaves alone: which Proton, native or not - how it runs,
    # not an option of it
    _NOT_CLEARED = ("proton", "native")

    def _changed_fields(self, now=None) -> List[str]:
        """The order: what differs from the blank form (switched on and back
        off is no order); after "Reset all options", every option."""
        if getattr(self, "clearing", False):
            return [f for f in FIELDS if f in self.available and (f not in self._NOT_CLEARED or f in self.touched)]
        now = now if now is not None else self._now()
        return [f for f in FIELDS if f in self.available and f in self.touched
                and FIELDS[f][1](now) != self.shown.get(f)]

    def _clear_all(self):
        """Every option back to the launchers' defaults (esync / fsync on,
        everything else off and empty; Proton and native as they are) - the
        order then goes out with Apply like any other."""
        self.touched.clear()
        self._write(lo.LaunchOptions())
        self.clearing = True
        self._recompute()

    def _recompute(self) -> bool:
        self._queued = False
        if self.tab is None:
            return False
        if self._written:
            now = self._now()
            self.touched.update(f for f in self._written if FIELDS[f][1](now) != self.shown.get(f))
            self._written.clear()
        self._refresh()
        return False

    def _reset(self):
        self._blank()

    # --- the bar ----------------------------------------------------------------------------
    def _bar(self) -> Gtk.Widget:
        _ = self._
        bar = Gtk.Box(spacing=10)
        bar.get_style_context().add_class("fo-bar")
        self.summary = Gtk.Label(xalign=0)
        self.summary.set_line_wrap(True)
        self.summary.set_lines(3)
        self.summary.set_ellipsize(Pango.EllipsizeMode.END)
        self.summary.get_style_context().add_class("fo-strong")
        bar.pack_start(self.summary, True, True, 0)
        self.undo_btn = self.app._dialog_button(_("fo_undo"), "ghost")
        self.undo_btn.connect("clicked", lambda *_a: self._undo())
        self.clear_btn = self.app._dialog_button(_("fo_clear_all"), "ghost")
        self.clear_btn.set_tooltip_text(_("fo_clear_all_tip"))
        self.clear_btn.connect("clicked", lambda *_a: self._clear_all())
        self.reset_btn = self.app._dialog_button(_("fo_reset"), "ghost")
        self.reset_btn.connect("clicked", lambda *_a: self._reset())
        self.some_btn = self.app._dialog_button(_("fo_apply_some"), "ghost")
        self.some_btn.connect("clicked", lambda *_a: self._apply_some())
        self.all_btn = self.app._dialog_button(_("fo_apply_all"), "accent")
        self.all_btn.connect("clicked", lambda *_a: self._apply_to(list(self.games)))
        for b in (self.undo_btn, self.clear_btn, self.reset_btn, self.some_btn, self.all_btn):
            b.set_valign(Gtk.Align.CENTER)      # beside a summary of two or three lines
            b.get_child().show_all()
            b.set_focus_on_click(False)
            bar.pack_start(b, False, False, 0)
        self.undo_btn.set_no_show_all(True)
        return bar

    def _field_title(self, field: str) -> str:
        row = self.rows.get(field)
        top = _title_box(row) if row is not None else None
        for w in (top.get_children() if top is not None else []):
            if isinstance(w, Gtk.Label) and not w.get_style_context().has_class("settings-pill"):
                text = w.get_text()
                # a card's head is shown in capitals (ENVIRONMENT VARIABLES); a row's title as it is (HDR)
                return text.capitalize() if w.get_style_context().has_class("gp-card-label") else text
        return field

    def _value_text(self, field: str, value) -> str:
        _ = self._
        if field == "env":
            return ", ".join(f"{k}={v}" for k, v in value.items()) or "–"
        if isinstance(value, bool):
            return _("fo_on") if value else _("fo_off")
        if isinstance(value, tuple):
            return "×".join(v for v in value if v) or "–"
        if field == "proton":
            combo = self.tab.ui.get("proton")
            return (combo.get_active_text() if combo is not None else None) or "–"
        return str(value) if value else "–"

    def _order_text(self) -> List[str]:
        now = self._now()
        if getattr(self, "clearing", False):
            # all options to their defaults, and what was set over that
            base = lo.LaunchOptions()
            extra = [f"{self._field_title(f)}: {self._value_text(f, FIELDS[f][1](now))}"
                     for f in self._changed_fields(now) if FIELDS[f][1](now) != FIELDS[f][1](base)]
            return [self._("fo_clearing")] + extra
        return [f"{self._field_title(f)}: {self._value_text(f, FIELDS[f][1](now))}" for f in self._changed_fields(now)]

    def _refresh(self):
        parts = self._order_text()
        if parts:
            self.summary.set_text(", ".join(parts))
        elif not self.undo_btn.get_visible():
            self.summary.set_text(self._("fo_nothing"))
        for b in (self.all_btn, self.some_btn):
            b.set_sensitive(bool(parts))
        self.reset_btn.set_sensitive(bool(parts))
        self.clear_btn.set_sensitive(not getattr(self, "clearing", False))

    # --- applying -----------------------------------------------------------------------------
    @staticmethod
    def _env_after(wanted: Dict[str, str], current: Dict[str, str]) -> Dict[str, str]:
        """A game's variables with the ordered ones set - its others kept."""
        out = dict(current)
        out.update(wanted)
        return out

    def _plan(self, g, now: lo.LaunchOptions, current: lo.LaunchOptions):
        """(the game's new options, [(field, "change" | "same" | "cant")])."""
        backend = lo.backend_for(g)
        unsupported = backend.unsupported(g) if backend is not None else frozenset()
        new = copy.deepcopy(current)
        verdicts = []
        for field in self._changed_fields():
            key, get, put = FIELDS[field]
            value = get(now)
            if key in unsupported or (field == "native" and not backend.native_capable(g)) \
                    or (field in ("battleye", "eac") and backend.anticheat_mode != "toggles"):
                if not getattr(self, "clearing", False):      # resetting: nothing there to reset
                    verdicts.append((field, "cant"))
                continue
            if field == "proton":
                if new.native and backend.native_capable(g):
                    verdicts.append((field, "native"))      # runs its Linux version: no Proton set
                    continue
                value = self._proton_for(g, value)
                if value is False:
                    verdicts.append((field, "cant"))
                    continue
            if field == "env" and not getattr(self, "clearing", False):
                value = self._env_after(value, get(current))       # set these, the game's others kept
            if get(current) == value:
                verdicts.append((field, "same"))
            else:
                put(new, value)
                verdicts.append((field, "change"))
        return new, verdicts

    def _apply_some(self):
        games = self._pick_games()
        if games:
            self._apply_to(games)

    def _apply_to(self, games):
        _ = self._
        now = self._now()
        plans, before, verdicts = {}, {}, {}
        for g in games:
            current = self.opts[g.uid]
            new, v = self._plan(g, now, current)
            verdicts[g.uid] = v
            if any(x == "change" for _f, x in v):
                plans[g.uid] = new
                before[g.uid] = copy.deepcopy(current)
        if not plans:
            self.app._show_info_dialog(_("fo_nothing_to_save"), transient_for=self.sw.dialog)
            return
        if not self._confirm(games, verdicts, now, len(plans)):
            return
        if not self._steam_ok([g for g in games if g.uid in plans]):
            return
        failed = self._save_all(plans)
        self.undo = {uid: opts for uid, opts in before.items() if uid not in failed}
        self.undo_btn.set_visible(bool(self.undo))
        self._blank()
        self.summary.set_text(_("fo_applied").format(n=len(plans) - len(failed)))
        if failed:
            self.app._show_info_dialog(f"{_('lo_save_failed')}:\n" + "\n".join(failed.values()),
                                       message_type=Gtk.MessageType.ERROR, transient_for=self.sw.dialog)

    def _confirm(self, games, verdicts, now, n: int) -> bool:
        """Game by game: what changes (with its new value), what's already so,
        what its launcher can't store - OK'd here."""
        _ = self._
        app = self.app
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        changing = [g for g in games if any(x == "change" for _f, x in verdicts[g.uid])]
        if any(g.launcher == "Steam" for g in changing) and lo.SteamBackend().blocking_process_running():
            box.pack_start(self._note(_("fo_steam_closed"), "fo-cant"), False, False, 0)
        for g in changing:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            card.get_style_context().add_class("fo-confirm-card")
            head = Gtk.Box(spacing=10)
            head.pack_start(self._game_image(g, 28), False, False, 0)
            title = Gtk.Label(label=g.title, xalign=0)
            title.set_ellipsize(Pango.EllipsizeMode.END)
            title.get_style_context().add_class("settings-row-title")
            head.pack_start(title, False, False, 0)
            launcher = Gtk.Label(label=g.launcher)
            launcher.get_style_context().add_class("fo-launcher")
            head.pack_start(launcher, False, False, 0)
            if g.uid in app.running_procs or g.uid in app.detected_running:
                head.pack_start(self._note(_("fo_running"), "fo-cant"), False, False, 0)
            card.pack_start(head, False, False, 0)
            groups = {"change": [], "same": [], "cant": [], "native": []}
            for field, verdict in verdicts[g.uid]:
                groups[verdict].append(field)
            if groups["native"]:
                card.pack_start(self._note(_("fo_native_line"), "fo-same"), False, False, 0)
            for verdict, key, css in (("change", "fo_changes_line", "fo-changed"),
                                      ("same", "fo_already_line", "fo-same"),
                                      ("cant", "fo_cant_line", "fo-cant")):
                fields = groups[verdict]
                if not fields:
                    continue
                items = [f"{self._field_title(f)} {self._value_text(f, FIELDS[f][1](now))}" if verdict == "change"
                         else self._field_title(f) for f in fields]
                card.pack_start(self._note(_(key).format(list=", ".join(items)), css), False, False, 0)
            box.pack_start(card, False, False, 0)
        rest = len(games) - len(changing)
        if rest:
            box.pack_start(self._note(_("fo_unchanged_games").format(n=rest), "fo-same"), False, False, 0)
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_overlay_scrolling(False)
        scroller.get_style_context().add_class("settings-scroll")
        scroller.set_min_content_width(560)
        scroller.add(box)
        box.show_all()
        natural = box.get_preferred_height()[1]
        scroller.set_min_content_height(min(natural, max(240, self.sw.dialog.get_allocated_height() - 260)))
        response = app._card_dialog(_("fo_confirm_title").format(n=n), None,
                                    [(_("dialog_cancel"), Gtk.ResponseType.CANCEL, "ghost"),
                                     (_("fo_apply").format(n=n), Gtk.ResponseType.OK, "accent")],
                                    icon_name="preferences-other-symbolic", extra=scroller,
                                    default=Gtk.ResponseType.OK, transient_for=self.sw.dialog)
        return response == Gtk.ResponseType.OK

    @staticmethod
    def _note(text: str, css: str) -> Gtk.Widget:
        lab = Gtk.Label(label=text, xalign=0)
        lab.set_line_wrap(True)
        lab.set_max_width_chars(64)
        lab.get_style_context().add_class(css)
        return lab

    # --- "some games": the list -----------------------------------------------------------
    def _pick_games(self) -> List:
        """The games to apply to, picked in a list (with what each has on now)."""
        _ = self._
        self.picked: Set[str] = set()
        self.checks: Dict[str, Gtk.CheckButton] = {}
        panel = self._games_panel()
        # as tall as its games, up to what the Shipwright window leaves - then it scrolls
        h = self.sw.dialog.get_allocated_height()
        panel.set_size_request(520, -1)
        self.list_scroller.set_propagate_natural_height(True)
        self.list_scroller.set_max_content_height(max(200, h - 330))
        self.next_btn = None
        response = self.app._card_dialog(_("fo_pick_title"), None,
                                         [(_("dialog_cancel"), Gtk.ResponseType.CANCEL, "ghost"),
                                          (_("fo_next"), Gtk.ResponseType.OK, "accent")],
                                         icon_name="preferences-other-symbolic", extra=panel, with_badge=False,
                                         default=Gtk.ResponseType.OK, transient_for=self.sw.dialog)
        if response != Gtk.ResponseType.OK:
            return []
        return [g for g in self.games if g.uid in self.picked]

    def _games_panel(self) -> Gtk.Widget:
        _ = self._
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.filter_launcher = None
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text(_("fo_search"))
        self.search.connect("search-changed", lambda *_a: self.list.invalidate_filter())
        panel.pack_start(self.search, False, False, 0)
        chips = Gtk.FlowBox()
        chips.set_selection_mode(Gtk.SelectionMode.NONE)
        chips.set_max_children_per_line(8)
        chips.set_column_spacing(6)
        chips.set_row_spacing(6)
        self.chip_buttons: Dict[Optional[str], Gtk.ToggleButton] = {}
        for name in [None] + sorted({g.launcher for g in self.games}):
            b = Gtk.ToggleButton(label=_("fo_all") if name is None else name)
            b.get_style_context().add_class("fo-chip")
            for side in ("top", "bottom", "start", "end"):
                getattr(b, f"set_margin_{side}")(3)      # room for the keyboard's ring
            b.set_active(name is None)
            b.connect("toggled", self._on_chip, name)
            self.chip_buttons[name] = b
            chips.add(b)
        panel.pack_start(chips, False, False, 0)
        links = Gtk.Box(spacing=14)
        for label, fn in ((_("fo_select_all"), self._select_all), (_("fo_select_none"), self._select_none)):
            b = Gtk.Button(label=label)
            b.set_relief(Gtk.ReliefStyle.NONE)
            b.get_style_context().add_class("sw-link")
            b.plunderhub_stop = True        # a stop for the keyboard / controller too
            b.connect("clicked", lambda *_a, f=fn: f())
            links.pack_start(b, False, False, 0)
        self.count_label = Gtk.Label(xalign=1)
        self.count_label.get_style_context().add_class("settings-row-sub")
        links.pack_end(self.count_label, False, False, 0)
        panel.pack_start(links, False, False, 0)
        self.list = Gtk.ListBox()
        self.list.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list.get_style_context().add_class("fo-gamelist")
        self.list.set_filter_func(self._filter)
        # each kind of badge in a column of its own: the kinds some game has, the launcher first
        kinds = [name for field, name in BADGES if any(FIELDS[field][1](self.opts[g.uid]) for g in self.games)]
        if any(self.opts[g.uid].fps_limit for g in self.games):
            kinds.append("FPS")
        self.badge_kinds = ["launcher"] + kinds
        self.badge_groups = {k: Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL) for k in self.badge_kinds}
        for g in self.games:
            self.list.add(self._game_row(g))
        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_overlay_scrolling(False)
        scroller.get_style_context().add_class("settings-scroll")
        scroller.add(self.list)
        self.list_scroller = scroller
        panel.pack_start(scroller, True, True, 0)
        self._count()
        return panel

    def _game_image(self, g, size: int) -> Gtk.Widget:
        """Its icon - its cover where it has none."""
        app = self.app
        try:
            if app._find_cached_artwork(app.game_icons_dir, g.uid) is not None:
                return app._create_game_icon_image(g, size)
            return app._create_cover_image(g, (int(size * 0.75), size))
        except Exception:
            return app._create_game_icon_image(g, size)

    def _game_row(self, g) -> Gtk.Widget:
        """The whole row is the pick (a toggle button): the keyboard's and
        controller's ring goes round the row, its box ticks with it."""
        row = Gtk.ListBoxRow()
        row.set_activatable(False)
        row.set_can_focus(False)
        row.game = g
        pick = Gtk.ToggleButton()
        pick.set_relief(Gtk.ReliefStyle.NONE)
        pick.get_style_context().add_class("fo-pick")
        line = Gtk.Box(spacing=10)
        check = Gtk.CheckButton()
        check.set_can_focus(False)
        check.set_valign(Gtk.Align.CENTER)
        syncing = {"on": False}

        def from_check(c):
            if not syncing["on"]:
                syncing["on"] = True
                pick.set_active(c.get_active())
                syncing["on"] = False

        def from_pick(b):
            if not syncing["on"]:
                syncing["on"] = True
                check.set_active(b.get_active())
                syncing["on"] = False
            self._on_check(b, g.uid)
        check.connect("toggled", from_check)
        pick.connect("toggled", from_pick)
        self.checks[g.uid] = pick
        line.pack_start(check, False, False, 0)
        line.pack_start(self._game_image(g, 40), False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        texts.set_valign(Gtk.Align.CENTER)
        title = Gtk.Label(label=g.title, xalign=0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.get_style_context().add_class("settings-row-title")
        texts.pack_start(title, False, False, 0)
        badges = Gtk.Box(spacing=5)
        opts = self.opts.get(g.uid)
        on = {name for field, name in BADGES if opts is not None and FIELDS[field][1](opts)}
        for kind in self.badge_kinds:
            if kind == "launcher":
                slot = Gtk.Label(label=g.launcher, xalign=0)
                slot.get_style_context().add_class("fo-launcher")
            elif kind == "FPS":
                text = f"{opts.fps_limit} FPS" if opts is not None and opts.fps_limit else ""
                slot = Gtk.Label(label=text)
                if text:
                    slot.get_style_context().add_class("fo-badge")
            else:
                slot = Gtk.Label(label=kind if kind in on else "")
                if kind in on:
                    slot.get_style_context().add_class("fo-badge")
            self.badge_groups[kind].add_widget(slot)
            badges.pack_start(slot, False, False, 0)
        texts.pack_start(badges, False, False, 0)
        line.pack_start(texts, True, True, 0)
        pick.add(line)
        row.add(pick)
        return row

    def _filter(self, row) -> bool:
        g = row.game
        if self.filter_launcher and g.launcher != self.filter_launcher:
            return False
        q = self.search.get_text().strip().lower()
        return not q or q in g.title.lower()

    def _on_chip(self, btn, name):
        if not btn.get_active():
            if all(not b.get_active() for b in self.chip_buttons.values()):
                btn.set_active(True)
            return
        for other, b in self.chip_buttons.items():
            if other != name and b.get_active():
                b.set_active(False)
        self.filter_launcher = name
        self.list.invalidate_filter()

    def _select_all(self):
        for row in self.list.get_children():
            if self._filter(row):
                self.checks[row.game.uid].set_active(True)

    def _select_none(self):
        for c in self.checks.values():
            c.set_active(False)

    def _on_check(self, check, uid):
        (self.picked.add if check.get_active() else self.picked.discard)(uid)
        self._count()

    def _count(self):
        self.count_label.set_text(self._("fo_n_games").format(n=len(self.picked)))

    # --- saving -----------------------------------------------------------------------------
    def _steam_ok(self, games) -> bool:
        """Steam writes its config back when it exits: closed first (asked once)."""
        if not any(g.launcher == "Steam" for g in games):
            return True
        backend = lo.SteamBackend()
        if not backend.blocking_process_running():
            return True
        app = self.app
        if not app._confirm_yes_no(self._("lo_steam_close_question"), transient_for=self.sw.dialog,
                                   message_type=Gtk.MessageType.WARNING, title=self._("lo_steam_close_and_save")):
            return False
        if not app._lo_close_steam(self.sw.dialog, backend):
            app._show_info_dialog(self._("lo_steam_still_running"), message_type=Gtk.MessageType.ERROR)
            return False
        return True

    def _save_all(self, plans: Dict[str, lo.LaunchOptions]) -> Dict[str, str]:
        """Saves each game's options; {uid: "title: why"} of the ones that failed."""
        failed: Dict[str, str] = {}
        rescan = False
        for g in self.games:
            new = plans.get(g.uid)
            if new is None:
                continue
            backend = lo.backend_for(g)
            try:
                backend.save(g, new)
                self.opts[g.uid] = backend.load(g)
                rescan = rescan or backend.needs_rescan
                logger.info(f"Fleet orders: saved launch options of {g.title!r} to {backend.where(g)}")
            except Exception as e:
                logger.error(f"Fleet orders: couldn't save launch options of {g.title!r}: {e}")
                failed[g.uid] = f"{g.title}: {e}"
        hints = getattr(self.app, "proton_hints", None)
        if hints is not None:
            hints.refresh()
        if rescan and hasattr(self.app, "reload"):
            GLib.idle_add(lambda: self.app.reload() and False)
        return failed

    def _undo(self):
        if not self.undo:
            return
        games = [g for g in self.games if g.uid in self.undo]
        if not self._steam_ok(games):
            return
        failed = self._save_all(self.undo)
        n = len(self.undo) - len(failed)
        self.undo = None
        self.undo_btn.hide()
        self.summary.set_text(self._("fo_undone").format(n=n))
        if failed:
            self.app._show_info_dialog(f"{self._('lo_save_failed')}:\n" + "\n".join(failed.values()),
                                       message_type=Gtk.MessageType.ERROR, transient_for=self.sw.dialog)
