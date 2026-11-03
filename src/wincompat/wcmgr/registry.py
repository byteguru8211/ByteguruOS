"""Реестр совместимости ByteguruOS.

Манифест утверждает ожидаемый уровень, но верим мы только прогонам. Поэтому
уровень «подтверждённый» и уровень «заявленный» хранятся раздельно, а публичный
вывод всегда говорит, на основании чего он сделан. Выдумать себе L1 здесь
нельзя: его может дать только записанный удачный прогон.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from wcmgr.errors import RegistryError
from wcmgr.manifest import CoverageLevel, Manifest, Runner

SCHEMA_VERSION = 1
_LEVEL_ORDER = {
    CoverageLevel.L0: 0,
    CoverageLevel.L2: 1,
    CoverageLevel.L1: 2,
}
# Для L3/L4 «успех» означает не работу в Wine, а подтверждённый обходной путь.
_BYPASS_LEVELS = (CoverageLevel.L3, CoverageLevel.L4)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS apps (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    version TEXT NOT NULL,
    publisher TEXT NOT NULL DEFAULT '',
    runner TEXT NOT NULL,
    expected_level TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    manifest_json TEXT NOT NULL,
    registered_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    app_id TEXT NOT NULL,
    ok INTEGER NOT NULL,
    level TEXT NOT NULL,
    build TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL,
    FOREIGN KEY (app_id) REFERENCES apps (id)
);
CREATE TABLE IF NOT EXISTS bypass (
    app_id TEXT PRIMARY KEY,
    level TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL,
    FOREIGN KEY (app_id) REFERENCES apps (id)
);
CREATE INDEX IF NOT EXISTS runs_app_idx ON runs (app_id, ts);
"""


@dataclass(frozen=True)
class RunRecord:
    app_id: str
    ok: bool
    level: CoverageLevel
    detail: str = ""
    build: str = ""
    ts: float = 0.0


@dataclass(frozen=True)
class AppEntry:
    manifest: Manifest
    expected_level: CoverageLevel
    proven_level: CoverageLevel | None
    runs_total: int
    runs_ok: int
    last_run_ts: float
    bypass_level: CoverageLevel | None

    @property
    def published_level(self) -> CoverageLevel:
        """Что показывать пользователю: подтверждённый уровень либо заявленный L0-без-доказательств."""
        return self.proven_level or self.expected_level

    @property
    def evidence(self) -> str:
        if self.proven_level is None:
            return "не проверено на этой сборке"
        return f"подтверждено прогонами ({self.runs_ok}/{self.runs_total})"


