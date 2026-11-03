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

# Проверяем только исполняемые файлы: debhelper — имя пакета, команды с таким
# именем нет, и проверка «есть ли debhelper» давала бы ложный отказ.
for tool in dpkg-buildpackage dh meson ninja pkg-config; do
	command -v "${tool}" >/dev/null 2>&1 || {
		echo "не найден ${tool}: установка пакета не выполнена" >&2
		exit 4
	}
done

mkdir -p "${OUT_DIR}"
rm -f "${OUT_DIR}"/*.deb

# Композитор — единственный C-компонент дерева, и он ни разу не компилировался.
# Его ошибка не должна блокировать образ, в котором есть оболочка, WinCompat и
# установщик: для прогона образа ставьте BG_SKIP_COMPOSITOR=1.
BUILD_ARGS=(--build=full)
if [[ "${BG_SKIP_COMPOSITOR:-0}" == "1" ]]; then
	echo ">>> композитор bgwl пропускается (BG_SKIP_COMPOSITOR=1)"
	export BG_SKIP_COMPOSITOR=1
fi

# У dpkg-buildpackage нет опции «каталог вывода»: результаты пишутся в каталог,
# родительский по отношению к дереву исходников. Поэтому дерево копируется в
# staging, а .deb забираются из ${STAGE}/.
STAGE="${SRC_DIR}/build/.debwork"
rm -rf "${STAGE}"
mkdir -p "${STAGE}/byteguru"
tar -C "${SRC_DIR}" \
	--exclude='./.git' --exclude='./build' --exclude='./.venv' \
	--exclude='./output' --exclude='*__pycache__*' \
	-cf - . | tar -C "${STAGE}/byteguru" -xf -

LOG="${STAGE}/build-packages.log"
: > "${LOG}"

echo ">>> dpkg-buildpackage (лог: ${LOG})"
if ! (cd "${STAGE}/byteguru" && dpkg-buildpackage -us -uc "${BUILD_ARGS[@]}") 2>&1 | tee "${LOG}"; then
	rc="${PIPESTATUS[0]}"
	echo "сборка пакетов не удалась (код ${rc}); последние строки лога:" >&2
	tail -n 40 "${LOG}" >&2
	# Хвост ошибки уходит в аннотацию прогона: лог GitHub отдаёт только по
	# авторизации, а аннотации видны на публичной странице — иначе первый сбой
	# нечем разобрать без токена.
	tail_line="$(tail -n 8 "${LOG}" | tr '\n' ' ' | sed -e 's/%/%25/g' -e 's/|/%7C/g' | cut -c1-700)"
	echo "::error::dpkg-buildpackage, код ${rc}: ${tail_line}"
	exit 5
fi

shopt -s nullglob
debs=("${STAGE}"/*.deb)
if [[ ${#debs[@]} -eq 0 ]]; then
	echo "deb-файлы не созданы" >&2
	echo "::error::dpkg-buildpackage завершился без ошибки, но .deb не появились" >&2
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
