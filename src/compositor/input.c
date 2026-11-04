/*
 * Input: libinput devices through wlr_cursor/wlr_seat, the keymap, the
 * keybinding table and pointer interaction (focus, buttons, dragging).
 *
 * Keybindings are matched on evdev keycodes rather than keysyms: with the
 * Russian layout active the physical Q key produces the keysym for "й", so a
 * keysym table would silently break Super+Shift+Q. This is what keeps a
 * ByteguruOS shortcut working in both layouts, which is the point of shipping
 * ru+us in one keymap.
 *
 * Dispatch order: session lock first (nothing else may consume keys while the
 * screen is locked), then the user's bindings from bgwl.conf, then the
 * built-in table, then the focused client. A user binding therefore overrides
 * a default instead of competing with it.
 */
#include <linux/input-event-codes.h>
#include <stdbool.h>
#include <stdlib.h>
#include <string.h>

#include <wayland-server-core.h>
#include <wlr/interfaces/wlr_keyboard.h>
#include <wlr/types/wlr_cursor.h>
#include <wlr/types/wlr_data_device.h>
#include <wlr/types/wlr_input_device.h>
#include <wlr/types/wlr_keyboard.h>
#include <wlr/types/wlr_output.h>
#include <wlr/types/wlr_output_layout.h>
#include <wlr/types/wlr_pointer.h>
#include <wlr/types/wlr_scene.h>
#include <wlr/types/wlr_seat.h>
#include <wlr/types/wlr_xcursor_manager.h>
#include <wlr/util/log.h>
#include <xkbcommon/xkbcommon.h>

#include "bgwl.h"

/* --- keyboard ----------------------------------------------------------- */

/* Built-in shortcuts. Kept short on purpose: the desktop's own keys are the
 * ones a user cannot rebind before they can type, so they must be few and
 * obvious. Everything else lives in bgwl.conf and can be rebound. */
static const struct {
	uint32_t modifiers;
	uint32_t keycode;
	const char *action;
} builtin_bindings[] = {
	{ WLR_MODIFIER_LOGO, KEY_ENTER, "terminal" },
	{ WLR_MODIFIER_LOGO, KEY_E, "file-manager" },
	{ WLR_MODIFIER_LOGO, KEY_L, "lock" },
	{ WLR_MODIFIER_LOGO, KEY_PRINT, "screenshot" },
	{ WLR_MODIFIER_LOGO | WLR_MODIFIER_SHIFT, KEY_Q, "exit" },

	/* Window actions. */
	{ WLR_MODIFIER_LOGO, KEY_C, "close" },
	{ WLR_MODIFIER_LOGO, KEY_M, "maximize" },
	{ WLR_MODIFIER_LOGO, KEY_F, "fullscreen" },
	{ WLR_MODIFIER_LOGO, KEY_P, "pin" },
	{ WLR_MODIFIER_LOGO, KEY_T, "layout-next" },
	{ WLR_MODIFIER_LOGO | WLR_MODIFIER_SHIFT, KEY_T, "layout-float" },
	{ WLR_MODIFIER_LOGO, KEY_R, "split-rotate" },
	{ WLR_MODIFIER_LOGO, KEY_D, "panel" },

	/* Focus and movement. */
	{ WLR_MODIFIER_LOGO, KEY_J, "focus-next" },
	{ WLR_MODIFIER_LOGO, KEY_K, "focus-prev" },
	{ WLR_MODIFIER_LOGO, KEY_H, "move-prev-slot" },
	{ WLR_MODIFIER_LOGO, KEY_V, "move-next-slot" },
	{ WLR_MODIFIER_LOGO | WLR_MODIFIER_SHIFT, KEY_J, "send-next" },
	{ WLR_MODIFIER_LOGO | WLR_MODIFIER_SHIFT, KEY_K, "send-prev" },
	{ WLR_MODIFIER_LOGO | WLR_MODIFIER_CTRL, KEY_LEFT, "snap-left" },
	{ WLR_MODIFIER_LOGO | WLR_MODIFIER_CTRL, KEY_RIGHT, "snap-right" },
	{ WLR_MODIFIER_LOGO | WLR_MODIFIER_CTRL, KEY_UP, "snap-top" },
	{ WLR_MODIFIER_LOGO | WLR_MODIFIER_CTRL, KEY_DOWN, "snap-bottom" },
	{ WLR_MODIFIER_LOGO | WLR_MODIFIER_SHIFT, KEY_LEFT, "window-next-output" },
	{ WLR_MODIFIER_LOGO | WLR_MODIFIER_SHIFT, KEY_RIGHT, "window-prev-output" },

	/* Master area in the tile layout. */
	{ WLR_MODIFIER_LOGO, KEY_EQUAL, "master-grow" },
	{ WLR_MODIFIER_LOGO, KEY_MINUS, "master-shrink" },
	{ WLR_MODIFIER_LOGO | WLR_MODIFIER_SHIFT, KEY_EQUAL, "master-more" },
	{ WLR_MODIFIER_LOGO | WLR_MODIFIER_SHIFT, KEY_MINUS, "master-fewer" },
};

