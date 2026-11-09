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

"""Shipwright's Proton page: Proton-GE and Proton-CachyOS for every
launcher (core/proton_store.py) - the Latest kept up to date, any release
on request - and every game's Proton, changeable right there (the same
writing as its Launch options).

The page shows a ProtonData: RealProtonData in the app,
PlunderHubDemos/proton_demo.py's FakeProtonData in the design demo. Work
runs on a worker thread (ui/proton_jobs.py); the page listens and redraws
the status card on progress, its lists when a job ends.

Its texts are in i18n.py as "pm_*" (S below)."""

import math
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Set, Tuple

import cairo

from plunderhub.gi_setup import Gdk, GdkPixbuf, GLib, Gtk, Pango, pixbuf_at_size
from plunderhub.icons import get_icon_pixbuf
from plunderhub.logging_setup import logger
from plunderhub.perf_guard import allow_hover_relayout
from plunderhub.ui.home_mixin import _accent_rgb, _pixbuf_area

LAUNCHERS = ("Steam", "Heroic", "Lutris", "Faugus", "Bottles")

class _Texts:
    """The page's texts, from i18n.py under "pm_<name>" in the app's
    language: S["remove"] is the app's _("pm_remove"). Bound to the app by
    whoever comes first (the sidebar's card, the page, the upkeep)."""
    app = None

    def bind(self, app):
        type(self).app = app

    def __getitem__(self, name: str) -> str:
        app = type(self).app
        return app._(f"pm_{name}") if app is not None else name


S = _Texts()


@dataclass
class BuildView:
    family: str
    name: str
    tag: str
    size_mb: int
    latest: bool


@dataclass
class GameProton:
    title: str
    launcher: str
    choice: str                     # the Proton's id ("" = the launcher's default)
    choices: List[Tuple[str, str]] = field(default_factory=list)   # (id, label)
    native: bool = False
    default_label: str = ""
    game: object = None
    backend: object = None

    def proton_name(self) -> str:
        """The build it runs on, as a folder name ("" unknown)."""
        if self.native:
            return ""
        if not self.choice:
            return self.default_label
        label = dict(self.choices).get(self.choice, self.choice)
        return _proton_name(label if "/" not in label else self.choice)


class ProtonData:
    """What the page shows and does."""

    def listen(self, callback: Callable[[], None]) -> Callable[[], None]:
        return lambda: None

    def builds(self) -> List[BuildView]:
        raise NotImplementedError

    def releases(self, family: str, ready: Callable[[], None]):
        """The family's releases, None while they load (ready is called
        then), [] when GitHub can't be reached."""
        raise NotImplementedError

    def games(self, ready: Callable[[], None]) -> Optional[List[GameProton]]:
        raise NotImplementedError

    def status(self) -> Dict:
        """{"title", "line", "progress" (0..1 or None), "button": "cancel" |
        "check" | "download" | None, "busy": bool}"""
        raise NotImplementedError

    def finished(self) -> Optional[object]:
        """The last finished job (its .kind, .name, .error), to tell about."""
        return None

    def install(self, family: str, release) -> bool:
        return False

    def get_latest(self, family: str) -> bool:
        return False

    def check(self) -> bool:
        return False

    def cancel(self):
        pass

    def remove(self, name: str) -> bool:
        return False

    def set_proton(self, entry: GameProton, choice: str, parent: Gtk.Window) -> bool:
        return False


def _proton_name(choice: str) -> str:
    """The name a launcher lists a Proton by - Heroic keeps the path of its
    proton script, Lutris / Bottles a folder."""
    if "/" not in choice:
        return choice
    path = Path(choice)
    return path.parent.name if path.name in ("proton", "wine", "wine64") else path.name


def _on_disk(choice: str, name: str) -> bool:
    """Whether the Proton a choice names is there: a launcher keeping a
    path (Heroic, Lutris, Bottles) has it at that path; Steam names it,
    and has it when PlunderHub's store or a compatibilitytools.d does."""
    if "/" in choice:
        return Path(choice).exists()
    from plunderhub.core import proton_store
    from plunderhub.core.steam_heroic import steam_roots
    return (proton_store.find(name) is not None
            or any((root / "compatibilitytools.d" / name / "proton").exists() for root in steam_roots()))


def _build_key(choice: str, label: str) -> str:
    """Which build a choice is: its folder for a path (Heroic's own
    tools/proton and Steam's compatibilitytools.d copies are one), its
    name otherwise - "Proton - X", as Heroic lists Steam's, is X."""
    if "/" in choice:
        return _proton_name(choice)
    return label[len("Proton - "):] if label.startswith("Proton - ") else label


def _once_each(choices: List[Tuple[str, str]], current: Optional[str]) -> List[Tuple[str, str]]:
    """Each build once - PlunderHub puts a download into every launcher's
    folder, and Heroic lists its own copy and both Steams' - the game's
    current one where it's among them, else the first (the launcher's
    own, as Heroic lists it first); in the order they came."""
    by_key: Dict[str, Tuple[str, str]] = {}
    order: List[str] = []
    for i, label in choices:
        key = _build_key(i, label)
        if key not in by_key:
            by_key[key] = (i, label)
            order.append(key)
        elif i == current:
            # the game's own copy, under the first one's name
            by_key[key] = (i, by_key[key][1])
    return [by_key[key] for key in order]


def recommended_first(choices: List[Tuple[str, str]], recommended: str,
                      missing: str = "", current: Optional[str] = None) -> List[Tuple[str, str]]:
    """(id, label) choices with Proton-GE Latest first, marked, and every
    build once (see _once_each) - the game's current copy if it's among
    them. A Latest that isn't downloaded (a game still set to it) says
    so instead."""
    from plunderhub.core import proton_store
    name = proton_store.FAMILIES["ge"].latest_name
    latest = [(i, label) for i, label in choices if _proton_name(label) == name or _proton_name(i) == name]
    rest = _once_each([(i, label) for i, label in choices if (i, label) not in latest], current)
    if not latest:
        return rest
    there = [(i, label) for i, label in latest if _on_disk(i, name)]
    there.sort(key=lambda c: c[0] != current)
    if there:
        top_id, top_label = there[0]
        return [(top_id, f"{name}  ({recommended})")] + rest
    top_id, top_label = latest[0]
    return [(top_id, f"{name}  ({missing})" if missing else top_label)] + rest


