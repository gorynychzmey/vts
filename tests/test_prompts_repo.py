from vts.db.models import Prompt


def test_prompt_model_columns():
    cols = set(Prompt.__table__.columns.keys())
    assert {"id", "user_id", "name", "system_prompt",
            "created_at", "updated_at"} <= cols
