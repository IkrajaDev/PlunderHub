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


import calendar
from datetime import datetime, timedelta
from typing import Dict, List

from plunderhub.gi_setup import Gtk


class PlaytimeMixin:
    # Month and weekday names come from the UI language, not strftime():
    # %a/%b/%B follow the system locale, so a Hungarian desktop showed
    # Hungarian dates whatever language PlunderHub itself was set to.
    def _date_names(self, key: str) -> List[str]:
        return self._(key).split(",")

    def _format_month_label(self, period_str: str) -> str:
        when = datetime.strptime(period_str, "%Y-%m")
        label = self._("date_format_month").format(
            month=self._date_names("date_months")[when.month - 1], year=when.year)
        return label[:1].upper() + label[1:]

    def _format_day_label(self, date_str: str) -> str:
        when = datetime.strptime(date_str, "%Y-%m-%d")
        label = self._("date_format_day").format(
            weekday=self._date_names("date_weekdays_short")[when.weekday()],
            month=self._date_names("date_months_short")[when.month - 1],
            day=when.day)
        return label[:1].upper() + label[1:]

    def _playtime_day_total(self, date_str: str) -> int:
        day = self.playtime_history.get(date_str)
        if not day:
            return 0
        return sum(day.values())

    def _current_week_dates(self) -> List[str]:
        today = datetime.now().date()
        monday = today - timedelta(days=today.weekday())
        return [(monday + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(7)]

    def _current_month_dates(self) -> List[str]:
        today = datetime.now().date()
        first = today.replace(day=1)
        days_in_month = calendar.monthrange(today.year, today.month)[1]
        return [(first + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(days_in_month)]

    def _current_year_months(self) -> List[str]:
        year = datetime.now().year
        return [f"{year}-{m:02d}" for m in range(1, 13)]

    def _playtime_month_total(self, year_month: str) -> int:
        total = 0
        for date_str, day in self.playtime_history.items():
            if date_str.startswith(year_month) and day:
                total += sum(day.values())
        return total

    def _dates_for_period(self, period: str) -> List[str]:
        # A "day" string (YYYY-MM-DD, 10 chars) picks itself out; a
        # "month" string (YYYY-MM, 7 chars, from the Year view) needs
        # expanding to every day actually recorded that month, so
        # _playtime_totals_by_game (which only understands day keys)
        # can be reused unchanged for both a Week/Month day selection
        # and a Year view's month selection.
        if len(period) == 7:
            return [d for d in self.playtime_history if d.startswith(period)]
        return [period]

    def _playtime_totals_by_game(self, dates: List[str]) -> Dict[str, int]:
        totals: Dict[str, int] = {}
        for d in dates:
            day = self.playtime_history.get(d)
            if not day:
                continue
            for uid, minutes in day.items():
                if uid == "__unknown__":
                    continue
                totals[uid] = totals.get(uid, 0) + minutes
        return totals

    def show_dashboard(self):
        from plunderhub.ui.dashboard_view import DashboardView
        dlg = Gtk.Dialog(title=self._("dashboard_title"), transient_for=self._dialog_transient_parent(), flags=0)
        dlg.set_modal(True)
        dlg.set_decorated(False)
        _w, h = self.get_size()
        # A size request, not just a default: window managers that remember
        # a dialog's size by its title would bring back the old, small one.
        # Bigger with a bigger font size (the drawing scales with it), as
        # far as the main window allows.
        f = max(1.0, self._text_factor())
        w = self.get_size()[0]
        dlg.set_size_request(min(int(1150 * f), max(800, w - 60)), max(640, min(int(940 * f), h - 60)))
        area = dlg.get_content_area()
        area.set_border_width(0)
        action_area = dlg.get_action_area()
        if action_area is not None:
            action_area.set_no_show_all(True)
            action_area.hide()
        view = DashboardView(self, close=lambda: dlg.response(Gtk.ResponseType.CLOSE))
        area.pack_start(view, True, True, 0)
        # Closed from the bottom-right, like the Settings window; the
        # strip under the drawing is painted in the dashboard's own colour.
        footer = Gtk.Box()
        footer.set_border_width(10)
        footer.set_margin_start(26)
        footer.set_margin_end(26)
        footer.set_margin_bottom(6)

        def paint(widget, cr):
            cr.set_source_rgb(*view._colors()["bg"])
            cr.paint()
            return False
        strip = Gtk.EventBox()
        # No window of its own: one would be stacked over the whole dialog
        # and hide the dashboard drawn under it (only the footer showed).
        strip.set_visible_window(False)
        strip.set_app_paintable(True)
        strip.connect("draw", paint)
        strip.add(footer)
        close_btn = Gtk.Button(label=self._("close_btn"))
        close_btn.set_focus_on_click(False)
        close_btn.connect("clicked", lambda *_: dlg.response(Gtk.ResponseType.CLOSE))
        footer.pack_end(close_btn, False, False, 0)
        area.pack_start(strip, False, False, 0)
        dlg.plunderhub_nav = view.controller      # the controller drives the dashboard directly
        dlg.show_all()
        view.grab_focus()
        self._enter_dimmed_dialog()
        try:
            dlg.run()
        finally:
            self._exit_dimmed_dialog()
            dlg.destroy()
