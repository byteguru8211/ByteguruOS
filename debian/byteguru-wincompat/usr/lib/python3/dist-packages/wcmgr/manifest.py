"""WCV — манифест Windows-приложения для подсистемы WinCompat.

Манифест — единственный источник истины о том, как приложение запускается и
какими ресурсами ему разрешено пользоваться. Формируем правила, которые нельзя
вычитать из файла позже: неизвестный ключ считается ошибкой (опечатка в
`networke` не должна тихо превращаться в «сети нет» или в «сеть открыта»).
"""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import PurePosixPath
from typing import Any

from wcmgr.errors import ManifestError

SCHEMA_VERSION = 1
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,63}$")
SEMVERISH_RE = re.compile(r"^[0-9][0-9A-Za-z.\-+~_]{0,63}$")
DRIVE_LETTERS = tuple(chr(code) for code in range(ord("C"), ord("Z") + 1))

# Буквы A и B зарезервированы под исторические гибкие диски: Wine на них
# спотыкается, а пользователь потом не может объяснить, почему «не монтируется».
FORBIDDEN_LETTERS = frozenset({"A", "B"})

# Пути, которые нельзя открывать на запись в Windows-окружение ни при каких
# значениях манифеста: их порча ломает систему, а не одно приложение.
SYSTEM_ROOTS = frozenset(
    {
        "/",
        "/bin",
        "/boot",
        "/dev",
        "/etc",
        "/home",
        "/lib",
        "/proc",
        "/root",
        "/run",
        "/sbin",
        "/srv",
        "/sys",
        "/tmp",
        "/usr",
        "/var",
    }
)


class CoverageLevel(str, Enum):
    """Уровень покрытия из раздела 5.1 плана разработки."""

    L0 = "l0"  # не поддерживается
    L1 = "l1"  # полноценная работа в WinCompat
    L2 = "l2"  # базовый сценарий (просмотр, печать)
    L3 = "l3"  # через виртуальную машину (RDP/RemoteApp)
    L4 = "l4"  # через перезагрузку в Windows на этом же диске

    @property
    def needs_windows_environment(self) -> bool:
        return self in (CoverageLevel.L3, CoverageLevel.L4)

    @property
    def is_supported(self) -> bool:
        return self is not CoverageLevel.L0


class Runner(str, Enum):
    WINE = "wine"
    KVM = "kvm"
    NATIVE = "native"


class GraphPath(str, Enum):
    AUTO = "auto"
    DXVK = "dxvk"
    VKD3D = "vkd3d"
    X11 = "x11"
    GLX = "glx"


class NetworkMode(str, Enum):
    BLOCKED = "blocked"
    ALLOWLIST = "allowlist"
    OPEN = "open"


class PrintMode(str, Enum):
    NONE = "none"
    QUEUE_ONLY = "queue-only"
    DIRECT = "direct"


class AppArmorMode(str, Enum):
    ENFORCE = "enforce"
    COMPLAIN = "complain"
    OFF = "off"


class SeccompMode(str, Enum):
    STRICT = "strict"
    BASIC = "basic"
    OFF = "off"


class UpdatePolicy(str, Enum):
    DISABLED = "disabled"
    MANUAL = "manual"
    AUTO = "auto"


class DriveKind(str, Enum):
    PREFIX = "prefix"
    BIND = "bind"
    IMAGE = "image"


class Mode(str, Enum):
    RO = "ro"
    RW = "rw"


@dataclass(frozen=True)
class DriveMapping:
    letter: str
    kind: DriveKind
    path: str | None = None
    mode: Mode = Mode.RW

    def as_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"letter": self.letter, "kind": self.kind.value, "mode": self.mode.value}
        if self.path is not None:
            data["path"] = self.path
        return data


@dataclass(frozen=True)
class PortRule:
    proto: str
    port: int

    def as_dict(self) -> dict[str, Any]:
        return {"proto": self.proto, "port": self.port}


@dataclass(frozen=True)
class UsbDevice:
    vendor: str
    product: str
    description: str = ""

    def as_dict(self) -> dict[str, Any]:
        data = {"vendor": self.vendor, "product": self.product}
        if self.description:
            data["description"] = self.description
        return data


