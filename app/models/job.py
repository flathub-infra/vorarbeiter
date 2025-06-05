import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy import JSON, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.webhook_event import Base


class JobStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class Job(Base):
    __tablename__ = "job"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, index=True
    )
    status: Mapped[JobStatus] = mapped_column(index=True, default=JobStatus.PENDING)
    job_type: Mapped[str] = mapped_column(String(255), index=True)
    position: Mapped[int] = mapped_column(Integer, index=True)

    provider_data: Mapped[dict[str, Any]] = mapped_column(JSON)

    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(default=func.now(), index=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)

    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline.id"), index=True
    )
    pipeline = relationship("Pipeline", backref="jobs")

    def __repr__(self):
        return (
            f"<Job(id={self.id}, type='{self.job_type}', status='{self.status.name}')>"
        )
