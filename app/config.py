"""Settings read from environment variables (see .env.example)."""

import os
from dataclasses import dataclass
from pathlib import Path


def _flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    anthropic_api_key: str
    app_password: str
    business_name: str
    advisor_name: str
    advisor_tagline: str
    advisor_email: str
    advisor_phone: str
    business_website: str
    default_tracking_fee: float
    quote_valid_days: int
    data_dir: Path
    chromium_path: str
    demo_mode: bool
    model: str
    cf_account_id: str
    cf_api_token: str
    rccl_graphql_url: str


def load_settings() -> Settings:
    return Settings(
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        app_password=os.getenv("APP_PASSWORD", ""),
        business_name=os.getenv("BUSINESS_NAME", "At Your Pace Travel"),
        advisor_name=os.getenv("ADVISOR_NAME", ""),
        advisor_tagline=os.getenv("ADVISOR_TAGLINE", ""),
        advisor_email=os.getenv("ADVISOR_EMAIL", ""),
        advisor_phone=os.getenv("ADVISOR_PHONE", ""),
        business_website=os.getenv("BUSINESS_WEBSITE", ""),
        default_tracking_fee=float(os.getenv("DEFAULT_TRACKING_FEE", "50")),
        quote_valid_days=int(os.getenv("QUOTE_VALID_DAYS", "7")),
        data_dir=Path(os.getenv("DATA_DIR", "./data")),
        chromium_path=os.getenv("CHROMIUM_PATH", ""),
        demo_mode=_flag("DEMO_MODE"),
        model=os.getenv("CLAUDE_MODEL", "claude-opus-5"),
        cf_account_id=os.getenv("CLOUDFLARE_ACCOUNT_ID", ""),
        cf_api_token=os.getenv("CLOUDFLARE_API_TOKEN", ""),
        rccl_graphql_url=os.getenv("RCCL_GRAPHQL_URL", "https://aws-prd.api.rccl.com/en/royal/web/graphql"),
    )


settings = load_settings()
