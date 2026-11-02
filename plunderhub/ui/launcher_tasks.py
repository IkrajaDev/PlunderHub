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
"""Installing and removing launchers without a terminal.

Everything runs in a progress window shaped like the update window.
Root goes through one core.root_helper session per install or removal -
one password, whatever the package manager - and Flatpak through
libflatpak (or the flatpak CLI when the library isn't there), whose own
polkit rules decide about a password.

  Arch      repositories: pacman -S / -Rns
            AUR: paru or yay, else makepkg - their sudo is the session
  Debian, Ubuntu  apt-get, after the recipe's i386 architecture / PPA
  Fedora    dnf, after the recipe's RPM Fusion / COPR
  openSUSE  zypper
  Flatpak   Flathub, system-wide

Before offering the system package, native_plan() asks whether the
repositories have it at all; if not, the dialog says so and points to
the Flatpak.

Cancelling an install undoes it: whatever it had installed by then is
removed again - repositories it added too - and what it had downloaded
is deleted. Downloads stop at once. apt's, dnf's and zypper's actual
install (like flatpak's deploy) is never cut short: Cancel waits for it,
then undoes it.
"""

import json
import math
import os
import re
import shutil
import signal
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from gi.repository import GObject

from plunderhub.core.persistence import save_settings
from plunderhub.core.root_helper import ACTION, FLATPAK_INSTALL, PACMAN_TMPDB, RootError, RootSession
from plunderhub.core.scan_paths import LAUNCHER_FLATPAK_IDS, resolve_launcher_invocation
from plunderhub.core.tools import TOOLS_BY_NAME, installed_path, reboot_needed
from plunderhub.core.utils import detect_linux_distro_family, immutable_system, which
from plunderhub.gi_setup import Gdk, GdkPixbuf, Gio, GLib, Gtk, pixbuf_at_size, theme_icon_pixbuf
from plunderhub.launchers.registry import (
    ARCH_AUR_PACKAGES, LAUNCHER_ICON_NAMES, LAUNCHER_REGISTRY, NATIVE_PACKAGE_TOOLS, NATIVE_RECIPES,
    NativeRecipe, bundled_launcher_icon,
)
from plunderhub.logging_setup import logger
from plunderhub.ui.progress_card import (
    RING_ICON, BatchList, LogPanel, ProgressRing, Stepper, card_dialog, pill_buttons, show_final, status_label,
)

try:
    import gi
    gi.require_version("Flatpak", "1.0")
    from gi.repository import Flatpak
except (ImportError, ValueError):
    Flatpak = None


def _system_installation():
    """libflatpak's system installation - None where it can't be used:
    Alpine's flatpak package has the typelib but not libflatpak itself,
    so it loads and then can't find a single function (the CLI then)."""
    if Flatpak is None:
        return None
    try:
        return Flatpak.Installation.new_system(None)
    except GLib.Error as e:
        logger.info(f"libflatpak unusable, using the flatpak command: {e.message}")
        return None

# pacman's progress lines, in order, mapped onto the bar - it prints no
# download percentages when its output isn't a terminal.
_PACMAN_INSTALL = [
    ("resolving dependencies", "download", 0.04),
    ("Retrieving packages", "download", 0.10),
    ("checking keys in keyring", "install", 0.50),
    ("checking package integrity", "install", 0.56),
    ("loading package files", "install", 0.62),
    ("checking for file conflicts", "install", 0.66),
    ("checking available disk space", "install", 0.70),
    ("Processing package changes", "install", 0.74),
    ("installing ", "install", 0.80),
    ("Running post-transaction hooks", "install", 0.90),
]
def _aur_build(package: Optional[str] = None):
    """makepkg's steps (an AUR helper runs it too). pacman's own lines
    mean the install only once the package is made ("Finished making:
    <package>") - before, pacman is installing what it's built with (go,
    rust...) or AUR dependencies made first, and taking those for the
    install skipped the Build step. A package made before isn't made
    again: paru says "<package>-<version> is up to date -- skipping
    build", yay "... already made -- skipping build", makepkg "A package
    has already been built" - those count as made too."""
    made = ((f"Finished making: {package} " if package else "Finished making"),
            "-- skipping build", "has already been built")
    return [
        ("Making package", "download", 0.06),
        ("Retrieving sources", "download", 0.12),
        ("Installing missing dependencies", "download", 0.18),
        ("Validating source", "download", 0.30),
        ("Extracting sources", "build", 0.36),
        ("Starting build()", "build", 0.42),
        ("Starting package()", "build", 0.58),
        ("Creating package", "build", 0.66),
        ("Finished making", "build", 0.72),
    ] + [(m, "install", max(f, 0.74), made) for m, _s, f in _PACMAN_INSTALL[2:]]


_AUR_BUILD = _aur_build()
_PACMAN_REMOVE = [
    ("checking dependencies", "remove", 0.10),
    ("Processing package changes", "remove", 0.40),
    ("removing ", "remove", 0.55),
    ("Running post-transaction hooks", "remove", 0.85),
]
# apt, dnf and zypper: the download and the install run as separate
# commands, so each has its own list.
_DOWNLOAD = [
    ("Get:", "download", 0.30),                   # apt
    ("Downloading Packages", "download", 0.25),   # dnf 4
    ("Downloading", "download", 0.30),            # dnf 5
    ("Retrieving:", "download", 0.30),            # zypper
]
_INSTALL = [
    ("Preparing to unpack", "install", 0.62),     # apt
    ("Unpacking", "install", 0.68),
    ("Setting up", "install", 0.82),
    ("Running transaction", "install", 0.66),     # dnf
    ("Installing", "install", 0.76),              # dnf, zypper
    ("Running scriptlet", "install", 0.86),
    ("Verifying", "install", 0.92),
]
_REMOVE = [
    ("Removing", "remove", 0.50),
    ("Erasing", "remove", 0.50),
]


# --- what's installed, and how ------------------------------------------------

@dataclass
class LauncherState:
    installed: bool
    method: Optional[str] = None            # "flatpak" | "native"
    package: Optional[str] = None           # the pacman/dpkg/rpm package that provides it
    flatpak_id: Optional[str] = None
    locked_by: List[str] = field(default_factory=list)   # bundles that require the package
    partial: bool = False                   # a tool's extras the repositories have aren't all installed
    source_label: str = ""


def display_name(name: str) -> str:
    return name


def _binary(name: str) -> Optional[str]:
    tool = TOOLS_BY_NAME.get(name)
    if tool is not None:
        return tool.binary
    return next((ldef.binary for ldef in LAUNCHER_REGISTRY if ldef.name == name), None)


# Launchers and Shipwright's tools are installed the same way; these
# find either one's packages.

def _arch_package(name: str) -> Optional[str]:
    tool = TOOLS_BY_NAME.get(name)
    return tool.arch if tool is not None else ARCH_AUR_PACKAGES.get(name)


def _recipe(family: str, name: str) -> Optional[NativeRecipe]:
    tool = TOOLS_BY_NAME.get(name)
    if tool is not None:
        return tool.recipes.get(family)
    return NATIVE_RECIPES.get(family, {}).get(name)


def _flatpak_ids(name: str) -> List[str]:
    tool = TOOLS_BY_NAME.get(name)
    return list(tool.flatpak) if tool is not None else LAUNCHER_FLATPAK_IDS.get(name, [])


def _package_installed(family: str, package: str) -> bool:
    if family == "arch":
        return _run_quiet(["pacman", "-Q", package])[0] == 0
    if family == "debian":
        code, out = _run_quiet(["dpkg-query", "-W", "-f", "${db:Status-Abbrev}", package])
        return code == 0 and out.startswith("ii")
    return _run_quiet(["rpm", "-q", package])[0] == 0


def _satisfied(family: str, package: str) -> bool:
    """Whether something installed already gives the package - itself,
    or another package that provides it (lib32-ocl-icd for
    lib32-opencl-icd-loader). Asking the package manager for the name
    anyway can end in a conflict with that other package, which fails
    the whole install (pacman, zypper) or removes it (apt -y)."""
    if family == "arch":
        return _run_quiet(["pacman", "-T", package])[0] == 0
    if family in ("fedora", "suse"):
        # "name.i686" is a package spec, a bare name may be a capability
        return (_run_quiet(["rpm", "-q", package])[0] == 0
                or _run_quiet(["rpm", "-q", "--whatprovides", package])[0] == 0)
    # apt: a name only provided (virtual) has no candidate, so _apt_has
    # leaves it out already
    return _package_installed(family, package)


def _not_satisfied(family: str, packages) -> List[str]:
    return [p for p in packages if not _satisfied(family, p)]


def _missing_extras(family: str, name: str) -> List[str]:
    """The tool's extras that the repositories have but aren't installed."""
    has = {"arch": _pacman_has, "debian": _apt_has, "fedora": _dnf_has, "suse": _zypper_has}.get(family)
    if has is None:
        return []
    return [p for p in _extras(family, name) if not _satisfied(family, p) and has(p)]


def _extras(family: str, name: str) -> List[str]:
    tool = TOOLS_BY_NAME.get(name)
    return list(tool.extras.get(family, ())) if tool is not None else []


def launch_argv(name: str) -> Optional[List[str]]:
    """How to open the launcher or tool - None for a tool without a
    window of its own, or when it isn't installed."""
    tool = TOOLS_BY_NAME.get(name)
    if tool is not None and not tool.gui:
        return None
    binary = _binary(name)
    if tool is not None and tool.opens:
        # the binary has no window: its interface, or else the Flatpak
        path = next((p for p in map(which, tool.opens) if p), None)
        if path:
            return [path] + list(tool.gui_args)
        binary = None
    argv = resolve_launcher_invocation(binary, _flatpak_ids(name))
    if argv is not None and tool is not None:
        argv = argv + list(tool.gui_args)
    return argv


def _run_quiet(cmd: List[str], timeout: int = 15) -> Tuple[int, str]:
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             env={**os.environ, "LC_ALL": "C"})
        return out.returncode, out.stdout
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def _run_quiet_all(cmd: List[str], timeout: int = 30) -> Tuple[int, str]:
    """_run_quiet with stderr too - where the package managers explain."""
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                             env={**os.environ, "LC_ALL": "C"})
        return out.returncode, out.stdout + out.stderr
    except (OSError, subprocess.SubprocessError):
        return 1, ""


def _owning_package(family: str, path: str) -> Optional[str]:
    """The package the launcher's binary belongs to, if the package
    manager knows it."""
    candidates = [path, os.path.realpath(path)]
    for candidate in dict.fromkeys(candidates):
        if family == "arch":
            code, out = _run_quiet(["pacman", "-Qqo", candidate])
        elif family == "debian":
            code, out = _run_quiet(["dpkg-query", "-S", candidate])
            # "steam-installer: /usr/games/steam" - or several, comma-separated
            out = out.split(":", 1)[0].split(",")[0] if code == 0 else ""
        elif family in ("fedora", "suse"):
            code, out = _run_quiet(["rpm", "-qf", "--qf", "%{NAME}\n", candidate])
        else:
            return None
        if code == 0 and out.strip():
            return out.split()[0]
    return None


def launcher_state(name: str) -> LauncherState:
    tool = TOOLS_BY_NAME.get(name)
    if tool is not None and tool.by_package:
        family = detect_linux_distro_family()
        package = _installed_from_recipe(family, name)
        if package is None:
            return LauncherState(installed=False)
        return LauncherState(installed=True, method="native", package=package,
                             source_label="pacman" if family == "arch" else NATIVE_PACKAGE_TOOLS.get(family, ""),
                             partial=not tool.removable and bool(_missing_extras(family, name)),
                             locked_by=(removal_plan(family, package, _installed_extras(family, name))[1]
                                        if tool.removable else []))
    if tool is not None:
        # a Vulkan layer has no binary - its layer file shows it's there
        path = installed_path(tool)
        invocation = [path] if path else resolve_launcher_invocation(None, tool.flatpak)
    else:
        binary = _binary(name)
        invocation = resolve_launcher_invocation(binary, LAUNCHER_FLATPAK_IDS.get(name, []))
        path = which(binary) if binary else None
    if invocation is None:
        return LauncherState(installed=False)
    if invocation[:2] == ["flatpak", "run"]:
        return LauncherState(installed=True, method="flatpak", flatpak_id=invocation[2], source_label="Flatpak")
    state = LauncherState(installed=True, method="native")
    family = detect_linux_distro_family()
    # The package that really owns the binary - e.g. Heroic may be
    # heroic-games-launcher or heroic-games-launcher-bin.
    state.package = _owning_package(family, path) if path else None
    if state.package:
        state.locked_by = removal_plan(family, state.package, _installed_extras(family, name))[1]
        state.source_label = "pacman" if family == "arch" else NATIVE_PACKAGE_TOOLS.get(family, "")
    return state


