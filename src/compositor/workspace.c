/*
 * Workspaces — in ByteguruOS they are called Потоки (Streams).
 *
 * Design choice that drives everything else in this file: a workspace owns a
 * scene tree, and its windows are children of that tree. Switching a
 * workspace is therefore one wlr_scene_node_set_enabled() call plus a slide,
 * not a loop over every window. Two consequences worth stating:
 *
 * - Cost: switching is O(1) regardless of how many windows are open, which is
 *   why this runs acceptably on the 2 GiB hardware the image targets.
 * - Constraint: a window can only be visible on one workspace at a time,
 *   because a scene node has exactly one parent. "Sticky" windows are
 *   re-parented on every switch instead.
 *
 * Each stream carries a name and an accent colour. The accent is not
 * decoration: it is drawn as the focus border, and the shell uses it to
 * re-tint the panel, so the user always knows which stream they are in
 * without reading a label.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include <wayland-server-core.h>
#include <wlr/types/wlr_scene.h>
#include <wlr/util/log.h>

#include "bgwl.h"

static const char *default_names[BGWL_MAX_WORKSPACES] = {
	"поток 1", "поток 2", "код", "сеть", "заметки",
	"медиа", "игра", "тест", "буфер", "резерв",
};

static void workspace_handle_destroy(struct bgwl_workspace *ws) {
	wl_list_remove(&ws->link);
	if (ws->tree != NULL) {
		wlr_scene_node_destroy(&ws->tree->node);
		ws->tree = NULL;
	}
	free(ws);
}

void bgwl_workspaces_init(struct bgwl_server *server) {
	struct bgwl_config *cfg = &server->cfg;

	for (int i = 0; i < cfg->workspace_count; i++) {
		struct bgwl_workspace *ws = calloc(1, sizeof(*ws));
		if (ws == NULL) {
			wlr_log(WLR_ERROR, "out of memory for workspace %d", i);
			continue;
		}
		ws->server = server;
		ws->index = i;
		wl_list_init(&ws->toplevels);

		if (cfg->workspace_names[i][0] != '\0') {
			snprintf(ws->name, sizeof(ws->name), "%s", cfg->workspace_names[i]);
		} else if (i < BGWL_MAX_WORKSPACES && default_names[i] != NULL) {
			snprintf(ws->name, sizeof(ws->name), "%s", default_names[i]);
		} else {
			snprintf(ws->name, sizeof(ws->name), "поток %d", i + 1);
		}

		/* Accent comes from the config; unset means "inherit the global one"
		 * so a single accent= line styles the whole desktop. */
		if (cfg->workspace_accents[i][3] > 0.0f) {
			memcpy(ws->accent, cfg->workspace_accents[i], sizeof(ws->accent));
		} else {
			memcpy(ws->accent, cfg->accent_color, sizeof(ws->accent));
		}

		ws->layout = cfg->layout;
		ws->split = BGWL_SPLIT_RIGHT;
		ws->master_count = 1;
		ws->master_pct = 55;

		ws->tree = wlr_scene_tree_create(server->trees[BGWL_TREE_NORMAL]);
		if (ws->tree == NULL) {
			wlr_log(WLR_ERROR, "wlr_scene_tree_create failed for workspace");
			free(ws);
			continue;
		}
		ws->tree->node.data = ws;
		wlr_scene_node_set_enabled(&ws->tree->node, i == 0);

		wl_list_insert(server->workspaces.prev, &ws->link);
	}

	server->active_ws = bgwl_workspace_by_index(server, 0);
	if (server->active_ws == NULL) {
		wlr_log(WLR_ERROR, "no workspace could be created");
	}
}

void bgwl_workspaces_finish(struct bgwl_server *server) {
	struct bgwl_workspace *ws, *tmp;
	wl_list_for_each_safe(ws, tmp, &server->workspaces, link) {
		workspace_handle_destroy(ws);
	}
	server->active_ws = NULL;
}

struct bgwl_workspace *bgwl_workspace_by_index(struct bgwl_server *server,
		int index) {
	struct bgwl_workspace *ws;
	wl_list_for_each(ws, &server->workspaces, link) {
		if (ws->index == index) {
			return ws;
		}
	}
	return NULL;
}

const char *bgwl_layout_name(enum bgwl_layout layout) {
	switch (layout) {
	case BGWL_LAYOUT_FLOAT: return "float";
	case BGWL_LAYOUT_TILE:  return "tile";
	case BGWL_LAYOUT_EVEN:  return "even";
	default:                return "float";
	}
}

int bgwl_layout_from_name(const char *name) {
	if (name == NULL) {
		return -1;
	}
	for (int i = 0; i < BGWL_LAYOUT_COUNT; i++) {
		if (strcmp(name, bgwl_layout_name((enum bgwl_layout)i)) == 0) {
			return i;
		}
	}
	return -1;
}

struct bgwl_toplevel *bgwl_workspace_first_toplevel(struct bgwl_workspace *ws) {
	struct bgwl_toplevel *toplevel;
	wl_list_for_each(toplevel, &ws->toplevels, ws_link) {
		if (toplevel->xdg_toplevel != NULL &&
				toplevel->xdg_toplevel->base != NULL &&
				toplevel->xdg_toplevel->base->surface != NULL &&
				toplevel->xdg_toplevel->base->surface->mapped) {
			return toplevel;
		}
	}
	return NULL;
}

