/*
 * The bgwl control socket.
 *
 * Why this exists instead of another Wayland protocol: the panel, the launcher,
 * the settings app and the test harness all need the same thing — a read of the
 * compositor's window and stream model, plus a way to change it. Writing four
 * protocol clients for that, in four languages, is how desktops end up with a
 * private fork of a protocol nobody documents. A line-based command socket with
 * a JSON reply is:
 *
 *   - readable by a human with `nc -U`, which is how this file got tested
 *     before bgctl existed;
 *   - one implementation on each side, and the schema is one C function;
 *   - not a security boundary — the socket lives in $XDG_RUNTIME_DIR with the
 *     session's own permissions, exactly like the Wayland socket next to it.
 *
 * The compositor remains the only owner of window state. Nothing here lets a
 * client bypass xdg-shell; it is the same authority, reached differently.
 *
 * Protocol: one command per line, one JSON object per reply, `EV <kind>` pushed
 * to subscribed clients when state changes. Clients that send nothing are
 * assumed to want the state snapshot first, so `nc -U` shows something useful.
 */
#include <errno.h>
#include <limits.h>
#include <stdarg.h>
#include <fcntl.h>
#include <grp.h>
#include <poll.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/un.h>
#include <unistd.h>

#include <wayland-server-core.h>
#include <wayland-util.h>
#include <wlr/backend/headless.h>
#include <wlr/types/wlr_scene.h>
#include <wlr/types/wlr_xdg_shell.h>
#include <wlr/util/log.h>

#include "bgwl.h"

#define BGWL_CTL_SOCKET_NAME "compositor.sock"
#define BGWL_CTL_BACKLOG 8
#define BGWL_CTL_LINE_MAX 1024
/* A slow reader must not make the compositor hold unbounded memory. When the
 * output buffer overflows the client is dropped; it reconnects and gets the
 * next snapshot. */
#define BGWL_CTL_OUTPUT_MAX 65536

struct bgwl_ctl_client {
	struct bgwl_ctl *ctl;
	struct wl_event_source *source;
	int fd;
	bool subscribed;
	char out[BGWL_CTL_OUTPUT_MAX];
	size_t out_len;
	char in[BGWL_CTL_LINE_MAX];
	size_t in_len;
	struct wl_list link;
};

struct bgwl_ctl {
	struct bgwl_server *server;
	int listen_fd;
	struct wl_event_source *source;
	struct wl_list clients;
};

/* --- JSON writing ------------------------------------------------------- */

struct bgwl_json {
	char *buffer;
	size_t size;	/* capacity */
	size_t len;	/* used, excluding the NUL */
	bool overflow;
};

static void json_reserve(struct bgwl_json *json, size_t extra) {
	if (json->overflow) {
		return;
	}
	if (json->len + extra + 1 > json->size) {
		size_t want = json->size * 2;
		while (want < json->len + extra + 1) {
			want *= 2;
		}
		char *grown = realloc(json->buffer, want);
		if (grown == NULL) {
			json->overflow = true;
			return;
		}
		json->buffer = grown;
		json->size = want;
	}
}

static void json_addf(struct bgwl_json *json, const char *format, ...) {
	va_list args;
	va_start(args, format);
	char scratch[512];
	int written = vsnprintf(scratch, sizeof(scratch), format, args);
	va_end(args);
	if (written < 0) {
		json->overflow = true;
		return;
	}
	json_reserve(json, (size_t)written);
	if (json->overflow) {
		return;
	}
	memcpy(json->buffer + json->len, scratch, (size_t)written);
	json->len += (size_t)written;
	json->buffer[json->len] = '\0';
}

/* Titles are arbitrary UTF-8 from any client; only the two characters that
 * would break the document get escaped, plus control bytes. Everything else
 * passes through as UTF-8, which is what the panel expects. */
