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
"""The "Auto" update: installs a newer GitHub release the same way the
running copy was installed, then restarts into it.

How PlunderHub was installed decides what an update even means, so the
first thing is working that out from where this package was loaded:

  - /usr/lib/plunderhub        pacman package (makepkg -si). The release
                               source is downloaded, built into a package
                               with makepkg as the user, and installed
                               with `pkexec pacman -U`.
  - /usr/local/lib/plunderhub  install.sh. The release source is
                               downloaded and its own install.sh run
                               through pkexec.
  - a git checkout             `git pull --ff-only` - nothing to install,
                               the checkout IS the installation.

Anything else (an unpacked source archive, a copy somewhere unexpected)
gets no Auto update - guessing at how to replace files we didn't put
there is how installs get broken.

Everything here is blocking and meant for a background thread. Progress
goes out through the callbacks given to Updater; nothing in this module
touches GTK.
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import urllib.request
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from plunderhub.core.update_check import GITHUB_REPO
from plunderhub.logging_setup import logger

METHOD_PACMAN = "pacman"
METHOD_SCRIPT = "script"
METHOD_GIT = "git"
METHOD_UNSUPPORTED = "unsupported"

STEP_DOWNLOAD = "download"
STEP_PULL = "pull"
STEP_BUILD = "build"
STEP_INSTALL = "install"

PACMAN_ROOT = Path("/usr/lib/plunderhub")
SCRIPT_ROOT = Path("/usr/local/lib/plunderhub")

# How much of the overall progress bar each step gets. The download is
# quick next to makepkg, but it's the one step with a real byte count,
# so it gets enough room for that movement to be visible.
_STEP_WEIGHTS = {
    STEP_DOWNLOAD: 0.30,
    STEP_PULL: 1.00,
    STEP_BUILD: 0.45,
    STEP_INSTALL: 0.25,
}

# makepkg's own "==>" milestones, in the order it prints them, mapped to
# how far through the build step each one is. Lines not listed here
# still show up in the dialog, they just don't move the bar.
_MAKEPKG_MILESTONES = (
    ("Retrieving sources", 0.10),
    ("Extracting sources", 0.25),
    ("Entering fakeroot", 0.45),
    ("Starting package()", 0.55),
    ("Tidying install", 0.70),
    ("Creating package", 0.85),
    ("Finished making", 1.00),
)

# pkexec's exit codes for "the user dismissed the password prompt" and
# "couldn't authorize at all" (wrong password, no polkit agent running).
_PKEXEC_DISMISSED = 126
_PKEXEC_NOT_AUTHORIZED = 127


class UpdateError(Exception):
    """A failed update. `key` is an i18n string key for the user-facing
    reason; `detail` is the raw output/exception behind it, shown in the
    dialog's details pane rather than in the headline."""

    def __init__(self, key: str, detail: str = ""):
        super().__init__(detail or key)
        self.key = key
        self.detail = detail


class UpdateCancelled(Exception):
    pass


def package_root() -> Path:
    """The directory the plunderhub package was imported from sits in -
    /usr/lib/plunderhub for a pacman install, the checkout itself when
    running from source."""
    import plunderhub
    return Path(plunderhub.__file__).resolve().parent.parent


def _same_dir(root: Path, fixed: Path) -> bool:
    """package_root() is symlink-resolved, so the fixed path has to be
    too: on Fedora Atomic (Bazzite, Silverblue, Kinoite...) /usr/local is
    a symlink to /var/usrlocal, and a plain compare called install.sh's
    install "unsupported" there (Auto update greyed out)."""
    try:
        return root == fixed.resolve()
    except OSError:
        return root == fixed


def detect_install_method() -> str:
    root = package_root()
    if _same_dir(root, PACMAN_ROOT):
        return METHOD_PACMAN
    if _same_dir(root, SCRIPT_ROOT):
        return METHOD_SCRIPT
    if (root / ".git").exists():
        return METHOD_GIT
    return METHOD_UNSUPPORTED


def missing_tools(method: str) -> List[str]:
    """Commands the given method needs that aren't on PATH. makepkg can't
    build a package without fakeroot (both come with base-devel), and
    every method that writes to system directories needs pkexec."""
    needed = {
        METHOD_PACMAN: ("makepkg", "fakeroot", "pacman", "pkexec"),
        METHOD_SCRIPT: ("sh", "pkexec"),
        METHOD_GIT: ("git",),
    }.get(method, ())
    return [tool for tool in needed if shutil.which(tool) is None]


