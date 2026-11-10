"""App settings: Supervisor's options.json plus fixed paths.

Paths can be overridden with environment variables so the tests can run the
whole service against temporary folders and a throwaway Paperless.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_PROCESSOR_VERSION = (
    "projects/paperlss-document-pipeline/locations/us/processors/484e7be4b20b5629/"
    "processorVersions/pretrained-ocr-v2.1-2024-08-07"
)
APP_ROOT = Path(__file__).resolve().parent.parent
PIN_FILE = APP_ROOT / "tested-paperless-version"


class ConfigError(RuntimeError):
    pass


def tested_paperless_version(pin_file: Path = PIN_FILE) -> str:
    """The Paperless version the end-to-end tests passed on (the pin)."""
    for line in pin_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line
    raise ConfigError(f"No version found in {pin_file}")


@dataclass(frozen=True)
class Settings:
    paperless_url: str
    paperless_token: str
    google_service_account: dict | None
    processor_version: str
    physical_mail_tag: str
    intake_tag: str
    ha_url: str
    webhook_started: str
    webhook_finished: str
    webhook_alert: str
    daily_page_cap: int
    min_free_mib: int
    keep_local_copies_days: int
    watchdog_minutes: int
    capture_dir: Path
    intake_dir: Path
    font_path: Path
    tested_paperless_version: str
    web_port: int = 8099
    # Only Supervisor's ingress proxy may reach the page in production.
    web_allowed_clients: tuple[str, ...] = ("172.30.32.2",)
    stable_seconds: float = 30.0
    poll_seconds: float = 5.0
    retry_minutes: int = 30
    retry_hours: int = 24
    extra: dict = field(default_factory=dict)

    @property
    def items_dir(self) -> Path:
        return self.intake_dir / "items"

    @property
    def duplicates_dir(self) -> Path:
        return self.intake_dir / "duplicates"

    @property
    def discarded_dir(self) -> Path:
        return self.intake_dir / "discarded"


def _service_account(raw: str | None) -> dict | None:
    if not raw or raw.strip() in {"", "-"}:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise ConfigError("google_service_account is not valid JSON") from None
    if not isinstance(value, dict) or value.get("type") != "service_account":
        raise ConfigError("google_service_account must be a service-account key (type service_account)")
    for key in ("client_email", "private_key", "token_uri"):
        if not value.get(key):
            raise ConfigError(f"google_service_account is missing {key}")
    return value


def load(options_path: Path | None = None) -> Settings:
    options_path = Path(os.environ.get("INTAKE_OPTIONS", options_path or "/data/options.json"))
    try:
        options = json.loads(options_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigError(f"Cannot read {options_path}: {error}") from None
    token = (options.get("paperless_token") or "").strip()
    if not token or token == "-":
        raise ConfigError("paperless_token is not set")
    allowed = os.environ.get("INTAKE_WEB_ALLOWED_CLIENTS")
    return Settings(
        paperless_url=options.get("paperless_url", "http://ca5234a0-paperless-ngx:80").rstrip("/"),
        paperless_token=token,
        google_service_account=_service_account(options.get("google_service_account")),
        processor_version=options.get("processor_version") or DEFAULT_PROCESSOR_VERSION,
        physical_mail_tag=options.get("physical_mail_tag", "Physical Mail"),
        intake_tag=options.get("intake_tag", "Mail intake"),
        ha_url=options.get("ha_url", "http://homeassistant:8123").rstrip("/"),
        webhook_started=options.get("webhook_started", ""),
        webhook_finished=options.get("webhook_finished", ""),
        webhook_alert=options.get("webhook_alert", ""),
        daily_page_cap=int(options.get("daily_page_cap", 200)),
        min_free_mib=int(options.get("min_free_mib", 1024)),
        keep_local_copies_days=int(options.get("keep_local_copies_days", 7)),
        watchdog_minutes=int(options.get("watchdog_minutes", 45)),
        capture_dir=Path(os.environ.get("INTAKE_CAPTURE_DIR", "/share/paperless/capture")),
        intake_dir=Path(os.environ.get("INTAKE_DIR", "/share/paperless/intake")),
        font_path=Path(os.environ.get("INTAKE_FONT", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")),
        tested_paperless_version=tested_paperless_version(
            Path(os.environ.get("INTAKE_PIN_FILE", PIN_FILE)),
        ),
        web_port=int(os.environ.get("INTAKE_WEB_PORT", 8099)),
        web_allowed_clients=tuple(allowed.split(",")) if allowed else ("172.30.32.2",),
        stable_seconds=float(os.environ.get("INTAKE_STABLE_SECONDS", 30)),
        poll_seconds=float(os.environ.get("INTAKE_POLL_SECONDS", 5)),
    )
