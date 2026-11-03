#!/usr/bin/env bash
#
# Сборка .deb-пакетов ByteguruOS из текущего дерева.
#
# Предназначена для запуска ВНУТРИ контейнера debian:trixie (см.
# build/docker/Dockerfile.packages): dpkg-buildpackage, debhelper и wlroots есть
# только там. На Windows этот скрипт остановится с объяснением, а не с падением
# dpkg на первой же строке.
#
# Результат складывается в build/live/config/packages.chroot/ — live-build
# устанавливает оттуда все .deb автоматически, без подключения репозитория.

set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_DIR="${SRC_DIR}/build/live/config/packages.chroot"

if [[ "$(uname -s)" != "Linux" ]]; then
	cat >&2 <<-EOF
		Сборка пакетов требует Linux: нужен dpkg-buildpackage, debhelper и
		binutils. Запустите в контейнере:
		  docker run --rm -v "${PWD}:/work" -w /work byteguru-build:ci \\
		    bash build/scripts/build-packages.sh
	EOF
	exit 3
fi

for tool in dpkg-buildpackage debhelper dh meson ninja pkg-config; do
	command -v "${tool}" >/dev/null 2>&1 || {
		echo "не найден ${tool}: установка пакета не выполнена" >&2
		exit 4
	}
done

mkdir -p "${OUT_DIR}"
rm -f "${OUT_DIR}"/*.deb

BUILD_TMP="${SRC_DIR}/build/.debtmp"
mkdir -p "${BUILD_TMP}"

echo ">>> dpkg-buildpackage"
dpkg-buildpackage -us -uc --build=full \
	--output-directory="${BUILD_TMP}" \
	|| { echo "сборка пакетов не удалась" >&2; exit 5; }

shopt -s nullglob
debs=("${BUILD_TMP}"/*.deb)
if [[ ${#debs[@]} -eq 0 ]]; then
	echo "deb-файлы не созданы" >&2
	exit 6
fi

for deb in "${debs[@]}"; do
	case "${deb}" in
		*_source.deb|*.changes|*.buildinfo) continue ;;
	esac
	cp -f "${deb}" "${OUT_DIR}/"
	echo "  + $(basename "${deb}")"
done

echo ">>> пакеты готовы: $(ls "${OUT_DIR}"/*.deb | wc -l) в ${OUT_DIR}"
echo ">>> live-build установит их автоматически на шаге chroot"
