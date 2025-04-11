from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    admin_token: str = "raeVenga1eez3Geeca"
    base_url: str = "http://localhost:8000"
    database_url: str = "sqlite+aiosqlite:///./vorarbeiter.db"
    debug: bool = False
    github_token: str = "test_github_token"
    github_webhook_secret: str = "test_webhook_secret"
    repo_token: str = "test_repo_token"
    sentry_dsn: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )


settings = Settings()
