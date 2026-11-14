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

"""The Bounty Board page: the PC games free to keep right now
(core/bounty.py), a card each - its picture, the store, how long it stays
free, what it usually costs. A click opens its store page in the browser.

Its sidebar row sits under the Treasure Map (under the Harbors when those
show); Settings can take it away ("bounty_enabled"). Shown over the
library like the Treasure Map: one of them at a time."""

import math
import threading
import time
from typing import Dict, List, Optional

import cairo

from plunderhub.core import bounty
from plunderhub.core.treasure import open_uri
from plunderhub.gi_setup import Gdk, GdkPixbuf, GLib, Gtk, Pango
from plunderhub.icons import get_icon_pixbuf
from plunderhub.logging_setup import logger
from plunderhub.perf_guard import allow_hover_relayout

SETTING = "bounty_enabled"
CARD_W = 330
ART_H = 210
CHECK_EVERY_S = 30 * 60      # looks whether the cache got old while the app runs
STORE_COLOURS = {"epic": (0.16, 0.16, 0.18), "steam": (0.10, 0.27, 0.45), "gog": (0.45, 0.20, 0.62),
                 "humble": (0.80, 0.24, 0.20)}


def enabled(app) -> bool:
    return bool(app.settings.get(SETTING, True))


def _rounded(cr, x, y, w, h, r):
    r = max(0.0, min(r, w / 2, h / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


class OfferCard(Gtk.Button):
    """One offer: its picture on top (store and time left over it), the
    game and its price under it. The picture is scaled once per width."""

    def __init__(self, board: "BountyBoard", offer: bounty.Offer, have: bool):
        super().__init__()
        self.board, self.offer, self.have = board, offer, have
        app = board.app
        _ = app._
        self.get_style_context().add_class("bb-card")
        self.set_relief(Gtk.ReliefStyle.NONE)
        self.set_tooltip_text(_("bb_open_tip").format(store=bounty.STORE_NAMES.get(offer.store, offer.store)))
        self.pixbuf: Optional[GdkPixbuf.Pixbuf] = None
        self._surface = None
        self._surface_for = (0, 0)
        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.art = Gtk.DrawingArea()
        self.art.set_size_request(-1, ART_H)
        self.art.connect("draw", self._draw_art)
        column.pack_start(self.art, False, False, 0)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        body.get_style_context().add_class("bb-body")
        title = Gtk.Label(label=offer.title, xalign=0)
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.get_style_context().add_class("bb-title")
        body.pack_start(title, False, False, 0)
        if offer.genres:
            genres = Gtk.Label(label="  ·  ".join(offer.genres[:3]), xalign=0)
            genres.get_style_context().add_class("bb-genres")
            body.pack_start(genres, False, False, 0)
        if offer.description:
            text = Gtk.Label(label=offer.description, xalign=0)
            text.set_line_wrap(True)
            text.set_lines(2)
            text.set_ellipsize(Pango.EllipsizeMode.END)
            text.set_max_width_chars(30)
            text.get_style_context().add_class("bb-desc")
            body.pack_start(text, False, False, 0)
        foot = Gtk.Box(spacing=10)
        foot.set_margin_top(4)
        if offer.price:
            was = Gtk.Label()
            was.set_markup(f"<s>{GLib.markup_escape_text(offer.price)}</s>")
            was.get_style_context().add_class("bb-was")
            foot.pack_start(was, False, False, 0)
        free = Gtk.Label(label=_("bb_free"))
        free.get_style_context().add_class("bb-free")
        foot.pack_start(free, False, False, 0)
        claim = Gtk.Box(spacing=6)
        claim_text = Gtk.Label(label=_("bb_claim").format(store=bounty.STORE_NAMES.get(offer.store, "")), xalign=1)
        claim_text.set_ellipsize(Pango.EllipsizeMode.END)      # a narrow card: shortened, not wider
        claim.pack_start(claim_text, True, True, 0)
        pb = get_icon_pixbuf("external-link-symbolic", 14, app._home_accent_hex())
        if pb is not None:
            claim.pack_start(Gtk.Image.new_from_pixbuf(pb), False, False, 0)
        claim.get_style_context().add_class("bb-claim")
        foot.pack_end(claim, True, True, 0)
        body.pack_start(foot, False, False, 0)
        column.pack_start(body, False, False, 0)
        self.add(column)
        self.connect("clicked", lambda *_a: open_uri(offer.url))

    def set_art(self, path):
        try:
            self.pixbuf = GdkPixbuf.Pixbuf.new_from_file(str(path))
        except Exception as e:
            logger.debug(f"Bounty Board picture {path}: {e}")
            self.pixbuf = None
        self._surface = None
        self.art.queue_draw()

    def _art_surface(self, w: int, h: int, scale: int):
        """The picture cropped to the card, rounded on top - drawn once per size."""
        if self._surface is not None and self._surface_for == (w, h, scale):
            return self._surface
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, w * scale, h * scale)
        surface.set_device_scale(scale, scale)
        cr = cairo.Context(surface)
        _rounded(cr, 0, 0, w, h + 24, 18)
        cr.clip()
        colour = STORE_COLOURS.get(self.offer.store, (0.2, 0.2, 0.25))
        if self.pixbuf is not None:
            pw, ph = self.pixbuf.get_width(), self.pixbuf.get_height()
            zoom = max(w * scale / pw, h * scale / ph)
            scaled = self.pixbuf.scale_simple(max(1, int(pw * zoom)), max(1, int(ph * zoom)),
                                              GdkPixbuf.InterpType.BILINEAR)
            cr.save()
            cr.scale(1 / scale, 1 / scale)
            Gdk.cairo_set_source_pixbuf(cr, scaled, (w * scale - scaled.get_width()) / 2,
                                        (h * scale - scaled.get_height()) / 2)
            cr.paint()
            cr.restore()
        else:
            gradient = cairo.LinearGradient(0, 0, w, h)
            gradient.add_color_stop_rgb(0, *colour)
            gradient.add_color_stop_rgb(1, *(c * 0.45 for c in colour))
            cr.set_source(gradient)
            cr.paint()
        shade = cairo.LinearGradient(0, h * 0.45, 0, h)
        shade.add_color_stop_rgba(0, 0, 0, 0, 0)
        shade.add_color_stop_rgba(1, 0, 0, 0, 0.72)
        cr.set_source(shade)
        cr.paint()
        self._surface, self._surface_for = surface, (w, h, scale)
        return surface

    def _pill(self, cr, widget, x, y, text: str, bg, fg=(1, 1, 1), right: bool = False, bold: bool = True):
        layout = widget.create_pango_layout(text)
        desc = widget.get_style_context().get_font(widget.get_style_context().get_state()).copy()
        desc.set_size(int(desc.get_size() * 0.82))
        if bold:
            desc.set_weight(Pango.Weight.BOLD)
        layout.set_font_description(desc)
        tw, th = layout.get_pixel_size()
        pw, ph = tw + 20, th + 8
        if right:
            x -= pw
        _rounded(cr, x, y, pw, ph, ph / 2)
        cr.set_source_rgba(*bg)
        cr.fill()
        cr.set_source_rgb(*fg)
        cr.move_to(x + 10, y + 4)
        from gi.repository import PangoCairo
        PangoCairo.show_layout(cr, layout)
        return pw, ph

    def _draw_art(self, widget, cr) -> bool:
        w, h = widget.get_allocated_width(), widget.get_allocated_height()
        cr.set_source_surface(self._art_surface(w, h, widget.get_scale_factor()), 0, 0)
        cr.paint()
        _ = self.board.app._
        colour = STORE_COLOURS.get(self.offer.store, (0.2, 0.2, 0.25))
        self._pill(cr, widget, 14, 14, bounty.STORE_NAMES.get(self.offer.store, self.offer.store),
                   (*colour, 0.92))
        if self.have:
            self._pill(cr, widget, w - 14, 14, "✓ " + _("bb_aboard"), (0.37, 0.81, 0.54, 0.92),
                       (0.05, 0.12, 0.08), right=True)
        left = self.board.time_left(self.offer)
        if left:
            text, urgent = left
            self._pill(cr, widget, 14, h - 42, "⏳ " + text,
                       (0.94, 0.42, 0.42, 0.95) if urgent else (0, 0, 0, 0.62))
        return False


class BountyBoard(Gtk.Box):
    def __init__(self, app):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.app = app
        self._ = app._
        self.offers: List[bounty.Offer] = bounty.cached()
        self.store = "all"
        self.query = ""
        self.loading = False
        self.failed = False
        self.cards: Dict[str, OfferCard] = {}
        self.get_style_context().add_class("bb-page")

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_overlay_scrolling(False)
        scroller.get_style_context().add_class("settings-scroll")
        self.scroller = scroller
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        page.set_margin_start(20)
        page.set_margin_end(24)
        page.set_margin_top(4)
        page.set_margin_bottom(28)
        page.pack_start(self._head(), False, False, 0)
        self.flow = Gtk.FlowBox()
        self.flow.set_selection_mode(Gtk.SelectionMode.NONE)
        self.flow.set_homogeneous(True)
        self.flow.set_min_children_per_line(1)
        self.flow.set_max_children_per_line(4)
        self.flow.set_column_spacing(18)
        self.flow.set_row_spacing(18)
        self.flow.set_valign(Gtk.Align.START)
        self.flow.set_filter_func(self._shown)
        page.pack_start(self.flow, False, False, 0)
        self.empty = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.empty.get_style_context().add_class("bb-empty")
        self.empty.set_no_show_all(True)
        page.pack_start(self.empty, False, False, 0)
        scroller.add(page)
        self.pack_start(scroller, True, True, 0)
        self._fill()
        GLib.timeout_add_seconds(CHECK_EVERY_S, self._tick)

    # the top ----------------------------------------------------------------------------------

    def _head(self) -> Gtk.Widget:
        _ = self._
        head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        row = Gtk.Box(spacing=12)
        title = Gtk.Label(label=_("bb_title"), xalign=0)
        title.get_style_context().add_class("home-section")
        row.pack_start(title, False, False, 0)
        self.refresh_btn = Gtk.Button()
        self.refresh_btn.get_style_context().add_class("about-close")
        self.refresh_btn.add(self.app._icon_image_at_size("view-refresh-symbolic", 16))
        self.refresh_btn.set_tooltip_text(_("bb_refresh"))
        self.refresh_btn.set_valign(Gtk.Align.CENTER)
        self.refresh_btn.set_focus_on_click(False)
        self.refresh_btn.connect("clicked", lambda *_a: self.refresh(force=True))
        row.pack_end(self.refresh_btn, False, False, 0)
        self.when = Gtk.Label(xalign=1)
        self.when.get_style_context().add_class("tm-sub")
        self.when.set_valign(Gtk.Align.CENTER)
        row.pack_end(self.when, False, False, 0)
        head.pack_start(row, False, False, 0)
        sub = Gtk.Label(xalign=0)
        sub.set_line_wrap(True)
        sub.set_max_width_chars(90)
        sub.set_markup(_("bb_sub"))
        sub.get_style_context().add_class("tm-sub")
        sub.connect("activate-link", lambda _l, uri: open_uri(uri) or True)
        head.pack_start(sub, False, False, 0)
        # one row: every store, with how many are free there now (0 too - see _fill)
        chips = Gtk.Box(spacing=8)
        chips.set_margin_top(12)
        self.chips: Dict[str, Gtk.ToggleButton] = {}
        for key in ("all",) + bounty.STORES:
            chip = Gtk.ToggleButton()
            chip.get_style_context().add_class("sw-chip")
            chip.set_focus_on_click(False)
            allow_hover_relayout(chip, "its dimmed text lights up under the pointer")
            chip.add(Gtk.Label(label=_("sw_filter_all") if key == "all" else bounty.STORE_NAMES[key]))
            chip.connect("toggled", lambda b, k=key: b.get_active() and self._pick_store(k))
            chips.pack_start(chip, False, False, 0)
            chip.set_no_show_all(True)
            chip.get_child().show()
            chip.show()
            self.chips[key] = chip
        self.chips["all"].set_active(True)
        head.pack_start(chips, False, False, 0)
        return head

    def _pick_store(self, key: str):
        self.store = key
        for other, chip in self.chips.items():
            if other != key and chip.get_active():
                chip.set_active(False)
        if not hasattr(self, "flow"):
            return      # still being built
        self.flow.invalidate_filter()
        self._update_empty()

    def set_query(self, text: str):
        self.query = (text or "").strip().lower()
        self.flow.invalidate_filter()
        self._update_empty()

    def _shown(self, child) -> bool:
        card = child.get_child()
        offer = getattr(card, "offer", None)
        if offer is None:
            return True
        return (self.store == "all" or offer.store == self.store) and \
            (not self.query or self.query in offer.title.lower())

    # the offers -------------------------------------------------------------------------------

    def time_left(self, offer: bounty.Offer):
        if offer.ends is None:
            return None
        left = offer.ends - time.time()
        if left <= 0:
            return self._("bb_ended"), True
        until = time.strftime("%m-%d %H:%M", time.localtime(offer.ends))
        if left >= 2 * 86400:
            return self._("bb_days_left").format(n=int(left // 86400), until=until), False
        if left >= 3600:
            return self._("bb_hours_left").format(n=int(left // 3600), until=until), left < 86400
        return self._("bb_minutes_left").format(n=max(1, int(left // 60))), True

    def _owned_titles(self) -> List[str]:
        titles = [g.title for g in self.app.game_index.values()]
        view = getattr(self.app, "_tm_view", None)
        for state in (getattr(view, "states", None) or {}).values():
            titles += [getattr(g, "title", "") for g in getattr(state, "games", [])]
        return titles

    def _fill(self):
        for child in self.flow.get_children():
            child.destroy()
        self.cards = {}
        self.offers = [o for o in self.offers if o.running()]
        have = bounty.owned(self.offers, self._owned_titles())
        for offer in self.offers:
            card = OfferCard(self, offer, have.get(offer.id, False))
            card.set_size_request(CARD_W, -1)
            self.flow.add(card)
            self.cards[offer.id] = card
        self.flow.show_all()
        for key, chip in self.chips.items():
            n = len(self.offers) if key == "all" else sum(1 for o in self.offers if o.store == key)
            label = chip.get_child()
            label.set_text((self._("sw_filter_all") if key == "all" else bounty.STORE_NAMES[key]) + f"  {n}")
        self._update_when()
        self._update_empty()
        self._load_art()
        refresh = getattr(self.app, "_refresh_home_counts", None)
        if refresh is not None:
            refresh()

    def _update_when(self):
        at = bounty.fetched_at()
        if self.loading:
            self.when.set_text(self._("bb_checking"))
        elif at:
            mins = int((time.time() - at) // 60)
            self.when.set_text(self._("bb_checked").format(
                when=self._("bb_just_now") if mins < 1 else
                self._("wc_minutes").format(n=mins) if mins < 60 else f"{mins // 60} h"))
        else:
            self.when.set_text("")

    def _update_empty(self):
        for child in self.empty.get_children():
            child.destroy()
        visible = [o for o in self.offers if (self.store == "all" or o.store == self.store)
                   and (not self.query or self.query in o.title.lower())]
        if visible:
            self.empty.hide()
            return
        _ = self._
        pb = get_icon_pixbuf("anchor-symbolic", 46, self.app._home_accent_hex())
        if pb is not None:
            self.empty.pack_start(Gtk.Image.new_from_pixbuf(pb), False, False, 0)
        # a store with nothing free (picked on its 0 chip): a word from the crew
        store_empty = (self.store != "all" and not self.query and self.offers
                       and not any(o.store == self.store for o in self.offers))
        store = bounty.STORE_NAMES.get(self.store, "")
        head = Gtk.Label(label=_("bb_offline") if self.failed and not self.offers else
                         _("bb_store_empty").format(store=store) if store_empty else
                         _("bb_none_here") if self.offers else _("bb_none"))
        head.get_style_context().add_class("pf-record-title")
        self.empty.pack_start(head, False, False, 0)
        text = Gtk.Label(label=_("bb_store_empty_sub").format(store=store) if store_empty else _("bb_none_sub"))
        text.set_line_wrap(True)
        text.set_justify(Gtk.Justification.CENTER)
        text.set_max_width_chars(60)
        text.get_style_context().add_class("tm-sub")
        self.empty.pack_start(text, False, False, 0)
        self.empty.set_no_show_all(False)
        self.empty.show_all()

    def _load_art(self):
        offers = list(self.offers)

        def work():
            for offer in offers:
                path = bounty.art_path(offer)
                if path is not None:
                    GLib.idle_add(self._art_ready, offer.id, path)
            bounty.tidy_art(offers)
        threading.Thread(target=work, name="plunderhub-bounty-art", daemon=True).start()

    def _art_ready(self, offer_id: str, path) -> bool:
        card = self.cards.get(offer_id)
        if card is not None:
            card.set_art(path)
        return False

    def refresh(self, force: bool = False):
        if self.loading:
            return
        self.loading = True
        self._update_when()
        self.refresh_btn.set_sensitive(False)

        def work():
            try:
                found = bounty.refresh(force=force)
            except Exception as e:
                logger.warning(f"Bounty Board not refreshed: {e}")
                found = None
            GLib.idle_add(done, found)

        def done(found) -> bool:
            self.loading = False
            self.refresh_btn.set_sensitive(True)
            self.failed = found is None
            if found is not None:
                self.offers = found
            self._fill()
            return False
        threading.Thread(target=work, name="plunderhub-bounty", daemon=True).start()

    def _tick(self) -> bool:
        if not enabled(self.app):
            return True
        if time.time() - bounty.fetched_at() >= bounty.REFRESH_S:
            self.refresh()
        else:
            # an offer that ran out goes; the time left counts down
            if any(not o.running() for o in self.offers):
                self._fill()
            else:
                for card in self.cards.values():
                    card.art.queue_draw()
                self._update_when()
        return True

    def count(self) -> int:
        return sum(1 for o in self.offers if o.running())


# --- in the main window ---------------------------------------------------------------------------

def attach(app) -> Optional[BountyBoard]:
    """The page over the library (hidden until picked) and its sidebar row
    right under the Harbors' row (under the Treasure Map's when there are
    no Harbors)."""
    view = BountyBoard(app)
    view.set_no_show_all(True)
    app.main_overlay.add_overlay(view)
    app.main_overlay.reorder_overlay(view, 1)
    app._bb_view, app._bb_active = view, False
    icon = app._icon_image_at_size("wanted-poster-symbolic", 18)
    row = app._home_nav_row("bounty", icon, "bb_title", with_count=True)
    children = app.home_nav_main.get_children()
    after = app.home_nav_harbors if app.home_nav_harbors in children else getattr(app, "_tm_row", None)
    app.home_nav_main.insert(row, children.index(after) + 1 if after in children else -1)
    row.show_all()
    row.set_no_show_all(True)
    app._bb_row = row
    row.set_visible(enabled(app))
    import sys
    if enabled(app) and "--self-test" not in sys.argv:       # the self-test reads nothing from the net
        GLib.timeout_add_seconds(4, lambda: view.refresh() and False)
    return view


def shown(app) -> Optional[BountyBoard]:
    return getattr(app, "_bb_view", None) if getattr(app, "_bb_active", False) else None


def show_board(app, on: bool):
    view = getattr(app, "_bb_view", None)
    if view is None or on == app._bb_active:
        return
    app._bb_active = on
    toolbar = app.view_grid_btn.get_parent()
    kids = toolbar.get_children()
    divider = kids[kids.index(app.view_list_btn) + 1]
    for w in (app.view_grid_btn, app.view_list_btn, divider, app.select_btn, app.reorder_btn, app.sort_btn):
        w.set_visible(not on)
    app.scroller.set_visible(not on)
    if on:
        app._set_scroll_top_visible(False)
        view.set_query(app.search_entry.get_text())
        view.set_no_show_all(False)
        view.show_all()
        import sys
        if time.time() - bounty.fetched_at() >= bounty.REFRESH_S and "--self-test" not in sys.argv:
            view.refresh()
    else:
        view.set_query("")
        if app.search_entry.get_text():
            app.search_entry.set_text("")
        view.hide()
        app._on_main_scroll_value_changed(app.scroller.get_vadjustment())
        app._on_search_changed()
    app._update_decorative_animation_pause_state()
    app._home_select_nav_row()


def set_enabled(app, on: bool):
    """Settings' switch: the row (and the page, if it's open) go or come back."""
    row = getattr(app, "_bb_row", None)
    if row is None:
        return
    if not on and shown(app) is not None:
        app._set_home_nav(app.home_nav)
    row.set_visible(on)
    if on:
        view = getattr(app, "_bb_view", None)
        if view is not None and not view.offers:
            view.refresh()
