import uuid
import pytest
from vts.db.models import UserStepWeights, User


def test_model_columns_exist():
    cols = set(UserStepWeights.__table__.columns.keys())
    assert cols == {"id", "user_id", "weights", "final_summary_fallback", "computed_at", "sample_counts"}


def test_user_id_unique_constraint():
    uniques = [c for c in UserStepWeights.__table__.constraints
               if c.__class__.__name__ == "UniqueConstraint"]
    cols = {tuple(c.columns.keys()) for c in uniques}
    assert ("user_id",) in cols
