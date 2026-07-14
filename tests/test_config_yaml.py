from vts.core.config import Settings, _normalize_yaml_overrides


def test_normalize_yaml_overrides_flattens_summary_tree() -> None:
    raw = {
        "summary": {
            "segment": {
                "ratio": 0.42,
                "min_ratio": 0.31,
                "max_ratio": 0.56,
                "min_floor": 210,
                "max_cap": 1700,
            },
            "pack": {
                "ratio": 0.91,
                "min_ratio": 0.81,
                "max_ratio": 0.96,
                "min_floor": 410,
                "batch_max_input_tokens": 11000,
            },
            "final": {
                "ratio": 0.71,
                "min_ratio": 0.61,
                "max_ratio": 0.81,
            },
        }
    }

    normalized = _normalize_yaml_overrides(raw)

    assert "summary" not in normalized
    assert normalized["summary_segment_ratio"] == 0.42
    assert normalized["summary_segment_min_ratio"] == 0.31
    assert normalized["summary_segment_max_ratio"] == 0.56
    assert normalized["summary_segment_min_floor"] == 210
    assert normalized["summary_segment_max_cap"] == 1700
    assert normalized["summary_pack_ratio"] == 0.91
    assert normalized["summary_pack_min_ratio"] == 0.81
    assert normalized["summary_pack_max_ratio"] == 0.96
    assert normalized["summary_pack_min_floor"] == 410
    assert normalized["summary_pack_batch_max_input_tokens"] == 11000
    assert normalized["summary_final_ratio"] == 0.71
    assert normalized["summary_final_min_ratio"] == 0.61
    assert normalized["summary_final_max_ratio"] == 0.81


def test_normalize_yaml_overrides_ignores_legacy_flat_keys() -> None:
    raw = {
        "summary_segment_ratio": 0.5,
        "ytdlp_verbose": True,
        "environment": {
            "productive": True,
            "host": "127.0.0.1",
            "port": 9999,
        },
        "summary": {
            "segment": {
                "ratio": 0.4,
                "min_ratio": 0.3,
            }
        },
        "ytdlp": {"verbose": False},
    }

    normalized = _normalize_yaml_overrides(raw)

    assert normalized["summary_segment_ratio"] == 0.4
    assert normalized["ytdlp_verbose"] is False
    assert normalized["summary_segment_min_ratio"] == 0.3
    assert normalized["environment"] == "prod"
    assert normalized["host"] == "127.0.0.1"
    assert normalized["port"] == 9999


def test_settings_accepts_structured_sections_from_yaml() -> None:
    raw = {
        "environment": {
            "productive": False,
            "host": "127.0.0.1",
            "port": 18080,
        },
        "dirs": {
            "artifacts": "/tmp/vts-artifacts",
            "prompts": "/tmp/vts-prompts",
            "config": "/tmp/vts-config",
        },
        "services": {
            "database": {
                "url": "postgresql+asyncpg://u:p@db:5432/vts",
                "write_throttle": {"ms": 200},
            },
            "redis": {"url": "redis://cache:6379/1", "prefix": "custom:"},
            "whisper": {"url": "http://whisper-internal:9000"},
            "llm": {
                "url": "http://llama-internal:8000/v1",
                "model": "Qwen2.5-14B-Instruct-Q4",
            },
        },
        "segment": {
            "target_seconds": 240,
            "search_window_seconds": 20,
            "overlap_seconds": 2,
        },
        "trim_silence": {
            "threshold_db": -32.0,
            "min_duration_sec": 0.5,
            "max_seconds": 25.0,
        },
        "language_detection": {"confidence_threshold": 0.7},
        "transcribe": {"parallel_per_task": 3},
        "event_throttle": {"hz": 5},
        "task_cancel_ttl": {"seconds": 7200},
        "night_mode": {"enabled": True, "start_hour": 23, "end_hour": 6},
        "media_ttl": {"hours": 96},
        "ytdlp": {
            "cookies_file": "/tmp/cookies.txt",
            "cookies_from_browser": ["firefox"],
            "youtube": {
                "player_client": "android",
                "po_token": "token-value",
            },
            "verbose": True,
        },
        "metrics": {
            "enabled": False,
            "jsonl_path": "/tmp/metrics.jsonl",
            "redundancy": {
                "shingle_n": 4,
                "simhash_bits": 128,
                "max_hamming": 5,
            },
        },
        "summary": {
            "segment": {"ratio": 0.44},
            "pack": {"batch_max_input_tokens": 10000},
        }
    }

    settings = Settings(**_normalize_yaml_overrides(raw))

    assert settings.environment == "dev"
    assert settings.host == "127.0.0.1"
    assert settings.port == 18080
    assert settings.database_url == "postgresql+asyncpg://u:p@db:5432/vts"
    assert str(settings.artifacts_root) == "/tmp/vts-artifacts"
    assert str(settings.prompts_dir) == "/tmp/vts-prompts"
    assert settings.redis_url == "redis://cache:6379/1"
    assert settings.redis_prefix == "custom:"
    assert settings.whisper_url == "http://whisper-internal:9000"
    assert settings.llm_url == "http://llama-internal:8000/v1"
    assert settings.llm_model == "Qwen2.5-14B-Instruct-Q4"
    assert settings.segment_target_seconds == 240
    assert settings.segment_search_window_seconds == 20
    assert settings.segment_overlap_seconds == 2
    assert settings.trim_silence_threshold_db == -32.0
    assert settings.trim_silence_min_duration_sec == 0.5
    assert settings.trim_silence_max_seconds == 25.0
    assert settings.language_detection_confidence_threshold == 0.7
    assert settings.transcribe_parallel_per_task == 3
    assert settings.event_throttle_hz == 5
    assert settings.services_database_write_throttle_ms == 200
    assert settings.task_cancel_ttl_seconds == 7200
    assert settings.night_mode_enabled is True
    assert settings.night_mode_start_hour == 23
    assert settings.night_mode_end_hour == 6
    assert settings.media_ttl_hours == 96
    assert str(settings.ytdlp_cookies_file) == "/tmp/cookies.txt"
    assert settings.ytdlp_cookies_from_browser == ["firefox"]
    assert settings.ytdlp_youtube_player_client == "android"
    assert settings.ytdlp_youtube_po_token == "token-value"
    assert settings.ytdlp_verbose is True
    assert settings.metrics_enabled is False
    assert str(settings.metrics_jsonl_path) == "/tmp/metrics.jsonl"
    assert settings.metrics_redundancy_shingle_n == 4
    assert settings.metrics_redundancy_simhash_bits == 128
    assert settings.metrics_redundancy_max_hamming == 5
    assert settings.summary_segment_ratio == 0.44
    assert settings.summary_pack_batch_max_input_tokens == 10000


