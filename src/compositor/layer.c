/*
 * wlr_layer_shell_v1: the ByteguruOS panel and any other layer surface.
 *
 * Scope discipline: bgwl does not implement panel behaviour. It maps layer
 * surfaces into the matching scene tree, configures them once, and lets
 * Super+D enable/disable the one surface whose namespace starts with
 * BGWL_PANEL_NAMESPACE_PREFIX.
 *
 * "Toggle" here means wlr_scene_node_set_enabled() on the layer surface's
 * scene tree, not wlroots' map/unmap: a compositor cannot unmap a client
 * surface behind the client's back, and calling wlr_layer_surface_v1_destroy()
 * to fake an unmap would close the client's resource for good. Enabling the
 * node keeps the panel process alive and its buffer warm, which is what the
 * panel component expects.
 */
#include <stdlib.h>
#include <string.h>

#include <wayland-server-core.h>
#include <wlr/types/wlr_layer_shell_v1.h>
#include <wlr/types/wlr_output.h>
#include <wlr/types/wlr_output_layout.h>
#include <wlr/types/wlr_scene.h>
#include <wlr/types/wlr_xdg_shell.h>
#include <wlr/util/log.h>

#include "bgwl.h"

static int tree_index_for_layer(enum zwlr_layer_shell_v1_layer layer) {
	switch (layer) {
	case ZWLR_LAYER_SHELL_V1_LAYER_BACKGROUND:
		return BGWL_TREE_BACKGROUND;
	case ZWLR_LAYER_SHELL_V1_LAYER_BOTTOM:
		return BGWL_TREE_BOTTOM;
	case ZWLR_LAYER_SHELL_V1_LAYER_TOP:
		return BGWL_TREE_TOP;
	case ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY:
		return BGWL_TREE_OVERLAY;
	default:
		return BGWL_TREE_TOP;
	}
}

/*
 * Every layer surface on an output is reconfigured together, in protocol layer
 * order, because exclusive zones stack: a panel and a dock on the same edge
 * must not both claim the pixels, and the only way to get that right is to
 * hand each surface the area the previous ones left.
 *
 * The scene helper does the arithmetic (wlr_scene_layer_surface_v1_configure
 * shrinks the usable area by this surface's exclusive zone and sends the
 * configure), so bgwl only has to keep the order and the running rectangle.
 */
void bgwl_layer_update_usable_areas(struct bgwl_server *server) {
	struct bgwl_output *bg_output;
	wl_list_for_each(bg_output, &server->outputs, link) {
		struct wlr_box full_area;
		wlr_output_layout_get_box(server->output_layout,
			bg_output->wlr_output, &full_area);
		if (full_area.width <= 0 || full_area.height <= 0) {
			int width = 0, height = 0;
			wlr_output_effective_resolution(bg_output->wlr_output,
				&width, &height);
			full_area.x = 0;
			full_area.y = 0;
			full_area.width = width;
			full_area.height = height;
		}

		struct wlr_box usable = full_area;
		for (int layer = 0; layer <= ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY;
				layer++) {
			struct bgwl_layer_surface *ls;
			wl_list_for_each(ls, &server->layer_surfaces, link) {
				if (ls->layer_surface->output != bg_output->wlr_output) {
					continue;
				}
				if ((int)ls->layer_surface->current.layer != layer) {
					continue;
				}
				if (ls->scene_layer_surface == NULL) {
					continue;
				}
				wlr_scene_layer_surface_v1_configure(
					ls->scene_layer_surface, &full_area, &usable);
			}
		}
		bg_output->usable = usable;
	}

	/* The desktop geometry only matters because the window layout follows it. */
	bgwl_reflow(server);
}

static void layer_surface_reconfigure(struct bgwl_layer_surface *ls) {
	bgwl_layer_update_usable_areas(ls->server);
}

static void layer_surface_handle_commit(struct wl_listener *listener,
		void *data) {
	struct bgwl_layer_surface *ls =
		wl_container_of(listener, ls, commit);

	/* initial_commit must be answered or the surface never maps. After that,
	 * re-run the pass only when something the usable area depends on changed
	 * — exclusive zone, anchors, size or layer — so a panel repainting its
	 * clock does not reflow every window on the desktop. */
	if (ls->layer_surface->initial_commit) {
		layer_surface_reconfigure(ls);
		return;
	}

	const struct wlr_layer_surface_v1_state *current =
		&ls->layer_surface->current;
	const struct wlr_layer_surface_v1_state *pending =
		&ls->layer_surface->pending;
	bool geometry_changed =
		current->exclusive_zone != pending->exclusive_zone ||
		current->anchor != pending->anchor ||
		current->desired_width != pending->desired_width ||
		current->desired_height != pending->desired_height ||
		current->layer != pending->layer;
	if (geometry_changed) {
		layer_surface_reconfigure(ls);
	}
}

static void layer_surface_handle_map(struct wl_listener *listener, void *data) {
	struct bgwl_layer_surface *ls = wl_container_of(listener, ls, map);
	struct bgwl_server *server = ls->server;

	if (!ls->is_panel) {
		return;
	}
	if (server->panel != NULL && server->panel != ls) {
		wlr_log(WLR_INFO, "replacing the tracked panel surface");
	}
	server->panel = ls;
	server->panel_visible = true;
	wlr_log(WLR_INFO, "panel mapped in layer %d",
		(int)ls->layer_surface->current.layer);
}