/* Super+1..0 is the workspace row. Handled as a family rather than ten table
 * rows, because both the bare and the Shift variant exist. */
static const uint32_t digit_keycodes[10] = {
	KEY_1, KEY_2, KEY_3, KEY_4, KEY_5, KEY_6, KEY_7, KEY_8, KEY_9, KEY_0,
};

static int workspace_for_keycode(uint32_t keycode) {
	for (size_t i = 0; i < 10; i++) {
		if (keycode == digit_keycodes[i]) {
			return (int)i;
		}
	}
	return -1;
}

/* The output a window is mostly on, plus its neighbour in one direction. */
static struct bgwl_output *output_under(struct bgwl_server *server,
		struct bgwl_toplevel *toplevel) {
	struct wlr_box frame;
	bgwl_toplevel_get_geometry(toplevel, &frame);
	struct wlr_output *found = wlr_output_layout_output_at(server->output_layout,
		frame.x + frame.width / 2, frame.y + frame.height / 2);

	struct bgwl_output *output;
	wl_list_for_each(output, &server->outputs, link) {
		if (output->wlr_output == found) {
			return output;
		}
	}
	return bgwl_server_first_output(server);
}

/* Snaps a floating window to a half of the output it is on. In a tiled
 * workspace the grid already does this, so the binding does nothing rather
 * than fighting the layout. */
static void snap_focused(struct bgwl_server *server, uint32_t edge) {
	struct bgwl_toplevel *toplevel = bgwl_server_focused_toplevel(server);
	if (toplevel == NULL || toplevel->fullscreen || toplevel->tiled) {
		return;
	}
	struct bgwl_output *target = output_under(server, toplevel);
	if (target == NULL) {
		return;
	}

	struct wlr_box box = target->usable;
	if (edge == WLR_EDGE_LEFT) {
		box.width = box.width / 2;
	} else if (edge == WLR_EDGE_RIGHT) {
		box.x += box.width / 2;
		box.width -= box.width / 2;
	} else if (edge == WLR_EDGE_TOP) {
		box.height = box.height / 2;
	} else if (edge == WLR_EDGE_BOTTOM) {
		box.y += box.height / 2;
		box.height -= box.height / 2;
	} else {
		return;
	}
	bgwl_toplevel_set_geometry(toplevel, box);
	toplevel->float_box = box;
	bgwl_ctl_broadcast(server, "windows");
}

/* Moves a window one slot through the tile order. */
static void move_focused_in_stack(struct bgwl_toplevel *toplevel,
		int direction) {
	struct bgwl_workspace *ws = toplevel->ws;
	if (ws == NULL || direction == 0) {
		return;
	}

	struct wl_list *next = direction > 0 ? toplevel->ws_link.next
		: toplevel->ws_link.prev;
	if (next == &ws->toplevels || next == NULL) {
		return;
	}
	struct bgwl_toplevel *neighbour = wl_container_of(next, neighbour, ws_link);
	wl_list_remove(&toplevel->ws_link);
	wl_list_insert(&neighbour->ws_link, &toplevel->ws_link);
	bgwl_reflow_workspace(ws);
	bgwl_ctl_broadcast(toplevel->server, "windows");
}

