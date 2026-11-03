#
# bgwl — ByteguruOS v0 Wayland compositor.
#
# This file is written to be pulled in from the repository top level with
# subdir('src/compositor'): the top-level meson.build owns project() and the
# global compiler setup, so there is deliberately no project() call here.
# Build it as part of the tree:
#
#   meson setup build . && ninja -C build bgwl && meson test -C build
#
# Собирается против wlroots 0.18 (Debian 13 trixie — там ISO и собирается) и
# против 0.19/0.20 (sid). Используемые сигнатуры проверены идентичными в
# 0.18.2, 0.19.3 и 0.20.2, поэтому порядок поиска — от стабильного релиза.

bgwl_version = '0.0.1'

# Debian ставит версионированные модули (wlroots-0.18.pc) и заголовки в
# /usr/include/wlroots-0.18; простого libwlroots-dev в trixie больше нет.
# ВЕРИФИЦИРОВАТЬ: в других дистрибутивах модуль может быть без суффикса —
# тогда сработает последняя проверка. `pkg-config --list-all | grep -i wlroots`
bgwl_wlroots_dep = dependency('wlroots-0.18', version: '>= 0.18.0', required: false)
if not bgwl_wlroots_dep.found()
	bgwl_wlroots_dep = dependency('wlroots-0.19', version: '>= 0.19.0', required: false)
endif
if not bgwl_wlroots_dep.found()
	bgwl_wlroots_dep = dependency('wlroots-0.20', version: '>= 0.20.0', required: false)
endif
if not bgwl_wlroots_dep.found()
	# Собранная из исходников как subproject, либо дистрибутив без версий в имени.
	bgwl_wlroots_dep = dependency('wlroots', required: false)
endif
if not bgwl_wlroots_dep.found()
	error('wlroots >= 0.18 development files not found. Debian trixie: ' +
		'apt-get install libwlroots-0.18-dev')
endif

# These are required by wlroots' own public headers, not by our taste:
#   pixman.h         — wlr/types/wlr_scene.h takes pixman_region32_t
#   xkbcommon/xkbcommon.h — wlr/types/wlr_keyboard.h
#   wayland-server.h — every wlr/types header
#   drm_fourcc.h     — buffer formats used by our cairo buffer in util.c
# cairo is ours alone: it draws the server-side titlebar and decodes the
# BGWL_BACKGROUND PNG.
bgwl_deps = [
	bgwl_wlroots_dep,
	dependency('wayland-server'),
	dependency('xkbcommon'),
	dependency('libdrm'),
	dependency('pixman-1'),
	dependency('cairo'),
]

# --- protocol code generation --------------------------------------------
#
# wlroots 0.19 installs no protocol XML, and its headers do
# #include "xdg-shell-protocol.h" / "wlr-layer-shell-unstable-v1-protocol.h",
# so every compositor has to run wayland-scanner itself. The output names below
# match wlroots' internal rules exactly, which is what makes the quoted
# includes resolve from the build directory.

bgwl_wayland_scanner = find_program('wayland-scanner', native: true,
	required: false)
if not bgwl_wayland_scanner.found()
	# Debian ships the binary in libwayland-bin; some setups only expose it
	# through pkg-config.
	bgwl_scanner_dep = dependency('wayland-scanner', native: true,
		required: false)
	if bgwl_scanner_dep.found()
		bgwl_wayland_scanner = find_program(
			bgwl_scanner_dep.get_variable('wayland_scanner',
				'wayland-scanner'),
			native: true)
	endif
endif
if not bgwl_wayland_scanner.found()
	error('wayland-scanner not found. Debian/Ubuntu: ' +
		'apt-get install libwayland-bin')
endif

bgwl_wayland_protocols_dep = dependency('wayland-protocols', version: '>= 1.31')
bgwl_wayland_protocols_dir = bgwl_wayland_protocols_dep.get_variable('pkgdatadir')

# xdg-shell comes from wayland-protocols. wlr-layer-shell is a wlroots-owned
# protocol with no home in wayland-protocols, so it is vendored verbatim in
# protocol/ (MIT, upstream copy — see README.md).
bgwl_protocols = {
	'xdg-shell': bgwl_wayland_protocols_dir / 'stable/xdg-shell/xdg-shell.xml',
	'wlr-layer-shell-unstable-v1': files('protocol/wlr-layer-shell-unstable-v1.xml'),
}

bgwl_protocol_code = []
bgwl_protocol_headers = []
foreach name, xml : bgwl_protocols
	bgwl_protocol_code += custom_target(
		name.underscorify() + '_code',
		input: xml,
		output: '@BASENAME@-protocol.c',
		command: [bgwl_wayland_scanner, 'private-code', '@INPUT@', '@OUTPUT@'])
	bgwl_protocol_headers += custom_target(
		name.underscorify() + '_header',
		input: xml,
		output: '@BASENAME@-protocol.h',
		command: [bgwl_wayland_scanner, 'server-header', '@INPUT@', '@OUTPUT@'])
endforeach

bgwl_config = configuration_data()
bgwl_config.set_quoted('BGWL_VERSION', bgwl_version)
bgwl_config_h = configure_file(output: 'bgwl_config.h',
	configuration: bgwl_config)

bgwl_sources = files(
	'server.c',
	'output.c',
	'view.c',
	'xdg.c',
	'layer.c',
	'input.c',
	'util.c',
)

# -DWLR_USE_UNSTABLE is mandatory: every wlr/* header stops with #error
# without it. The -I makes the generated protocol headers visible to wlroots'
# quoted includes; meson normally adds the build directory as a dependency of
# the custom targets above, so this is a belt-and-braces path.
# ВЕРИФИЦИРОВАТЬ: the generated protocol headers must be reachable as
# "xdg-shell-protocol.h" / "wlr-layer-shell-unstable-v1-protocol.h" from
# wlroots' own quoted includes. Meson usually adds the custom_target output
# directory by itself; the explicit -I below is a belt-and-braces path and
# can be deleted once the build is green.
bgwl_c_args = ['-DWLR_USE_UNSTABLE', '-I@0@'.format(meson.current_build_dir())]

bgwl_exe = executable(
	'bgwl',
	[bgwl_sources, bgwl_protocol_code, bgwl_protocol_headers, bgwl_config_h],
	dependencies: bgwl_deps,
	include_directories: include_directories('.'),
	c_args: bgwl_c_args,
	install: true,
)

# Headless smoke test: --version prints and exits before any backend is
# created, so it also proves the binary links against wlroots in CI runners
# without GPUs or input devices.
test('bgwl-version', bgwl_exe, args: ['--version'])
