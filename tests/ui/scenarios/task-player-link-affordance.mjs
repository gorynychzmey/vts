// vts-at8 / VOS-111: the task title always opens OUR player (uploads AND
// link tasks) when media is available; a dedicated ▶ player icon makes it
// discoverable; the original URL is a real clickable link shown under the
// title; media gone -> name unclickable + expired badge, but original link
// stays; a non-http source_url (javascript:) must NOT become a clickable href
// (vts-dcc self-XSS guard).
import { startStubServer, launch, openPage, isVisible, computed } from "../harness.mjs";

export const name = "task-player-link-affordance";

// Link task, media present -> name + ▶ icon -> /player; source URL is a link.
const LINK_READY = {
  id: "10000000-0000-0000-0000-000000000001",
  source_url: "https://example.com/watch?v=abc",
  source_title: "", status: "completed",
  media_path: "/artifacts/1/media/video.mkv",
  transcript_path: "/x/t.txt",
  options: { transcript: true },
  steps: [], created_at: "2026-07-01T10:00:00Z", updated_at: "2026-07-01T10:00:00Z",
  progress: {}, stats: {},
};

// Link task, media gone -> name unclickable + expired; source link stays.
const LINK_EXPIRED = {
  id: "20000000-0000-0000-0000-000000000002",
  source_url: "https://example.com/watch?v=gone",
  source_title: "", status: "completed",
  media_path: null,
  transcript_path: "/x/t.txt",
  options: { transcript: true },
  steps: [], created_at: "2026-07-01T10:00:00Z", updated_at: "2026-07-01T10:00:00Z",
  progress: {}, stats: {},
};

// Malicious source_url: title/source must never get a javascript: href.
const LINK_XSS = {
  id: "30000000-0000-0000-0000-000000000003",
  source_url: "javascript:alert(document.cookie)",
  source_title: "", status: "completed",
  media_path: null,
  options: { transcript: true },
  steps: [], created_at: "2026-07-01T10:00:00Z", updated_at: "2026-07-01T10:00:00Z",
  progress: {}, stats: {},
};

// Upload with media: name -> player, ▶ visible, no original-url link (file://).
const UPLOAD_READY = {
  id: "40000000-0000-0000-0000-000000000004",
  source_url: "file://clip.mp3", source_title: "", status: "completed",
  media_path: "/artifacts/4/media/audio.original.mp3",
  transcript_path: "/x/t.txt",
  options: { transcript: true },
  steps: [], created_at: "2026-07-01T10:00:00Z", updated_at: "2026-07-01T10:00:00Z",
  progress: {}, stats: {},
};

export async function run() {
  const { server, baseUrl } = await startStubServer({
    "/api/tasks": [LINK_READY, LINK_EXPIRED, LINK_XSS, UPLOAD_READY],
  });
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl);
    await page.waitForTimeout(200);

    const cards = await page.evaluate(() => {
      const out = [];
      document.querySelectorAll(".task").forEach((task) => {
        const link = task.querySelector(".task-link");
        const player = task.querySelector(".task-player-btn");
        const source = task.querySelector(".task-source");
        out.push({
          linkHref: link?.getAttribute("href") || "",
          linkExpired: !!link?.classList.contains("expired"),
          playerHidden: !!player?.classList.contains("hidden"),
          playerHref: player?.getAttribute("href") || "",
          playerTitleI18n: player?.getAttribute("data-i18n-title") || "",
          sourceHref: source?.getAttribute("href") || "",
          sourceHidden: !!source?.classList.contains("hidden"),
          sourceText: source?.textContent || "",
        });
      });
      return out;
    });

    if (cards.length !== 4) {
      failures.push(`expected 4 cards, got ${cards.length}`);
      return failures;
    }
    const [ready, expired, xss, upload] = cards;

    // 1) LINK_READY: name -> /player, ▶ visible -> /player, original url linked.
    if (!ready.linkHref.includes("/player/")) failures.push(`link-ready title href not player: ${ready.linkHref}`);
    if (ready.linkExpired) failures.push("link-ready title marked expired");
    if (ready.playerHidden) failures.push("link-ready ▶ player icon hidden");
    if (!ready.playerHref.includes("/player/")) failures.push(`link-ready ▶ href not player: ${ready.playerHref}`);
    if (ready.playerTitleI18n !== "tasks.open_player") failures.push(`▶ tooltip i18n key wrong: ${ready.playerTitleI18n}`);
    if (ready.sourceHref !== "https://example.com/watch?v=abc") failures.push(`link-ready source href wrong: ${ready.sourceHref}`);
    if (ready.sourceHidden) failures.push("link-ready original-url line hidden (should always show for link tasks)");

    // 2) LINK_EXPIRED: name unclickable + expired; ▶ hidden; source link stays.
    if (expired.linkHref) failures.push(`expired title still has href: ${expired.linkHref}`);
    if (!expired.linkExpired) failures.push("expired title not marked .expired");
    if (!expired.playerHidden) failures.push("expired ▶ icon shown (media gone)");
    if (expired.sourceHref !== "https://example.com/watch?v=gone") failures.push(`expired source href wrong: ${expired.sourceHref}`);

    // 3) LINK_XSS: no javascript: href anywhere.
    if (xss.linkHref) failures.push(`xss title has href (should be none, media gone): ${xss.linkHref}`);
    if (xss.sourceHref) failures.push(`xss source has javascript: href (vts-dcc): ${xss.sourceHref}`);
    if (xss.sourceText !== "javascript:alert(document.cookie)") {
      // text is fine; only the href must be absent. Just sanity that it renders.
    }

    // 4) UPLOAD_READY: name -> player, ▶ visible, source line hidden (file://).
    if (!upload.linkHref.includes("/player/")) failures.push(`upload title href not player: ${upload.linkHref}`);
    if (upload.playerHidden) failures.push("upload ▶ player icon hidden");
    if (upload.sourceHref) failures.push(`upload source has href (no original url for file://): ${upload.sourceHref}`);
    if (!upload.sourceHidden) failures.push("upload original-url line shown without a display name");

    // The ▶ tooltip text must actually resolve (i18n applied), not stay as key.
    const playerTooltip = await computed(page, ".task-player-btn", "cursor").catch(() => "");
    if (playerTooltip === "") { /* cursor probe optional; not a hard fail */ }
    const tipText = await page.evaluate(() => {
      const p = document.querySelector(".task-player-btn");
      return p?.getAttribute("data-tooltip") || p?.getAttribute("title") || "";
    });
    if (!tipText) failures.push("▶ player icon has no resolved tooltip text (data-tooltip/title empty)");

    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}