/* target == BGWL_LAYOUT_COUNT means "cycle"; anything else selects a layout. */
static void cycle_layout(struct bgwl_server *server, int target) {
	struct bgwl_workspace *ws = server->active_ws;
	if (ws == NULL) {
		return;
	}
	if (target >= (int)BGWL_LAYOUT_COUNT) {
		target = ((int)ws->layout + 1) % (int)BGWL_LAYOUT_COUNT;
	}
	if (target == (int)ws->layout) {
		return;
	}
	bgwl_workspace_set_layout(ws, (enum bgwl_layout)target);
	bgwl_ctl_broadcast(server, "workspace");
}

static void send_to_neighbour_output(struct bgwl_server *server,
		struct bgwl_toplevel *toplevel, int direction) {
	struct bgwl_output *current = output_under(server, toplevel);
	struct bgwl_output *target = bgwl_output_neighbour(server, current,
		direction);
	if (target == NULL) {
		return;
	}

	struct wlr_box frame;
	bgwl_toplevel_get_geometry(toplevel, &frame);
	struct wlr_box box = target->usable;
	box.width = bgwl_min(frame.width, box.width);
	box.height = bgwl_min(frame.height, box.height);
	bgwl_toplevel_set_geometry(toplevel, box);
	bgwl_reflow(server);
	bgwl_ctl_broadcast(server, "windows");
}

static void focused_window_action(struct bgwl_server *server,
		const char *action) {
	struct bgwl_toplevel *toplevel = bgwl_server_focused_toplevel(server);
	struct bgwl_workspace *ws = toplevel != NULL ? toplevel->ws
		: server->active_ws;

	/* Layout-level actions do not need a window; the rest do. */
	if (strcmp(action, "layout-next") == 0) {
		cycle_layout(server, BGWL_LAYOUT_COUNT);
		return;
	}
	if (strcmp(action, "layout-float") == 0) {
		cycle_layout(server, BGWL_LAYOUT_FLOAT);
		return;
	}
	if (strcmp(action, "layout-tile") == 0) {
		cycle_layout(server, BGWL_LAYOUT_TILE);
		return;
	}
	if (strcmp(action, "layout-even") == 0) {
		cycle_layout(server, BGWL_LAYOUT_EVEN);
		return;
	}
	if (strcmp(action, "split-rotate") == 0 && ws != NULL) {
		ws->split = ws->split == BGWL_SPLIT_RIGHT ? BGWL_SPLIT_BOTTOM
			: BGWL_SPLIT_RIGHT;
		bgwl_reflow_workspace(ws);
		bgwl_ctl_broadcast(server, "workspace");
		return;
	}
	if (strcmp(action, "master-grow") == 0 && ws != NULL) {
		bgwl_tiling_adjust_master(ws, 1);
		return;
	}
	if (strcmp(action, "master-shrink") == 0 && ws != NULL) {
		bgwl_tiling_adjust_master(ws, -1);
		return;
	}
	if (strcmp(action, "master-more") == 0 && ws != NULL) {
		bgwl_tiling_rotate_stack(ws, 1);
		return;
	}
	if (strcmp(action, "master-fewer") == 0 && ws != NULL) {
		bgwl_tiling_rotate_stack(ws, -1);
		return;
	}
	if (strcmp(action, "focus-next") == 0 && ws != NULL) {
		bgwl_workspace_cycle_focus(ws, 1);
		return;
	}
	if (strcmp(action, "focus-prev") == 0 && ws != NULL) {
		bgwl_workspace_cycle_focus(ws, -1);
		return;
	}

	if (toplevel == NULL) {
		return;
	}

	if (strcmp(action, "close") == 0) {
		bgwl_toplevel_close(toplevel);
	} else if (strcmp(action, "maximize") == 0) {
		bgwl_toplevel_set_maximized(toplevel, !toplevel->maximized);
	} else if (strcmp(action, "fullscreen") == 0) {
		bgwl_toplevel_set_fullscreen(toplevel, !toplevel->fullscreen);
	} else if (strcmp(action, "pin") == 0) {
		toplevel->above = !toplevel->above;
		bgwl_toplevel_raise(toplevel);
		bgwl_ctl_broadcast(server, "windows");
	} else if (strcmp(action, "sticky") == 0) {
		toplevel->sticky = !toplevel->sticky;
		bgwl_ctl_broadcast(server, "windows");
	} else if (strcmp(action, "move-next-slot") == 0) {
		move_focused_in_stack(toplevel, 1);
	} else if (strcmp(action, "move-prev-slot") == 0) {
		move_focused_in_stack(toplevel, -1);
	} else if (strcmp(action, "send-next") == 0) {
		int next = ws != NULL ? (ws->index + 1) % server->cfg.workspace_count
			: 1;
		bgwl_workspace_send_window(toplevel,
			bgwl_workspace_by_index(server, next));
	} else if (strcmp(action, "send-prev") == 0) {
		int prev = ws != NULL
			? (ws->index - 1 + server->cfg.workspace_count)
				% server->cfg.workspace_count
			: 0;
		bgwl_workspace_send_window(toplevel,
			bgwl_workspace_by_index(server, prev));
	} else if (strcmp(action, "snap-left") == 0) {
		snap_focused(server, WLR_EDGE_LEFT);
	} else if (strcmp(action, "snap-right") == 0) {
		snap_focused(server, WLR_EDGE_RIGHT);
	} else if (strcmp(action, "snap-top") == 0) {
		snap_focused(server, WLR_EDGE_TOP);
	} else if (strcmp(action, "snap-bottom") == 0) {
		snap_focused(server, WLR_EDGE_BOTTOM);
	} else if (strcmp(action, "window-next-output") == 0) {
		send_to_neighbour_output(server, toplevel, 1);
	} else if (strcmp(action, "window-prev-output") == 0) {
		send_to_neighbour_output(server, toplevel, -1);
	}
}

