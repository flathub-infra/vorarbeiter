from logging.config import fileConfig

from sqlalchemy import engine_from_config
from sqlalchemy import pool

from alembic import context

import sys
import os

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))

# Import Base and models
from app.models.webhook_event import Base

# Import pydantic settings
from app.config import settings

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Use the metadata from your Base
target_metadata = Base.metadata

# other values from the config, defined by the needs of env.py,
# can be acquired:
# my_important_option = config.get_main_option("my_important_option")
# ... etc.


# Function to get synchronous URL for Alembic
def get_sync_database_url() -> str:
    # Replace async driver prefix with sync equivalent if necessary
    db_url = settings.database_url
    if db_url.startswith("sqlite+aiosqlite"):
        return db_url.replace("sqlite+aiosqlite", "sqlite")
    if db_url.startswith("postgresql+asyncpg"):
        return db_url.replace("postgresql+asyncpg", "postgresql")
    # Add other driver replacements if needed
    return db_url


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    # Use the URL from settings, adjusted for sync driver
    url = get_sync_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # compare_type=True might need to be added here too for offline mode
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    # Use a configuration dictionary built from settings
    # engine_from_config expects a dictionary-like object
    # We provide the sync url directly
    connectable = context.config.attributes.get("connection", None)

    if connectable is None:
        # create engine only if needed
        connectable = engine_from_config(
            {"sqlalchemy.url": get_sync_database_url()},
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
