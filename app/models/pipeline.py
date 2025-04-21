import secrets
import uuid
from datetime import datetime
from enum import Enum
from typing import Any

import sqlalchemy
from sqlalchemy import JSON, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.webhook_event import Base


class PipelineStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PUBLISHED = "published"
    SUCCEEDED = "succeeded"
    SUPERSEDED = "superseded"


class PipelineTrigger(Enum):
    WEBHOOK = "webhook"
    MANUAL = "manual"


class Pipeline(Base):
    __tablename__ = "pipeline"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, index=True
    )
    status: Mapped[PipelineStatus] = mapped_column(
        index=True,
        default=PipelineStatus.PENDING,
        type_=sqlalchemy.Enum(
            PipelineStatus, values_callable=lambda x: [e.value for e in x]
        ),
    )
    app_id: Mapped[str] = mapped_column(String(255), index=True)
    params: Mapped[dict[str, Any]] = mapped_column(JSON)
    repo: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    flat_manager_repo: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True
    )
    triggered_by: Mapped[PipelineTrigger] = mapped_column(
        index=True,
        default=PipelineTrigger.WEBHOOK,
        type_=sqlalchemy.Enum(
            PipelineTrigger, values_callable=lambda x: [e.value for e in x]
        ),
    )

    provider: Mapped[str] = mapped_column(String(255), index=True, nullable=True)
    provider_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    created_at: Mapped[datetime] = mapped_column(default=func.now(), index=True)
    started_at: Mapped[datetime | None] = mapped_column(nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)

    webhook_event_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("webhookevent.id"), nullable=True, index=True
    )
    webhook_event = relationship("WebhookEvent", backref="pipelines")

    log_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    build_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    callback_token: Mapped[str] = mapped_column(
        String(32), default=lambda: secrets.token_hex(16)
    )

    def __repr__(self):
        return f"<Pipeline(id={self.id}, status='{self.status.name}', app_id='{self.app_id}')>"