def test_settings_ignores_legacy_flat_yaml_keys() -> None:
    raw = {
        "database_url": "postgresql+asyncpg://legacy:legacy@db:5432/legacy",
        "summary_segment_ratio": 0.12,
    }

    settings = Settings(**_normalize_yaml_overrides(raw))

    assert settings.database_url == "postgresql+asyncpg://vts:vts@postgres:5432/vts"
    assert settings.summary_segment_ratio == 0.78  # default, legacy flat key ignored


def test_lane_settings_defaults():
    from vts.core.config import Settings
    s = Settings()
    assert s.worker_max_active_tasks == 4
    assert s.lane_network_slots == 1
    assert s.lane_ffmpeg_slots == 2
    assert s.lane_gpu_slots == 1
    assert s.gpu_asr_burst == 3


def test_lane_settings_yaml_override() -> None:
    raw = {
        "environment": {
            "productive": False,
            "host": "127.0.0.1",
            "port": 18080,
        },
        "dirs": {
            "artifacts": "/tmp/vts-artifacts",
            "prompts": "/tmp/vts-prompts",
            "config": "/tmp/vts-config",
        },
        "services": {
            "database": {
                "url": "postgresql+asyncpg://u:p@db:5432/vts",
                "write_throttle": {"ms": 200},
            },
            "redis": {"url": "redis://cache:6379/1", "prefix": "custom:"},
            "whisper": {"url": "http://whisper-internal:9000"},
            "llm": {
                "url": "http://llama-internal:8000/v1",
                "model": "Qwen2.5-14B-Instruct-Q4",
            },
        },
        "segment": {
            "target_seconds": 240,
            "search_window_seconds": 20,
            "overlap_seconds": 2,
        },
        "trim_silence": {
            "threshold_db": -32.0,
            "min_duration_sec": 0.5,
            "max_seconds": 25.0,
        },
        "language_detection": {"confidence_threshold": 0.7},
        "transcribe": {"parallel_per_task": 3},
        "worker": {"max_active_tasks": 2},
        "lane": {"gpu_slots": 2},
        "gpu": {"asr_burst": 5},
        "event_throttle": {"hz": 5},
        "task_cancel_ttl": {"seconds": 7200},
        "night_mode": {"enabled": True, "start_hour": 23, "end_hour": 6},
        "media_ttl": {"hours": 96},
        "ytdlp": {
            "cookies_file": "/tmp/cookies.txt",
            "cookies_from_browser": ["firefox"],
            "youtube": {
                "player_client": "android",
                "po_token": "token-value",
            },
            "verbose": True,
        },
        "metrics": {
            "enabled": False,
            "jsonl_path": "/tmp/metrics.jsonl",
            "redundancy": {
                "shingle_n": 4,
                "simhash_bits": 128,
                "max_hamming": 5,
            },
        },
        "summary": {
            "segment": {"ratio": 0.44},
            "pack": {"batch_max_input_tokens": 10000},
        }
    }

    settings = Settings(**_normalize_yaml_overrides(raw))

    assert settings.worker_max_active_tasks == 2
    assert settings.lane_gpu_slots == 2
    assert settings.gpu_asr_burst == 5