void bgwl_workspace_send_window(struct bgwl_toplevel *toplevel,
		struct bgwl_workspace *target) {
	if (toplevel == NULL || target == NULL || toplevel->ws == target) {
		return;
	}
	struct bgwl_server *server = toplevel->server;

	bgwl_toplevel_move_to_workspace(toplevel, target);

	/* Follow the window only when it went to the workspace the user is on;
	 * sending a window away and having the desktop jump after it is the
	 * single most annoying behaviour a workspace switcher can have. */
	if (target == server->active_ws) {
		bgwl_toplevel_focus(toplevel);
		bgwl_reflow(server);
	}
	bgwl_ctl_broadcast(server, "windows");
}

void bgwl_workspace_set_layout(struct bgwl_workspace *ws,
		enum bgwl_layout layout) {
	if (ws == NULL || ws->layout == layout) {
		return;
	}
	struct bgwl_server *server = ws->server;
	struct bgwl_toplevel *toplevel;

	ws->layout = layout;

	if (layout == BGWL_LAYOUT_FLOAT) {
		wl_list_for_each(toplevel, &ws->toplevels, ws_link) {
			if (toplevel->has_restore) {
				/* A maximised or fullscreen window keeps its own saved
				 * box; overwriting it with tile geometry would lose the size
				 * the user chose. */
				continue;
			}
			bgwl_toplevel_set_tiled(toplevel, false);
			if (toplevel->float_box.width > 0) {
				bgwl_toplevel_set_geometry(toplevel, toplevel->float_box);
			}
		}
		struct bgwl_toplevel *focused = bgwl_server_focused_toplevel(server);
		if (focused != NULL && focused->ws == ws) {
			bgwl_toplevel_raise(focused);
		}
		return;
	}

	wl_list_for_each(toplevel, &ws->toplevels, ws_link) {
		bgwl_toplevel_set_tiled(toplevel, true);
	}
	bgwl_reflow_workspace(ws);
}

void bgwl_workspace_cycle_focus(struct bgwl_workspace *ws, int direction) {
	if (ws == NULL) {
		return;
	}
	struct bgwl_toplevel *current = bgwl_workspace_first_toplevel(ws);
	if (current == NULL || direction == 0) {
		return;
	}

	/* ws_link is kept in tiling order, so "next" is the neighbour in the
	 * layout — cycling focus walks the tiles, not the z-stack. */
	struct bgwl_toplevel *candidate = current;
	for (int step = 0; step < ws->mapped_count; step++) {
		struct wl_list *next = direction > 0 ? candidate->ws_link.next
			: candidate->ws_link.prev;
		if (next == &ws->toplevels || next == NULL) {
			continue;
		}
		candidate = wl_container_of(next, candidate, ws_link);
		if (candidate->xdg_toplevel != NULL &&
				candidate->xdg_toplevel->base != NULL &&
				candidate->xdg_toplevel->base->surface != NULL &&
				candidate->xdg_toplevel->base->surface->mapped) {
			bgwl_toplevel_focus(candidate);
			return;
		}
	}
}

void bgwl_workspace_switch(struct bgwl_server *server,
		struct bgwl_workspace *target, bool animate) {
	if (target == NULL || target == server->active_ws) {
		return;
	}
	struct bgwl_workspace *from = server->active_ws;
	int direction = 0;
	if (from != NULL) {
		direction = target->index > from->index ? 1 : -1;
	}

	if (from != NULL) {
		wlr_scene_node_set_enabled(&from->tree->node, false);
		/* Park the old tree off-screen so the slide has somewhere to come
		 * from; it stays disabled, so no damage is paid for it. */
		wlr_scene_node_set_position(&from->tree->node, -direction *
			bgwl_server_usable_box(server).width, 0);
	}

	server->active_ws = target;
	wlr_scene_node_set_position(&target->tree->node, 0, 0);
	wlr_scene_node_set_enabled(&target->tree->node, true);

	/* Sticky windows follow the user rather than staying behind: that is the
	 * whole point of marking one. They land at the end of the new stack. */
	struct bgwl_toplevel *toplevel, *tmp;
	wl_list_for_each_safe(toplevel, tmp, &server->toplevels, link) {
		if (toplevel->sticky && toplevel->ws != target) {
			bgwl_toplevel_move_to_workspace(toplevel, target);
		}
	}

	bgwl_reflow_workspace(target);

	/* Give the stream's own accent to the panel and the focus ring: this is
	 * the visible half of the Потоки idea. */
	/* The window that was focused lives on the stream we are leaving, so it
	 * must stop being the compositor's focus answer — otherwise a window action
	 * (maximise, close) would silently apply to an invisible window. */
	if (server->focus_view != NULL && server->focus_view->ws != target
			&& !server->focus_view->sticky) {
		server->focus_view = NULL;
	}

	struct bgwl_toplevel *focused = bgwl_workspace_first_toplevel(target);
	if (focused != NULL) {
		bgwl_toplevel_focus(focused);
	} else {
		wlr_seat_keyboard_clear_focus(server->seat);
	}

	if (animate && server->cfg.animations && direction != 0) {
		bgwl_anim_workspace_slide(server, from, target, direction);
	}

	bgwl_ctl_broadcast(server, "workspace");
	wlr_log(WLR_INFO, "workspace -> %d (%s, layout %s)", target->index,
		target->name, bgwl_layout_name(target->layout));
}
