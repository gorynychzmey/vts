"""semantic chunks of a recording, with embeddings (vts-twe7 / VOS-131)

Chunks hang off the RECORDING rather than the task: the recording is what lasts
(0027), and a corpus that died with its tasks would be no corpus at all.

The embedding column is HALFVEC(1024), not VECTOR. bge-m3 is 1024-dimensional,
so float4 costs 4 KB per chunk against 2 KB in fp16 — and vectors, not text,
become the bulk of this database once a corpus exists (all transcript text today
is 5.6 MB). Measured before choosing: fp16 shifts cosine scores by 0.00001,
0.01% of the 0.379..0.521 band that separates answerable queries from
unanswerable ones, and changes no ranking. TOAST cannot help — dense binary
values barely compress — so the type is the only lever.

The HNSW index uses halfvec_cosine_ops. Cosine rather than L2 because the
codebase already forbids L2 for its other embeddings, and because bge-m3
returns normalised vectors (verified: norm 1.0000), which makes cosine the
natural metric. This is the project's first ANN index of any kind.

Revision ID: 0028_transcript_chunks
Revises: 0027_recordings
"""
import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import HALFVEC
from sqlalchemy.dialects import postgresql

revision = "0028_transcript_chunks"
down_revision = "0027_recordings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "transcript_chunks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("recording_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("start_sec", sa.Float(), nullable=False),
        sa.Column("end_sec", sa.Float(), nullable=False),
        sa.Column("speakers", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        # Nullable: a chunk exists as soon as the text is split, and gets its
        # vector when the embedding pass reaches it. A failed embedding leaves a
        # searchable-by-text row rather than losing the chunk.
        sa.Column("embedding", HALFVEC(1024), nullable=True),
        sa.Column("embedding_model", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["recording_id"], ["recordings.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("recording_id", "chunk_index", name="uq_chunks_recording_index"),
    )
    op.create_index("ix_chunks_recording", "transcript_chunks", ["recording_id"])
    # Scoping a search to one user is the access rule, not an optimisation.
    op.create_index("ix_chunks_user", "transcript_chunks", ["user_id"])

    # HNSW over cosine. Built on an empty table, so it costs nothing now and
    # avoids a rebuild once the corpus is indexed.
    op.execute(
        sa.text(
            "CREATE INDEX ix_chunks_embedding_hnsw ON transcript_chunks "
            "USING hnsw (embedding halfvec_cosine_ops)"
        )
    )


def downgrade() -> None:
    op.drop_index("ix_chunks_embedding_hnsw", table_name="transcript_chunks")
    op.drop_index("ix_chunks_user", table_name="transcript_chunks")
    op.drop_index("ix_chunks_recording", table_name="transcript_chunks")
    op.drop_table("transcript_chunks")