# --- install plans ---------------------------------------------------------------

@dataclass
class Plan:
    kind: str                     # pacman | aur_helper | aur_makepkg | apt | dnf | zypper | flatpak
    tool: str = ""
    package: str = ""
    helper: str = ""
    recipe: Optional[NativeRecipe] = None
    add_repo: bool = False        # the recipe's ppa / copr / rpmfusion, because the repos lack it
    base_devel: bool = False      # makepkg without the tools it builds with
    extras: List[str] = field(default_factory=list)   # a tool's 32-bit build, installed alongside
    post: List[str] = field(default_factory=list)     # root_helper actions once it's installed


@dataclass
class Unavailable:
    """Why the system package can't be offered - an i18n key, and the
    values it's formatted with."""
    reason: str
    args: dict = field(default_factory=dict)


def _os_release_ids() -> str:
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    ids = []
    for line in text.splitlines():
        key, _, value = line.partition("=")
        if key.strip() in ("ID", "ID_LIKE"):
            ids.append(value.strip().strip('"').lower())
    return " ".join(ids)


def _ubuntu_codename() -> str:
    """The Ubuntu release under this system (Mint and the like say theirs in UBUNTU_CODENAME)."""
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    values = {}
    for line in text.splitlines():
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').lower()
    return values.get("UBUNTU_CODENAME") or values.get("VERSION_CODENAME", "")


def _ppa_has(ppa: str, package: str) -> Optional[bool]:
    """Whether the PPA builds package for this Ubuntu release - a PPA
    often skips the older ones (Faugus' has no 22.04). None: can't tell."""
    from plunderhub.core.utils import http_get_json
    owner, _, name = ppa.partition("/")
    series = _ubuntu_codename()
    if not (owner and name and series):
        return None
    data = http_get_json(f"https://api.launchpad.net/1.0/~{owner}/+archive/ubuntu/{name}"
                         f"?ws.op=getPublishedSources&source_name={package}&status=Published"
                         f"&distro_series=https://api.launchpad.net/1.0/ubuntu/{series}",
                         {"Accept": "application/json"}, timeout=10, retries=2)
    if not isinstance(data, dict) or "total_size" not in data:
        return None
    return data["total_size"] > 0


# Each answers True (the repositories have it), False (they certainly
# don't) or None (couldn't tell - e.g. no package lists cached yet). None
# is treated as available: the install itself then finds out, and its
# error offers the Flatpak.

def _pacman_has(pkg: str) -> Optional[bool]:
    return _run_quiet(["pacman", "-Si", pkg])[0] == 0


def _aur_has(pkg: str) -> Optional[bool]:
    url = f"https://aur.archlinux.org/rpc/v5/info?arg[]={urllib.parse.quote(pkg)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "PlunderHub"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.load(resp).get("resultcount", 0) > 0
    except (OSError, ValueError):
        return None


def _aur_deps_unbuildable(pkg: str) -> List[str]:
    """The AUR package's dependencies that are neither installed nor in
    the repositories - makepkg alone can't get them (they're AUR packages
    themselves), only paru or yay can."""
    url = f"https://aur.archlinux.org/rpc/v5/info?arg[]={urllib.parse.quote(pkg)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "PlunderHub"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            results = json.load(resp).get("results") or []
    except (OSError, ValueError):
        return []
    if not results:
        return []
    deps = list(results[0].get("Depends") or []) + list(results[0].get("MakeDepends") or [])
    missing = []
    for dep in deps:
        # "pacman -T" knows what's installed (providers included), "-Sp"
        # what the repositories can bring
        if _run_quiet(["pacman", "-T", dep])[0] == 0:
            continue
        if _run_quiet(["pacman", "-Sp", "--print-format", "%n", dep])[0] == 0:
            continue
        missing.append(dep)
    return missing


def _apt_has(pkg: str) -> Optional[bool]:
    try:
        if not any(Path("/var/lib/apt/lists").glob("*_Packages*")):
            return None
    except OSError:
        return None
    code, out = _run_quiet(["apt-cache", "policy", pkg])
    if code != 0:
        return None
    for line in out.splitlines():
        if line.strip().startswith("Candidate:"):
            return line.split(":", 1)[1].strip() != "(none)"
    return False    # apt-cache prints nothing for a name it has never heard of


def _dnf_has(pkg: str) -> Optional[bool]:
    # Cache only: a metadata download could take minutes. Without a
    # cache dnf fails, which is "couldn't tell".
    code, out = _run_quiet(["dnf", "-q", "-C", "repoquery", "--available", pkg], timeout=30)
    if code != 0:
        return None
    return bool(out.strip())


def _zypper_has(pkg: str) -> Optional[bool]:
    code, _out = _run_quiet(["zypper", "--non-interactive", "--no-refresh", "search", "--match-exact",
                             "-t", "package", pkg], timeout=30)
    if code == 0:
        return True
    return False if code == 104 else None     # 104: ZYPPER_EXIT_INF_CAP_NOT_FOUND


def _repo_hint(name: str, family: str) -> Unavailable:
    """What to tell someone whose repositories lack the launcher -
    Steam usually just needs a repository switched on."""
    if name == "Steam":
        if family == "arch":
            return Unavailable("task_native_hint_multilib")
        if family == "debian" and "ubuntu" not in _os_release_ids():
            return Unavailable("task_native_hint_contrib")
        if family == "debian":
            return Unavailable("task_native_hint_multiverse")
        if family == "suse":
            return Unavailable("task_native_hint_nonoss")
    if family == "debian":
        # often in a component that isn't switched on
        return Unavailable("task_native_not_in_components", {"name": name})
    return Unavailable("task_native_not_in_repos", {"name": name})


# What to say instead when there's no Flatpak to point to.
_NO_FLATPAK = {
    "task_native_unavailable": "task_native_unavailable_noflat",
    "task_native_not_in_repos": "task_native_not_in_repos_noflat",
    "task_native_no_pkexec": "task_native_no_pkexec_noflat",
    "task_native_not_in_components": "task_native_not_in_components_noflat",
    "task_native_immutable": "task_native_immutable_noflat",
    "task_native_aur_needs_helper": "task_native_aur_needs_helper_noflat",
}


def native_plan(name: str):
    """A Plan, or Unavailable. Can take a few seconds (it asks the
    package manager, and on Arch the AUR) - call it off the GTK thread."""
    tool = TOOLS_BY_NAME.get(name)
    system = immutable_system()
    if system:
        # rpm-ostree / SteamOS / transactional-update: not through here
        result = Unavailable("task_native_immutable", {"system": system})
    elif tool is not None and tool.headers and not _kernel_headers():
        return Unavailable("task_native_no_headers")
    else:
        result = _native_plan(name)
    if isinstance(result, Unavailable) and not _flatpak_ids(name) and result.reason in _NO_FLATPAK:
        result = Unavailable(_NO_FLATPAK[result.reason], result.args)
    if isinstance(result, Plan) and tool is not None:
        result.post = list(tool.post_install)
    return result


def _kernel_headers() -> bool:
    """A DKMS driver is built against the running kernel's headers."""
    return os.path.isdir(f"/usr/lib/modules/{os.uname().release}/build") or \
        os.path.isdir(f"/lib/modules/{os.uname().release}/build")


def _post(w: "TaskWindow", session: RootSession, actions: List[str]):
    """root_helper actions after an install (or before a removal) -
    switching LACT's daemon on, reloading the udev rules."""
    for action in actions:
        w.run_command(session.command([ACTION, action]), [], env=_root_env(session), interruptible=False,
                      check=False)


def _native_plan(name: str):
    family = detect_linux_distro_family()
    if family == "arch":
        pkg = _arch_package(name)
        if not pkg:
            return Unavailable("task_native_unavailable")
        if not shutil.which("pkexec"):
            return Unavailable("task_native_no_pkexec")
        # lib32-* only when multilib is switched on - else just the 64-bit one.
        # Skips what's already there, also through another package's
        # "provides" - --needed only knows exact names (see _satisfied).
        wanted = _not_satisfied(family, _extras(family, name))
        in_repos = [p for p in wanted if _pacman_has(p)]
        if _pacman_has(pkg):
            return Plan("pacman", tool="pacman", package=pkg, extras=in_repos)
        if name == "Steam":
            # Arch's Steam is in multilib, never in the AUR.
            return _repo_hint(name, family)
        if _aur_has(pkg) is False:
            return _repo_hint(name, family)
        helper = next((h for h in ("paru", "yay") if shutil.which(h)), "")
        if helper:
            extras = in_repos + [p for p in wanted if p not in in_repos and _aur_has(p)]
            return Plan("aur_helper", tool=helper, package=pkg, helper=helper, extras=extras)
        if shutil.which("makepkg"):
            unbuildable = _aur_deps_unbuildable(pkg)
            if unbuildable:
                return Unavailable("task_native_aur_needs_helper", {"deps": ", ".join(unbuildable[:4])})
            return Plan("aur_makepkg", tool="makepkg", package=pkg, base_devel=not shutil.which("fakeroot"),
                        extras=in_repos)
        return Unavailable("task_native_unavailable")

    recipe = _recipe(family, name)
    if recipe is None:
        return Unavailable("task_native_unavailable")
    if not shutil.which("pkexec"):
        return Unavailable("task_native_no_pkexec")
    has = {"debian": _apt_has, "fedora": _dnf_has, "suse": _zypper_has}[family]
    kind = {"debian": "apt", "fedora": "dnf", "suse": "zypper"}[family]
    tool = NATIVE_PACKAGE_TOOLS.get(family, family)
    unsure = None
    # dnf can't be asked reliably without downloading metadata, so the
    # .i686 build is tried after the install, and its absence shrugged off.
    extras = _not_satisfied(family, _extras(family, name))
    if family in ("debian", "suse"):
        # apt and zypper refuse the whole command over one unknown name
        extras = [p for p in extras if has(p)]
    for pkg in recipe.packages:
        found = has(pkg)
        if found:
            return Plan(kind, tool=tool, package=pkg, recipe=recipe, extras=extras)
        if found is None and unsure is None:
            unsure = pkg
    # Not in the repositories as they are - but maybe in the one the
    # recipe adds.
    can_add = ((recipe.ppa and "ubuntu" in _os_release_ids()
                and _ppa_has(recipe.ppa, recipe.packages[0]) is not False)
               or recipe.copr or recipe.rpmfusion)
    if can_add:
        return Plan(kind, tool=tool, package=recipe.packages[0], recipe=recipe, add_repo=True, extras=extras)
    if unsure is not None:
        return Plan(kind, tool=tool, package=unsure, recipe=recipe, extras=extras)
    return _repo_hint(name, family)


def flatpak_plan(name: str) -> Optional[Plan]:
    ids = _flatpak_ids(name)
    if not ids or not shutil.which("flatpak"):
        return None
    return Plan("flatpak", tool="Flathub", package=ids[0])


# --- the progress window -----------------------------------------------------------

class TaskError(Exception):
    pass


class Cancelled(Exception):
    pass


