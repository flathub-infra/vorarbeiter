from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    admin_token: str = "raeVenga1eez3Geeca"
    base_url: str = "http://localhost:8000"
    database_url: str = "postgresql+psycopg://postgres:postgres@db:5432/test_db"
    debug: bool = False
    github_token: str = "test_github_token"
    github_status_token: str = "test_github_status_token"
    github_webhook_secret: str = "test_webhook_secret"
    gnome_gitlab_token: str = "test_gnome_gitlab_token"
    flat_manager_token: str = "test_repo_token"
    flat_manager_url: str = "https://hub.flathub.org"
    sentry_dsn: str | None = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )


settings = Settings()
