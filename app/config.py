from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    admin_token: str = "raeVenga1eez3Geeca"
    base_url: str = "http://localhost:8000"
    database_url: str = "postgresql+psycopg://postgres:postgres@db:5432/test_db"
    database_replica_url: str | None = None
    debug: bool = False
    flathubbot_token: str = "test_flathubbot_token"
    github_actions_token: str = "test_github_actions_token"
    github_app_id: int | None = None
    github_app_installation_id: int | None = None
    github_app_private_key_file: str | None = None
    github_webhook_hook_id: int | None = None
    github_webhook_secret: str = "test_webhook_secret"
    flat_manager_token: str = "test_repo_token"
    flat_manager_url: str = "https://hub.flathub.org"
    permission_assessment_url: str | None = None
    permission_assessment_secret: str | None = None
    permission_status_repos: list[str] = []
    statuspage_url: str = "https://status.flathub.org"
    sentry_dsn: str | None = None
    ff_build_diagnosis: bool = False
    ff_build_diagnosis_comments: bool = False
    ff_auto_retry_cancelled_test_builds: bool = False
    llm_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    decisions_url: str = ""
    decisions_api_key: str = ""
    decisions_model: str = ""
    source_download_max_bytes: int = 100 * 1024 * 1024
    source_extracted_max_bytes: int = 512 * 1024 * 1024
    source_max_files: int = 20000
    source_timeout_seconds: int = 180
    source_cache_entries: int = 8
    source_read_max_bytes: int = 2 * 1024 * 1024
    source_search_max_bytes: int = 20 * 1024 * 1024
    source_search_max_files: int = 500
    ff_reprocheck_issues: bool = False
    ff_disable_admin_ping_comment: bool = False
    ff_disable_test_builds: bool = False
    max_concurrent_builds: int = 15
    test_build_failure_streak_limit: int = 3
    test_build_penalty_minutes: int = 60

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )


settings = Settings()