class TaskWindow:
    """Steps, the accent bar, a status line and collapsible details - the
    update window's layout. `work(window)` runs on a thread and reports
    back through step() / progress() / log()."""

    def __init__(self, app, parent, name: str, title: str, subtitle: str, steps: List[str],
                 work: Callable, done_title: str, done_text: str, on_success: Callable,
                 on_closed: Callable, launch_argv: Optional[Callable] = None,
                 alternative: Optional[Callable] = None, batch: Optional[List[Tuple[str, str]]] = None):
        self.app = app
        self._ = app._
        self.steps = steps
        self.work = work
        self.done_title = done_title
        self.done_text = done_text
        self.on_success = on_success
        self.on_closed = on_closed
        self.launch_argv = launch_argv
        # Offered when the task fails - installing the Flatpak instead.
        self.alternative = alternative
        self.cancellable = Gio.Cancellable()
        self.can_cancel = True
        # False while a step runs that must not be cut short (apt's or
        # dnf's actual install): Cancel then waits for it to finish.
        self.interruptible = True
        self.finished = False
        self.current: Optional[str] = None
        self.target = 0.0
        self.shown = 0.0
        self.creep_from: Optional[float] = None
        self.proc: Optional[subprocess.Popen] = None
        self.session: Optional[RootSession] = None
        # A batch: one root session for all its items (one password), and
        # each item's progress mapped onto its share of the ring.
        self.shared_session: Optional[RootSession] = None
        self.span: Optional[Tuple[float, float]] = None
        # Quitting PlunderHub asks while it runs (App.request_quit)
        app.add_quit_blocker(self)

        self._step_labels = labels = {"auth": self._("step_password"), "prepare": self._("step_prepare"),
                  "download": self._("step_download"), "build": self._("step_build"),
                  "install": self._("step_install"), "remove": self._("step_remove"),
                  "done": self._("step_done")}
        # The update window's card (see progress_card): the launcher's
        # icon in a progress ring, the steps in a row, the log behind
        # Details, pill buttons.
        dlg, column = card_dialog(parent, title)
        dlg.connect("delete-event", self._on_delete)
        self.dialog = dlg
        self.ring = ProgressRing(launcher_pixbuf(name, RING_ICON))
        if name in TOOLS_BY_NAME:
            self.ring.symbolic = TOOLS_BY_NAME[name].icon
        column.pack_start(self.ring, False, False, 0)
        self.title = Gtk.Label(label=title)
        self.title.set_line_wrap(True)
        self.title.set_max_width_chars(30)
        self.title.set_justify(Gtk.Justification.CENTER)
        self.title.get_style_context().add_class("home-dialog-title")
        column.pack_start(self.title, False, False, 0)
        sub = Gtk.Label(label=subtitle)
        sub.get_style_context().add_class("home-update-versions")
        column.pack_start(sub, False, False, 0)
        self.stepper = Stepper([(step, labels[step]) for step in steps + ["done"]])
        column.pack_start(self.stepper, False, False, 0)
        self.status = status_label(self._("update_status_preparing"))
        column.pack_start(self.status, False, False, 0)
        self.batch = BatchList(batch) if batch else None
        if self.batch is not None:
            column.pack_start(self.batch.widget, False, False, 0)

        self.log_panel = LogPanel(self._("update_details"))
        column.pack_start(self.log_panel.revealer, False, False, 0)
        buttons = Gtk.Box(spacing=8)
        buttons.set_margin_top(16)
        buttons.pack_start(self.log_panel.toggle, False, False, 0)
        self.cancel_btn, self.alt_btn, self.close_btn, self.launch_btn = pill_buttons(app, (
            (self._("dialog_cancel"), "ghost"), (self._("task_install_flatpak_instead"), "ghost"),
            (self._("close_btn"), "ghost"), (self._("task_launch").format(name=name), "accent")))
        self.cancel_btn.connect("clicked", lambda *_: self._cancel())
        self.close_btn.connect("clicked", lambda *_: self._close())
        self.launch_btn.connect("clicked", lambda *_: self._launch())
        self.alt_btn.connect("clicked", lambda *_: self._try_alternative())
        for b in (self.launch_btn, self.close_btn, self.alt_btn, self.cancel_btn):
            buttons.pack_end(b, False, False, 0)
        column.pack_start(buttons, False, False, 0)

        dlg.get_content_area().show_all()
        self.cancel_btn.show()
        dlg.show()
        dlg.set_focus(None)
        self.anim = GLib.timeout_add(40, self._animate)
        threading.Thread(target=self._thread, daemon=True).start()

    # Called from the worker thread - each hops onto the GTK thread.

    def set_steps(self, steps: List[str]) -> bool:
        """The steps shown, once a batch knows what it'll run (main thread)."""
        self.steps = steps
        self.stepper.set_steps([(step, self._step_labels[step]) for step in steps + ["done"]])
        return False

    def step(self, step: str, status: Optional[str] = None):
        GLib.idle_add(self._step, step, status)

    def progress(self, fraction: float, status: Optional[str] = None, creep: bool = False):
        if self.span is not None:
            fraction = self.span[0] + fraction * self.span[1]
        GLib.idle_add(self._progress, fraction, status, creep)

    def log(self, line: str):
        GLib.idle_add(self._log, line)

    def lock_cancel(self):
        """From here on the work finishes rather than stopping half-way."""
        self.can_cancel = False
        GLib.idle_add(self.cancel_btn.hide)

    def check_cancelled(self):
        if self.cancellable.is_cancelled():
            raise Cancelled()

    def cleaning_up(self):
        """After Cancel: what the task did is being undone."""
        GLib.idle_add(self.status.set_text, self._("task_status_cleaning"))

    @contextmanager
    def root_session(self):
        """Asks for the password once; the commands in the block run as
        root through session.command(), with session.env. Inside a
        batch the batch's session is reused - no second password."""
        if self.shared_session is not None:
            yield self.shared_session
            return
        self.step("auth", self._("update_status_auth"))
        self.progress(0.02, creep=True)
        session = RootSession()
        self.session = session
        try:
            try:
                session.start()
            except (RootError, OSError) as e:
                self.check_cancelled()
                if isinstance(e, RootError):
                    for line in e.tail:      # pkexec's own words, in Details
                        self.log(line)
                    logger.warning(f"pkexec exited with {e.code}: {' | '.join(e.tail)}")
                    raise TaskError(self.friendly_error(e.code, e.tail))
                raise TaskError(str(e))
            self.check_cancelled()
            self.log("Root helper ready")
            yield session
        finally:
            session.close()
            self.session = None

    def run_command(self, cmd: List[str], milestones, env=None, cwd=None, cleanup: bool = False,
                    interruptible: bool = True, check: bool = True, ok_codes: Tuple[int, ...] = (0,)):
        """Runs cmd, following its output against milestones. A cleanup
        command runs after Cancel, so neither stops for it nor fails the
        task. One that isn't interruptible runs to its end even after
        Cancel, which then takes effect once it's done. Without check, a
        failing command only gets logged."""
        if not cleanup:
            self.check_cancelled()
        self.log("$ " + " ".join(cmd))
        self.interruptible = interruptible or cleanup
        try:
            self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         errors="replace", bufsize=1, stdin=subprocess.DEVNULL,
                                         env=env, cwd=cwd, start_new_session=True)
        except OSError as e:
            self.interruptible = True
            if cleanup:
                logger.warning(f"Clean-up command {cmd} failed: {e}")
                return
            raise TaskError(str(e))
        fraction, tail = 0.0, []
        # milestones that count only after a line of their own was seen
        opened = set()
        for raw in self.proc.stdout:
            line = raw.rstrip()
            if not line:
                continue
            tail = (tail + [line])[-25:]
            self.log(line)
            if cleanup:
                continue
            for marker, step, value, *after in milestones:
                # after: a line (or one of several) that must come first
                gates = (after[0],) if after and isinstance(after[0], str) else tuple(after[0]) if after else ()
                if any(gate in line for gate in gates):
                    opened.add(gates)
                if gates and gates not in opened:
                    continue
                if marker in line and value > fraction:
                    fraction = value
                    if step != self.current:
                        self.step(step)
                    self.progress(fraction, creep=True)
            self.progress(fraction, line.strip(": =>"), creep=True)
        code = self.proc.wait()
        self.proc = None
        self.interruptible = True
        if cleanup:
            if code != 0:
                logger.warning(f"Clean-up command {cmd} exited with {code}")
            return
        if self.cancellable.is_cancelled():
            raise Cancelled()
        if code not in ok_codes:
            if not check:
                logger.warning(f"{cmd} exited with {code}")
                return
            raise TaskError(self.friendly_error(code, tail))

    def friendly_error(self, code: int, tail: List[str]) -> str:
        text = "\n".join(tail)
        low = text.lower()
        # "No authentication agent found", or "Error creating textual
        # authentication agent" when none runs and there is no terminal either
        if "authentication agent" in low:
            return self._("task_err_no_agent")
        if code in (126, 127):     # pkexec: password window closed / not allowed
            return self._("task_err_password")
        if "unable to lock database" in low or "could not get lock" in low or "is locked" in low:
            return self._("task_err_locked")
        if "could not resolve all dependencies" in low or "failed to install missing dependencies" in low:
            return self._("task_err_deps")
        if ("failed retrieving file" in low or "could not resolve host" in low or "temporary failure" in low
                or "unable to connect" in low):
            return self._("task_err_network")
        if ("target not found" in low or "unable to locate package" in low or "no match for argument" in low
                or "not found in package names" in low or "no provider of" in low
                or "has no installation candidate" in low):
            return self._("task_err_not_found")
        if "breaks dependency" in low or "required by" in low:
            return self._("task_err_required")
        if "no space left" in low or "not enough free disk space" in low:
            return self._("task_err_disk")
        return self._("task_err_generic").format(code=code)

    # --- GTK side ------------------------------------------------------------

    def _thread(self):
        try:
            self.work(self)
        except Cancelled:
            GLib.idle_add(self._close)
            return
        except TaskError as e:
            GLib.idle_add(self._fail, str(e))
            return
        except GLib.Error as e:
            if self.cancellable.is_cancelled():
                GLib.idle_add(self._close)
            else:
                GLib.idle_add(self._fail, self.friendly_error(1, [e.message]))
            return
        except Exception as e:
            logger.exception("Launcher task failed")
            GLib.idle_add(self._fail, str(e))
            return
        GLib.idle_add(self._succeed)

    def _step(self, step: str, status: Optional[str]) -> bool:
        if step not in self.stepper.states:
            return False
        order = [key for key, _label in self.stepper.steps]
        if self.current in order and order.index(step) < order.index(self.current):
            # a batch's next item starting over (Download after the last
            # one's Install): the later steps aren't done for this one
            for key in order[order.index(step) + 1:]:
                if self.stepper.states.get(key) in ("done", "active"):
                    self._set_state(key, "pending")
        elif self.current is not None:
            self._set_state(self.current, "done")
        # Steps before this one that never said they started (a package
        # manager's output read differently than expected) are done all
        # the same - never an empty dot behind the one under way.
        self._mark_done_before(step)
        self.current = step
        self._set_state(step, "active")
        if status:
            self.status.set_text(status)
        return False

    def _progress(self, fraction: float, status: Optional[str], creep: bool) -> bool:
        if fraction > self.target:
            self.target = fraction
            self.creep_from = time.monotonic() if creep else None
        elif creep and self.creep_from is None:
            self.creep_from = time.monotonic()
        if status:
            self.status.set_text(status)
        return False

    def _set_state(self, step: str, state: str):
        self.stepper.set_state(step, state)

    def _animate(self) -> bool:
        target = self.target
        if self.creep_from is not None and not self.finished:
            # Between milestones the bar creeps a little, so it never
            # looks frozen - but never claims the next milestone either.
            target += 0.08 * (1 - math.exp(-(time.monotonic() - self.creep_from) / 5.0))
        self.shown = min(target, 0.99 if not self.finished else 1.0)
        self.ring.tick(self.shown)
        self.stepper.tick()
        if self.finished and self.ring.shown == self.shown:
            # settled: a finished card left open mustn't keep repainting
            self.anim = 0
            return False
        return True

    def _log(self, line: str) -> bool:
        self.log_panel.append(line)
        return False

    def _mark_done_before(self, step: str):
        order = [key for key, _label in self.stepper.steps]
        for key in order[:order.index(step)] if step in order else ():
            if self.stepper.states.get(key) in ("pending", "active"):
                self._set_state(key, "done")

    def _succeed(self) -> bool:
        self.finished = True
        if self.current:
            self._set_state(self.current, "done")
        # it worked: every step is done, whichever the output showed
        self._mark_done_before("done")
        self._set_state("done", "done")
        self.target = self.shown = 1.0
        self.ring.finish("done")
        self.title.set_text(self.done_title)
        show_final(self.status, self.done_text)
        self.cancel_btn.hide()
        self.close_btn.show()
        try:
            self.on_success()
        except Exception:
            logger.exception("After a launcher task")
        if self.launch_argv is not None and self.launch_argv() is not None:
            self.launch_btn.show()
            self.launch_btn.grab_focus()
        else:
            self.close_btn.grab_focus()
        return False

    def _fail(self, message: str) -> bool:
        self.finished = True
        if self.current:
            self._set_state(self.current, "failed")
        self.title.set_text(self._("task_failed_title"))
        if self.alternative is not None and message == self._("task_err_not_found"):
            message = self._("task_err_not_found_flatpak")
        show_final(self.status, message, error=True)
        self.ring.finish("failed")
        self.log_panel.set_open(True)       # the log says what went wrong
        self.cancel_btn.hide()
        self.close_btn.show()
        if self.alternative is not None:
            self.alt_btn.show()
            self.alt_btn.grab_focus()
        else:
            self.close_btn.grab_focus()
        return False

    def _try_alternative(self):
        alternative = self.alternative
        self._close()
        alternative()

    def _cancel(self):
        if not self.can_cancel or self.finished or self.cancellable.is_cancelled():
            return
        self.cancel_btn.set_sensitive(False)
        self.status.set_text(self._("update_status_cancelling"))
        self.cancellable.cancel()
        if not self.interruptible:
            # run_command raises Cancelled once this step is done.
            self.status.set_text(self._("task_status_cancel_after_step"))
            return
        proc = self.proc
        if proc is not None and proc.poll() is None:
            # The whole group: an AUR helper's makepkg, git and curl too.
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except OSError:
                try:
                    proc.terminate()    # also closes a waiting password window
                except OSError:
                    pass
        session = self.session
        if session is not None:
            session.interrupt()

    def _on_delete(self, *_args) -> bool:
        if self.finished:
            self._close()
        else:
            self._cancel()
        return True

    def _launch(self):
        argv = self.launch_argv()
        if argv:
            try:
                subprocess.Popen(argv, start_new_session=True,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError as e:
                logger.warning(f"Couldn't start {argv}: {e}")
        self._close()

    # --- for App.request_quit -----------------------------------------------------

    def quit_line(self) -> str:
        return f"{self.title.get_text()} ({int(self.shown * 100)}%)"

    def quit_stop(self):
        """As its Cancel: a download stops, an install is undone - a step
        that mustn't be cut short is finished first."""
        self._cancel()

    def quit_done(self) -> bool:
        return self.finished

    def _close(self) -> bool:
        self.app.remove_quit_blocker(self)
        if self.anim:
            GLib.source_remove(self.anim)
            self.anim = 0
        self.dialog.destroy()
        self.on_closed()
        return False


def launcher_pixbuf(name: str, size: int):
    """The launcher's logo bundled with PlunderHub - else its icon from
    the icon theme, or None."""
    bundled = bundled_launcher_icon(name)
    if bundled is not None:
        try:
            return pixbuf_at_size(bundled, size, size)
        except GLib.Error as e:
            logger.debug(f"Couldn't load the bundled icon {bundled}: {e}")
    theme = Gtk.IconTheme.get_default()
    for candidate in LAUNCHER_ICON_NAMES.get(name, []):
        try:
            if theme.has_icon(candidate):
                return theme_icon_pixbuf(theme, candidate, size, Gtk.IconLookupFlags.FORCE_SIZE)
        except GLib.Error:
            pass
    return None


def launcher_icon(app, name: str, size: int) -> Gtk.Image:
    pb = launcher_pixbuf(name, size)
    if pb is not None:
        return Gtk.Image.new_from_pixbuf(pb)
    return app._icon_image_at_size("applications-games-symbolic", size)


# --- the jobs ------------------------------------------------------------------------

# apt: keep the configuration files the user changed, and wait for a
# running unattended-upgrades rather than fail on its lock.
# --no-remove: with -y apt would otherwise take out an installed package
# that conflicts with a new one; failing is the safer answer.
_APT_INSTALL_OPTS = ["--no-remove"]
_APT_OPTS = ["-o", "Dpkg::Options::=--force-confdef", "-o", "Dpkg::Options::=--force-confold",
             "-o", "DPkg::Lock::Timeout=120"]
# zypper's 100-107 are "done, but note...": reboot needed, a repository
# skipped, a scriptlet warning.
_ZYPPER_OK = tuple([0] + list(range(100, 108)))


def _installed_packages(family: str) -> set:
    """Every installed package, exactly enough to remove it again."""
    if family == "arch":
        code, out = _run_quiet(["pacman", "-Qq"])
        return set(out.split()) if code == 0 else set()
    if family == "debian":
        code, out = _run_quiet(["dpkg-query", "-W", "-f", "${db:Status-Abbrev} ${binary:Package}\n"])
        if code != 0:
            return set()
        return {line.split()[1] for line in out.splitlines() if line.startswith("ii ") and len(line.split()) > 1}
    if family in ("fedora", "suse"):
        # by name and arch only: one the install merely upgraded isn't new
        # (by version it was - and the undo's rpm -e took it away)
        code, out = _run_quiet(["rpm", "-qa", "--qf", "%{NAME}.%{ARCH}\n"])
        # gpg-pubkey has no architecture.
        return {line.replace(".(none)", "") for line in out.split()} if code == 0 else set()
    return set()


def _remove_command(family: str, packages: List[str]) -> List[str]:
    if family == "arch":
        # Not -s: every new package is named, and -s could also take
        # along older orphans the install had nothing to do with.
        return ["pacman", "-Rn", "--noconfirm"] + packages
    if family == "debian":
        return ["apt-get", "purge", "-y"] + _APT_OPTS + packages
    return ["rpm", "-e"] + packages


def _aur_clone_root(helper: str) -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return cache / "paru" / "clone" if helper == "paru" else cache / helper


def _listing(directory: Path) -> set:
    try:
        return {p.name for p in directory.iterdir()}
    except OSError:
        return set()


def _root_env(session: RootSession) -> dict:
    return {**os.environ, "LC_ALL": "C", **session.env}


def _undo_native(w: TaskWindow, session: RootSession, family: str, packages_before: set,
                 undo: List[List[str]] = (), new_dirs: Callable = lambda: []):
    """After Cancel: removes the packages the install had put on the
    system by then, runs undo (the repositories or architecture it
    added, taken away again) and deletes the package files it
    downloaded and its build directories."""
    w.cleaning_up()
    session.wait_idle()
    # An empty "before" means the package query failed - then
    # everything would look new.
    new = sorted(_installed_packages(family) - packages_before) if packages_before else []
    if new:
        w.log("Removing again: " + " ".join(new))
        w.run_command(session.command(_remove_command(family, new)), [], env=_root_env(session), cleanup=True)
    for cmd in undo:
        w.run_command(session.command(cmd), [], env=_root_env(session), cleanup=True)
    session.cleanup()
    for path in new_dirs():
        w.log(f"Deleting {path}")
        shutil.rmtree(path, ignore_errors=True)


def _job_pacman_install(plan: Plan):
    def work(w: TaskWindow):
        with w.root_session() as session:
            before = _installed_packages("arch")
            try:
                w.run_command(session.command(["pacman", "-S", "--needed", "--noconfirm", plan.package]
                                              + plan.extras), _PACMAN_INSTALL, env=_root_env(session))
                _post(w, session, plan.post)
            except Cancelled:
                _undo_native(w, session, "arch", before)
                raise
    return work


def _job_aur_helper(plan: Plan):
    def work(w: TaskWindow):
        with w.root_session() as session:
            w.step("download", w._("update_status_preparing"))
            before = _installed_packages("arch")
            clones = _aur_clone_root(plan.helper)
            clones_before = _listing(clones)
            flags = (["--skipreview"] if plan.helper == "paru"
                     else ["--answerdiff", "None", "--answerclean", "None", "--answeredit", "None"])
            cmd = ([plan.helper, "-S", "--needed", "--noconfirm", "--sudo", session.sudo] + flags + [plan.package]
                   + plan.extras)
            try:
                w.run_command(cmd, _aur_build(plan.package), env=_root_env(session))
                _post(w, session, plan.post)
            except Cancelled:
                _undo_native(w, session, "arch", before,
                             new_dirs=lambda: [clones / name for name in sorted(_listing(clones) - clones_before)])
                raise
    return work


def _job_aur_makepkg(plan: Plan):
    def work(w: TaskWindow):
        workdir = Path(tempfile.mkdtemp(prefix="plunderhub-aur-"))
        try:
            with w.root_session() as session:
                w.step("download", w._("update_status_preparing"))
                before = _installed_packages("arch")
                try:
                    if plan.base_devel:
                        # makepkg -s installs the package's dependencies,
                        # not the tools it builds with.
                        w.run_command(session.command(["pacman", "-S", "--needed", "--noconfirm", "base-devel"]),
                                      _PACMAN_INSTALL, env=_root_env(session))
                    url = f"https://aur.archlinux.org/cgit/aur.git/snapshot/{plan.package}.tar.gz"
                    archive = workdir / "snapshot.tar.gz"
                    w.log(f"GET {url}")
                    req = urllib.request.Request(url, headers={"User-Agent": "PlunderHub"})
                    try:
                        with urllib.request.urlopen(req, timeout=30) as resp, open(archive, "wb") as out:
                            shutil.copyfileobj(resp, out)
                    except OSError as e:
                        raise TaskError(w._("task_err_network") + f"\n{e}")
                    w.check_cancelled()
                    with tarfile.open(archive) as tar:
                        tar.extractall(workdir, filter="data")
                    build_dir = workdir / plan.package
                    # makepkg reads PACMAN_AUTH only from its config: the
                    # system's own config, plus the session for installing
                    # dependencies and the finished package.
                    conf = workdir / "makepkg.conf"
                    conf.write_text(f"source /etc/makepkg.conf\nPACMAN_AUTH=({session.sudo})\n")
                    w.run_command(["makepkg", "-si", "--noconfirm", "--needed", "--config", str(conf)],
                                  _aur_build(plan.package), env=_root_env(session), cwd=str(build_dir))
                    if plan.extras:
                        w.run_command(session.command(["pacman", "-S", "--needed", "--noconfirm"] + plan.extras),
                                      [], env=_root_env(session), check=False)
                    _post(w, session, plan.post)
                except Cancelled:
                    _undo_native(w, session, "arch", before)
                    raise
        finally:
            shutil.rmtree(workdir, ignore_errors=True)
    return work


def _steam_debconf(session: RootSession) -> str:
    """Debian's and Ubuntu's Steam packages ask for the Steam license
    in a debconf dialog nobody would see here, and without an answer the
    install stops. Clicking Install is taken as the answer; Steam shows
    its own agreement again on first start."""
    path = os.path.join(session.dir, "debconf")
    with open(path, "w") as f:
        for owner in ("steam", "steam-installer"):
            f.write(f"{owner} steam/question select I AGREE\n{owner} steam/license note\n")
    return path


def _job_apt(plan: Plan):
    def work(w: TaskWindow):
        with w.root_session() as session:
            env = _root_env(session)
            before = _installed_packages("debian")
            undo: List[List[str]] = []
            recipe = plan.recipe
            try:
                w.step("prepare", w._("update_status_preparing"))
                w.progress(0.06, creep=True)
                if recipe.i386 and "i386" not in _run_quiet(["dpkg", "--print-foreign-architectures"])[1].split():
                    w.run_command(session.command(["dpkg", "--add-architecture", "i386"]), [], env=env,
                                  interruptible=False)
                    undo.append(["dpkg", "--remove-architecture", "i386"])
                if plan.add_repo and recipe.ppa:
                    if not shutil.which("add-apt-repository"):
                        w.run_command(session.command(["apt-get", "install", "-y"] + _APT_OPTS
                                                      + _APT_INSTALL_OPTS + ["software-properties-common"]),
                                      [], env=env, interruptible=False)
                    w.run_command(session.command(["add-apt-repository", "-y", f"ppa:{recipe.ppa}"]), [],
                                  env=env, interruptible=False)
                    undo.append(["add-apt-repository", "--remove", "-y", f"ppa:{recipe.ppa}"])
                if plan.package in ("steam-installer", "steam") and shutil.which("debconf-set-selections"):
                    w.run_command(session.command(["debconf-set-selections", _steam_debconf(session)]), [],
                                  env=env, interruptible=False, check=False)
                w.progress(0.12, creep=True)
                # A broken third-party source makes update fail without
                # the install being any worse off.
                w.run_command(session.command(["apt-get", "update"]), [], env=env, check=False)
                w.step("download")
                w.run_command(session.command(["apt-get", "install", "-y", "--download-only"] + _APT_OPTS
                                              + _APT_INSTALL_OPTS + [plan.package] + plan.extras),
                              _DOWNLOAD, env=env)
                w.step("install")
                w.progress(0.58, creep=True)
                w.run_command(session.command(["apt-get", "install", "-y"] + _APT_OPTS + _APT_INSTALL_OPTS
                                              + [plan.package] + plan.extras), _INSTALL, env=env,
                              interruptible=False)
                _post(w, session, plan.post)
            except Cancelled:
                if undo:
                    undo = list(reversed(undo)) + [["apt-get", "update"]]
                _undo_native(w, session, "debian", before, undo)
                raise
    return work


def _rpmfusion_urls() -> Optional[List[str]]:
    for macro, base in (("%fedora", "fedora"), ("%rhel", "el")):
        version = _run_quiet(["rpm", "-E", macro])[1].strip()
        if version.isdigit():
            return [f"https://mirrors.rpmfusion.org/{part}/{base}/rpmfusion-{part}-release-{version}.noarch.rpm"
                    for part in ("free", "nonfree")]
    return None


def _job_dnf(plan: Plan):
    def work(w: TaskWindow):
        with w.root_session() as session:
            env = _root_env(session)
            before = _installed_packages("fedora")
            undo: List[List[str]] = []
            recipe = plan.recipe
            try:
                w.step("prepare", w._("update_status_preparing"))
                w.progress(0.06, creep=True)
                if plan.add_repo and recipe.rpmfusion:
                    urls = _rpmfusion_urls()
                    if urls:
                        # Its release packages go again with the rest on
                        # Cancel.
                        w.run_command(session.command(["dnf", "install", "-y"] + urls), [], env=env,
                                      interruptible=False)
                if plan.add_repo and recipe.copr:
                    # The copr command: a plugin on dnf 4, built in on dnf 5.
                    w.run_command(session.command(["dnf", "install", "-y", "dnf-command(copr)"]), [], env=env,
                                  interruptible=False, check=False)
                    repos_before = _listing(Path("/etc/yum.repos.d"))
                    w.run_command(session.command(["dnf", "-y", "copr", "enable", recipe.copr]), [], env=env,
                                  interruptible=False)
                    if _listing(Path("/etc/yum.repos.d")) - repos_before:
                        undo.append(["dnf", "-y", "copr", "remove", recipe.copr])
                w.step("download")
                w.run_command(session.command(["dnf", "install", "-y", "--downloadonly", plan.package]), _DOWNLOAD,
                              env=env)
                w.step("install")
                w.progress(0.58, creep=True)
                w.run_command(session.command(["dnf", "install", "-y", plan.package]), _INSTALL, env=env,
                              interruptible=False)
                if plan.extras:
                    # names the repositories lack, or that would conflict with
                    # something installed, are skipped, not fatal
                    dnf5 = os.path.realpath(shutil.which("dnf") or "").endswith("dnf5")
                    skip = ["--skip-unavailable", "--skip-broken"] if dnf5 else ["--setopt=strict=0"]
                    w.run_command(session.command(["dnf", "install", "-y"] + skip + plan.extras), [], env=env,
                                  interruptible=False, check=False)
                _post(w, session, plan.post)
            except Cancelled:
                _undo_native(w, session, "fedora", before, undo)
                raise
    return work


def _job_zypper(plan: Plan):
    def work(w: TaskWindow):
        with w.root_session() as session:
            env = _root_env(session)
            before = _installed_packages("suse")
            base = ["zypper", "--non-interactive", "install", "--auto-agree-with-licenses"]
            try:
                w.step("download", w._("update_status_preparing"))
                w.progress(0.06, creep=True)
                w.run_command(session.command(base + ["--download-only", plan.package]), _DOWNLOAD,
                              env=env, ok_codes=_ZYPPER_OK)
                w.step("install")
                w.progress(0.58, creep=True)
                w.run_command(session.command(base + [plan.package]), _INSTALL, env=env,
                              interruptible=False, ok_codes=_ZYPPER_OK)
                # Non-interactive zypper cancels the whole command over one
                # conflict, so each extra goes on its own and a failing one
                # is only logged, like dnf's.
                for extra in plan.extras:
                    w.run_command(session.command(base + [extra]), [], env=env, interruptible=False,
                                  check=False, ok_codes=_ZYPPER_OK)
                _post(w, session, plan.post)
            except Cancelled:
                _undo_native(w, session, "suse", before)
                raise
    return work


def _job_native_remove(family: str, packages: List[str], pre: List[str] = ()):
    def work(w: TaskWindow):
        with w.root_session() as session:
            # Half a removal can't be undone the way half an install can.
            w.lock_cancel()
            _post(w, session, list(pre))
            if family == "arch":
                cmd, milestones, ok = ["pacman", "-Rns", "--noconfirm"] + packages, _PACMAN_REMOVE, (0,)
            elif family == "debian":
                cmd, milestones, ok = ["apt-get", "remove", "-y"] + _APT_OPTS + packages, _REMOVE, (0,)
            elif family == "fedora":
                cmd, milestones, ok = ["dnf", "remove", "-y"] + packages, _REMOVE, (0,)
            else:
                cmd, milestones, ok = ["zypper", "--non-interactive", "remove"] + packages, _REMOVE, _ZYPPER_OK
            w.run_command(session.command(cmd), milestones, env=_root_env(session), interruptible=False,
                          ok_codes=ok)
    return work


def _nice_ref(ref: str) -> str:
    parts = ref.split("/")
    if len(parts) >= 4:
        return f"{parts[1]} {parts[3]}" if parts[0] == "runtime" else parts[1]
    return ref


def _flatpak_transaction(w: TaskWindow, add_ops: Callable, installation=None) -> dict:
    """One libflatpak transaction, its per-operation progress turned
    into a single bar across all operations.

    Cancel stops a download at once. Once an operation has finished
    downloading, though, flatpak's system helper deploys it as root -
    and goes on doing so even when the transaction is cancelled. So
    Cancel then waits for that operation to finish and stops before the
    next one, rather than leave something installing out of sight.
    Returns what Cancel caught it doing ("current", "pct")."""
    installation = installation or Flatpak.Installation.new_system(None)
    inner = Gio.Cancellable()
    t = Flatpak.Transaction.new_for_installation(installation, inner)
    t.set_no_interaction(True)
    add_ops(t)
    state = {"ops": [], "done": 0, "current": None, "pct": 0}

    def deploying() -> bool:
        return state["current"] is not None and state["pct"] >= 100

    def on_cancel(_c):
        if not deploying():
            inner.cancel()

    def on_ready(trans):
        state["ops"] = trans.get_operations()
        w.log("Planned: " + ", ".join(op.get_ref() for op in state["ops"]))
        return True

    def on_new_operation(_trans, op, progress):
        if w.cancellable.is_cancelled():
            inner.cancel()
            return
        index = state["done"]
        total = max(1, len(state["ops"]))
        name = _nice_ref(op.get_ref())
        state["current"], state["pct"] = op.get_ref(), 0
        w.log(op.get_ref())

        def changed(p):
            pct = p.get_progress()
            state["pct"] = pct
            transferred = p.get_bytes_transferred()
            size = f" · {transferred / 1e6:.0f} MB" if transferred else ""
            if not w.cancellable.is_cancelled():
                w.progress(0.06 + 0.92 * (index + pct / 100.0) / total, f"{name} - {pct}%{size}")
        progress.connect("changed", changed)

    def on_done(*_args):
        state["done"] += 1
        state["current"] = None
        if w.cancellable.is_cancelled():
            inner.cancel()

    t.connect("ready", on_ready)
    t.connect("new-operation", on_new_operation)
    t.connect("operation-done", on_done)
    # GObject's connect: Gio.Cancellable's own connect() is
    # g_cancellable_connect, which takes no signal name.
    handler = GObject.Object.connect(w.cancellable, "cancelled", on_cancel)
    try:
        if w.cancellable.is_cancelled():
            raise Cancelled()
        t.run(inner)
    except GLib.Error as e:
        if w.cancellable.is_cancelled():
            raise Cancelled(state)
        w.log(e.message)
        raise TaskError(e.message)
    finally:
        GObject.Object.disconnect(w.cancellable, handler)
    # Cancel came during the last operation's deploy: it finished, and
    # is undone like the rest.
    if w.cancellable.is_cancelled():
        raise Cancelled(state)
    return state


def _flatpak_refs(installation) -> Optional[set]:
    """The installed refs; None when the query failed (an empty set is
    a fresh Flatpak with nothing installed yet)."""
    try:
        return {ref.format_ref() for ref in installation.list_installed_refs(None)}
    except GLib.Error:
        return None


def _flatpak_refs_cli() -> Optional[set]:
    code, out = _run_quiet(["flatpak", "list", "--system", "--columns=ref"])
    return set(out.split()) if code == 0 else None


def _remove_flatpak_download_caches(since: float):
    """The half-filled repos a cancelled system install downloads into."""
    for path in Path("/var/tmp").glob("flatpak-cache-*"):
        try:
            st = path.lstat()
        except OSError:
            continue
        if path.is_dir() and not path.is_symlink() and st.st_uid == os.getuid() and st.st_mtime >= since - 5:
            shutil.rmtree(path, ignore_errors=True)


def _undo_flatpak(w: TaskWindow, installation, before: set, caught: Optional[dict], since: float):
    """After Cancel: uninstalls what the transaction had installed by
    then - runtimes included - and deletes what it downloaded."""
    w.cleaning_up()
    if caught and caught.get("current") and caught.get("pct", 0) >= 95:
        # Cancel may have crossed the moment the download finished, and
        # the system helper may be deploying it still - give it the few
        # seconds that takes.
        deadline = time.monotonic() + 12
        while caught["current"] not in (_flatpak_refs(installation) or set()) and time.monotonic() < deadline:
            time.sleep(0.5)
    new = sorted((_flatpak_refs(installation) or set()) - before) if before is not None else []
    if new:
        w.log("Removing again: " + ", ".join(new))
        t = Flatpak.Transaction.new_for_installation(installation, None)
        t.set_no_interaction(True)
        for ref in new:
            t.add_uninstall(ref)
        try:
            t.run(None)
        except GLib.Error as e:
            logger.warning(f"Undoing a cancelled Flatpak install: {e.message}")
    try:
        installation.prune_local_repo(None)
    except GLib.Error as e:
        logger.debug(f"Pruning the Flatpak repo: {e.message}")
    _remove_flatpak_download_caches(since)


def _ensure_flathub(w: TaskWindow):
    """Flathub as a system remote: a freshly installed flatpak (Debian's,
    Ubuntu's...) comes without it."""
    w.run_command(["flatpak", "remote-add", "--system", "--if-not-exists", "flathub",
                   "https://dl.flathub.org/repo/flathub.flatpakrepo"], [])


def _job_flatpak_install(app_id: str):
    def work(w: TaskWindow):
        w.step("prepare", w._("update_status_preparing"))
        w.progress(0.02, creep=True)
        since = time.time()
        w.step("download")
        _ensure_flathub(w)
        installation = _system_installation()
        if installation is not None:
            before = _flatpak_refs(installation)
            try:
                _flatpak_transaction(w, lambda t: t.add_install("flathub", f"app/{app_id}/x86_64/stable", None),
                                     installation)
            except Cancelled as e:
                _undo_flatpak(w, installation, before, e.args[0] if e.args else None, since)
                raise
        else:
            before = _flatpak_refs_cli()
            try:
                w.run_command(["flatpak", "install", "--system", "-y", "--noninteractive", "flathub", app_id],
                              [("Installing", "download", 0.3)])
            except Cancelled:
                w.cleaning_up()
                new = sorted((_flatpak_refs_cli() or set()) - before) if before is not None else []
                if new:
                    w.run_command(["flatpak", "uninstall", "--system", "-y", "--noninteractive"] + new, [],
                                  cleanup=True)
                _remove_flatpak_download_caches(since)
                raise
    return work


def _job_flatpak_with_flatpak(family: str, app_id: str):
    """Flatpak itself first - not there (Ubuntu ships without it), and the
    launcher exists only as one (Heroic, Bottles): the flatpak package and
    the Flathub remote and the app, all as root under the batch's one
    password."""
    def work(w: TaskWindow):
        with w.root_session() as session:
            env = _root_env(session)
            w.step("prepare", w._("task_status_flatpak_setup"))
            w.progress(0.02, creep=True)
            w.run_command(session.command(_install_command(family, ["flatpak"])),
                          _PACMAN_INSTALL if family == "arch" else _INSTALL, env=env,
                          ok_codes=_ZYPPER_OK if family == "suse" else (0,))
            # the root helper's own actions: it runs package tools only
            w.run_command(session.command([ACTION, "add-flathub"]), [], env=env)
            # as root too: a fresh flatpak would ask its own password
            w.step("download")
            w.run_command(session.command([ACTION, FLATPAK_INSTALL, app_id]),
                          [("Installing", "download", 0.3)], env=env)
    return work


def _job_flatpak_remove(app_id: str):
    def work(w: TaskWindow):
        w.step("remove", w._("update_status_preparing"))
        w.progress(0.05, creep=True)
        w.lock_cancel()
        installation = _system_installation()
        if installation is not None:
            try:
                ref = installation.get_installed_ref(Flatpak.RefKind.APP, app_id, None, None, None).format_ref()
                _flatpak_transaction(w, lambda t: t.add_uninstall(ref), installation)
                return
            except GLib.Error:
                pass    # e.g. installed per-user - the CLI finds it either way
        w.run_command(["flatpak", "uninstall", "-y", "--noninteractive", app_id], [("Uninstalling", "remove", 0.4)])
    return work


def _job_flatpak_refs(refs: List[str], install: bool):
    """Shipwright's Vulkan layer extensions for Flatpak launchers -
    runtimes, one per runtime version the launchers use."""
    def work(w: TaskWindow):
        w.step("prepare" if install else "remove", w._("update_status_preparing"))
        w.progress(0.02, creep=True)
        since = time.time()
        if not install:
            # flatpak finds them wherever they are (system or per-user)
            w.lock_cancel()
            w.run_command(["flatpak", "uninstall", "-y", "--noninteractive"] + [r.split("/", 1)[1] for r in refs],
                          [("Uninstalling", "remove", 0.4)])
            return
        w.step("download")
        _ensure_flathub(w)
        installation = _system_installation()
        if installation is not None:
            before = _flatpak_refs(installation)

            def ops(t):
                for r in refs:
                    t.add_install("flathub", r, None)
            try:
                _flatpak_transaction(w, ops, installation)
            except Cancelled as e:
                _undo_flatpak(w, installation, before, e.args[0] if e.args else None, since)
                raise
        else:
            before = _flatpak_refs_cli()
            try:
                w.run_command(["flatpak", "install", "--system", "-y", "--noninteractive", "flathub"] + refs,
                              [("Installing", "download", 0.3)])
            except Cancelled:
                w.cleaning_up()
                new = sorted((_flatpak_refs_cli() or set()) - before) if before is not None else []
                if new:
                    w.run_command(["flatpak", "uninstall", "--system", "-y", "--noninteractive"] + new, [],
                                  cleanup=True)
                _remove_flatpak_download_caches(since)
                raise
    return work


def start_layer(app, layer, refs: List[str], action: str, parent: Gtk.Window, on_finished: Callable):
    """Installs or removes a Flatpak launchers' layer extension."""
    _ = app._
    name = f"{layer.name} · Flatpak"
    install = action == "install"
    w = TaskWindow(app, parent, name, _("task_install_title" if install else "task_remove_title").format(name=name),
                   _("task_sub_flatpak"), ["prepare", "download"] if install else ["remove"],
                   _job_flatpak_refs(refs, install),
                   _("task_ready_title" if install else "task_removed_title").format(name=name),
                   _("sw_layer_ready_text" if install else "sw_layer_removed_text"),
                   on_success=lambda: None, on_closed=on_finished)
    w.ring.symbolic = layer.icon


def _install_command(family: str, packages: List[str]) -> List[str]:
    if family == "arch":
        return ["pacman", "-S", "--needed", "--noconfirm"] + packages
    if family == "debian":
        return ["apt-get", "install", "-y"] + _APT_OPTS + _APT_INSTALL_OPTS + packages
    if family == "fedora":
        return ["dnf", "install", "-y"] + packages
    return ["zypper", "--non-interactive", "install", "--auto-agree-with-licenses"] + packages


def fix_work(fix, family: str) -> Callable:
    """The work of a Ship inspection fix: root_helper actions,
    package-manager commands and packages to install, in that order,
    under one password."""
    def work(w: TaskWindow):
        with w.root_session() as session:
            env = _root_env(session)
            # a half-applied fix can't be taken back like an install
            w.lock_cancel()
            w.step("install", w._("update_status_preparing"))
            w.progress(0.08, creep=True)
            for action in fix.actions:
                w.run_command(session.command([ACTION, action]), [], env=env, interruptible=False)
            for cmd in fix.commands:
                w.run_command(session.command(list(cmd)), _PACMAN_INSTALL if cmd[0] == "pacman" else _INSTALL,
                              env=env, interruptible=False, ok_codes=_ZYPPER_OK if cmd[0] == "zypper" else (0,))
            packages = _not_satisfied(family, fix.packages)
            if packages:
                cmd = _install_command(family, packages)
                w.run_command(session.command(cmd), _PACMAN_INSTALL if family == "arch" else _INSTALL, env=env,
                              interruptible=False, ok_codes=_ZYPPER_OK if family == "suse" else (0,))
    return work


def run_fix(app, parent: Gtk.Window, title: str, icon: str, fix, on_finished: Callable):
    _ = app._
    work = fix_work(fix, detect_linux_distro_family())
    w = TaskWindow(app, parent, title, title, _("sw_fix_sub"), ["auth", "install"], work,
                   _("sw_fix_done_title"), _(fix.done_key), on_success=lambda: None, on_closed=on_finished)
    w.ring.symbolic = icon


# --- batches: several installs, removals or fixes in one window --------------------------

@dataclass
class BatchItem:
    """key: a tool id, a launcher's name, "flatpak:<layer id>",
    "fix:<check id>" or "update:..."; name: what the list shows. layer /
    refs for a Flatpak layer, fix for a Ship inspection fix; for an
    update the packages (full: the whole system, as Arch and Tumbleweed
    need; aur: AUR packages to rebuild) or the Flatpak apps."""
    key: str
    name: str
    action: str = "install"         # install | remove | fix | update
    layer: object = None
    refs: List[str] = field(default_factory=list)
    fix: object = None
    packages: List[str] = field(default_factory=list)
    full: bool = False
    aur: List[str] = field(default_factory=list)
    flatpak_ids: List[str] = field(default_factory=list)
    source: str = ""                # an install's pick: "native" | "flatpak" | "" (native, else Flatpak)
    # the launcher or tool whose logo the window shows when it's alone
    logo: str = ""
    # an install's steps, once planned
    steps: List[str] = field(default_factory=list)


def _job_update(family: str, packages: List[str], full: bool, aur: List[str]):
    """Downloads first - Cancel is fine until then - then installs, which
    can't be stopped half-way. full: the whole system (pacman -Syu,
    zypper dup); else just packages, which apt, dnf and zypper on Leap
    handle with their dependencies."""
    def work(w: TaskWindow):
        with w.root_session() as session:
            env = _root_env(session)
            w.step("download", w._("update_status_preparing"))
            w.progress(0.04, creep=True)
            if family == "arch":
                # downloaded against a copy of the databases (a Cancel leaves
                # the system's own untouched), synced for real with the upgrade
                download = ["pacman", "-Syuw", "--noconfirm", "--dbpath", PACMAN_TMPDB]
                install = ["pacman", "-Syu", "--noconfirm"]
            elif family == "debian":
                w.run_command(session.command(["apt-get", "update"]), [], env=env, check=False)
                download = ["apt-get", "install", "-y", "--only-upgrade", "--download-only"] + _APT_OPTS + packages
                install = ["apt-get", "install", "-y", "--only-upgrade"] + _APT_OPTS + packages
            elif family == "fedora":
                download = ["dnf", "upgrade", "-y", "--downloadonly"] + packages
                install = ["dnf", "upgrade", "-y"] + packages
            else:
                base = ["zypper", "--non-interactive"]
                verb = ["dup", "--auto-agree-with-licenses"] if full else ["update", "--auto-agree-with-licenses"]
                download = base + verb + ["--download-only"] + ([] if full else packages)
                install = base + verb + ([] if full else packages)
            ok = _ZYPPER_OK if family == "suse" else (0,)
            try:
                if family == "arch" and full:
                    w.run_command(session.command([ACTION, "pacman-tmpdb"]), [], env=env, interruptible=False)
                if family != "arch" or full:
                    w.run_command(session.command(download), _PACMAN_INSTALL if family == "arch" else _DOWNLOAD,
                                  env=env, ok_codes=ok)
            except Cancelled:
                w.cleaning_up()
                session.cleanup()
                raise
            w.lock_cancel()
            w.step("install")
            w.progress(0.55, creep=True)
            if family != "arch" or full:
                w.run_command(session.command(install), _PACMAN_INSTALL if family == "arch" else _INSTALL,
                              env=env, interruptible=False, ok_codes=ok)
            helper = next((h for h in ("paru", "yay") if shutil.which(h)), None)
            if family == "arch" and aur and helper:
                # with the system up to date, AUR packages rebuild one by one
                flags = (["--skipreview"] if helper == "paru"
                         else ["--answerdiff", "None", "--answerclean", "None", "--answeredit", "None"])
                w.run_command([helper, "-S", "--noconfirm", "--sudo", session.sudo] + flags + aur, _AUR_BUILD,
                              env=env, interruptible=False)
    return work


def _job_flatpak_update(app_ids: List[str]):
    def work(w: TaskWindow):
        w.step("download", w._("update_status_preparing"))
        w.progress(0.04, creep=True)
        # per-user or system-wide, flatpak finds them
        w.run_command(["flatpak", "update", "-y", "--noninteractive"] + app_ids,
                      [("Updating", "download", 0.3), ("Installing", "install", 0.6)])
    return work


def remove_work(app, name: str):
    """(job, needs root) that removes the launcher or tool - or the
    reason it can't be, as text."""
    _ = app._
    state = launcher_state(name)
    if not state.installed:
        return None
    if state.locked_by:
        return _("settings_bundle_lock").format(bundle=", ".join(state.locked_by), name=name)
    if state.method == "flatpak":
        return _job_flatpak_remove(state.flatpak_id), False
    if immutable_system():
        return _("uninstall_immutable").format(system=immutable_system())
    family = detect_linux_distro_family()
    package = state.package or _installed_from_recipe(family, name)
    if not package or family not in ("arch", "debian", "fedora", "suse") or not shutil.which("pkexec"):
        return _("uninstall_unsupported_distro")
    pre = list(TOOLS_BY_NAME[name].pre_remove) if name in TOOLS_BY_NAME else []
    return _job_native_remove(family, removal_plan(family, package, _installed_extras(family, name))[0], pre), True


def _resolve_batch_item(app, item: BatchItem):
    """(job, needs root, launcher enabled after) - or the reason it's
    skipped, as text. Off the GTK thread (asks the package manager)."""
    _ = app._
    if item.action == "fix":
        return fix_work(item.fix, detect_linux_distro_family()), True, None
    if item.action == "update":
        if item.flatpak_ids:
            return _job_flatpak_update(item.flatpak_ids), False, None
        if immutable_system():
            return _("sw_update_immutable").format(system=immutable_system())
        family = detect_linux_distro_family()
        if family not in ("arch", "debian", "fedora", "suse") or not shutil.which("pkexec"):
            return _("task_native_unavailable_noflat")
        return _job_update(family, item.packages, item.full, item.aur), True, None
    if item.layer is not None:
        if not item.refs:
            return _("sw_batch_nothing")
        if item.action == "install":
            from plunderhub.core import flatpak_layers
            there = [r for r in item.refs if flatpak_layers.on_flathub(r)]
            if not there:
                return _("sw_layer_not_on_flathub").format(
                    branches=", ".join(r.rsplit("/", 1)[-1] for r in item.refs))
            item.refs = there
        return _job_flatpak_refs(item.refs, item.action == "install"), False, None
    if item.action == "remove":
        resolved = remove_work(app, item.logo or item.name)
        if resolved is None:
            return _("source_not_installed")
        if isinstance(resolved, str):
            return resolved
        return resolved[0], resolved[1], None
    # The launcher's or tool's own name: what the list shows has
    # " · Flatpak" after it when the Flatpak was picked.
    name = item.logo or item.name
    # The Flatpak when that's what was picked; else the system package
    # where there is one, else the Flatpak.
    if item.source == "flatpak":
        plan = flatpak_plan(name)
        if plan is None:
            return _("task_flatpak_none")
    else:
        plan = native_plan(name)
    if not isinstance(plan, Plan):
        flat = flatpak_plan(name)
        if flat is None:
            family = detect_linux_distro_family()
            if (_flatpak_ids(name) and not shutil.which("flatpak") and not immutable_system()
                    and family in ("arch", "debian", "fedora", "suse") and shutil.which("pkexec")):
                # only a Flatpak, and no Flatpak here yet: that comes first
                item.steps = ["prepare", "download"]
                return _job_flatpak_with_flatpak(family, _flatpak_ids(name)[0]), True, \
                    (name if name not in TOOLS_BY_NAME else None)
            return _(plan.reason).format(**plan.args)
        plan = flat
    work, item.steps, _sub = install_work(app, plan)
    is_launcher = name not in TOOLS_BY_NAME
    return work, plan.kind != "flatpak", name if is_launcher else None


def start_batch(app, parent: Gtk.Window, items: List[BatchItem], on_finished: Callable):
    """One window for all of items: the system-package ones first, under
    one password, then the Flatpak ones. One failing doesn't stop the
    rest; Cancel undoes the one under way and skips the rest."""
    _ = app._
    removing = all(i.action == "remove" for i in items)
    fixing = all(i.action == "fix" for i in items)
    updating = all(i.action == "update" for i in items)
    title = _("sw_batch_remove_title" if removing else "sw_batch_fix_title" if fixing
              else "sw_batch_update_title" if updating else "sw_batch_title")
    kernel = os.uname().release
    enabled: List[str] = []
    removed: List[str] = []
    installing = all(i.action == "install" and i.layer is None for i in items)
    alone = items[0] if len(items) == 1 and items[0].logo else None
    outcome = {"done": 0, "failed": [], "skipped": []}

    def work(w: TaskWindow):
        w.step("prepare" if not removing else "remove", _("sw_batch_planning"))
        planned = []
        for item in items:
            w.check_cancelled()
            try:
                resolved = _resolve_batch_item(app, item)
            except Exception as e:
                logger.exception(f"Planning {item.name}")
                resolved = str(e)
            if isinstance(resolved, str):
                outcome["skipped"].append(item.name)
                GLib.idle_add(w.batch.set_state, item.key, "skipped", resolved)
            else:
                planned.append((item,) + resolved)
        if installing and planned:
            # the steps of what it'll run, as one install's own window has
            # them (Download, Build...), not just Install
            wanted = {step for p in planned for step in p[0].steps} | ({"auth"} if any(p[2] for p in planned) else set())
            GLib.idle_add(w.set_steps, [s for s in ("auth", "prepare", "download", "build", "install") if s in wanted])
        rooted = [p for p in planned if p[2]]
        rest = [p for p in planned if not p[2]]
        total = max(1, len(planned))
        done_count = [0]

        def run_one(item, job, _root, launcher):
            index = done_count[0]
            w.span = (index / total, 1 / total)
            GLib.idle_add(w.batch.set_state, item.key, "active")
            w.progress(0.0, _("sw_batch_now").format(i=index + 1, n=total, name=item.name))
            w.log(f"=== {item.name}")
            try:
                job(w)
            except (TaskError, GLib.Error) as e:
                outcome["failed"].append(item.name)
                GLib.idle_add(w.batch.set_state, item.key, "failed", getattr(e, "message", None) or str(e))
            except Cancelled:
                GLib.idle_add(w.batch.set_state, item.key, "skipped", _("sw_batch_cancelled"))
                raise
            else:
                outcome["done"] += 1
                if launcher:
                    enabled.append(launcher)
                if item.action == "remove":
                    removed.append(item.name)
                GLib.idle_add(w.batch.set_state, item.key, "done")
            finally:
                done_count[0] += 1
                w.span = None

        try:
            if rooted:
                with w.root_session() as session:
                    w.shared_session = session
                    try:
                        for p in rooted:
                            run_one(*p)
                    finally:
                        w.shared_session = None
            for p in rest:
                run_one(*p)
        except Cancelled:
            # the ones done before Cancel are really installed / removed
            GLib.idle_add(lambda: (after(), False)[1])
            raise
        w.span = None
        failed = outcome["failed"]
        if failed or outcome["skipped"]:
            text = _("sw_batch_partly").format(ok=outcome["done"], n=len(items))
            if failed or not outcome["done"]:      # nothing done: not a "Done"
                # the launchers that did install are switched on all the same
                GLib.idle_add(lambda: (after(), False)[1])
                raise TaskError(text + (" " + _("sw_batch_failed_list").format(names=", ".join(failed))
                                        if failed else ""))
            w.done_text = text
        else:
            w.done_text = _("sw_batch_all_done").format(n=outcome["done"])
        if updating and reboot_needed(detect_linux_distro_family(), kernel):
            w.done_text += " " + _("sw_reboot_needed")

    def after():
        # A launcher installed here isn't switched on: the user does that
        # in Settings, when they want its games in the library. The reload
        # still runs, so Settings knows it's installed now.
        # as a removal from Settings: switched off, gone from the sidebar
        for name in removed:
            if name not in TOOLS_BY_NAME:
                app.enabled_launchers.discard(name)
        if enabled or removed:
            app.settings["enabled_launchers"] = sorted(app.enabled_launchers)
            save_settings(app.settings)
            Gtk.IconTheme.get_default().rescan_if_needed()
            app.reload()

    steps = ["auth", "remove"] if removing else ["auth", "download", "install"] if updating else ["auth", "install"]
    sub = _("sw_batch_sub").format(n=len(items))
    if alone is not None and alone.action in ("install", "remove"):
        # one launcher or tool: its own logo and words, as from Settings
        title = _("task_install_title" if alone.action == "install" else "task_remove_title").format(name=alone.logo)
    w = TaskWindow(app, parent, alone.logo if alone is not None else "Shipwright", title, sub, steps, work,
                   _("sw_fix_done_title" if fixing else "sw_batch_done_title"), "", on_success=after,
                   on_closed=on_finished, batch=[(i.key, i.name) for i in items])
    if alone is None:
        w.ring.symbolic = "hammer-symbolic"


# --- entry point: the dialogs that start a task ----------------------------------------

def start(app, name: str, action: str, parent: Gtk.Window, on_finished: Callable):
    if action == "install":
        _ask_install(app, name, parent, on_finished)
    else:
        _ask_remove(app, name, parent, on_finished)


def _set_enabled(app, name: str, enabled: bool):
    if name in TOOLS_BY_NAME:
        return      # a tool isn't a library source
    if enabled:
        app.enabled_launchers.add(name)
    else:
        app.enabled_launchers.discard(name)
    app.settings["enabled_launchers"] = sorted(app.enabled_launchers)
    save_settings(app.settings)
    toggle = getattr(app, "source_toggles", {}).get(name)
    if toggle is not None:
        toggle.set_active(enabled)
    # Settings sees no change in the toggle (enabled_launchers already
    # has it), so the main window is rescanned from here: the launcher
    # and its games appear, or go, without a manual refresh. The icon
    # theme first, for the icon the launcher just installed.
    Gtk.IconTheme.get_default().rescan_if_needed()
    app.reload()


def _dialog(app, parent, title: str):
    """A card dialog (as the progress window); Esc cancels."""
    dlg, column = card_dialog(parent, title)
    column.set_spacing(12)

    def on_key(_w, event) -> bool:
        if event.keyval == Gdk.KEY_Escape:
            dlg.response(Gtk.ResponseType.CANCEL)
            return True
        return False
    dlg.connect("key-press-event", on_key)
    dlg.plunderhub_nav = lambda action: action == "back" and dlg.response(Gtk.ResponseType.CANCEL)
    return dlg, column


def _head(app, name: str, text: str) -> Gtk.Box:
    """The launcher's icon over a centred heading."""
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
    box.pack_start(launcher_icon(app, name, 64), False, False, 0)
    head = Gtk.Label(label=text)
    head.set_line_wrap(True)
    head.set_max_width_chars(36)
    head.set_justify(Gtk.Justification.CENTER)
    head.get_style_context().add_class("home-dialog-title")
    box.pack_start(head, False, False, 0)
    box.set_margin_bottom(6)
    return box


def _choice(app, choices: List[Gtk.ToggleButton], icon: str, title: str, desc: str,
            pill: Optional[str] = None, sensitive: bool = True) -> Gtk.ToggleButton:
    btn = Gtk.ToggleButton()
    btn.get_style_context().add_class("install-choice")
    box = Gtk.Box(spacing=14)
    box.pack_start(app._icon_image_at_size(icon, 26), False, False, 0)
    texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
    top = Gtk.Box(spacing=8)
    t = Gtk.Label(label=title, xalign=0)
    t.get_style_context().add_class("settings-row-title")
    top.pack_start(t, False, False, 0)
    if pill:
        p = Gtk.Label(label=pill)
        p.get_style_context().add_class("settings-pill")
        top.pack_start(p, False, False, 0)
    texts.pack_start(top, False, False, 0)
    d = Gtk.Label(label=desc, xalign=0)
    d.set_line_wrap(True)
    d.set_max_width_chars(46)
    d.get_style_context().add_class("settings-row-sub")
    texts.pack_start(d, False, False, 0)
    box.pack_start(texts, True, True, 0)
    btn.add(box)
    btn.desc_label = d
    btn.set_sensitive(sensitive)
    choices.append(btn)
    btn.connect("toggled", lambda b: b.get_active() and [c.set_active(False) for c in choices if c is not b])
    return btn


def _native_desc(app, plan: Plan) -> str:
    if plan.kind in ("aur_helper", "aur_makepkg"):
        text = app._("task_native_aur_desc").format(tool=plan.tool)
    else:
        text = app._("task_native_repo_desc").format(tool=plan.tool)
    if plan.extras:
        text += " " + app._("task_native_extras").format(packages=", ".join(plan.extras))
    return text


def _ask_install(app, name: str, parent: Gtk.Window, on_finished: Callable):
    _ = app._
    flat = flatpak_plan(name)
    dlg, content = _dialog(app, parent, _("task_install_title").format(name=name))
    content.pack_start(_head(app, name, _("task_where_from").format(name=name)), False, False, 0)

    choices: List[Gtk.ToggleButton] = []
    # The system package always comes first and is the recommended one;
    # Flatpak is the alternative (and the default only when the system
    # package can't be used). Whether the repositories have it takes a
    # moment to find out, so the dialog opens first and asks meanwhile.
    native_btn = _choice(app, choices, "drive-harddisk-symbolic", _("install_method_native"),
                         _("task_native_checking"), pill=_("task_recommended"), sensitive=False)
    content.pack_start(native_btn, False, False, 0)
    flat_desc = (_("task_flatpak_desc").format(app_id=flat.package) if flat is not None
                 else (_("task_flatpak_missing") if _flatpak_ids(name) else _("task_flatpak_none")))
    flat_btn = _choice(app, choices, "store-symbolic", _("install_method_flatpak"), flat_desc,
                       sensitive=flat is not None)
    # A tool that isn't on Flathub at all (GameMode, MangoHud...) has
    # just the one choice.
    if name not in TOOLS_BY_NAME or _flatpak_ids(name):
        content.pack_start(flat_btn, False, False, 0)

    row = Gtk.Box(spacing=8)
    row.set_halign(Gtk.Align.END)
    row.set_margin_top(8)
    cancel = app._dialog_button(_("dialog_cancel"), "ghost", "back", "Esc")
    cancel.connect("clicked", lambda *_a: dlg.response(Gtk.ResponseType.CANCEL))
    go = app._dialog_button(_("install_btn"), "accent")
    go.connect("clicked", lambda *_a: dlg.response(Gtk.ResponseType.OK))
    row.pack_start(cancel, False, False, 0)
    row.pack_start(go, False, False, 0)
    content.pack_start(row, False, False, 0)

    state = {"native": None, "picked": False, "closed": False}

    def update_go():
        go.set_sensitive(any(c.get_active() and c.get_sensitive() for c in choices))

    def picked(btn):
        if btn.get_active() and not state.get("setting"):
            state["picked"] = True
        update_go()

    for choice in choices:
        choice.connect("toggled", picked)
    if flat is not None:
        state["setting"] = True
        flat_btn.set_active(True)
        state["setting"] = False
    update_go()

    def checked(result) -> bool:
        if state["closed"]:
            return False
        state["native"] = result
        if isinstance(result, Plan):
            native_btn.desc_label.set_text(_native_desc(app, result))
            native_btn.set_sensitive(True)
            if not state["picked"]:
                state["setting"] = True
                native_btn.set_active(True)
                state["setting"] = False
        else:
            native_btn.desc_label.set_text(_(result.reason).format(**result.args))
        update_go()
        return False

    threading.Thread(target=lambda: GLib.idle_add(checked, native_plan(name)), daemon=True).start()
    content.show_all()
    # Nothing ringed at first; Enter installs the picked one.
    dlg.set_focus(None)

    def on_enter(_w, event) -> bool:
        if event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter) and go.get_sensitive() and dlg.get_focus() is None:
            dlg.response(Gtk.ResponseType.OK)
            return True
        return False
    dlg.connect("key-press-event", on_enter)
    response = dlg.run()
    use_flatpak = flat_btn.get_active()
    state["closed"] = True
    dlg.destroy()
    if response != Gtk.ResponseType.OK:
        return
    plan = flat if use_flatpak else state["native"]
    if not isinstance(plan, Plan):
        return
    _start_install(app, name, plan, parent, on_finished, fallback=None if use_flatpak else flat)