bool bgwl_handle_binding(struct bgwl_server *server, uint32_t modifiers,
		uint32_t keycode, bool pressed) {
	bool shift = (modifiers & WLR_MODIFIER_SHIFT) != 0;
	uint32_t without_locks = modifiers & ~(WLR_MODIFIER_CAPS
		| WLR_MODIFIER_MOD2 | WLR_MODIFIER_MOD5);

	for (int i = 0; i < server->cfg.binding_count; i++) {
		struct bgwl_binding *binding = &server->cfg.bindings[i];
		if (binding->keycode == keycode &&
				(binding->modifiers & ~WLR_MODIFIER_CAPS) == modifiers) {
			if (pressed) {
				focused_window_action(server, binding->action);
				bgwl_run_action(server, binding->action);
			}
			return true;
		}
	}

	for (size_t i = 0;
			i < sizeof(builtin_bindings) / sizeof(builtin_bindings[0]); i++) {
		if (builtin_bindings[i].keycode == keycode &&
				builtin_bindings[i].modifiers == without_locks) {
			if (pressed) {
				const char *action = builtin_bindings[i].action;
				if (strcmp(action, "exit") == 0) {
					bgwl_server_terminate(server);
				} else if (strcmp(action, "panel") == 0) {
					bgwl_panel_toggle(server);
				} else if (strcmp(action, "launcher") == 0) {
					bgwl_spawn(server, server->cfg.launcher);
				} else {
					focused_window_action(server, action);
					bgwl_run_action(server, action);
				}
			}
			return true;
		}
	}

	/* Super+digit switches a stream, Super+Shift+digit sends the window there. */
	int workspace_index = workspace_for_keycode(keycode);
	if (workspace_index >= 0 && (without_locks == WLR_MODIFIER_LOGO ||
			without_locks == (WLR_MODIFIER_LOGO | WLR_MODIFIER_SHIFT))) {
		struct bgwl_workspace *target = bgwl_workspace_by_index(server,
			workspace_index);
		if (target != NULL) {
			if (pressed) {
				if (shift) {
					bgwl_workspace_send_window(
						bgwl_server_focused_toplevel(server), target);
				} else {
					bgwl_workspace_switch(server, target, true);
				}
			}
			return true;
		}
	}

	if (without_locks == WLR_MODIFIER_LOGO && keycode == KEY_TAB) {
		if (pressed && server->active_ws != NULL) {
			int next = (server->active_ws->index + (shift ? -1 : 1)
				+ server->cfg.workspace_count) % server->cfg.workspace_count;
			bgwl_workspace_switch(server,
				bgwl_workspace_by_index(server, next), true);
		}
		return true;
	}

	return false;
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

	/* The call re-applies the mask to the keyboard's xkb_state and emits the
	 * modifiers event, which our modifiers listener forwards to the client. */
	wlr_keyboard_notify_modifiers(wlr_keyboard,
		wlr_keyboard->modifiers.depressed, wlr_keyboard->modifiers.latched,
		wlr_keyboard->modifiers.locked, next);

	bgwl_ctl_broadcast(keyboard->server, "layout");
	const char *name = xkb_keymap_layout_get_name(keymap, next);
	wlr_log(WLR_INFO, "keyboard layout switched to %s (index %d/%d)",
		name ? name : "?", (int)next, (int)count);
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

	bgwl_session_activity(server);

	/* A locked session hands every key to the lock surface and consumes
	 * nothing else: a keybinding that worked through the lock screen would
	 * defeat the lock. */
	if (bgwl_server_is_locked(server)) {
		wlr_seat_set_keyboard(seat, keyboard->wlr_keyboard);
		wlr_seat_keyboard_notify_key(seat, event->time_msec, event->keycode,
			event->state);
		return;
	}

	uint32_t modifiers = wlr_keyboard_get_modifiers(keyboard->wlr_keyboard);
	if (bgwl_handle_binding(server, modifiers, event->keycode,
			event->state == WL_KEYBOARD_KEY_STATE_PRESSED)) {
		/* Consume both press and release so clients never see half a
		 * shortcut. A release that arrives after Shift was let go no longer
		 * matches the combo and is forwarded — harmless, and the alternative
		 * is a keymap-wide state machine bgwl does not need. */
		return;
	}

	wlr_seat_set_keyboard(seat, keyboard->wlr_keyboard);
	wlr_seat_keyboard_notify_key(seat, event->time_msec, event->keycode,
		event->state);
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
	/* Returns void: wlr_cursor only validates the device type internally and
	 * logs if the device is already attached. */
	wlr_cursor_attach_input_device(server->cursor, device);
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
	case WLR_INPUT_DEVICE_SWITCH:
		/* Laptop lid: session.c owns the decision between lock and suspend. */
		bgwl_session_add_switch(server, device);
		break;
	default:
		/* Touch and tablet are attached through the same cursor path once a
		 * seat needs them; they are not v1.0 blockers. */
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

/* Cursor shape for the pointer's current position, so resizing is discoverable
 * without reading a manual. */
static void set_cursor_shape(struct bgwl_server *server, uint32_t edges) {
	const char *name = "left_ptr";
	bool left = edges & WLR_EDGE_LEFT;
	bool right = edges & WLR_EDGE_RIGHT;
	bool top = edges & WLR_EDGE_TOP;
	bool bottom = edges & WLR_EDGE_BOTTOM;

	if (left && top) {
		name = "top_left_corner";
	} else if (right && bottom) {
		name = "bottom_right_corner";
	} else if (right && top) {
		name = "top_right_corner";
	} else if (left && bottom) {
		name = "bottom_left_corner";
	} else if (left || right) {
		name = "col-resize";
	} else if (top || bottom) {
		name = "row-resize";
	}
	wlr_cursor_set_xcursor(server->cursor, server->cursor_mgr, name);
}

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
	uint32_t edges = toplevel != NULL
		? bgwl_toplevel_resize_edges_at(toplevel, server->cursor->x,
			server->cursor->y)
		: 0;

	if (bgwl_server_is_locked(server)) {
		/* Only the lock surface is reachable while locked. */
		if (surface == NULL) {
			wlr_seat_pointer_clear_focus(seat);
			return;
		}
		set_cursor_shape(server, 0);
		wlr_seat_pointer_notify_enter(seat, surface, sx, sy);
		wlr_seat_pointer_notify_motion(seat, time, sx, sy);
		return;
	}

	set_cursor_shape(server, edges);

	if (surface == NULL) {
		/* Over the background, a titlebar or nothing at all: the shape above
		 * already picked the right cursor for the frame. */
		wlr_seat_pointer_clear_focus(seat);
	} else {
		wlr_seat_pointer_notify_enter(seat, surface, sx, sy);
		wlr_seat_pointer_notify_motion(seat, time, sx, sy);
	}

	/* Focus follows the mouse, but only across toplevels: sliding the pointer
	 * over a panel or the wallpaper must not steal the keyboard. */
	if (toplevel != NULL && server->cfg.focus_follows_mouse) {
		bgwl_toplevel_focus(toplevel);
	}
}

