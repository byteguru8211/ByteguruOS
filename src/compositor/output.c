/*
 * Outputs: hotplug handling, preferred mode, left-to-right layout, per-output
 * background and the frame callback.
 */
#include <stdlib.h>
#include <time.h>

#include <wayland-server-core.h>
#include <wlr/types/wlr_output.h>
#include <wlr/types/wlr_output_layout.h>
#include <wlr/types/wlr_scene.h>
#include <wlr/util/log.h>

#include "bgwl.h"

static void bgwl_output_destroy_background(struct bgwl_output *output) {
	if (output->bg_image_node != NULL) {
		wlr_scene_node_destroy(&output->bg_image_node->node);
		output->bg_image_node = NULL;
	}
	if (output->bg_rect != NULL) {
		wlr_scene_node_destroy(&output->bg_rect->node);
		output->bg_rect = NULL;
	}
	/* bgwl_buffer refs handed to the scene are owned by the scene from that
	 * point on: wlr_scene_buffer_create() locks the buffer and the node
	 * destroy above drops it again. */
}

static void bgwl_output_update_background(struct bgwl_output *output) {
	struct bgwl_server *server = output->server;
	int width = 0, height = 0;
	wlr_output_effective_resolution(output->wlr_output, &width, &height);
	if (width <= 0 || height <= 0) {
		return;
	}

	/* Keep the per-output background aligned with the output's place in the
	 * layout. */
	struct wlr_box box;
	wlr_output_layout_get_box(server->output_layout, output->wlr_output, &box);
	wlr_scene_node_set_position(&output->bg_tree->node, box.x, box.y);

	if (server->cfg.bg_image != NULL) {
		if (output->bg_image_node != NULL) {
			/* Already drawn; only the destination size can change. */
			wlr_scene_buffer_set_dest_size(output->bg_image_node,
				width, height);
			return;
		}
		struct bgwl_buffer *image = bgwl_load_png(server->cfg.bg_image);
		if (image == NULL) {
			wlr_log(WLR_ERROR, "background image unusable, using solid fill");
			output->bg_rect = wlr_scene_rect_create(output->bg_tree,
				width, height, server->cfg.bg_color);
			return;
		}
		output->bg_image_node = wlr_scene_buffer_create(output->bg_tree,
			&image->base);
		if (output->bg_image_node == NULL) {
			bgwl_buffer_drop(image);
			return;
		}
		wlr_scene_buffer_set_dest_size(output->bg_image_node, width, height);
		bgwl_buffer_drop(image);
		return;
	}

	if (output->bg_rect != NULL) {
		wlr_scene_rect_set_size(output->bg_rect, width, height);
		wlr_scene_rect_set_color(output->bg_rect, server->cfg.bg_color);
		return;
	}
	output->bg_rect = wlr_scene_rect_create(output->bg_tree, width, height,
		server->cfg.bg_color);
}

static void bgwl_output_apply_mode(struct bgwl_output *output) {
	struct wlr_output *wlr_output = output->wlr_output;

	/* ВЕРИФИЦИРОВАТЬ: the two-argument wlr_output_commit_state() form and
	 * wlr_output_event_request_state.state. Both were diffed against the
	 * wlroots 0.18.2, 0.19.3 and 0.20.2 headers and are identical, so this
	 * only needs re-checking if the pin moves past 0.20.x. */
	struct wlr_output_state state;
	wlr_output_state_init(&state);
	wlr_output_state_set_enabled(&state, true);

	struct wlr_output_mode *mode = wlr_output_preferred_mode(wlr_output);
	if (mode != NULL) {
		wlr_output_state_set_mode(&state, mode);
	}

	if (!wlr_output_commit_state(wlr_output, &state)) {
		wlr_log(WLR_ERROR, "failed to commit output state for '%s'",
			wlr_output->name);
	}
	wlr_output_state_finish(&state);
}

static void output_frame(struct wl_listener *listener, void *data) {
	struct bgwl_output *output = wl_container_of(listener, output, frame);
	struct wlr_scene_output *scene_output = wlr_scene_get_scene_output(
		output->server->scene, output->wlr_output);
	if (scene_output == NULL) {
		return;
	}

	if (!wlr_scene_output_commit(scene_output, NULL)) {
		wlr_log(WLR_DEBUG, "scene output commit failed for '%s'",
			output->wlr_output->name);
	}

	struct timespec now;
	clock_gettime(CLOCK_MONOTONIC, &now);
	wlr_scene_output_send_frame_done(scene_output, &now);
}