def install_work(app, plan: Plan) -> Tuple[Callable, List[str], str]:
    """The job that carries plan out, its steps and the window's subtitle."""
    _ = app._
    native_steps = ["auth", "prepare", "download", "install"]
    if plan.kind == "flatpak":
        return _job_flatpak_install(plan.package), ["prepare", "download"], _("task_sub_flatpak")
    if plan.kind == "pacman":
        return _job_pacman_install(plan), ["auth", "download", "install"], _("task_sub_native").format(tool="pacman")
    if plan.kind == "aur_helper":
        return _job_aur_helper(plan), ["auth", "download", "build", "install"], \
            _("task_sub_native").format(tool=f"AUR · {plan.tool}")
    if plan.kind == "aur_makepkg":
        return _job_aur_makepkg(plan), ["auth", "download", "build", "install"], \
            _("task_sub_native").format(tool="AUR · makepkg")
    if plan.kind == "apt":
        return _job_apt(plan), native_steps, _("task_sub_native").format(tool=plan.tool)
    if plan.kind == "dnf":
        return _job_dnf(plan), native_steps, _("task_sub_native").format(tool=plan.tool)
    return _job_zypper(plan), ["auth", "download", "install"], _("task_sub_native").format(tool=plan.tool)


def _start_install(app, name: str, plan: Plan, parent: Gtk.Window, on_finished: Callable,
                   fallback: Optional[Plan] = None):
    """The install's progress window. A failed system-package install
    offers fallback - the Flatpak - straight from its error."""
    _ = app._
    work, steps, sub = install_work(app, plan)
    alternative = None
    if fallback is not None:
        def alternative():
            _start_install(app, name, fallback, parent, on_finished)

    tool = TOOLS_BY_NAME.get(name)
    if tool is None:
        ready = _("task_ready_text")
    else:
        ready = _(tool.ready_key or ("tool_ready_text_gui" if tool.gui else "tool_ready_text"))
    TaskWindow(app, parent, name, _("task_install_title").format(name=name), sub, steps, work,
               _("task_ready_title").format(name=name), ready,
               on_success=lambda: _set_enabled(app, name, True), on_closed=on_finished,
               launch_argv=lambda: launch_argv(name), alternative=alternative)