@dataclass(frozen=True)
class Manifest:
    """Разобранный и проверенный WCV-манифест."""

    schema: int
    id: str
    name: str
    version: str
    executable: str
    runner: Runner
    expected_level: CoverageLevel
    publisher: str = ""
    wine: str = ""
    tested: bool = False
    tested_notes: str = ""
    prefix: str = ""
    installer_sha256: str = ""
    installer_filename: str = ""
    graph_path: GraphPath = GraphPath.AUTO
    dxvk_version: str = ""
    drives: tuple[DriveMapping, ...] = ()
    network_mode: NetworkMode = NetworkMode.BLOCKED
    domains: tuple[str, ...] = ()
    ports: tuple[PortRule, ...] = ()
    audio: bool = True
    camera: bool = False
    usb: tuple[UsbDevice, ...] = ()
    printers: PrintMode = PrintMode.QUEUE_ONLY
    visible: tuple[str, ...] = ()
    hidden: tuple[str, ...] = ()
    apparmor: AppArmorMode = AppArmorMode.ENFORCE
    seccomp: SeccompMode = SeccompMode.STRICT
    no_new_privs: bool = True
    menu: bool = True
    mime: tuple[str, ...] = ()
    update_policy: UpdatePolicy = UpdatePolicy.DISABLED
    extra_comments: tuple[str, ...] = field(default=())

    @property
    def app_id(self) -> str:
        return self.id

    @property
    def prefix_name(self) -> str:
        return self.prefix or self.id

    @property
    def evidence_complete(self) -> bool:
        """Манифест утверждён как рабочий только при подтверждённом прогоне."""
        return self.tested and self.expected_level is not CoverageLevel.L0

    def fingerprint(self) -> str:
        """Стабильный хэш содержания: меняется при любой правке политики."""
        return hashlib.sha256(
            json.dumps(self.to_nested(), sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        return {k: _plain(v) for k, v in asdict(self).items()}

    def to_nested(self) -> dict[str, Any]:
        """Представление теми же секциями, что и файл манифеста."""
        return _nested(self)

    @classmethod
    def from_nested(cls, data: dict[str, Any]) -> "Manifest":
        """Сборка из секционного вида: используется реестром и порталом API."""
        merged: dict[str, Any] = {}
        for section, pairs in _SECTION_FIELDS.items():
            block = data.get(section, {})
            if not isinstance(block, dict):
                continue
            for out_key, attr in pairs:
                if out_key in block:
                    merged[attr] = block[out_key]
        merged["schema"] = int(merged.get("schema", SCHEMA_VERSION))
        merged["runner"] = Runner(merged.get("runner", "wine"))
        merged["expected_level"] = CoverageLevel(merged.get("expected_level", "l0"))
        merged["graph_path"] = GraphPath(merged.get("graph_path", "auto"))
        merged["network_mode"] = NetworkMode(merged.get("network_mode", "blocked"))
        merged["printers"] = PrintMode(merged.get("printers", "queue-only"))
        merged["apparmor"] = AppArmorMode(merged.get("apparmor", "enforce"))
        merged["seccomp"] = SeccompMode(merged.get("seccomp", "strict"))
        merged["update_policy"] = UpdatePolicy(merged.get("update_policy", "disabled"))
        merged["drives"] = tuple(
            DriveMapping(
                letter=item["letter"],
                kind=DriveKind(item["kind"]),
                path=item.get("path") or None,
                mode=Mode(item.get("mode", "rw")),
            )
            for item in merged.get("drives", ())
        )
        merged["ports"] = tuple(
            PortRule(proto=item["proto"], port=int(item["port"]))
            for item in merged.get("ports", ())
        )
        merged["usb"] = tuple(
            UsbDevice(
                vendor=item["vendor"],
                product=item["product"],
                description=item.get("description", ""),
            )
            for item in merged.get("usb", ())
        )
        for key in ("domains", "visible", "hidden", "mime", "extra_comments"):
            merged[key] = tuple(merged.get(key, ()) or ())
        merged["tested"] = bool(merged.get("tested", False))
        merged["audio"] = bool(merged.get("audio", True))
        merged["camera"] = bool(merged.get("camera", False))
        merged["menu"] = bool(merged.get("menu", True))
        merged["no_new_privs"] = bool(merged.get("no_new_privs", True))
        known = {f for f in Manifest.__dataclass_fields__}
        return cls(**{key: value for key, value in merged.items() if key in known})


def _plain(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return {key: _plain(item) for key, item in asdict(value).items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    return value


_SECTION_FIELDS: dict[str, tuple[tuple[str, str], ...]] = {
    "wcv": (
        ("schema", "schema"),
        ("id", "id"),
        ("name", "name"),
        ("version", "version"),
        ("publisher", "publisher"),
        ("executable", "executable"),
        ("runner", "runner"),
        ("expected_level", "expected_level"),
        ("wine", "wine"),
        ("prefix", "prefix"),
        ("tested", "tested"),
        ("tested_notes", "tested_notes"),
        ("installer_filename", "installer_filename"),
        ("installer_sha256", "installer_sha256"),
        ("comments", "extra_comments"),
    ),
    "graph": (("path", "graph_path"), ("dxvk_version", "dxvk_version")),
    "drives": (("mappings", "drives"),),
    "network": (("mode", "network_mode"), ("domains", "domains"), ("ports", "ports")),
    "devices": (("audio", "audio"), ("camera", "camera"), ("printers", "printers"), ("usb", "usb")),
    "filesystem": (("visible", "visible"), ("hidden", "hidden")),
    "security": (("apparmor", "apparmor"), ("seccomp", "seccomp"), ("no_new_privs", "no_new_privs")),
    "integration": (("menu", "menu"), ("mime", "mime")),
    "update": (("policy", "update_policy"),),
}


def _nested(manifest: Manifest) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for section, pairs in _SECTION_FIELDS.items():
        block: dict[str, Any] = {}
        for out_key, attr in pairs:
            block[out_key] = _plain(getattr(manifest, attr))
        result[section] = block
    return result


# --------------------------------------------------------------------------- парсинг

_SECTIONS: dict[str, set[str]] = {
    "wcv": {
        "schema",
        "id",
        "name",
        "version",
        "publisher",
        "executable",
        "runner",
        "expected_level",
        "wine",
        "tested",
        "tested_notes",
        "prefix",
        "installer_filename",
        "installer_sha256",
        "comments",
    },
    "graph": {"path", "dxvk_version"},
    "drives": {"mappings"},
    "network": {"mode", "domains", "ports"},
    "devices": {"audio", "camera", "usb", "printers"},
    "filesystem": {"visible", "hidden"},
    "security": {"apparmor", "seccomp", "no_new_privs"},
    "integration": {"menu", "mime"},
    "update": {"policy"},
}
_REQUIRED_TOP = {"schema", "id", "name", "version", "executable", "runner", "expected_level"}


def _abs_posix(value: Any, where: str, problems: list[str], *, forbid_system_root: bool = False) -> str:
    if not isinstance(value, str) or not value:
        problems.append(f"{where}: ожидается непустая строка, получено {value!r}")
        return ""
    pure = PurePosixPath(value)
    if not pure.is_absolute():
        problems.append(f"{where}: путь обязан быть абсолютным POSIX ({value})")
    if ".." in pure.parts:
        problems.append(f"{where}: путь не должен содержать '..' ({value})")
    if "\\" in value:
        problems.append(f"{where}: путь не должен содержать обратные слэши ({value})")
    if forbid_system_root:
        normalized = value if value == "/" else value.rstrip("/")
        if normalized in SYSTEM_ROOTS:
            problems.append(f"{where}: путь {value} совпадает с системным корнем и запрещён")
    return value


def _enum(enum_cls: type[Enum], value: Any, where: str, problems: list[str]) -> Any:
    if not isinstance(value, str):
        problems.append(f"{where}: ожидается строка, получено {value!r}")
        return None
    try:
        return enum_cls(value.lower())
    except ValueError:
        allowed = ", ".join(sorted(str(item.value) for item in enum_cls))
        problems.append(f"{where}: значение {value!r} вне списка [{allowed}]")
        return None


def _boolean(value: Any, where: str, problems: list[str]) -> bool:
    if not isinstance(value, bool):
        problems.append(f"{where}: ожидается true/false, получено {value!r}")
        return False
    return value


def _string_list(value: Any, where: str, problems: list[str]) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        problems.append(f"{where}: ожидается список строк")
        return ()
    return tuple(value)


def _check_unknown(section: str, data: dict[str, Any], problems: list[str]) -> None:
    known = _SECTIONS[section]
    for key in data:
        if key not in known:
            problems.append(f"[{section}]: неизвестный ключ {key!r} (известные: {', '.join(sorted(known))})")


def _parse_mapping(raw: Any, index: int, problems: list[str]) -> DriveMapping | None:
    where = f"drives.mappings[{index}]"
    if not isinstance(raw, dict):
        problems.append(f"{where}: ожидается таблица")
        return None
    allowed_keys = {"letter", "kind", "path", "mode"}
    unknown = set(raw) - allowed_keys
    if unknown:
        problems.append(f"{where}: неизвестные ключи {sorted(unknown)}")
    letter = raw.get("letter", "")
    if not isinstance(letter, str) or len(letter) != 1:
        problems.append(f"{where}.letter: ожидается одна буква диска (например 'E')")
        return None
    letter = letter.upper()
    if letter in FORBIDDEN_LETTERS:
        problems.append(f"{where}.letter: буква {letter} зарезервирована и недоступна")
        return None
    if letter not in DRIVE_LETTERS:
        problems.append(f"{where}.letter: недопустимая буква диска {letter!r}")
        return None
    kind = _enum(DriveKind, raw.get("kind", ""), f"{where}.kind", problems)
    mode = _enum(Mode, raw.get("mode", Mode.RW.value), f"{where}.mode", problems)
    if kind is None:
        return None
    path = None
    if kind is DriveKind.BIND:
        path = _abs_posix(
            raw.get("path", ""), f"{where}.path", problems, forbid_system_root=True
        )
        if not path:
            return None
        if path.startswith("/var/lib/byteguru/wcv"):
            problems.append(f"{where}.path: окружение WinCompat нельзя монтировать внутрь самого себя")
    elif kind is DriveKind.IMAGE:
        path = _abs_posix(raw.get("path", ""), f"{where}.path", problems)
        if not path:
            return None
        if not path.lower().endswith((".img", ".iso")):
            problems.append(f"{where}.path: для kind='image' ожидается файл образа (.img/.iso)")
    elif "path" in raw and raw["path"]:
        problems.append(f"{where}: для kind='prefix' путь не задают")
    if kind is DriveKind.PREFIX and letter != "C":
        problems.append(f"{where}: литер C обязана быть корневым префиксом, а не буквой {letter}")
    if letter == "C" and kind is not DriveKind.PREFIX:
        problems.append(f"{where}: диск C: в WinCompat всегда соответствует корню префикса")
    return DriveMapping(letter=letter, kind=kind, path=path, mode=mode or Mode.RW)


def _parse_port(raw: Any, index: int, problems: list[str]) -> PortRule | None:
    where = f"network.ports[{index}]"
    if not isinstance(raw, dict):
        problems.append(f"{where}: ожидается таблица {{proto, port}}")
        return None
    unknown = set(raw) - {"proto", "port"}
    if unknown:
        problems.append(f"{where}: неизвестные ключи {sorted(unknown)}")
    proto = str(raw.get("proto", "")).lower()
    if proto not in ("tcp", "udp"):
        problems.append(f"{where}.proto: ожидается tcp или udp")
        return None
    port = raw.get("port")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        problems.append(f"{where}.port: ожидается целый номер порта 1..65535")
        return None
    return PortRule(proto=proto, port=port)


def _parse_usb(raw: Any, index: int, problems: list[str]) -> UsbDevice | None:
    where = f"devices.usb[{index}]"
    if not isinstance(raw, dict):
        problems.append(f"{where}: ожидается таблица {{vendor, product}}")
        return None
    unknown = set(raw) - {"vendor", "product", "description"}
    if unknown:
        problems.append(f"{where}: неизвестные ключи {sorted(unknown)}")
    hex4 = re.compile(r"^[0-9a-fA-F]{4}$")
    vendor = str(raw.get("vendor", ""))
    product = str(raw.get("product", ""))
    if not hex4.match(vendor):
        problems.append(f"{where}.vendor: ожидается 4 hex-разряда ID производителя")
        return None
    if not hex4.match(product):
        problems.append(f"{where}.product: ожидается 4 hex-разряда ID устройства")
        return None
    return UsbDevice(
        vendor=vendor.lower(),
        product=product.lower(),
        description=str(raw.get("description", "")),
    )


def validate_manifest_text(text: str) -> list[str]:
    """Список проблем пуст — значит манифест пригоден к исполнению."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return [f"TOML не разобран: {exc}"]
    return _validate_data(data)


def _validate_data(data: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    unknown_top = set(data) - set(_SECTIONS) - {"source"}
    if unknown_top:
        problems.append(f"корень: неизвестные секции {sorted(unknown_top)}")

    wcv = data.get("wcv")
    if not isinstance(wcv, dict):
        problems.append("wcv: секция обязательна и должна быть таблицей")
        return problems
    _check_unknown("wcv", wcv, problems)
    missing = _REQUIRED_TOP - set(wcv)
    if missing:
        problems.append(f"wcv: не хватает обязательных ключей {sorted(missing)}")

    schema = wcv.get("schema")
    if schema != SCHEMA_VERSION:
        problems.append(f"wcv.schema: поддерживается только {SCHEMA_VERSION}, получено {schema!r}")
    app_id = wcv.get("id", "")
    if not isinstance(app_id, str) or not ID_RE.match(app_id):
        problems.append(f"wcv.id: {app_id!r} не соответствует [a-z0-9][a-z0-9._-]{{1,63}}")
    for key in ("name", "version", "executable"):
        value = wcv.get(key)
        if not isinstance(value, str) or not value.strip():
            problems.append(f"wcv.{key}: требуется непустая строка")
    version = wcv.get("version", "")
    if isinstance(version, str) and version and not SEMVERISH_RE.match(version):
        problems.append(f"wcv.version: {version!r} выглядит не как номер версии")
    _enum(Runner, wcv.get("runner", ""), "wcv.runner", problems)
    _enum(CoverageLevel, wcv.get("expected_level", ""), "wcv.expected_level", problems)
    exe = wcv.get("executable")
    if isinstance(exe, str) and exe and not exe.lower().endswith((".exe", ".msi")):
        problems.append(f"wcv.executable: ожидается .exe или .msi, получено {exe!r}")
    if "wine" in wcv and not isinstance(wcv["wine"], str):
        problems.append("wcv.wine: ожидается строка с версией Wine")
    if "tested" in wcv:
        _boolean(wcv["tested"], "wcv.tested", problems)
    sha = wcv.get("installer_sha256", "")
    if sha and (not isinstance(sha, str) or not SHA256_RE.match(sha)):
        problems.append("wcv.installer_sha256: ожидается 64 hex-разряда строчными")

    for section in ("graph", "drives", "network", "devices", "filesystem", "security", "integration", "update"):
        block = data.get(section, {})
        if not isinstance(block, dict):
            problems.append(f"{section}: ожидается таблица")
            continue
        _check_unknown(section, block, problems)

    graph = data.get("graph", {})
    if isinstance(graph, dict):
        _enum(GraphPath, graph.get("path", "auto"), "graph.path", problems)

    seen_letters: set[str] = set()
    for index, raw in enumerate((data.get("drives") or {}).get("mappings", []) or []):
        parsed = _parse_mapping(raw, index, problems)
        if parsed is None:
            continue
        if parsed.letter in seen_letters:
            problems.append(f"drives.mappings[{index}]: литер {parsed.letter}: назначена дважды")
        seen_letters.add(parsed.letter)

    network = data.get("network", {})
    if isinstance(network, dict):
        mode = _enum(NetworkMode, network.get("mode", "blocked"), "network.mode", problems)
        domains = network.get("domains", [])
        if domains:
            _string_list(domains, "network.domains", problems)
            if mode is not NetworkMode.ALLOWLIST:
                problems.append("network.domains: список доменов имеет смысл только при mode='allowlist'")
        for index, _raw in enumerate(network.get("ports", []) or []):
            _parse_port(_raw, index, problems)
        if mode is NetworkMode.OPEN:
            problems.append(
                "network.mode='open' запрещён в подписанных манифестах: "
                "используйте allowlist, либо не публикуйте приложение в реестре"
            )

    devices = data.get("devices", {})
    if isinstance(devices, dict):
        for key in ("audio", "camera"):
            if key in devices:
                _boolean(devices[key], f"devices.{key}", problems)
        _enum(PrintMode, devices.get("printers", "queue-only"), "devices.printers", problems)
        for index, raw in enumerate(devices.get("usb", []) or []):
            _parse_usb(raw, index, problems)

    fs = data.get("filesystem", {})
    if isinstance(fs, dict):
        for key in ("visible", "hidden"):
            if key in fs:
                for item in _string_list(fs[key], f"filesystem.{key}", problems):
                    _abs_posix(item, f"filesystem.{key}", problems)

    security = data.get("security", {})
    if isinstance(security, dict):
        _enum(AppArmorMode, security.get("apparmor", "enforce"), "security.apparmor", problems)
        _enum(SeccompMode, security.get("seccomp", "strict"), "security.seccomp", problems)
        if "no_new_privs" in security:
            no_new_privs = _boolean(security["no_new_privs"], "security.no_new_privs", problems)
            if no_new_privs is False:
                problems.append("security.no_new_privs=false запрещён политикой безопасности ОС")
        apparmor = security.get("apparmor", "enforce")
        if isinstance(apparmor, str) and apparmor.lower() == "off":
            problems.append("security.apparmor='off' допустим только в локальных отладочных манифестах")

    integration = data.get("integration", {})
    if isinstance(integration, dict) and "mime" in integration:
        _string_list(integration["mime"], "integration.mime", problems)
    update = data.get("update", {})
    if isinstance(update, dict):
        _enum(UpdatePolicy, update.get("policy", "disabled"), "update.policy", problems)

    return problems


def _comments(value: Any) -> tuple[str, ...]:
    """Комментарии допускают и многострочную строку, и список строк."""
    if isinstance(value, str):
        return tuple(line.strip() for line in value.splitlines() if line.strip())
    if isinstance(value, (list, tuple)):
        return tuple(str(item).strip() for item in value if str(item).strip())
    return ()


def _build(data: dict[str, Any]) -> Manifest:
    wcv = data["wcv"]
    graph = data.get("graph", {})
    drives_block = data.get("drives", {})
    network = data.get("network", {})
    devices = data.get("devices", {})
    fs = data.get("filesystem", {})
    security = data.get("security", {})
    integration = data.get("integration", {})
    update = data.get("update", {})

    drives = tuple(
        _parse_mapping(raw, index, [])
        for index, raw in enumerate((drives_block or {}).get("mappings", []) or [])
    )
    ports = tuple(
        _parse_port(raw, index, []) for index, raw in enumerate((network or {}).get("ports", []) or [])
    )
    usb = tuple(_parse_usb(raw, index, []) for index, raw in enumerate((devices or {}).get("usb", []) or []))

    return Manifest(
        schema=int(wcv["schema"]),
        id=str(wcv["id"]),
        name=str(wcv["name"]),
        version=str(wcv["version"]),
        executable=str(wcv["executable"]),
        runner=Runner(str(wcv["runner"]).lower()),
        expected_level=CoverageLevel(str(wcv["expected_level"]).lower()),
        publisher=str(wcv.get("publisher", "")),
        wine=str(wcv.get("wine", "")),
        tested=bool(wcv.get("tested", False)),
        tested_notes=str(wcv.get("tested_notes", "")),
        prefix=str(wcv.get("prefix", "")),
        installer_filename=str(wcv.get("installer_filename", "")),
        installer_sha256=str(wcv.get("installer_sha256", "")),
        graph_path=GraphPath(str(graph.get("path", "auto")).lower()),
        dxvk_version=str(graph.get("dxvk_version", "")),
        drives=tuple(item for item in drives if item is not None),
        network_mode=NetworkMode(str((network or {}).get("mode", "blocked")).lower()),
        domains=tuple((network or {}).get("domains", []) or ()),
        ports=tuple(item for item in ports if item is not None),
        audio=bool((devices or {}).get("audio", True)),
        camera=bool((devices or {}).get("camera", False)),
        usb=tuple(item for item in usb if item is not None),
        printers=PrintMode(str((devices or {}).get("printers", "queue-only")).lower()),
        visible=tuple((fs or {}).get("visible", []) or ()),
        hidden=tuple((fs or {}).get("hidden", []) or ()),
        apparmor=AppArmorMode(str((security or {}).get("apparmor", "enforce")).lower()),
        seccomp=SeccompMode(str((security or {}).get("seccomp", "strict")).lower()),
        no_new_privs=bool((security or {}).get("no_new_privs", True)),
        menu=bool((integration or {}).get("menu", True)),
        mime=tuple((integration or {}).get("mime", []) or ()),
        update_policy=UpdatePolicy(str((update or {}).get("policy", "disabled")).lower()),
        extra_comments=_comments(wcv.get("comments", ())),
    )


def load_manifest_text(text: str) -> Manifest:
    problems = validate_manifest_text(text)
    if problems:
        raise ManifestError(problems)
    return _build(tomllib.loads(text))


def load_manifest(path: str) -> Manifest:
    with open(path, encoding="utf-8") as stream:
        return load_manifest_text(stream.read())


# --------------------------------------------------------------------------- запись

_SCALAR_ORDER = {
    "wcv": (
        "schema",
        "id",
        "name",
        "version",
        "publisher",
        "executable",
        "runner",
        "expected_level",
        "wine",
        "prefix",
        "tested",
        "tested_notes",
        "installer_filename",
        "installer_sha256",
        "comments",
    ),
    "graph": ("path", "dxvk_version"),
    "drives": ("mappings",),
    "network": ("mode", "domains", "ports"),
    "devices": ("audio", "camera", "printers", "usb"),
    "filesystem": ("visible", "hidden"),
    "security": ("apparmor", "seccomp", "no_new_privs"),
    "integration": ("menu", "mime"),
    "update": ("policy",),
}
_DEFAULTS: dict[str, dict[str, Any]] = {
    "graph": {"path": "auto", "dxvk_version": ""},
    "network": {"mode": "blocked", "domains": (), "ports": ()},
    "devices": {"audio": True, "camera": False, "printers": "queue-only", "usb": ()},
    "filesystem": {"visible": (), "hidden": ()},
    "security": {"apparmor": "enforce", "seccomp": "strict", "no_new_privs": True},
    "integration": {"menu": True, "mime": ()},
    "update": {"policy": "disabled"},
}


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, Enum):
        return f'"{value.value}"'
    if isinstance(value, str):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_scalar(item) for item in value) + "]"
    raise TypeError(f"не умеем писать TOML-значение для {type(value)}")


def dump_toml(manifest: Manifest, *, header_comments: tuple[str, ...] = ()) -> str:
    """Канонический вид: тот же манифест всегда даёт тот же файл."""
    payload = manifest.to_nested()
    lines: list[str] = []
    for comment in header_comments or manifest.extra_comments:
        lines.append(f"# {comment}")
    for section in (
        "wcv",
        "graph",
        "drives",
        "network",
        "devices",
        "filesystem",
        "security",
        "integration",
        "update",
    ):
        lines.append(f"[{section}]")
        defaults = _DEFAULTS.get(section, {})
        block = payload[section]
        for key in _SCALAR_ORDER[section]:
            value = block.get(key, defaults.get(key))
            if isinstance(value, (list, tuple)) and value and isinstance(value[0], dict):
                rendered_items = []
                for item in value:
                    pairs = ", ".join(
                        f"{name} = {_toml_scalar(val)}"
                        for name, val in item.items()
                        if val not in ("", None)
                    )
                    rendered_items.append(f"  {{{pairs}}}")
                lines.append(f"{key} = [\n" + ",\n".join(rendered_items) + "\n]")
                continue
            if not value and value != 0:
                continue
            lines.append(f"{key} = {_toml_scalar(value)}")
        lines.append("")
    text = "\n".join(lines).rstrip() + "\n"
    return re.sub(r"\n{3,}", "\n\n", text)
