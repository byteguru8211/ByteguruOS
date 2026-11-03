/*
 * xdg-shell: new toplevels and popups. The window state itself lives in
 * view.c so that the decoration code stays in one place.
 */
#include <stdlib.h>

#include <wayland-server-core.h>
#include <wlr/types/wlr_scene.h>
#include <wlr/types/wlr_xdg_shell.h>
#include <wlr/util/log.h>

#include "bgwl.h"

void bgwl_new_xdg_toplevel(struct wl_listener *listener, void *data) {
	struct bgwl_server *server =
		wl_container_of(listener, server, new_xdg_toplevel);
	struct wlr_xdg_toplevel *xdg_toplevel = data;

	/* Creation failure is logged inside; a toplevel we cannot decorate is
	 * still usable if the scene allocation failed part-way, and wlroots will
	 * destroy the client resource on its own if we return. */
	if (bgwl_toplevel_create(server, xdg_toplevel) == NULL) {
		wlr_log(WLR_ERROR, "dropping xdg toplevel: allocation failed");
		wlr_xdg_toplevel_send_close(xdg_toplevel);
	}
}

static void popup_handle_commit(struct wl_listener *listener, void *data) {
	struct bgwl_popup *popup = wl_container_of(listener, popup, commit);

	if (popup->xdg_popup->base->initial_commit) {
		/* An empty configure is the legal reply: the popup keeps the size it
		 * asked for and bgwl never repositions it. */
		wlr_xdg_surface_schedule_configure(popup->xdg_popup->base);
	}
}

static void popup_handle_destroy(struct wl_listener *listener, void *data) {
	struct bgwl_popup *popup = wl_container_of(listener, popup, destroy);

	wl_list_remove(&popup->commit.link);
	wl_list_remove(&popup->destroy.link);
	free(popup);
}

void bgwl_new_xdg_popup(struct wl_listener *listener, void *data) {
	struct wlr_xdg_popup *xdg_popup = data;

	struct bgwl_popup *popup = calloc(1, sizeof(*popup));
	if (popup == NULL) {
		wlr_log(WLR_ERROR, "out of memory for xdg popup");
		return;
	}
	popup->xdg_popup = xdg_popup;

	struct wlr_xdg_surface *parent =
		wlr_xdg_surface_try_from_wlr_surface(xdg_popup->parent);
	if (parent == NULL) {
		/* Not a role we handed out a scene node for; drop the popup rather
		 * than assert on client behaviour. */
		wlr_log(WLR_ERROR, "xdg popup with non-xdg parent");
		free(popup);
		return;
	}
	struct wlr_scene_tree *parent_tree = parent->data;
	if (parent_tree == NULL) {
		wlr_log(WLR_ERROR, "xdg popup parent has no scene node");
		free(popup);
		return;
	}

	xdg_popup->base->data = wlr_scene_xdg_surface_create(parent_tree,
		xdg_popup->base);

	popup->commit.notify = popup_handle_commit;
	wl_signal_add(&xdg_popup->base->surface->events.commit, &popup->commit);
	popup->destroy.notify = popup_handle_destroy;
	wl_signal_add(&xdg_popup->events.destroy, &popup->destroy);
}
