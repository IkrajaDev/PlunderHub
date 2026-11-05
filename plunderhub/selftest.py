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


import sys
import time
import traceback
from typing import Callable, List, Tuple

from plunderhub.logging_setup import logger

_STEP_INTERVAL_MS = 400

_TIMEOUT_SECONDS = 240

_failures: List[str] = []
_css_errors: List[str] = []


def record_css_error(section, error) -> None:
    try:
        message = error.message if hasattr(error, "message") else str(error)
        start = section.get_start_line() + 1 if section else "?"
        detail = f"CSS parse error at line {start}: {message}"
    except Exception:
        detail = f"CSS parse error: {error}"
    _css_errors.append(detail)
    logger.error(f"[self-test] {detail}")


def _step(name: str, action: Callable[[], None]) -> Tuple[str, Callable[[], None]]:
    return (name, action)


def _build_steps(window) -> List[Tuple[str, Callable[[], None]]]:
    from plunderhub.theme import THEME_ORDER

    steps: List[Tuple[str, Callable[[], None]]] = []

    def show_window():
        window.show_all()
        assert window.get_realized() or window.get_visible(), "window never appeared"

    steps.append(_step("window builds and appears", show_window))

    for theme in THEME_ORDER:
        steps.append(_step(f"apply theme: {theme}",
                           lambda t=theme: window._apply_theme(t)))

    steps.append(_step("restore the configured theme",
                       lambda: window._apply_theme(window.theme)))

    steps.append(_step("retint icons", lambda: window._refresh_icon_tints()))

    class _Toggled:
        @staticmethod
        def get_active():
            return True

    # each toggle saves to the real config: end on the user's own view
    user_mode = window.card_view_mode
    for mode in ("list", "grid", user_mode):
        steps.append(_step(f"card view: {mode}",
                           lambda m=mode: window._on_card_view_mode_toggled(_Toggled, m)))

    steps.append(_step("re-render the library", lambda: window._render_launchers()))

    steps.append(_step("enter Console Mode", lambda: window._enter_console_mode()))
    steps.append(_step("leave Console Mode", lambda: window._exit_console_mode()))

    steps.append(_step("build the shortcuts reference",
                       lambda: window._build_shortcuts_panels()))
    steps.append(_step("measure the cargo hold",
                       lambda: window._compute_storage_results()))

    def build_settings():
        from plunderhub.ui.settings_window import SettingsWindow
        settings = SettingsWindow(window)
        settings._refresh_launcher_rows()
        for page in ("general", "appearance", "library", "covers", "console", "system"):
            settings.goto(page)
        settings.dialog.destroy()
        window._options_dialog = None

    steps.append(_step("build the Settings window", build_settings))

    def build_launcher_menus():
        from plunderhub.ui.game_page import LauncherMenu
        for section in window._cached_launchers:
            menu = LauncherMenu(window, section["name"], window)
            for side in ("icon", "open"):
                menu._open_side(side)
                menu._close_side()
            menu.destroy()
    steps.append(_step("build the launcher menus", build_launcher_menus))

    def edit_order_both_views():
        if not window.game_index:
            return              # an empty library has no order to edit (a fresh install, a container)
        mode, nav, sort = window.card_view_mode, window.home_nav, window.sort_mode
        try:
            for view in ("grid", "list"):
                window.card_view_mode = view
                window.reorder_mode = False
                window._enter_reorder_mode()
                for action in ("activate", "down", "right", "back"):
                    window._reorder_grid.handle(action)
                window._exit_reorder_mode()
        finally:
            # Edit order saves the sort and the view it switched to
            from plunderhub.core.persistence import save_settings
            window.card_view_mode, window.home_nav, window.sort_mode = mode, nav, sort
            window.settings["home_nav"], window.settings["sort_mode"] = nav, sort
            save_settings(window.settings)
            window._render_launchers()
    steps.append(_step("Edit order in the grid and list views", edit_order_both_views))

    # --- performance guards (perf_guard.py): hovering must not lay a
    # window out again, a window left alone must not keep repainting
    from plunderhub import perf_guard

    steps.append(_step("performance: hovering the library",
                       lambda: perf_guard.assert_calm(window, "main window", idle=False)))


    def calm_settings():
        from plunderhub.ui.settings_window import SettingsWindow
        settings = SettingsWindow(window)
        settings.dialog.show_all()
        try:
            for page in ("general", "appearance", "library", "covers", "console", "system"):
                settings.goto(page)
                perf_guard.assert_calm(settings.dialog, f"Settings window ({page})")
        finally:
            settings.dialog.destroy()
            window._options_dialog = None
    steps.append(_step("performance: the Settings window", calm_settings))

    def calm_shipwright():
        from plunderhub.ui import shipwright
        sw = shipwright.show(window)
        try:
            perf_guard.assert_calm(sw.dialog, "Shipwright")
        finally:
            sw.close()
    steps.append(_step("performance: Shipwright", calm_shipwright))

    def calm_shipwright_card():
        import time
        from plunderhub.gi_setup import Gtk
        from plunderhub.ui import shipwright
        sw = shipwright.show(window)
        try:
            sw.show_card(next(iter(sw.items)))
            # past the icon's pop and ring (about 2 s), then calm
            end = time.monotonic() + 2.4
            while time.monotonic() < end:
                if not Gtk.main_iteration_do(False):
                    time.sleep(0.005)
            perf_guard.assert_calm(sw.card.dialog, "Shipwright's tool card")
        finally:
            if sw.card is not None:
                sw.card.close()
            sw.close()
    steps.append(_step("performance: Shipwright's tool card", calm_shipwright_card))

    def calm_proton_page():
        from gi.repository import Gtk
        from plunderhub.ui import shipwright
        sw = shipwright.show(window, focus="protons")
        try:
            page = sw.proton_page
            perf_guard.assert_calm(sw.dialog, "Shipwright's Proton page")
            page.tab_chips["games"].set_active(True)
            # its games are read on a worker thread
            deadline = time.monotonic() + 10
            while page.data.games(lambda: None) is None and time.monotonic() < deadline:
                Gtk.main_iteration_do(False)
            page._render()
            perf_guard.assert_calm(sw.dialog, "Shipwright's Proton page (games)")
        finally:
            sw.close()
    steps.append(_step("performance: Shipwright's Proton page", calm_proton_page))

    def calm_bilge_page():
        # made-up leftovers (folders in a temporary place, nothing is
        # deleted): the tiles, a row with a pill and its buttons, the BETA note
        import tempfile
        from pathlib import Path
        from gi.repository import Gtk
        from plunderhub.core import bilge
        from plunderhub.ui import bilge_page, shipwright
        tmp = Path(tempfile.mkdtemp(prefix="plunderhub-selftest-bilge-"))
        items = []
        for kind, launcher, name, saves in (("steam_prefix", "Steam", "1091500", 3),
                                            ("shader_cache", "Steam", "1091501", 0),
                                            ("prefix", "Heroic", "Some Game", 2), ("proton", "PlunderHub",
                                                                                   "GE-Proton10-1", 0)):
            (tmp / name).mkdir()
            items.append(bilge.Item(kind, launcher, tmp / name, title=name, appid=name, size=1 << 30,
                                    changed=time.time(), saves=saves, saves_newest=time.time()))
        real_scan = bilge_page.SCAN
        bilge_page.SCAN = lambda app: bilge.Found(items=list(items))
        sw = None
        try:
            sw = shipwright.show(window, focus="bilge")
            page = sw.bilge_page
            deadline = time.monotonic() + 10
            while page.found is None and time.monotonic() < deadline:
                Gtk.main_iteration_do(False)
            assert page.found is not None and len(page.found.items) == 4, "the Bilge page didn't read its leftovers"
            perf_guard.assert_calm(sw.dialog, "Shipwright's Bilge page")
            page._pick_tab("prefix")
            perf_guard.assert_calm(sw.dialog, "Shipwright's Bilge page (launcher prefixes)")
        finally:
            bilge_page.SCAN = real_scan
            if sw is not None:
                sw.close()
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
    steps.append(_step("performance: Shipwright's Bilge page", calm_bilge_page))

    def calm_release_notes():
        from types import SimpleNamespace as R
        from plunderhub.ui.release_notes_window import SINCE, open_notes
        game = next(iter(window.game_index.values()), None)
        name = game.title if game is not None else "Star Citizen"
        notes = ("# GE-Proton99-2\n\nHotfix Release\n\n## Fixes\n\n- Fixed **%s** crashing at start "
                 "([#865](https://github.com/GloriousEggroll/proton-ge-custom/issues/865)).\n"
                 "  - Nested point with `PROTON_LOG=1` and https://gitlab.winehq.org/wine/wine/-/merge_requests/1\n"
                 "\n```\nPROTON_ENABLE_WAYLAND=1 %%command%%\n```\n\n---\n\n1. Numbered\n" % name)
        releases = [R(tag=f"GE-Proton99-{n}", date="2026-11-0%d" % n, size=500 << 20, notes=notes)
                    for n in (3, 2, 1)]
        builds = [R(family="ge", tag="GE-Proton99-1", latest=True)]
        notes_window = open_notes(window, window, "ge", None, releases, builds)
        try:
            assert notes_window.current == SINCE, "doesn't open on 'Since your version'"
            from plunderhub.core.release_notes import _forms
            if game is not None and _forms(name):
                assert notes_window.mention_box.get_visible(), f"no chip for {name!r} the notes mention"
            perf_guard.assert_calm(notes_window.dialog, "the release notes window")
            notes_window.listbox.select_row(notes_window.rows["GE-Proton99-2"])
            perf_guard.assert_calm(notes_window.dialog, "the release notes window (one release)")
        finally:
            notes_window.dialog.destroy()
    steps.append(_step("performance: the release notes window", calm_release_notes))

    def calm_game_menus():
        from plunderhub.ui.game_page import GameMenu, LauncherMenu
        menus = []
        if window.game_index:
            menus.append(("a game's menu", GameMenu(window, next(iter(window.game_index.values())), window)))
        for section in window._cached_launchers[:1]:
            menus.append(("a launcher's menu", LauncherMenu(window, section["name"], window)))
        for what, menu in menus:
            menu.popup()
            try:
                perf_guard.assert_calm(window, f"main window ({what})", root=menu, idle=False)
            finally:
                menu.popdown()
                menu.destroy()
    steps.append(_step("performance: the game and launcher menus", calm_game_menus))

    def calm_game_page():
        if not window.game_index:
            return
        from gi.repository import GLib, Gtk
        from plunderhub.ui.game_page import GamePage
        outcome = {}
        # the page's art plays on purpose: still for the check of a
        # window left alone
        animated, window.animated_covers = window.animated_covers, False

        def check() -> bool:
            page = window._game_page
            if page is None:
                return True
            try:
                perf_guard.assert_calm(page, "game page")
                if page.launch is not None:
                    page._show_tab("launch")
                    perf_guard.assert_calm(page, "game page (launch options)")
            except AssertionError as e:
                outcome["error"] = e
            finally:
                page.response(Gtk.ResponseType.CLOSE)
            return False
        GLib.timeout_add(800, check)
        try:
            GamePage(window, next(iter(window.game_index.values())))
        finally:
            window.animated_covers = animated
        if "error" in outcome:
            raise outcome["error"]
    steps.append(_step("performance: the game page", calm_game_page))

    def calm_card_dialog(what: str, run_dialog, wait_ms: int = 800):
        """Checks a card dialog that blocks in run(): the check is
        scheduled first (after its opening animation, if it has one),
        finds the dialog and closes it."""
        from gi.repository import GLib, Gtk
        outcome = {}
        animated, window.animated_covers = window.animated_covers, False

        def check() -> bool:
            for top in Gtk.Window.list_toplevels():
                if (isinstance(top, Gtk.Dialog) and top.get_visible()
                        and "home-launch-dialog" in top.get_style_context().list_classes()):
                    try:
                        perf_guard.assert_calm(top, what)
                    except AssertionError as e:
                        outcome["error"] = e
                    finally:
                        top.response(Gtk.ResponseType.CANCEL)
                    return False
            return True
        GLib.timeout_add(wait_ms, check)
        try:
            run_dialog()
        finally:
            window.animated_covers = animated
        if "error" in outcome:
            raise outcome["error"]

    def calm_set_sail():
        if not window.game_index:
            return
        game = next(iter(window.game_index.values()))
        calm_card_dialog("Set sail dialog", lambda: window._confirm_launch(game.title, game.uid))
    steps.append(_step("performance: the Set sail dialog", calm_set_sail))
    steps.append(_step("performance: the welcome card",
                       lambda: calm_card_dialog("welcome card", window._show_welcome, wait_ms=2500)))
    steps.append(_step("performance: the shortcuts dialog",
                       lambda: calm_card_dialog("shortcuts dialog", window._show_shortcuts_dialog)))
    steps.append(_step("performance: the Report bug card",
                       lambda: calm_card_dialog("Report bug card", window._show_bug_report)))

    def calm_captains_log():
        from plunderhub.ui import captains_log
        info = {"tag": "v9.9.9", "date": "2026-11-03",
                "notes": "## PlunderHub 9.9.9\n\nA test.\n\n- One **fix**\n  - and `a detail`\n"
                         "- [#1](https://github.com/IkrajaDev/PlunderHub/issues/1)\n"}
        calm_card_dialog("Captain's Log", lambda: captains_log.show(window, window, info, "9.9.9"),
                         wait_ms=1500)
    steps.append(_step("performance: the Captain's Log", calm_captains_log))

    def captains_log_only_after_an_update():
        """A first install marks the version seen (no log); a start after
        an update fetches it; the same version again doesn't."""
        from plunderhub.constants import APP_VERSION
        from plunderhub.ui import captains_log
        saved = dict(window.settings)
        calls = []
        real = captains_log.fetch_release_notes
        captains_log.fetch_release_notes = lambda version: calls.append(version)
        try:
            for welcome_shown, seen, expect in ((False, None, False), (True, "0.0.1", True),
                                                (True, APP_VERSION, False)):
                calls.clear()
                window.settings.pop("tip_hide", None)
                window.settings["welcome_shown"] = welcome_shown
                window.settings.pop(captains_log.SEEN_KEY, None)
                if seen:
                    window.settings[captains_log.SEEN_KEY] = seen
                if not welcome_shown:
                    window._show_welcome, real_welcome = (lambda: None), window._show_welcome
                try:
                    window._maybe_show_first_run_tip()
                finally:
                    if not welcome_shown:
                        del window._show_welcome
                deadline = time.monotonic() + 3
                while not calls and time.monotonic() < deadline:
                    from gi.repository import Gtk
                    Gtk.main_iteration_do(False)
                assert bool(calls) == expect, \
                    f"welcome_shown={welcome_shown}, seen={seen}: log {'not ' if expect else ''}asked for"
                if not welcome_shown:
                    assert window.settings.get(captains_log.SEEN_KEY) == APP_VERSION, \
                        "a first install didn't mark its version seen"
        finally:
            captains_log.fetch_release_notes = real
            window.settings.clear()
            window.settings.update(saved)
            from plunderhub.core.persistence import save_settings
            save_settings(window.settings)
    steps.append(_step("the Captain's Log comes after an update, never after a first install",
                       captains_log_only_after_an_update))

    def log_keeps_personal_details_out():
        from pathlib import Path
        from plunderhub.constants import LOG_FILE
        from plunderhub.core import bug_report
        home = str(Path.home())
        marker = "self-test privacy probe"
        logger.info(f"{marker}: {home}/x userdata/123456789/c ?key=abc123 Bearer abcdef123456 a@b.org")
        with open(LOG_FILE, encoding="utf-8", errors="replace") as f:
            written = [line for line in f if marker in line][-1]
        for leak in (home + "/", "123456789", "abc123", "abcdef123456", "a@b.org"):
            assert leak not in written, f"the log kept {leak!r}: {written.strip()}"
        system = bug_report.system_text(bug_report.system_lines())
        assert home not in system, "the system info shows the home folder"
    steps.append(_step("the log and the bug report keep personal details out",
                       log_keeps_personal_details_out))

    def hero_keeps_its_colours():
        # an animated hero plays its 2nd round on from JPEGs it kept: a
        # gdk-pixbuf (glycin) update turned them grey once
        import cairo
        from plunderhub.ui import animated_hero
        frame = cairo.ImageSurface(cairo.FORMAT_ARGB32, 64, 32)
        cr = cairo.Context(frame)
        cr.set_source_rgb(0.9, 0.2, 0.1)
        cr.paint()
        pb = animated_hero._rgb_pixbuf(frame)
        ok, data = pb.save_to_bufferv("jpeg", ["quality"], ["90"])
        back = animated_hero._from_jpeg(data)
        back.flush()
        b, g, r = bytes(back.get_data())[4 * (16 * 64 + 32):4 * (16 * 64 + 32) + 3]
        assert ok and r - b > 100, f"a kept frame came back without its colours (r={r} g={g} b={b})"
    steps.append(_step("animated heroes keep their colours", hero_keeps_its_colours))

    def check_no_css_errors():
        assert not _css_errors, "; ".join(_css_errors)

    steps.append(_step("no CSS was rejected", check_no_css_errors))
    return steps


