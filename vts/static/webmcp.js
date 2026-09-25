/* Browser-facing adapter for VTS capabilities.
 *
 * The HTTP handlers remain the application boundary: using `api()` here means
 * WebMCP gets the same session, acting-user scope, validation, and ownership
 * checks as the visible UI.  No corpus or artifact logic belongs in this file.
 */
(function registerVtsWebMcp() {
  const context = document.modelContext;
  if (!context || typeof context.registerTool !== "function") return;

  const readOnly = {
    readOnlyHint: true,
    consequentialHint: false,
    // Transcript text is user-provided data and must not be treated as agent
    // instructions merely because it arrived through a tool.
    untrustedContentHint: true,
  };
  const create = { readOnlyHint: false, consequentialHint: false };
  const update = { readOnlyHint: false, consequentialHint: false };
  const destructive = { readOnlyHint: false, consequentialHint: true };

  function jsonRequest(path, method, body, signal) {
    return api(path, {
      method,
      signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }

  function queryPath(path, values) {
    const params = new URLSearchParams();
    for (const [key, value] of Object.entries(values)) {
      if (value !== undefined && value !== null && value !== "") {
        params.set(key, String(value));
      }
    }
    const query = params.toString();
    return query ? `${path}?${query}` : path;
  }

  function boundedInteger(value, fallback, minimum, maximum, name) {
    const result = value === undefined ? fallback : Number(value);
    if (!Number.isInteger(result) || result < minimum || result > maximum) {
      throw new Error(`${name} must be an integer from ${minimum} to ${maximum}`);
    }
    return result;
  }

  const tools = [
    {
      name: "search_transcripts",
      title: "Search transcripts",
      description: "Search the current user's recording transcripts and return relevant timed passages. An empty hits array means no passage cleared the relevance threshold.",
      inputSchema: {
        type: "object",
        properties: {
          query: { type: "string", description: "Natural-language search query." },
          limit: { type: "integer", minimum: 1, maximum: 100, default: 20 },
          offset: { type: "integer", minimum: 0, default: 0 },
          threshold: { type: "number", minimum: 0, maximum: 1 },
          recording_id: { type: "string", format: "uuid" },
          person: { type: "string", description: "Only recordings featuring this person." },
          created_from: { type: "string", format: "date-time" },
          created_to: { type: "string", format: "date-time" },
        },
        required: ["query"],
        additionalProperties: false,
      },
      annotations: readOnly,
      async execute(input, options) {
        const query = String(input.query || "").trim();
        if (!query) throw new Error("query must not be empty");
        return api(queryPath("/api/search", {
          q: query,
          limit: boundedInteger(input.limit, 20, 1, 100, "limit"),
          offset: boundedInteger(input.offset, 0, 0, 10000, "offset"),
          threshold: input.threshold,
          recording_id: input.recording_id,
          person: input.person,
          created_from: input.created_from,
          created_to: input.created_to,
        }), { signal: options?.signal });
      },
    },
    {
      name: "get_recording_transcript",
      title: "Get recording transcript",
      description: "Read a full recording transcript, or timed lines around a search result. Use the stable recording_id returned by search_transcripts or list_recordings.",
      inputSchema: {
        type: "object",
        properties: {
          recording_id: { type: "string", format: "uuid" },
          variant: { type: "string", enum: ["raw", "redacted", "summary"], default: "raw" },
          around_sec: { type: "number", minimum: 0 },
          window_sec: { type: "number", minimum: 1, maximum: 600, default: 60 },
        },
        required: ["recording_id"],
        additionalProperties: false,
      },
      annotations: readOnly,
      async execute(input, options) {
        if (!input.recording_id) throw new Error("recording_id is required");
        return api(queryPath(
          `/api/recordings/${encodeURIComponent(input.recording_id)}/transcript`,
          { variant: input.variant || "raw", around_sec: input.around_sec, window_sec: input.window_sec }
        ), { signal: options?.signal });
      },
    },
    {
      name: "list_recordings",
      title: "List recordings",
      description: "List recordings belonging to the current user, newest first, with artifact availability and stable recording IDs.",
      inputSchema: {
        type: "object",
        properties: {
          limit: { type: "integer", minimum: 1, maximum: 200, default: 50 },
          offset: { type: "integer", minimum: 0, default: 0 },
          query: { type: "string", description: "Filter recording titles." },
          created_from: { type: "string", format: "date-time" },
          created_to: { type: "string", format: "date-time" },
        },
        additionalProperties: false,
      },
      annotations: readOnly,
      async execute(input, options) {
        return api(queryPath("/api/recordings", {
          limit: boundedInteger(input.limit, 50, 1, 200, "limit"),
          offset: boundedInteger(input.offset, 0, 0, 10000, "offset"),
          q: input.query,
          created_from: input.created_from,
          created_to: input.created_to,
        }), { signal: options?.signal });
      },
    },
    {
      name: "list_people",
      title: "List known people",
      description: "List people in the current user's voice registry for use when narrowing transcript searches.",
      inputSchema: { type: "object", properties: {}, additionalProperties: false },
      annotations: readOnly,
      async execute(_input, options) {
        return api("/api/speakers", { signal: options?.signal });
      },
    },
    {
      name: "submit_video", title: "Submit video", annotations: create,
      description: "Submit a video URL to the current user's VTS processing queue.",
      inputSchema: { type: "object", properties: {
        url: { type: "string", format: "uri" }, language: { type: "string" },
        audio_only: { type: "boolean", default: false }, transcript: { type: "boolean", default: true },
        diarize: { type: "boolean", default: false }, prompts: { type: "array", items: { type: "object" } },
        preset: { type: "object" }, delivery: { type: "array", items: { type: "object" } },
      }, required: ["url"], additionalProperties: false },
      execute(input, options) { return jsonRequest("/api/tasks", "POST", input, options?.signal); },
    },
    {
      name: "delete_task", title: "Delete task", annotations: destructive,
      description: "Permanently delete one task, its recording, artifacts, and searchable transcript. Confirm with the user first.",
      inputSchema: { type: "object", properties: { task_id: { type: "string", format: "uuid" } }, required: ["task_id"], additionalProperties: false },
      execute(input, options) { return jsonRequest("/api/tasks", "DELETE", { task_ids: [input.task_id] }, options?.signal); },
    },
    {
      name: "rename_recording", title: "Rename recording", annotations: update,
      description: "Change a recording's display name; an empty name restores the derived title.",
      inputSchema: { type: "object", properties: { recording_id: { type: "string", format: "uuid" }, display_name: { type: ["string", "null"] } }, required: ["recording_id"], additionalProperties: false },
      execute(input, options) { return jsonRequest(`/api/recordings/${encodeURIComponent(input.recording_id)}`, "PATCH", { display_name: input.display_name ?? null }, options?.signal); },
    },
    {
      name: "list_prompts", title: "List prompts", annotations: readOnly,
      description: "List system and user-defined prompts available to the current user.",
      inputSchema: { type: "object", properties: {}, additionalProperties: false },
      execute(_input, options) { return api("/api/prompts", { signal: options?.signal }); },
    },
    {
      name: "create_prompt", title: "Create prompt", annotations: create,
      description: "Create a user-defined processing prompt.",
      inputSchema: { type: "object", properties: { name: { type: "string" }, system_prompt: { type: "string" } }, required: ["name", "system_prompt"], additionalProperties: false },
      execute(input, options) { return jsonRequest("/api/prompts", "POST", input, options?.signal); },
    },
    {
      name: "update_prompt", title: "Update prompt", annotations: update,
      description: "Update the name and/or body of a user-defined prompt.",
      inputSchema: { type: "object", properties: { prompt_id: { type: "string", format: "uuid" }, name: { type: "string" }, system_prompt: { type: "string" } }, required: ["prompt_id"], additionalProperties: false },
      execute(input, options) { const { prompt_id, ...body } = input; return jsonRequest(`/api/prompts/${encodeURIComponent(prompt_id)}`, "PATCH", body, options?.signal); },
    },
    {
      name: "delete_prompt", title: "Delete prompt", annotations: destructive,
      description: "Permanently delete a user-defined prompt.",
      inputSchema: { type: "object", properties: { prompt_id: { type: "string", format: "uuid" } }, required: ["prompt_id"], additionalProperties: false },
      execute(input, options) { return api(`/api/prompts/${encodeURIComponent(input.prompt_id)}`, { method: "DELETE", signal: options?.signal }); },
    },
    {
      name: "list_presets", title: "List presets", annotations: readOnly,
      description: "List system and user-defined processing presets.",
      inputSchema: { type: "object", properties: {}, additionalProperties: false },
      execute(_input, options) { return api("/api/presets", { signal: options?.signal }); },
    },
    {
      name: "create_preset", title: "Create preset", annotations: create,
      description: "Create a user-defined processing preset.",
      inputSchema: { type: "object", properties: { name: { type: "string" }, options: { type: "object" } }, required: ["name", "options"], additionalProperties: false },
      execute(input, options) { return jsonRequest("/api/presets", "POST", input, options?.signal); },
    },
    {
      name: "update_preset", title: "Update preset", annotations: update,
      description: "Update the name and/or options of a user-defined preset.",
      inputSchema: { type: "object", properties: { preset_id: { type: "string", format: "uuid" }, name: { type: "string" }, options: { type: "object" } }, required: ["preset_id"], additionalProperties: false },
      execute(input, options) { const { preset_id, ...body } = input; return jsonRequest(`/api/presets/${encodeURIComponent(preset_id)}`, "PATCH", body, options?.signal); },
    },
    {
      name: "delete_preset", title: "Delete preset", annotations: destructive,
      description: "Permanently delete a user-defined processing preset.",
      inputSchema: { type: "object", properties: { preset_id: { type: "string", format: "uuid" } }, required: ["preset_id"], additionalProperties: false },
      execute(input, options) { return api(`/api/presets/${encodeURIComponent(input.preset_id)}`, { method: "DELETE", signal: options?.signal }); },
    },
    {
      name: "get_default_preset", title: "Get default preset", annotations: readOnly,
      description: "Get the current user's default processing preset reference.",
      inputSchema: { type: "object", properties: {}, additionalProperties: false },
      execute(_input, options) { return api("/api/me/default_preset", { signal: options?.signal }); },
    },
    {
      name: "set_default_preset", title: "Set default preset", annotations: update,
      description: "Set the current user's default system or user processing preset.",
      inputSchema: { type: "object", properties: { source: { type: "string", enum: ["system", "user"] }, id: { type: "string" } }, required: ["source", "id"], additionalProperties: false },
      execute(input, options) { return jsonRequest("/api/me/default_preset", "PUT", input, options?.signal); },
    },
  ];

  // Registration can be disabled by Permissions Policy or an experimental
  // browser flag. That must not affect the human-facing application.
  Promise.allSettled(tools.map((tool) => context.registerTool(tool))).then((results) => {
    results.forEach((result, index) => {
      if (result.status === "rejected") {
        console.debug(`WebMCP tool ${tools[index].name} was not registered`, result.reason);
      }
    });
  });
}());
