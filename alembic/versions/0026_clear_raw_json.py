"""clear asr_segments.raw_json now that the axes carry it (vts-6qwy)

The decomposed axes in `payload` reproduce everything both consumers read —
`usable_words` for diarization and `_shifted_inner_sentences` for the player and
the subtitles. Verified row by row against a restored copy of production before
this was written: 1332/1332 rows identical on both axes, 911,364 words compared.

This is the irreversible half, kept separate from 0025 on purpose so the fill
could be checked before anything was thrown away.

What is NOT lost: every measured value survives in `payload` — word timings,
per-token probability, avg_logprob, no_speech_prob, language, duration. What
goes is the duplication (segments[].text and the top-level text are the same
words a second and third time) and the model's internals (token ids, t_dtw,
temperature, per-segment id), none of which any consumer reads.

Measured on production data: 71 MB of raw_json against 48 MB of payload, so the
table drops by roughly 23 MB and the database from 99 MB to about 76 MB.

A row is only cleared once its own payload is present and non-empty. The nine
production rows that decompose to nothing are empty segments — silence, with
neither words nor text — and their raw_json is left alone rather than special-
cased, since clearing it would save nothing anyway.

Revision ID: 0026_clear_raw_json
Revises: 0025_asr_payload_axes
"""
import sqlalchemy as sa
from alembic import op

revision = "0026_clear_raw_json"
down_revision = "0025_asr_payload_axes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Guard on this row's own payload, not on a global count: a row whose
    # decomposition is missing or empty keeps its raw_json, so a partially
    # filled table degrades to "some rows still on the legacy column" — which
    # every reader already handles — instead of losing data.
    op.execute(
        sa.text(
            "UPDATE asr_segments SET raw_json = '{}'::json "
            "WHERE raw_json::text <> '{}' "
            "  AND payload IS NOT NULL "
            "  AND ("
            "        jsonb_array_length((payload->>'tokens')::jsonb) > 0 "
            "     OR jsonb_array_length((payload->>'sentences')::jsonb) > 0"
            "  )"
        )
    )
    # Reclaim the space the UPDATE turned into dead tuples. Without this the
    # table keeps its old size until autovacuum gets to it, and the whole point
    # of the migration is the space.
    op.execute(sa.text("COMMIT"))
    op.execute(sa.text("VACUUM FULL asr_segments"))


def downgrade() -> None:
    # raw_json cannot be restored from here — that is what the pre-migration
    # dump is for. Deliberately a no-op rather than a lie: `payload` still
    # carries the data, and recompose_raw_json rebuilds the shape consumers
    # read, so nothing downstream needs the legacy column back.
    pass