class CompatibilityRegistry:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(self.path))
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        stored = self._db.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone()
        if stored is None:
            self._db.execute(
                "INSERT INTO meta (key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),)
            )
            self._db.commit()
        elif int(stored["value"]) != SCHEMA_VERSION:
            raise RegistryError(
                f"реестр создан верчением схемы {stored['value']}, а программа ожидает {SCHEMA_VERSION}"
            )

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> "CompatibilityRegistry":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ запись
    def register(self, manifest: Manifest, *, now: float | None = None) -> bool:
        """Класть манифест в реестр. True, если запись новая или изменилась."""
        stamp = now if now is not None else time.time()
        existing = self._db.execute("SELECT fingerprint FROM apps WHERE id = ?", (manifest.id,)).fetchone()
        payload = json.dumps(manifest.to_nested(), sort_keys=True, ensure_ascii=False)
        if existing is None:
            self._db.execute(
                "INSERT INTO apps (id, name, version, publisher, runner, expected_level,"
                " fingerprint, manifest_json, registered_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    manifest.id,
                    manifest.name,
                    manifest.version,
                    manifest.publisher,
                    manifest.runner.value,
                    manifest.expected_level.value,
                    manifest.fingerprint(),
                    payload,
                    stamp,
                    stamp,
                ),
            )
            self._db.commit()
            return True
        if existing["fingerprint"] == manifest.fingerprint():
            return False
        self._db.execute(
            "UPDATE apps SET name = ?, version = ?, publisher = ?, runner = ?, expected_level = ?,"
            " fingerprint = ?, manifest_json = ?, updated_at = ? WHERE id = ?",
            (
                manifest.name,
                manifest.version,
                manifest.publisher,
                manifest.runner.value,
                manifest.expected_level.value,
                manifest.fingerprint(),
                payload,
                stamp,
                manifest.id,
            ),
        )
        self._db.commit()
        return True

    def record_run(self, record: RunRecord) -> None:
        if not self._db.execute("SELECT 1 FROM apps WHERE id = ?", (record.app_id,)).fetchone():
            raise RegistryError(f"приложение {record.app_id!r} не зарегистрировано в реестре")
        self._db.execute(
            "INSERT INTO runs (app_id, ok, level, build, detail, ts) VALUES (?, ?, ?, ?, ?, ?)",
            (
                record.app_id,
                1 if record.ok else 0,
                record.level.value,
                record.build,
                record.detail,
                record.ts if record.ts else time.time(),
            ),
        )
        self._db.commit()

    def note_bypass(self, app_id: str, level: CoverageLevel, note: str = "") -> None:
        """Зафиксировать, что приложение работает только обходным путём (L3/L4)."""
        if level not in _BYPASS_LEVELS:
            raise RegistryError("обходным путём может быть только L3 или L4")
        if not self._db.execute("SELECT 1 FROM apps WHERE id = ?", (app_id,)).fetchone():
            raise RegistryError(f"приложение {app_id!r} не зарегистрировано в реестре")
        self._db.execute(
            "INSERT INTO bypass (app_id, level, note, ts) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(app_id) DO UPDATE SET level = excluded.level, note = excluded.note,"
            " ts = excluded.ts",
            (app_id, level.value, note, time.time()),
        )
        self._db.commit()

    # ------------------------------------------------------------------ чтение
    def get(self, app_id: str) -> AppEntry | None:
        row = self._db.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
        if row is None:
            return None
        stats = self._db.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(ok), 0) AS ok, MAX(ts) AS last"
            " FROM runs WHERE app_id = ?",
            (app_id,),
        ).fetchone()
        bypass = self._db.execute("SELECT level FROM bypass WHERE app_id = ?", (app_id,)).fetchone()
        return self._entry(row, stats, bypass)

    def entries(self) -> list[AppEntry]:
        rows = self._db.execute("SELECT * FROM apps ORDER BY name COLLATE NOCASE").fetchall()
        result = []
        for row in rows:
            stats = self._db.execute(
                "SELECT COUNT(*) AS total, COALESCE(SUM(ok), 0) AS ok, MAX(ts) AS last"
                " FROM runs WHERE app_id = ?",
                (row["id"],),
            ).fetchone()
            bypass = self._db.execute(
                "SELECT level FROM bypass WHERE app_id = ?", (row["id"],)
            ).fetchone()
            result.append(self._entry(row, stats, bypass))
        return result

    def proven_level(self, app_id: str) -> CoverageLevel | None:
        entry = self.get(app_id)
        return entry.proven_level if entry else None

    def summary(self) -> dict[str, int]:
        counts = {level.value: 0 for level in CoverageLevel}
        for entry in self.entries():
            counts[entry.published_level.value] += 1
        counts["total"] = sum(counts[level.value] for level in CoverageLevel)
        counts["unproven"] = sum(1 for entry in self.entries() if entry.proven_level is None)
        return counts

    def export_json(self) -> str:
        payload = []
        for entry in self.entries():
            payload.append(
                {
                    "id": entry.manifest.id,
                    "name": entry.manifest.name,
                    "version": entry.manifest.version,
                    "expected_level": entry.expected_level.value,
                    "proven_level": entry.proven_level.value if entry.proven_level else None,
                    "bypass_level": entry.bypass_level.value if entry.bypass_level else None,
                    "published_level": entry.published_level.value,
                    "evidence": entry.evidence,
                    "runs_total": entry.runs_total,
                    "runs_ok": entry.runs_ok,
                    "runner": entry.manifest.runner.value,
                    "fingerprint": entry.manifest.fingerprint(),
                }
            )
        return json.dumps({"schema": SCHEMA_VERSION, "apps": payload}, ensure_ascii=False, indent=2)

    # ----------------------------------------------------------------- внутреннее
    def _entry(
        self, row: sqlite3.Row, stats: sqlite3.Row | None, bypass: sqlite3.Row | None
    ) -> AppEntry:
        manifest = Manifest.from_nested(json.loads(row["manifest_json"]))
        proven = self._proven_from_stats(
            row["id"], int(stats["total"] or 0), int(stats["ok"] or 0), row["expected_level"]
        )
        return AppEntry(
            manifest=manifest,
            expected_level=CoverageLevel(row["expected_level"]),
            proven_level=proven,
            runs_total=int(stats["total"] or 0),
            runs_ok=int(stats["ok"] or 0),
            last_run_ts=float(stats["last"] or 0.0),
            bypass_level=CoverageLevel(bypass["level"]) if bypass else None,
        )

    def _proven_from_stats(
        self, app_id: str, total: int, ok: int, expected_level: str
    ) -> CoverageLevel | None:
        if total == 0:
            return None
        last_ok = self._db.execute(
            "SELECT ok, level FROM runs WHERE app_id = ? ORDER BY ts DESC, id DESC LIMIT 1",
            (app_id,),
        ).fetchone()
        best = self._db.execute(
            "SELECT level FROM runs WHERE app_id = ? AND ok = 1 ORDER BY"
            " CASE level WHEN 'l1' THEN 2 WHEN 'l2' THEN 1 ELSE 0 END DESC LIMIT 1",
            (app_id,),
        ).fetchone()
        if last_ok is None:
            return None
        if int(last_ok["ok"]) == 0 and best is None:
            return CoverageLevel.L0
        if best is None:
            return CoverageLevel.L0
        level = CoverageLevel(best["level"])
        # Прогон «с обходным путём» не доказывает работу Wine-слоя.
        if level not in _LEVEL_ORDER:
            return None
        declared = CoverageLevel(expected_level)
        if _LEVEL_ORDER[level] < _LEVEL_ORDER.get(declared, 0):
            return level
        return level
