"""Состояние машины для панели: сеть, батарея, громкость, яркость.

Всё читается из сервисов, которые и так запущены в сеансе (NetworkManager,
UPower, PipeWire через pactl, подсветка через brightnessctl). Правило одно:
любой источник может отсутствовать. Live-образ без батареи, контейнер без
NetworkManager, машина без PipeWire — это не ошибка, а пустой индикатор,
поэтому каждый сборщик возвращает None вместо исключения.

Методы синхронные и короткие (локальный D-Bus, один fork на вызов) и запускаются
из цикла GTK по таймеру, а не по событию: панель обязана оставаться живой, даже
если какой-то демон завис.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field

# Свой таймаут на вызов: один зависший процесс не должен подвешивать панель.
_TIMEOUT = 2.0


def _run(args: list[str], timeout: float = _TIMEOUT) -> str | None:
    """Один внешний процесс, без исключений наружу."""
    if shutil.which(args[0]) is None:
        return None
    try:
        result = subprocess.run(args, capture_output=True, text=True,
            timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _dbus_get(service: str, path: str, interface: str, prop: str,
        system: bool = True) -> str | None:
    """Одно свойство D-Bus через gdbus.

    gdbus есть везде, где есть GSettings, и он не тянет в оболочку асинхронную
    D-Bus-обвязку: нам нужно одно значение раз в несколько секунд. Ответ —
    строка вида `(<variant string 'ethernet'>)`, поэтому вызывающие ищут в ней
    подстроки, а не разбирают GVariant.
    """
    if shutil.which("gdbus") is None:
        return None
    return _run([
        "gdbus", "call", "--system" if system else "--session",
        "--dest", service, "--object-path", path,
        "--method", "org.freedesktop.DBus.Properties.Get",
        interface, prop,
    ])


@dataclass(frozen=True)
class Network:
    connected: bool
    kind: str = ""         # ethernet | wifi | wwan | vpn | ""
    description: str = ""  # SSID или имя подключения
    strong: bool = True

    @property
    def icon(self) -> str:
        if not self.connected:
            return "network-wired-disconnected-symbolic"
        if self.kind == "wifi":
            return "network-wireless-signal-" + (
                "good-symbolic" if self.strong else "weak-symbolic")
        if self.kind == "wwan":
            return "network-wireless-signal-good-symbolic"
        if self.kind == "vpn":
            return "network-vpn-symbolic"
        return "network-wired-symbolic"

    @property
    def label(self) -> str:
        if not self.connected:
            return "сеть отключена"
        return self.description or (self.kind or "сеть")


@dataclass(frozen=True)
class Battery:
    percent: int
    charging: bool
    present: bool
    time_minutes: int = 0

    @property
    def icon(self) -> str:
        if not self.present:
            return "battery-missing-symbolic"
        level = ("full" if self.percent >= 95 else
                 "good" if self.percent >= 60 else
                 "low" if self.percent >= 20 else "caution")
        return f"battery-{level}" + ("-charging-symbolic" if self.charging
                                     else "-symbolic")

    @property
    def label(self) -> str:
        if not self.present:
            return "сеть"
        remainder = ""
        if self.time_minutes > 0:
            hours, minutes = divmod(int(self.time_minutes), 60)
            remainder = f" · {hours}ч {minutes:02d}м" if hours else f" · {minutes}м"
        mark = "⟳" if self.charging else ""
        return f"{self.percent}%{mark}{remainder}"


@dataclass(frozen=True)
class Audio:
    volume: int
    muted: bool

    @property
    def icon(self) -> str:
        if self.muted or self.volume == 0:
            return "audio-volume-muted-symbolic"
        if self.volume < 40:
            return "audio-volume-low-symbolic"
        if self.volume < 80:
            return "audio-volume-medium-symbolic"
        return "audio-volume-high-symbolic"


@dataclass(frozen=True)
class SystemInfo:
    network: Network | None = None
    battery: Battery | None = None
    audio: Audio | None = None
    brightness: int | None = None
    # Панель показывает иконки только тех индикаторов, что реально есть;
    # эта пара флагов позволяет не рисовать пустые квадраты.
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def needs_attention(self) -> bool:
        if self.battery is not None and self.battery.present:
            if not self.battery.charging and self.battery.percent <= 20:
                return True
        return False


def _split_machine_field(line: str) -> list[str]:
    """Разбор machine-readable вывода nmcli.

    nmcli экранирует разделитель: ':' внутри имени сети приходит как '\\:', а
    сам обратный слеш — как '\\\\'. Обычный split(':') превращает «My SSID:5GHz»
    в три поля и показывает в панели обрезанное имя, поэтому разбор идёт
    посимвольно.
    """
    parts: list[str] = []
    current: list[str] = []
    index = 0
    while index < len(line):
        char = line[index]
        if char == "\\" and index + 1 < len(line):
            current.append(line[index + 1])
            index += 2
            continue
        if char == ":":
            parts.append("".join(current))
            current = []
            index += 1
            continue
        current.append(char)
        index += 1
    parts.append("".join(current))
    return parts


def read_network() -> Network | None:
    """Состояние сети.

    nmcli даёт за одно чтение и тип, и имя подключения (SSID для Wi-Fi);
    gdbus по свойствам корня NetworkManager не отвечает на вопрос «какая именно
    сеть», поэтому порядок такой: nmcli, а если его нет — только факт
    подключения из D-Bus.
    """
    output = _run(["nmcli", "-t", "-f", "TYPE,NAME,STATE", "connection",
        "show", "--active"])
    if output is not None:
        lines = [line for line in output.splitlines() if line.strip()]
        if not lines:
            return Network(connected=False)
        first = _split_machine_field(lines[0])
        kind = first[0].strip().lower() if first else ""
        name = first[1].strip() if len(first) > 1 else ""
        mapped = {"802-11-wireless": "wifi", "802-11-wireless-security": "wifi",
            "wifi": "wifi", "gsm": "wwan", "cdma": "wwan", "lte": "wwan",
            "vpn": "vpn", "wire": "ethernet", "ethernet": "ethernet"}.get(kind, kind)
        return Network(connected=True, kind=mapped, description=name)

    state = _dbus_get("org.freedesktop.NetworkManager",
        "/org/freedesktop/NetworkManager", "org.freedesktop.NetworkManager",
        "State")
    if state is None:
        return None
    if "disconnect" in state.lower() or "asleep" in state.lower():
        return Network(connected=False)
    return Network(connected=True)


def read_battery() -> Battery | None:
    percentage = _dbus_get("org.freedesktop.UPower",
        "/org/freedesktop/UPower/devices/DisplayDevice",
        "org.freedesktop.UPower.Device", "Percentage")
    if percentage is None:
        return None
    try:
        percent = int(round(float(percentage.split()[-1])))
    except (ValueError, IndexError):
        return None

    state = _dbus_get("org.freedesktop.UPower",
        "/org/freedesktop/UPower/devices/DisplayDevice",
        "org.freedesktop.UPower.Device", "State") or ""
    # UPower: 2 = charging, 3 = discharging, 4 = empty, 5 = fully charged.
    charging = "uint32 2" in state
    present = _dbus_get("org.freedesktop.UPower", "/org/freedesktop/UPower",
        "org.freedesktop.UPower", "DisplayDevice") or ""
    has_battery = "/devices/" in present and "/DisplayDevice" not in present
    time_to_empty = _dbus_get("org.freedesktop.UPower",
        "/org/freedesktop/UPower/devices/DisplayDevice",
        "org.freedesktop.UPower.Device", "TimeToEmpty") or ""
    try:
        minutes = int(float(time_to_empty.split()[-1])) // 60
    except (ValueError, IndexError):
        minutes = 0
    return Battery(percent=min(100, max(0, percent)), charging=charging,
        present=has_battery, time_minutes=minutes)


def read_audio() -> Audio | None:
    """Громкость выхода PipeWire через pactl.

    pactl устанавливается вместе с pipewire и понимает тот же синтаксис, что и
    PulseAudio, поэтому отдельная библиотека оболочке не нужна.
    """
    reply = _run(["pactl", "get-sink-volume", "@DEFAULT_SINK@"])
    if reply is None:
        return None
    # Формат: "Volume: front-left: 45872 /  70% / -9.34 dB". Число до '%' —
    # проценты; первое попавшееся число это байты амплитуды, и брать его
    # нельзя.
    volume = None
    for token in reply.split():
        if token.endswith("%"):
            digits = token.rstrip("%")
            if digits.isdigit():
                volume = int(digits)
                break
    if volume is None:
        return None
    mute = _run(["pactl", "get-sink-mute", "@DEFAULT_SINK@"]) or ""
    return Audio(volume=min(100, volume), muted="Muted: yes" in mute)


def set_audio_volume(percent: int) -> bool:
    percent = min(150, max(0, int(percent)))
    return _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{percent}%"]) \
        is not None


def toggle_audio_mute() -> bool:
    return _run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"]) is not None


def read_brightness() -> int | None:
    """Процент подсветки из sysfs — файл есть не на каждом ноутбуке."""
    base = "/sys/class/backlight"
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return None
    for name in names:
        try:
            with open(f"{base}/{name}/brightness") as handle:
                current = int(handle.read().strip())
            with open(f"{base}/{name}/max_brightness") as handle:
                maximum = int(handle.read().strip())
        except (OSError, ValueError):
            continue
        if maximum > 0:
            return round(current * 100 / maximum)
    return None


def set_brightness(percent: int) -> bool:
    """Яркость меняет brightnessctl: прямая запись в sysfs требует root, а
    оболочка работает обычным пользователем. Без утилиты — молча False, и
    панель не показывает ползунок, который ничего не делает.
    """
    return _run(["brightnessctl", "set",
        f"{min(100, max(0, int(percent)))}%"]) is not None


def collect() -> SystemInfo:
    warnings: list[str] = []
    network = read_network()
    battery = read_battery()
    audio = read_audio()
    brightness = read_brightness()
    if network is None:
        warnings.append("network")
    if audio is None:
        warnings.append("audio")
    return SystemInfo(network=network, battery=battery, audio=audio,
        brightness=brightness, warnings=tuple(warnings))


def as_json(info: SystemInfo) -> str:
    """Для диагностики: `bgpanel --dump-status`."""
    from dataclasses import asdict  # noqa: PLC0415 — локально, чтобы не тянуть

    return json.dumps(asdict(info), ensure_ascii=False, default=str)
