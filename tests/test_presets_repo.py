from vts.db.models import Preset, User


def test_preset_model_columns():
    cols = set(Preset.__table__.columns.keys())
    assert {"id", "user_id", "name", "options", "created_at", "updated_at"} <= cols


def test_user_has_default_preset_column():
    assert "default_preset" in set(User.__table__.columns.keys())
