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

"""Checks whether PlunderHub's own runtime dependencies are actually
importable, and can offer to install whatever's missing through the
current distro's package manager (with a native zenity/kdialog prompt
and a pkexec-elevated install - no GTK of PlunderHub's own required,
which matters specifically for the HARD dependencies below, since
PlunderHub can't display anything with its own UI toolkit if that
toolkit itself is what's missing).

This module is intentionally standalone - it imports nothing from
plunderhub.ui or plunderhub.gi_setup, and is safe to import and call
before either of those ever gets touched.

Two tiers, matching how the rest of the codebase already treats these:

- HARD ("gtk", "yaml"): PlunderHub can't start at all without these -
  checked once, very early, in plunderhub.py itself, before the app
  is imported.
- SOFT ("cairo", "secret"): the app already degrades gracefully
  without these (see covers_mixin.py's own guarded `import cairo`,
  and secret_store.py's guarded `import gi.repository.Secret`) - the
  running app checks for these itself and offers to fix them via a
  dismissible banner, same pattern as the update-check banner.

The package-name tables below mirror install.sh's own per-distro
INSTALL_CMD lists exactly - kept here as data, in one place, so
install.sh's tables and this module's tables can be compared directly
against each other in tests rather than silently drifting apart.
"""

import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

HARD_DEPS = ("gtk", "yaml")
SOFT_DEPS = ("cairo", "secret")

DEP_LABELS: Dict[str, str] = {
    "gtk": "PyGObject with GTK 3 bindings",
    "yaml": "PyYAML",
    "cairo": "pycairo (rounded cover/icon corners)",
    "secret": "libsecret (secure API-key storage)",
}

# manager -> dependency id -> list of package names for that manager.
# "python" itself is deliberately NOT included here - if Python 3
# weren't present, plunderhub.py could never have started running in
# the first place, so there's nothing this module could meaningfully
# check or fix about that particular case.
PACKAGE_TABLE: Dict[str, Dict[str, List[str]]] = {
    "pacman": {
        "gtk": ["python-gobject", "gtk3"],
        "yaml": ["python-yaml"],
        "cairo": ["python-cairo"],
        "secret": ["libsecret"],
    },
    "apt": {
        "gtk": ["python3-gi", "gir1.2-gtk-3.0"],
        "yaml": ["python3-yaml"],
        "cairo": ["python3-gi-cairo"],
        "secret": ["gir1.2-secret-1"],
    },
    "dnf": {
        "gtk": ["python3-gobject", "gtk3"],
        "yaml": ["python3-pyyaml"],
        "cairo": ["python3-cairo"],
        "secret": ["libsecret"],
    },
    "zypper": {
        # the GTK 3 typelib is a package of its own there
        "gtk": ["python3-gobject", "gtk3", "typelib-1_0-Gtk-3_0", "typelib-1_0-Pango-1_0",
                "typelib-1_0-GdkPixbuf-2_0"],
        "yaml": ["python3-PyYAML"],
        "cairo": ["python3-gobject-cairo", "python3-cairo"],
        "secret": ["libsecret-1-0", "typelib-1_0-Secret-1"],
    },
    "apk": {
        "gtk": ["py3-gobject3", "gtk+3.0"],
        "yaml": ["py3-yaml"],
        # pycairo itself (py3-gobject3 has the gi side of it built in)
        "cairo": ["py3-cairo"],
        "secret": ["libsecret"],
    },
    "xbps": {
        "gtk": ["python3-gobject", "gtk+3"],
        "yaml": ["python3-yaml"],
        "cairo": ["python3-cairo"],
        "secret": ["libsecret"],
    },
}


