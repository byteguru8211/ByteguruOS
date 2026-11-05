#!/usr/bin/env bash
# Снимки рабочего стола из headless-сеанса: чем проверяется «красиво и работает».
#
# README и ARCHITECTURE утверждают, что интерфейс проверен снимками. Без
# скрипта это утверждение нельзя ни повторить, ни оспорить: правки композитора
# видно только на картинке (янтарный заголовок, игнорируемая exclusive-зона
# панели и окна «вполовину непрозрачные» — все три найдены именно так).
#
# Использование:
#   build/scripts/desktop-shot.sh [папка для png]
# Внутри — тот же headless-прогон, что и в тестах: WLR_BACKENDS=headless,
# pixman-рендер, grim. Кадр делается со стороны хоста, поэтому композитор
# живёт ровно столько, сколько нужно на снимок.
#
# Требует: bgwl (ninja -C build-c), grim, foot, python3-gi + GTK 4.

set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OUT_DIR="${1:-/tmp/byteguru-desktop-shots}"
BGWL="${BGWL:-${REPO_ROOT}/build-c/src/compositor/bgwl}"

[ -x "${BGWL}" ] || { echo "нет собранного композитора: ${BGWL} (ninja -C build-c)" >&2; exit 1; }
command -v grim >/dev/null || { echo "нужен grim: apt-get install grim" >&2; exit 1; }

mkdir -p "${OUT_DIR}"
export XDG_RUNTIME_DIR="${OUT_DIR}/runtime"
rm -rf "${XDG_RUNTIME_DIR}"; mkdir -p "${XDG_RUNTIME_DIR}"; chmod 700 "${XDG_RUNTIME_DIR}"
export WLR_BACKENDS=headless WLR_RENDERER=pixman
export PYTHONPATH="${REPO_ROOT}/src/shell:${REPO_ROOT}/src/bytegurud:${REPO_ROOT}/src/installer:${REPO_ROOT}/src/wincompat"
export PATH="${REPO_ROOT}/bin:${PATH}" WAYLAND_DISPLAY=wayland-0
unset DISPLAY

# Каждый кадр — отдельный сеанс: общий композитор на все случаи означал бы,
# что падение одного кадра утаскивает остальные, и непонятно, какой именно
# интерфейс сломался.
shoot() { # shoot <имя файла> <команда внутри сеанса> <секунд до кадра>
	local name="$1" command="$2" delay="${3:-4}"
	local log="${OUT_DIR}/${name}.log"
	"${BGWL}" -b headless -c "${command} & sleep 25" >"${log}" 2>&1 &
	local bgwl_pid=$!
	sleep "${delay}"
	grim "${OUT_DIR}/${name}.png" 2>/dev/null || echo "  ! grim не снял ${name}"
	kill "${bgwl_pid}" 2>/dev/null
	pkill -x bgpanel; pkill -x bgline; pkill -x bginstaller; pkill -x foot
	sleep 0.5
	if grep -qaE 'Traceback|rror' "${log}"; then
		echo "  ! в логе ${name} есть ошибки:"
		tr -d '\000' < "${log}" | grep -aE 'Traceback|rror' | head -4 | sed 's/^/      /'
	fi
	echo "  + ${OUT_DIR}/${name}.png"
}

echo ">>> кадры рабочего стола в ${OUT_DIR}"
shoot panel			"bgpanel & foot --title=окно"
shoot palette		"bgpanel & bgline"
shoot apps			"bglauncher"
shoot settings	"bgsettings"
shoot firstboot		"bgfirstboot"
shoot installer		"bginstaller --gui"
echo ">>> готово; кадры читаются как картинки — именно так они и проверяются"
