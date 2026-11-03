# bgwl — ByteguruOS compositor (v0)

`bgwl` is the small Wayland compositor for ByteguruOS. It is **not** a desktop
environment: it is the minimum server that can start a session, draw windows
with a uniform frame, host the ByteguruOS panel over layer-shell, and get the
keyboard to type in both Russian and English.

This directory is written for review, not for pride: the code has never been
compiled here (see [Verification status](#verification-status)).

## Target wlroots version

**wlroots 0.19.3** — pinned in `meson.build` via
`dependency('wlroots-0.19', version: '>= 0.19.0')`.

The task brief assumed Debian 13 "trixie" ships 0.19.x through
`libwlroots-dev`. That assumption is wrong, and it was verified:

| Suite | wlroots | dev package name |
| --- | --- | --- |
| trixie (stable) | 0.18.2-3 | `libwlroots-0.18-dev` |
| sid (unstable) | 0.19.3-1 and 0.20.2-1 | `libwlroots-0.19-dev`, `libwlroots-0.20-dev` |

Also note that `libwlroots-dev` no longer exists as a name in trixie/sid: the
packages are version-suffixed (`libwlroots-0.19-dev`), and headers install to
`/usr/include/wlroots-0.19`. So CI must not ask for plain `libwlroots-dev`.

Consequences for the build image:

* Building against **0.19.3** means the CI container must be sid (or trixie
  with an explicit `-t sid` / pinned source), not plain trixie stable.
* The downgrade cost is lower than expected, and that was measured rather than
  assumed: each signature used here was diffed against the wlroots **0.18.2**
  and **0.20.2** headers as well. Identical in all three:

  | API | 0.18.2 / 0.19.3 / 0.20.2 |
  | --- | --- |
  | `wlr_output_commit_state(output, state)` | two arguments |
  | `wlr_seat_pointer_notify_axis(..., source, relative_direction)` | 7 arguments |
  | `wlr_output_layout_create(display)` | takes the display |
  | `wlr_backend_autocreate(loop, &session_ptr)` | two arguments |
  | `wlr_compositor_create(display, version, renderer)` | three arguments |
  | `wlr_renderer_init_wl_display(renderer, display)` | unchanged |
  | `wlr_xdg_shell_create(display, version)` + `events.new_toplevel` | unchanged |
  | `wlr_layer_shell_v1_create(display, version)` | unchanged |
  | `wlr_scene_output_commit(scene_output, options)` / `wlr_scene_layer_surface_v1_configure(...)` | unchanged |
  | `wlr_keyboard_notify_modifiers(kbd, depressed, latched, locked, group)` | unchanged |
  | `wlr_xdg_toplevel_set_size()`, `wlr_cursor_warp()`, `wlr_cursor_set_xcursor()` | unchanged |

  So switching the pin to `libwlroots-0.18-dev` is a one-line change in
  `meson.build`; nothing in these sources has to be rewritten for it. What
  *is* still unverified is behaviour rather than prototypes — the wlroots
  headers installed on the runner remain the authority for the fields we read
  directly (`wlr_keyboard.modifiers.group`, `wlr_xdg_surface.geometry`,
  `wlr_output.current_mode`, …).
* The layer-shell protocol version differs between wlroots releases:
  `protocol/wlr-layer-shell-unstable-v1.xml` as vendored here declares
  `zwlr_layer_shell_v1` at version 5, and `bgwl` binds the global at 4.

## Upstream shape

The file layout and the split of responsibilities follow **`tinywl`**, the
single-file example that wlroots ships in its own tree (`tinywl/tinywl.c` in
0.19.x). Rootston was removed from wlroots' examples before 0.19, so tinywl is
the current reference shape. Original code, same skeleton:
`backend → renderer/allocator → globals → scene → output layout → xdg → cursor
→ seat → socket → start → run`. The cairo-backed `struct wlr_buffer` for the
titlebar and the PNG background follows
`examples/cairo-buffer.c`.

`bgwl` uses the **`wlr_scene` API exclusively**. There is no
`wlr_root`/legacy surface damage path in this tree: since wlroots 0.16 the
scene graph is what upstream examples are written against, and in 0.19 it is
the only supported rendering route.

## Files

| File | Contents |
| --- | --- |
| `bgwl.h` | All shared structs and cross-file prototypes. One header, no `server.h`. |
| `server.c` | `main`, hand-rolled `--help`/`--version`/`-c`/`-b` parser, backend auto-detect with headless fallback, global registration, SIGTERM/SIGINT, shutdown order. |
| `output.c` | Hotplug, preferred mode, left-to-right layout (`wlr_output_layout_add_auto`), frame commit, per-output background (solid or PNG). |
| `view.c` | Xdg toplevels, server-side titlebar, stacking, focus, hit testing, interactive move/resize. |
| `xdg.c` | `new_toplevel` / `new_popup` wiring, popup configure. |
| `layer.c` | `wlr_layer_shell_v1` handling, layer→scene-tree mapping, panel tracking and toggle. |
| `input.c` | libinput devices via `wlr_cursor`/`wlr_seat`, ru+us keymap, keybindings, focus-follows-mouse, cursor warp. |
| `util.c` | Env parsing, `/bin/sh` spawn, cairo `struct wlr_buffer`, titlebar drawing, PNG loading, xkb keymap builder. |
| `protocol/` | Vendored `wlr-layer-shell-unstable-v1.xml` (MIT, copied verbatim from wlroots 0.19.3). |
| `meson.build` | Subdir-friendly build file; runs `wayland-scanner` for xdg-shell and layer-shell. |

The include guard style is `BGWL_*`; nothing in this directory relies on
wlroots' internal guard macros, and each `.c` includes `<wlr/...>` headers
directly rather than through a private wrapper.

## Build (Debian-based CI image)

```sh
apt-get update && apt-get install -y --no-install-recommends \
  build-essential meson ninja-build pkg-config \
  libwayland-dev libwayland-bin wayland-protocols \
  libwlroots-0.19-dev \
  libxkbcommon-dev libpixman-1-dev libcairo2-dev libdrm-dev \
  libinput-dev \
  xkb-data x11-standard-config \
  linux-libc-dev
```

Packages I am confident exist in Debian 13/sid: `build-essential`, `meson`,
`ninja-build`, `pkg-config`, `libwayland-dev`, `libwayland-bin` (this is where
`wayland-scanner` lives), `wayland-protocols`, `libxkbcommon-dev`,
`libpixman-1-dev`, `libcairo2-dev`, `libdrm-dev`, `libinput-dev`, `xkb-data`,
`linux-libc-dev`.

Unsure / worth checking on the runner:

* **`libwlroots-0.19-dev`** — exists in **sid**, not in trixie (trixie has
  `libwlroots-0.18-dev`). Confirm the suite your image pins.
* **`x11-standard-config`** — I listed it because it provides the Xcursor
  theme `wlr_xcursor_manager_create(NULL, 24)` falls back to. On Debian the
  theme actually comes from `desktop-base` (`/usr/share/icons/default`);
  check with `apt-file search cursor-theme` if cursor rendering looks wrong.
* Whether wlroots in the archive was built with the X11/Wayland backends
  enabled — irrelevant for `bgwl` itself, it only changes what
  `wlr_backend_autocreate` may return.

Then:

```sh
meson setup build .
ninja -C build bgwl
./build/src/compositor/bgwl --version
```

`src/compositor/meson.build` contains no `project()` call on purpose; the
top-level `meson.build` (owned by another component) must
`subdir('src/compositor')`. The generated protocol headers land in the build
directory, and `-I<builddir>` plus `-DWLR_USE_UNSTABLE` are added locally.

## Run

```sh
# real hardware, VT or seat available
./bgwl

# force the headless backend (no GPU, no input devices) — what CI can use
./bgwl --backend headless
WAYLAND_DISPLAY=wayland-1 weston-simple-csd &   # any xdg client
./bgwl -c 'foot'                                 # start a client with the session

# nested inside another Wayland session
./bgwl                        # autocreate picks the Wayland backend

# background image instead of solid colour
BGWL_BACKGROUND=$HOME/.config/byteguruos/bg.png ./bgwl
BGWL_BACKGROUND_COLOR='#101820' ./bgwl
```

`bgwl` sets `WAYLAND_DISPLAY`, `XDG_SESSION_TYPE=wayland`,
`XDG_CURRENT_DESKTOP=ByteguruOS` and **`BGWL_SESSION_TYPE=wayland`** in its own
environment, so anything it spawns inherits them.

### Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `BGWL_TERMINAL` | `foot` | Command run by Super+Enter, through `/bin/sh -c`. |
| `BGWL_BACKGROUND` | unset | PNG path for the wallpaper (stretched to each output). |
| `BGWL_BACKGROUND_COLOR` | `#141823` | Solid wallpaper; `#RRGGBB` or `#RRGGBBAA`. Wins over `BGWL_BACKGROUND`. |
| `BGWL_DECOR_COLOR` | `#262A33` | Titlebar fill. |
| `BGWL_TEXT_COLOR` | `#E0E2E6` | Titlebar text. |
| `BGWL_XKB_LAYOUT` | `ru,us` | Layout list; both are compiled into one keymap. |
| `BGWL_XKB_VARIANT` | empty | xkb variant. |
| `BGWL_XKB_MODEL` | `pc105` | xkb model. |
| `BGWL_XKB_OPTIONS` | `grp:alt_shift_toggle` | xkb options; keep a `grp:*` option if you want Alt+Shift as well. |
| `BGWL_HEADLESS_SIZE` | `1280x800` | Output size for the headless fallback. |
| `WLR_BACKENDS`, `WLR_RENDERER`, `WLR_LIBINPUT_NO_DEVICES` | — | Interpreted by wlroots, not by us. |
| `BGWL_SESSION_TYPE` | set by us | Reports `wayland` to session consumers. |

## Keybindings

`Mod4` is the Super key (`WLR_MODIFIER_LOGO` in wlroots).

| Keys | Action | Notes |
| --- | --- | --- |
| Super+Enter | Launch terminal | `$BGWL_TERMINAL`, default `foot`. Numpad Enter works too. |
| Super+Shift+Q | Exit the compositor | Also `SIGTERM`/`SIGINT` do a clean shutdown. |
| Super+D | Show/hide the panel | Enables/disables the layer surface's scene node. |
| Super+Space | Switch keyboard layout (ru ⇄ us) | bgwl's own toggle; Alt+Shift also works because of `grp:alt_shift_toggle` in the xkb options. |
| Drag on the titlebar | Move the window | Server-side decoration, no buttons. |
| Escape | *(nothing)* | Unlike tinywl, Escape does not kill the session. |

Bindings match **evdev keycodes**, not keysyms: with the Russian layout the Q
key emits `й`, so a keysym table would break Super+Shift+Q. Ctrl/Alt are
excluded from the modifier mask so client-side Ctrl+Alt+… chords keep working.

## Panel contract

The panel itself belongs to another ByteguruOS component. `bgwl` only:

1. advertises `zwlr_layer_shell_v1` (bound at version 4),
2. puts each layer surface in the scene tree for its layer
   (background/bottom/**normal**=xdg toplevels/top/overlay),
3. configures it once on its initial commit via
   `wlr_scene_layer_surface_v1_configure()`,
4. remembers the first mapped surface whose `namespace` starts with
   `bgwl-panel` as *the* panel, and flips its scene node on Super+D.

So the panel must call `zwlr_layer_surface_v1.set_namespace("bgwl-panel")`.

## Decisions worth arguing about

* **Server-side decoration, drawn with cairo.** wlroots 0.19 has no
  `wlr_scene_text` node, and the only supported way to get our own pixels into
  the scene is a `struct wlr_buffer` implementation — exactly the pattern in
  upstream `examples/cairo-buffer.c`. CSD-only would have been less code but no
  uniform frame. The titlebar is 28 px, shows `xdg_toplevel.title`, has no
  buttons, and no resize borders exist (resize by dragging is implemented but
  only reachable through a client's `request_resize`).
* **Headless fallback.** `wlr_backend_autocreate()` returns NULL when no DRM
  session can be opened, which is exactly the CI case, so `bgwl` builds a
  multi-backend containing a headless backend with one 1280×800 output and
  continues. If autocreate *succeeds* but yields no DRM backend (nested
  session), a headless output is added as well so the session is never
  output-less.
* **Shutdown order.** Clients are destroyed first, then the signal sources and
  server listeners, then the **backend** (which fires the output/input destroy
  events our handlers use to drop scene nodes and free per-device structs while
  the scene graph is still alive), then scene → cursor → allocator/renderer →
  output layout → display.
* **Focus follows the mouse, but only across toplevels.** Hovering a panel or
  the wallpaper never moves the keyboard focus.
* **`wl_shm` comes from `wlr_renderer_init_wl_display()`**, which calls
  `wlr_shm_create_with_renderer()` internally. Creating a second `wl_shm`
  global here would be a bug, so we do not.

## Verification status

Read this before trusting any line of code in this directory.

* Written on Windows: no C compiler, no `make`, no meson, no wlroots — **the
  code has never been compiled or run**.
* What *was* done: `wlroots` 0.19.3 was cloned and every API call in these
  sources was checked against that tree's headers
  (`include/wlr/**/*.h`), its `tinywl/tinywl.c`, its
  `examples/cairo-buffer.c`, its `types/scene/layer_shell_v1.c` and its
  `backend/backend.c`. The wlroots version table above is from
  packages.debian.org.
* What was *not* verified: nothing has been through a preprocessor, linker or
  runtime. `wl_event_loop_add_signal()` and `wl_event_loop_signal_func_t` come
  from libwayland headers that are not present on this machine.
* Every place that still needs a check against installed headers carries the
  marker `/* ВЕРИФИЦИРОВАТЬ: ... */`. Reviewers should fix compile errors by
  grepping that string first:

  ```sh
  grep -rn "ВЕРИФИЦИРОВАТЬ" src/compositor/
  ```

Expected first-compile problems, most likely first:

1. **meson**, not C: the runner image has no `wlroots-0.19.pc` (trixie ships
   0.18) → `dependency('wlroots-0.19')` fails and the `error()` message fires.
   Fix by changing the name or pinning sid.
2. **Generated protocol headers not found.** wlroots' headers do
   `#include "xdg-shell-protocol.h"`, which only resolves if the build
   directory is on the include path. If the `-I@BUILD_DIR@` in `meson.build`
   is not enough, add `include_directories: include_directories('.',
   meson.current_build_dir())` — the same pattern upstream `protocol/meson.build`
   uses.
3. **`<wlr/interfaces/wlr_buffer.h>` / `<wlr/interfaces/wlr_keyboard.h>`**:
   unstable headers used by `util.c` and `input.c`. Field names
   (`destroy`, `begin_data_ptr_access`, `end_data_ptr_access`) match the
   0.19.3 `examples/cairo-buffer.c`; re-check them if the pin moves.
4. **`<linux/input-event-codes.h>`** — present on any Debian with
   `linux-libc-dev`, but this is where `KEY_ENTER`/`KEY_D`/`KEY_Q`/`KEY_SPACE`
   come from, so it is the first thing to look at if `input.c` fails.
5. **`wlr_log` format specifiers** for `int32_t refresh` and
   `(double) scale` — harmless on x86-64/aarch64, but `%d` for `int32_t` may
   warn under `-Wformat` on 32-bit ARM.
6. **Unused `data` parameters** in listener callbacks (`output_frame`,
   `keyboard_handle_modifiers`, …). Only a problem if the top-level
   `meson.build` adds `-Wextra -Werror`; `-Wall` alone does not warn.

Things that were **checked and are not problems**, so reviewers do not redo
them: the two-argument `wlr_output_commit_state()`, the seven-argument
`wlr_seat_pointer_notify_axis()`, `wlr_output_layout_create(display)` and all
other prototypes in the table above; `wlr_xdg_surface_*`/`wlr_xdg_toplevel_*`
`try_from_wlr_surface()` helpers dereferencing NULL (guarded in `view.c` and
`xdg.c`); `wlr_scene_output` being destroyed by the output addon rather than by
us (`output.c`); the maximum protocol versions (`WM_BASE_VERSION` is 6, so
binding xdg at 3 is fine; `LAYER_SHELL_VERSION` is 5, so binding layer-shell at
4 is fine).

## Что НЕ реализовано

Deliberately out of scope for v0 — not "not yet written", but excluded:

* **XWayland** — no `wlr_xwayland`, no X11 clients at all.
* **Workspaces / virtual desktops** — no `ext-workspace`, no switching.
* **Tiling or any window management policy** — windows float; nothing snaps,
  cascades or tiles.
* **Maximize and fullscreen** — the requests are answered with an empty
  configure only, because xdg-shell requires a reply; the window does not
  change.
* **Animations**, transitions, compositor-side blur/shadow/rounded corners.
* **DRM lease** (`wlr_drm_lease`) — no VR / passthrough.
* **Screencopy / screen capture** (`wlr_screencopy`, ext-image-copy-capture).
* **Input-method (`text-input-v3`, `input-method-v2`)**, on-screen keyboards,
  IME.
* **Explicit sync / fences** (`linux-drm-syncobj`) — deliberately not wired,
  even though 0.19 supports it.
* **Session restore**, per-monitor configuration persistence, `wlr-output-
  management` (randr-like) protocol, output scale/transform/rotation controls.
* **Window buttons** (close/minimise/maximise), resize borders, double-click to
  maximise, alt-drag from anywhere.
* **Binding configuration** — keybindings are compiled in; no config file, no
  `wlr-keyboard-shortcuts-inhibit` handling.
* **Touch, tablets, pen, keyboard groups, multiple seats** — only one `seat0`
  with pointer + keyboard are handled; other device types are ignored.
* **Primary selection / `wlr-data-control`**, drag-and-drop beyond what
  `wl_data_device_manager` gives for free.
* **Idle handling, power management, `ext-session-lock`**, PIP audio, session
  bus integration.
* **Panel logic** — bgwl only shows/hides a layer surface; layout, items, tray
  and clock belong to the panel component.
* **HiDPI** — `wlr_xcursor_manager_load()` is called with the output's scale,
  but no scale factor is ever set, so everything renders at scale 1.