static void json_add_string(struct bgwl_json *json, const char *text) {
	json_addf(json, "\"");
	for (const unsigned char *p = (const unsigned char *)text; p != NULL && *p;
			p++) {
		switch (*p) {
		case '"':
			json_addf(json, "\\\"");
			break;
		case '\\':
			json_addf(json, "\\\\");
			break;
		case '\n':
			json_addf(json, "\\n");
			break;
		case '\r':
			json_addf(json, "\\r");
			break;
		case '\t':
			json_addf(json, "\\t");
			break;
		default:
			if (*p < 0x20) {
				json_addf(json, "\\u%04x", *p);
			} else {
				json_reserve(json, 1);
				if (json->overflow) {
					return;
				}
				json->buffer[json->len++] = (char)*p;
				json->buffer[json->len] = '\0';
			}
		}
	}
	json_addf(json, "\"");
}

static void json_add_box(struct bgwl_json *json, struct wlr_box box) {
	json_addf(json, "{\"x\":%d,\"y\":%d,\"width\":%d,\"height\":%d}", box.x,
		box.y, box.width, box.height);
}

static void json_color(char *out, size_t size, const float color[static 4]) {
	/* Stored premultiplied; the panel wants the plain sRGB triplet. */
	float alpha = color[3] > 0.0f ? color[3] : 1.0f;
	snprintf(out, size, "#%02x%02x%02x",
		(int)(color[0] / alpha * 255.0f + 0.5f),
		(int)(color[1] / alpha * 255.0f + 0.5f),
		(int)(color[2] / alpha * 255.0f + 0.5f));
}

/* --- state snapshot ----------------------------------------------------- */

static void json_window(struct bgwl_json *json, struct bgwl_toplevel *toplevel,
		bool focused, struct bgwl_server *server) {
	struct wlr_xdg_toplevel *xdg = toplevel->xdg_toplevel;
	struct wlr_box frame;
	bgwl_toplevel_get_geometry(toplevel, &frame);
	char accent[16];
	json_color(accent, sizeof(accent),
		toplevel->ws != NULL ? toplevel->ws->accent
			: server->cfg.accent_color);

	json_addf(json, "{\"id\":%d", toplevel->id);
	json_addf(json, ",\"app_id\":");
	json_add_string(json, xdg->app_id != NULL ? xdg->app_id : "");
	json_addf(json, ",\"title\":");
	json_add_string(json, xdg->title != NULL ? xdg->title : "");
	json_addf(json, ",\"workspace\":%d",
		toplevel->ws != NULL ? toplevel->ws->index : -1);
	pid_t pid = -1;
	uid_t uid = -1;
	struct wl_client *client = xdg->base->surface->resource != NULL
		? wl_resource_get_client(xdg->base->surface->resource) : NULL;
	if (client != NULL) {
		/* The panel shows which process owns a window, and the test harness
		 * uses it to prove a spawned client really mapped. */
		wl_client_get_credentials(client, &pid, &uid, NULL);
	}
	json_addf(json, ",\"pid\":%d", (int)pid);
	json_addf(json, ",\"focused\":%s", focused ? "true" : "false");
	json_addf(json, ",\"tiled\":%s", toplevel->tiled ? "true" : "false");
	json_addf(json, ",\"maximized\":%s", toplevel->maximized ? "true" : "false");
	json_addf(json, ",\"fullscreen\":%s", toplevel->fullscreen ? "true" : "false");
	json_addf(json, ",\"above\":%s", toplevel->above ? "true" : "false");
	json_addf(json, ",\"sticky\":%s", toplevel->sticky ? "true" : "false");
	json_addf(json, ",\"decorated\":%s", toplevel->decorated ? "true" : "false");
	json_addf(json, ",\"mapped\":%s",
		xdg->base->surface->mapped ? "true" : "false");
	json_addf(json, ",\"accent\":\"%s\"", accent);
	json_addf(json, ",\"box\":");
	json_add_box(json, frame);
	json_addf(json, "}");
}