def _installed_from_recipe(family: str, name: str) -> Optional[str]:
    """When the binary's owner can't be asked: the package PlunderHub
    would have installed, if it is installed."""
    if family == "arch":
        candidates = [_arch_package(name) or ""]
    else:
        recipe = _recipe(family, name)
        candidates = list(recipe.packages) if recipe else []
    for pkg in filter(None, candidates):
        if family == "arch":
            code = _run_quiet(["pacman", "-Q", pkg])[0]
        elif family == "debian":
            code, out = _run_quiet(["dpkg-query", "-W", "-f", "${db:Status-Abbrev}", pkg])
            code = 0 if code == 0 and out.startswith("ii") else 1
        else:
            code = _run_quiet(["rpm", "-q", pkg])[0]
        if code == 0:
            return pkg
    return None


def _installed_extras(family: str, name: str) -> List[str]:
    """The tool's extras (its 32-bit build...) that are installed."""
    out = []
    for pkg in _extras(family, name):
        if family == "arch":
            installed = _run_quiet(["pacman", "-Q", pkg])[0] == 0
        elif family == "debian":
            code, status = _run_quiet(["dpkg-query", "-W", "-f", "${db:Status-Abbrev}", pkg])
            installed = code == 0 and status.startswith("ii")
        else:
            installed = _run_quiet(["rpm", "-q", pkg])[0] == 0
        if installed:
            out.append(pkg)
    return out


