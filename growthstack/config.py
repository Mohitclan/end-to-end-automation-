"""Runtime configuration. Every secret comes from the environment (or a .env file
that is never committed). Integrations whose settings are empty are treated as
"not configured" and are skipped rather than failing."""

from __future__ import annotations

import os
from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # --- core -------------------------------------------------------------
    environment: str = "development"  # development | production
    secret_key: SecretStr = SecretStr("dev-insecure-change-me")
    base_url: str = "http://localhost:8000"  # public URL used in unsubscribe links
    database_url: str = "sqlite:///./growthstack.db"
    redis_url: str = "redis://localhost:6379/0"
    queue_prefix: str = "growthstack"
    session_cookie_secure: bool = False  # set true behind HTTPS
    forwarded_allow_ips: str = "127.0.0.1"  # reverse proxy address(es) trusted for X-Forwarded-For
    log_level: str = "INFO"
    data_dir: str = "./dev-data"  # writable directory for refreshed data files

    # --- sender identity / compliance ------------------------------------
    company_name: str = "Our Company Ltd"
    company_postal_address: str = ""  # shown in every outreach email footer
    unsubscribe_mailto: str = ""  # optional mailto: target for List-Unsubscribe

    # --- Resend (stage 4) --------------------------------------------------
    resend_api_key: SecretStr | None = None
    resend_webhook_secret: SecretStr | None = None  # whsec_...
    # When true, nothing is sent to Resend; messages are logged as sent. Default
    # on outside production so a dev box can never email a real prospect.
    email_dry_run: bool = True

    # --- LLM (stage 2) -----------------------------------------------------
    llm_provider: str = "fake"  # anthropic | ollama | fake
    llm_model: str = "claude-opus-5-5"
    llm_effort: str = "medium"
    anthropic_api_key: SecretStr | None = None
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"
    langgraph_checkpoint_path: str = "./langgraph-checkpoints.sqlite"

    # Ideal customer profile used by research + scoring
    icp_description: str = "B2B software companies with 10-200 employees"
    icp_title_keywords: str = "founder,ceo,cto,head,director,vp,owner"
    icp_countries: str = ""  # comma separated ISO-ish country names, empty = any
    icp_sic_prefixes: str = ""  # comma separated SIC code prefixes, empty = any
    product_pitch: str = "We help small B2B teams find and win their first customers."
    sender_first_name: str = "Alex"  # sign-off used in AI drafts
    min_score_to_draft: int = Field(default=30, ge=0, le=100)

    # --- research (stage 2) -----------------------------------------------
    crawl4ai_url: str = ""  # e.g. http://crawl4ai:11235
    crawl4ai_token: SecretStr | None = None
    firecrawl_api_key: SecretStr | None = None
    crawler_user_agent: str = "growthstack-research/0.1 (+contact: set CRAWLER_USER_AGENT)"
    research_max_chars: int = 12000

    # --- CRM (stage 6) ----------------------------------------------------
    twenty_api_url: str = ""  # e.g. https://crm.example.com/rest
    twenty_api_key: SecretStr | None = None

    # --- analytics (stage 7) ----------------------------------------------
    posthog_project_api_key: str = ""  # phc_... (public ingestion key)
    posthog_personal_api_key: SecretStr | None = None  # for HogQL queries
    posthog_project_id: str = ""
    posthog_region: str = "us"  # us | eu
    posthog_signup_event: str = "user_signed_up"

    # --- search + page quality (stage 5) ----------------------------------
    gsc_site_url: str = ""  # sc-domain:example.com or https://example.com/
    gsc_service_account_file: str = ""
    psi_api_key: SecretStr | None = None
    psi_urls: str = ""  # comma separated pages to score

    # --- sending domain (stage 3) -----------------------------------------
    sending_domain: str = ""

    # --- lead sources (stage 1) -------------------------------------------
    companies_house_api_key: SecretStr | None = None
    hunter_api_key: SecretStr | None = None

    # --- newsletters (phase 3) --------------------------------------------
    listmonk_url: str = ""
    listmonk_api_user: str = ""
    listmonk_api_token: SecretStr | None = None

    # --- reply detection --------------------------------------------------
    reply_poll_seconds: int = Field(default=300, ge=30)

    # helpers -----------------------------------------------------------------
    @staticmethod
    def _csv(value: str) -> list[str]:
        return [v.strip().lower() for v in value.split(",") if v.strip()]

    @property
    def icp_titles(self) -> list[str]:
        return self._csv(self.icp_title_keywords)

    @property
    def icp_country_list(self) -> list[str]:
        return self._csv(self.icp_countries)

    @property
    def icp_sic_list(self) -> list[str]:
        return self._csv(self.icp_sic_prefixes)

    @property
    def psi_url_list(self) -> list[str]:
        return [v.strip() for v in self.psi_urls.split(",") if v.strip()]

    @property
    def is_production(self) -> bool:
        return self.environment == "production"

    def check_production_safety(self) -> list[str]:
        """Problems that must be fixed before the app or worker may start. The secret
        key is checked in every environment: it signs login sessions and unsubscribe
        links, so a known or empty key means anyone can forge a session."""
        problems = []
        key = self.secret_key.get_secret_value()
        if key in ("", "dev-insecure-change-me") or len(key) < 32:
            problems.append(
                "SECRET_KEY must be a random value of 32+ characters "
                '(python -c "import secrets;print(secrets.token_urlsafe(48))")'
            )
        if self.is_production:
            if not self.base_url.startswith("https://"):
                problems.append("BASE_URL must be https:// in production (unsubscribe links)")
            if not self.session_cookie_secure:
                problems.append("SESSION_COOKIE_SECURE must be true in production")
            if not self.company_postal_address:
                problems.append("COMPANY_POSTAL_ADDRESS is required in outreach footers")
            if self.llm_provider == "fake":
                problems.append("LLM_PROVIDER=fake writes placeholder drafts; use anthropic or ollama")
        return problems


@lru_cache
def get_settings() -> Settings:
    # GROWTHSTACK_ENV_FILE picks the dotenv file; set it empty to ignore .env (tests do).
    env_file = os.environ.get("GROWTHSTACK_ENV_FILE", ".env")
    return Settings(_env_file=env_file or None)