static void json_state(struct bgwl_json *json, struct bgwl_server *server) {
	char accent[16];
	json_color(accent, sizeof(accent), server->cfg.accent_color);

	json_addf(json, "{\"type\":\"state\",\"version\":1");
	json_addf(json, ",\"compositor\":\"bgwl\",\"locked\":%s",
		server->locked ? "true" : "false");
	json_addf(json, ",\"panel_visible\":%s",
		server->panel_visible ? "true" : "false");

	json_addf(json, ",\"settings\":{\"layout\":\"%s\"",
		bgwl_layout_name(server->cfg.layout));
	json_addf(json, ",\"workspaces\":%d", server->cfg.workspace_count);
	json_addf(json, ",\"gap-inner\":%d", server->cfg.gap_inner);
	json_addf(json, ",\"gap-outer\":%d", server->cfg.gap_outer);
	json_addf(json, ",\"border-width\":%d", server->cfg.border_width);
	json_addf(json, ",\"animations\":%s", server->cfg.animations ? "true" : "false");
	json_addf(json, ",\"anim-duration\":%d", server->cfg.anim_duration_ms);
	json_addf(json, ",\"focus-follows-mouse\":%s",
		server->cfg.focus_follows_mouse ? "true" : "false");
	json_addf(json, ",\"idle-timeout\":%d", server->cfg.idle_timeout_seconds);
	json_addf(json, ",\"accent\":\"%s\"", accent);
	json_addf(json, ",\"terminal\":");
	json_add_string(json, server->cfg.terminal);
	json_addf(json, ",\"launcher\":");
	json_add_string(json, server->cfg.launcher);
	json_addf(json, ",\"background\":");
	json_add_string(json, server->cfg.bg_image != NULL ? server->cfg.bg_image
		: "");
	json_addf(json, "}");

	json_addf(json, ",\"streams\":[");
	struct bgwl_workspace *ws;
	bool first_ws = true;
	wl_list_for_each(ws, &server->workspaces, link) {
		char ws_accent[16];
		json_color(ws_accent, sizeof(ws_accent), ws->accent);
		json_addf(json, "%s{\"index\":%d", first_ws ? "" : ",", ws->index);
		first_ws = false;
		json_addf(json, ",\"name\":");
		json_add_string(json, ws->name);
		json_addf(json, ",\"layout\":\"%s\"", bgwl_layout_name(ws->layout));
		json_addf(json, ",\"active\":%s", ws == server->active_ws
			? "true" : "false");
		json_addf(json, ",\"master-percent\":%d", ws->master_pct);
		json_addf(json, ",\"master-count\":%d", ws->master_count);
		json_addf(json, ",\"split\":\"%s\"",
			ws->split == BGWL_SPLIT_RIGHT ? "right" : "bottom");
		json_addf(json, ",\"windows\":%d", ws->mapped_count);
		json_addf(json, ",\"accent\":\"%s\"", ws_accent);
		json_addf(json, "}");
	}
	json_addf(json, "]");

	json_addf(json, ",\"outputs\":[");
	struct bgwl_output *output;
	bool first_output = true;
	wl_list_for_each(output, &server->outputs, link) {
		json_addf(json, "%s{\"name\":", first_output ? "" : ",");
		first_output = false;
		json_add_string(json, output->wlr_output->name);
		json_addf(json, ",\"description\":");
		json_add_string(json, output->wlr_output->description != NULL
			? output->wlr_output->description : "");
		/* Whether this output reaches a physical screen. A compositor on the
		 * headless fallback answers on the socket like a working one, so
		 * without this flag "session is up" cannot be told apart from
		 * "session is up but there is nowhere to draw". */
		json_addf(json, ",\"headless\":%s",
			wlr_output_is_headless(output->wlr_output) ? "true" : "false");
		struct wlr_box box;
		wlr_output_layout_get_box(server->output_layout, output->wlr_output,
			&box);
		json_addf(json, ",\"box\":");
		json_add_box(json, box);
		json_addf(json, ",\"usable\":");
		json_add_box(json, output->usable);
		json_addf(json, ",\"scale\":%g", (double)output->wlr_output->scale);
		json_addf(json, ",\"enabled\":%s", output->dpms_on ? "true" : "false");
		json_addf(json, ",\"refresh\":%d",
			output->wlr_output->current_mode != NULL
				? output->wlr_output->current_mode->refresh : 0);
		json_addf(json, "}");
	}
	json_addf(json, "]");

	struct bgwl_toplevel *focused = bgwl_server_focused_toplevel(server);
	json_addf(json, ",\"windows\":[");
	struct bgwl_toplevel *toplevel;
	bool first_window = true;
	/* Emitted in tiling order per stream so the panel's window list matches
	 * what the user sees left-to-right. */
	wl_list_for_each(ws, &server->workspaces, link) {
		wl_list_for_each(toplevel, &ws->toplevels, ws_link) {
			if (toplevel->xdg_toplevel == NULL ||
					toplevel->xdg_toplevel->base == NULL ||
					toplevel->xdg_toplevel->base->surface == NULL) {
				continue;
			}
			json_addf(json, "%s", first_window ? "" : ",");
			first_window = false;
			json_window(json, toplevel, toplevel == focused, server);
		}
	}
	json_addf(json, "]");
	json_addf(json, ",\"focused\":%d", focused != NULL ? focused->id : -1);
	json_addf(json, "}");
}

