/*
 * Input: libinput devices through wlr_cursor/wlr_seat, the ru+us keymap,
 * compositor keybindings and focus-follows-mouse.
 *
 * Keybindings are matched on evdev keycodes rather than keysyms: with the
 * Russian layout active the physical Q key produces the keysym for "й", so a
 * keysym table would silently break Super+Shift+Q. That costs the ability to
 * rebind by symbol, which bgwl v0 does not offer anyway.
 * ВЕРИФИЦИРОВАТЬ: KEY_* come from <linux/input-event-codes.h> (Debian:
 * linux-libc-dev, pulled in by build-essential).
 */
#include <linux/input-event-codes.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>

#include <wayland-server-core.h>
#include <wlr/interfaces/wlr_keyboard.h>
#include <wlr/types/wlr_cursor.h>
#include <wlr/types/wlr_input_device.h>
#include <wlr/types/wlr_keyboard.h>
#include <wlr/types/wlr_pointer.h>
#include <wlr/types/wlr_seat.h>
#include <wlr/types/wlr_xcursor_manager.h>
#include <wlr/util/log.h>
#include <xkbcommon/xkbcommon.h>

#include "bgwl.h"

/* --- keyboard ----------------------------------------------------------- */

enum bgwl_binding {
	BGWL_BIND_NONE,
	BGWL_BIND_TERMINAL,
	BGWL_BIND_PANEL,
	BGWL_BIND_LAYOUT,
	BGWL_BIND_EXIT,
};

/* Mod4 is the Super key; wlroots reports it as WLR_MODIFIER_LOGO. */
static enum bgwl_binding match_binding(uint32_t modifiers, uint32_t keycode) {
	if ((modifiers & WLR_MODIFIER_LOGO) == 0 ||
			(modifiers & (WLR_MODIFIER_CTRL | WLR_MODIFIER_ALT)) != 0) {
		return BGWL_BIND_NONE;
	}
	bool shift = (modifiers & WLR_MODIFIER_SHIFT) != 0;

	switch (keycode) {
	case KEY_ENTER:
	case KEY_KPENTER:
		return shift ? BGWL_BIND_NONE : BGWL_BIND_TERMINAL;
	case KEY_D:
		return shift ? BGWL_BIND_NONE : BGWL_BIND_PANEL;
	case KEY_SPACE:
		return shift ? BGWL_BIND_NONE : BGWL_BIND_LAYOUT;
	case KEY_Q:
		return shift ? BGWL_BIND_EXIT : BGWL_BIND_NONE;
	default:
		return BGWL_BIND_NONE;
	}
}

void bgwl_keyboard_switch_layout(struct bgwl_keyboard *keyboard) {
	struct wlr_keyboard *wlr_keyboard = keyboard->wlr_keyboard;
	struct xkb_keymap *keymap = wlr_keyboard->keymap;
	if (keymap == NULL) {
		return;
	}

	xkb_layout_index_t count = xkb_keymap_num_layouts(keymap);
	if (count < 2) {
		wlr_log(WLR_INFO, "keymap has %d layout(s), nothing to switch",
			(int)count);
		return;
	}

	xkb_layout_index_t current = wlr_keyboard->modifiers.group;
	xkb_layout_index_t next = (current + 1) % count;

	/* wlr_keyboard_notify_modifiers() re-applies the mask to the keyboard's
	 * xkb_state and emits the modifiers event, which our modifiers listener
	 * forwards to the focused client. */
	wlr_keyboard_notify_modifiers(wlr_keyboard,
		wlr_keyboard->modifiers.depressed, wlr_keyboard->modifiers.latched,
		wlr_keyboard->modifiers.locked, next);

	const char *name = xkb_keymap_layout_get_name(keymap, next);
	wlr_log(WLR_INFO, "keyboard layout switched to %s (index %d/%d)",
		name ? name : "?", (int)next, (int)count);
}

static bool handle_keybinding(struct bgwl_server *server,
		struct bgwl_keyboard *keyboard, uint32_t modifiers, uint32_t keycode,
		bool pressed) {
	if (!pressed) {
		return false;
	}

	if (binding_pressed(modifiers, keycode, 0)) {
		switch (keycode) {
		case KEY_ENTER:
		case KEY_KPENTER:
			if (!bgwl_spawn(server, server->cfg.terminal)) {
				wlr_log(WLR_ERROR, "failed to launch terminal '%s'",
					server->cfg.terminal);
			}
			return true;
		case KEY_D:
			bgwl_panel_toggle(server);
			return true;
		case KEY_SPACE:
			bgwl_keyboard_switch_layout(keyboard);
			return true;
		default:
			break;
		}
	}

	if (binding_pressed(modifiers, keycode, WLR_MODIFIER_SHIFT) &&
			keycode == KEY_Q) {
		bgwl_server_terminate(server);
		return true;
	}

	return false;
}