static void output_request_state(struct wl_listener *listener, void *data) {
	struct bgwl_output *output =
		wl_container_of(listener, output, request_state);
	const struct wlr_output_event_request_state *event = data;

	if (!wlr_output_commit_state(output->wlr_output, event->state)) {
		wlr_log(WLR_ERROR, "failed to apply requested state for '%s'",
			output->wlr_output->name);
		return;
	}
	bgwl_output_update_background(output);
}

static void output_destroy(struct wl_listener *listener, void *data) {
	struct bgwl_output *output = wl_container_of(listener, output, destroy);

	/* The scene output is owned by the output addon machinery in 0.19: it is
	 * destroyed together with the struct wlr_output, so we must not call
	 * wlr_scene_output_destroy() here. */
	output->scene_output = NULL;

	bgwl_output_destroy_background(output);
	if (output->bg_tree != NULL) {
		wlr_scene_node_destroy(&output->bg_tree->node);
		output->bg_tree = NULL;
	}

	wl_list_remove(&output->frame.link);
	wl_list_remove(&output->request_state.link);
	wl_list_remove(&output->destroy.link);
	wl_list_remove(&output->link);

	/* Layer surfaces anchored to this output keep pointing at it through
	 * wlr_layer_surface_v1.output; wlroots clears that pointer itself when
	 * the output goes away, and layer.c drops the panel reference on unmap. */

	free(output);
}

void bgwl_new_output(struct wl_listener *listener, void *data) {
	struct bgwl_server *server = wl_container_of(listener, server, new_output);
	struct wlr_output *wlr_output = data;

	/* Must happen before the first commit: binds our allocator+renderer pair
	 * to this output. */
	if (!wlr_output_init_render(wlr_output, server->allocator,
			server->renderer)) {
		wlr_log(WLR_ERROR, "wlr_output_init_render failed, ignoring output");
		return;
	}

	struct bgwl_output *output = calloc(1, sizeof(*output));
	if (output == NULL) {
		wlr_log(WLR_ERROR, "out of memory for output");
		return;
	}
	output->server = server;
	output->wlr_output = wlr_output;

	bgwl_output_apply_mode(output);

	output->frame.notify = output_frame;
	wl_signal_add(&wlr_output->events.frame, &output->frame);
	output->request_state.notify = output_request_state;
	wl_signal_add(&wlr_output->events.request_state, &output->request_state);
	output->destroy.notify = output_destroy;
	wl_signal_add(&wlr_output->events.destroy, &output->destroy);

	wl_list_insert(&server->outputs, &output->link);

	/* add_auto places outputs left-to-right in arrival order, which is the
	 * layout policy bgwl v0 documents. */
	struct wlr_output_layout_output *l_output =
		wlr_output_layout_add_auto(server->output_layout, wlr_output);
	if (l_output == NULL) {
		wlr_log(WLR_ERROR, "wlr_output_layout_add_auto failed");
		return;
	}

	output->scene_output = wlr_scene_output_create(server->scene, wlr_output);
	if (output->scene_output == NULL) {
		wlr_log(WLR_ERROR, "wlr_scene_output_create failed");
		return;
	}
	wlr_scene_output_layout_add_output(server->scene_layout, l_output,
		output->scene_output);

	/* Cursor themes are per scale factor; headless outputs report scale 1. */
	if (!wlr_xcursor_manager_load(server->cursor_mgr, wlr_output->scale)) {
		wlr_log(WLR_ERROR, "failed to load cursor theme at scale %f",
			(double)wlr_output->scale);
	}

	output->bg_tree = wlr_scene_tree_create(server->trees[BGWL_TREE_BG]);
	if (output->bg_tree != NULL) {
		bgwl_output_update_background(output);
	}

	/* Hotplug: pull the pointer onto the new screen so a single-monitor
	 * session that just grew does not leave the cursor off-screen. */
	bgwl_server_warp_cursor_to_center(server);

	wlr_log(WLR_INFO, "output '%s' connected (%dx%d, preferred mode %d mHz)",
		wlr_output->name, wlr_output->width, wlr_output->height,
		wlr_output->current_mode != NULL ? wlr_output->current_mode->refresh : 0);
}