/* --- client IO ---------------------------------------------------------- */

static void client_flush(struct bgwl_ctl_client *client) {
	size_t sent = 0;
	while (sent < client->out_len) {
		ssize_t written = write(client->fd, client->out + sent,
			client->out_len - sent);
		if (written <= 0) {
			if (errno == EAGAIN || errno == EWOULDBLOCK) {
				break;
			}
			client->out_len = 0;
			return;
		}
		sent += (size_t)written;
	}
	if (sent > 0) {
		memmove(client->out, client->out + sent, client->out_len - sent);
		client->out_len -= sent;
	}
}

static void client_write(struct bgwl_ctl_client *client, const char *text,
		size_t length) {
	if (client->out_len + length >= sizeof(client->out)) {
		/* Drop the slow reader rather than grow without bound. */
		wlr_log(WLR_INFO, "ctl client overflowed; disconnecting it");
		client->out_len = 0;
		return;
	}
	memcpy(client->out + client->out_len, text, length);
	client->out_len += length;
}

static int client_handle_fd(int fd, uint32_t mask, void *data) {
	struct bgwl_ctl_client *client = data;
	struct bgwl_server *server = client->ctl->server;

	if (mask & (WL_EVENT_HANGUP | WL_EVENT_ERROR)) {
		wl_list_remove(&client->link);
		wl_event_source_remove(client->source);
		close(client->fd);
		free(client);
		return 0;
	}

	if (mask & WL_EVENT_READABLE) {
		char buffer[512];
		ssize_t got = read(client->fd, buffer, sizeof(buffer));
		if (got <= 0) {
			wl_list_remove(&client->link);
			wl_event_source_remove(client->source);
			close(client->fd);
			free(client);
			return 0;
		}
		for (ssize_t i = 0; i < got; i++) {
			char c = buffer[i];
			if (c == '\n' || c == '\r') {
				if (client->in_len == 0) {
					continue;
				}
				client->in[client->in_len] = '\0';
				bgwl_ctl_handle_command(server, client, client->in);
				client->in_len = 0;
			} else if (client->in_len < sizeof(client->in) - 1) {
				client->in[client->in_len++] = c;
			}
		}
	}

	if (mask & WL_EVENT_WRITABLE || client->out_len > 0) {
		client_flush(client);
	}
	return 1;
}

/* --- command handling --------------------------------------------------- */

static void send_state(struct bgwl_ctl_client *client) {
	struct bgwl_json json = { calloc(8192, 1), 8192, 0, false };
	if (json.buffer == NULL) {
		return;
	}
	json.buffer[0] = '\0';
	json_state(&json, client->ctl->server);
	if (!json.overflow) {
		json_addf(&json, "\n");
		client_write(client, json.buffer, json.len);
	}
	free(json.buffer);
}

static int parse_int(const char *text, int fallback) {
	if (text == NULL || *text == '\0') {
		return fallback;
	}
	return atoi(text);
}

/* Applies a runtime setting and reports whether it was a known key, so the
 * shell can show a real error instead of a silent no-op. */
