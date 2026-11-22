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

"""How far a store launcher in a Faugus prefix has got starting up, said
under Setting sail till its window is up (user, 11-22: "Now playing GOG
Galaxy" showed while nothing was on screen - it looked as if nothing
happened). Stages:
  proton      its program isn't running yet (umu / Proton getting ready)
  loading     its program runs, no window of it yet
  background  it already ran before this launch, hidden (closed to its tray):
              asked again, most launchers bring their window back
  restarting  no window came back (GOG Galaxy under Wine: "Second client
              instance detected ... Failed to locate Galaxy Client window"):
              stopped and started again - unless it's busy
  busy        hidden and working (downloading): left alone, the player is
              told to open it from its tray icon
  ready       a window of a process in its prefix is up (or, where the
              desktop lists no windows, a while after its program started).
Followed off the GTK thread (a KWin window listing is a script round trip)."""

import os
import signal
import threading
import time
from typing import Callable, Optional, Sequence

GIVE_UP_SECONDS = 180.0
NO_WINDOWS_GRACE = 12.0          # no window listing: its program running this long counts as up
BACKGROUND_WAIT = 8.0            # a hidden one asked again: this long for its window to come back
# launchers known never to bring a hidden window back under Wine: restarted at once
NO_RESTORE = {"gog"}
BUSY_SHOWN = 12.0                # "it's working in the background" stays on screen this long
BUSY_WRITES = 2 * 1024 * 1024    # bytes a second its prefix writes = it's downloading


def prefix_pids(prefix: str):
    """{pid: command line (lower case, / for \\)} of the processes in a Wine prefix."""
    roots = {os.path.realpath(prefix), os.path.join(os.path.realpath(prefix), "pfx")}
    out = {}
    for p in os.listdir("/proc"):
        if not p.isdigit():
            continue
        try:
            with open(f"/proc/{p}/environ", "rb") as f:
                env = f.read()
        except OSError:
            continue
        i = env.find(b"WINEPREFIX=")
        if i < 0:
            continue
        value = env[i + len(b"WINEPREFIX="):].split(b"\0", 1)[0].decode("utf-8", "replace").rstrip("/")
        if os.path.realpath(value) not in roots:
            continue
        try:
            with open(f"/proc/{p}/cmdline", "rb") as f:
                out[int(p)] = f.read().replace(b"\0", b" ").decode("utf-8", "replace").replace("\\", "/").lower()
        except OSError:
            continue
    return out


_prefix_pids = prefix_pids


def _container_pids(prefix: str):
    """umu's container around the prefix (srt-bwrap, pv-adverb...): no WINEPREFIX
    of their own, the prefix's path on their command line."""
    mine = os.getpid()
    key = os.path.realpath(prefix).encode()
    out = []
    for p in os.listdir("/proc"):
        if not p.isdigit() or int(p) == mine:
            continue
        try:
            with open(f"/proc/{p}/cmdline", "rb") as f:
                cmd = f.read()
        except OSError:
            continue
        if key in cmd and (b"pressure-vessel" in cmd or b"srt-bwrap" in cmd or b"pv-adverb" in cmd
                           or b"umu-run" in cmd or b"/proton " in cmd.replace(b"\0", b" ")):
            out.append(int(p))
    return out


# lock files a launcher leaves behind when it's stopped hard (session end, its prefix killed):
# GOG Galaxy then takes its own lock for a "Second client instance", finds no window and quits
# at once - every other start failed so (11-22: "crashed"); its service can't tell under Wine
STALE_LOCKS = ("drive_c/ProgramData/GOG.com/Galaxy/lock-files",)


def clear_stale_locks(path: str) -> int:
    """Before a start in a Wine prefix where nothing runs: the lock files a
    launcher left there removed. How many were."""
    if "/drive_c/" not in (path or ""):
        return 0
    prefix = path.split("/drive_c/")[0]
    removed = 0
    for rel in STALE_LOCKS:
        folder = os.path.join(prefix, rel)
        try:
            names = os.listdir(folder)
        except OSError:
            continue
        if not names:
            continue
        try:
            if prefix_pids(prefix):
                return 0        # something of it still runs: its locks may be real
        except OSError:
            return 0
        for name in names:
            try:
                os.remove(os.path.join(folder, name))
                removed += 1
            except OSError:
                pass
    return removed


