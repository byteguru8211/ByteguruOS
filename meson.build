#
# ByteguruOS — верхний уровень сборки нативных компонентов.
#
#   meson setup build && ninja -C build && meson test -C build
#
# Единая точка входа для dpkg-buildpackage (debian/rules) и для ручной сборки.
# Композитор — единственный нативный компонент оболочки; всё остальное —
# Python, распространяется как есть.

project(
	'byteguruos',
	'c',
	version: '1.0.0',
	license: 'Apache-2.0',
	meson_version: '>= 0.60.0',
	default_options: [
		'c_std=c11',
		'warning_level=2',
		'buildtype=debugoptimized',
	],
)

add_project_arguments(
	[
		'-D_GNU_SOURCE',
		'-Wno-unused-parameter',
		'-Werror=implicit-function-declaration',
		'-Werror=incompatible-pointer-types',
		'-Werror=return-type',
	],
	language: 'c',
)

subdir('src/compositor')