def detect_pkg_manager(os_release_path: str = "/etc/os-release") -> Tuple[str, str]:
    """(pkg_manager, distro_name). pkg_manager is one of pacman/apt/
    dnf/zypper/apk/xbps/unknown. Mirrors install.sh's own /etc/os-
    release parsing and ID/ID_LIKE family matching exactly - accepts
    a path override so tests can point this at a fixture file instead
    of the real, current machine's /etc/os-release."""
    distro_name = "unknown"
    pkg_manager = "unknown"
    fields: Dict[str, str] = {}
    try:
        for line in Path(os_release_path).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            fields[key] = value.strip().strip('"')
    except OSError:
        pass

    distro_name = fields.get("PRETTY_NAME") or fields.get("NAME") or "unknown"
    tokens = set(f"{fields.get('ID', '')} {fields.get('ID_LIKE', '')}".split())
    if "arch" in tokens:
        pkg_manager = "pacman"
    elif "fedora" in tokens or "rhel" in tokens:
        pkg_manager = "dnf"
    elif "suse" in tokens or "sles" in tokens:
        pkg_manager = "zypper"
    elif "debian" in tokens or "ubuntu" in tokens:
        pkg_manager = "apt"
    elif "alpine" in tokens:
        pkg_manager = "apk"
    elif "void" in tokens:
        pkg_manager = "xbps"
    else:
        # os-release names no known family (Garuda without its ID_LIKE
        # hook, small derivatives): go by the package manager that's there
        from plunderhub.core.utils import family_from_tools
        pkg_manager = {"arch": "pacman", "debian": "apt", "suse": "zypper", "fedora": "dnf",
                       "alpine": "apk", "void": "xbps"}.get(family_from_tools(), "unknown")
    return pkg_manager, distro_name


def check_dependency(dep_id: str) -> bool:
    """A real, isolated import attempt for one dependency ID - never
    raises. Each branch is independent of the others (e.g. checking
    "gtk" doesn't also import cairo), so one missing dependency's
    check can't mask or interfere with another's."""
    try:
        if dep_id == "gtk":
            import gi
            gi.require_version("Gtk", "3.0")
            gi.require_version("Gdk", "3.0")
            gi.require_version("Gio", "2.0")
            from gi.repository import Gdk, Gio, Gtk  # noqa: F401
            return True
        if dep_id == "yaml":
            import yaml  # noqa: F401
            return True
        if dep_id == "cairo":
            import cairo  # noqa: F401
            import gi
            # and its link to GTK (python3-gi-cairo): Ubuntu 26.04 has
            # pycairo without it - nothing the app draws itself showed
            gi.require_foreign("cairo")
            return True
        if dep_id == "secret":
            import gi
            gi.require_version("Secret", "1")
            from gi.repository import Secret  # noqa: F401
            return True
    except Exception:
        return False
    return False


def missing_dependencies(dep_ids: Sequence[str]) -> List[str]:
    return [d for d in dep_ids if not check_dependency(d)]


def _commands_for(pkg_manager: str, packages: Sequence[str]) -> Tuple[Optional[List[str]], Optional[List[str]]]:
    """(update_command_or_None, install_command) for one manager - apt
    and apk need a separate update step first, matching install.sh's
    own UPDATE_CMD handling."""
    if pkg_manager == "pacman":
        return None, ["pacman", "-S", "--needed", "--noconfirm", *packages]
    if pkg_manager == "apt":
        # --no-remove: -y would also let apt take out a conflicting package
        return ["apt-get", "update"], ["apt-get", "install", "-y", "--no-remove", *packages]
    if pkg_manager == "dnf":
        return None, ["dnf", "install", "-y", *packages]
    if pkg_manager == "zypper":
        return None, ["zypper", "install", "-y", *packages]
    if pkg_manager == "apk":
        return ["apk", "update"], ["apk", "add", *packages]
    if pkg_manager == "xbps":
        return None, ["xbps-install", "-Sy", *packages]
    return None, None


def packages_for(pkg_manager: str, dep_ids: Sequence[str]) -> List[str]:
    """The deduplicated, order-preserving list of package names this
    manager needs to satisfy every dependency in dep_ids."""
    table = PACKAGE_TABLE.get(pkg_manager, {})
    packages: List[str] = []
    seen = set()
    for dep_id in dep_ids:
        for pkg in table.get(dep_id, []):
            if pkg not in seen:
                seen.add(pkg)
                packages.append(pkg)
    return packages


def install_command_string(pkg_manager: str, dep_ids: Sequence[str]) -> Optional[str]:
    """A single, human-readable shell command line covering every
    dependency in dep_ids - shown to the person as "here's exactly
    what will run" before they agree to anything, and also the literal
    command handed to pkexec."""
    packages = packages_for(pkg_manager, dep_ids)
    if not packages:
        return None
    update_cmd, install_cmd = _commands_for(pkg_manager, packages)
    if install_cmd is None:
        return None
    if update_cmd:
        return f"{shlex.join(update_cmd)} && {shlex.join(install_cmd)}"
    return shlex.join(install_cmd)