def steps_for(method: str) -> List[str]:
    return {
        METHOD_PACMAN: [STEP_DOWNLOAD, STEP_BUILD, STEP_INSTALL],
        METHOD_SCRIPT: [STEP_DOWNLOAD, STEP_INSTALL],
        METHOD_GIT: [STEP_PULL],
    }.get(method, [])


def launcher_path(method: str) -> Optional[Path]:
    """What to run to start the freshly installed version."""
    if method == METHOD_PACMAN:
        return Path("/usr/bin/plunderhub")
    if method == METHOD_SCRIPT:
        return Path("/usr/local/bin/plunderhub")
    if method == METHOD_GIT:
        return package_root() / "plunderhub.py"
    return None


# Started as a separate process, outside this one's session, so it
# survives PlunderHub quitting. It waits for the old process to be gone
# before starting the new one: PlunderHub is a single-instance
# Gtk.Application, so a new copy started while the old one still holds
# the D-Bus name would just hand over to it and exit.
# A process that has exited but not been reaped yet (state "Z") still
# answers kill(pid, 0), so /proc is checked too.
_RESTART_HELPER = """\
import os, sys, time
pid = int(sys.argv[1])

def alive():
    try:
        with open(f"/proc/{pid}/stat") as f:
            return f.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False

deadline = time.monotonic() + 30
while alive() and time.monotonic() < deadline:
    time.sleep(0.1)
os.execv(sys.argv[2], sys.argv[2:])
"""