def run(window) -> None:
    from gi.repository import GLib

    steps = _build_steps(window)
    state = {"index": 0}
    logger.info(f"[self-test] {len(steps)} steps")

    def finish() -> bool:
        if _failures:
            logger.error(f"[self-test] FAILED - {len(_failures)} step(s):")
            for failure in _failures:
                logger.error(f"[self-test]   {failure}")
        else:
            logger.info("[self-test] PASSED")
        window.get_application().quit()
        return False

    def next_step() -> bool:
        if state["index"] >= len(steps):
            return finish()
        name, action = steps[state["index"]]
        state["index"] += 1
        try:
            action()
            logger.info(f"[self-test] ok   {name}")
        except Exception as e:
            _failures.append(f"{name}: {e}")
            logger.error(f"[self-test] FAIL {name}: {e}\n"
                         f"{traceback.format_exc()}")
        GLib.timeout_add(_STEP_INTERVAL_MS, next_step)
        return False

    def give_up() -> bool:
        if state["index"] < len(steps):
            _failures.append(f"timed out at step {state['index'] + 1}/{len(steps)}")
            finish()
        return False

    GLib.timeout_add_seconds(_TIMEOUT_SECONDS, give_up)
    GLib.timeout_add(_STEP_INTERVAL_MS, next_step)


def exit_code() -> int:
    return 1 if (_failures or _css_errors) else 0


def finish_process() -> None:
    sys.exit(exit_code())