def problem_text(problem: Optional[Exception], fallback: str = "") -> str:
    """What went wrong with a Proton download or GitHub lookup, said in
    words - with how long to wait when it's GitHub's hourly limit."""
    from plunderhub.core.proton_store import ProtonError
    kind = problem.kind if isinstance(problem, ProtonError) else ""
    detail = problem.detail if isinstance(problem, ProtonError) else {}
    if kind == "rate_limit":
        minutes = detail.get("minutes") or 0
        return S["err_rate_limit"] + " " + (S["err_wait"].format(minutes=minutes) if minutes
                                            else S["err_wait_later"])
    if kind == "http":
        return S["err_http"].format(code=detail.get("code") or "?")
    if kind == "space":
        return S["err_space"].format(size=_size(int(detail.get("need") or 0) >> 20))
    if kind in ("offline", "checksum", "archive", "in_use"):
        return S[f"err_{kind}"]
    return S["failed"].format(error=fallback or (str(problem) if problem else ""))


def _when(stamp: Optional[float]) -> str:
    if stamp is None:
        return ""
    return time.strftime("%H:%M", time.localtime(stamp))


class RealProtonData(ProtonData):
    def __init__(self, app):
        from plunderhub.ui import proton_jobs
        S.bind(app)
        self.app = app
        self.jobs = proton_jobs.jobs(app)
        self._releases: Dict[str, Optional[list]] = {}
        # why a family's release list couldn't be had
        self.release_problems: Dict[str, Exception] = {}
        self._loading: set = set()
        self._sizes: Dict[Tuple[str, str], int] = {}
        self._games: Optional[List[GameProton]] = None
        self._games_loading = False

    def listen(self, callback):
        return self.jobs.listen(callback)

    def builds(self) -> List[BuildView]:
        from plunderhub.core import proton_store
        found = []
        for build in proton_store.installed():
            key = (build.name, build.tag)
            if key not in self._sizes:
                self._sizes[key] = proton_store.build_size(build) >> 20
            found.append(BuildView(build.family, build.name, build.tag, self._sizes[key], build.latest))
        return found

    def releases(self, family: str, ready):
        if family in self._releases:
            return self._releases[family]
        if family not in self._loading:
            self._loading.add(family)

            def fetch():
                from plunderhub.core import proton_store
                problem = None
                try:
                    found = proton_store.releases(family)
                except Exception as e:
                    logger.warning(f"Proton releases of {family}: {e}")
                    found, problem = [], e
                GLib.idle_add(done, found, problem)

            def done(found, problem):
                self._loading.discard(family)
                self._releases[family] = found
                hints = getattr(self.app, "proton_hints", None)
                if hints is not None and found:
                    hints.refresh()     # a fresh list may mention more games
                if problem is not None:
                    self.release_problems[family] = problem
                else:
                    self.release_problems.pop(family, None)
                ready()
                return False
            threading.Thread(target=fetch, daemon=True, name="plunderhub-proton-list").start()
        return None

    def forget_releases(self, family: str):
        self._releases.pop(family, None)

    def games(self, ready):
        if self._games is not None:
            return self._games
        if not self._games_loading:
            self._games_loading = True
            games = list(self.app.game_index.values())

            def read():
                found = [entry for entry in (_game_proton(g) for g in games) if entry is not None]
                found.sort(key=lambda e: e.title.lower())
                GLib.idle_add(done, found)

            def done(found):
                self._games_loading = False
                self._games = found
                ready()
                return False
            threading.Thread(target=read, daemon=True, name="plunderhub-proton-games").start()
        return None

    def forget_games(self):
        self._games = None

    def status(self) -> Dict:
        from plunderhub.core import proton_store
        job = self.jobs.job
        ge = proton_store.FAMILIES["ge"].latest_name
        latest = proton_store.latest_build("ge")
        if job is not None:
            title = job.name
            if job.kind == "check":
                return {"title": ge, "line": S["st_checking"], "progress": None, "button": None, "busy": True}
            if job.kind == "remove":
                return {"title": title, "line": S["st_removing"].format(name=job.name), "progress": None,
                        "button": None, "busy": True}
            tag = job.tag or job.name
            if job.stage == "download":
                line = S["st_downloading"].format(tag=tag, pct=job.percent(), done=job.done >> 20,
                                                  total=job.total >> 20)
            elif job.stage == "check":
                line = S["st_checking_download"].format(tag=tag)
            elif job.stage == "unpack":
                line = S["st_unpacking"].format(tag=tag)
            else:
                line = S["st_downloading"].format(tag=tag, pct=0, done=0, total=0)
            return {"title": title, "line": line, "progress": job.percent() / 100, "button": "cancel",
                    "busy": True}
        waiting = ge in proton_store.pending()
        if waiting:
            tag = (proton_store._read_marker(proton_store.store_dir() / f"{proton_store.STAGING}{ge}")
                   or {}).get("tag", "")
            return {"title": ge, "line": S["st_waiting"].format(tag=tag), "progress": None,
                    "button": None, "busy": False}
        last = self.jobs.last
        if latest is None:
            line = S["st_missing"]
            if last is not None and last.error and last.error != "cancelled":
                line = problem_text(last.problem, last.error)
            return {"title": ge, "line": line, "progress": None, "button": "download", "busy": False}
        if last is not None and last.error and last.error != "cancelled" and last.name == ge:
            line = problem_text(last.problem, last.error)
        elif "ge" in self.jobs.checked_at:
            line = S["st_ok"].format(tag=latest.tag, when=_when(self.jobs.checked_at["ge"]))
        else:
            line = S["st_ok_unchecked"].format(tag=latest.tag)
        return {"title": ge, "line": line, "progress": None, "button": "check", "busy": False}

    def finished(self):
        return self.jobs.last

    def install(self, family: str, release) -> bool:
        return self.jobs.install(release, latest=False)

    def get_latest(self, family: str) -> bool:
        return self.jobs.update_latest(family, install_missing=True)

    def check(self) -> bool:
        return self.jobs.update_latest("ge", install_missing=True)

    def cancel(self):
        self.jobs.cancel()

    def remove(self, name: str) -> bool:
        return self.jobs.remove(name)

    def set_proton(self, entry: GameProton, choice: str, parent: Gtk.Window) -> bool:
        try:
            opts = entry.backend.load(entry.game)
        except Exception as e:
            logger.warning(f"Couldn't read {entry.title}'s launch options: {e}")
            return False
        opts.proton = choice or None
        if self.app._lo_save(entry.game, entry.backend, opts, parent):
            entry.choice = choice
            return True
        return False