void bgwl_cursor_motion(struct wl_listener *listener, void *data) {
	struct bgwl_server *server = wl_container_of(listener, server, cursor_motion);
	struct wlr_pointer_motion_event *event = data;

	bgwl_session_activity(server);
	wlr_cursor_move(server->cursor, &event->pointer->base, event->delta_x,
		event->delta_y);
	process_cursor_motion(server, event->time_msec);
}

void bgwl_cursor_motion_absolute(struct wl_listener *listener, void *data) {
	struct bgwl_server *server =
		wl_container_of(listener, server, cursor_motion_absolute);
	struct wlr_pointer_motion_absolute_event *event = data;

	bgwl_session_activity(server);
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

	bgwl_session_activity(server);

	if (bgwl_server_is_locked(server)) {
		wlr_seat_pointer_notify_button(server->seat, event->time_msec,
			event->button, event->state);
		return;
	}

	wlr_seat_pointer_notify_button(server->seat, event->time_msec,
		event->button, event->state);

	if (event->state == WL_POINTER_BUTTON_STATE_RELEASED) {
		bgwl_toplevel_reset_cursor_mode(server);
		return;
	}

	struct bgwl_toplevel *toplevel = bgwl_toplevel_at(server, server->cursor->x,
		server->cursor->y);

	/* Alt+drag: left button moves, right button resizes, from anywhere in the
	 * window. This is how a tiled window gets moved without leaving the grid,
	 * and how a border too thin to hit is still grabbable. */
	struct wlr_keyboard *keyboard = wlr_seat_get_keyboard(server->seat);
	uint32_t modifiers = keyboard != NULL
		? wlr_keyboard_get_modifiers(keyboard) : 0;
	if ((modifiers & WLR_MODIFIER_ALT) != 0 && toplevel != NULL) {
		if (event->button == BTN_LEFT) {
			bgwl_toplevel_focus(toplevel);
			bgwl_toplevel_begin_interactive(toplevel, BGWL_CURSOR_MOVE, 0);
		} else if (event->button == BTN_RIGHT) {
			uint32_t edges = bgwl_toplevel_resize_edges_at(toplevel,
				server->cursor->x, server->cursor->y);
			if (edges == 0) {
				edges = WLR_EDGE_RIGHT | WLR_EDGE_BOTTOM;
			}
			bgwl_toplevel_begin_interactive(toplevel, BGWL_CURSOR_RESIZE, edges);
		}
		return;
	}

	if (toplevel == NULL) {
		return;
	}

	bgwl_toplevel_focus(toplevel);

	/* A press that reaches the window but not any client surface landed on
	 * our own frame: a button, a resize border, or the titlebar. */
	struct wlr_surface *surface = bgwl_surface_at(server, server->cursor->x,
		server->cursor->y, NULL, NULL);
	if (surface != NULL) {
		return;
	}

	switch (bgwl_toplevel_button_at(toplevel, server->cursor->x,
			server->cursor->y)) {
	case BGWL_BTN_CLOSE:
		bgwl_toplevel_close(toplevel);
		return;
	case BGWL_BTN_MAXIMIZE:
		bgwl_toplevel_set_maximized(toplevel, !toplevel->maximized);
		return;
	case BGWL_BTN_MENU:
		toplevel->above = !toplevel->above;
		bgwl_toplevel_raise(toplevel);
		bgwl_ctl_broadcast(server, "windows");
		return;
	default:
		break;
	}

	uint32_t edges = bgwl_toplevel_resize_edges_at(toplevel, server->cursor->x,
		server->cursor->y);
	if (edges != 0) {
		bgwl_toplevel_begin_interactive(toplevel, BGWL_CURSOR_RESIZE, edges);
	} else {
		bgwl_toplevel_begin_interactive(toplevel, BGWL_CURSOR_MOVE, 0);
	}
}

void bgwl_cursor_axis(struct wl_listener *listener, void *data) {
	struct bgwl_server *server = wl_container_of(listener, server, cursor_axis);
	struct wlr_pointer_axis_event *event = data;

	bgwl_session_activity(server);
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
	struct bgwl_server *server =
		wl_container_of(listener, server, request_cursor);
	struct wlr_seat_pointer_request_set_cursor_event *event = data;
	struct wlr_seat_client *focused_client =
		server->seat->pointer_state.focused_client;

	/* Only honour a request from the client that currently holds pointer
	 * focus; otherwise an unfocused window could change the cursor. */
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
