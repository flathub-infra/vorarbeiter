import uuid
from datetime import datetime
from enum import Enum
from typing import Any

import sqlalchemy
from sqlalchemy import JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.webhook_event import Base


class GitHubTaskStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class GitHubTask(Base):
    __tablename__ = "github_task"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, index=True
    )
    task_type: Mapped[str] = mapped_column(String(50), index=True)
    status: Mapped[GitHubTaskStatus] = mapped_column(
        index=True,
        default=GitHubTaskStatus.PENDING,
        type_=sqlalchemy.Enum(
            GitHubTaskStatus, values_callable=lambda x: [e.value for e in x]
        ),
    )

    method: Mapped[str] = mapped_column(String(10))
    url: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    attempt_count: Mapped[int] = mapped_column(default=0)
    max_attempts: Mapped[int] = mapped_column(default=5)
    next_attempt_at: Mapped[datetime] = mapped_column(default=func.now(), index=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(default=func.now(), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    context: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    def __repr__(self):
        return f"<GitHubTask(id={self.id}, type='{self.task_type}', status='{self.status.name}')>"
