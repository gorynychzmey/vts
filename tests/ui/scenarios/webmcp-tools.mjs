import { startStubServer, launch } from "../harness.mjs";

export const name = "webmcp-tools";

export async function run() {
  const failures = [];
  const { server, baseUrl } = await startStubServer({
    "/api/search": { hits: [{ recording_id: "rec-1", text: "matched" }], total: 1 },
    "/api/recordings/rec-1/transcript": { recording_id: "rec-1", content: "full text" },
    "/api/recordings": { items: [{ id: "rec-1" }], total: 1 },
    "/api/speakers": [{ id: "person-1", name: "Test Person" }],
  });
  const browser = await launch();
  try {
    const page = await browser.newPage();
    await page.addInitScript(() => {
      window.__registeredWebMcpTools = [];
      Object.defineProperty(document, "modelContext", {
        configurable: true,
        value: {
          async registerTool(tool) { window.__registeredWebMcpTools.push(tool); },
        },
      });
    });
    await page.goto(baseUrl, { waitUntil: "networkidle" });
    await page.waitForFunction(() => window.__registeredWebMcpTools.length === 17);

    const result = await page.evaluate(async () => {
      const byName = Object.fromEntries(window.__registeredWebMcpTools.map((tool) => [tool.name, tool]));
      const controller = new AbortController();
      return {
        names: Object.keys(byName).sort(),
        readsAnnotated: ["search_transcripts", "get_recording_transcript", "list_recordings", "list_people"]
          .every((name) => byName[name].annotations?.readOnlyHint === true && byName[name].annotations?.untrustedContentHint === true),
        deletesConsequential: ["delete_task", "delete_prompt", "delete_preset"]
          .every((name) => byName[name].annotations?.consequentialHint === true),
        search: await byName.search_transcripts.execute({ query: "needle", limit: 3 }, { signal: controller.signal }),
        transcript: await byName.get_recording_transcript.execute({ recording_id: "rec-1" }, { signal: controller.signal }),
        recordings: await byName.list_recordings.execute({}, { signal: controller.signal }),
        people: await byName.list_people.execute({}, { signal: controller.signal }),
        submitted: await byName.submit_video.execute({ url: "https://example.com/video" }, { signal: controller.signal }),
        renamed: await byName.rename_recording.execute({ recording_id: "rec-1", display_name: "New name" }, { signal: controller.signal }),
      };
    });
    const expected = ["create_preset", "create_prompt", "delete_preset", "delete_prompt", "delete_task",
      "get_default_preset", "get_recording_transcript", "list_people", "list_presets", "list_prompts",
      "list_recordings", "rename_recording", "search_transcripts", "set_default_preset", "submit_video",
      "update_preset", "update_prompt"];
    if (JSON.stringify(result.names) !== JSON.stringify(expected)) failures.push(`tool names: ${JSON.stringify(result.names)}`);
    if (!result.readsAnnotated) failures.push("read tools lack read-only/untrusted-content annotations");
    if (!result.deletesConsequential) failures.push("delete tools lack consequential annotations");
    if (result.search.hits?.[0]?.text !== "matched") failures.push("search did not reuse /api/search");
    if (result.transcript.content !== "full text") failures.push("transcript did not reuse recording endpoint");
    if (result.recordings.total !== 1) failures.push("recording list did not reuse recording endpoint");
    if (result.people[0]?.name !== "Test Person") failures.push("people did not reuse speaker endpoint");
    if (result.submitted.status !== "ok") failures.push("submit did not reuse task endpoint");
    if (result.renamed.status !== "ok") failures.push("rename did not reuse recording endpoint");
  } catch (error) {
    failures.push(String(error));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}