static bool apply_setting(struct bgwl_server *server, const char *key,
		const char *value) {
	struct bgwl_config *cfg = &server->cfg;
	bool boolean;
	int layout;

	if (strcmp(key, "layout") == 0 && (layout = bgwl_parse_layout(value)) >= 0) {
		cfg->layout = (enum bgwl_layout)layout;
		struct bgwl_workspace *ws;
		wl_list_for_each(ws, &server->workspaces, link) {
			bgwl_workspace_set_layout(ws, cfg->layout);
		}
		return true;
	}
	if (strcmp(key, "gap-inner") == 0) {
		cfg->gap_inner = bgwl_max(0, parse_int(value, cfg->gap_inner));
		bgwl_reflow(server);
		return true;
	}
	if (strcmp(key, "gap-outer") == 0) {
		cfg->gap_outer = bgwl_max(0, parse_int(value, cfg->gap_outer));
		bgwl_reflow(server);
		return true;
	}
	if (strcmp(key, "border-width") == 0) {
		cfg->border_width = bgwl_max(0, parse_int(value, cfg->border_width));
		struct bgwl_toplevel *toplevel;
		wl_list_for_each(toplevel, &server->toplevels, link) {
			bgwl_toplevel_update_appearance(toplevel);
		}
		return true;
	}
	if (strcmp(key, "accent") == 0 || strcmp(key, "accent-color") == 0) {
		if (!bgwl_parse_color(value, cfg->accent_color)) {
			return false;
		}
		struct bgwl_workspace *ws;
		wl_list_for_each(ws, &server->workspaces, link) {
			memcpy(ws->accent, cfg->accent_color, sizeof(ws->accent));
		}
		bgwl_reflow(server);
		return true;
	}
	if (strcmp(key, "animations") == 0 && bgwl_parse_bool(value, &boolean)) {
		cfg->animations = boolean;
		return true;
	}
	if (strcmp(key, "anim-duration") == 0) {
		cfg->anim_duration_ms = bgwl_max(0, parse_int(value, 140));
		return true;
	}
	if (strcmp(key, "focus-follows-mouse") == 0 &&
			bgwl_parse_bool(value, &boolean)) {
		cfg->focus_follows_mouse = boolean;
		return true;
	}
	if (strcmp(key, "idle-timeout") == 0) {
		cfg->idle_timeout_seconds = bgwl_max(0, parse_int(value, 300));
		return true;
	}
	if (strcmp(key, "terminal") == 0 || strcmp(key, "launcher") == 0 ||
			strcmp(key, "file-manager") == 0 ||
			strcmp(key, "screenshot") == 0) {
		char **slot = strcmp(key, "terminal") == 0 ? &cfg->terminal
			: strcmp(key, "launcher") == 0 ? &cfg->launcher
			: strcmp(key, "file-manager") == 0 ? &cfg->file_manager
			: &cfg->screenshot;
		free(*slot);
		*slot = strdup(value);
		return true;
	}
	if (strcmp(key, "background") == 0) {
		free(cfg->bg_image);
		cfg->bg_image = value[0] != '\0' ? strdup(value) : NULL;
		struct bgwl_output *output;
		wl_list_for_each(output, &server->outputs, link) {
			bgwl_output_update_background(output);
		}
		return true;
	}
	if (strcmp(key, "panel-visible") == 0) {
		if (server->panel != NULL) {
			bgwl_panel_toggle(server);
		}
		return true;
	}
	if (strcmp(key, "ws-layout") == 0 && server->active_ws != NULL &&
			(layout = bgwl_parse_layout(value)) >= 0) {
		bgwl_workspace_set_layout(server->active_ws, (enum bgwl_layout)layout);
		return true;
	}
	if (strcmp(key, "ws-name") == 0 && server->active_ws != NULL) {
		snprintf(server->active_ws->name, BGWL_NAME_LEN, "%s", value);
		return true;
	}
	if (strcmp(key, "ws-accent") == 0 && server->active_ws != NULL) {
		if (!bgwl_parse_color(value, server->active_ws->accent)) {
			return false;
		}
		bgwl_reflow(server);
		return true;
	}
	return false;
}

