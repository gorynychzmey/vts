"""decompose asr_segments.raw_json into payload axes (vts-6qwy)

`raw_json` holds whisper's whole answer, most of which is duplicated
(segments[].text and the top-level text are the same words again) or internal
to the model (token ids, t_dtw, temperature). Following the owner's rule, it is
not deleted but brought into a usable form: `payload` = {tokens, sentences,
meta} — see vts/services/asr_payload.py for what each axis is and why both
granularities are needed.

Measured at 34% of the original on a realistic chunk (1440 subword tokens
across 90 sentences), with no measured value lost — probability, avg_logprob
and no_speech_prob all survive, because they describe the quality of the source
MATERIAL and cannot be recovered without re-running ASR.

This migration ADDS the column and fills it. It deliberately does NOT clear
raw_json: that step is separate and irreversible, and should only run once the
decomposition has been verified against the originals it came from.

Revision ID: 0025_asr_payload_axes
Revises: 0024_clear_stale_awaiting_step
"""
import json

import sqlalchemy as sa
from alembic import op

revision = "0025_asr_payload_axes"
down_revision = "0024_clear_stale_awaiting_step"
branch_labels = None
depends_on = None

_BATCH = 200


def upgrade() -> None:
    op.add_column("asr_segments", sa.Column("payload", sa.JSON(), nullable=True))

    # Decompose in batches: a recording's payload runs to hundreds of KB, and
    # loading every row at once would hold the whole table in memory.
    from vts.services.asr_payload import decompose_raw_json

    conn = op.get_bind()
    last_id = None
    while True:
        query = sa.text(
            "SELECT id, raw_json FROM asr_segments "
            "WHERE payload IS NULL AND raw_json IS NOT NULL"
            + (" AND id > :last" if last_id else "")
            + " ORDER BY id LIMIT :limit"
        )
        params = {"limit": _BATCH}
        if last_id:
            params["last"] = last_id
        rows = conn.execute(query, params).fetchall()
        if not rows:
            break
        for row_id, raw in rows:
            # Postgres JSON comes back parsed; a text column would not.
            if isinstance(raw, str):
                try:
                    raw = json.loads(raw)
                except ValueError:
                    raw = None
            decomposed = decompose_raw_json(raw)
            conn.execute(
                sa.text("UPDATE asr_segments SET payload = :p WHERE id = :i"),
                {"p": json.dumps(decomposed, ensure_ascii=False), "i": row_id},
            )
            last_id = row_id


def downgrade() -> None:
    # raw_json was never touched, so dropping the derived column loses nothing.
    op.drop_column("asr_segments", "payload")
