#!/usr/bin/env python3
"""ulta_gui — графический интерфейс для мыши Ardor Gaming Ulta (GTK4/libadwaita).

Требуется пакет python3-gi с Gtk 4.0 и Adw 1. Запуск:

    python3 ulta_gui.py
"""
import os
import queue
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, GLib, Gtk

import ultactl as u

POLL_ORDER = [1000, 500, 250, 125]
SPEED_MODES = ("breath", "stream", "neon", "scroll", "colorbreath")


# --------------------------------------------------------------- опрос ----

def collect_status(m):
    """Прочитать состояние мыши в простой словарь (в рабочем потоке)."""
    bat = m.battery()
    m.image = {}
    m.read_profile()

    mode = u.EFFECT_NAMES.get(m.mem(u.A_LED))
    rgb = (m.mem(u.A_LED + 1) or 0, m.mem(u.A_LED + 2) or 0,
           m.mem(u.A_LED + 3) or 0)
    speeds, bris = u.tables_for(mode) if mode else (u.SPEED_STD, u.BRI_STD)
    speed = m.mem(u.A_LED + 4)
    bright = m.mem(u.A_LED + 5)
    speed_lv = speeds.index(speed) + 1 if speed in speeds else 6
    if bright == 0xFF:
        bright_pct = 100
    elif bright in bris:
        bright_pct = (bris.index(bright) + 1) * 10
    else:
        bright_pct = 100

    stages = []
    for i in range(8):
        code = m.mem(u.A_DPI_TABLE + 4 * i)
        mul = m.mem(u.A_DPI_TABLE + 4 * i + 2)
        if code is None or mul is None:
            stages.append(None)
        else:
            stages.append(u.dpi_decode(code, mul & 0x03))

    return {
        "link": m.link,
        "path": m.path,
        "battery": bat[0],
        "charging": bat[1],
        "mode": mode,
        "rgb": rgb,
        "speed": speed_lv,
        "brightness": bright_pct,
        "poll": u.HZ_CODE.get(m.mem(u.A_POLLING)),
        "lod": u.LOD_MM.get(m.mem(u.A_LOD), "—"),
        "motion": not (m.mem(u.A_LED_OFF_MOVING) or 0),
        "dpi": stages,
        "active": m.mem(u.A_DPI_ACTIVE),
    }


class Worker:
    """Один фоновый поток: все обращения к hidraw идут строго по очереди."""

    def __init__(self, on_busy):
        self._q = queue.Queue()
        self._mouse = None
        self._pending = 0
        self._on_busy = on_busy
        self._silent = False
        threading.Thread(target=self._loop, daemon=True).start()

    def silence(self):
        self._silent = True

    def submit(self, fn, on_done=None, on_error=None):
        self._pending += 1
        self._on_busy(True)
        self._q.put((fn, on_done, on_error))

    def _open(self):
        if self._mouse is None:
            m = u.Mouse()
            m.open()
            self._mouse = m
        return self._mouse

    def _drop(self):
        if self._mouse is not None:
            self._mouse.close()
            self._mouse = None

    def _loop(self):
        while True:
            item = self._q.get()
            if item is None:
                self._drop()
                return
            fn, on_done, on_error = item
            try:
                result = fn(self._open())
            except BaseException as e:      # noqa: BLE001 — показать любую ошибку
                self._drop()
                GLib.idle_add(self._finish, on_error, e)
            else:
                GLib.idle_add(self._finish, on_done, result)

    def _finish(self, cb, value):
        self._pending -= 1
        if self._silent:
            return
        self._on_busy(self._pending > 0)
        if cb is not None:
            cb(value)


# --------------------------------------------------------------- окно ----

class UltaWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._loading = False
        self._st = None
        self._dpi_spins = []
        self._dpi_checks = []
        self._dpi_group = None

        self.worker = Worker(self._on_busy)

        self.set_title("Ardor Gaming Ulta")
        self.set_default_size(640, 960)

        self._build_ui()
        self.connect("close-request", self._on_close)
        self.refresh()

    # ------------------------------------------------------------ разметка --

    def _build_ui(self):
        header = Adw.HeaderBar()
        self.window_title = Adw.WindowTitle(
            title="Ardor Gaming Ulta", subtitle="подключение…")
        header.set_title_widget(self.window_title)

        self.refresh_btn = Gtk.Button(
            icon_name="view-refresh-symbolic", tooltip_text="Обновить")
        self.refresh_btn.connect("clicked", lambda *_: self.refresh())
        header.pack_end(self.refresh_btn)

        self.spinner = Gtk.Spinner()
        self.spinner.set_visible(False)
        self.spinner.set_tooltip_text("Связь с мышью…")
        header.pack_end(self.spinner)

        page = Adw.PreferencesPage()
        page.add(self._build_status_group())
        page.add(self._build_light_group())
        page.add(self._build_dpi_group())
        page.add(self._build_device_group())

        self.toasts = Adw.ToastOverlay()
        self.toasts.set_child(page)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self.toasts)
        self.set_content(toolbar)

    def _build_status_group(self):
        group = Adw.PreferencesGroup(title="Состояние")

        self.row_link = Adw.ActionRow(title="Подключение")
        self.row_link.set_subtitle("поиск устройства…")
        group.add(self.row_link)

        self.row_battery = Adw.ActionRow(title="Заряд")
        self.battery_bar = Gtk.LevelBar.new_for_interval(0, 100)
        self.battery_bar.set_size_request(150, -1)
        self.battery_bar.set_valign(Gtk.Align.CENTER)
        self.battery_label = Gtk.Label(label="—")
        self.battery_label.add_css_class("dim-label")
        self.battery_label.set_valign(Gtk.Align.CENTER)
        self.row_battery.add_suffix(self.battery_bar)
        self.row_battery.add_suffix(self.battery_label)
        group.add(self.row_battery)

        self.row_lod = Adw.ActionRow(title="Высота отрыва (LOD)")
        self.lod_label = Gtk.Label(label="—")
        self.lod_label.add_css_class("dim-label")
        self.row_lod.add_suffix(self.lod_label)
        group.add(self.row_lod)

        return group

    def _build_light_group(self):
        group = Adw.PreferencesGroup(title="Подсветка")

        self.mode_row = Adw.ComboRow(title="Режим")
        self.mode_row.set_model(
            Gtk.StringList.new([u.MODE_LABELS[n] for n in u.MODES_ORDER]))
        self.mode_row.connect("notify::selected", self._on_mode_changed)
        group.add(self.mode_row)

        self.color_row = Adw.ActionRow(title="Цвет")
        dialog = Gtk.ColorDialog()
        dialog.set_with_alpha(False)
        self.color_btn = Gtk.ColorDialogButton(dialog=dialog)
        self.color_btn.set_valign(Gtk.Align.CENTER)
        rgba = Gdk.RGBA()
        rgba.red, rgba.green, rgba.blue, rgba.alpha = 1.0, 1.0, 1.0, 1.0
        self.color_btn.set_rgba(rgba)
        self.color_row.add_suffix(self.color_btn)
        group.add(self.color_row)

        self.bright_row = Adw.ActionRow(title="Яркость")
        self.bright_scale = self._make_scale(0, 100, 1)
        self.bright_scale.set_value(100)
        self.bright_row.add_suffix(self.bright_scale)
        group.add(self.bright_row)

        self.speed_row = Adw.ActionRow(title="Скорость эффекта")
        self.speed_scale = self._make_scale(1, 10, 1)
        self.speed_scale.set_value(6)
        self.speed_row.add_suffix(self.speed_scale)
        group.add(self.speed_row)

        self.light_btn = Gtk.Button(label="Применить")
        self.light_btn.add_css_class("suggested-action")
        self.light_btn.set_valign(Gtk.Align.CENTER)
        self.light_btn.connect("clicked", self._apply_lighting)
        row = Adw.ActionRow(title="Применить подсветку")
        row.set_activatable(False)
        row.add_suffix(self.light_btn)
        group.add(row)

        return group

    def _make_scale(self, lo, hi, step):
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, lo, hi, step)
        scale.set_size_request(170, -1)
        scale.set_draw_value(True)
        scale.set_value_pos(Gtk.PositionType.RIGHT)
        scale.set_valign(Gtk.Align.CENTER)
        return scale

    def _build_dpi_group(self):
        group = Adw.PreferencesGroup(
            title="DPI",
            description="Ступени 50–19000. Галочка — активная ступень.")

        for i in range(8):
            row = Adw.ActionRow(title=f"Ступень {i + 1}")
            spin = Gtk.SpinButton.new_with_range(50, 19000, 50)
            spin.set_numeric(True)
            spin.set_width_chars(6)
            spin.set_valign(Gtk.Align.CENTER)
            check = Gtk.CheckButton(tooltip_text="Сделать активной")
            check.set_valign(Gtk.Align.CENTER)
            if self._dpi_group is None:
                self._dpi_group = check
            else:
                check.set_group(self._dpi_group)
            row.add_suffix(spin)
            row.add_suffix(check)
            self._dpi_spins.append(spin)
            self._dpi_checks.append(check)
            group.add(row)

        self.dpi_btn = Gtk.Button(label="Сохранить")
        self.dpi_btn.add_css_class("suggested-action")
        self.dpi_btn.set_valign(Gtk.Align.CENTER)
        self.dpi_btn.connect("clicked", self._apply_dpi)
        row = Adw.ActionRow(title="Сохранить ступени DPI")
        row.set_activatable(False)
        row.add_suffix(self.dpi_btn)
        group.add(row)

        return group

    def _build_device_group(self):
        group = Adw.PreferencesGroup(title="Устройство")

        self.poll_row = Adw.ComboRow(title="Частота опроса USB")
        self.poll_row.set_model(
            Gtk.StringList.new([f"{hz} Гц" for hz in POLL_ORDER]))
        self.poll_row.connect("notify::selected", self._on_poll)
        group.add(self.poll_row)

        self.motion_row = Adw.SwitchRow(
            title="Подсветка при движении",
            subtitle="Выключено — подсветка гаснет, когда мышь двигается")
        self.motion_row.connect("notify::active", self._on_motion)
        group.add(self.motion_row)

        return group

    # ------------------------------------------------------------- статус --

    def _selected_mode(self):
        idx = self.mode_row.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(u.MODES_ORDER):
            return "steady"
        return u.MODES_ORDER[idx]

    def _update_sensitivity(self):
        mode = self._selected_mode()
        self.color_btn.set_sensitive(mode in u.COLOR_MODES)
        self.speed_scale.set_sensitive(mode in SPEED_MODES)
        self.bright_scale.set_sensitive(mode != "off")

    def _on_mode_changed(self, *_):
        self._update_sensitivity()

    def _apply_status(self, st):
        self._loading = True
        try:
            self._st = st
            self.window_title.set_subtitle(f"{st['link']} · {st['path']}")
            self.row_link.set_subtitle(f"{st['link']} · {st['path']}")

            self.battery_bar.set_value(st["battery"])
            mark = " ⟳" if st["charging"] else ""
            self.row_battery.set_subtitle(
                "зарядка идёт" if st["charging"] else "от батареи")
            self.battery_label.set_label(f"{st['battery']}%{mark}")
            self.lod_label.set_label(st["lod"])

            mode = st["mode"]
            if mode in u.MODES_ORDER:
                self.mode_row.set_selected(u.MODES_ORDER.index(mode))
            else:
                self.mode_row.set_selected(Gtk.INVALID_LIST_POSITION)
            rgba = Gdk.RGBA()
            rgba.red, rgba.green, rgba.blue = (c / 255.0 for c in st["rgb"])
            rgba.alpha = 1.0
            self.color_btn.set_rgba(rgba)
            self.bright_scale.set_value(st["brightness"])
            self.speed_scale.set_value(st["speed"])

            if st["poll"] in POLL_ORDER:
                self.poll_row.set_selected(POLL_ORDER.index(st["poll"]))
            self.motion_row.set_active(bool(st["motion"]))

            for i, spin in enumerate(self._dpi_spins):
                value = st["dpi"][i]
                spin.set_sensitive(value is not None)
                if value is not None:
                    spin.set_value(value)
            active = st["active"]
            for i, check in enumerate(self._dpi_checks):
                check.set_active(active == i)
        finally:
            self._loading = False
        self._update_sensitivity()

    def _toast(self, text):
        self.toasts.add_toast(Adw.Toast.new(text))

    def _on_error(self, err):
        self.window_title.set_subtitle("нет связи")
        self.row_link.set_subtitle("нет связи — проверьте донгл или кабель")
        self._toast(f"Ошибка: {err}")
        self._revert_controls()

    def _revert_controls(self):
        st = self._st
        if not st:
            return
        self._loading = True
        try:
            if st["poll"] in POLL_ORDER:
                self.poll_row.set_selected(POLL_ORDER.index(st["poll"]))
            self.motion_row.set_active(bool(st["motion"]))
        finally:
            self._loading = False

    def _on_busy(self, busy):
        self.spinner.set_visible(busy)
        if busy:
            self.spinner.start()
        else:
            self.spinner.stop()
        self.refresh_btn.set_sensitive(not busy)

    def refresh(self):
        def task(m):
            return collect_status(m)

        def done(st):
            self._apply_status(st)

        self.worker.submit(task, done, self._on_error)

    # ----------------------------------------------------------- действия --

    def _apply_lighting(self, _btn):
        mode = self._selected_mode()
        rgba = self.color_btn.get_rgba()
        rgb = (round(rgba.red * 255), round(rgba.green * 255),
               round(rgba.blue * 255))
        bright = int(round(self.bright_scale.get_value()))
        level = int(round(self.speed_scale.get_value()))

        def task(m):
            if mode == "off":
                u.led_set(m, "off")
            else:
                u.led_set(m, mode, rgb, brightness_pct=bright, level=level)

        def done(_):
            self._toast(f"Подсветка: {u.MODE_LABELS.get(mode, mode)}")
            self.refresh()

        self.worker.submit(task, done, self._on_error)

    def _active_stage(self):
        for i, check in enumerate(self._dpi_checks):
            if check.get_active():
                return i + 1
        return 1

    def _apply_dpi(self, _btn):
        values = [u.dpi_snap(int(round(s.get_value()))) for s in self._dpi_spins]
        enabled = [s.get_sensitive() for s in self._dpi_spins]
        active = self._active_stage()
        st = self._st or {}
        old_stages = st.get("dpi") or [None] * 8
        old_active = st.get("active")

        def task(m):
            changed = 0
            for i, value in enumerate(values):
                if not enabled[i]:
                    continue
                if old_stages[i] != value:
                    u.dpi_set_stage(m, i + 1, value)
                    changed += 1
            if old_active != active - 1:
                u.dpi_set_active(m, active)
                changed += 1
            return changed

        def done(changed):
            if changed:
                self._toast("Настройки DPI сохранены")
            else:
                self._toast("DPI не изменились")
            self.refresh()

        self.worker.submit(task, done, self._on_error)

    def _on_poll(self, row, _pspec):
        if self._loading:
            return
        idx = row.get_selected()
        if idx == Gtk.INVALID_LIST_POSITION or idx >= len(POLL_ORDER):
            return
        hz = POLL_ORDER[idx]

        def task(m):
            u.polling_set(m, hz)

        def done(_):
            self._toast(f"Частота опроса: {hz} Гц")
            self.refresh()

        self.worker.submit(task, done, self._on_error)

    def _on_motion(self, row, _pspec):
        if self._loading:
            return
        state = bool(row.get_active())

        def task(m):
            u.motion_led_set(m, state)

        def done(_):
            self._toast("Подсветка при движении: "
                        + ("горит" if state else "гаснет"))
            self.refresh()

        self.worker.submit(task, done, self._on_error)

    # ------------------------------------------------------------- закрытие --

    def _on_close(self, *_):
        self.worker.silence()
        self.worker._q.put(None)
        return False


class UltaApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id="io.github.ultactl.UltaGui")

    def do_activate(self):
        win = self.get_active_window()
        if win is None:
            win = UltaWindow(application=self)
        win.present()


def main(argv=None):
    app = UltaApp()
    return app.run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    sys.exit(main())
