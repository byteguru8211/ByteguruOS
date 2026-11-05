#!/usr/bin/env bash
#
# build-iso.sh — ЕДИНСТВЕННАЯ точка сборки образа ByteguruOS.
#
#   Вход:  build/live/                   конфиг live-build (auto/config,
#                                         config/package-lists/, config/hooks/,
#                                         config/archives.sample/, config/packages.chroot/)
#          iso/overlay/                  файлы, попадающие в корень live-системы
#          build/configs/kernel.defconfig  ядро — СЕЙЧАС не участвует: первый ISO
#                                         собирается на штатном ядре Debian (см.
#                                         auto/config), конфиг ядра — задел
#   Выход: build/out/byteguruos-<arch>-<дата>+<версия>.iso и .iso.sha256
#
# ПОЧЕМУ ОДНА ТОЧКА ВХОДА. Корневая FS определяется ровно здесь: live-build +
# overlay. Когда проект перейдёт на immutable-образ с dm-verity/A/B, изменится
# только реализация стадии упаковки rootfs, а вызов build-iso.sh, содержимое
# iso/overlay/ и скрипты dual boot останутся прежними.
#
# ВАЖНО ПРО СРЕДУ. Сборка требует Linux и root: live-build монтирует rootfs,
# работает с loop-устройствами и вызывает debootstrap. Локально это Debian 13
# (trixie) — та же suites, из которой собирается образ; в CI — контейнер
# debian:trixie (.github/workflows/iso.yml). Проверка uname и EUID в начале —
# чтобы на неподходящей машине ошибка была объяснением, а не «тихим» падением
# на середине часового процесса.
set -euo pipefail

# --- очистка окружения ---------------------------------------------------------
# Пустой http_proxy= (не отсутствующий, а ПУСТОЙ) — известная ловушка:
# debootstrap вызывает wget, wget видит переменную, пытается соединиться с
# «пустым» прокси и зависает без сообщения. Проверено на реальной сборке:
# bootstrap стоял на скачивании less.deb больше пяти минут при работающем
# напрямую wget. Поэтому пустые значения снимаем, а заданные — бережём.
for _var in http_proxy https_proxy ftp_proxy all_proxy HTTP_PROXY HTTPS_PROXY FTP_PROXY ALL_PROXY; do
	if [ -n "${!_var:-}" ]; then
		continue
	fi
	unset "$_var" 2>/dev/null || true
done
export DEBIAN_FRONTEND=noninteractive

# --- сетевые таймауты ----------------------------------------------------------
# live-build сам скачивает индексы Contents (стадия chroot_firmware) через
# wget, и у этого wget таймаута нет: на одной сборке соединение с зеркалом
# «замёрзло» на 26 минутах без единого байта, и сборка стояла молча.
# apt от такого защищён Acquire::Retries, wget — нет, поэтому задаём ему
# WGETRC: таймаут и повторные попытки вместо вечного ожидания.
WGETRC_FILE="${TMPDIR:-/tmp}/byteguru-wgetrc.$$"
cat > "${WGETRC_FILE}" <<'WGETRC'
timeout = 60
tries = 5
continue = on
WGETRC
export WGETRC="${WGETRC_FILE}"
trap 'rm -f "${WGETRC_FILE}"' EXIT

# --- пути ----------------------------------------------------------------------------
SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
SCRIPT_DIR="$(dirname "${SCRIPT_PATH}")"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd -P)"
LIVE_DIR="${REPO_ROOT}/build/live"
OVERLAY_DIR="${REPO_ROOT}/iso/overlay"
OUT_DIR="${REPO_ROOT}/build/out"
INCLUDES_DIR="${LIVE_DIR}/config/includes.chroot"
DEBOOTSTRAP_SCRIPTS_DIR="/usr/share/debootstrap/scripts"
LOG_PREFIX="build-iso"

