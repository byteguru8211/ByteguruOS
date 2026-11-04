/*
 * Wayland protocol globals.
 *
 * Each global here exists because a specific piece of the desktop does not
 * work without it — this file is not a checklist. The reasoning is recorded
 * next to each one because "why is this advertised?" comes up every time the
 * surface area is reviewed.
 */
#include <stdlib.h>

#include <wayland-server-core.h>
#include <wlr/backend.h>
#include <wlr/render/wlr_renderer.h>
#include <wlr/types/wlr_alpha_modifier_v1.h>
#include <wlr/types/wlr_content_type_v1.h>
#include <wlr/types/wlr_cursor_shape_v1.h>
#include <wlr/types/wlr_data_control_v1.h>
#include <wlr/types/wlr_ext_foreign_toplevel_list_v1.h>
#include <wlr/types/wlr_foreign_toplevel_management_v1.h>
#include <wlr/types/wlr_fractional_scale_v1.h>
#include <wlr/types/wlr_fullscreen_shell_v1.h>
#include <wlr/types/wlr_gamma_control_v1.h>
#include <wlr/types/wlr_idle_inhibit_v1.h>
#include <wlr/types/wlr_idle_notify_v1.h>
#include <wlr/types/wlr_input_method_v2.h>
#include <wlr/types/wlr_linux_dmabuf_v1.h>
#include <wlr/types/wlr_output_management_v1.h>
#include <wlr/types/wlr_pointer_constraints_v1.h>
#include <wlr/types/wlr_presentation_time.h>
#include <wlr/types/wlr_primary_selection_v1.h>
#include <wlr/types/wlr_relative_pointer_v1.h>
#include <wlr/types/wlr_screencopy_v1.h>
#include <wlr/types/wlr_security_context_v1.h>
#include <wlr/types/wlr_session_lock_v1.h>
#include <wlr/types/wlr_single_pixel_buffer_v1.h>
#include <wlr/types/wlr_tearing_control_v1.h>
#include <wlr/types/wlr_text_input_v3.h>
#include <wlr/types/wlr_virtual_keyboard_v1.h>
#include <wlr/types/wlr_virtual_pointer_v1.h>
#include <wlr/types/wlr_viewporter.h>
#include <wlr/types/wlr_xdg_decoration_v1.h>
#include <wlr/types/wlr_xdg_output_v1.h>
#include <wlr/types/wlr_xdg_shell.h>
#include <wlr/util/log.h>

#include "bgwl.h"

static void decoration_handle_new(struct wl_listener *listener, void *data) {
	struct bgwl_server *server =
		wl_container_of(listener, server, new_decoration);
	struct wlr_xdg_toplevel_decoration_v1 *decoration = data;

	/* Decide now and never negotiate afterwards: an undecided decoration
	 * leaves the client drawing a titlebar on top of ours. */
	enum wlr_xdg_toplevel_decoration_v1_mode mode =
		server->cfg.decorate_by_default
			? WLR_XDG_TOPLEVEL_DECORATION_V1_MODE_SERVER_SIDE
			: WLR_XDG_TOPLEVEL_DECORATION_V1_MODE_CLIENT_SIDE;

	if (decoration->requested_mode != WLR_XDG_TOPLEVEL_DECORATION_V1_MODE_NONE) {
		mode = decoration->requested_mode;
	}
	wlr_xdg_toplevel_decoration_v1_set_mode(decoration, mode);

	bool server_side = mode == WLR_XDG_TOPLEVEL_DECORATION_V1_MODE_SERVER_SIDE;
	struct wlr_scene_tree *tree = decoration->toplevel->base->data;
	struct bgwl_toplevel *toplevel = tree != NULL ? tree->node.data : NULL;
	if (toplevel != NULL) {
		bgwl_toplevel_set_decorated(toplevel, server_side);
	}
}