def run_install(pkg_manager: str, dep_ids: Sequence[str], timeout: int = 600) -> Tuple[bool, str]:
    """Runs the install command elevated via pkexec (a graphical,
    polkit-backed prompt - no GTK of PlunderHub's own needed for the
    prompt itself, unlike a plain `sudo` which would need a terminal).
    Never raises - pkexec missing, a timeout, or the install itself
    failing are all reported back as (False, explanation) rather than
    an exception, since every caller of this needs to show something
    sensible to the person regardless of what went wrong."""
    command_str = install_command_string(pkg_manager, dep_ids)
    if not command_str:
        return False, f"No package mapping available for this distro ({pkg_manager})."
    if not shutil.which("pkexec"):
        return False, "pkexec not found - can't request administrator privileges."
    try:
        result = subprocess.run(
            ["pkexec", "/bin/sh", "-c", command_str],
            capture_output=True, text=True, timeout=timeout,
        )
        output = (result.stdout or "") + (result.stderr or "")
        return result.returncode == 0, output.strip()
    except subprocess.TimeoutExpired:
        return False, "Installation timed out."
    except OSError as e:
        return False, f"Couldn't run pkexec: {e}"


def _native_dialog_confirm(title: str, message: str) -> Optional[bool]:
    """True/False for an explicit answer via zenity or kdialog
    (whichever is found first), or None if neither is available -
    distinguished from False since "no dialog tool" and "person
    clicked Cancel" need different handling by the caller (the former
    falls back to a plain terminal message, the latter just gives up
    quietly)."""
    if shutil.which("zenity"):
        # --no-markup: zenity parses --text as Pango markup by default, and
        # these messages carry raw shell commands ("apt-get update && ...")
        # and package-manager output ("<pkg>") that aren't valid markup -
        # the explanation then failed to show at all.
        result = subprocess.run(
            ["zenity", "--question", "--no-markup", "--title", title, "--text", message,
             "--ok-label=Install", "--cancel-label=Cancel"],
        )
        return result.returncode == 0
    if shutil.which("kdialog"):
        result = subprocess.run(["kdialog", "--title", title, "--yesno", message])
        return result.returncode == 0
    return None


def _native_dialog_info(title: str, message: str, is_error: bool = False) -> None:
    if shutil.which("zenity"):
        flag = "--error" if is_error else "--info"
        subprocess.run(["zenity", flag, "--no-markup", "--title", title, "--text", message])
    elif shutil.which("kdialog"):
        flag = "--error" if is_error else "--msgbox"
        subprocess.run(["kdialog", "--title", title, flag, message])
    else:
        print(message, file=sys.stderr if is_error else sys.stdout)


def prompt_and_maybe_install(pkg_manager: str, dep_ids: Sequence[str], distro_name: str) -> bool:
    """The full hard-dependency flow: explain what's missing, ask via
    a native dialog (or plain stderr text if no dialog tool exists),
    and if agreed, run the install and report the outcome. Returns
    True only if every one of dep_ids is now actually importable -
    the caller (plunderhub.py) re-checks independently rather than
    trusting this return value blindly, but it's returned here too so
    callers don't have to re-derive it themselves in the common case.
    """
    labels = ", ".join(DEP_LABELS.get(d, d) for d in dep_ids)
    command_str = install_command_string(pkg_manager, dep_ids)
    title = "PlunderHub - missing dependencies"

    if pkg_manager == "unknown" or not command_str:
        message = (
            f"PlunderHub can't start - missing: {labels}.\n\n"
            f"Your distro ({distro_name}) wasn't recognized automatically, "
            "so this can't be installed automatically. Please install the "
            "above manually using your distro's package manager, then try "
            "again - see install.sh for typical package names."
        )
        _native_dialog_info(title, message, is_error=True)
        print(message, file=sys.stderr)
        return False

    message = (
        f"PlunderHub can't start - missing: {labels}.\n\n"
        f"This will run:\n{command_str}\n\n"
        "Install now? You'll be asked for your administrator password."
    )
    answer = _native_dialog_confirm(title, message)
    if answer is None:
        print(message, file=sys.stderr)
        print(
            "No zenity/kdialog found, so this can't be confirmed graphically - "
            "please run install.sh, or the command above yourself, then try again.",
            file=sys.stderr,
        )
        return False
    if not answer:
        return False

    ok, output = run_install(pkg_manager, dep_ids)
    still_missing = missing_dependencies(dep_ids)
    if not still_missing:
        _native_dialog_info(title, "Dependencies installed - starting PlunderHub…")
        return True
    failure_message = "Installation didn't finish successfully.\n\n" + (output[-2000:] if output else "")
    _native_dialog_info(title, failure_message, is_error=True)
    print(failure_message, file=sys.stderr)
    return False
