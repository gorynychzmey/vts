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

    environment: str = "dev"
    host: str = "0.0.0.0"
    port: int = 8080

    database_url: str = Field(
        default="postgresql+asyncpg://vts:vts@postgres:5432/vts",
        description="Async SQLAlchemy DSN.",
    )
    redis_url: str = "redis://redis:6379/0"
    redis_prefix: str = "vts:"

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
    whisper_backend: str = "asr"
    llm_url: str = "http://llama:8000/v1"
    llm_api_key: str | None = None
    llm_model: str = "Qwen2.5-7B-Instruct-Q4"
    llm_tokenizer_path: Path | None = None
    llm_temperature: float = 0.2
    llm_top_p: float | None = None
    llm_min_p: float | None = None
    llm_repeat_penalty: float | None = None
    llm_thinking: bool | None = None
    llm_chat_timeout_seconds: int = 600
    llm_final_timeout_seconds: int = 1800
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
    services_database_write_throttle_ms: int = 150
    task_cancel_ttl_seconds: int = 3600

    night_mode_enabled: bool = False
    night_mode_start_hour: int = 22
    night_mode_end_hour: int = 7

    timezone: str | None = None

    media_ttl_hours: int = 72

    # Token budgeting for the summarization pipeline
    summary_safety_margin: int = 768

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

    # Web Push (VAPID) — if any of these are empty, push is disabled at runtime.
    vapid_public_key: str | None = None
    vapid_private_key: str | None = None
    vapid_subject: str = "mailto:admin@example.com"

    # Feature flags
    features_donor_clone: bool = False

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
    return _normalize_yaml_overrides(data)


def _flatten_nested_overrides(
    source: dict[str, Any],
    *,
    prefix: str,
    destination: dict[str, Any],
) -> None:
    for key, value in source.items():
        full_key = f"{prefix}_{key}"
        if isinstance(value, dict):
            _flatten_nested_overrides(
                value,
                prefix=full_key,
                destination=destination,
            )
            continue
        destination.setdefault(full_key, value)


def _normalize_yaml_overrides(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize structured YAML blocks into flat Settings keys.

    Supports nested sections such as:
      services.database.url -> database_url
      ytdlp.youtube.player_client -> ytdlp_youtube_player_client
      metrics.redundancy.shingle_n -> metrics_redundancy_shingle_n
      summary.segment.ratio -> summary_segment_ratio
    """
    normalized: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, dict):
            _flatten_nested_overrides(
                value,
                prefix=key,
                destination=normalized,
            )
    # In the new YAML schema root config is structured; legacy flat keys
    # (for example, database_url or summary_segment_ratio) are ignored.
    # Structured aliases for the consolidated directories section.
    if "dirs_artifacts" in normalized:
        normalized["artifacts_root"] = normalized["dirs_artifacts"]
    if "dirs_prompts" in normalized:
        normalized["prompts_dir"] = normalized["dirs_prompts"]
    # Structured aliases for the consolidated services section.
    services_aliases = {
        "services_database_url": "database_url",
        "services_redis_url": "redis_url",
        "services_redis_prefix": "redis_prefix",
        "services_whisper_url": "whisper_url",
        "services_whisper_backend": "whisper_backend",
        "services_llm_url": "llm_url",
        "services_llm_api_key": "llm_api_key",
        "services_llm_model": "llm_model",
        "services_llm_temperature": "llm_temperature",
        "services_llm_top_p": "llm_top_p",
        "services_llm_min_p": "llm_min_p",
        "services_llm_repeat_penalty": "llm_repeat_penalty",
        "services_llm_chat_timeout_seconds": "llm_chat_timeout_seconds",
        "services_llm_final_timeout_seconds": "llm_final_timeout_seconds",
        "services_llm_tokenizer_path": "llm_tokenizer_path",
        "services_llm_thinking": "llm_thinking",
        # Legacy aliases kept for backwards compatibility
        "services_llama_url": "llm_url",
        "services_llama_model": "llm_model",
        "services_llama_temperature": "llm_temperature",
        "services_llama_top_p": "llm_top_p",
        "services_llama_min_p": "llm_min_p",
        "services_llama_repeat_penalty": "llm_repeat_penalty",
        "services_llama_chat_timeout_seconds": "llm_chat_timeout_seconds",
        "services_llama_final_timeout_seconds": "llm_final_timeout_seconds",
        "services_llama_tokenizer_path": "llm_tokenizer_path",
    }
    for source_key, target_key in services_aliases.items():
        if source_key in normalized:
            normalized[target_key] = normalized[source_key]
    # Structured aliases for runtime environment settings.
    if "environment_productive" in normalized:
        productive = bool(normalized["environment_productive"])
        normalized["environment"] = "prod" if productive else "dev"
    if "environment_host" in normalized:
        normalized["host"] = normalized["environment_host"]
    if "environment_port" in normalized:
        normalized["port"] = normalized["environment_port"]
    if "environment_timezone" in normalized:
        normalized["timezone"] = normalized["environment_timezone"]
    return normalized


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    overrides = _load_yaml_overrides()
    return Settings(**overrides)
