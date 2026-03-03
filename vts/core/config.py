from __future__ import annotations

from functools import lru_cache
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Any

import yaml
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="VTS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "vts"
    environment: str = "dev"
    host: str = "0.0.0.0"
    port: int = 8080

    database_url: str = Field(
        default="postgresql+asyncpg://vts:vts@postgres:5432/vts",
        description="Async SQLAlchemy DSN.",
    )
    redis_url: str = "redis://redis:6379/0"
    redis_prefix: str = "vts:"

    config_dir: Path = Path("/opt/vts/config")
    prompts_dir: Path = Path("/opt/vts/prompts")
    artifacts_root: Path = Path("/srv/vts-data")

    trusted_proxy_cidrs: list[str] = [
        "127.0.0.1/32",
        "::1/128",
        "172.16.0.0/12",
        "10.0.0.0/8",
    ]
    admin_emails: list[str] = []

    whisper_url: str = "http://whisper:9000"
    llama_url: str = "http://llama:8000/v1"
    llama_model: str = "Qwen2.5-7B-Instruct-Q4"
    llama_chat_timeout_seconds: int = 600
    llama_final_timeout_seconds: int = 1800
    ytdlp_cookies_file: Path | None = None
    ytdlp_cookies_from_browser: list[str] = Field(default_factory=list)
    ytdlp_youtube_player_client: str | None = None
    ytdlp_youtube_po_token: str | None = None
    ytdlp_verbose: bool = False

    segment_target_seconds: int = 300
    segment_search_window_seconds: int = 30
    segment_overlap_seconds: int = 3
    trim_silence_threshold_db: float = -35.0
    trim_silence_min_duration_sec: float = 0.4
    trim_silence_max_seconds: float = 30.0
    language_detection_confidence_threshold: float = 0.60

    transcribe_parallel_per_task: int = 2
    heavy_slot_limit: int = 1
    event_throttle_hz: int = 4
    db_write_throttle_ms: int = 150
    task_cancel_ttl_seconds: int = 3600

    night_mode_enabled: bool = False
    night_mode_start_hour: int = 22
    night_mode_end_hour: int = 7

    media_ttl_hours: int = 72

    # Token budgeting for the summarization pipeline
    summary_n_ctx: int = 32768
    summary_safety_margin: int = 768
    summary_final_out_budget: int = 1400

    summary_segment_ratio: float = 0.40
    summary_segment_min_ratio: float = 0.30
    summary_segment_max_ratio: float = 0.55
    summary_segment_min_floor: int = 200
    summary_segment_max_cap: int = 1800

    summary_pack_ratio: float = 0.90
    summary_pack_min_ratio: float = 0.80
    summary_pack_max_ratio: float = 0.95
    summary_pack_min_floor: int = 400
    summary_pack_batch_max_input_tokens: int = 12000

    summary_final_ratio: float = 0.70
    summary_final_min_ratio: float = 0.60
    summary_final_max_ratio: float = 0.80

    # Metrics collection
    metrics_enabled: bool = True
    metrics_jsonl_path: Path = Path("/opt/vts/logs/metrics.jsonl")
    metrics_redundancy_shingle_n: int = 3
    metrics_redundancy_simhash_bits: int = 64
    metrics_redundancy_max_hamming: int = 3

    @field_validator("ytdlp_cookies_file", mode="before")
    @classmethod
    def _normalize_optional_cookie_path(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @property
    def config_yaml_path(self) -> Path:
        return self.config_dir / "config.yaml"

    def is_trusted_proxy(self, host: str) -> bool:
        remote = ip_address(host)
        return any(remote in ip_network(cidr) for cidr in self.trusted_proxy_cidrs)

    def is_admin(self, email: str) -> bool:
        normalized = email.strip().lower()
        return normalized in {item.strip().lower() for item in self.admin_emails}


def _load_yaml_overrides() -> dict[str, Any]:
    default_path = Path("/opt/vts/config/config.yaml")
    local_path = Path("config.yaml")
    path = default_path if default_path.exists() else local_path
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}
    if not isinstance(data, dict):
        return {}
    return data


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    overrides = _load_yaml_overrides()
    return Settings(**overrides)
