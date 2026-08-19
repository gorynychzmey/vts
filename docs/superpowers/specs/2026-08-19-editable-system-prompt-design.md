# vts-kujy — Editable system prompt with vendor restore

**Beads:** vts-kujy
**Date:** 2026-08-19
**Status:** Design approved, ready for implementation plan

## Scope

Turn the final-summary system prompt into something each user can edit, and
let them put the vendor's version back.

**In scope:**
- `global_prompt.md` becomes a per-user row in `prompts`, created on first use.
- Editing it is the ordinary prompt CRUD — no new endpoint.
- Restoring the vendor text reuses `DELETE`: the row goes, the next request
  recreates it from the file.
- A confirmation dialog before `DELETE`, worded for what the button actually
  does in each case.
- A startup refresh that rewrites every copy the user has not edited, so a
  newly shipped prompt reaches them.

**Out of scope, deliberately:**
- `segment_prompt.md` and `pack_prompt.md`. They stay file-only; see
  *One prompt, not three*.
- Telling the user their copy is out of date, or asking before refreshing it.
  An untouched copy is refreshed silently, because it carries nothing of
  theirs to lose.
- Sharing one edited prompt across users, or an admin-level override.

## Background — what "system prompt" means today

`SYSTEM_PROMPTS` (`vts/services/prompt_registry.py`) holds one entry:
`summary` → `global_prompt.md`. A task refers to it as
`{"source": "system", "id": "summary"}`, exactly as it refers to a user prompt
by uuid.

The reference resolves by reading the file. `GET /api/prompts/system/{key}/text`
(`vts/api/routers/meta.py:192`) calls `load_prompt(settings.prompts_dir, ...)`
and returns the text; there is no write path. In the editor the prompt opens
with `editable=false`, so the body is read-only and the delete button is
hidden.

So the prompt is visible and selectable but not editable — which is the gap
this closes.

## Where the vendor text lives

The vendor's copy stays in `prompts/global_prompt.md`, exactly where it is
now. Nothing is copied into the database to serve as a reference.

This is what makes restore free. The alternative — storing the vendor text
beside the user's — buys the ability to say "the vendor updated this prompt",
and costs a second copy that has to be refreshed on every deploy, plus a
decision about what to do when the user never edited theirs. Deleting a row
and reading the file again achieves the restore without any of that.

Once a user's copy exists, later edits to `global_prompt.md` do not reach it
on their own. *Refreshing untouched copies* below is what closes that gap.

## Model

One column on `Prompt`:

```python
is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
```

`false` is a user prompt, which is every row that exists today; `true` is a
user's copy of a vendor prompt. A boolean rather than a key, because there is
one system prompt and a `boolean → varchar` migration is cheap if a second
ever appears.

A partial unique index guards the lazy creation:

```sql
CREATE UNIQUE INDEX ix_prompts_one_system_per_user
    ON prompts (user_id) WHERE is_system;
```

Without it, a worker and the API creating the copy at the same moment produce
two rows and the user sees a duplicate. With it the loser gets an
`IntegrityError` and re-reads.

`updated_at` also changes — see *`updated_at` becomes explicit* below.

Alembic migration: add `is_system` with `server_default='false'` and drop the
server default; make `updated_at` nullable; create the index. Existing rows
keep their `updated_at`, which is correct — they are user prompts, and their
timestamps mean what they always meant.

## Lazy creation

```python
async def get_or_create_system_prompt(session, user_id: UUID) -> Prompt
```

Looks for the user's row with `is_system=true`; if there is none, reads
`global_prompt.md` and inserts one. On `IntegrityError` from the index it
re-reads rather than failing — that is the race resolving correctly.

It lives in the service layer because two callers need it. The API needs it
when the user opens the prompt list. The **pipeline** needs it too: a task
runs in the worker, and a user who has never opened the UI has no copy yet.
Resolving `{"source": "system", "id": "summary"}` goes through this function
instead of reading the file.

Creating on demand rather than at registration means no hook in the signup
path, and a row lost for any reason simply comes back.

## Refreshing untouched copies

A user's copy is frozen at the moment it was made, so a better prompt shipped
later would never reach anyone who had already run a summary. On startup,
every copy that still matches the vendor file is rewritten from it; every copy
the user has edited is left alone.

**"Edited" is recorded, not inferred.** `updated_at` answers exactly one
question — *when did the user change this?* — and `NULL` means *never*. The
refresh rewrites `WHERE is_system AND updated_at IS NULL`.

Two ways of inferring it were considered and both fail:

*Comparing the copy to the file* breaks on the second release. A copy made
from v1 and never touched does not match v2 either, so every untouched copy
reads as edited the moment a new prompt ships — which is precisely when the
refresh needs to work.

*Storing the file's mtime* does not survive the build: measured on the
production host, `global_prompt.md` has mtime `19:45:23` inside the container
against `19:29:32` in the checkout, because the timestamp is set when the file
is copied into the image. Every rebuild would mark every untouched copy as
stale.

### `updated_at` becomes explicit

The column loses `default=utcnow` and `onupdate=utcnow` and becomes nullable.
Both are filled in by hand instead:

| path | `updated_at` |
|---|---|
| user creates a prompt | `utcnow()` |
| system copy is created | `NULL` — the user has not touched it |
| any prompt is edited | `utcnow()` |

