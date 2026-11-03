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

static void layer_surface_reconfigure(struct bgwl_layer_surface *ls) {
	struct bgwl_server *server = ls->server;
	struct wlr_layer_surface_v1 *layer_surface = ls->layer_surface;
	struct wlr_output *wlr_output = layer_surface->output;
	if (wlr_output == NULL) {
		return;
	}

	/* The scene helper works in layout coordinates, so hand it the output's
	 * box in the layout; exclusive zones then shrink the usable area for the
	 * next surface configured in the same pass. */
	struct wlr_box full_area;
	wlr_output_layout_get_box(server->output_layout, wlr_output, &full_area);
	if (full_area.width <= 0 || full_area.height <= 0) {
		int width = 0, height = 0;
		wlr_output_effective_resolution(wlr_output, &width, &height);
		full_area.x = 0;
		full_area.y = 0;
		full_area.width = width;
		full_area.height = height;
	}

	/* ВЕРИФИЦИРОВАТЬ: wlr_scene_layer_surface_v1_configure() is expected to call
	 * wlr_layer_surface_v1_configure() itself (types/scene/layer_shell_v1.c).
	 * If a wlroots version stops doing that, the panel will never map and we
	 * must send the configure here. Also: map/unmap are taken from
	 * wlr_surface.events, because struct wlr_layer_surface_v1 has no map
	 * signal of its own in 0.19.3. */
	struct wlr_box usable_area = full_area;
	wlr_scene_layer_surface_v1_configure(ls->scene_layer_surface, &full_area,
		&usable_area);
}

static void layer_surface_handle_commit(struct wl_listener *listener,
		void *data) {
	struct bgwl_layer_surface *ls =
		wl_container_of(listener, ls, commit);

	if (ls->layer_surface->initial_commit) {
		/* The client is waiting for its first configure; without it the
		 * surface never maps. Later commits are not re-configured: bgwl v0
		 * does not track exclusive-zone ordering, and re-configuring on every
		 * commit would ping-pong with the panel. */
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

	if (ls->server->panel == ls) {
		ls->server->panel = NULL;
		ls->server->panel_visible = false;
	}
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