# (family, packages) -> (package database's mtime, blockers)
_blocker_cache: Dict[Tuple[str, Tuple[str, ...]], Tuple[float, List[str]]] = {}
_PACKAGE_DBS = {"arch": "/var/lib/pacman/local", "debian": "/var/lib/dpkg/status",
                "fedora": "/var/lib/rpm", "suse": "/var/lib/rpm"}


def _removal_blockers(family: str, packages: List[str]) -> List[str]:
    """The installed packages besides these that need them - a gaming
    bundle, a distribution's meta package. pacman refuses the removal
    then, while apt, dnf and zypper would take them along. Asked of the
    package manager without root, as the removal itself would go:
    pacman -Rp, apt-get -s, rpm -e --test."""
    try:
        stamp = os.stat(_PACKAGE_DBS.get(family, "/")).st_mtime
    except OSError:
        stamp = 0.0
    key = (family, tuple(packages))
    cached = _blocker_cache.get(key)
    if cached is not None and cached[0] == stamp:
        return list(cached[1])
    found: List[str] = []
    if family == "arch":
        code, out = _run_quiet_all(["pacman", "-Rsp", "--print-format", "%n"] + packages)
        if code != 0:
            found = re.findall(r"required by (\S+)", out)
    elif family == "debian":
        code, out = _run_quiet_all(["apt-get", "-s", "remove"] + packages)
        found = [m for m in re.findall(r"^Remv (\S+)", out, re.M) if m.split(":")[0] not in packages]
    elif family in ("fedora", "suse"):
        code, out = _run_quiet_all(["rpm", "-e", "--test"] + packages)
        nevras = re.findall(r"is needed by \(installed\) (\S+)", out)
        if nevras:
            names = _run_quiet(["rpm", "-q", "--qf", "%{NAME}\\n"] + nevras)[1].split()
            found = [n for n in names if n not in packages] or nevras
    found = list(dict.fromkeys(found))
    _blocker_cache[key] = (stamp, found)
    return found


