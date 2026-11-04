"""Литеры Windows-диска и фильтрация видимых путей.

Windows-приложение оперирует буквами (C:, Z:), наша файловая система — путями.
Этот слой переводит одно в другое и решает единственный вопрос безопасности,
который нельзя отдать Wine: что именно приложение вправе увидеть.
"""

from __future__ import annotations

import posixpath
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from wcmgr.errors import LaunchError
from wcmgr.manifest import DriveKind, Manifest, Mode

USER_PLACEHOLDER = "%USER%"
DOS_DEVICE_DIR = PurePosixPath("dosdevices")


@dataclass(frozen=True)
class MountAction:
    """Действие, которое должно выполниться до запуска окружения."""

    source: str
    target: str
    options: tuple[str, ...]
    letter: str

    @property
    def read_only(self) -> bool:
        return "ro" in self.options


def expand_user(path: str, user: str) -> str:
    if USER_PLACEHOLDER not in path:
        return path
    if not user:
        raise LaunchError("имя пользователя обязательно: в пути есть %USER%, но сеанс не передан")
    return path.replace(USER_PLACEHOLDER, user)


def dos_device_path(prefix_path: str | Path, letter: str) -> str:
    normalized = (letter or "").upper().rstrip(":")
    if len(normalized) != 1:
        raise LaunchError(f"некорректная литера диска: {letter!r}")
    return posixpath.join(
        posixpath.normpath(str(prefix_path)), DOS_DEVICE_DIR.as_posix(), f"{normalized.lower()}:"
    )


def drive_for(manifest: Manifest, letter: str) -> object | None:
    normalized = letter.upper().rstrip(":")
    for mapping in manifest.drives:
        if mapping.letter == normalized:
            return mapping
    return None


def mount_plan(manifest: Manifest, prefix_path: str | Path, *, user: str = "") -> list[MountAction]:
    """Что и куда монтируем: C: — корень префикса, остальные — из манифеста."""
    prefix = posixpath.normpath(str(prefix_path))
    actions = [
        MountAction(
            source=posixpath.join(prefix, "drive_c"),
            target=dos_device_path(prefix, "C"),
            options=("bind", "rw"),
            letter="C",
        )
    ]
    for mapping in manifest.drives:
        if mapping.letter == "C":
            continue
        target = dos_device_path(prefix, mapping.letter)
        if mapping.kind is DriveKind.PREFIX:
            continue
        source = expand_user(mapping.path or "", user) if mapping.path else ""
        if not source:
            raise LaunchError(f"литера {mapping.letter}: без источника пути")
        mode = "ro" if mapping.mode is Mode.RO else "rw"
        actions.append(
            MountAction(
                source=source,
                target=target,
                options=("bind", mode),
                letter=mapping.letter,
            )
        )
    return actions


def visible_roots(manifest: Manifest, prefix_path: str | Path, *, user: str = "") -> tuple[str, ...]:
    """Разрешённые к открытию корни: префикс плюс явный список из манифеста."""
    prefix = posixpath.normpath(str(prefix_path))
    roots = [
        prefix,
        posixpath.join(prefix, "drive_c"),
        posixpath.join(prefix, "temp"),
        posixpath.join(prefix, "logs"),
    ]
    for entry in manifest.visible:
        roots.append(expand_user(entry, user) if USER_PLACEHOLDER in entry else entry)
    for mapping in manifest.drives:
        if mapping.kind is DriveKind.BIND and mapping.path:
            roots.append(expand_user(mapping.path, user))
    return tuple(dict.fromkeys(roots))


def is_path_allowed(
    path: str,
    manifest: Manifest,
    prefix_path: str | Path,
    *,
    user: str = "",
    resolve: Callable[[str], str] | None = None,
) -> bool:
    """Разрешён ли доступ к пути, с учётом символьных ссылок и скрытых путей.

    Проверяем и нормализованный путь, и результат раскрытия ссылок: иначе
    `Z:/подкаталог -> /etc` обошёл бы весь фильтр.
    """
    if not path:
        return False
    normalizer = resolve or (lambda item: item)
    candidates = {posixpath.normpath(path), posixpath.normpath(normalizer(path))}
    hidden = {posixpath.normpath(entry) for entry in manifest.hidden}
    roots = visible_roots(manifest, prefix_path, user=user)
    for candidate in candidates:
        if any(candidate == entry or candidate.startswith(entry.rstrip("/") + "/") for entry in hidden):
            return False
        if not _under_any(candidate, roots):
            return False
    return True


def _under_any(candidate: str, roots) -> bool:
    for root in roots:
        normalized = posixpath.normpath(root)
        if candidate == normalized or candidate.startswith(normalized.rstrip("/") + "/"):
            return True
    return False


def posix_to_windows(
    path: str, manifest: Manifest, prefix_path: str | Path, *, user: str = ""
) -> str:
    """Обратное преобразование: для «Recent items» и перетаскивания файлов."""
    prefix = posixpath.normpath(str(prefix_path))
    normalized = posixpath.normpath(path)
    drive_c = posixpath.join(prefix, "drive_c")
    if normalized == drive_c or normalized.startswith(drive_c + "/"):
        tail = normalized[len(drive_c) :].strip("/").replace("/", "\\")
        return f"C:\\{tail}" if tail else "C:\\"
    for mapping in manifest.drives:
        if mapping.kind is not DriveKind.BIND or not mapping.path:
            continue
        source = posixpath.normpath(expand_user(mapping.path, user))
        if normalized == source or normalized.startswith(source + "/"):
            tail = normalized[len(source) :].strip("/")
            if tail:
                return f"{mapping.letter}:\\" + tail.replace("/", "\\")
            return f"{mapping.letter}:\\"
    raise LaunchError(f"путь {path} не отображается ни на одну литеру WinCompat")


def windows_to_posix(
    win_path: str, manifest: Manifest, prefix_path: str | Path, *, user: str = ""
) -> str:
    letter = (win_path[:1] or "").upper()
    mapping = drive_for(manifest, letter)
    tail = win_path[2:].replace("\\", "/").strip("/")
    prefix = posixpath.normpath(str(prefix_path))
    if letter == "C" or (mapping is not None and getattr(mapping, "kind", None) is DriveKind.PREFIX):
        base = posixpath.join(prefix, "drive_c")
    elif mapping is not None and getattr(mapping, "path", None):
        base = posixpath.normpath(expand_user(mapping.path, user))
    else:
        raise LaunchError(f"литера {letter}: не настроена для этого приложения")
    return PurePosixPath(base, tail).as_posix() if tail else base
