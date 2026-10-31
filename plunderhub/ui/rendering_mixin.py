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


from typing import Dict, List, Optional, Tuple

from plunderhub.core.models import GameItem
from plunderhub.core.persistence import save_settings
from plunderhub.core.scan_paths import LAUNCHER_FLATPAK_IDS, resolve_launcher_invocation
from plunderhub.gi_setup import GdkPixbuf, GLib, Gtk
from plunderhub.launchers.registry import LAUNCHER_REGISTRY, bundled_launcher_icon


class RenderingMixin:
    def _any_launcher_installed(self) -> bool:
        for ldef in LAUNCHER_REGISTRY:
            flatpak_ids = LAUNCHER_FLATPAK_IDS.get(ldef.name, [])
            if ldef.binary and resolve_launcher_invocation(ldef.binary, flatpak_ids) is not None:
                return True
        return False

    def _build_getting_started_box(self, title_text: Optional[str] = None) -> Gtk.Box:
        """The library with nothing in it: a card with the launchers'
        logos, what's wrong in a line, and the way to the Library page.
        title_text is unused - the card picks its own words."""
        no_sources = not self.enabled_launchers
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.get_style_context().add_class("empty-card")
        card.set_halign(Gtk.Align.CENTER)

        logos = Gtk.Box(spacing=14)
        logos.set_halign(Gtk.Align.CENTER)
        logos.set_margin_bottom(10)
        for ldef in LAUNCHER_REGISTRY:
            path = bundled_launcher_icon(ldef.name)
            if path is None:
                continue
            try:
                logo = Gtk.Image.new_from_pixbuf(GdkPixbuf.Pixbuf.new_from_file_at_size(str(path), 44, 44))
            except GLib.Error:
                continue
            logo.set_tooltip_text(ldef.name)
            # the launchers switched on stand out, the others step back
            if ldef.name not in self.enabled_launchers:
                logo.set_opacity(0.45)
            logos.pack_start(logo, False, False, 0)
        card.pack_start(logos, False, False, 0)

        title = Gtk.Label(label=self._("empty_no_sources_title" if no_sources else "empty_no_games_title"))
        title.get_style_context().add_class("empty-title")
        title.set_line_wrap(True)
        title.set_justify(Gtk.Justification.CENTER)
        card.pack_start(title, False, False, 0)
        sub_key = "empty_no_sources_sub" if no_sources else "empty_no_games_sub"
        if not self._any_launcher_installed():
            sub_key = "getting_started_install_hint"
        sub = Gtk.Label(label=self._(sub_key))
        sub.set_line_wrap(True)
        sub.set_max_width_chars(48)
        sub.set_justify(Gtk.Justification.CENTER)
        sub.get_style_context().add_class("dim-label")
        card.pack_start(sub, False, False, 0)

        button = self._dialog_button(self._("empty_setup_btn"), "accent")
        button.set_halign(Gtk.Align.CENTER)
        button.set_margin_top(12)
        button.connect("clicked", lambda *_: self.show_options(initial_tab="library"))
        card.pack_start(button, False, False, 0)
        return card

    def _sort_games(self, games: List[GameItem], per_launcher: Optional[bool] = None) -> List[GameItem]:
        """per_launcher: the custom order of each game's launcher rather
        than All Plunder's own - by default, as the sidebar is on a
        launcher (or one of Heroic's stores) or not."""
        if self.sort_mode == "custom":
            if per_launcher is None:
                per_launcher = self.home_nav.startswith(("l:", "s:"))
            return sorted(games, key=self._launcher_sort_key if per_launcher else self._all_sort_key)
        if self.sort_mode == "playtime":
            return sorted(games, key=lambda g: (-self.playtime.get(g.uid, 0), g.title.lower()))
        if self.sort_mode == "recent":
            return sorted(games, key=lambda g: (-self.recent.get(g.uid, 0), g.title.lower()))
        if self.sort_mode == "newest":
            return sorted(games, key=lambda g: (-self.first_seen.get(g.uid, 0), g.title.lower()))
        return sorted(games, key=lambda g: g.title.lower())

    def _launcher_sort_key(self, game: GameItem) -> Tuple[int, int, str]:
        """A launcher's own custom order, launchers in the sidebar's order."""
        launcher_names = self._effective_launcher_names()
        try:
            launcher_rank = launcher_names.index(game.launcher)
        except ValueError:
            launcher_rank = len(launcher_names)
        order_list = self.game_order.get(game.launcher, [])
        try:
            pos = order_list.index(game.uid)
        except ValueError:
            pos = len(order_list) + 1_000_000
        return (launcher_rank, pos, game.title.lower())

    def _all_sort_key(self, game: GameItem) -> Tuple:
        """All Plunder's own custom order, whatever the launchers' - the
        games it hasn't placed yet after it, as their launchers have them
        (so until it's first edited, it's just that)."""
        try:
            return (0, self.all_game_order.index(game.uid))
        except ValueError:
            return (1, *self._launcher_sort_key(game))

    @staticmethod
    def _merge_order(full_order: List[str], visible_order: List[str]) -> List[str]:
        """visible_order (the games on screen - hidden ones are left out
        of Edit order) written back into the slots exactly those games
        hold in full_order, every other game's place kept."""
        own = set(full_order)
        visible_order = [uid for uid in visible_order if uid in own]
        visible = set(visible_order)
        slots = [i for i, uid in enumerate(full_order) if uid in visible]
        if len(slots) != len(visible_order):
            return list(visible_order) + [uid for uid in full_order if uid not in visible]
        merged = list(full_order)
        for slot, uid in zip(slots, visible_order):
            merged[slot] = uid
        return merged

    def _effective_launcher_names(self) -> List[str]:
        known = [s["name"] for s in self._cached_launchers]
        order = [n for n in self.launcher_order if n in known]
        for n in known:
            if n not in order:
                order.append(n)
        return order

    def _ordered_launcher_sections(self, launchers: List[Dict]) -> List[Dict]:
        order = self._effective_launcher_names()
        by_name = {s["name"]: s for s in launchers}
        return [by_name[n] for n in order if n in by_name]

    def _store_game_order(self, launcher: str, visible_order: List[str]):
        """Saves a reordering of the launcher's games on screen (see
        _merge_order). A card dropped onto another launcher's section
        brings that section's uids along - they don't belong in it."""
        section_games = next((s["games"] for s in self._cached_launchers if s["name"] == launcher), [])
        full_order = [g.uid for g in sorted(section_games, key=self._launcher_sort_key)]
        self.game_order[launcher] = self._merge_order(full_order, visible_order)
        self.settings["game_order"] = self.game_order
        save_settings(self.settings)

    def _store_all_order(self, visible_order: List[str]):
        """Saves a reordering of All Plunder (or Favorites) on screen."""
        games = [g for s in self._cached_launchers for g in s["games"]]
        full_order = [g.uid for g in sorted(games, key=self._all_sort_key)]
        self.all_game_order = self._merge_order(full_order, visible_order)
        self.settings["all_game_order"] = self.all_game_order
        save_settings(self.settings)
