from vts.core.config import Settings


def test_diarization_noise_max_distance_default():
    s = Settings()
    assert s.diarization_noise_max_distance == 0.25


def test_diarization_noise_max_distance_env(monkeypatch):
    monkeypatch.setenv("VTS_DIARIZATION_NOISE_MAX_DISTANCE", "0.3")
    s = Settings()
    assert s.diarization_noise_max_distance == 0.3


def test_tasks_page_size_defaults_to_10() -> None:
    from vts.core.config import Settings
    assert Settings().tasks_page_size == 10


def test_tasks_page_size_env_overrides_default(monkeypatch) -> None:
    from vts.core.config import Settings
    monkeypatch.setenv("VTS_TASKS_PAGE_SIZE", "7")
    assert Settings().tasks_page_size == 7


def test_tasks_page_size_yaml_init_kwarg_overrides_env(monkeypatch) -> None:
    from vts.core.config import Settings
    # get_settings() loads YAML overrides and passes them as init kwargs to
    # Settings(**overrides) — see _load_yaml_overrides/get_settings. Standard
    # pydantic-settings source precedence is init > env > dotenv > default (no
    # settings_customise_sources reordering is applied here, only an
    # env-source class swap), so an explicit init kwarg wins over env.
    monkeypatch.setenv("VTS_TASKS_PAGE_SIZE", "7")
    assert Settings(tasks_page_size=25).tasks_page_size == 25
