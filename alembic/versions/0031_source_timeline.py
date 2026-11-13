"""move pre-fix transcripts onto the source media's timeline (vts-kb4z)

trim_initial_silence cut leading silence off the audio that ASR and diarization
read, and nothing added it back: every timecode counted from where the speech
started, while the player and search hits seek in the untrimmed media. A
recording that opened with 9.3 s of silence played each sentence 9.3 s before
it was spoken.

The pipeline now writes source-timeline times. This migration moves what was
written before: asr_segments and transcript_chunks rows, and the artifacts
built from them (transcript.json, asr/segments_raw.json, diarization.json). See
vts/services/source_timeline.py for why each half is safe to repeat.

The artifacts live on disk, so this needs the data volume mounted where
migrations run, as the pod's migrate container has it. Where it is not, the
recordings are skipped as a whole and stay as they were.

Revision ID: 0031_source_timeline
Revises: 0030_user_sessions
"""
from alembic import op

revision = "0031_source_timeline"
down_revision = "0030_user_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    from vts.services.source_timeline import migrate_old_timelines

    moved = migrate_old_timelines(op.get_bind())
    print(f"source timeline: moved {moved} recordings onto the media's timeline")


def downgrade() -> None:
    # Refused rather than a silent no-op. The rows carry no marker of their
    # own, so a downgrade that left them moved and a later upgrade would move
    # them a second time, while the files (which do carry one) stayed put.
    # Reversing is no better: recordings processed after the upgrade are
    # already on the source timeline and indistinguishable from migrated ones.
    raise RuntimeError(
        "0031_source_timeline cannot be downgraded: timecodes were moved onto "
        "the source media's timeline, and re-running the upgrade after a "
        "downgrade would shift them twice. Restore a database backup instead."
    )
