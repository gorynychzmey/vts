// Redesign v2, stage 4: status colour on the task card itself.
//
// The status used to live only in the pill at the right edge, so scanning a
// long list meant reading every pill. The card now also carries a status-*
// class, which drives its border colour and a dot in the header row.
//
// The dot is a flex sibling of .task-main, which never declared a grow factor —
// adding it made the title shrink-wrap and read as centred. The alignment
// assertion below exists to keep that from coming back.
import { startStubServer, launch, openPage } from "../harness.mjs";

export const name = "task-status-dot";

const iso = new Date(Date.now() - 3600e3).toISOString();
const mk = (id, status, title) => ({
  id, status, source_url: `https://youtube.com/watch?v=${id}`, source_title: title,
  display_name: title, created_at: iso, updated_at: iso,
  options: { transcript: true, prompts: [] }, steps: [],
});

const TASKS = [
  mk("a", "running", "Running task"),
  mk("b", "awaiting_input", "Needs review"),
  mk("c", "failed", "Failed task"),
  mk("d", "queued", "Queued task"),
  mk("e", "completed", "Completed task"),
  mk("f", "archived", "Archived task"),
];

export async function run() {
  const { server, baseUrl } = await startStubServer({ "/api/tasks": TASKS });
  const browser = await launch();
  const failures = [];
  try {
    const { page, errors } = await openPage(browser, baseUrl, { width: 1100, height: 1200 });
    if (errors.length) failures.push("JS errors: " + JSON.stringify(errors));

    const cards = await page.evaluate(() =>
      [...document.querySelectorAll(".task")].map((t) => {
        const dot = t.querySelector(".task-dot");
        const link = t.querySelector(".task-link");
        const main = t.querySelector(".task-main");
        return {
          status: [...t.classList].filter((c) => c.startsWith("status-")),
          border: getComputedStyle(t).borderTopColor,
          dotBg: dot ? getComputedStyle(dot).backgroundColor : null,
          // Measure the CELL against the CARD, not the title against the cell:
          // without the grow factor the title still sits at its cell's left
          // edge — it is the whole cell that shrink-wraps and drifts right.
          cardLeft: Math.round(t.getBoundingClientRect().left),
          cardWidth: Math.round(t.getBoundingClientRect().width),
          mainLeft: main ? Math.round(main.getBoundingClientRect().left) : null,
          mainWidth: main ? Math.round(main.getBoundingClientRect().width) : null,
          title: link?.textContent?.trim(),
        };
      }),
    );

    if (cards.length !== TASKS.length) failures.push(`expected ${TASKS.length} cards, got ${cards.length}`);

    for (const [i, c] of cards.entries()) {
      const want = `status-${TASKS[i].status}`;
      if (!c.status.includes(want)) failures.push(`card ${i}: classes ${JSON.stringify(c.status)}, expected ${want}`);
      if (c.status.length !== 1) failures.push(`card ${i}: stale status classes ${JSON.stringify(c.status)}`);
      if (!c.dotBg || /rgba\(0, 0, 0, 0\)/.test(c.dotBg)) failures.push(`card ${i}: dot has no colour`);
      // The title must be rendered, and its cell must actually fill the row.
      if (!c.title) failures.push(`card ${i}: no title rendered`);
      // The dot plus padding is ~40px; anything beyond that means the cell
      // stopped growing and the content drifted toward the middle.
      if (c.mainLeft !== null && c.mainLeft - c.cardLeft > 60)
        failures.push(`card ${i}: content starts ${c.mainLeft - c.cardLeft}px into the card — cell is not filling the row`);
      // A shrink-wrapped cell is a fraction of the card; a growing one is most of it.
      if (c.mainWidth !== null && c.mainWidth < c.cardWidth * 0.4)
        failures.push(`card ${i}: cell is ${c.mainWidth}px of ${c.cardWidth}px — not growing`);
    }

    // Distinct states must be distinguishable, not all one accent colour.
    const dots = new Set(cards.map((c) => c.dotBg));
    if (dots.size < 4) failures.push(`only ${dots.size} distinct dot colours across 6 statuses`);
    const borders = new Set(cards.map((c) => c.border));
    if (borders.size < 4) failures.push(`only ${borders.size} distinct card borders across 6 statuses`);

    // The class must be replaced on transition, not accumulated.
    await page.evaluate(() => {
      const card = document.querySelector(".task");
      if (card && card._runtime) {
        card._runtime.baseStatus = "failed";
        renderTaskRuntime(card);
      }
    });
    await page.waitForTimeout(150);
    const after = await page.evaluate(() =>
      [...document.querySelector(".task").classList].filter((c) => c.startsWith("status-")),
    );
    if (after.length !== 1 || after[0] !== "status-failed")
      failures.push(`after transition the card carries ${JSON.stringify(after)}, expected only status-failed`);
  } finally {
    await browser.close();
    server.close();
  }
  return failures;
}
