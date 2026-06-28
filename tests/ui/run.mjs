// Runs every scenario in scenarios/, prints a summary, exits non-zero on any failure.
import fs from "fs";
import path from "path";
import url from "url";

const here = path.dirname(url.fileURLToPath(import.meta.url));
const scenDir = path.join(here, "scenarios");
const files = fs.readdirSync(scenDir).filter((f) => f.endsWith(".mjs")).sort();

let anyFail = false;
for (const file of files) {
  const mod = await import(path.join(scenDir, file));
  const label = mod.name || file;
  let failures;
  try {
    failures = await mod.run();
  } catch (e) {
    failures = ["threw: " + e.message];
  }
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
