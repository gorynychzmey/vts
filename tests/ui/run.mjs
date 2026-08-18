// Runs every scenario in scenarios/, prints a summary, exits non-zero on any failure.
//
// Scenarios run in PARALLEL, one child process each, because the suite is
// dominated by waiting rather than by computing: measured dynamically, 117s of
// the 218s sequential run was unconditional page.waitForTimeout(), and most of
// the rest is page.goto(..., networkidle) at ~600ms a page. That is idle time a
// single process cannot fill, so concurrency — not shaving individual waits —
// is where the wall clock actually is.
//
// Running each scenario in its own PROCESS rather than concurrently in this one
// is deliberate. Scenarios are only independent because each builds its own
// stub server on a dynamic port (`listen(0)`) and its own browser; a process
// boundary makes that isolation structural instead of a property we would have
// to keep re-auditing. It also means a scenario that hard-crashes its child
// cannot take the whole run down with it.
//
// The output contract is unchanged and deterministic: results are collected and
// then printed in the original sorted filename order, never in completion order,
// so CI and the verifier-web skill parse exactly what they parsed before.
import fs from "fs";
import os from "os";
import path from "path";
import url from "url";
import { fork } from "child_process";

const here = path.dirname(url.fileURLToPath(import.meta.url));
const scenDir = path.join(here, "scenarios");
const files = fs.readdirSync(scenDir).filter((f) => f.endsWith(".mjs")).sort();

// Each worker drives a real Chromium, so the ceiling is CPU, not sockets.
// CI (ubuntu-latest) gives 4 cores where a dev box gives 16, and oversubscribing
// a small runner slows the run down instead of speeding it up — so scale to the
// host. The cap of 8 is not a resource limit but a determinism one: past it the
// long-tail scenarios stop being the critical path and extra workers only add
// scheduling noise. VTS_UI_JOBS=1 forces the old sequential behaviour, which is
// the first thing to try when diagnosing a suspected concurrency problem.
const DEFAULT_JOBS = Math.max(1, Math.min(8, (os.availableParallelism?.() ?? os.cpus().length) - 1));
const JOBS = Math.max(1, Number(process.env.VTS_UI_JOBS) || DEFAULT_JOBS);

const CHILD = path.join(here, "scenario-child.mjs");
const results = new Map();
let next = 0;

async function runOne(file) {
  return new Promise((resolve) => {
    const child = fork(CHILD, [path.join(scenDir, file)], { stdio: ["ignore", "ignore", "pipe", "ipc"] });
    let message = null;
    let stderr = "";
    child.stderr.on("data", (c) => { stderr += c.toString(); });
    child.on("message", (m) => { message = m; });
    child.on("error", (e) => { message = { label: file, failures: ["failed to start scenario: " + e.message] }; });
    child.on("exit", (code, signal) => {
      if (message) { resolve(message); return; }
      // No message means the child died before reporting — a crash, an OOM kill,
      // or a top-level import error. Surface it as a failure with whatever it
      // wrote to stderr, rather than letting a dead scenario read as a pass.
      const how = signal ? `killed by ${signal}` : `exited with code ${code}`;
      const tail = stderr.trim().split("\n").slice(-8).join("\n        - ");
      resolve({ label: file, failures: [`scenario process ${how}` + (tail ? `\n        - ${tail}` : "")] });
    });
  });
}

async function worker() {
  while (next < files.length) {
    const file = files[next++];
    results.set(file, await runOne(file));
  }
}

await Promise.all(Array.from({ length: Math.min(JOBS, files.length) }, worker));

let anyFail = false;
for (const file of files) {
  const { label, failures } = results.get(file);
  if (failures.length) {
    anyFail = true;
    console.log(`FAIL  ${label}`);
    for (const f of failures) console.log(`        - ${f}`);
  } else {
    console.log(`PASS  ${label}`);
  }
}
console.log(anyFail ? "\nUI VERIFY: FAILED" : "\nUI VERIFY: PASSED");
process.exit(anyFail ? 1 : 0);
