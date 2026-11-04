/*
 * Outputs: hotplug, mode and scale selection, per-output background, the frame
 * callback, DPMS, and the output-management protocol that lets the settings app
 * rearrange screens.
 *
 * Every output keeps two rectangles:
 *
 *   full box    — where the monitor physically sits in the layout
 *   usable box  — what is left after layer surfaces reserve their exclusive
 *                 zones (the panel), recomputed by layer.c
 *
 * Tiling reads only the usable box. That single rule is why windows never end
 * up under the panel.
 */
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include <wayland-server-core.h>
#include <wlr/types/wlr_output.h>
#include <wlr/types/wlr_output_layout.h>
#include <wlr/types/wlr_output_management_v1.h>
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

void bgwl_output_update_background(struct bgwl_output *output) {
	struct bgwl_server *server = output->server;
	if (output->bg_tree == NULL) {
		/* bgwl_new_output() applies a mode before it has created the output's
		 * scene nodes; drawing a background then has nowhere to put it. */
		return;
	}
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
			wlr_scene_buffer_set_dest_size(output->bg_image_node, width,
				height);
			return;
		}
		struct bgwl_buffer *image = bgwl_load_png(server->cfg.bg_image);
		if (image == NULL) {
			wlr_log(WLR_ERROR, "background image unusable, using solid fill");
			output->bg_rect = wlr_scene_rect_create(output->bg_tree, width,
				height, server->cfg.bg_color);
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

/* The output's own box in layout coordinates; the usable box starts here and
 * is then shrunk by layer.c for every exclusive zone. */
static void bgwl_output_reset_usable(struct bgwl_output *output) {
	struct wlr_box box;
	wlr_output_layout_get_box(output->server->output_layout, output->wlr_output,
		&box);
	if (box.width <= 0 || box.height <= 0) {
		int width = 0, height = 0;
		wlr_output_effective_resolution(output->wlr_output, &width, &height);
		box.x = 0;
		box.y = 0;
		box.width = width;
		box.height = height;
	}
	output->usable = box;
}

bool bgwl_output_apply_mode(struct bgwl_output *output,
		const struct wlr_output_state *requested) {
	struct wlr_output *wlr_output = output->wlr_output;

	struct wlr_output_state state;
	wlr_output_state_init(&state);
	if (requested != NULL) {
		/* A configuration from the output-management protocol: mode, scale,
		 * transform and enable state all come from the client. */
		wlr_output_state_set_enabled(&state, requested->enabled);
		if (requested->committed & WLR_OUTPUT_STATE_MODE) {
			if (requested->mode_type == WLR_OUTPUT_STATE_MODE_CUSTOM) {
				wlr_output_state_set_custom_mode(&state,
					requested->custom_mode.width, requested->custom_mode.height,
					requested->custom_mode.refresh);
			} else {
				wlr_output_state_set_mode(&state, requested->mode);
			}
		}
		if (requested->committed & WLR_OUTPUT_STATE_SCALE) {
			wlr_output_state_set_scale(&state, requested->scale);
		}
		if (requested->committed & WLR_OUTPUT_STATE_TRANSFORM) {
			wlr_output_state_set_transform(&state, requested->transform);
		}
		if (requested->committed & WLR_OUTPUT_STATE_ADAPTIVE_SYNC_ENABLED) {
			wlr_output_state_set_adaptive_sync_enabled(&state,
				requested->adaptive_sync_enabled);
		}
	} else {
		wlr_output_state_set_enabled(&state, true);
		struct wlr_output_mode *mode = wlr_output_preferred_mode(wlr_output);
		if (mode != NULL) {
			wlr_output_state_set_mode(&state, mode);
		}
		if (output->server->cfg.output_scale > 0.0f) {
			wlr_output_state_set_scale(&state,
				output->server->cfg.output_scale);
		}
	}

	bool ok = wlr_output_commit_state(wlr_output, &state);
	wlr_output_state_finish(&state);
	if (!ok) {
		wlr_log(WLR_ERROR, "failed to commit output state for '%s'",
			wlr_output->name);
		return false;
	}

	/* Cursor themes are per scale factor, so a scale change needs a reload. */
	wlr_xcursor_manager_load(output->server->cursor_mgr, wlr_output->scale);
	bgwl_output_reset_usable(output);
	bgwl_output_update_background(output);
	return true;
}

void bgwl_output_apply_configured(struct bgwl_output *output) {
	bgwl_output_apply_mode(output, NULL);
}

/* DPMS: the cheap half of power saving. Enabling/disabling an output is a full
 * modeset on DRM, so this is only ever called on the idle timer and the lid,
 * never from a per-frame path. */
void bgwl_output_set_dpms(struct bgwl_output *output, bool on) {
	if (output->dpms_on == on) {
		return;
	}
	struct wlr_output_state state;
	wlr_output_state_init(&state);
	wlr_output_state_set_enabled(&state, on);
	/* Carry the current mode across: a commit that changes nothing else keeps
	 * the framebuffer, so the panel comes back with its image intact. */
	if (output->wlr_output->current_mode != NULL) {
		wlr_output_state_set_mode(&state, output->wlr_output->current_mode);
	}
	if (wlr_output_commit_state(output->wlr_output, &state)) {
		output->dpms_on = on;
		if (output->bg_tree != NULL) {
			wlr_scene_node_set_enabled(&output->bg_tree->node, on);
		}
		wlr_log(WLR_INFO, "output '%s' DPMS %s", output->wlr_output->name,
			on ? "on" : "off");
	} else {
		wlr_log(WLR_ERROR, "DPMS change failed for '%s'",
			output->wlr_output->name);
	}
	wlr_output_state_finish(&state);
}

/* The next output in layout order, wrapping around. Direction is +1/-1 and the
 * order is by x then y, which is what "the screen to the right" means to a
 * user standing at the desk. */
struct bgwl_output *bgwl_output_neighbour(struct bgwl_server *server,
		struct bgwl_output *current, int direction) {
	if (wl_list_length(&server->outputs) < 2 || current == NULL) {
		return NULL;
	}

	struct bgwl_output *ordered[BGWL_MAX_OUTPUTS];
	int n = 0;
	struct bgwl_output *output;
	wl_list_for_each(output, &server->outputs, link) {
		if (n < BGWL_MAX_OUTPUTS) {
			ordered[n++] = output;
		}
	}
	for (int i = 0; i < n; i++) {
		for (int j = i + 1; j < n; j++) {
			struct wlr_box a, b;
			wlr_output_layout_get_box(server->output_layout,
				ordered[i]->wlr_output, &a);
			wlr_output_layout_get_box(server->output_layout,
				ordered[j]->wlr_output, &b);
			if (b.x < a.x || (b.x == a.x && b.y < a.y)) {
				struct bgwl_output *tmp = ordered[i];
				ordered[i] = ordered[j];
				ordered[j] = tmp;
			}
		}
	}

	int index = 0;
	for (int i = 0; i < n; i++) {
		if (ordered[i] == current) {
			index = i;
			break;
		}
	}
	int next = (index + direction + n) % n;
	return ordered[next];
}

/* Publishes the current layout as the manager's configuration; every settings
 * client reads exactly this, so it is refreshed after any change that came
 * from anywhere, including a hotplug. */
void bgwl_output_publish_configuration(struct bgwl_server *server) {
	if (server->output_manager == NULL) {
		return;
	}
	struct wlr_output_configuration_v1 *config =
		wlr_output_configuration_v1_create();
	if (config == NULL) {
		return;
	}

	struct bgwl_output *output;
	wl_list_for_each(output, &server->outputs, link) {
		struct wlr_output_configuration_head_v1 *head =
			wlr_output_configuration_head_v1_create(config,
				output->wlr_output);
		if (head == NULL) {
			continue;
		}
		struct wlr_box box;
		wlr_output_layout_get_box(server->output_layout, output->wlr_output,
			&box);
		head->state.x = box.x;
		head->state.y = box.y;
		head->state.enabled = output->dpms_on;
	}
	wlr_output_manager_v1_set_configuration(server->output_manager, config);
}

/* Applies a client-proposed configuration. test_only means "would this work",
 * and must not touch the layout. */
static void apply_output_configuration(struct bgwl_server *server,
		struct wlr_output_configuration_v1 *config, bool test_only) {
	bool ok = true;
	struct wlr_output_configuration_head_v1 *head;

	/* First pass: validate without committing, so a configuration with one
	 * bad head fails as a whole instead of half-applying. */
	wl_list_for_each(head, &config->heads, link) {
		if (head->state.output == NULL) {
			ok = false;
			break;
		}
		if (!head->state.enabled) {
			continue;
		}
		struct wlr_output_state state;
		wlr_output_state_init(&state);
		wlr_output_head_v1_state_apply(&head->state, &state);
		if (!wlr_output_test_state(head->state.output, &state)) {
			ok = false;
		}
		wlr_output_state_finish(&state);
		if (!ok) {
			break;
		}
	}

	if (!ok) {
		wlr_output_configuration_v1_send_failed(config);
		if (!test_only) {
			wlr_log(WLR_ERROR, "rejected an output configuration");
			bgwl_output_publish_configuration(server);
		}
		return;
	}
	if (test_only) {
		wlr_output_configuration_v1_send_succeeded(config);
		return;
	}

	wl_list_for_each(head, &config->heads, link) {
		struct bgwl_output *output = NULL;
		struct bgwl_output *candidate;
		wl_list_for_each(candidate, &server->outputs, link) {
			if (candidate->wlr_output == head->state.output) {
				output = candidate;
				break;
			}
		}
		if (output == NULL) {
			continue;
		}

		if (!head->state.enabled) {
			bgwl_output_set_dpms(output, false);
			continue;
		}
		bgwl_output_set_dpms(output, true);

		/* The head state is a client proposal in the manager's own struct;
		 * translate it into a wlr_output_state before committing. */
		struct wlr_output_state state;
		wlr_output_state_init(&state);
		wlr_output_head_v1_state_apply(&head->state, &state);
		wlr_output_state_set_enabled(&state, true);
		bool applied = wlr_output_commit_state(head->state.output, &state);
		wlr_output_state_finish(&state);
		if (!applied) {
			wlr_log(WLR_ERROR, "could not apply mode to '%s'",
				head->state.output->name);
			continue;
		}

		/* Position is the caller's job: wlr_output_head_v1_state_apply does
		 * not move anything in the layout. */
		wlr_output_layout_add(server->output_layout, head->state.output,
			head->state.x, head->state.y);
		wlr_xcursor_manager_load(server->cursor_mgr, head->state.output->scale);
		bgwl_output_reset_usable(output);
		bgwl_output_update_background(output);
		bgwl_output_publish_configuration(server);
	}

	bgwl_layer_update_usable_areas(server);
	bgwl_reflow(server);
	bgwl_output_publish_configuration(server);
	bgwl_ctl_broadcast(server, "outputs");
	wlr_output_configuration_v1_send_succeeded(config);
}

static void output_manager_handle_apply(struct wl_listener *listener,
		void *data) {
	struct bgwl_server *server =
		wl_container_of(listener, server, output_manager_apply);
	apply_output_configuration(server, data, false);
}

static void output_manager_handle_test(struct wl_listener *listener,
		void *data) {
	struct bgwl_server *server =
		wl_container_of(listener, server, output_manager_test);
	apply_output_configuration(server, data, true);
}

void bgwl_output_management_install(struct bgwl_server *server) {
	if (server->output_manager == NULL) {
		return;
	}
	server->output_manager_apply.notify = output_manager_handle_apply;
	wl_signal_add(&server->output_manager->events.apply,
		&server->output_manager_apply);
	server->output_manager_test.notify = output_manager_handle_test;
	wl_signal_add(&server->output_manager->events.test,
		&server->output_manager_test);
	bgwl_output_publish_configuration(server);
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
	bgwl_output_reset_usable(output);
	bgwl_output_update_background(output);
	bgwl_layer_update_usable_areas(output->server);
	bgwl_ctl_broadcast(output->server, "outputs");
}

static void output_destroy(struct wl_listener *listener, void *data) {
	struct bgwl_output *output = wl_container_of(listener, output, destroy);

	/* The scene output is owned by the output addon machinery: it is
	 * destroyed together with the struct wlr_output, so calling
	 * wlr_scene_output_destroy() here would double-destroy. */
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

	/* One screen disappeared: every window that was tiled on it has to be
	 * re-placed, and the settings clients need to hear about it. */
	bgwl_layer_update_usable_areas(output->server);
	bgwl_reflow(output->server);
	bgwl_output_publish_configuration(output->server);
	bgwl_ctl_broadcast(output->server, "outputs");

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
	output->dpms_on = true;

	bgwl_output_apply_mode(output, NULL);

	output->frame.notify = output_frame;
	wl_signal_add(&wlr_output->events.frame, &output->frame);
	output->request_state.notify = output_request_state;
	wl_signal_add(&wlr_output->events.request_state, &output->request_state);
	output->destroy.notify = output_destroy;
	wl_signal_add(&wlr_output->events.destroy, &output->destroy);

	wl_list_insert(&server->outputs, &output->link);

	/* add_auto places outputs left-to-right in arrival order. A configured
	 * layout, if one exists, is applied afterwards by the output manager
	 * when the settings client proposes it. */
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

	if (!wlr_xcursor_manager_load(server->cursor_mgr, wlr_output->scale)) {
		wlr_log(WLR_ERROR, "failed to load cursor theme at scale %f",
			(double)wlr_output->scale);
	}

	output->bg_tree = wlr_scene_tree_create(server->trees[BGWL_TREE_BG]);
	if (output->bg_tree != NULL) {
		bgwl_output_reset_usable(output);
		bgwl_output_update_background(output);
	}

	/* Hotplug: pull the pointer onto the new screen so a single-monitor
	 * session that just grew does not leave the cursor off-screen. */
	bgwl_server_warp_cursor_to_center(server);
	bgwl_layer_update_usable_areas(server);
	bgwl_output_publish_configuration(server);
	bgwl_ctl_broadcast(server, "outputs");

	wlr_log(WLR_INFO, "output '%s' connected (%dx%d at scale %g)",
		wlr_output->name, wlr_output->width, wlr_output->height,
		(double)wlr_output->scale);
}
