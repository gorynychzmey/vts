// Runs exactly one scenario and reports its result to the parent over IPC.
// Kept separate from run.mjs so the parent never imports scenario code into its
// own process: a scenario that leaks a handle, wedges a browser, or throws at
// import time then affects only its own child.
const file = process.argv[2];
let payload;
try {
  const mod = await import(file);
  const label = mod.name || file.split("/").pop();
  try {
    payload = { label, failures: await mod.run() };
  } catch (e) {
    payload = { label, failures: ["threw: " + e.message] };
  }
} catch (e) {
  // An import-time failure has no mod.name to report under, so fall back to the
  // filename — the same label run.mjs would have used.
  payload = { label: file.split("/").pop(), failures: ["threw: " + e.message] };
}
await new Promise((resolve) => process.send(payload, resolve));
// Scenarios close their own browsers and servers, but a stray handle would keep
// this child alive and stall the pool, so exit explicitly once the result is out.
process.exit(0);
