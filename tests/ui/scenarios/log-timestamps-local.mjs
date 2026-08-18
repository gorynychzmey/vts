// The pipeline writes "YYYY-MM-DD HH:MM:SS,mmm" at the start of each log line in
// the SERVER's zone — in practice UTC, because the `timezone` setting that would
// change it is not configured. Reading 13:37 for something that happened at
// 15:37 local made the log hard to line up with anything the user remembers.
//
// Converted for DISPLAY only: the file on disk keeps its original stamps so
// grepping it and correlating with other server logs still works.
//
// Runs the page in a FIXED timezone rather than the host's, so the expected
// output is a constant instead of whatever the machine happens to be set to.
import { chromium } from "playwright";
import { startStubServer } from "../harness.mjs";

export const name = "log-timestamps-local";

const ISO = "2026-08-18T09:00:00Z";
const TASK = {
  id: "t1", status: "completed",
  source_url: "https://y/a", source_title: "T", display_name: "T",
  created_at: ISO, updated_at: ISO,
  media_path: "/m.mp4", transcript_path: "/t.txt",
  options: { transcript: true, prompts: [] }, steps: [],
};

// Second line crosses midnight in Europe/Berlin (+02:00 in August), so a naive
// time-only shift that forgets to move the DATE would be caught here.
// Third line has a timestamp INSIDE the message: it must be left alone, or the
// converter is corrupting content rather than relabelling it.
const LOG =
  "2026-08-18 13:37:02,579 INFO step started\n" +
  "2026-08-18 22:30:00,000 INFO late step\n" +
  "2026-08-18 13:40:00,000 INFO copied file 2026-01-01 00:00:00,000 to outputs\n";

export async function run() {
  const { server, baseUrl } = await startStubServer({
    "/api/tasks": [TASK],
    "/api/tasks/t1/log": LOG,
  });
  const browser = await chromium.launch();
  const failures = [];
  try {
    // Berlin is UTC+2 in August, so 13:37 -> 15:37 and 22:30 -> 00:30 the NEXT day.
    const context = await browser.newContext({
      viewport: { width: 1200, height: 950 },
      timezoneId: "Europe/Berlin",
    });
    const page = await context.newPage();
    const errors = [];
    page.on("pageerror", (e) => errors.push("pageerror: " + e.message));
    await page.goto(baseUrl, { waitUntil: "networkidle" });
    await page.waitForTimeout(300);

    await page.click(".task .toggle-btn");
    await page.waitForSelector('.task .tab-btn[data-tab="log"]', { state: "visible" });
    await page.click('.task .tab-btn[data-tab="log"]');
    // The log panel polls, so wait for content rather than a fixed sleep.
    await page
      .waitForFunction(
        () => (document.querySelector(".task .tab-content.log")?.textContent || "").includes("INFO"),
        null,
        { timeout: 5000 },
      )
      .catch(() => {});

    const text = await page.evaluate(
      () => document.querySelector(".task .tab-content.log")?.textContent || ""
    );

    if (!text.includes("INFO")) {
      failures.push("the log panel rendered no log at all — this check is vacuous");
      return failures;
    }
    // Still UTC on screen: the conversion did not happen.
    if (text.includes("2026-08-18 13:37:02,579")) {
      failures.push("log timestamps are still in the server's zone (13:37 shown for a 15:37 local event)");
    }
    if (!text.includes("2026-08-18 15:37:02,579")) {
      failures.push(`expected the first line at 15:37:02,579 local, got: ${JSON.stringify(text.slice(0, 60))}`);
    }
    // Crossing midnight must move the DATE, not just the clock.
    if (!text.includes("2026-08-19 00:30:00,000")) {
      failures.push(
        `a 22:30 UTC line must render as 00:30 on the NEXT day in Berlin; got: ${JSON.stringify(text)}`
      );
    }
    // A timestamp inside a message is content, not a log stamp.
    if (!text.includes("2026-01-01 00:00:00,000 to outputs")) {
      failures.push("a timestamp inside a log MESSAGE was rewritten — that corrupts content");
    }

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}
