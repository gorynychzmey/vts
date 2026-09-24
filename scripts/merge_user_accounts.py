#!/usr/bin/env python3
"""Give one account another person's — usually their own — new address (vts-ash6).

Written for a renamed mail domain. The person signs in with the new address,
OAuth does not recognise it, `get_or_create_user` hands them an empty account,
and their tasks and library appear to have vanished: both are still there, on
the row keyed by the old address.

This renames that row and absorbs the empty one, so the id everything hangs
off — API tokens, delivery credentials, step weights — never changes.

    python scripts/merge_user_accounts.py --old a@old.example --new a@new.example
    python scripts/merge_user_accounts.py --old ... --new ... --commit
    python scripts/merge_user_accounts.py --old ... --new ... --move-artifacts --commit

Without --commit it reports what it would do and rolls back, so the numbers
below are the ones a commit will produce.

Two things this deliberately does NOT do:

* It does not touch `oauth_allowed_domains`. While the old domain is still
  allowed, a sign-in with the old address creates a fresh empty account again —
  the very state being repaired here. Remove it from the host config separately.
* It does not stop the services. --move-artifacts refuses while any task is in
  flight, but the safe way to run it is with the webapi and worker down.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Run from anywhere, the same way scripts/prodq.py and gen_ui_inventory.py do.
# Running a script BY PATH puts the script's own directory on sys.path, not the
# repo root — and where this script actually runs, inside the application image
# against the live database, `vts` is a source tree at /app rather than an
# installed package. Without this line the invocation printed above dies on the
# next import, before parsing a single argument (two operators hit that during
# a real rename on 2026-09-24 and each worked around it with PYTHONPATH=/app).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from vts.core.config import get_settings
from vts.db.session import SessionLocal
from vts.services.account_merge import MergeReport, apply_artifact_move, merge_accounts


def _render(report: MergeReport, *, committed: bool) -> str:
    lines = [
        f"{'APPLIED' if committed else 'DRY RUN — nothing written'}",
        f"  surviving account : {report.kept_user_id}  ({report.old_username} -> {report.new_username})",
    ]
    if report.absorbed_user_id is None:
        lines.append("  absorbed account  : none — nobody has signed in with the new address")
    else:
        lines.append(f"  absorbed account  : {report.absorbed_user_id} (deleted)")

    lines.append(f"  sessions revoked  : {report.sessions_revoked} (everyone affected signs in again)")

    for title, counts in (
        ("rows moved", report.moved),
        ("duplicates dropped", report.dropped),
        ("paths repointed", report.paths_rewritten),
    ):
        if counts:
            listed = ", ".join(f"{table}={n}" for table, n in sorted(counts.items()))
            lines.append(f"  {title:<17} : {listed}")
        else:
            lines.append(f"  {title:<17} : none")

    if report.artifact_move is not None:
        lines.append(f"  directory move    : {report.artifact_move.source}")
        lines.append(f"                   -> {report.artifact_move.destination}")

    return "\n".join(lines)


async def _run(old: str, new: str, *, commit: bool, move_artifacts: bool) -> int:
    settings = get_settings()
    artifacts_root = Path(settings.artifacts_root) if move_artifacts else None

    async with SessionLocal() as session:
        try:
            report = await merge_accounts(
                session, old_username=old, new_username=new, artifacts_root=artifacts_root
            )
        except (LookupError, RuntimeError) as exc:
            print(f"refused: {exc}", file=sys.stderr)
            return 2

        if not commit:
            await session.rollback()
            print(_render(report, committed=False))
            print("\nRe-run with --commit to apply.")
            return 0

        await session.commit()

    # Only now, and deliberately last. The database is the source of truth and
    # its change was atomic; if the rename below fails, the files are still
    # under the old name and a single `mv` finishes the job. The other order
    # fails less visibly — moved files with nothing recording that they moved.
    if report.artifact_move is not None:
        try:
            apply_artifact_move(report.artifact_move.source, report.artifact_move.destination)
        except OSError as exc:
            print(_render(report, committed=True))
            print(
                f"\nThe database was committed but the directory move failed: {exc}\n"
                f"Stored paths now point at {report.artifact_move.destination}.\n"
                f"Finish by hand:  mv {report.artifact_move.source} "
                f"{report.artifact_move.destination}",
                file=sys.stderr,
            )
            return 1

    print(_render(report, committed=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--old", required=True, help="address of the account that keeps its data")
    parser.add_argument("--new", required=True, help="address it should be known by from now on")
    parser.add_argument(
        "--move-artifacts",
        action="store_true",
        help=(
            "also fold the on-disk artifact directory into the one the new address "
            "hashes to, and repoint the stored paths. Without it nothing on disk "
            "moves and old work keeps reading from the old directory, which is "
            "correct but leaves two directories behind."
        ),
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="write the changes; without it the script only reports what it would do",
    )
    args = parser.parse_args()

    return asyncio.run(
        _run(args.old, args.new, commit=args.commit, move_artifacts=args.move_artifacts)
    )


if __name__ == "__main__":
    raise SystemExit(main())