static void reply(struct bgwl_ctl_client *client, const char *text) {
	client_write(client, text, strlen(text));
}

void bgwl_ctl_handle_command(struct bgwl_server *server,
		struct bgwl_ctl_client *client, const char *line) {
	char command[BGWL_CTL_LINE_MAX];
	snprintf(command, sizeof(command), "%s", line);

	char *save = NULL;
	char *verb = strtok_r(command, " \t", &save);
	if (verb == NULL) {
		return;
	}

	/* Every command that changes state answers with a fresh snapshot; one
	 * round trip per interaction keeps the panel honest. */
	bool changed = false;

	if (strcmp(verb, "state") == 0) {
		send_state(client);
		return;
	}
	if (strcmp(verb, "subscribe") == 0) {
		client->subscribed = true;
		send_state(client);
		return;
	}
	if (strcmp(verb, "ping") == 0) {
		reply(client, "{\"type\":\"pong\"}\n");
		return;
	}

	struct bgwl_toplevel *focused = bgwl_server_focused_toplevel(server);
	struct bgwl_workspace *ws = server->active_ws;

	if (strcmp(verb, "workspace") == 0) {
		char *arg = strtok_r(NULL, " \t", &save);
		if (arg == NULL) {
			send_state(client);
			return;
		}
		struct bgwl_workspace *target = NULL;
		if (strcmp(arg, "next") == 0 && ws != NULL) {
			target = bgwl_workspace_by_index(server,
				(ws->index + 1) % server->cfg.workspace_count);
		} else if (strcmp(arg, "prev") == 0 && ws != NULL) {
			target = bgwl_workspace_by_index(server,
				(ws->index - 1 + server->cfg.workspace_count)
					% server->cfg.workspace_count);
		} else {
			target = bgwl_workspace_by_index(server, atoi(arg));
		}
		if (target != NULL) {
			bgwl_workspace_switch(server, target, true);
			changed = true;
		}
	} else if (strcmp(verb, "send") == 0) {
		char *arg = strtok_r(NULL, " \t", &save);
		struct bgwl_workspace *target = arg != NULL
			? bgwl_workspace_by_index(server, atoi(arg)) : NULL;
		if (target == NULL && arg != NULL && strcmp(arg, "next") == 0 && ws) {
			target = bgwl_workspace_by_index(server,
				(ws->index + 1) % server->cfg.workspace_count);
		}
		if (target != NULL && focused != NULL) {
			bgwl_workspace_send_window(focused, target);
			changed = true;
		}
	} else if (strcmp(verb, "close") == 0) {
		if (focused != NULL) {
			bgwl_toplevel_close(focused);
			changed = true;
		}
	} else if (strcmp(verb, "maximize") == 0 ||
			strcmp(verb, "fullscreen") == 0 || strcmp(verb, "pin") == 0 ||
			strcmp(verb, "sticky") == 0) {
		if (focused != NULL) {
			if (strcmp(verb, "maximize") == 0) {
				bgwl_toplevel_set_maximized(focused, !focused->maximized);
			} else if (strcmp(verb, "fullscreen") == 0) {
				bgwl_toplevel_set_fullscreen(focused,
					!focused->fullscreen);
			} else if (strcmp(verb, "pin") == 0) {
				focused->above = !focused->above;
				bgwl_toplevel_raise(focused);
			} else {
				focused->sticky = !focused->sticky;
			}
			changed = true;
		}
	} else if (strcmp(verb, "focus") == 0) {
		char *arg = strtok_r(NULL, " \t", &save);
		int id = arg != NULL ? atoi(arg) : -1;
		struct bgwl_toplevel *toplevel;
		wl_list_for_each(toplevel, &server->toplevels, link) {
			if (toplevel->id == id) {
				/* Clicking a window in another stream has to bring that
				 * stream forward first, or the focus would go to a window the
				 * user cannot see. */
				if (toplevel->ws != NULL && toplevel->ws != ws) {
					bgwl_workspace_switch(server, toplevel->ws, true);
				}
				bgwl_toplevel_focus(toplevel);
				changed = true;
				break;
			}
		}
	} else if (strcmp(verb, "master") == 0) {
		char *arg = strtok_r(NULL, " \t", &save);
		int direction = arg != NULL && arg[0] == '-' ? -1 : 1;
		if (ws != NULL) {
			bgwl_tiling_adjust_master(ws, direction);
			changed = true;
		}
	} else if (strcmp(verb, "master-count") == 0) {
		char *arg = strtok_r(NULL, " \t", &save);
		int direction = arg != NULL && arg[0] == '-' ? -1 : 1;
		if (ws != NULL) {
			bgwl_tiling_rotate_stack(ws, direction);
			changed = true;
		}
	} else if (strcmp(verb, "reflow") == 0) {
		bgwl_reflow(server);
		changed = true;
	} else if (strcmp(verb, "lock") == 0) {
		/* "lock" запускает локер; сам локер говорит "lock begin", когда готов
		 * закрыть экран, и "lock end" после успешной аутентификации. */
		char *arg = strtok_r(NULL, " \t", &save);
		if (arg != NULL && strcmp(arg, "begin") == 0) {
			bgwl_server_set_locked(server, true);
		} else if (arg != NULL && strcmp(arg, "end") == 0) {
			if (bgwl_session_ext_lock_active(server)) {
				reply(client, "{\"type\":\"error\",\"error\":"
					"\"ext-session-lock owns the screen\"}\n");
				return;
			}
			bgwl_server_set_locked(server, false);
		} else {
			bgwl_run_action(server, server->cfg.lock_command);
		}
	} else if (strcmp(verb, "run") == 0) {
		char *rest = strtok_r(NULL, "\n", &save);
		if (rest != NULL) {
			bgwl_run_action(server, rest);
		}
	} else if (strcmp(verb, "quit") == 0) {
		bgwl_server_terminate(server);
		return;
	} else if (strcmp(verb, "set") == 0) {
		char *key = strtok_r(NULL, " \t", &save);
		char *value = strtok_r(NULL, "", &save);
		if (key == NULL || value == NULL) {
			reply(client, "{\"type\":\"error\",\"error\":\"usage: set key value\"}\n");
			return;
		}
		/* trim() the value: the shell sends "set background /path". */
		while (*value == ' ' || *value == '\t') {
			value++;
		}
		if (!apply_setting(server, key, value)) {
			char scratch[BGWL_CTL_LINE_MAX];
			snprintf(scratch, sizeof(scratch),
				"{\"type\":\"error\",\"error\":\"unknown or bad setting\","
				"\"key\":\"%s\"}\n", key);
			reply(client, scratch);
			return;
		}
		changed = true;
	} else {
		reply(client, "{\"type\":\"error\",\"error\":\"unknown command\"}\n");
		return;
	}

	(void)changed;
	send_state(client);
}

