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

"""The app's Proton work (core/proton_store.py) on a worker thread, one job
at a time - a download, a Latest's update, a removal - and whoever shows it
(Shipwright's Proton page, the home page's card) told on the main thread.
Progress is passed on at most a few times a second, and only when the
shown percentage changes."""

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from plunderhub.core import proton_store
from plunderhub.gi_setup import GLib
from plunderhub.logging_setup import logger


@dataclass
class Job:
    kind: str                       # "install" | "update" | "remove" | "check"
    name: str                       # the build it's about
    family: str = ""
    tag: str = ""                   # the release being installed
    stage: str = "start"            # start | download | check | unpack | done
    done: int = 0
    total: int = 0
    error: str = ""
    # what went wrong, to tell in words (proton_page.problem_text)
    problem: Optional[Exception] = None
    # its failure was told in a dialog - once, by whichever page shows
    told: bool = False
    placed: bool = True             # False: waits for a game to end
    cancel: threading.Event = field(default_factory=threading.Event)

    def percent(self) -> int:
        return int(100 * self.done / self.total) if self.total else 0


class ProtonJobs:
    def __init__(self, app):
        self.app = app
        self.job: Optional[Job] = None
        self.last: Optional[Job] = None     # the one before, finished
        self.checked_at: Dict[str, float] = {}     # family -> when its Latest was last checked
        self._listeners: List[Callable[[], None]] = []
        self._told = 0.0
        self._told_pct = -1

    # --- who's watching -------------------------------------------------------------

    def listen(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Calls callback (main thread) on every change; returns the
        function that stops it."""
        self._listeners.append(callback)
        return lambda: callback in self._listeners and self._listeners.remove(callback)

    def _tell(self, force: bool = False):
        job = self.job
        now = time.monotonic()
        if not force and job is not None:
            pct = job.percent()
            if pct == self._told_pct or now - self._told < 0.25:
                return
            self._told_pct = pct
        self._told = now
        GLib.idle_add(self._tell_now)

    def _tell_now(self) -> bool:
        for callback in list(self._listeners):
            try:
                callback()
            except Exception as e:
                logger.warning(f"Proton job listener failed: {e}")
        return False

    # --- jobs ---------------------------------------------------------------------------

    def busy(self) -> bool:
        return self.job is not None

    def cancel(self):
        if self.job is not None:
            self.job.cancel.set()

    def install(self, release: proton_store.Release, latest: bool = False) -> bool:
        """Downloads release (as its family's Latest with latest); False
        when another job runs."""
        # named as the build will be: games set to it wait for this job
        name = proton_store.FAMILIES[release.family].latest_name if latest else proton_store.build_name(release)
        return self._start(Job("install", name, release.family, release.tag),
                           lambda job: self._install(job, release, latest))

    def update_latest(self, family: str, install_missing: bool = True) -> bool:
        """Looks for a newer release for the family's Latest and installs
        it - also when there's no Latest yet, with install_missing."""
        name = proton_store.FAMILIES[family].latest_name
        return self._start(Job("check", name, family), lambda job: self._update(job, install_missing))

    def remove(self, name: str) -> bool:
        return self._start(Job("remove", name), self._remove)

    def _start(self, job: Job, work: Callable[[Job], None]) -> bool:
        if self.job is not None:
            return False
        self.job = job
        self._told_pct = -1
        self._tell(force=True)

        def run():
            try:
                work(job)
            except proton_store.Cancelled:
                job.error = "cancelled"
            except Exception as e:
                logger.warning(f"Proton {job.kind} of {job.name} failed: {e}")
                job.error = str(e) or type(e).__name__
                job.problem = e
            job.stage = "done"
            GLib.idle_add(self._finish, job)
        threading.Thread(target=run, daemon=True, name="plunderhub-proton").start()
        return True

    def _finish(self, job: Job) -> bool:
        if self.job is job:
            self.job = None
        self.last = job
        self._tell(force=True)
        return False

    def _progress(self, job: Job):
        def report(stage: str, done: int, total: int):
            job.stage, job.done, job.total = stage, done, total
            self._tell(force=stage != "download")
        return report

    def _install(self, job: Job, release: proton_store.Release, latest: bool):
        _build, job.placed = proton_store.install(release, latest, self._progress(job), job.cancel)

    def _update(self, job: Job, install_missing: bool):
        family = job.family
        release = proton_store.latest_outdated(family)
        self.checked_at[family] = time.time()
        if release is None:
            return
        if proton_store.latest_build(family) is None and not install_missing:
            return
        job.kind, job.tag = "install", release.tag
        self._tell(force=True)
        self._install(job, release, True)

    def _remove(self, job: Job):
        proton_store.remove(job.name)
        # games set to it go to Proton-GE Latest - before the job's over,
        # so the page shows them moved once it reads them again
        from plunderhub.ui.proton_upkeep import move_games_off
        move_games_off(self.app, job.name)


def jobs(app) -> ProtonJobs:
    """The app's one ProtonJobs."""
    existing = getattr(app, "_proton_jobs", None)
    if existing is None:
        existing = app._proton_jobs = ProtonJobs(app)
    return existing
