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

"""Keeping the Proton builds in step without being asked:

- a little after start (the window has built itself up), every build of the
  store into every launcher's folder (one installed since gets them), then
  a look for a newer Proton-GE: offered on the sidebar's card, downloaded
  only by its Download button - or by itself when Settings says so
  ("proton_auto_download", off by default);
- after every library scan, the folders again (a launcher just installed);
- while a build downloads, the games that run on it (their own choice or
  their launcher's default) wait: App._proton_waits makes their cards say
  so and their launch refused; the others start as usual;
- an update that waited for a game to close is swapped in once it has."""

import threading
import time
from pathlib import Path
from typing import Dict, Optional, Set

from plunderhub.core import proton_store
from plunderhub.gi_setup import GLib
from plunderhub.logging_setup import logger
from plunderhub.ui.proton_jobs import jobs

START_DELAY_MS = 2500
AUTO_KEY = "proton_auto_download"
# Proton-GE Latest missing and fetching it failed (automatic mode): tried
# again after this (after GitHub's hourly limit resets, when that was it)
RETRY_S = 15 * 60
PENDING_RETRY_S = 30


# {removed build: [uids of Steam games still on it - Steam ran]}
REASSIGN_KEY = "proton_reassign"


def _set_to_missing(entry) -> bool:
    """Whether the game is set to a Proton that isn't there any more: a
    path that's gone (Heroic, Faugus...), or a Steam compat tool Steam
    doesn't have - not one of Steam's own (proton_...), which Steam
    downloads again itself."""
    choice = entry.choice
    if not choice:
        return False
    if "/" in choice:
        return not Path(choice).exists()
    if entry.launcher == "Steam":
        if choice.startswith(("proton_", "steamlinuxruntime")):
            return False    # Steam's own (Proton, Steam Linux Runtime): it fetches them itself
        try:
            # what Steam has, without the game's own choice added in
            installed = {c.id for c in entry.backend.proton_choices(entry.game, None)}
        except Exception:
            return False
        return choice not in installed
    return False


def move_games_off(app, name: Optional[str], only: Optional[Set[str]] = None) -> Set[str]:
    """Games set to the build `name` (removed) - or, with None, to any
    Proton that's gone - onto Proton-GE Latest, or their launcher's default
    when there's no Latest. Worker thread. A Steam game waits while Steam
    runs (it can't be written then): it's remembered in the settings and
    moved at a later round. Returns the ones still waiting. Never in a
    self-test, which mustn't write to the launchers."""
    import sys
    if "--self-test" in sys.argv:
        return set()
    from plunderhub.ui.proton_page import _game_proton, _proton_name
    latest = proton_store.FAMILIES["ge"].latest_name
    have_latest = proton_store.find(latest) is not None
    waiting: Set[str] = set()
    for game in list(app.game_index.values()):
        if only is not None and game.uid not in only:
            continue
        entry = _game_proton(game)
        if entry is None or entry.native or not entry.choice:
            continue
        if (entry.proton_name() != name) if name is not None else not _set_to_missing(entry):
            continue
        gone = name or entry.proton_name()
        target = ""
        if have_latest:
            target = next((i for i, label in entry.choices
                           if _proton_name(i if "/" in i else label) == latest), "")
        try:
            if entry.backend.blocking_process_running():
                waiting.add(game.uid)
                continue
            opts = entry.backend.load(game)
            opts.proton = target or None
            entry.backend.save(game, opts)
            logger.info(f"Proton: {game.title} was on {gone}, which is gone - now on "
                        f"{latest if target else 'the launcher default'}")
        except Exception as e:
            logger.warning(f"Proton: couldn't move {game.title} off {gone}: {e}")
    if name is not None:
        GLib.idle_add(_remember_waiting, app, name, waiting)
    return waiting


def _remember_waiting(app, name: str, waiting: Set[str]) -> bool:
    from plunderhub.core.persistence import save_settings
    kept = dict(app.settings.get(REASSIGN_KEY) or {})
    if waiting:
        kept[name] = sorted(waiting)
    else:
        kept.pop(name, None)
    if kept != (app.settings.get(REASSIGN_KEY) or {}):
        app.settings[REASSIGN_KEY] = kept
        save_settings(app.settings)
    return False


