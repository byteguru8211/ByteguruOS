"""Окружение WCV (wineprefix + версионируемое состояние).

Одно приложение — одно изолированное окружение. Общего реестра на все программы
нет сознательно: обновление одной программы не должно ломать другую, а откат
обязан возвращать ровно то состояние, с которым манифест был протестирован.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from wcmgr.errors import PrefixError
from wcmgr.manifest import ID_RE, Manifest

MANIFEST_FILENAME = "manifest.wcv"
CURRENT_FILENAME = "CURRENT"
STATE_DIRNAME = "state"
VERSIONS_DIRNAME = "versions"
DRIVE_C = "drive_c"
SYSTEM_DIR = "system"
LOGS_DIR = "logs"
TEMP_DIR = "temp"


@dataclass(frozen=True)
class PrefixInfo:
    app_id: str
    path: Path
    version: str
    manifest_fingerprint: str
    updated_at: float

    @property
    def windows_root(self) -> Path:
        return self.path / DRIVE_C


class PrefixStore:
    def __init__(self, root: str | Path, *, base_runtime: str | Path | None = None) -> None:
        self.root = Path(root).resolve()
        self.base_runtime = Path(base_runtime).resolve() if base_runtime else None
        if self.base_runtime is not None and not self.base_runtime.is_dir():
            raise PrefixError(f"базовый рантайм не найден: {self.base_runtime}")

    # ---------------------------------------------------------------- helpers
    def _validate_id(self, app_id: str) -> None:
        if not ID_RE.match(app_id or ""):
            raise PrefixError(f"некорректный идентификатор приложения: {app_id!r}")

    def path_for(self, app_id: str) -> Path:
        self._validate_id(app_id)
        candidate = (self.root / app_id).resolve()
        # Разрыв через '..' или символьную ссылку не должен давать доступ наружу.
        if candidate != self.root and self.root not in candidate.parents:
            raise PrefixError(f"путь окружения выходит за пределы хранилища: {candidate}")
        return candidate

    def exists(self, app_id: str) -> bool:
        return (self.path_for(app_id) / CURRENT_FILENAME).is_file()

    # ------------------------------------------------------------------ создание
    def create(self, manifest: Manifest, *, version: str | None = None, now: float | None = None) -> Path:
        path = self.path_for(manifest.prefix_name)
        if path.exists() and (path / CURRENT_FILENAME).exists():
            raise PrefixError(f"окружение {manifest.id} уже существует: {path}")
        tag = version or _time_tag(now)
        for sub in (DRIVE_C, SYSTEM_DIR, LOGS_DIR, TEMP_DIR, STATE_DIRNAME, VERSIONS_DIRNAME):
            (path / sub).mkdir(parents=True, exist_ok=True)
        self._write_manifest(path, manifest)
        self._commit_version(path, manifest, tag)
        if self.base_runtime is not None:
            (path / STATE_DIRNAME / "overlay_source").write_text(
                str(self.base_runtime) + "\n", encoding="utf-8"
            )
        (path / STATE_DIRNAME / "created.json").write_text(
            json.dumps(
                {
                    "app_id": manifest.id,
                    "created": now if now is not None else time.time(),
                    "fingerprint": manifest.fingerprint(),
                    "schema": manifest.schema,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return path

    def update_manifest(self, manifest: Manifest, *, version: str | None = None, now: float | None = None) -> Path:
        """Новый манифест = новая версия окружения; старая остаётся для отката."""
        path = self.path_for(manifest.prefix_name)
        if not path.is_dir():
            raise PrefixError(f"окружение не создано: {path}")
        previous = self.current_version(manifest.prefix_name)
        self._write_manifest(path, manifest)
        tag = version or _time_tag(now)
        self._commit_version(path, manifest, tag, previous=previous)
        return path

    def remove(self, app_id: str, *, keep_data: bool = False) -> None:
        path = self.path_for(app_id)
        if not path.is_dir():
            raise PrefixError(f"окружение не найдено: {path}")
        if keep_data:
            shutil.rmtree(path / VERSIONS_DIRNAME, ignore_errors=True)
            (path / CURRENT_FILENAME).unlink(missing_ok=True)
            return
        shutil.rmtree(path)

    # ------------------------------------------------------------------ версии
    def current_version(self, app_id: str) -> str:
        pointer = self.path_for(app_id) / CURRENT_FILENAME
        if not pointer.is_file():
            raise PrefixError(f"окружение {app_id} не инициализировано")
        return pointer.read_text(encoding="utf-8").strip()

    def current(self, app_id: str) -> PrefixInfo | None:
        path = self.path_for(app_id)
        pointer = path / CURRENT_FILENAME
        if not pointer.is_file():
            return None
        tag = pointer.read_text(encoding="utf-8").strip()
        record = self._version_record(path, tag)
        return PrefixInfo(
            app_id=app_id,
            path=path,
            version=tag,
            manifest_fingerprint=record["fingerprint"],
            updated_at=float(record.get("ts", 0.0)),
        )

    def versions(self, app_id: str) -> list[str]:
        path = self.path_for(app_id)
        store = path / VERSIONS_DIRNAME
        if not store.is_dir():
            return []
        return sorted(item.name for item in store.iterdir() if (item / "version.json").is_file())

    def rollback(self, app_id: str, *, to_version: str | None = None) -> str:
        """Вернуть окружение на предыдущую (или явную) версию манифеста и реестра."""
        path = self.path_for(app_id)
        available = self.versions(app_id)
        if not available:
            raise PrefixError(f"нет ни одной записанной версии для {app_id}")
        current = self.current_version(app_id)
        if to_version is None:
            index = available.index(current) if current in available else len(available) - 1
            if index == 0:
                raise PrefixError(f"откатывать некуда: {current} — первая версия")
            target = available[index - 1]
        else:
            if to_version not in available:
                raise PrefixError(f"версия {to_version} не найдена, есть: {', '.join(available)}")
            target = to_version
        manifest_copy = path / VERSIONS_DIRNAME / target / MANIFEST_FILENAME
        if not manifest_copy.is_file():
            raise PrefixError(f"в версии {target} потерян манифест")
        shutil.copyfile(manifest_copy, path / MANIFEST_FILENAME)
        self._write_pointer(path, target)
        return target

    def verify(self, app_id: str, manifest: Manifest) -> bool:
        """Манифест на диске совпадает с тем, что заявлен как текущий."""
        path = self.path_for(app_id)
        stored = path / MANIFEST_FILENAME
        if not stored.is_file():
            return False
        digest = hashlib.sha256(stored.read_bytes()).hexdigest()
        record = self._version_record(path, self.current_version(app_id))
        if record.get("manifest_sha256") != digest:
            return False
        return record.get("fingerprint") == manifest.fingerprint()

    def size_bytes(self, app_id: str) -> int:
        path = self.path_for(app_id)
        if not path.is_dir():
            raise PrefixError(f"окружение не найдено: {path}")
        total = 0
        for item in path.rglob("*"):
            if item.is_file():
                total += item.stat().st_size
        return total

    # ----------------------------------------------------------------- внутреннее
    def _write_manifest(self, path: Path, manifest: Manifest) -> None:
        from wcmgr.manifest import dump_toml

        (path / MANIFEST_FILENAME).write_text(dump_toml(manifest), encoding="utf-8")

    def _write_pointer(self, path: Path, tag: str) -> None:
        (path / CURRENT_FILENAME).write_text(tag + "\n", encoding="utf-8")

    def _commit_version(self, path: Path, manifest: Manifest, tag: str, *, previous: str | None = None) -> None:
        if not ID_RE.match(tag or "") and not _TIME_TAG_RE.match(tag or ""):
            raise PrefixError(f"некорректное имя версии: {tag!r}")
        target = path / VERSIONS_DIRNAME / tag
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path / MANIFEST_FILENAME, target / MANIFEST_FILENAME)
        digest = hashlib.sha256((target / MANIFEST_FILENAME).read_bytes()).hexdigest()
        record = {
            "version": tag,
            "fingerprint": manifest.fingerprint(),
            "manifest_sha256": digest,
            "app_id": manifest.id,
            "ts": time.time(),
        }
        if previous:
            record["previous"] = previous
        (target / "version.json").write_text(
            json.dumps(record, sort_keys=True), encoding="utf-8"
        )
        self._write_pointer(path, tag)

    def _version_record(self, path: Path, tag: str) -> dict[str, object]:
        record = path / VERSIONS_DIRNAME / tag / "version.json"
        if not record.is_file():
            raise PrefixError(f"запись версии {tag} повреждена или отсутствует")
        return json.loads(record.read_text(encoding="utf-8"))


_TIME_TAG_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")


def _time_tag(now: float | None = None) -> str:
    stamp = now if now is not None else time.time()
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(stamp))