static void layer_surface_handle_unmap(struct wl_listener *listener, void *data) {
	struct bgwl_layer_surface *ls =
		wl_container_of(listener, ls, unmap);

	wl_list_remove(&ls->link);
	wl_list_init(&ls->link);
	if (ls->server->panel == ls) {
		ls->server->panel = NULL;
		ls->server->panel_visible = false;
	}
	bgwl_layer_update_usable_areas(ls->server);
}

static void layer_surface_handle_new_popup(struct wl_listener *listener,
		void *data) {
	struct bgwl_layer_surface *ls =
		wl_container_of(listener, ls, new_popup);
	struct wlr_xdg_popup *xdg_popup = data;

	if (ls->scene_layer_surface == NULL) {
		return;
	}
	bgwl_popup_create(xdg_popup, ls->scene_layer_surface->tree);
}

static void layer_surface_handle_destroy(struct wl_listener *listener,
		void *data) {
	struct bgwl_layer_surface *ls =
		wl_container_of(listener, ls, destroy);
	struct bgwl_server *server = ls->server;

	if (server->panel == ls) {
		server->panel = NULL;
		server->panel_visible = false;
	}
	wl_list_remove(&ls->link);
	wl_list_init(&ls->link);

	wl_list_remove(&ls->map.link);
	wl_list_remove(&ls->unmap.link);
	wl_list_remove(&ls->commit.link);
	wl_list_remove(&ls->new_popup.link);
	wl_list_remove(&ls->destroy.link);

	/* wlr_scene_layer_surface_v1 registers its own destroy listeners on both
	 * the scene tree and the layer surface, so the node it created is torn
	 * down for us — touching it here would double-destroy. */
	ls->scene_layer_surface = NULL;
	ls->layer_surface->data = NULL;
	bgwl_layer_update_usable_areas(server);

	free(ls);
}

void bgwl_new_layer_surface(struct wl_listener *listener, void *data) {
	struct bgwl_server *server =
		wl_container_of(listener, server, new_layer_surface);
	struct wlr_layer_surface_v1 *layer_surface = data;

	if (layer_surface->output == NULL) {
		/* The protocol allows an unanchored surface; put it on the first
		 * output rather than dropping it. */
		struct bgwl_output *first = bgwl_server_first_output(server);
		if (first == NULL) {
			wlr_log(WLR_ERROR,
				"layer surface '%s' requested an output but none exists",
				layer_surface->namespace ? layer_surface->namespace : "");
			wlr_layer_surface_v1_destroy(layer_surface);
			return;
		}
		layer_surface->output = first->wlr_output;
	}

	struct bgwl_layer_surface *ls = calloc(1, sizeof(*ls));
	if (ls == NULL) {
		wlr_log(WLR_ERROR, "out of memory for layer surface");
		wlr_layer_surface_v1_destroy(layer_surface);
		return;
	}
	ls->server = server;
	ls->layer_surface = layer_surface;
	wl_list_init(&ls->link);
	/* Every layer surface is tracked, not only the panel: exclusive zones are
	 * computed across all of them, so a dock or a notification strip has to be
	 * in the same pass. */
	wl_list_insert(&server->layer_surfaces, &ls->link);

	const char *namespace = layer_surface->namespace;
	ls->is_panel = namespace != NULL &&
		strncmp(namespace, BGWL_PANEL_NAMESPACE_PREFIX,
			strlen(BGWL_PANEL_NAMESPACE_PREFIX)) == 0;

	struct wlr_scene_tree *parent =
		server->trees[tree_index_for_layer(layer_surface->pending.layer)];
	ls->scene_layer_surface = wlr_scene_layer_surface_v1_create(parent,
		layer_surface);
	if (ls->scene_layer_surface == NULL) {
		wlr_log(WLR_ERROR, "wlr_scene_layer_surface_v1_create failed");
		free(ls);
		wlr_layer_surface_v1_destroy(layer_surface);
		return;
	}

	layer_surface->data = ls;

	struct wlr_surface *surface = layer_surface->surface;
	ls->map.notify = layer_surface_handle_map;
	wl_signal_add(&surface->events.map, &ls->map);
	ls->unmap.notify = layer_surface_handle_unmap;
	wl_signal_add(&surface->events.unmap, &ls->unmap);
	ls->commit.notify = layer_surface_handle_commit;
	wl_signal_add(&surface->events.commit, &ls->commit);
	ls->destroy.notify = layer_surface_handle_destroy;
	wl_signal_add(&layer_surface->events.destroy, &ls->destroy);
	ls->new_popup.notify = layer_surface_handle_new_popup;
	wl_signal_add(&layer_surface->events.new_popup, &ls->new_popup);

	wlr_log(WLR_INFO, "layer surface '%s' created (namespace '%s')",
		layer_surface->output->name, namespace ? namespace : "");
}

void bgwl_panel_toggle(struct bgwl_server *server) {
	if (server->panel == NULL || server->panel->scene_layer_surface == NULL) {
		wlr_log(WLR_INFO,
			"no panel to toggle: the ByteguruOS panel owns layer-shell and "
			"must use the namespace prefix '%s'",
			BGWL_PANEL_NAMESPACE_PREFIX);
		return;
	}

	server->panel_visible = !server->panel_visible;
	wlr_scene_node_set_enabled(
		&server->panel->scene_layer_surface->tree->node,
		server->panel_visible);
	wlr_log(WLR_INFO, "panel %s", server->panel_visible ? "shown" : "hidden");
}