class ProtonUpkeep:
    def __init__(self, app):
        from plunderhub.ui.proton_page import S
        S.bind(app)
        self.app = app
        self.jobs = jobs(app)
        self.waits_for: Optional[str] = None
        self._pending_watch: Optional[int] = None
        self.jobs.listen(self._changed)

    # --- start, scans -----------------------------------------------------------------

    def start(self):
        GLib.timeout_add(START_DELAY_MS, self._start)

    def _start(self) -> bool:
        def work():
            try:
                proton_store.reconcile()
            except Exception as e:
                logger.warning(f"Proton: couldn't put the builds in place: {e}")
            self._moves_waiting()
            GLib.idle_add(self._auto_check)
        threading.Thread(target=work, daemon=True, name="plunderhub-proton-upkeep").start()
        return False

    def _auto_check(self) -> bool:
        import sys
        self._watch_pending()
        if "--self-test" in sys.argv:
            return False
        if self.app.settings.get(AUTO_KEY, False):
            self.jobs.update_latest("ge", install_missing=True)
            return False

        def work():
            try:
                release = proton_store.latest_outdated("ge")
            except Exception as e:
                logger.info(f"Proton: couldn't look for a newer Proton-GE: {e}")
                return
            self.jobs.checked_at["ge"] = time.time()
            GLib.idle_add(self._offer, release)
        threading.Thread(target=work, daemon=True, name="plunderhub-proton-check").start()
        return False

    def _offer(self, release) -> bool:
        self.jobs.offer = release
        self.jobs._tell(force=True)
        return False

    def after_scan(self):
        """A launcher may have come: the builds into its folders too."""
        def work():
            try:
                proton_store.reconcile()
            except Exception as e:
                logger.warning(f"Proton: couldn't put the builds in place: {e}")
            self._moves_waiting()
        threading.Thread(target=work, daemon=True, name="plunderhub-proton-upkeep").start()

    def _moves_waiting(self):
        """Steam games left on a removed build while Steam ran, and any game
        on a Proton that's gone some other way (worker thread)."""
        for name, uids in dict(self.app.settings.get(REASSIGN_KEY) or {}).items():
            move_games_off(self.app, name, set(uids))
        move_games_off(self.app, None)

    # --- games waiting for a build ------------------------------------------------------

    def _changed(self):
        job = self.jobs.job
        if job is None:
            self._retry_if_missing()
        name = job.name if job is not None and job.kind == "install" else None
        if name == self.waits_for:
            return
        self.waits_for = name
        if name is None:
            self._set_waits({})
            self._watch_pending()
            return
        games = list(self.app.game_index.values())

        def work():
            from plunderhub.ui.proton_page import _game_proton
            affected: Set[str] = set()
            for game in games:
                entry = _game_proton(game)
                if entry is not None and entry.proton_name() == name:
                    affected.add(game.uid)
            GLib.idle_add(done, affected)

        def done(affected) -> bool:
            if self.waits_for == name:
                self._set_waits({uid: name for uid in affected})
            return False
        threading.Thread(target=work, daemon=True, name="plunderhub-proton-games").start()

    def _retry_if_missing(self):
        """Automatic mode: the first Proton-GE Latest couldn't be fetched -
        tried again by itself a while later."""
        last = self.jobs.last
        if (getattr(self, "_retry_id", None) is not None or last is None or not last.error
                or last.error == "cancelled" or proton_store.latest_build("ge") is not None
                or not self.app.settings.get(AUTO_KEY, False)):
            return
        problem = last.problem
        minutes = (problem.detail.get("minutes") or 0) if isinstance(problem, proton_store.ProtonError) \
            and problem.kind == "rate_limit" else 0
        delay = (minutes + 1) * 60 if minutes else RETRY_S
        logger.info(f"Proton: Proton-GE Latest not there yet - trying again in {delay // 60} min")

        def retry() -> bool:
            self._retry_id = None
            self._auto_check()
            return False
        self._retry_id = GLib.timeout_add_seconds(delay, retry)

    def _set_waits(self, waits: Dict[str, str]):
        if waits == self.app._proton_waits:
            return
        if waits:
            logger.info(f"Proton: {len(waits)} game(s) wait for {next(iter(waits.values()))}")
        self.app._proton_waits = waits
        self.app._render_launchers()

    def wait_text(self, uid: str) -> Optional[str]:
        from plunderhub.ui.proton_page import S
        name = self.app._proton_waits.get(uid)
        if name is None:
            return None
        # the progress is on the sidebar's card and the Proton page
        return S["wait_status"].format(name=name)

    # --- an update waiting for a game to close ----------------------------------------------

    def _watch_pending(self):
        if self._pending_watch is None and proton_store.pending():
            self._pending_watch = GLib.timeout_add_seconds(PENDING_RETRY_S, self._try_pending)

    def _try_pending(self) -> bool:
        if self.jobs.busy():
            return True
        if not proton_store.pending():
            self._pending_watch = None
            return False

        def work():
            done = proton_store.finish_pending()
            if done:
                logger.info(f"Proton: {', '.join(done)} swapped in")
                GLib.idle_add(self.jobs._tell_now)
        threading.Thread(target=work, daemon=True, name="plunderhub-proton-upkeep").start()
        return True


def upkeep(app) -> ProtonUpkeep:
    existing = getattr(app, "_proton_upkeep", None)
    if existing is None:
        existing = app._proton_upkeep = ProtonUpkeep(app)
    return existing
