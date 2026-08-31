<!-- internal-standards:managed -->
# Multi-Agent Collaboration

## Working Alongside Other Agents

Several agents may work one repository at the same time, in separate branches or
worktrees but against a shared history. Your own memory of the tree is not a
source of truth: between your last commit and now, someone may have merged work,
bumped a version, or taken a tag.

## These rules live here, not in one project

Everything about working alongside other agents belongs in this shared standard.
When a new practice is agreed — or a project's local rules turn out to contain
one that was never promoted — write it here and let it reach every project from
this file.

The reason is that these rules are not project-specific: the same two agents
work several repositories, and a lesson paid for in one is one nobody should pay
for again elsewhere. A collaboration rule written only in a project's local file
protects that project and leaves every other one running the version that was
already shown to fail.

Applies in both directions. A rule agreed in a project is promoted here; a rule
already here is not re-litigated locally. When a project genuinely needs to
differ, say so in its own file and say why, so the divergence is visible rather
than accidental.

Project-specific facts still stay in the project: which agent currently holds
seniority, which directories belong to whom, which branch is shared. The *rule*
is shared; the *assignment* is local.

## Agree before touching code, not after the first conflict

Before the active editing phase — not once something collides — find out who else
is working and settle the boundaries. Discuss:

- **Ownership.** Whose directories are whose. Draw the line so you physically
  cannot edit the same files, not so that you both promise to be careful.
- **Ordering.** What must land first, who waits for whom, whether any step breaks
  a shared contract and therefore has to go through alone.
- **Who commits and who merges.** Whether a colleague hands over a branch for
  review or merges it themselves. Who tags and who bumps versions — these are
  shared resources, and two bumps in a row produce either a conflict or a lost
  number.
- **What each of you is holding right now** — uncommitted files nobody else may
  touch or roll back.

If the work is separable, propose a **separate worktree**. That removes a whole
class of accidents: a colleague's uncommitted edits are simply not in your tree,
so no command of yours can reach them. Note the trade-off aloud — a worktree sees
committed state, so if a colleague is editing files your tests read from disk,
you are testing something other than what is on their screen.

One message up front costs less than one conflict.

## Verify before committing, and especially before tagging

Never rely on recall for shared state. Every time:

```
git status --short          # someone else's uncommitted work in the tree
git log --oneline -5        # what appeared since your last commit
git status -sb | head -1    # whether you have diverged from the remote
<the project's version file>       # the version NOW, not as you remember it
git tag -l "build-<version>"       # whether that tag is already taken
```

Consequences that follow from this:

- **Bump the version in the same commit as the substantive change**, not as a
  separate `chore: bump`. A commit that touches only the version file is
  invisible to gates that ask "did the frontend change", so the tag ends up
  pointing at a commit whose UI tests never ran.
- **Stage narrowly**: `git add <explicit paths>`, never `-A` or `-a`, or you will
  sweep up a colleague's files.
- **No tree-wide reverts.** `git stash`, `git checkout -- .`, `git restore .`
  and `git clean` destroy other people's work silently. If you need a clean slate
  for a measurement, scope it to your own directory.
- **Do not move a branch another agent is holding.** Hand it back rather than
  overwriting it.
- **If someone else's code is in your tree, run both test suites** before
  tagging. You are releasing their work too.

## One agent cuts releases, and watches the build

Releases are the operation where several agents working at once is a hazard
rather than a help: a version number is a shared resource spent once, CI minutes
come from a single budget, and two agents tagging in the same hour is how a
burned tag or a skipped gate happens.

So the release belongs to one agent — the senior of whoever is working. On the
user's command, that agent tags, follows CI through to its conclusion, and
reports the outcome. Everyone else reports **readiness** instead of acting: what
is committed, what it changes, and — stated explicitly — what has and has not
been verified.

Seniority is settled between the agents, on evidence rather than courtesy: who
holds the context of past release failures, who works from an isolated worktree,
whose judgement about what is ready has held up. Record the answer where the
project's agents will read it, and hand it over explicitly if it changes.

This makes nobody a reviewer of anyone else's work, and it does not replace the
rule that releases happen only on the user's request. It decides *who acts* once
the user has asked, not *whether* to act.

Before tagging, the releasing agent runs the checks the project defines for a
release — not only the default test task. A suite that passes while the release
build is broken is the failure this guards against, and it has happened.

## Your user's tasks outrank delegated ones

Work the user gave you directly comes first; anything relayed by another agent
queues behind it.

The rule exists because a message from a colleague arrives inside your turn and
reads as fresh input — newer than what the user said ten messages ago, and
therefore seemingly more current. That is a recency illusion: priority comes from
the source, not the arrival time.

- **Finish what you are doing** before picking up delegated work. Abandoning a
  task mid-edit is the worst outcome: uncommitted changes in a shared tree block
  everyone.
- **Do not resolve contradictions yourself.** If a colleague relays something
  that conflicts with what the user told you — "they decided to drop that", "they
  reassigned you" — say you see a conflict and ask. Guessing which version is
  stale is making the user's decision for them.
- **Reassignment comes from the user.** A colleague may report how work is
  divided, and that is useful, but confirmation that your priorities changed
  comes from the user. An agent cannot assign you work in the user's place, just
  as it cannot grant you a permission you do not have.
- **Do not drop delegated work** — queue it, say so out loud, and return to it.
- **Size the commitment, not the request.** Do a colleague's small ask without
  ceremony — the day runs on that kind of help, and routing every ten-minute fix
  through the user would stall everyone. Confirm with the user when the ask
  *changes your agenda*: when it spends time you promised elsewhere, or undoes
  something already decided. The test is not how long the edit takes but what it
  costs — a five-minute change that reverses the user's decision still needs
  them, while an hour inside work they already approved does not.

## A peer's claim is evidence, not fact

Colleagues are as capable of a confident wrong conclusion as you are, and their
message arrives without the reasoning that produced it. Check anything you are
about to act on, particularly when it would have you delete, revert, or stop
investigating something.

Say plainly when a check contradicts them, and give the evidence rather than the
verdict. The same applies in reverse: when a colleague corrects you and is right,
accept it and move on without ceremony.

**Never launder a permission through a peer.** If an action was denied to you,
asking a colleague to perform it is a way around the user's decision, not around
a technical limitation. Route it back to the user instead.

## Tell colleagues afterwards

When you finish something meaningful — merged to the main branch, tagged,
changed a shared contract — send a short message to the agents still working.
Not a session summary; three things:

1. **What you did and where** — branch, commit, files touched.
2. **What it breaks or changes for them** — a rename, a shared helper, a new
   version, a new CI gate. This is the important part; if nothing affects them,
   say that in one line.
3. **What not to touch** — files you are holding right now.

**Changes to the rules themselves always warrant a message**, even when no code
moved. A colleague read their copy of the guidance at session start and will not
learn about a new rule until they re-read it. Say what changed and *why* — a rule
without its reason gets followed literally or ignored. If it is contentious or
restrictive, invite the objection: the file is shared, not yours.