static void keyboard_handle_modifiers(struct wl_listener *listener, void *data) {
	struct bgwl_keyboard *keyboard =
		wl_container_of(listener, keyboard, modifiers);

	wlr_seat_set_keyboard(keyboard->server->seat, keyboard->wlr_keyboard);
	wlr_seat_keyboard_notify_modifiers(keyboard->server->seat,
		&keyboard->wlr_keyboard->modifiers);
}

static void keyboard_handle_key(struct wl_listener *listener, void *data) {
	struct bgwl_keyboard *keyboard = wl_container_of(listener, keyboard, key);
	struct bgwl_server *server = keyboard->server;
	struct wlr_keyboard_key_event *event = data;
	struct wlr_seat *seat = server->seat;

	uint32_t modifiers = wlr_keyboard_get_modifiers(keyboard->wlr_keyboard);
	bool handled = handle_keybinding(server, keyboard, modifiers,
		event->keycode, event->state == WL_KEYBOARD_KEY_STATE_PRESSED);

	if (!handled) {
		wlr_seat_set_keyboard(seat, keyboard->wlr_keyboard);
		wlr_seat_keyboard_notify_key(seat, event->time_msec, event->keycode,
			event->state);
	}
}

static void keyboard_handle_destroy(struct wl_listener *listener, void *data) {
	struct bgwl_keyboard *keyboard =
		wl_container_of(listener, keyboard, destroy);

	wl_list_remove(&keyboard->modifiers.link);
	wl_list_remove(&keyboard->key.link);
	wl_list_remove(&keyboard->destroy.link);
	wl_list_remove(&keyboard->link);
	free(keyboard);
}

static void server_add_keyboard(struct bgwl_server *server,
		struct wlr_input_device *device) {
	struct wlr_keyboard *wlr_keyboard = wlr_keyboard_from_input_device(device);
	if (wlr_keyboard == NULL) {
		wlr_log(WLR_ERROR, "input device claimed to be a keyboard is not");
		return;
	}

	struct bgwl_keyboard *keyboard = calloc(1, sizeof(*keyboard));
	if (keyboard == NULL) {
		wlr_log(WLR_ERROR, "out of memory for keyboard");
		return;
	}
	keyboard->server = server;
	keyboard->wlr_keyboard = wlr_keyboard;

	struct xkb_keymap *keymap = bgwl_xkb_keymap_new(&server->cfg);
	if (keymap == NULL) {
		/* Without a keymap the seat cannot deliver keys; keep the device
		 * attached but let the user see why nothing types. */
		wlr_log(WLR_ERROR, "no xkb keymap installed for the keyboard");
	} else {
		if (!wlr_keyboard_set_keymap(wlr_keyboard, keymap)) {
			wlr_log(WLR_ERROR, "wlr_keyboard_set_keymap failed");
		}
		xkb_keymap_unref(keymap);
	}
	wlr_keyboard_set_repeat_info(wlr_keyboard, 25, 600);

	keyboard->modifiers.notify = keyboard_handle_modifiers;
	wl_signal_add(&wlr_keyboard->events.modifiers, &keyboard->modifiers);
	keyboard->key.notify = keyboard_handle_key;
	wl_signal_add(&wlr_keyboard->events.key, &keyboard->key);
	keyboard->destroy.notify = keyboard_handle_destroy;
	wl_signal_add(&device->events.destroy, &keyboard->destroy);

	wlr_seat_set_keyboard(server->seat, wlr_keyboard);
	wl_list_insert(&server->keyboards, &keyboard->link);
}

static void server_add_pointer(struct bgwl_server *server,
		struct wlr_input_device *device) {
	if (!wlr_cursor_attach_input_device(server->cursor, device)) {
		wlr_log(WLR_ERROR, "wlr_cursor_attach_input_device failed");
	}
}

void bgwl_new_input(struct wl_listener *listener, void *data) {
	struct bgwl_server *server = wl_container_of(listener, server, new_input);
	struct wlr_input_device *device = data;

	switch (device->type) {
	case WLR_INPUT_DEVICE_KEYBOARD:
		server_add_keyboard(server, device);
		break;
	case WLR_INPUT_DEVICE_POINTER:
		server_add_pointer(server, device);
		break;
	default:
		/* Touch, tablet and keyboard-group devices are out of scope for v0. */
		break;
	}

	/* bgwl always draws a cursor, so pointer capability stays on even before
	 * a physical mouse exists (headless sessions). */
	uint32_t caps = WL_SEAT_CAPABILITY_POINTER;
	if (!wl_list_empty(&server->keyboards)) {
		caps |= WL_SEAT_CAPABILITY_KEYBOARD;
	}
	wlr_seat_set_capabilities(server->seat, caps);
}

/* --- pointer ------------------------------------------------------------ */

