#!/usr/bin/env bash
#
# Проверка, что собранный live-rootfs пригоден для входа.
#
# Зачем отдельный скрипт: лог прогона GitHub доступен только по авторизации, а
# вывод этой проверки уходит в аннотации и в job summary — иначе причина «опять
# просит пароль» разбирается гаданием. Скрипт исполняется после lb build, пока
# build/live/chroot ещё не удалён.

set -uo pipefail

CHROOT="${1:-build/live/chroot}"
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/null}"
rc=0

note() { echo "$@" | tee -a "${SUMMARY}"; }
fail() {
	note "- **$1**"
	echo "::error::$1"
	rc=1
}

note "## Проверка входа в live-образ"

# Имя live-пользователя берётся из того же источника, что и хук, который его
# создаёт (0200-byteguru-live-user.chroot). Хардкод «user» пережил смену имени
# на byteguru и докладывал «учётной записи нет» про полностью рабочий образ —
# проверка, которая врёт при исправном объекте, хуже отсутствующей.
LIVE_USER="${BG_LIVE_USER:-byteguru}"

if [ ! -d "${CHROOT}" ]; then
	fail "нет каталога ${CHROOT}: стадия chroot не оставила rootfs для проверки"
	exit 1
fi

PASSWD_LINE="$(grep -E "^${LIVE_USER}:" "${CHROOT}/etc/passwd" 2>/dev/null || true)"
if [ -z "${PASSWD_LINE}" ]; then
	fail "в /etc/passwd нет учётной записи ${LIVE_USER}"
else
	note "- учётная запись: \`${PASSWD_LINE}\`"
	SHELL_FIELD="$(printf '%s' "${PASSWD_LINE}" | cut -d: -f7)"
	if [ "${SHELL_FIELD}" != "/bin/bash" ]; then
		# agetty --autologin не проходит через PAM-проверку shell, но bash
		# нужен: именно .bash_profile запускает сеанс.
		fail "shell пользователя — ${SHELL_FIELD}, а не /bin/bash"
	fi
fi

# /etc/shadow имеет режим 0640 root:shadow. Без root grep молча вернул бы пустоту,
# и проверка доложила бы «пароля нет» там, где просто нет доступа — ровно так и
# вышло в первом прогоне. Эти два случая теперь различаются явно.
if [ ! -r "${CHROOT}/etc/shadow" ]; then
	note "- /etc/shadow: **не читается без root** — шаг CI обязан идти под sudo"
	echo "::warning::нет прав на ${CHROOT}/etc/shadow: вывод про пароль неинформативен"
else
	SHADOW_LINE="$(grep -E "^${LIVE_USER}:" "${CHROOT}/etc/shadow" 2>/dev/null || true)"
	SHADOW_HASH="$(printf '%s' "${SHADOW_LINE}" | cut -d: -f2)"
	case "${SHADOW_HASH}" in
		'' | '*'* | '!'*)
			# agetty --autologin входит без пароля, так что сеанс поднимется и
			# здесь. Но без хэша не работают ручной вход на другой tty, su и
			# sudo с запросом пароля — то есть ровно то, что проверяет человек,
			# когда «что-то пошло не так».
			fail "в /etc/shadow нет рабочего хэша пароля для ${LIVE_USER} (su, sudo и ручной вход не сработают)"
			;;
		*) note "- пароль ${LIVE_USER}: хэш установлен (${SHADOW_HASH:0:3}...)" ;;
	esac
fi

# Достаточно ОДНОГО механизма автологина. Требовать оба — значит валить сборку
# при исправном образе: lightdm из образа убран намеренно (свой сеанс запускает
# getty), и проверка это пережила.
AUTOLOGIN_FOUND=""
for conf in \
	etc/systemd/system/getty@tty1.service.d/50-byteguru-autologin.conf \
	etc/lightdm/lightdm.conf.d/50-byteguru-autologin.conf; do
	if [ -f "${CHROOT}/${conf}" ]; then
		note "- автологин: ${conf}"
		AUTOLOGIN_FOUND="yes"
	fi
done
if [ -z "${AUTOLOGIN_FOUND}" ]; then
	fail "нет ни getty-автологина, ни lightdm-конфига: на tty1 будет приглашение входа"
fi

# Локаль и раскладка: печатаем содержимое, а не только вердикт. По одному
# «не найдено» невозможно отличить не отработавший хук от файла, перезаписанного
# более поздней стадией live-build.
if [ -f "${CHROOT}/etc/default/locale" ]; then
	note "- \`/etc/default/locale\`: \`$(head -n 2 "${CHROOT}/etc/default/locale" | tr '\n' ' ')\`"
	grep -q 'ru_RU' "${CHROOT}/etc/default/locale" ||
		note "- локаль: **ru_RU отсутствует** — хук 1100 не применил LANG (его перезаписывает 1020-create-locales-files из live-build)"
else
	note "- локаль: **/etc/default/locale отсутствует** — хук 1100 не отработал"
fi

if [ -f "${CHROOT}/etc/default/keyboard" ]; then
	# Только строки вида KEY=value. Ранее файл печатался целиком через
	# tr '\n' ' ', и в отчёте CI одна «строка» на 1800 символов с комментариями
	# скрывала ровно то, ради чего строка и нужна, — XKBLAYOUT.
	KEYS="$(grep -E '^[A-Z_]+=' "${CHROOT}/etc/default/keyboard" | tr '\n' ' ')"
	note "- \`/etc/default/keyboard\`: \`${KEYS}\`"
fi

SESSIONS="$(ls "${CHROOT}/usr/share/xsessions" "${CHROOT}/usr/share/wayland-sessions" 2>/dev/null | tr '\n' ' ')"
note "- доступные сессии: ${SESSIONS:-**нет ни одной**}"
[ -n "${SESSIONS}" ] || fail "в образе нет ни xsessions, ни wayland-sessions"

DM_TARGET="$(readlink "${CHROOT}/etc/systemd/system/display-manager.service" 2>/dev/null || true)"
note "- display-manager: ${DM_TARGET:-не включён}"

OUR_BINS=""
for bin in "${CHROOT}"/usr/bin/bg* "${CHROOT}"/usr/bin/wcmgr "${CHROOT}"/usr/bin/byteguru*; do
	[ -e "${bin}" ] && OUR_BINS="${OUR_BINS}$(basename "${bin}") "
done
note "- наши исполняемые файлы: ${OUR_BINS:-**отсутствуют**}"

note ""
note "Итог: $( [ "${rc}" -eq 0 ] && echo 'вход настроен' || echo 'Есть проблемы — см. выше' )"
exit "${rc}"
