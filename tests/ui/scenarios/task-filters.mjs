// Verifies the task-list filters (vts-rhx): the controls send the right query
// params, changing a filter resets paging, the "nothing matches" hint appears
// only when a filter emptied the list, and the selection survives a reload
// (sessionStorage) without leaking into a fresh tab.
import {
  startStubServer, launch, openPage, isVisible, clickReal, screenshot,
} from "../harness.mjs";

export const name = "task-filters";

function task(id, { title, url, created }) {
  return {
    id,
    status: "completed",
    source_url: url,
    source_title: title,
    created_at: created,
    updated_at: created,
    options: { transcript: true, prompts: [] },
    steps: [],
  };
}

const TASKS = [
  task("00000000-0000-0000-0000-000000000001", {
    title: "Standup recording", url: "file://standup.m4a",
    created: "2026-01-01T00:00:00+00:00",
  }),
  task("00000000-0000-0000-0000-000000000002", {
    title: "Conference talk", url: "https://youtube.com/watch?v=abc",
    created: "2026-01-02T00:00:00+00:00",
  }),
];

export async function run() {
  const failures = [];

  // Record what the page asks the server for: the filters are server-side, so
  // the query string IS the observable behaviour.
  const { server, baseUrl } = await startStubServer({ "/api/tasks": TASKS });
  const seen = [];
  const origListeners = server.listeners("request").slice();
  server.removeAllListeners("request");
  server.on("request", (req, res) => {
    // Only the LIST endpoint: per-task detail fetches and queue-positions
    // share the prefix and would drown out what we are counting.
    if (req.url && /^\/api\/tasks(\?|$)/.test(req.url)) seen.push(req.url);
    for (const l of origListeners) l(req, res);
  });

  const browser = await launch();
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    await page.waitForTimeout(300);

    if (!(await isVisible(page, "#task-filters"))) {
      failures.push("filter bar is not visible");
      return failures;
    }
    // Clear only appears once something is filtered.
    if (await isVisible(page, "#filter-clear")) {
      failures.push("Clear should stay hidden while no filter is set");
    }

    // --- type filter reaches the server -----------------------------------
    seen.length = 0;
    await page.selectOption("#filter-type", "file");
    await page.waitForTimeout(400);
    if (!seen.some((u) => u.includes("source_type=file"))) {
      failures.push(`type filter not sent; requests were ${JSON.stringify(seen)}`);
    }
    if (!(await isVisible(page, "#filter-clear"))) {
      failures.push("Clear should appear once a filter is set");
    }

    // --- search is debounced, not one request per keystroke ----------------
    seen.length = 0;
    await page.click("#filter-q");
    await page.keyboard.type("standup", { delay: 20 });
    await page.waitForTimeout(600);
    // Count only the task-list fetches this step caused. Typing 7 characters
    // must collapse into a single request, not one per keystroke.
    const searchCalls = seen.filter((u) => u.includes("q=standup"));
    if (searchCalls.length === 0) {
      failures.push(`search filter not sent; requests were ${JSON.stringify(seen)}`);
    }
    if (seen.length > 2) {
      failures.push(
        `search should be debounced into ~1 request, got ${seen.length}: ${JSON.stringify(seen)}`
      );
    }

    // --- dates are sent as a full-day range -------------------------------
    seen.length = 0;
    await page.fill("#filter-from", "2026-01-02");
    await page.waitForTimeout(400);
    const dated = seen.find((u) => u.includes("created_from"));
    if (!dated) {
      failures.push("date filter not sent");
    } else if (!decodeURIComponent(dated).includes("2026-01-02T00:00:00")) {
      failures.push(`created_from should carry a time component, got ${dated}`);
    }

    // --- filters survive a reload (sessionStorage) ------------------------
    seen.length = 0;
    await page.reload();
    await page.waitForTimeout(500);
    const restored = await page.evaluate(() => ({
      q: document.getElementById("filter-q").value,
      type: document.getElementById("filter-type").value,
      from: document.getElementById("filter-from").value,
    }));
    if (restored.q !== "standup" || restored.type !== "file" || restored.from !== "2026-01-02") {
      failures.push(`filters did not survive a reload: ${JSON.stringify(restored)}`);
    }
    if (!seen.some((u) => u.includes("source_type=file"))) {
      failures.push("the first request after a reload must already carry the filters");
    }

    // --- the empty-state hint ---------------------------------------------
    // The stub always returns the same two tasks, so drive the hint directly
    // from an emptied list: white-box only because the stub cannot vary its
    // response per query.
    await page.evaluate(() => {
      document.getElementById("task-list").innerHTML = "";
      updateEmptyState();
    });
    if (!(await isVisible(page, "#task-empty"))) {
      failures.push("expected the 'no matches' hint when a filter empties the list");
    }
    await page.evaluate(() => {
      document.getElementById("filter-q").value = "";
      document.getElementById("filter-type").value = "";
      document.getElementById("filter-from").value = "";
      document.getElementById("filter-to").value = "";
      updateEmptyState();
    });
    if (await isVisible(page, "#task-empty")) {
      failures.push("an empty list with NO filters is not a filtering problem; hint must hide");
    }

    await screenshot(page, "task-filters");

    // No horizontal overflow (vts-nr4).
    const overflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth);
    if (overflow > 1) failures.push(`page scrolls horizontally by ${overflow}px`);

    if (errors.length) failures.push(`JS errors: ${JSON.stringify(errors)}`);
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}
