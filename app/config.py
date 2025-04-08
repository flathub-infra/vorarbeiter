from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./vorarbeiter.db"
    debug: bool = False
    sentry_dsn: str | None = None
    github_webhook_secret: str | None = None
    github_token: str | None = None


settings = Settings()
