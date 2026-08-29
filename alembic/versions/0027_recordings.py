"""a Recording that outlives the task that produced it (vts-8w1r / VOS-130)

Until now the task WAS the recording: deleting a task deleted the transcript,
the media and the segments with it. A knowledge library needs the recording to
be the lasting object, with a task as one way of creating or updating it.

Additive: this creates the table and backfills one recording per existing task,
so nothing that works today stops working. The behaviour change that follows
from it — task deletion no longer removing a directory a live recording owns —
lives in the application, not here.

`duration_sec` and `language` are filled from what the database already knows
rather than from the media files: duration as the last segment's end (the ASR
covers the whole recording), language from the task's options. Both used to be
derivable only from files that archiving deletes, which is exactly why they
become columns.

Revision ID: 0027_recordings
Revises: 0026_clear_raw_json
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0027_recordings"
down_revision = "0026_clear_raw_json"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recordings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_task_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("artifact_dir", sa.Text(), nullable=False),
        sa.Column("transcript_path", sa.Text(), nullable=True),
        sa.Column("summary_path", sa.Text(), nullable=True),
        sa.Column("duration_sec", sa.Float(), nullable=True),
        sa.Column("language", sa.String(32), nullable=True),
        sa.Column("tags", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("meta", sa.JSON(), nullable=False, server_default=sa.text("'{}'::json")),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        # SET NULL, not CASCADE: the recording is what lasts.
        sa.ForeignKeyConstraint(["source_task_id"], ["tasks.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_recordings_user_created", "recordings", ["user_id", "created_at"])
    op.create_index(
        "uq_recordings_source_task", "recordings", ["source_task_id"],
        unique=True, postgresql_where=sa.text("source_task_id IS NOT NULL"),
    )

    # One recording per existing task. Every task gets one, including failed
    # ones: a recording with no transcript is a truthful record of an attempt,
    # and inventing a rule about which tasks "count" here would be a product
    # decision made in a migration.
    op.execute(
        sa.text(
            """
            INSERT INTO recordings (
                id, user_id, source_task_id, title, source_url, artifact_dir,
                transcript_path, summary_path, duration_sec, language,
                tags, meta, recorded_at, created_at, updated_at
            )
            SELECT
                gen_random_uuid(),
                t.user_id,
                t.id,
                t.source_title,
                t.source_url,
                t.artifact_dir,
                t.transcript_path,
                t.summary_path,
                -- The ASR covers the whole recording, so the last segment's end
                -- is its length — and unlike probing the media file, this
                -- survives archiving.
                (SELECT max(s.end_sec) FROM asr_segments s WHERE s.task_id = t.id),
                -- Explicit choice first, detection second: the same order
                -- effective_language() uses. Mapped to a code because the
                -- pipeline stores whichever spelling its backend produced —
                -- production carries both "russian" (cpp) and "ru" (the ASR
                -- sidecar) for the same language, and a library that lists them
                -- separately is wrong on its face. Unknown values pass through:
                -- showing what is stored beats guessing.
                CASE lower(coalesce(
                        nullif(t.options->>'language', ''),
                        nullif(t.options->>'detected_language', '')))
                    WHEN 'russian'    THEN 'ru'
                    WHEN 'english'    THEN 'en'
                    WHEN 'german'     THEN 'de'
                    WHEN 'ukrainian'  THEN 'uk'
                    WHEN 'french'     THEN 'fr'
                    WHEN 'spanish'    THEN 'es'
                    WHEN 'italian'    THEN 'it'
                    WHEN 'portuguese' THEN 'pt'
                    WHEN 'polish'     THEN 'pl'
                    WHEN 'dutch'      THEN 'nl'
                    WHEN 'turkish'    THEN 'tr'
                    WHEN 'kazakh'     THEN 'kk'
                    WHEN 'belarusian' THEN 'be'
                    WHEN 'czech'      THEN 'cs'
                    WHEN 'chinese'    THEN 'zh'
                    WHEN 'japanese'   THEN 'ja'
                    WHEN 'korean'     THEN 'ko'
                    WHEN 'arabic'     THEN 'ar'
                    WHEN 'hebrew'     THEN 'he'
                    WHEN 'hindi'      THEN 'hi'
                    ELSE lower(coalesce(
                        nullif(t.options->>'language', ''),
                        nullif(t.options->>'detected_language', '')))
                END,
                '[]'::json,
                '{}'::json,
                t.created_at,
                t.created_at,
                t.updated_at
            FROM tasks t
            WHERE NOT EXISTS (
                SELECT 1 FROM recordings r WHERE r.source_task_id = t.id
            )
            """
        )
    )


def downgrade() -> None:
    op.drop_index("uq_recordings_source_task", table_name="recordings")
    op.drop_index("ix_recordings_user_created", table_name="recordings")
    op.drop_table("recordings")
