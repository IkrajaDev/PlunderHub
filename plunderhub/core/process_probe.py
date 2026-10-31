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


import threading
import time
from typing import Dict, Optional, Tuple, Union

from plunderhub.core.steam_heroic import is_steam_appid_running
from plunderhub.core.steam_launch_status import SteamLaunchStage, steam_launch_stage
from plunderhub.core.utils import GameProcessMatch, is_directory_process_running, is_game_process_running
from plunderhub.logging_setup import logger


Reading = Optional[bool]
# (steam appid, what identifies the game's processes) - the latter either
# a plain install directory or a full GameProcessMatch.
ProbeTarget = Tuple[Optional[str], Union[None, str, GameProcessMatch]]


class ProcessProbe:

    def __init__(self, interval: float = 1.0):
        self._interval = interval
        self._lock = threading.Lock()
        self._targets: Dict[str, ProbeTarget] = {}
        self._results: Dict[str, Reading] = {}
        # Steam launches still on their way: uid -> when asked for
        self._launches: Dict[str, float] = {}
        self._stages: Dict[str, SteamLaunchStage] = {}
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="plunderhub-probe",
                                        daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            # join() returns quickly - the wake.set() above interrupts
            # the wait() in _run() immediately rather than making this
            # wait out the full poll interval, and the loop itself
            # checks _stop between individual targets, so at most one
            # more _probe() call (a fast local check, not a network
            # call) runs before it exits. A timeout is still kept as a
            # safety net so a genuinely stuck probe can never hang
            # shutdown forever.
            self._thread.join(timeout=2.0)
            # Resetting this is what actually makes start() usable
            # again afterward - the guard there (`if self._thread is
            # not None: return`) would otherwise keep pointing at this
            # now-finished thread forever, silently refusing to spawn
            # a new one on any future start() call.
            self._thread = None
        # Cleared, not left set - a future start() spawns a fresh _run()
        # loop that immediately re-checks `while not self._stop.is_set()`;
        # leaving it set would make that loop exit before its first
        # iteration, silently doing nothing.
        self._stop.clear()

    def set_targets(self, targets: Dict[str, ProbeTarget],
                    launches: Optional[Dict[str, float]] = None):
        """`launches`: the Steam games among them still launching, with
        when each was asked for, to follow in Steam's logs (stage())."""
        launches = {uid: t for uid, t in (launches or {}).items() if uid in targets}
        with self._lock:
            self._targets = dict(targets)
            self._launches = launches
            self._results = {uid: r for uid, r in self._results.items() if uid in targets}
            self._stages = {uid: st for uid, st in self._stages.items() if uid in launches}
        self._wake.set()

    def stage(self, uid: str) -> Optional[SteamLaunchStage]:
        with self._lock:
            return self._stages.get(uid)

    def reading(self, uid: str) -> Reading:
        with self._lock:
            return self._results.get(uid)

    def _run(self):
        while not self._stop.is_set():
            with self._lock:
                targets = dict(self._targets)
                launches = dict(self._launches)
            fresh: Dict[str, Reading] = {}
            stages: Dict[str, SteamLaunchStage] = {}
            for uid, (appid, process_match) in targets.items():
                if self._stop.is_set():
                    return
                fresh[uid] = self._probe(uid, appid, process_match)
                if appid is not None and uid in launches:
                    stage = steam_launch_stage(appid, launches[uid])
                    if stage is not None:
                        stages[uid] = stage
            with self._lock:
                self._results = {uid: r for uid, r in fresh.items() if uid in self._targets}
                self._stages = {uid: st for uid, st in stages.items() if uid in self._launches}
            self._wake.wait(self._interval)
            self._wake.clear()

    def _probe(self, uid: str, appid: Optional[str],
               process_match: Union[None, str, GameProcessMatch]) -> Reading:
        try:
            if appid is not None:
                reading = is_steam_appid_running(appid)
                if reading is not None:
                    return reading
            if isinstance(process_match, GameProcessMatch):
                return is_game_process_running(process_match)
            if process_match:
                return is_directory_process_running(process_match)
        except Exception as e:
            logger.debug(f"Process probe for {uid} failed: {e}")
        return None


def wait_for_first_reading(probe: ProcessProbe, uid: str, timeout: float = 2.0) -> Reading:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        reading = probe.reading(uid)
        if reading is not None:
            return reading
        time.sleep(0.02)
    return probe.reading(uid)

