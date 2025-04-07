from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    database_url: str = "sqlite+aiosqlite:///./vorarbeiter.db"
    debug: bool = False


settings = Settings()
