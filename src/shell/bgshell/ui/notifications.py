"""bgnote — сервер уведомлений ByteguruOS.

Зачем свой, если есть mako/wormhorn: их в trixie нет, а обещание «как в
обычном дистрибутиве» без org.freedesktop.Notifications не выполняется —
приложения стучатся именно в это имя на сессийной D-Bus шине. Реализация
намеренно маленькая: четыре метода, два сигнала, карточки поверх стола.

Что здесь важно:

1. ИСТОРИЯ. Каждое уведомление дописывается в
   ~/.local/share/byteguru/notifications.json, а панель читает этот файл.
   Так «не видел, потому что был в другом потоке» перестаёт быть проблемой:
   уведомление не исчезает вместе с окном.
2. РЕЖИМ «НЕ БЕСПОКОИТЬ». Флаг живёт в ~/.config/byteguru/notifications.conf.
   В этом режиме история пишется, всплывающих окон нет. Это тот случай, когда
   настройки оболочки и демон читают одно и то же значение, а не хранят по
   копии.
3. CRITICAL не исчезает сам. Так требует спецификация, и так правильно:
   «разрядился ноутбук» не должно успеть промелькнуть.
4. Закрытие по таймеру — один GLib-таймер на карточку, не поток.

PAM/политик здесь нет намеренно: уведомления — не канал передачи секретов, и
любой процесс в сеансе имеет право их показать. Это же причина, по которой мы
не выполняем action: он передаётся клиенту обратно по D-Bus, а не исполняется.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from bgshell.ui import bggui

DBUS_NAME = "org.freedesktop.Notifications"
DBUS_PATH = "/org/freedesktop/Notifications"
HISTORY_FILE = Path.home() / ".local/share/byteguru/notifications.json"
CONF_FILE = Path.home() / ".config/byteguru/notifications.conf"
HISTORY_LIMIT = 50
DEFAULT_TIMEOUT_MS = 10_000
MIN_TIMEOUT_MS = 3_000
MAX_VISIBLE = 5
NOTIFY_WIDTH = 380

INTROSPECTION = """
<node>
  <interface name="org.freedesktop.Notifications">
    <method name="Notify">
      <arg type="s" name="app_name" direction="in"/>
      <arg type="u" name="replaces_id" direction="in"/>
      <arg type="s" name="app_icon" direction="in"/>
      <arg type="s" name="summary" direction="in"/>
      <arg type="s" name="body" direction="in"/>
      <arg type="as" name="actions" direction="in"/>
      <arg type="a{sv}" name="hints" direction="in"/>
      <arg type="i" name="expire_timeout" direction="in"/>
      <arg type="u" name="id" direction="out"/>
    </method>
    <method name="CloseNotification">
      <arg type="u" name="id" direction="in"/>
    </method>
    <method name="GetCapabilities">
      <arg type="as" name="capabilities" direction="out"/>
    </method>
    <method name="GetServerInformation">
      <arg type="s" name="name" direction="out"/>
      <arg type="s" name="vendor" direction="out"/>
      <arg type="s" name="version" direction="out"/>
      <arg type="s" name="spec_version" direction="out"/>
    </method>
    <signal name="NotificationClosed">
      <arg type="u" name="id"/>
      <arg type="u" name="reason"/>
    </signal>
    <signal name="ActionInvoked">
      <arg type="u" name="id"/>
      <arg type="s" name="action_key"/>
    </signal>
  </interface>