bool bgwl_protocols_create(struct bgwl_server *server) {
	struct wl_display *display = server->wl_display;

	/* Cursor shapes: GTK4 and Qt ask for "grabbing"/"text" by name. Without
	 * this global every pointer in the desktop stays an arrow. */
	if (wlr_cursor_shape_manager_v1_create(display, 1) == NULL) {
		wlr_log(WLR_ERROR, "no cursor-shape: GTK4 and Qt will draw an arrow");
	}

	/* Presentation feedback: without it Chromium/Firefox time their
	 * animations against the wrong clock and feel broken. */
	if (wlr_presentation_create(display, server->backend) == NULL) {
		wlr_log(WLR_ERROR, "no presentation feedback: browser animations "
			"will mis-time themselves");
	}

	/* Zero-copy via dmabuf. Version 2 is what the 0.18 helper needs in order
	 * to advertise the renderer's supported formats and modifiers itself. */
	/* dmabuf needs a DRM file descriptor from the renderer. A pixman software
	 * renderer — headless CI, WLR_RENDERER=pixman, a VM without 3D — has none,
	 * and that must not end the session: clients simply fall back to shared
	 * memory, which is slower but correct. */
	if (wlr_linux_dmabuf_v1_create_with_renderer(display, 2,
			server->renderer) == NULL) {
		wlr_log(WLR_INFO, "no dmabuf support (software renderer?); "
			"clients will use shared memory");
	}

	/* Buffer scaling: viewporter + fractional scale is what makes a Wayland
	 * client render at the output's real scale instead of being blurred. */
	if (wlr_viewporter_create(display) == NULL ||
			wlr_fractional_scale_manager_v1_create(display, 1) == NULL ||
			wlr_single_pixel_buffer_manager_v1_create(display) == NULL) {
		wlr_log(WLR_ERROR, "scaling protocols unavailable: HiDPI will be "
			"blurry rather than sharp");
	}

	/* The panel, launcher and notifications are layer-shell clients; the
	 * clipboard manager needs data-control; GNOME-style apps look for the
	 * toplevel list. */
	if (wlr_data_control_manager_v1_create(display) == NULL ||
			wlr_primary_selection_v1_device_manager_create(display) == NULL ||
			wlr_ext_foreign_toplevel_list_v1_create(display, 1) == NULL) {
		wlr_log(WLR_ERROR, "clipboard/toplevel globals unavailable");
	}

	/* Sandboxed apps (Flatpak is in the ByteguruOS image) refuse to start
	 * unless the compositor says which security context their client is in. */
	if (wlr_security_context_manager_v1_create(display) == NULL) {
		/* Sandboxed Flatpak apps refuse to start without this, which is the
		 * one failure a user notices as "my apps are gone". Loud, but not
		 * fatal: the rest of the desktop still works. */
		wlr_log(WLR_ERROR, "no security-context: Flatpak apps will not start");
	}

	/* Portals: screenshot and screen-capture go through screencopy, output
	 * configuration through output-management, brightness through gamma.
	 *
	 * wlr-output-power-management-v1 is not advertised: its XML
	 * lives in wlroots rather than wayland-protocols, so a build would have to
	 * vendor a protocol file for no user-visible gain — bgwl already blanks
	 * its own outputs through DRM (session.c), which is the same effect. */
	if (wlr_screencopy_manager_v1_create(display) == NULL ||
			wlr_gamma_control_manager_v1_create(display) == NULL) {
		wlr_log(WLR_ERROR, "screencopy/gamma unavailable: portals will refuse "
			"to capture the screen");
	}
	server->output_manager = wlr_output_manager_v1_create(display);
	if (server->output_manager == NULL) {
		wlr_log(WLR_ERROR, "no output-management: display settings are read-only");
	}

	/* Lock screen and idle: bgwl owns both (session.c), so these are not
	 * decorative. ext-idle-notify replaces the removed wlr_idle protocol. */
	server->lock_manager = wlr_session_lock_manager_v1_create(display);
	server->idle_notifier = wlr_idle_notifier_v1_create(display);
	server->idle_inhibit = wlr_idle_inhibit_v1_create(display);
	if (server->lock_manager == NULL || server->idle_notifier == NULL ||
			server->idle_inhibit == NULL) {
		/* The one case worth refusing to start for: a desktop that cannot
		 * lock is a desktop that cannot be left unattended. */
		wlr_log(WLR_ERROR, "lock/idle globals unavailable; the session cannot "
			"be locked");
		return false;
	}

	/* On-screen keyboard (the shell ships one) needs a virtual keyboard, and
	 * a virtual pointer for remote-control clients. */
	if (wlr_virtual_keyboard_manager_v1_create(display) == NULL ||
			wlr_virtual_pointer_manager_v1_create(display) == NULL) {
		wlr_log(WLR_ERROR, "virtual input unavailable: the on-screen keyboard "
			"will not work");
	}

	/* Text input + input method: without text-input-v3 GTK3 and Qt compose
	 * input methods badly; the IM v2 manager is what fcitx/ibus need. */
	if (wlr_text_input_manager_v3_create(display) == NULL ||
			wlr_input_method_manager_v2_create(display) == NULL) {
		wlr_log(WLR_ERROR, "text-input/input-method unavailable");
	}

	/* Games and video: relative motion, locked cursors, and the low-latency
	 * tearing flag. All three are cheap to advertise and costly to miss. */
	if (wlr_relative_pointer_manager_v1_create(display) == NULL ||
			wlr_pointer_constraints_v1_create(display) == NULL ||
			wlr_tearing_control_manager_v1_create(display, 1) == NULL) {
		wlr_log(WLR_ERROR, "pointer constraint/tearing unavailable: games may "
			"not grab the cursor");
	}

	/* Content types let clients tell us "this is a video" so the compositor
	 * can pick the right presentation hints; alpha-modifier covers legacy
	 * transparency requests; fullscreen-shell is used by kiosk-style apps. */
	if (wlr_content_type_manager_v1_create(display, 1) == NULL ||
			wlr_alpha_modifier_v1_create(display) == NULL ||
			wlr_fullscreen_shell_v1_create(display) == NULL) {
		wlr_log(WLR_ERROR, "content-type/alpha/fullscreen-shell unavailable");
	}

	/* Window lists in the panel read foreign-toplevel-management; xdg-output
	 * gives clients their output name and logical size. */
	if (wlr_foreign_toplevel_manager_v1_create(display) == NULL ||
			wlr_xdg_output_manager_v1_create(display, server->output_layout) == NULL) {
		wlr_log(WLR_ERROR, "foreign-toplevel/xdg-output unavailable");
	}

	server->xdg_decoration = wlr_xdg_decoration_manager_v1_create(display);
	if (server->xdg_decoration == NULL) {
		/* Without it every CSD client draws its own titlebar on top of ours,
		 * which is a visible defect rather than a degraded feature. */
		wlr_log(WLR_ERROR, "no xdg-decoration: windows may be double-framed");
	}
	server->new_decoration.notify = decoration_handle_new;
	wl_signal_add(&server->xdg_decoration->events.new_toplevel_decoration,
		&server->new_decoration);

	return true;
}
