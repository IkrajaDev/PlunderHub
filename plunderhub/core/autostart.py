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

"""Manages the XDG autostart .desktop file (~/.config/autostart/) that
makes PlunderHub launch automatically with the desktop session, for
the Settings -> "Run on startup, minimized" option.

Every function here is guarded and never raises - a failure to write
this file (a read-only home directory, an unusual XDG setup, etc.)
should never crash the app or block saving the rest of Settings; it
just leaves the checkbox effectively a no-op until the person retries,
the same "degrade quietly" philosophy secret_store.py already follows
for keyring failures."""

import os
import stat
import sys
from pathlib import Path

from plunderhub.logging_setup import logger

_AUTOSTART_DIR = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "autostart"
_AUTOSTART_FILE = _AUTOSTART_DIR / "plunderhub.desktop"

_DESKTOP_ENTRY_TEMPLATE = """[Desktop Entry]
Type=Application
Name=PlunderHub
Comment=Unified game launcher
Exec={exec_cmd} --minimized
Icon=PlunderHub
Terminal=false
Categories=Game;
X-GNOME-Autostart-enabled=true
"""


def _resolve_exec_command() -> str:
    """The exact command that re-launches this same install, whether
    PlunderHub is running as the installed /usr/bin/plunderhub (or
    /usr/local/bin, per install.sh) command, or straight from a
    source checkout via "python3 plunderhub.py" - re-deriving
    install.sh/PKGBUILD's own "plunderhub" command name would
    silently break for anyone not using exactly that installation
    method, so this reads sys.argv[0] (the path this very process was
    actually launched with) instead. plunderhub.py itself carries a
    "#!/usr/bin/env python3" shebang and is installed executable
    (install -Dm755) either way, so it can normally run on its own -
    an explicit sys.executable prefix is only added as a fallback for
    a plain, non-executable source checkout."""
    script = os.path.abspath(sys.argv[0])
    try:
        is_executable = bool(os.stat(script).st_mode & stat.S_IXUSR)
    except OSError:
        is_executable = False
    if is_executable:
        return _quote_exec_arg(script)
    return f"{_quote_exec_arg(sys.executable)} {_quote_exec_arg(script)}"


def _quote_exec_arg(arg: str) -> str:
    """Quotes one Exec= argument per the Desktop Entry spec: inside the
    quotes ", `, $ and \\ take a backslash, then the value's own string
    escaping doubles every backslash, and % (a field code) becomes %%.
    A plain f'"{path}"' broke on any install path holding one of them."""
    for ch in ("\\", '"', "`", "$"):
        arg = arg.replace(ch, "\\" + ch)
    arg = arg.replace("\\", "\\\\").replace("%", "%%")
    return f'"{arg}"'


def enable_autostart() -> bool:
    """Writes the autostart .desktop file. Always includes
    --minimized - this file only exists at all because the person
    turned on "Run on startup, minimized", so unconditionally starting
    minimized (rather than also checking a second, separate setting
    at launch time) keeps the file's own contents the single source of
    truth for what happens when the session starts it."""
    try:
        _AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
        content = _DESKTOP_ENTRY_TEMPLATE.format(exec_cmd=_resolve_exec_command())
        _AUTOSTART_FILE.write_text(content, encoding="utf-8")
        return True
    except Exception as e:
        logger.warning(f"Couldn't enable run-on-startup: {e}")
        return False


def disable_autostart() -> bool:
    try:
        _AUTOSTART_FILE.unlink(missing_ok=True)
        return True
    except Exception as e:
        logger.warning(f"Couldn't disable run-on-startup: {e}")
        return False


def is_autostart_enabled() -> bool:
    return _AUTOSTART_FILE.exists()