/* --- server side -------------------------------------------------------- */

void bgwl_ctl_broadcast(struct bgwl_server *server, const char *event) {
	struct bgwl_ctl *ctl = server->ctl;
	if (ctl == NULL) {
		return;
	}

	struct bgwl_json json = { calloc(256, 1), 256, 0, false };
	if (json.buffer == NULL) {
		return;
	}
	json.buffer[0] = '\0';
	json_addf(&json, "EV %s\n", event);

	struct bgwl_ctl_client *client;
	wl_list_for_each(client, &ctl->clients, link) {
		if (client->subscribed && !json.overflow) {
			client_write(client, json.buffer, json.len);
		}
	}
	free(json.buffer);
}

static int listen_handle_fd(int fd, uint32_t mask, void *data) {
	struct bgwl_ctl *ctl = data;

	int client_fd = accept4(ctl->listen_fd, NULL, NULL, SOCK_NONBLOCK);
	if (client_fd < 0) {
		return 1;
	}

	struct bgwl_ctl_client *client = calloc(1, sizeof(*client));
	if (client == NULL) {
		close(client_fd);
		return 1;
	}
	client->ctl = ctl;
	client->fd = client_fd;
	wl_list_init(&client->link);

	struct wl_event_loop *loop = wl_display_get_event_loop(
		ctl->server->wl_display);
	client->source = wl_event_loop_add_fd(loop, client_fd,
		WL_EVENT_READABLE, client_handle_fd, client);
	if (client->source == NULL) {
		close(client_fd);
		free(client);
		return 1;
	}
	wl_list_insert(&ctl->clients, &client->link);
	wlr_log(WLR_DEBUG, "ctl client connected");
	return 1;
}

