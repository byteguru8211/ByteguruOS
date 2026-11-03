#!/usr/bin/env python3
"""Проверка имён пакетов из build/live/config/package-lists по индексу архива.

Зачем: опечатка в имени пакета (policykit-1 в trixie больше нет) роняет
lb chroot_install-packages через 20+ минут прогона. Эта проверка стоит ~40 секунд
и ловит то же самое до сборки.

Источник истины — Packages.gz нужного suite, а не HTML-страницы packages.debian.org:
страницы при rate limit отдают разный ответ для одного и того же имени.
"""

from __future__ import annotations

import argparse
import gzip
import io
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

COMPONENTS = ("main", "contrib", "non-free", "non-free-firmware")
NAME_RE = re.compile(r"^Package: (\S+)", re.M)


def fetch_index(suite: str, arch: str, cache: Path) -> set[str]:
    names: set[str] = set()
    for component in COMPONENTS:
        target = cache / f"{suite}-{component}-{arch}.Packages.gz"
        if not target.is_file():
            url = (
                f"https://deb.debian.org/debian/dists/{suite}/{component}/"
                f"binary-{arch}/Packages.gz"
            )
            print(f"  загрузка {component} ...", flush=True)
            try:
                with urllib.request.urlopen(url, timeout=120) as response:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(response.read())
            except (urllib.error.URLError, OSError) as exc:  # noqa: PERF203
                print(f"  не удалось скачать {url}: {exc}", file=sys.stderr)
                raise SystemExit(3) from exc
        with gzip.open(target, "rt", encoding="utf-8", errors="replace") as stream:
            names.update(match.group(1) for match in NAME_RE.finditer(stream.read()))
    return names


def declared_packages(lists_dir: Path) -> dict[str, list[str]]:
    """Имя пакета → файлы, где оно встречается. Комментарии и пустые строки пропускаются."""
    result: dict[str, list[str]] = {}
    for path in sorted(lists_dir.glob("*.list*.chroot")):
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            for token in re.split(r"\s+", line):
                if token.startswith("!"):        # конструктивный элемент формата списков
                    continue
                result.setdefault(token, []).append(path.name)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", default="trixie")
    parser.add_argument("--arch", default="amd64")
    parser.add_argument("--lists", default="build/live/config/package-lists")
    parser.add_argument("--cache", default=".cache/debian-index")
    args = parser.parse_args(argv)

    lists_dir = Path(args.lists)
    if not lists_dir.is_dir():
        print(f"каталог списков не найден: {lists_dir}", file=sys.stderr)
        return 2

    declared = declared_packages(lists_dir)
    print(f"имён в package-lists: {len(declared)}")
    available = fetch_index(args.suite, args.arch, Path(args.cache))
    print(f"имён в индексе {args.suite}/{args.arch}: {len(available)}")

    unknown = {name: files for name, files in declared.items() if name not in available}
    if unknown:
        for name, files in sorted(unknown.items()):
            near = sorted(other for other in available if other.startswith(name[:6]))[:4]
            print(f"НЕТ в архиве: {name} (файл: {', '.join(files)})")
            if near:
                print(f"    рядомстоящие: {' '.join(near)}")
        return 1

    print("все имена пакетов существуют в архиве")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
