import json
from pathlib import Path

from vts.services.storage import write_json_atomic


def test_write_json_atomic_roundtrip(tmp_path: Path):
    p = tmp_path / "out" / "data.json"
    write_json_atomic(p, {"a": 1, "b": [2, 3]})
    assert json.loads(p.read_text(encoding="utf-8")) == {"a": 1, "b": [2, 3]}


def test_write_json_atomic_no_temp_left_behind(tmp_path: Path):
    p = tmp_path / "data.json"
    write_json_atomic(p, {"x": 1})
    leftovers = [q.name for q in tmp_path.iterdir() if q.name != "data.json"]
    assert leftovers == []