Verified against SQLAlchemy: a plain `session.add(...)` with `updated_at=None`
does **not** keep the `NULL` — the column default overrides it, and the row
lands with the current time. Only a Core `insert()` bypasses the default. So
the choice is between working around the ORM in one function and hoping
nobody replaces it with `session.add()` later, or removing the default and
assigning explicitly everywhere. The second is more code and less to go wrong.

`created_at` keeps its default: it is always "now" and never `NULL`, so
automation fits it. The asymmetry is deliberate — `updated_at` now carries
meaning rather than just a timestamp, which is what earns it an explicit
assignment.

The blast radius is small: `Prompt` is constructed in exactly one place in
`vts/db/repo.py` and updated in one other.

**Where it runs.** In the `migrate` initContainer, alongside `alembic upgrade
head` (`docker/vts-entrypoint.sh`). That is the one place that executes once
per deploy, before either `webapi` or `worker` starts serving — so there is no
race between two processes doing a mass UPDATE, and no window where the worker
creates a copy from the old file after the refresh has run.

The refresh logs how many copies it rewrote and how many it skipped as edited.
An operation that silently rewrites user data leaves nothing to reason from
when someone asks why their prompt changed.

Edge case, accepted: a user who edited the prompt and then typed the original
text back has a copy indistinguishable from an untouched one, and it will be
refreshed. The text is identical either way, so nothing is lost.

## Restore is `DELETE`

`DELETE /api/prompts/{id}` needs no change at all. `Repo.delete_prompt`
(`vts/db/repo.py`) removes the row and nothing else, and lazy creation brings
the vendor text back on the next request.

This works because a task refers to the system prompt **by key**, not by row
id: `{"source": "system", "id": "summary"}` still resolves after the row is
gone. (A user prompt is referenced by uuid, so deleting one does leave a
dangling reference — pre-existing behaviour, untouched here.)

## UI

One button, two meanings, decided by `is_system` on the prompt:

| | user prompt | system prompt |
|---|---|---|
| button label | Delete | Restore |
| tooltip | removes the prompt | puts the vendor's version back |
| after a 204 | refresh the list | refresh the list, then re-open the prompt so the restored text is visible immediately |

The visual treatment and position do not change — it is the same button in
the same place, and the editor already hides or shows it via
`syncPromptEditorState`. The system prompt becomes `editable=true`, so the
button appears there without new plumbing.

In the list itself the system prompt looks like any other. The difference
belongs in the editor, where the action is.

**`is_system` must be exposed in `PromptOut`**, or the frontend cannot tell
the two cases apart.

## Confirmation before deleting

There is none today: `prompt-delete-btn` fires `DELETE` on click
(`vts/static/app.js:5399`), and a deleted user prompt is unrecoverable.

Both actions get a `window.confirm`, worded for what actually happens:

- system: restoring discards the user's edits, and they cannot be recovered
- user: the prompt is gone for good

Adding it for the user prompt is outside this feature's strict scope, but
confirming only the *reversible* action while the irreversible one fires
silently would be worse than either choice on its own.

Two i18n keys in all three locales (`confirm.prompt_restore`,
`confirm.prompt_delete`), plus the label and tooltip for the restore case.
`docs/ui-inventory.md` is generated — regenerate it with `make ui-inventory`
rather than editing.

## One prompt, not three

`segment_prompt.md` and `pack_prompt.md` are also read from disk on every run
and are not in `SYSTEM_PROMPTS`. Making them editable is the same mechanism,
but each needs its own registry entry, its own row, and a way to tell the
copies apart — which is exactly the `boolean → varchar` migration this design
defers.

`segment_prompt.md` is the more tempting of the two, since it governs the
cleanup stage. It is also the one under active tuning, where the vendor text
changing under the user is a feature rather than a nuisance — so freezing a
per-user copy of it right now would work against the experiments in progress.

## Error handling

| situation | behaviour |
|---|---|
| two callers create the copy at once | the unique index rejects one; it re-reads and returns the winner's row |
| `global_prompt.md` missing or unreadable | `load_prompt`'s existing fallback applies; the copy is created from it rather than failing the task |
| user deletes their copy mid-task | the reference resolves by key, so the next resolution recreates it |
| `DELETE` on a prompt of another user | unchanged: `Repo.delete_prompt` filters by `user_id` and returns 404 |

## Testing

- lazy creation from the API path and from the pipeline path
- a second call returns the same row rather than creating another
- concurrent creation yields exactly one row (the index does its job)
- an edit survives and is what the pipeline uses
- `DELETE` followed by a resolution returns the vendor text
- the migration sets `is_system=false` on existing rows
- the startup refresh rewrites a copy with `updated_at IS NULL` and leaves an
  edited one alone
- a system copy is created with `updated_at = NULL`, and editing it sets a
  timestamp — the guard against a future `session.add()` quietly restoring the
  column default
- an untouched copy is still refreshed after the vendor prompt changes twice
  (the case that defeats comparing text to the current file)
- the refresh reports the counts it acted on
- `PromptOut` carries `is_system`
- UI: the button reads "Restore" for the system prompt and "Delete"
  otherwise; both paths confirm first; a cancelled confirmation sends no
  request; after restoring, the editor shows the vendor text without a manual
  reload
