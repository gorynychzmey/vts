// A file shared from Android must land in the upload list (reported 2026-09-02).
//
// Chrome installs the PWA, offers it in the share sheet, and even switches the
// form to File mode — but the file never appears in the list. The service
// worker's half works: it stashes the payload and redirects with
// ?share_pending=file. The page's half then wrote the file straight into
// `fileInput.files`, which stopped being the source of truth when multi-file
// upload landed: the visible list is built from `stagedFiles`, and nothing put
// the file there.
//
// The service worker cannot run against a plain http stub, so this drives the
// receiver the same way the browser does: land on /?share_pending=file with
// /_share_inbox serving the payload.
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { launch, openPage } from "../harness.mjs";

export const name = "share-target-file-handoff";

const STATIC = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)), "../../../vts/static",
);
const SHARED_NAME = "созвон запись.m4a";

export async function run() {
  const failures = [];

  const server = http.createServer((req, res) => {
    const [url] = req.url.split("?");
    if (url === "/_share_inbox") {
      // What the service worker would have cached: the payload plus the
      // original filename, which multipart does not otherwise preserve.
      res.setHeader("Content-Type", "audio/mp4");
      res.setHeader("X-Share-Filename", encodeURIComponent(SHARED_NAME));
      res.end(Buffer.from("fake audio bytes"));
      return;
    }
    if (url.startsWith("/api/")) {
      res.setHeader("Content-Type", "application/json");
      res.end(JSON.stringify(url === "/api/tasks" ? [] : {}));
      return;
    }
    const f = url === "/" ? "/index.html" : url.replace("/static/", "/");
    const fp = path.join(STATIC, f);
    if (!fp.startsWith(STATIC) || !fs.existsSync(fp)) { res.statusCode = 404; res.end("nf"); return; }
    const ext = path.extname(fp);
    const ct = ext === ".js" ? "text/javascript" : ext === ".css" ? "text/css"
      : ext === ".json" ? "application/json" : ext === ".webmanifest" ? "application/manifest+json"
      : "text/html";
    res.setHeader("Content-Type", `${ct}; charset=utf-8`);
    res.end(fs.readFileSync(fp));
  });
  await new Promise((resolve) => server.listen(0, resolve));

  const browser = await launch();
  try {
    const base = `http://127.0.0.1:${server.address().port}`;
    const { page } = await openPage(browser, `${base}/?share_pending=file`);

    // The list is what the user actually sees, so that is what is asserted —
    // not the hidden input the old code filled.
    const staged = await page.waitForFunction(
      (want) => {
        const rows = [...document.querySelectorAll("#file-list .file-row")];
        return rows.length === 1 && rows[0].textContent.includes(want) ? rows.length : null;
      },
      SHARED_NAME,
      { timeout: 8000 },
    ).then(() => true).catch(() => false);

    if (!staged) {
      const seen = await page.evaluate(() => ({
        listHtmlLength: (document.getElementById("file-list")?.innerHTML || "").length,
        hiddenInputCount: document.getElementById("file-input")?.files?.length ?? -1,
        sourceIsFile: document.getElementById("source-type-file")?.checked ?? null,
      }));
      failures.push(
        `the shared file did not reach the upload list: ${JSON.stringify(seen)}`,
      );
    }

    // The marker must be gone so a reload does not re-add the same file.
    const stillMarked = await page.evaluate(() => window.location.search.includes("share_pending"));
    if (stillMarked) {
      failures.push("share_pending survived in the URL — a reload would re-import the file");
    }
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}