def spawn_restart(method: str) -> bool:
    """Launches the helper that starts the new version once this process
    has exited. The caller still has to quit the app itself."""
    launcher = launcher_path(method)
    if launcher is None or not launcher.exists():
        logger.warning(f"Can't restart after update: no launcher at {launcher}")
        return False
    try:
        subprocess.Popen(
            [sys.executable, "-c", _RESTART_HELPER, str(os.getpid()),
             sys.executable, str(launcher)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
    except OSError as e:
        logger.warning(f"Can't restart after update: {e}")
        return False
    return True


def spawn_relaunch() -> bool:
    """Starts this same PlunderHub again once this process has exited
    (after Sink the config). The caller still has to quit the app."""
    script = Path(sys.argv[0]).resolve() if sys.argv and sys.argv[0] else None
    if script is None or not script.is_file():
        logger.warning(f"Can't relaunch: no script at {script}")
        return False
    try:
        subprocess.Popen(
            [sys.executable, "-c", _RESTART_HELPER, str(os.getpid()), sys.executable, str(script)],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True, close_fds=True,
        )
    except OSError as e:
        logger.warning(f"Can't relaunch: {e}")
        return False
    return True


class Updater:
    """Runs one update from start to finish. `on_step(step)` fires when a
    step starts; `on_progress(step, fraction, status)` reports progress
    inside it - fraction is 0..1, or None when the step can't tell how
    far along it is; `on_log(line)` gets every line of output worth
    showing in the details pane.

    run() returns normally on success, and raises UpdateError or
    UpdateCancelled otherwise. cancel() may be called from any thread;
    it is honoured up to the start of the install step, after which the
    update is left to finish rather than abandoned half-installed."""

    def __init__(self, method: str, tag: str,
                 on_step: Callable[[str], None],
                 on_progress: Callable[[str, Optional[float], str], None],
                 on_log: Callable[[str], None]):
        self.method = method
        self.tag = tag
        self._on_step = on_step
        self._on_progress = on_progress
        self._on_log = on_log
        self._cancelled = threading.Event()
        self._proc_lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._cancellable = True

    # --- control -------------------------------------------------------

    def cancel(self):
        if not self._cancellable:
            return
        self._cancelled.set()
        with self._proc_lock:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()

    @property
    def cancellable(self) -> bool:
        return self._cancellable

    def _check_cancelled(self):
        if self._cancelled.is_set():
            raise UpdateCancelled()

    # --- the update itself ---------------------------------------------

    def run(self):
        if self.method == METHOD_GIT:
            self._git_pull()
            return
        if self.method not in (METHOD_PACMAN, METHOD_SCRIPT):
            raise UpdateError("update_auto_unavailable_install")

        workdir = Path(tempfile.mkdtemp(prefix="plunderhub-update-"))
        try:
            self._on_step(STEP_DOWNLOAD)
            source_dir = self._download_and_extract(workdir)
            self._check_cancelled()
            if self.method == METHOD_PACMAN:
                self._on_step(STEP_BUILD)
                package = self._makepkg(source_dir, workdir)
                self._check_cancelled()
                self._on_step(STEP_INSTALL)
                self._cancellable = False
                self._run_privileged(["pacman", "-U", "--noconfirm", str(package)])
            else:
                self._on_step(STEP_INSTALL)
                self._cancellable = False
                # install.sh is plain POSIX sh: no bash on Alpine or Void
                self._run_privileged([shutil.which("sh") or "/bin/sh",
                                      str(source_dir / "install.sh")])
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _source_url(self) -> Tuple[str, Optional[str]]:
        """Where the release's source comes from, and its sha256 when
        GitHub gives one: the tarball attached to the release
        (plunderhub-X.Y.Z.tar.gz, since 1.0.2 - its downloads are counted),
        else GitHub's own archive of the tag (older releases, or the API
        can't be reached / its hourly limit is used up)."""
        from plunderhub.core.utils import http_get_json
        archive_url = f"https://github.com/{GITHUB_REPO}/archive/refs/tags/{self.tag}.tar.gz"
        version = self.tag[1:] if self.tag.startswith("v") else self.tag
        data = http_get_json(f"https://api.github.com/repos/{GITHUB_REPO}/releases/tags/{self.tag}",
                             {"Accept": "application/vnd.github+json"}, timeout=10)
        assets = data.get("assets") if isinstance(data, dict) else None
        for asset in assets or []:
            if asset.get("name") == asset_name(version) and asset.get("browser_download_url"):
                digest = str(asset.get("digest") or "")
                return asset["browser_download_url"], (digest[7:].lower() if digest.startswith("sha256:")
                                                        else None)
        return archive_url, None

    def _download_and_extract(self, workdir: Path) -> Path:
        url, sha256 = self._source_url()
        archive = workdir / "source.tar.gz"
        self._on_log(f"GET {url}")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "PlunderHub-updater"})
            with urllib.request.urlopen(req, timeout=30) as resp, open(archive, "wb") as out:
                total = int(resp.headers.get("Content-Length") or 0)
                done = 0
                while True:
                    self._check_cancelled()
                    chunk = resp.read(64 * 1024)
                    if not chunk:
                        break
                    out.write(chunk)
                    done += len(chunk)
                    fraction = min(done / total, 1.0) if total else None
                    self._on_progress(STEP_DOWNLOAD, fraction, _format_size(done))
        except UpdateCancelled:
            raise
        except Exception as e:
            raise UpdateError("update_error_download", str(e))
        self._on_log(f"Downloaded {_format_size(archive.stat().st_size)}")
        if sha256:
            found = _sha256(archive)
            if found != sha256:
                raise UpdateError("update_error_download",
                                  f"The download's checksum doesn't match GitHub's ({found} != {sha256})")
            self._on_log("Checksum OK (sha256, as GitHub lists it)")

        extract_dir = workdir / "source"
        extract_dir.mkdir()
        try:
            with tarfile.open(archive) as tar:
                _safe_extract(tar, extract_dir)
        except Exception as e:
            raise UpdateError("update_error_download", f"Couldn't unpack the archive: {e}")

        entries = [p for p in extract_dir.iterdir() if p.is_dir()]
        if len(entries) != 1:
            raise UpdateError("update_error_download", "Unexpected archive layout")
        source_dir = entries[0]

        # The tag says which version this should be; the code inside is
        # what actually gets installed. Refusing a mismatch means a
        # mislabelled release can't put the app into an update loop
        # where it never stops offering the same version.
        found = _read_app_version(source_dir / "plunderhub" / "constants.py")
        expected = self.tag[1:] if self.tag.startswith("v") else self.tag
        if found != expected:
            raise UpdateError("update_error_version_mismatch",
                              f"Tag {self.tag} contains APP_VERSION {found!r}")
        self._on_progress(STEP_DOWNLOAD, 1.0, "")
        return source_dir

    def _makepkg(self, source_dir: Path, workdir: Path) -> Path:
        out_dir = workdir / "out"
        out_dir.mkdir()
        env = dict(os.environ)
        # Keep every makepkg output inside workdir, whatever the user's
        # makepkg.conf says - otherwise the package could land in a
        # PKGDEST we'd then have to go looking for.
        env.update(PKGDEST=str(out_dir), SRCDEST=str(workdir / "srcdest"),
                   BUILDDIR=str(workdir / "build"), SRCPKGDEST=str(out_dir))
        # --nodeps: installing missing dependencies needs root, and
        # `pacman -U` below resolves them from the repos anyway.
        self._run(["makepkg", "--force", "--nodeps", "--noconfirm", "--noprogressbar"],
                  cwd=source_dir, env=env, step=STEP_BUILD, error_key="update_error_build",
                  milestones=_MAKEPKG_MILESTONES)
        packages = sorted(p for p in out_dir.glob("plunderhub-*.pkg.tar.*")
                          if "-debug-" not in p.name and not p.name.endswith(".sig"))
        if not packages:
            raise UpdateError("update_error_build", "makepkg finished but produced no package")
        return packages[-1]

    def _run_privileged(self, cmd: List[str]):
        self._on_progress(STEP_INSTALL, None, "")
        self._on_log("Waiting for authorization (pkexec)…")
        self._run(["pkexec"] + cmd, step=STEP_INSTALL, error_key="update_error_install")

    def _git_pull(self):
        self._on_step(STEP_PULL)
        self._run(["git", "-C", str(package_root()), "pull", "--ff-only", "--progress"],
                  step=STEP_PULL, error_key="update_error_pull")

    def _run(self, cmd: List[str], step: str, error_key: str,
             cwd: Optional[Path] = None, env: Optional[dict] = None,
             milestones=()):
        self._check_cancelled()
        self._on_log("$ " + " ".join(cmd))
        try:
            with self._proc_lock:
                self._proc = subprocess.Popen(
                    cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, errors="replace", bufsize=1,
                )
            proc = self._proc
        except OSError as e:
            raise UpdateError(error_key, str(e))

        fraction: Optional[float] = None
        tail: List[str] = []
        assert proc.stdout is not None
        for raw in proc.stdout:
            line = raw.rstrip()
            if not line:
                continue
            status = _strip_makepkg_arrow(line)
            # git --progress redraws "Receiving objects:  40% (2/5)" in
            # place; every redraw arrives as its own line here. They
            # belong in the status line, not one by one in the log.
            if _GIT_PROGRESS_RE.match(line) and not line.endswith("done."):
                self._on_progress(step, fraction, status)
                continue
            self._on_log(line)
            tail = (tail + [line])[-15:]
            for marker, value in milestones:
                if marker in line:
                    fraction = value
            self._on_progress(step, fraction, status)
        code = proc.wait()
        with self._proc_lock:
            self._proc = None

        if self._cancelled.is_set() and self._cancellable:
            raise UpdateCancelled()
        if code == 0:
            self._on_progress(step, 1.0, "")
            return
        if cmd[0] == "pkexec" and code in (_PKEXEC_DISMISSED, _PKEXEC_NOT_AUTHORIZED):
            raise UpdateError("update_error_auth", "\n".join(tail))
        raise UpdateError(error_key, f"exit code {code}\n" + "\n".join(tail))


