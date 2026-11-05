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

"""Options > Library: "PlunderHub in Steam" - adds PlunderHub to Steam as
a non-Steam game (core/steam_shortcut.py), or takes it out. Steam is
closed for the moment of writing and started again if it ran."""

import subprocess
from typing import List, Optional

from plunderhub.core import steam_shortcut
from plunderhub.core.persistence import save_settings
from plunderhub.gi_setup import Gtk
from plunderhub.logging_setup import logger

SETTING = "steam_shortcut_appid"


def _added(app, users: List[steam_shortcut.SteamUser]) -> Optional[int]:
    remembered = app.settings.get(SETTING)
    for user in users:
        found = steam_shortcut.find(user, remembered)
        if found is not None:
            return found
    return None


def build(sw, card: Gtk.Box) -> None:
    """The row in the settings window `sw` (its row / button helpers)."""
    _ = sw._
    users = steam_shortcut.steam_users()
    if not users:
        return                              # no Steam: nothing to offer
    status = Gtk.Label(xalign=0)
    status.set_line_wrap(True)
    status.set_max_width_chars(60)
    status.get_style_context().add_class("settings-row-sub")
    button = sw.button("", "list-add-symbolic")
    holder = {"button": button}

    def refresh():
        added = _added(sw.app, users) is not None
        status.set_text(("✓ " + _("ss_in_steam")) if added else _("ss_sub"))
        status.get_style_context().remove_class("settings-status-on")
        if added:
            status.get_style_context().add_class("settings-status-on")
        holder["button"].set_label(_("ss_remove_short") if added else _("ss_add_short"))

    def clicked(_b):
        sw.stop_edit()
        if _added(sw.app, users) is not None:
            _run(sw, users, adding=False)
        else:
            _run(sw, users, adding=True)
        refresh()

    button.connect("clicked", clicked)
    if all(u.snap for u in users):
        button.set_sensitive(False)
        status.set_text(_("ss_snap"))
    row = sw.row(card, _("ss_title"), None, [button], icon="input-gaming-symbolic", extra_lines=[status])
    row.plunderhub_first = True             # stays first when the launcher rows are rebuilt
    if button.get_sensitive():
        refresh()


def _steam_running() -> bool:
    from plunderhub.core.launch_options import launcher_pids
    return bool(launcher_pids("Steam"))


def _start_steam() -> None:
    from plunderhub.core.scan_paths import LAUNCHER_FLATPAK_IDS, resolve_launcher_invocation
    invocation = resolve_launcher_invocation("steam", LAUNCHER_FLATPAK_IDS["Steam"])
    if not invocation:
        return
    try:
        subprocess.Popen(invocation, start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        logger.warning(f"Steam shortcut: starting Steam again: {e}")


def _run(sw, users: List[steam_shortcut.SteamUser], adding: bool) -> None:
    app, _ = sw.app, sw._
    parent = sw.dialog
    if steam_shortcut.running_under_steam() and _steam_running():
        # Steam closes what it started when it quits: PlunderHub would go before writing
        app._show_info_dialog(_("ss_from_steam"), transient_for=parent)
        return
    if steam_shortcut.in_gamepad_ui():
        # closing Steam would end Game Mode itself
        app._show_info_dialog(_("ss_desktop_only"), transient_for=parent)
        return
    users = [u for u in users if not u.snap]
    was_running = _steam_running()
    if was_running:
        if not app._confirm_yes_no(_("ss_close_q"), transient_for=parent,
                                   title=_("ss_add") if adding else _("ss_remove"),
                                   yes_label=_("ss_close_ok")):
            return
        if not app.close_launcher_asking("Steam", parent):
            return
    appid = None
    try:
        for user in users:
            if adding:
                appid = steam_shortcut.add(user)
            else:
                steam_shortcut.remove(user, app.settings.get(SETTING))
    except Exception as e:
        logger.warning(f"Steam shortcut: {'adding' if adding else 'removing'}: {e}")
        app._show_info_dialog(_("ss_failed").format(error=e), transient_for=parent)
        if was_running:
            _start_steam()
        return
    if adding:
        app.settings[SETTING] = appid
    else:
        app.settings.pop(SETTING, None)
    save_settings(app.settings)
    if was_running:
        _start_steam()
    if adding and any(u.flatpak for u in users) and steam_shortcut.flatpak_steam_can_start_host() is False:
        if app._confirm_yes_no(_("ss_flatpak_q"), transient_for=parent, title=_("ss_title"),
                               yes_label=_("ss_flatpak_ok")):
            try:
                subprocess.run(["flatpak", "override", "--user", "--talk-name=org.freedesktop.Flatpak",
                                "com.valvesoftware.Steam"], check=True, timeout=30)
            except (OSError, subprocess.SubprocessError) as e:
                logger.warning(f"Steam shortcut: Flatpak permission: {e}")
                app._show_info_dialog(_("ss_failed").format(error=e), transient_for=parent)
                return
    app._show_info_dialog(_("ss_added") if adding else _("ss_removed"), transient_for=parent)