def removal_plan(family: str, package: str, extras: List[str]) -> Tuple[List[str], List[str]]:
    """What to remove - the package with its installed extras, which may
    need each other (lib32-vkbasalt needs vkbasalt) - and what else
    installed would stand in the way. Extras something else still needs
    stay, when the package can go without them."""
    full = [package] + [e for e in extras if e != package]
    blockers = _removal_blockers(family, full)
    if blockers and len(full) > 1 and not _removal_blockers(family, [package]):
        return [package], []
    return full, blockers


def _ask_remove(app, name: str, parent: Gtk.Window, on_finished: Callable):
    _ = app._
    state = launcher_state(name)
    if not state.installed:
        return
    family = detect_linux_distro_family()
    if state.method == "flatpak":
        work, steps, sub = _job_flatpak_remove(state.flatpak_id), ["remove"], _("task_sub_flatpak")
    elif immutable_system():
        app._show_info_dialog(_("uninstall_immutable").format(system=immutable_system()),
                              message_type=Gtk.MessageType.WARNING)
        return
    else:
        package = state.package or _installed_from_recipe(family, name)
        if not package or family not in ("arch", "debian", "fedora", "suse") or not shutil.which("pkexec"):
            app._show_info_dialog(_("uninstall_unsupported_distro"), message_type=Gtk.MessageType.WARNING)
            return
        tool = "pacman" if family == "arch" else NATIVE_PACKAGE_TOOLS.get(family, "")
        pre = list(TOOLS_BY_NAME[name].pre_remove) if name in TOOLS_BY_NAME else []
        work, steps, sub = _job_native_remove(family, removal_plan(family, package, _installed_extras(family, name))[0],
                                              pre), \
            ["auth", "remove"], \
            _("task_sub_native").format(tool=tool)

    dlg, content = _dialog(app, parent, _("task_remove_question").format(name=name))
    content.pack_start(_head(app, name, _("task_remove_question").format(name=name)), False, False, 0)
    is_tool = name in TOOLS_BY_NAME
    text = Gtk.Label(label=_("tool_remove_text" if is_tool else "task_remove_text").format(name=name))
    text.set_line_wrap(True)
    text.set_max_width_chars(46)
    text.set_justify(Gtk.Justification.CENTER)
    text.get_style_context().add_class("settings-row-sub")
    content.pack_start(text, False, False, 0)
    row = Gtk.Box(spacing=8, homogeneous=True)
    row.set_margin_top(12)
    cancel = app._dialog_button(_("dialog_cancel"), "ghost", "back", "Esc")
    cancel.connect("clicked", lambda *_a: dlg.response(Gtk.ResponseType.CANCEL))
    remove = app._dialog_button(_("task_remove_btn"), "danger")
    remove.connect("clicked", lambda *_a: dlg.response(Gtk.ResponseType.OK))
    row.pack_start(cancel, True, True, 0)
    row.pack_start(remove, True, True, 0)
    content.pack_start(row, False, False, 0)
    content.show_all()
    dlg.set_focus(None)
    response = dlg.run()
    dlg.destroy()
    if response != Gtk.ResponseType.OK:
        return
    TaskWindow(app, parent, name, _("task_remove_title").format(name=name), sub, steps, work,
               _("task_removed_title").format(name=name),
               _("tool_removed_text" if is_tool else "task_removed_text").format(name=name),
               on_success=lambda: _set_enabled(app, name, False), on_closed=on_finished)