def asset_name(version: str) -> str:
    """The source tarball attached to a release (PlunderHubTools/
    make_release_asset.sh makes it from the tag)."""
    return f"plunderhub-{version}.tar.gz"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


_GIT_PROGRESS_RE = re.compile(r"^(remote: )?[A-Za-z ]+:\s+\d+% \(")
_APP_VERSION_RE = re.compile(r'^APP_VERSION\s*=\s*"([^"]*)"', re.MULTILINE)


def _read_app_version(constants_py: Path) -> Optional[str]:
    try:
        match = _APP_VERSION_RE.search(constants_py.read_text(encoding="utf-8"))
    except OSError:
        return None
    return match.group(1) if match else None


def _safe_extract(tar: tarfile.TarFile, dest: Path):
    """extractall() with the "data" filter where Python has it (3.12,
    and security backports to 3.9+), which refuses absolute paths, ".."
    and links pointing outside dest. On an older 3.9 without it, the
    same refusal by hand for what a GitHub source archive can contain."""
    if hasattr(tarfile, "data_filter"):
        tar.extractall(dest, filter="data")
        return
    dest_resolved = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if dest_resolved not in target.parents and target != dest_resolved:
            raise ValueError(f"Unsafe path in archive: {member.name}")
        if member.issym() or member.islnk():
            link = (target.parent / member.linkname).resolve()
            if dest_resolved not in link.parents:
                raise ValueError(f"Unsafe link in archive: {member.name}")
        elif not (member.isfile() or member.isdir()):
            raise ValueError(f"Unsupported entry in archive: {member.name}")
    tar.extractall(dest)


def _strip_makepkg_arrow(line: str) -> str:
    for prefix in ("==> ", "  -> "):
        if line.startswith(prefix):
            return line[len(prefix):]
    return line


def _format_size(n: int) -> str:
    if n < 1024 * 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def overall_fraction(steps: List[str], step: str, fraction: float) -> float:
    """Where `fraction` of the way through `step` lands on the dialog's
    single overall progress bar."""
    total = sum(_STEP_WEIGHTS[s] for s in steps) or 1.0
    before = 0.0
    for s in steps:
        if s == step:
            return (before + _STEP_WEIGHTS[s] * max(0.0, min(fraction, 1.0))) / total
        before += _STEP_WEIGHTS[s]
    return 0.0

