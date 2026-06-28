// Negative test: inject the exact bad CSS that shipped (display:flex on the
// closed restart dialog) via the stub server's __extraCss, and confirm the
// harness OBSERVES the dialog as visible-when-closed — i.e. the verifier can
// actually catch this class of regression, not just pass everything.
import { startStubServer, launch, openPage, isVisible } from "./harness.mjs";

const BAD_CSS = "#restart-final-dialog { display: flex !important; }";

const { server, baseUrl } = await startStubServer({ __extraCss: BAD_CSS });
const browser = await launch();
let detected = false;
try {
  const { page } = await openPage(browser, baseUrl);
  // With the bad CSS, the CLOSED dialog should be visible — the harness must see it.
  detected = await isVisible(page, "#restart-final-dialog");
} finally {
  await browser.close();
  server.close();
}
console.log(detected
  ? "SELF-CHECK PASSED: harness detects the closed-dialog-visible regression"
  : "SELF-CHECK FAILED: harness did NOT detect the injected regression");
process.exit(detected ? 0 : 1);