</node>
"""

CAPABILITIES = ["actions", "body", "body-hyperlinks", "body-markup",
    "icon-static", "x-gnome-notifications-capability"]

# Причины закрытия по спецификации.
REASON_EXPIRED = 1
REASON_USER = 2
REASON_PROGRAM = 3
REASON_UNKNOWN = 4


@dataclass
class Notice:
    ident: int
    app_name: str
    summary: str
    body: str = ""
    urgency: str = "normal"
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    actions: list[str] = field(default_factory=list)
    shown_at: float = field(default_factory=time.time)

    def as_history_entry(self) -> dict:
        return {
            "id": self.ident,
            "app": self.app_name,
            "title": self.summary,
            "body": self.body,
            "urgency": self.urgency,
            "at": datetime.fromtimestamp(self.shown_at).isoformat(
                timespec="seconds"),
        }


def _layer_shell():
    return bggui.LAYER_SHELL


def read_quiet_mode() -> bool:
    """Флаг «не беспокоить» из общего конфига оболочки.

    Читается при каждом уведомлении, а не один раз при старте: панель меняет
    его без перезапуска демона, и «тихий режим, который не подействовал до
    следующей сессии» — это ровно тот баг, который невозможно поймать.
    """
    try:
        text = CONF_FILE.read_text(encoding="utf-8")
    except OSError:
        return False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "quiet":
            return value.strip().lower() in ("1", "true", "yes", "on")
    return False


def set_quiet_mode(quiet: bool) -> None:
    CONF_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"quiet = {'true' if quiet else 'false'}"]
    CONF_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def append_history(entry: dict) -> None:
    try:
        HISTORY_FILE.parent.mkdir(parents=True, exist_ok=True)
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            data = []
    except (OSError, ValueError):
        data = []
    data.append(entry)
    del data[:-HISTORY_LIMIT]
    try:
        HISTORY_FILE.write_text(json.dumps(data, ensure_ascii=False),
            encoding="utf-8")
    except OSError:
        # История не должна ронять демон: максимум, что пользователь потеряет,
        # — пропущенное уведомление в колоколе панели.
        pass


class Notificator:
    def __init__(self, application):
        self.application = application
        self.next_id = 1
        self.visible: list[Notice] = []
        self.window = None
        self.box = None
        self.connection = None
        self._subscription_id = 0

    # ------------------------------------------------------------ D-Bus
    def export(self) -> bool:
        Gio = bggui.Gio
        try:
            Gio.bus_own_name(Gio.BusType.SESSION, DBUS_NAME,
                Gio.BusNameOwnerFlags.NONE, self._on_bus, None, None)
        except Exception as error:  # noqa: BLE001 — без шины демоном быть нельзя
            print(f"bgnote: не удалось занять {DBUS_NAME}: {error}", flush=True)
            return False
        return True

    def _on_bus(self, connection, name) -> None:
        # PyGObject вызывает bus-acquired с (connection, name): идентификатор
        # владельца в привязке не передаётся, и полагаться на него нельзя.
        # Именно поэтому в D-Bus-колбэках ниже нет self._owner_id.
        self.connection = connection
        node = bggui.Gio.DBusNodeInfo.new_for_xml(INTROSPECTION)
        try:
            connection.register_object(DBUS_PATH, node.interfaces[0],
                self._handle_call)
        except Exception as error:  # noqa: BLE001 — без сервиса демон бесполезен
            print(f"bgnote: регистрация {DBUS_NAME} не удалась: {error}",
                flush=True)

    def _handle_call(self, connection, sender, object_path, interface_name,
            method_name, parameters, invocation) -> None:
        GLib = bggui.GLib
        Gio = bggui.Gio
        try:
            if method_name == "Notify":
                self._notify(invocation, sender, parameters)
            elif method_name == "CloseNotification":
                self._close(int(parameters.unpack()[0]))
                invocation.return_value(None)
            elif method_name == "GetCapabilities":
                invocation.return_value(GLib.Variant("(as)",
                    (list(CAPABILITIES),)))
            elif method_name == "GetServerInformation":
                invocation.return_value(GLib.Variant("(ssss)", (
                    "ByteguruOS Notifications", "ByteguruOS", "1.0", "1.2")))
            else:
                invocation.return_error_literal(Gio.DBusError,
                    Gio.IOErrorEnum.NOT_SUPPORTED,
                    f"метод {method_name} не поддерживается")
        except Exception as error:  # noqa: BLE001 — D-Bus не должен ронять демон
            invocation.return_error_literal(Gio.DBusError,
                Gio.IOErrorEnum.FAILED, f"bgnote: {error}")

    def _notify(self, invocation, sender, parameters) -> None:
        Gio = bggui.Gio
        GLib = bggui.GLib
        (app_name, replaces_id, _icon, summary, body, actions, hints,
            expire) = parameters.unpack()

        ident = int(replaces_id) if replaces_id else self.next_id
        if not replaces_id:
            self.next_id += 1
        urgency = "normal"
        for key, value in (hints or {}).items():
            if key == "urgency":
                urgency = {0: "low", 1: "normal", 2: "critical"}.get(
                    int(value), "normal")

        timeout = int(expire) if expire and expire > 0 else DEFAULT_TIMEOUT_MS
        notice = Notice(ident=ident, app_name=app_name or sender,
            summary=summary or "", body=body or "", urgency=urgency,
            timeout_ms=max(MIN_TIMEOUT_MS, timeout),
            actions=list(actions or []))
        if replaces_id:
            self._dismiss(ident, REASON_PROGRAM, redraw=False)

        append_history(notice.as_history_entry())
        self._show(notice)
        invocation.return_value(GLib.Variant("(u)", (ident,)))

    # ------------------------------------------------------------ интерфейс
    def _ensure_window(self) -> None:
        Gtk = bggui.Gtk
        if self.window is not None:
            return
        self.window = bggui.build_window(self.application, "bgnote")
        self.window.add_css_class("bg-notifications")
        layer = _layer_shell()
        if layer is not None and bggui.layer_shell_supported():
            layer.init_for_window(self.window)
            layer.set_layer(self.window, layer.Layer.OVERLAY)
            layer.set_anchor(self.window, layer.Edge.TOP, True)
            layer.set_anchor(self.window, layer.Edge.RIGHT, True)
            layer.set_namespace(self.window, "bgwl-notifications")
            # NONE: уведомления не имеют права забирать клавиатуру — иначе
            # приславшее их приложение потеряет фокус в момент показа.
            layer.set_keyboard_mode(self.window, layer.KeyboardMode.NONE)
            layer.set_margin(self.window, layer.Edge.TOP, 42)
            layer.set_margin(self.window, layer.Edge.RIGHT, 12)
        else:
            self.window.set_default_size(380, 10)

        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        # Размер задаём контейнеру: layer-surface берёт размер из окна, а окно
        # без собственного размера равно натуральному размеру содержимого.
        # 1px по высоте здесь опасен: окно с default_size(…, 1) оставалось
        # полосой в один пиксель, и карточку нельзя было увидеть.
        self.box.set_size_request(NOTIFY_WIDTH, -1)
        # GTK4 не даёт окну быть меньше 200x200 (минимум GtkWindow), поэтому
        # поверхность всегда 380x200: под единственной карточкой остаётся
        # прозрачная полоса. Оставить её кликабельной нельзя, но и убрать
        # нельзя без собственного слоя вместо ApplicationWindow — цена
        # (несколько секунд перехвата в углу экрана) ниже цены такой переделки.
        self.window.set_resizable(False)
        self.window.set_child(self.box)
        self.window.set_visible(False)

    def _show(self, notice: Notice) -> None:
        if read_quiet_mode():
            return
        self._ensure_window()
        self.window.set_visible(True)
        if len(self.visible) >= MAX_VISIBLE:
            # Старейшая карточка уходит, новые не должны вытеснять те, что
            # пользователь ещё не прочитал, в пользу бесконечного потока.
            oldest = self.visible[0]
            self._dismiss(oldest.ident, REASON_EXPIRED)

        card = self._build_card(notice)
        self.box.append(card)
        self.visible.append(notice)
        self.window.present()

        if notice.urgency != "critical":
            bggui.GLib.timeout_add(notice.timeout_ms,
                lambda n=notice: self._expire(n))

    def _build_card(self, notice: Notice) -> object:
        Gtk = bggui.Gtk
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        card.add_css_class("bg-card")
        card.add_css_class(f"bg-notify-{notice.urgency}")

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        title = Gtk.Label(label=notice.summary or notice.app_name)
        title.add_css_class("heading")
        title.set_xalign(0.0)
        title.set_hexpand(True)
        title.set_wrap(True)
        header.append(title)
        close = Gtk.Button(label="×")
        close.add_css_class("flat")
        close.connect("clicked",
            lambda _b, n=notice: self._dismiss(n.ident, REASON_USER))
        header.append(close)
        card.append(header)

        if notice.body:
            body = Gtk.Label(label=notice.body)
            body.set_xalign(0.0)
            body.set_wrap(True)
            body.add_css_class("bg-muted")
            card.append(body)

        # Пары (key, label) из спецификации: показываем только подписи.
        labels = [notice.actions[i + 1] for i in range(0, len(notice.actions) - 1, 2)
            if notice.actions[i + 1]]
        if labels:
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            for index, label in enumerate(labels[:2]):
                button = Gtk.Button(label=label)
                button.connect("clicked", lambda _b, n=notice, i=index:
                    self._invoke(n, i))
                row.append(button)
            card.append(row)
        return card

    def _invoke(self, notice: Notice, index: int) -> None:
        keys = [notice.actions[i] for i in range(0, len(notice.actions), 2)]
        if index < len(keys) and self.connection is not None:
            self.connection.emit_signal(None, DBUS_PATH, DBUS_NAME,
                "ActionInvoked", bggui.GLib.Variant("(us)",
                    (notice.ident, keys[index])))
        self._dismiss(notice.ident, REASON_USER)

    def _expire(self, notice: Notice) -> bool:
        self._dismiss(notice.ident, REASON_EXPIRED)
        return False

    def _emit_closed(self, ident: int, reason: int) -> None:
        if self.connection is None:
            return
        self.connection.emit_signal(None, DBUS_PATH, DBUS_NAME,
            "NotificationClosed", bggui.GLib.Variant("(uu)", (ident, reason)))

    def _dismiss(self, ident: int, reason: int, redraw: bool = True) -> None:
        remaining = [n for n in self.visible if n.ident != ident]
        if len(remaining) == len(self.visible):
            return
        self.visible = remaining
        self._emit_closed(ident, reason)
        if self.box is not None:
            # Карточки перестраиваются целиком: их максимум пять, а искать
            # виджет по указателю — способ поймать рассинхрон списка и экрана.
            while self.box.get_first_child() is not None:
                self.box.remove(self.box.get_first_child())
            for notice in self.visible:
                self.box.append(self._build_card(notice))
            if not self.visible and self.window is not None and redraw:
                self.window.set_visible(False)
        return True

    def _close(self, ident: int) -> None:
        self._dismiss(ident, REASON_PROGRAM)


def build_notificator(application) -> object:
    bggui.load_theme(None, "byteguru-dark")
    notificator = Notificator(application)
    notificator._ensure_window()   # окно живёт с демона, но скрыто до карточки
    if not notificator.export():
        return None
    application.hold()
    return notificator


def main(argv: list[str] | None = None) -> int:
    return bggui.run_gtk(build_notificator, argv)


if __name__ == "__main__":
    raise SystemExit(main())