def _game_proton(game) -> Optional[GameProton]:
    """The game's Proton as its launcher has it (worker thread: files only)."""
    from plunderhub.core.launch_options import backend_for
    try:
        backend = backend_for(game)
        if backend is None:
            return None
        opts = backend.load(game)
        choices = [(c.id, _proton_name(c.label) if "/" in c.label else c.label)
                   for c in backend.proton_choices(game, opts.proton)]
    except Exception as e:
        logger.debug(f"Proton page: couldn't read {game.title}: {e}")
        return None
    if not choices and not opts.proton:
        return None             # a launcher with no Proton choice for this game
    return GameProton(game.title, backend.launcher, opts.proton or "", choices, bool(opts.native),
                      launcher_default_name(backend), game, backend)


def launcher_default_name(backend) -> str:
    """The Proton the launcher runs a game on by default, as a folder
    name - "" when it doesn't say."""
    default = ""
    try:
        if hasattr(backend, "default_tool"):
            default = backend.default_tool()
        elif hasattr(backend, "_defaults"):
            wine = backend._defaults().get("wineVersion")
            default = wine.get("name", "") if isinstance(wine, dict) else ""
    except Exception:
        default = ""
    return _proton_name(default) if default else ""


def proton_combo_items(app, choices: List[Tuple[str, str]], current: str,
                       default_name: str) -> Tuple[List[Tuple[str, str]], str, bool]:
    """A Proton list as both Launch options and the games list show it:
    (items, the one to select, whether there's anything to pick).

    The launcher's default first ("" id); every build once, Proton-GE
    Latest marked Recommended. When the default IS Proton-GE Latest the
    two are one row - Recommended - and a game set to the Latest itself
    shows on it (the same build either way). With no Proton at all, one
    row says so."""
    _ = app._
    latest = proton_store_latest_name()
    items = recommended_first(choices, _("task_recommended"), _("pm_st_missing"), current)
    latest_ids = [i for i, label in items if _proton_name(i if "/" in i else label) == latest
                  or label.startswith(latest)]
    active = current
    merged = default_name == latest and bool(latest_ids) and _on_disk(latest_ids[0], latest)
    if merged:
        items = [(i, label) for i, label in items if i not in latest_ids]
        default_row = ("", f"{latest}  ({_('task_recommended')})")
        if current in latest_ids:
            active = ""
    elif default_name:
        default_row = ("", _("pm_default_named").format(name=default_name))
    else:
        default_row = ("", _("lo_proton_default"))
    if current and current not in [i for i, _l in items] and active:
        items.append((current, _proton_name(current)))

    def there(choice: str, label: str) -> bool:
        # a path is there or not; Steam names its own builds, which it has
        if "/" in choice:
            return Path(choice).exists()
        name = _build_key(choice, label)
        return _on_disk(choice, name) if name == latest else True
    if not merged and not any(there(i, label) for i, label in items):
        return [("", _("pm_none_downloaded"))], "", False
    return [default_row] + items, active, True


def proton_store_latest_name() -> str:
    from plunderhub.core import proton_store
    return proton_store.FAMILIES["ge"].latest_name


# --- drawing helpers ------------------------------------------------------------------

def _hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(round(c * 255) for c in rgb)


class _Ring(Gtk.DrawingArea):
    """The family's mark: a wine glass in an accent circle - or, while a
    build downloads, a ring filling up round it."""

    def __init__(self, px: int = 52):
        super().__init__()
        self.px = px
        self.progress: Optional[float] = None
        self.set_size_request(px, px)
        self.set_valign(Gtk.Align.CENTER)
        self.connect("draw", self._draw)

    def set_progress(self, progress: Optional[float]):
        if progress != self.progress:
            self.progress = progress
            self.queue_draw()

    def _draw(self, widget, cr) -> bool:
        accent = _accent_rgb(widget)
        r = self.px / 2
        cr.arc(r, r, r - 1, 0, 2 * math.pi)
        cr.set_source_rgba(*accent, 0.16)
        cr.fill()
        if self.progress is not None:
            cr.set_line_width(4)
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            cr.set_source_rgba(*accent, 0.22)
            cr.arc(r, r, r - 3, 0, 2 * math.pi)
            cr.stroke()
            cr.set_source_rgb(*accent)
            cr.arc(r, r, r - 3, -math.pi / 2, -math.pi / 2 + 2 * math.pi * max(0.02, self.progress))
            cr.stroke()
        icon = round(self.px * 0.46)
        pb = get_icon_pixbuf("cloud-download-symbolic" if self.progress is not None else "wine-symbolic",
                             icon, _hex(accent))
        if pb is not None:
            Gdk.cairo_set_source_pixbuf(cr, pb, r - icon / 2, r - icon / 2)
            cr.paint()
        return False


def _pill(text: str, css: str) -> Gtk.Label:
    label = Gtk.Label(label=text)
    label.get_style_context().add_class("proton-pill")
    label.get_style_context().add_class(css)
    label.set_valign(Gtk.Align.CENTER)
    return label


def _launcher_logo(name: str, px: int = 18) -> Gtk.Widget:
    from plunderhub.launchers.registry import bundled_launcher_icon
    path = bundled_launcher_icon(name)
    pb = None
    if path is not None:
        try:
            pb = pixbuf_at_size(path, px, px)
        except GLib.Error:
            pb = None
    if pb is None:
        return Gtk.Label(label=name[:1])
    area = _pixbuf_area(pb)
    area.set_tooltip_text(name)
    return area