# --- режимы ---------------------------------------------------------------------------
DO_CLEAN=0
DO_SHELL=0
# Только имя артефакта: реальная архитектура пинится в auto/config (--architecture),
# иначе имя файла и содержимое образа могли бы разойтись.
ARCH="amd64"
SUITE="?"

usage() {
	cat <<EOF
Использование: sudo bash build/scripts/build-iso.sh [опции] [-- extra-args для lb build]

Собирает загрузочный гибрид ISO (UEFI+BIOS) через live-build (\`lb build\`).
ТРЕБУЕТСЯ Linux и root: live-build монтирует rootfs и squashfs, работает с
loop-устройствами и вызывает debootstrap. Локально это Debian 13 (trixie),
в CI — контейнер debian:trixie (.github/workflows/iso.yml).

Опции:
  -c, --clean         \`lb clean\` + удаление build/out, затем сборка с нуля
  -s, --shell         отладка: \`lb shell\` внутри уже собранного chroot
                      (нужен предыдущий успешный lb build)
  -a, --arch АРХ      только часть имени артефакта (по умолчанию ${ARCH});
                      фактическая архитектура задаётся в build/live/auto/config
  -h, --help          эта справка

Примеры:
  sudo bash build/scripts/build-iso.sh
  sudo bash build/scripts/build-iso.sh --clean
  sudo bash build/scripts/build-iso.sh --shell
EOF
}

log() { printf '[%s] %s\n' "${LOG_PREFIX}" "$*"; }
warn() { printf '[%s] ПРЕДУПРЕЖДЕНИЕ: %s\n' "${LOG_PREFIX}" "$*" >&2; }
die() {
	printf '\n[%s] ОШИБКА: %s\n' "${LOG_PREFIX}" "$*" >&2
	exit 1
}

on_error() {
	local rc=$?
	printf '\n[%s] Сборка прервана (строка %s, код %s).\n' "${LOG_PREFIX}" "${1}" "${rc}" >&2
	printf '[%s] Типичные причины:\n' "${LOG_PREFIX}" >&2
	printf '[%s]   1) не Linux / нет root (см. guard выше);\n' "${LOG_PREFIX}" >&2
	printf '[%s]   2) live-build на хосте не знает опцию из auto/config\n' "${LOG_PREFIX}" >&2
	printf '[%s]      (в Ubuntu 24.04 это 3.0~a57 — сверяйтесь с man lb_config);\n' "${LOG_PREFIX}" >&2
	printf '[%s]   3) debootstrap не знает suite «%s» — см. --fix-debootstrap ниже по логу;\n' "${LOG_PREFIX}" "${SUITE}" >&2
	printf '[%s]   4) падали стадии binary_* (grub-efi/xorriso) — ищите первую\n' "${LOG_PREFIX}" >&2
	printf '[%s]      ошибку в выводе lb build выше.\n' "${LOG_PREFIX}" >&2
	printf '[%s] Рабочий каталог сборки: %s\n' "${LOG_PREFIX}" "${LIVE_DIR}" >&2
	# Код возврата сохраняем: CI обязан видеть падение.
	exit "${rc}"
}

# --- разбор аргументов ----------------------------------------------------------------
LB_EXTRA=()
while [ "$#" -gt 0 ]; do
	case "$1" in
	-c | --clean) DO_CLEAN=1 ;;
	-s | --shell) DO_SHELL=1 ;;
	-a | --arch)
		[ "$#" -ge 2 ] || die "--arch требует значение"
		ARCH="$2"
		shift
		;;
	-h | --help)
		usage
		exit 0
		;;
	--)
		shift
		while [ "$#" -gt 0 ]; do
			LB_EXTRA+=("$1")
			shift
		done
		break
		;;
	*) die "неизвестная опция: $1 (справка: --help)" ;;
	esac
	shift
done

# --- ГЛАВНЫЙ GUARD: НЕ LINUX -----------------------------------------------------------
# Специально до всего остального и максимально разговорчиво: владелец проекта на
# Windows, и тихое «command not found» здесь стоило бы часов разбора.
OS_NAME="$(uname -s)"
if [ "${OS_NAME}" != "Linux" ]; then
	cat >&2 <<EOF

===============================================================================
 [${LOG_PREFIX}] СБОРКА ISO НА ЭТОЙ МАШИНЕ НЕВОЗМОЖНА
===============================================================================
 Обнаружена ОС: ${OS_NAME}
 Требуется:     Linux x86_64 с live-build (ubuntu-latest в GitHub Actions)

 Почему нельзя собрать здесь: hybrid-ISO требует xorriso, isolinux/grub-efi,
 squashfs-tools, loop-устройств и монтирования squashfs. В Windows-среде
 разработки этого нет (ни WSL, ни docker по условию проекта не используется).

 Что делать вместо этого:
   1) Запушить в main (или Actions -> "iso" -> Run workflow) в
      byteguru8211/ByteguruOS — ISO соберёт GitHub Actions на ubuntu-latest.
   2) Скачать артефакт job-а "iso-amd64": .iso + .iso.sha256 (хранятся 30 дней).
   3) Проверить целостность:  sha256sum -c byteguruos-*.iso.sha256
   4) VirtualBox: новая ВМ, Linux 2.x/64-bit, включить UEFI и ВЫКЛЮЧИТЬ
      Secure Boot (первый образ не подписан), подключить ISO как оптический
      диск. Ручной путь установки — /usr/share/doc/byteguruos/INSTALL.txt
      внутри самой live-сессии.

 этот скрипт идемпотентен и сам не устанавливает зависимостей: он проверяет
 окружение и вызывает \`lb config\` + \`lb build\`.
===============================================================================
EOF
	exit 2
fi

# Под ловушкой только Linux-путь.
trap 'on_error "${LINENO}"' ERR

if [ "$(id -u)" -ne 0 ]; then
	die "live-build обязан работать от root (нужны mount и loop-устройства).
        Запустите: sudo -E bash build/scripts/build-iso.sh"
fi

require_cmd() {
	command -v "$1" >/dev/null 2>&1 ||
		die "не найдена обязательная утилита «$1».
        Пакеты для ubuntu-latest перечислены в .github/workflows/iso.yml:
        live-build debootstrap squashfs-tools dosfstools mtools xorriso
        grub-efi-amd64-bin grub-pc-bin grub-common isolinux syslinux
        syslinux-common syslinux-utils curl git binutils dpkg-dev rsync"
}

# rsync из списка убран: overlay копируется cp -a (см. sync_overlay), а
# обязательный, но неиспользуемый инструмент — это лишенняя зависимость сборки.
for c in lb debootstrap xorriso losetup mksquashfs awk sed grep curl find; do
	require_cmd "${c}"
done

[ -d "${LIVE_DIR}" ] || die "нет каталога live-build конфигурации: ${LIVE_DIR}"
[ -f "${LIVE_DIR}/auto/config" ] || die "нет ${LIVE_DIR}/auto/config"

# --- единственный источник истины по suite/зеркалу: auto/config -------------------------
# Значения читаются из конфига, чтобы предпроверки не могли разойтись с реальной
# сборкой (пин релиза должен жить в одном месте).
parse_assign() {
	# $1 = имя переменной, $2 = значение по умолчанию. Читаем
	# VAR="значение" из auto/config, снимая кавычки и хвостовой пробел.
	local name="$1" def="$2" line val
	line="$(grep -m1 -E "^[[:space:]]*${name}=" "${LIVE_DIR}/auto/config" || true)"
	if [ -z "${line}" ]; then
		printf '%s\n' "${def}"
		return 0
	fi
	val="${line#*=}"
	val="${val%\"}"
	val="${val#\"}"
	printf '%s\n' "${val}"
}

SUITE="$(parse_assign BYTEGURU_SUITE trixie)"
MIRROR="$(parse_assign BYTEGURU_MIRROR http://deb.debian.org/debian/)"
log "целевая suite: ${SUITE}    зеркало: ${MIRROR}"

# --- предпроверка 1: debootstrap должен знать suite --------------------------------------
# Ubuntu 24.04 несёт debootstrap 1.0.134; поддержка trixie появилась позже. Без
# скрипта /usr/share/debootstrap/scripts/<suite> бутстрап падает невнятно.
# Стандартный приём Debian — симлинк на универсальный скрипт stretch (на него
# свежие debootstrap ссылают buster/bullseye/bookworm/trixie).
if [ ! -e "${DEBOOTSTRAP_SCRIPTS_DIR}/${SUITE}" ]; then
	warn "debootstrap не знает suite «${SUITE}» (нет скрипта в ${DEBOOTSTRAP_SCRIPTS_DIR})"
	if [ -e "${DEBOOTSTRAP_SCRIPTS_DIR}/stretch" ]; then
		log "создаю симлинк ${SUITE} -> stretch (известный приём для свежих релизов)"
		ln -sf stretch "${DEBOOTSTRAP_SCRIPTS_DIR}/${SUITE}"
	else
		die "нет ни скрипта ${SUITE}, ни stretch. Обновите debootstrap:
            sudo apt-get install -y --only-upgrade debootstrap"
	fi
fi

# --- предпроверка 2: suite реально доступен на зеркале -----------------------------------
# Дешёвая проверка, спасает от «Debian заморозил/переименовал suite» через 20 минут
# на бутстрапе. Различает два отказа: 404 — suite действительно нет, и это
# конфигурация; всё остальное — сеть, а один сбой сети не должен обрывать
# сборку (проверка падала именно так, когда зеркало отвечало через минуту).
release_url="${MIRROR}dists/${SUITE}/Release"
tries=0
while : ; do
	code="$(curl -sI --max-time 25 -o /dev/null -w '%{http_code}' "${release_url}" 2>/dev/null || true)"
	case "${code}" in
		200 | 304 | 301 | 302)
			break ;;
		403 | 404 | 410)
			die "зеркало отвечает ${code} на dists/${SUITE}/Release — suite «${SUITE}» там нет
        (заморожен/переименован). Измените BYTEGURU_SUITE в
        ${LIVE_DIR}/auto/config — и больше нигде." ;;
		*)
			tries=$((tries + 1))
			if [ "${tries}" -ge 3 ]; then
				die "зеркало ${MIRROR} недоступно (последний ответ: ${code:-пусто}) после ${tries} попыток.
        Это сеть, а не состав архива: проверьте соединение и повторите."
			fi
			log "зеркало не ответило (${code:-пусто}), попытка ${tries} из 3…"
			sleep 10 ;;
	esac
done
log "suite ${SUITE} доступен на зеркале"

# --- предпроверка 3: ключи архива ---------------------------------------------------------
if [ ! -f /usr/share/keyrings/debian-archive-keyring.gpg ]; then
	warn "нет /usr/share/keyrings/debian-archive-keyring.gpg — бутстрап может упасть"
	warn "на проверке подписи Release: sudo apt-get install -y debian-archive-keyring"
fi

# --- предпроверка 4: loop + squashfs ------------------------------------------------------
# Нужны для стадии chroot/binary: live-build монтирует rootfs и squashfs.
modprobe loop 2>/dev/null || true
modprobe squashfs 2>/dev/null || true
if ! losetup -f >/dev/null 2>&1; then
	die "нет свободных loop-устройств (losetup -f). Требуется ядро хоста с BLK_DEV_LOOP."
fi
if ! grep -qw squashfs /proc/filesystems; then
	die "ядро хоста не поддерживает squashfs — rootfs live-образа не собрать и не
        примонтировать. Требуется SQUASHFS=y/m на сборочной машине."
fi

# --- синхронизация overlay ------------------------------------------------------------------
# Единственный источник файлов образа — iso/overlay/ (см. начало файла). live-build
# читает их из config/includes.chroot/, поэтому перед сборкой делаем точную копию.
# Полная пересборка каталога (rm -rf + копия), а не инкрементальный rsync:
# так файл, удалённый из iso/overlay/, гарантированно не переживёт сборку.
sync_overlay() {
	[ -d "${OVERLAY_DIR}" ] || die "нет каталога overlay: ${OVERLAY_DIR}"
	rm -rf "${INCLUDES_DIR}"
	mkdir -p "${INCLUDES_DIR}"
	# cp, а не rsync: каталог пересоздаётся целиком, поэтому инкрементальность
	# rsync здесь не нужна, а сам он умеет зависать на сокетах (в живой сборке
	# 2026-11-05 rsync -a на 176 КБ простоял 49 минут и не завершился).
	cp -a "${OVERLAY_DIR}/." "${INCLUDES_DIR}/"
	# Git на Windows не восстанавливает бит +x (core.filemode=false), а live-build
	# требует исполняемости auto/config и hooks/*: без этого стадия молча не
	# запускается. Права выставляем здесь, а не в CI-шаге.
	chmod 0755 "${LIVE_DIR}/auto/config" 2>/dev/null || true
	# live-build (trixie: 20250505) ищет хуки в config/hooks/normal/*.chroot, а не
	# в config/hooks/ —Flat-раскладка молча игнорируется. Обход по всему дереву,
	# чтобы хук не потерялся при следующем переезде формата.
	if [ -d "${LIVE_DIR}/config/hooks" ]; then
		find "${LIVE_DIR}/config/hooks" -type f -name '*.chroot' -exec chmod 0755 {} + 2>/dev/null || true
		find "${LIVE_DIR}/config/hooks" -type f -name '*.binary' -exec chmod 0755 {} + 2>/dev/null || true
	fi
	if [ -d "${INCLUDES_DIR}/usr/local" ]; then
		find "${INCLUDES_DIR}/usr/local" -type f -exec chmod 0755 {} + 2>/dev/null || true
	fi
	# Конфиги/юниты — 0644, как у Debian-пакетов (world-readable, но не исполняемые).
	if [ -d "${INCLUDES_DIR}/etc" ]; then
		find "${INCLUDES_DIR}/etc" -type f -exec chmod 0644 {} + 2>/dev/null || true
	fi
	chmod 0644 "${INCLUDES_DIR}/usr/share/doc/byteguruos/INSTALL.txt" 2>/dev/null || true
	log "overlay синхронизирован: ${OVERLAY_DIR} -> ${INCLUDES_DIR}"
}

# --- проверка парность .list/.key в config/archives -----------------------------------------
# Активный список репозитория без парного ключа = небезопасный источник или падение
# live-build. Шаблоны намеренно живут в config/archives.sample/ (вне сканирования).
check_archives_pairs() {
	local dir="${LIVE_DIR}/config/archives"
	[ -d "${dir}" ] || return 0
	local f base
	for f in "${dir}"/*.list*; do
		[ -e "${f}" ] || continue
		base="$(basename "${f}")"
		base="${base%%.list*}"
		if ! ls "${dir}/${base}".key* >/dev/null 2>&1; then
			warn "в config/archives есть «${base}.list…» без парного «${base}.key…» —"
			warn "  live-build не сможет проверить подпись; переносите оба файла сразу"
		fi
	done
	return 0
}

do_clean() {
	log "lb clean: удаление стадий…"
	(cd "${LIVE_DIR}" && lb clean) || warn "lb clean вернул ошибку — сносим артефакты руками"
	rm -f "${LIVE_DIR}"/*.iso "${LIVE_DIR}"/*.hybrid.iso 2>/dev/null || true
	rm -rf "${LIVE_DIR}/binary" "${LIVE_DIR}/binary_"* "${LIVE_DIR}/chroot" \
		"${LIVE_DIR}/archive" "${LIVE_DIR}/cache" "${INCLUDES_DIR}" 2>/dev/null || true
	# Отметки стадий — то, из-за чего «чистая» сборка не бывает чистой. lb clean
	# снимает их сам, но при прерванной сборке (питание, Ctrl+C, упавший
	# xorriso) они остаются, и следующий прогон пропускает стадию, чей результат
	# уже удалён: так было получено «chroot без ядра» на cp chroot/boot/vmlinuz-*.
	rm -rf "${LIVE_DIR}/.build" "${LIVE_DIR}/.lock" 2>/dev/null || true
	rm -rf "${OUT_DIR}" 2>/dev/null || true
	log "очистка завершена"
}

publish_iso() {
	local found version stamp name sha size
	# live-build кладёт готовый образ в корень своего каталога, но точное имя
	# зависит от версии (обычно live-image-<arch>.hybrid.iso), поэтому ищем
	# самый свежий .iso, а не угадываем имя.
	found="$(find "${LIVE_DIR}" -maxdepth 1 -type f -name '*.iso' -printf '%T@ %p\n' 2>/dev/null |
		sort -rn | head -n1 | cut -d' ' -f2- || true)"
	if [ -z "${found}" ] || [ ! -f "${found}" ]; then
		die "lb build завершился без ошибки, но ISO в ${LIVE_DIR} не найден.
        Первая ошибка в выводе lb build выше — почти всегда это binary_* (grub-efi
        или xorriso)."
	fi
	mkdir -p "${OUT_DIR}"
	# Версия из git (тег/хэш); в пустом репозитории или без тего — fallback на
	# short HEAD, а если git недоступен — "local".
	version="$(git -C "${REPO_ROOT}" describe --tags --always --dirty 2>/dev/null || true)"
	[ -n "${version}" ] || version="$(git -C "${REPO_ROOT}" rev-parse --short HEAD 2>/dev/null || true)"
	[ -n "${version}" ] || version="local"
	# В имя артефакта не пропускаем ничего, кроме безопасных символов: имя
	# попадает в artifacts и в имена файлов на Windows.
	version="$(printf '%s' "${version}" | tr -c 'A-Za-z0-9._+' '-' | cut -c1-32)"
	stamp="$(date -u +%Y%m%d)"
	name="byteguruos-${ARCH}-${stamp}+${version}.iso"
	install -m 0644 "${found}" "${OUT_DIR}/${name}"
	sha="$(sha256sum "${OUT_DIR}/${name}" | awk '{ print $1 }')"
	# Формат, пригодный для `sha256sum -c` прямо в этом каталоге.
	printf '%s  %s\n' "${sha}" "${name}" >"${OUT_DIR}/${name}.sha256"
	size="$(du -h "${OUT_DIR}/${name}" | awk '{ print $1 }')"

	printf '\n==== ИТОГ СБОРКИ ============================================\n'
	printf 'ISO:    %s\n' "${OUT_DIR}/${name}"
	printf 'РАЗМЕР: %s\n' "${size}"
	printf 'SHA256: %s\n' "${sha}"
	printf 'ПРОВЕРКА: (cd %s && sha256sum -c %s.sha256)\n' "${OUT_DIR}" "${name}"
	printf '=============================================================\n'
}

# --- основной путь --------------------------------------------------------------------------
if [ "${DO_CLEAN}" -eq 1 ]; then
	do_clean
fi

if [ "${DO_SHELL}" -eq 1 ]; then
	[ -d "${LIVE_DIR}/chroot" ] ||
		die "--shell требует уже собранного chroot: сначала соберите образ без --shell"
	log "вход в chroot для отладки (lb shell); выход — exit"
	cd "${LIVE_DIR}"
	exec lb shell
fi

sync_overlay
check_archives_pairs

log "lb config…"
cd "${LIVE_DIR}"
lb config

log "lb build: бутстрап Debian ${SUITE}, установка пакетов, squashfs, гибрид ISO…"
log "это долгая стадия (десятки минут на runner) — CI timeout задан с запасом"
if [ "${#LB_EXTRA[@]}" -gt 0 ]; then
	lb build "${LB_EXTRA[@]}"
else
	lb build
fi

publish_iso
log "Сборка завершена успешно."
exit 0