static void process_cursor_motion(struct bgwl_server *server, uint32_t time) {
	if (server->cursor_mode != BGWL_CURSOR_PASSTHROUGH) {
		bgwl_toplevel_process_motion(server);
		return;
	}

	struct wlr_seat *seat = server->seat;
	double sx = 0, sy = 0;
	struct wlr_surface *surface = bgwl_surface_at(server, server->cursor->x,
		server->cursor->y, &sx, &sy);

	struct bgwl_toplevel *toplevel = bgwl_toplevel_at(server,
		server->cursor->x, server->cursor->y);

	if (surface == NULL) {
		/* Over the background, a titlebar or nothing at all: restore the
		 * theme cursor so it stays visible outside client surfaces. */
		wlr_cursor_set_xcursor(server->cursor, server->cursor_mgr, "default");
		wlr_seat_pointer_clear_focus(seat);
	} else {
		wlr_seat_pointer_notify_enter(seat, surface, sx, sy);
		wlr_seat_pointer_notify_motion(seat, time, sx, sy);
	}

	/* Focus follows the mouse, but only across toplevels: sliding the pointer
	 * over a panel or the wallpaper must not steal the keyboard. */
	if (toplevel != NULL) {
		bgwl_toplevel_focus(toplevel);
	}
}

void bgwl_cursor_motion(struct wl_listener *listener, void *data) {
	struct bgwl_server *server = wl_container_of(listener, server, cursor_motion);
	struct wlr_pointer_motion_event *event = data;

	wlr_cursor_move(server->cursor, &event->pointer->base, event->delta_x,
		event->delta_y);
	process_cursor_motion(server, event->time_msec);
}

void bgwl_cursor_motion_absolute(struct wl_listener *listener, void *data) {
	struct bgwl_server *server =
		wl_container_of(listener, server, cursor_motion_absolute);
	struct wlr_pointer_motion_absolute_event *event = data;

	/* Absolute input (a nested Wayland/X11 window, a touchscreen mapped to
	 * the pointer) has to be warped into the layout, otherwise the pointer
	 * teleports between edges. */
	wlr_cursor_warp_absolute(server->cursor, &event->pointer->base, event->x,
		event->y);
	process_cursor_motion(server, event->time_msec);
}

void bgwl_cursor_button(struct wl_listener *listener, void *data) {
	struct bgwl_server *server = wl_container_of(listener, server, cursor_button);
	struct wlr_pointer_button_event *event = data;

	wlr_seat_pointer_notify_button(server->seat, event->time_msec,
		event->button, event->state);

	if (event->state == WL_POINTER_BUTTON_STATE_RELEASED) {
		bgwl_toplevel_reset_cursor_mode(server);
		return;
	}

	struct bgwl_toplevel *toplevel = bgwl_toplevel_at(server, server->cursor->x,
		server->cursor->y);
	if (toplevel == NULL) {
		return;
	}

	bgwl_toplevel_focus(toplevel);

	/* A press inside the server-side titlebar starts an interactive move; the
	 * client never sees these button events because the titlebar is not a
	 * client surface. */
	double sx = 0, sy = 0;
	if (bgwl_surface_at(server, server->cursor->x, server->cursor->y,
			&sx, &sy) == NULL) {
		struct wlr_box box;
		struct wlr_scene_node *node = &toplevel->scene_tree->node;
		box.x = node->x;
		box.y = node->y;
		box.width = toplevel->decor_width;
		box.height = BGWL_DECOR_HEIGHT;
		if (box.width > 0 && server->cursor->y < box.y + box.height &&
				server->cursor->y >= box.y &&
				server->cursor->x >= box.x &&
				server->cursor->x < box.x + box.width) {
			bgwl_toplevel_begin_interactive(toplevel, BGWL_CURSOR_MOVE, 0);
		}
	}
}

void bgwl_cursor_axis(struct wl_listener *listener, void *data) {
	struct bgwl_server *server = wl_container_of(listener, server, cursor_axis);
	struct wlr_pointer_axis_event *event = data;

	wlr_seat_pointer_notify_axis(server->seat, event->time_msec,
		event->orientation, event->delta, event->delta_discrete,
		event->source, event->relative_direction);
}

void bgwl_cursor_frame(struct wl_listener *listener, void *data) {
	struct bgwl_server *server = wl_container_of(listener, server, cursor_frame);

	wlr_seat_pointer_notify_frame(server->seat);
}

/* --- seat requests ------------------------------------------------------ */

void bgwl_seat_request_cursor(struct wl_listener *listener, void *data) {
	struct bgwl_server *server = wl_container_of(listener, server, request_cursor);
	struct wlr_seat_pointer_request_set_cursor_event *event = data;
	struct wlr_seat_client *focused_client =
		server->seat->pointer_state.focused_client;

	if (focused_client == event->seat_client) {
		wlr_cursor_set_surface(server->cursor, event->surface,
			event->hotspot_x, event->hotspot_y);
	}
}

void bgwl_seat_request_set_selection(struct wl_listener *listener, void *data) {
	struct bgwl_server *server =
		wl_container_of(listener, server, request_set_selection);
	struct wlr_seat_request_set_selection_event *event = data;

	wlr_seat_set_selection(server->seat, event->source, event->serial);
}
