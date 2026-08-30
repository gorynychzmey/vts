"""a recording owns its name (vts-lib2)

Two problems, one column.

**Recordings had no name of their own.** 55 of 122 production recordings showed
as untitled — not because anything was lost, but because their TASK was
untitled too: an upload is only titled if the user types something. The name
was in `source_url` all along (`file://19.05.2026 22.10.m4a`), and the task
list already fell back to it while the library printed "untitled". Deriving it
in the browser would make the name a property of one rendering rather than of
the recording, so it is derived here and backfilled.

**A recording must be renameable independently.** It outlives its task and is
the object the library is about. Without `title_is_custom`, the next task
rename — or simply the next pipeline run, which upserts the recording — would
overwrite a deliberate name with a derived one, silently. Clearing the name
resets the flag so the recording follows its task again.

Revision ID: 0029_recording_title_custom
Revises: 0028_transcript_chunks
"""
import sqlalchemy as sa
from alembic import op

revision = "0029_recording_title_custom"
down_revision = "0028_transcript_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recordings",
        sa.Column("title_is_custom", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
    )
    # Backfill the names that were never derived. An upload's filename comes
    # out of the file:// pseudo-URL; anything else falls back to the URL, which
    # is a poor name but a true one — better than none.
    op.execute(
        sa.text(
            """
            UPDATE recordings
               SET title = CASE
                     WHEN source_url LIKE 'file://%'
                       THEN substring(source_url from 8)
                     ELSE source_url
                   END
             WHERE (title IS NULL OR btrim(title) = '')
               AND source_url IS NOT NULL
               AND btrim(source_url) <> ''
            """
        )
    )


def downgrade() -> None:
    # The backfilled titles are left in place: they are correct names, and
    # restoring NULLs would only reinstate the display bug.
    op.drop_column("recordings", "title_is_custom")