struct bgwl_ctl *bgwl_ctl_create(struct bgwl_server *server) {
	const char *runtime = bgwl_runtime_dir();
	if (runtime == NULL) {
		wlr_log(WLR_ERROR, "no runtime directory: the control socket is off");
		return NULL;
	}

	struct bgwl_ctl *ctl = calloc(1, sizeof(*ctl));
	if (ctl == NULL) {
		return NULL;
	}
	ctl->server = server;
	wl_list_init(&ctl->clients);

	char path[PATH_MAX];
	snprintf(path, sizeof(path), "%s/" BGWL_CTL_SOCKET_NAME, runtime);

	ctl->listen_fd = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC,
		0);
	if (ctl->listen_fd < 0) {
		wlr_log(WLR_ERROR, "ctl socket() failed: %s", strerror(errno));
		free(ctl);
		return NULL;
	}

	struct sockaddr_un addr = { .sun_family = AF_UNIX };
	if (strlen(path) >= sizeof(addr.sun_path)) {
		/* sun_path is 108 bytes on Linux; a HOME under a deep tree can
		 * genuinely overflow it, and truncating silently would bind a socket
		 * nobody can find. */
		wlr_log(WLR_ERROR, "control socket path too long: %s", path);
		close(ctl->listen_fd);
		free(ctl);
		return NULL;
	}
	snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", path);
	/* A stale socket from a crashed session would make bind() fail; unlinking
	 * first is safe because the path is private to this user's runtime dir. */
	unlink(path);

	if (bind(ctl->listen_fd, (struct sockaddr *)&addr, sizeof(addr)) != 0) {
		wlr_log(WLR_ERROR, "ctl bind(%s) failed: %s", path, strerror(errno));
		close(ctl->listen_fd);
		free(ctl);
		return NULL;
	}
	chmod(path, 0600);
	if (listen(ctl->listen_fd, BGWL_CTL_BACKLOG) != 0) {
		wlr_log(WLR_ERROR, "ctl listen failed: %s", strerror(errno));
		close(ctl->listen_fd);
		unlink(path);
		free(ctl);
		return NULL;
	}

	struct wl_event_loop *loop = wl_display_get_event_loop(server->wl_display);
	ctl->source = wl_event_loop_add_fd(loop, ctl->listen_fd, WL_EVENT_READABLE,
		listen_handle_fd, ctl);
	if (ctl->source == NULL) {
		wlr_log(WLR_ERROR, "could not watch the control socket");
		close(ctl->listen_fd);
		unlink(path);
		free(ctl);
		return NULL;
	}

	wlr_log(WLR_INFO, "control socket ready at %s", path);
	return ctl;
}

void bgwl_ctl_destroy(struct bgwl_ctl *ctl) {
	if (ctl == NULL) {
		return;
	}
	struct bgwl_ctl_client *client, *tmp;
	wl_list_for_each_safe(client, tmp, &ctl->clients, link) {
		wl_event_source_remove(client->source);
		close(client->fd);
		wl_list_remove(&client->link);
		free(client);
	}
	if (ctl->source != NULL) {
		wl_event_source_remove(ctl->source);
	}
	if (ctl->listen_fd >= 0) {
		char path[PATH_MAX];
		snprintf(path, sizeof(path), "%s/" BGWL_CTL_SOCKET_NAME,
			bgwl_runtime_dir() != NULL ? bgwl_runtime_dir() : "/tmp");
		unlink(path);
		close(ctl->listen_fd);
	}
	free(ctl);
}
