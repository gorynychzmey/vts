from vts.core.config import Settings


def test_diarization_noise_max_distance_default():
    s = Settings()
    assert s.diarization_noise_max_distance == 0.25


def test_diarization_noise_max_distance_env(monkeypatch):
    monkeypatch.setenv("VTS_DIARIZATION_NOISE_MAX_DISTANCE", "0.3")
    s = Settings()
    assert s.diarization_noise_max_distance == 0.3