def stop_prefix(prefix: str, wait: float = 15.0) -> bool:
    """Every process in the prefix stopped (TERM, then KILL), and umu's container
    around it gone too - a launcher started again while the old container still
    went down found its old lock taken (GOG Galaxy: "Second client instance
    detected"); True when all is gone."""
    def left():
        return list(prefix_pids(prefix)) + _container_pids(prefix)
    for sig, until in ((signal.SIGTERM, 6.0), (signal.SIGKILL, wait)):
        for pid in left():
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        end = time.time() + until
        while time.time() < end:
            if not left():
                time.sleep(3.0)          # its locks let go (a Galaxy started 1 s after still found its own taken)
                return True
            time.sleep(0.3)
    return not left()


class LauncherStart:
    def __init__(self, prefix: str, program_tails: Sequence[str], list_windows: Optional[Callable],
                 busy: Optional[Callable[[], bool]] = None, restart: Optional[Callable[[], None]] = None):
        self.prefix = prefix
        self.tails = [t.replace("\\", "/").lower() for t in program_tails if t]
        self.list_windows = list_windows        # () -> [(wid, pid, workspace)] or None
        self.busy = busy                        # () -> True while it downloads (its own records)
        self.restart = restart                  # stops it and starts it again (called on the GTK thread)
        self.kind = ""
        self.windows = []
        self.on_window: Optional[Callable] = None   # ([(wid, pid, workspace)]) once its window is up
        self.started = time.time()
        self._program_at: Optional[float] = None
        self._stop = False
        self._restarted = False
        self._busy_at: Optional[float] = None
        running = self._program_running(prefix_pids(prefix))
        self.stage = "background" if running else "proton"
        if running:
            self._program_at = self.started
        threading.Thread(target=self._run, name="plunderhub-launcher-start", daemon=True).start()

    def stop(self):
        self._stop = True

    def _program_running(self, pids) -> bool:
        return any(t in cmd for cmd in pids.values() for t in self.tails)

    def _windows_up(self, pids) -> Optional[bool]:
        if self.list_windows is None:
            return None
        try:
            windows = self.list_windows()
        except Exception:
            return None
        if windows is None:
            return None
        self.windows = [w for w in windows if w[1] and w[1] in pids]
        return bool(self.windows)

    def _is_busy(self) -> bool:
        try:
            if self.busy is not None and self.busy():
                return True
        except Exception:
            pass
        from plunderhub.core.prefix_io import PrefixWriteRate
        meter = PrefixWriteRate(self.prefix)
        meter.poll()
        time.sleep(2.0)
        return meter.poll() > BUSY_WRITES

    def restarted(self):
        """Its restart is done (its program started again): followed from the start."""
        self._restarts = getattr(self, "_restarts", 0) + 1
        self._restarted = True
        self._program_at = None
        self.started = time.time()
        self.stage = "proton"

    def _run(self):
        while not self._stop and self.stage != "ready":
            now = time.time()
            if now - self.started > GIVE_UP_SECONDS:
                self.stage = "ready"
                break
            if self.stage == "restarting":
                time.sleep(0.5)
                continue
            if self.stage == "busy":
                if now - (self._busy_at or now) > BUSY_SHOWN:
                    self.stage = "ready"
                time.sleep(0.5)
                continue
            try:
                pids = prefix_pids(self.prefix)
            except OSError:
                pids = {}
            running = self._program_running(pids)
            if running:
                self._gone_at = None
            elif self._program_at is not None:
                self._gone_at = getattr(self, "_gone_at", None) or now
            if self._program_at is None and running:
                self._program_at = now
                self.stage = "loading"
            elif self.stage == "loading" and not running and self._program_at is not None \
                    and now - (self._gone_at or now) >= 3 and self._restarted \
                    and getattr(self, "_restarts", 0) < 2 and self.restart is not None:
                # (gone for 3 s: Galaxy's first process hands over to the next one in a blink)
                # started again, it quit at once with no window (an old lock still taken): once more
                self.stage = "restarting"
                self.restart()
                continue
            if self._program_at is not None:
                up = self._windows_up(pids)
                if up:
                    if self.on_window is not None:
                        try:
                            self.on_window(self.windows)      # brought to where the player is
                        except Exception:
                            pass
                    self.stage = "ready"
                    break
                if up is None and now - self._program_at > NO_WINDOWS_GRACE:
                    self.stage = "ready"
                    break
                wait = 0.0 if self.kind in NO_RESTORE else BACKGROUND_WAIT
                if self.stage == "background" and now - self.started > wait:
                    # hidden, and asking again didn't bring it back
                    if self._is_busy():
                        self._busy_at = time.time()
                        self.stage = "busy"
                    elif self.restart is not None and not self._restarted:
                        self.stage = "restarting"
                        self.restart()
                    else:
                        self.stage = "ready"
            time.sleep(1.0)
