import uuid
from vts.pipeline.steps.base import Step, StepState


def test_step_state_holds_fields():
    st = StepState(task_id=uuid.uuid4(), user_id="u", dirs={}, logger=None, task_options={})
    assert st.user_id == "u"


def test_step_defaults():
    class _S(Step):
        name = "x"
        async def run(self, ctx, st): return None
    assert _S().lane is None


import asyncio
def test_already_done_defaults_false():
    class _S(Step):
        name = "x"
        async def run(self, ctx, st): return None
    assert asyncio.run(_S().already_done(None, None)) is False