def _size(mb: int) -> str:
    return f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb} MB"


def _used(n: int) -> str:
    return S["used_by_0"] if n == 0 else S["used_by_1"] if n == 1 else S["used_by"].format(n=n)


# --- the page ---------------------------------------------------------------------------

class ProtonPage(Gtk.Box):
    @property
    def TITLE(self) -> str:
        return S["title"]

    @property
    def SUB(self) -> str:
        return S["sub"]

    def __init__(self, sw, data: ProtonData):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        S.bind(sw.app)
        self.sw, self.app, self.data = sw, sw.app, data
        self.family = "ge"
        self.view = "versions"
        self.show_all_releases = False
        self.games_filter = "all"
        # launchers that ran while a game's Proton was changed here: they
        # pick it up once closed - said above the list, with a way to
        self._restart_needed: Set[str] = set()
        self._restart_box: Optional[Gtk.Box] = None
        self._restart_watch = 0
        self.connect("destroy", lambda *_: self._restart_watch and GLib.source_remove(self._restart_watch))
        self._busy_shown: Optional[bool] = None
        self._told = data.finished()

        self.pack_start(self._status_card(), False, False, 0)
        tabs = Gtk.Box(spacing=8)
        tabs.set_margin_top(4)
        self.tab_chips: Dict[str, Gtk.ToggleButton] = {}
        for key in ("versions", "games"):
            chip = self._chip(S[f"tab_{key}"], key == self.view, lambda b, k=key: self._set_view(b, k))
            tabs.pack_start(chip, False, False, 0)
            self.tab_chips[key] = chip
        self.pack_start(tabs, False, False, 0)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.pack_start(self.body, False, False, 0)
        self._render()
        stop = data.listen(self._changed)
        self.connect("destroy", lambda *_: stop())

    # --- the status card on top -------------------------------------------------------

    def _status_card(self) -> Gtk.Box:
        card = Gtk.Box(spacing=18)
        card.get_style_context().add_class("sw-hero")
        self.ring = _Ring(56)
        card.pack_start(self.ring, False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        texts.set_valign(Gtk.Align.CENTER)
        head = Gtk.Box(spacing=8)
        self.status_title = Gtk.Label(xalign=0)
        self.status_title.get_style_context().add_class("settings-row-title")
        self.status_title.get_style_context().add_class("proton-status-title")
        head.pack_start(self.status_title, False, False, 0)
        self.status_pills = [_pill(S["recommended"], "proton-pill-accent"),
                             _pill(S["default_all"], "proton-pill-quiet")]
        for pill in self.status_pills:
            head.pack_start(pill, False, False, 0)
        texts.pack_start(head, False, False, 0)
        self.status_line = Gtk.Label(xalign=0)
        # wrapped: what went wrong, and how long to wait, takes two lines
        self.status_line.set_line_wrap(True)
        self.status_line.set_max_width_chars(60)
        self.status_line.get_style_context().add_class("settings-row-sub")
        texts.pack_start(self.status_line, False, False, 0)
        seen = Gtk.Box(spacing=6)
        seen.set_margin_top(2)
        label = Gtk.Label(label=S["seen_by"], xalign=0)
        label.get_style_context().add_class("settings-row-sub")
        seen.pack_start(label, False, False, 0)
        for name in LAUNCHERS:
            seen.pack_start(_launcher_logo(name, 16), False, False, 0)
        texts.pack_start(seen, False, False, 0)
        card.pack_start(texts, True, True, 0)
        self.status_btn = self.app._dialog_button(S["check"], "ghost")
        self.status_btn.set_valign(Gtk.Align.CENTER)
        self.status_btn.set_focus_on_click(False)
        self.status_btn.set_no_show_all(True)
        self.status_btn.connect("clicked", self._on_status_btn)
        card.pack_end(self.status_btn, False, False, 0)
        # the notes: since the installed Latest when newer ones are out
        self.notes_btn = self._notes_button(None, "ghost")
        card.pack_end(self.notes_btn, False, False, 0)
        self._update_status()
        return card

    def _update_status(self):
        st = self.data.status()
        self.status_title.set_text(st["title"])
        latest_shown = st["title"].endswith("Latest") and "GE" in st["title"]
        for pill in self.status_pills:
            pill.set_visible(latest_shown)
        self.status_line.set_text(st["line"])
        self.ring.set_progress(st["progress"])
        button = st["button"]
        self.status_btn.plunderhub_action = button
        if button is None:
            self.status_btn.hide()
        else:
            self.status_btn.get_child().get_children()[0].set_text(S[button])
            # show_all() does nothing on a no_show_all widget
            self.status_btn.get_child().show_all()
            self.status_btn.show()
        return st

    def _on_status_btn(self, *_):
        action = getattr(self.status_btn, "plunderhub_action", None)
        if action == "cancel":
            self.data.cancel()
        elif action in ("check", "download"):
            self.data.check()

    def _changed(self):
        """A job moved on: the card; and the lists once it started or ended."""
        st = self._update_status()
        if st["busy"] != self._busy_shown:
            self._busy_shown = st["busy"]
            if not st["busy"]:
                if hasattr(self.data, "forget_games"):
                    self.data.forget_games()
                self._tell_failure()
            self._render()

    def _tell_failure(self):
        """A dialog for a job that failed - once, from the page on screen:
        a page of a Shipwright closed before stays quiet (its status line
        says it, should it come back)."""
        job = self.data.finished()
        if job is None or job is self._told or getattr(job, "told", False) or not self.get_mapped():
            return
        self._told = job
        if hasattr(job, "told"):
            job.told = True
        error = getattr(job, "error", "")
        if not error or error == "cancelled":
            return
        if "in use" in error:
            text = S["in_use"].format(name=job.name)
        elif "free space" in error:
            text = S["no_space"].format(tag=getattr(job, "tag", "") or job.name)
        else:
            text = problem_text(getattr(job, "problem", None), error)
        self.app._show_info_dialog(text, message_type=Gtk.MessageType.ERROR, transient_for=self.sw.dialog)

    # --- tabs -------------------------------------------------------------------------

    def _chip(self, text: str, active: bool, on_toggled: Callable) -> Gtk.ToggleButton:
        chip = Gtk.ToggleButton(label=text)
        chip.get_style_context().add_class("sw-chip")
        allow_hover_relayout(chip, "its dimmed text lights up under the pointer")
        chip.set_focus_on_click(False)
        chip.set_active(active)
        chip.connect("toggled", on_toggled)
        return chip

    def _one_of(self, chip, key: str, attr: str, chips: Optional[Dict] = None) -> bool:
        """Chip groups where one is always on; True when attr changed."""
        if not chip.get_active():
            if getattr(self, attr) == key:
                chip.set_active(True)
            return False
        setattr(self, attr, key)
        for other_key, other in (chips or {}).items():
            if other_key != key and other.get_active():
                other.set_active(False)
        return True

    def _set_view(self, chip, key: str):
        if self._one_of(chip, key, "view", self.tab_chips):
            self._render()

    def _render(self):
        for child in self.body.get_children():
            child.destroy()
        if self.view == "versions":
            self._versions()
        else:
            self._games()
        self.body.show_all()

    # --- Proton versions -------------------------------------------------------------

    def _group(self, title: str, extra: Optional[Gtk.Widget] = None) -> Gtk.Box:
        head = Gtk.Box(spacing=10)
        head.set_margin_top(6)
        label = Gtk.Label(label=title.upper(), xalign=0)
        label.get_style_context().add_class("settings-group-title")
        head.pack_start(label, False, False, 0)
        if extra is not None:
            head.pack_end(extra, False, False, 0)
        self.body.pack_start(head, False, False, 0)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.get_style_context().add_class("settings-card")
        self.body.pack_start(card, False, False, 0)
        return card

    @staticmethod
    def _add_row(card: Gtk.Box, row: Gtk.Widget):
        if card.get_children():
            sep = Gtk.Box()
            sep.get_style_context().add_class("settings-row-sep")
            sep.set_margin_start(16)
            sep.set_margin_end(16)
            card.pack_start(sep, False, False, 0)
        card.pack_start(row, False, False, 0)

    def _row(self, title: str, sub: str, pills=(), buttons=(), wrap: bool = False) -> Gtk.Box:
        row = Gtk.Box(spacing=12)
        row.get_style_context().add_class("settings-row")
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        texts.set_valign(Gtk.Align.CENTER)
        head = Gtk.Box(spacing=8)
        name = Gtk.Label(label=title, xalign=0)
        name.get_style_context().add_class("settings-row-title")
        if wrap:
            name.set_line_wrap(True)
            name.set_max_width_chars(60)
        else:
            name.set_ellipsize(Pango.EllipsizeMode.END)
        head.pack_start(name, False, False, 0)
        for pill in pills:
            head.pack_start(pill, False, False, 0)
        texts.pack_start(head, False, False, 0)
        if sub:
            line = Gtk.Label(label=sub, xalign=0)
            line.get_style_context().add_class("settings-row-sub")
            line.set_ellipsize(Pango.EllipsizeMode.END)
            texts.pack_start(line, False, False, 0)
        row.pack_start(texts, True, True, 0)
        for btn in reversed(list(buttons)):
            btn.set_valign(Gtk.Align.CENTER)
            btn.set_focus_on_click(False)
            row.pack_end(btn, False, False, 0)
        return row

    def _button(self, text: str, kind: str, action: Callable[[], object]) -> Gtk.Button:
        btn = self.app._dialog_button(text, kind)
        btn.connect("clicked", lambda *_: action())
        btn.set_sensitive(not self.data.status()["busy"])
        return btn

    def _notes_button(self, tag: Optional[str], kind: str = "ghost") -> Gtk.Button:
        """What's new: the release notes window - never greyed out, a
        download going on or not."""
        btn = self.app._dialog_button(S["notes_btn"], kind)
        btn.set_valign(Gtk.Align.CENTER)
        btn.set_focus_on_click(False)
        btn.connect("clicked", lambda *_: self._open_notes(tag))
        return btn

    def _open_notes(self, tag: Optional[str]):
        from plunderhub.ui.release_notes_window import open_notes
        releases = self.data.releases(self.family, lambda: None)
        open_notes(self.app, self.get_toplevel(), self.family, tag, releases or None, self.data.builds())

    def _used_by(self) -> Optional[Dict[str, int]]:
        games = self.data.games(lambda: self.view == "versions" and self._render())
        if games is None:
            return None
        counts: Dict[str, int] = {}
        for entry in games:
            name = entry.proton_name()
            if name:
                counts[name] = counts.get(name, 0) + 1
        return counts

    def _versions(self):
        from plunderhub.core import proton_store
        families = Gtk.Box(spacing=8)
        self.family_chips: Dict[str, Gtk.ToggleButton] = {}
        for key, text in (("ge", "Proton-GE  ★"), ("cachyos", "Proton-CachyOS")):
            chip = self._chip(text, key == self.family, lambda b, k=key: self._set_family(b, k))
            self.family_chips[key] = chip
            families.pack_start(chip, False, False, 0)
        self.body.pack_start(families, False, False, 0)
        note = S["ge_note"] if self.family == "ge" else S["cachyos_note"].format(
            level="x86-64-v3" if proton_store._cpu_level_v3() else "x86-64")
        blurb = Gtk.Label(label=note, xalign=0)
        blurb.set_line_wrap(True)
        blurb.set_max_width_chars(90)
        blurb.get_style_context().add_class("settings-row-sub")
        self.body.pack_start(blurb, False, False, 0)

        used = self._used_by()
        mine = [b for b in self.data.builds() if b.family == self.family]
        card = self._group(S["on_computer"])
        latest_name = proton_store.FAMILIES[self.family].latest_name
        if self.family == "cachyos" and not any(b.latest for b in mine):
            self._add_row(card, self._row(latest_name, S["latest_sub_manual"], (),
                                          (self._button(S["get_latest"], "accent",
                                                        lambda: self.data.get_latest("cachyos")),)))
        for build in mine:
            pills, sub = [], []
            if build.latest:
                pills.append(_pill(build.tag, "proton-pill-quiet"))
                if self.family == "ge":
                    pills.append(_pill(S["default_pill"], "proton-pill-accent"))
                sub.append(S["latest_sub"] if self.family == "ge" else S["latest_sub_manual"])
            sub.append(_size(build.size_mb))
            if used is not None:
                sub.append(_used(used.get(build.name, 0)))
            buttons = []
            if build.latest and self.family == "cachyos":
                buttons.append(self._button(S["check"], "ghost", lambda: self.data.get_latest("cachyos")))
            buttons.append(self._button(S["remove"], "ghost",
                                        lambda b=build: self._remove(b, (used or {}).get(b.name, 0))))
            self._add_row(card, self._row(build.name, "  ·  ".join(sub), pills, buttons))
        if not card.get_children():
            self._add_row(card, self._row(S["nothing_ge"] if self.family == "ge" else S["nothing"], ""))

        releases = self.data.releases(self.family, lambda: self._render())
        more = None
        if releases:
            more = Gtk.Button(label=S["fewer"] if self.show_all_releases
                              else S["all_releases"].format(n=len(releases)))
            more.get_style_context().add_class("sw-link")
            more.set_relief(Gtk.ReliefStyle.NONE)
            more.set_focus_on_click(False)
            more.connect("clicked", lambda *_: self._toggle_all())
        card = self._group(S["other_releases"], more)
        if releases is None:
            self._add_row(card, self._row(S["loading"], ""))
            return
        if not releases:
            retry = self._button(S["retry"], "ghost", self._retry_releases)
            problem = getattr(self.data, "release_problems", {}).get(self.family)
            self._add_row(card, self._row(problem_text(problem) if problem is not None else S["unreachable"],
                                          "", (), (retry,), wrap=True))
            return
        # a GE folder is named after its tag, a CachyOS one after its archive
        own = {key for b in mine if not b.latest for key in (b.name, b.tag)}
        shown = [r for r in releases if r.tag not in own]
        for release in (shown if self.show_all_releases else shown[:6]):
            get = self._button(S["download"], "ghost", lambda r=release: self.data.install(self.family, r))
            self._add_row(card, self._row(release.tag, S["release_sub"].format(
                date=release.date, size=_size(release.size >> 20)), (), (get,)))

    def _retry_releases(self):
        if hasattr(self.data, "forget_releases"):
            self.data.forget_releases(self.family)
        self._render()

    def _set_family(self, chip, key: str):
        if self._one_of(chip, key, "family", self.family_chips):
            self.show_all_releases = False
            self._render()

    def _toggle_all(self):
        self.show_all_releases = not self.show_all_releases
        self._render()

    def _remove(self, build: BuildView, used: int):
        text = S["remove_q"].format(name=build.name, size=_size(build.size_mb))
        if used:
            text += "\n\n" + (S["remove_used_1"] if used == 1 else S["remove_used"].format(n=used))
        if self.app._confirm_yes_no(text, transient_for=self.sw.dialog, title=S["remove"],
                                    yes_label=S["remove"], destructive=True):
            self.data.remove(build.name)

    # --- games and their Proton ------------------------------------------------------

    def _games(self):
        games = self.data.games(lambda: self.view == "games" and self._render())
        if games is None:
            note = Gtk.Label(label=S["games_loading"], xalign=0)
            note.get_style_context().add_class("settings-row-sub")
            self.body.pack_start(note, False, False, 0)
            return
        launchers = sorted({g.launcher for g in games})
        if self.games_filter != "all" and self.games_filter not in launchers:
            self.games_filter = "all"
        chips = Gtk.Box(spacing=8)
        self.filter_chips: Dict[str, Gtk.ToggleButton] = {}
        for key in ["all"] + launchers:
            count = len(games) if key == "all" else sum(g.launcher == key for g in games)
            chip = self._chip(f"{S['all'] if key == 'all' else key}  {count}", key == self.games_filter,
                              lambda b, k=key: self._set_games_filter(b, k))
            self.filter_chips[key] = chip
            chips.pack_start(chip, False, False, 0)
        self.body.pack_start(chips, False, False, 0)
        note = Gtk.Label(label=S["games_note"], xalign=0)
        note.set_line_wrap(True)
        note.set_max_width_chars(90)
        note.get_style_context().add_class("settings-row-sub")
        self.body.pack_start(note, False, False, 0)
        self._restart_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.body.pack_start(self._restart_box, False, False, 0)
        self._show_restart_notices()
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        card.get_style_context().add_class("settings-card")
        self.body.pack_start(card, False, False, 0)
        for entry in games:
            if self.games_filter == "all" or entry.launcher == self.games_filter:
                self._add_row(card, self._game_row(entry))
        if not card.get_children():
            self._add_row(card, self._row(S["games_none"], ""))

    def _show_restart_notices(self):
        """A note for each launcher still running since a game's Proton
        was changed here, with its Close button - as Launch options has."""
        from plunderhub.core.launch_options import launcher_pids
        box = self._restart_box
        if box is None:
            return
        self._restart_needed = {name for name in self._restart_needed if launcher_pids(name)}
        for child in box.get_children():
            child.destroy()
        for name in sorted(self._restart_needed):
            note = Gtk.Box(spacing=12)
            note.get_style_context().add_class("gp-notice")
            pb = get_icon_pixbuf("triangle-alert-symbolic", 18, "#e8a33d")
            if pb is not None:
                note.pack_start(Gtk.Image.new_from_pixbuf(pb), False, False, 0)
            text = Gtk.Label(label=S["restart_needed"].format(launcher=name), xalign=0)
            text.set_line_wrap(True)
            note.pack_start(text, True, True, 0)
            close = self.app._dialog_button(self.app._("lo_close_launcher").format(launcher=name), "ghost")
            close.set_valign(Gtk.Align.CENTER)
            close.connect("clicked", lambda _b, n=name: self.app.close_launcher_asking(n, self.sw.dialog)
                          and self._show_restart_notices())
            note.pack_end(close, False, False, 0)
            box.pack_start(note, False, False, 0)
        box.show_all()
        if self._restart_needed and not self._restart_watch:
            # gone by itself once the launcher is closed some other way
            self._restart_watch = GLib.timeout_add(2000, self._watch_restart)

    def _watch_restart(self) -> bool:
        from plunderhub.core.launch_options import launcher_pids
        if any(not launcher_pids(name) for name in self._restart_needed):
            self._show_restart_notices()
        if not self._restart_needed:
            self._restart_watch = 0
            return False
        return True

    def _set_games_filter(self, chip, key: str):
        if self._one_of(chip, key, "games_filter", self.filter_chips):
            self._render()

    def _game_row(self, entry: GameProton) -> Gtk.Box:
        row = Gtk.Box(spacing=14)
        row.get_style_context().add_class("settings-row")
        if entry.game is not None:
            icon = self.app._create_game_icon_image(entry.game, 36)
            icon.set_valign(Gtk.Align.CENTER)
            row.pack_start(icon, False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        texts.set_valign(Gtk.Align.CENTER)
        title = Gtk.Label(label=entry.title, xalign=0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.get_style_context().add_class("settings-row-title")
        texts.pack_start(title, False, False, 0)
        where = Gtk.Box(spacing=6)
        where.pack_start(_launcher_logo(entry.launcher, 14), False, False, 0)
        sub = Gtk.Label(label=entry.launcher, xalign=0)
        sub.get_style_context().add_class("settings-row-sub")
        where.pack_start(sub, False, False, 0)
        # a newer release mentions it (ui/proton_hints.py): the pill opens those notes
        hints = getattr(self.app, "proton_hints", None)
        found = hints.get(entry.game.uid) if hints is not None and entry.game is not None else None
        if found is not None:
            pill = Gtk.Button(label=S["hint_pill"].format(tag=found[0].tag))
            pill.get_style_context().add_class("ph-pill")
            pill.set_focus_on_click(False)
            pill.set_tooltip_text(f"“{found[0].excerpt}”")
            pill.connect("clicked", lambda *_a, uid=entry.game.uid: hints.read(uid, self.get_toplevel()))
            where.pack_start(pill, False, False, 6)
        texts.pack_start(where, False, False, 0)
        row.pack_start(texts, True, True, 0)
        if entry.native:
            row.pack_end(_pill(S["native"], "proton-pill-quiet"), False, False, 0)
            return row
        combo = Gtk.ComboBoxText()
        items, active, any_proton = proton_combo_items(self.app, entry.choices, entry.choice, entry.default_label)
        for choice_id, label in items:
            combo.append(choice_id, label)
        combo.set_active_id(active)
        combo.set_sensitive(any_proton)
        # what's selected may stand for the game's own setting (the Latest
        # as the default row): a change is only one away from that
        entry.shown = active
        combo.set_valign(Gtk.Align.CENTER)
        # one width for every row, whatever the launcher's longest name
        for cell in combo.get_cells():
            cell.set_property("ellipsize", Pango.EllipsizeMode.END)
            cell.set_property("width-chars", 32)
            cell.set_property("max-width-chars", 32)
        combo.connect("changed", self._on_game_proton, entry)
        row.pack_end(combo, False, False, 0)
        return row

    def _on_game_proton(self, combo, entry: GameProton):
        choice = combo.get_active_id() or ""
        if choice == entry.choice or choice == getattr(entry, "shown", entry.choice):
            return
        if not self.data.set_proton(entry, choice, self.sw.dialog):
            # not saved (Steam left running, an error): back as it was
            shown = getattr(entry, "shown", entry.choice)
            GLib.idle_add(lambda: combo.set_active_id(shown) and False)
            return
        entry.shown = choice
        try:
            running = entry.backend.advisory_process_running()
        except Exception:
            running = False
        if running:
            # saved, but the running launcher only picks it up once closed
            self._restart_needed.add(entry.launcher)
            GLib.idle_add(lambda: self._show_restart_notices() and False)


def latest_choice(game) -> str:
    """Proton-GE Latest's id in the game's Proton list - "" when it isn't
    downloaded, the game runs natively or is already on it."""
    from plunderhub.core import proton_store
    latest = proton_store.FAMILIES["ge"].latest_name
    if proton_store.find(latest) is None:
        return ""
    entry = _game_proton(game)
    if entry is None or entry.native:
        return ""
    target = next((i for i, label in entry.choices if _proton_name(i if "/" in i else label) == latest), "")
    return "" if target == entry.choice else target


class HomeOfferCard(Gtk.Revealer):
    """In the main window's sidebar, like the app's own update card: a newer
    Proton-GE is out. Downloads only when Download is pressed; Later hides
    it until an even newer one."""

    def __init__(self, app, data: "RealProtonData"):
        super().__init__()
        self.app, self.data = app, data
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.set_transition_duration(260)
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.get_style_context().add_class("home-update-card")
        top = Gtk.Box(spacing=10)
        top.pack_start(_Ring(36), False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        texts.set_valign(Gtk.Align.CENTER)
        self.title = Gtk.Label(xalign=0)
        self.title.get_style_context().add_class("home-update-title")
        self.line = Gtk.Label(xalign=0)
        self.line.get_style_context().add_class("home-update-versions")
        for label in (self.title, self.line):
            label.set_ellipsize(Pango.EllipsizeMode.END)
            label.set_max_width_chars(1)
            texts.pack_start(label, False, False, 0)
        top.pack_start(texts, True, True, 0)
        card.pack_start(top, False, False, 0)
        buttons = Gtk.Box(spacing=6, homogeneous=True)
        self.later = Gtk.Button()
        self.download = Gtk.Button()
        for btn, css in ((self.later, "home-update-later"), (self.download, "home-update-install")):
            btn.get_style_context().add_class(css)
            btn.set_can_focus(False)
            buttons.pack_start(btn, True, True, 0)
        self.later.connect("clicked", lambda *_: self._later())
        self.download.connect("clicked", lambda *_: self._download())
        card.pack_start(buttons, False, False, 0)
        self.add(card)
        self.set_margin_start(12)
        self.set_margin_end(12)
        self.set_margin_bottom(8)
        self.show_all()
        stop = data.listen(self._refresh)
        self.connect("destroy", lambda *_: stop())
        self._refresh()

    def _offered(self):
        from plunderhub.core import proton_store
        offer = self.data.jobs.offer
        current = proton_store.latest_build("ge")
        if (offer is None or self.data.jobs.busy() or (current is not None and current.tag == offer.tag)
                or offer.tag == self.app.settings.get("pm_offer_later")):
            return None, current
        return offer, current

    def _refresh(self):
        offer, current = self._offered()
        if offer is not None:
            self.title.set_text(S["offer_title"])
            # the new one alone: both names don't fit the sidebar
            self.line.set_text(offer.tag if current is not None else S["offer_missing"].format(tag=offer.tag))
            self.line.set_tooltip_text(f"{current.tag}  →  {offer.tag}" if current is not None else None)
            self.later.set_label(self.app._("update_later_btn"))
            self.download.set_label(S["offer_download"])
        if self.get_reveal_child() != (offer is not None):
            self.set_reveal_child(offer is not None)

    def _later(self):
        from plunderhub.core.persistence import save_settings
        offer, _current = self._offered()
        if offer is not None:
            self.app.settings["pm_offer_later"] = offer.tag
            save_settings(self.app.settings)
        self._refresh()

    def _download(self):
        offer, _current = self._offered()
        if offer is not None:
            self.data.jobs.install(offer, latest=True)


# --- the home page's card while a Proton downloads --------------------------------

NOTES_CARD_MS = 3000       # how long the sidebar's "what's new" card stays


class HomeDownloadCard(Gtk.Revealer):
    """In the main window's sidebar, where the update card sits: shown
    while a Proton job runs, its progress round the mark; a click opens
    Shipwright's Proton page."""

    def __init__(self, app, data: ProtonData, open_page: Callable[[], None]):
        super().__init__()
        self.app, self.data = app, data
        self.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self.set_transition_duration(260)
        button = Gtk.Button()
        button.set_relief(Gtk.ReliefStyle.NONE)
        button.set_can_focus(False)
        button.get_style_context().add_class("home-update-card")
        button.get_style_context().add_class("proton-home-card")
        self._notes_tag: Optional[str] = None
        self._notes_timer: Optional[int] = None
        self.connect("destroy", lambda *_: self._stop_notes_timer())
        button.connect("clicked", lambda *_: self._clicked(open_page))
        top = Gtk.Box(spacing=10)
        self.ring = _Ring(36)
        top.pack_start(self.ring, False, False, 0)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        texts.set_valign(Gtk.Align.CENTER)
        self.title = Gtk.Label(xalign=0)
        self.title.get_style_context().add_class("home-update-title")
        self.title.set_ellipsize(Pango.EllipsizeMode.END)
        self.line = Gtk.Label(xalign=0)
        self.line.get_style_context().add_class("home-update-versions")
        self.line.set_ellipsize(Pango.EllipsizeMode.END)
        app._apply_tabular_figures(self.line)
        # Asking for no width of their own: the sidebar's width stays
        # put while the numbers change, the text ellipsized to it.
        for label in (self.title, self.line):
            label.set_max_width_chars(1)
        texts.pack_start(self.title, False, False, 0)
        texts.pack_start(self.line, False, False, 0)
        top.pack_start(texts, True, True, 0)
        button.add(top)
        self.add(button)
        self.set_margin_start(12)
        self.set_margin_end(12)
        self.set_margin_bottom(8)
        self.show_all()
        stop = data.listen(self._refresh)
        self.connect("destroy", lambda *_: stop())
        self._refresh()

    def _refresh(self):
        from plunderhub.core import proton_store
        st = self.data.status()
        busy = st["busy"] and st["progress"] is not None
        # Proton-GE Latest isn't there and fetching it failed (no
        # connection, GitHub's hourly limit): said here too, never silent
        last = self.data.finished()
        failed = (not st["busy"] and proton_store.latest_build("ge") is None and last is not None
                  and bool(last.error) and last.error != "cancelled")
        # Proton-GE Latest got a new release: what's new in it, for a few seconds
        notes = None
        if not st["busy"] and last is not None and not last.error and last.kind == "install" \
                and last.name == proton_store.FAMILIES["ge"].latest_name and last.tag \
                and last.tag != self.app.settings.get("pm_notes_seen"):
            notes = last.tag
        self._notes_tag = notes if not (busy or failed) else None
        if self._notes_tag:
            if self._notes_timer is None:
                self._notes_timer = GLib.timeout_add(NOTES_CARD_MS, self._notes_expired, self._notes_tag)
            self.title.set_text(S["notes_card_title"])
            self.line.set_text(S["notes_card_line"].format(tag=notes))
            self.get_child().set_tooltip_text(None)
            self.ring.set_progress(1.0)
        if busy or failed:
            self.title.set_text(st["title"])
            # the reason is too long for the sidebar: in short here, in full on hover
            self.line.set_text(S["st_missing"] if failed else st["line"].split("  ·  ")[0])
            self.get_child().set_tooltip_text(st["line"] if failed else None)
            self.ring.set_progress(st["progress"] if busy else 0.0)
        shown = busy or failed or bool(self._notes_tag)
        if self.get_reveal_child() != shown:
            self.set_reveal_child(shown)

    def _stop_notes_timer(self):
        if self._notes_timer is not None:
            GLib.source_remove(self._notes_timer)
            self._notes_timer = None

    def _notes_expired(self, tag: str) -> bool:
        from plunderhub.core.persistence import save_settings
        self._notes_timer = None
        self.app.settings["pm_notes_seen"] = tag
        save_settings(self.app.settings)
        if self._notes_tag == tag:
            self._notes_tag = None
            self.set_reveal_child(False)
        return False

    def _clicked(self, open_page: Callable[[], None]):
        tag = self._notes_tag
        if not tag:
            open_page()
            return
        self._stop_notes_timer()
        from plunderhub.core.persistence import save_settings
        from plunderhub.ui.release_notes_window import open_notes
        self.app.settings["pm_notes_seen"] = tag
        save_settings(self.app.settings)
        self._notes_tag = None
        self.set_reveal_child(False)
        open_notes(self.app, self.get_toplevel(), "ge", tag, None, self.data.builds())
