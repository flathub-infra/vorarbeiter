import uuid
import secrets
from datetime import datetime
from enum import Enum
from typing import Any, Optional

import sqlalchemy
from sqlalchemy import String, JSON, Text, func, ForeignKey
from sqlalchemy.orm import mapped_column, Mapped, relationship

from app.models.webhook_event import Base


class PipelineStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PUBLISHED = "published"
    SUCCEEDED = "succeeded"


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
    triggered_by: Mapped[PipelineTrigger] = mapped_column(
        index=True,
        default=PipelineTrigger.WEBHOOK,
        type_=sqlalchemy.Enum(
            PipelineTrigger, values_callable=lambda x: [e.value for e in x]
        ),
    )

    provider: Mapped[str] = mapped_column(String(255), index=True, nullable=True)
    provider_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    result: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(default=func.now(), index=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
    published_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)

    webhook_event_id: Mapped[Optional[uuid.UUID]] = mapped_column(
        ForeignKey("webhookevent.id"), nullable=True, index=True
    )
    webhook_event = relationship("WebhookEvent", backref="pipelines")

    log_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    build_url: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    callback_token: Mapped[str] = mapped_column(
        String(32), default=lambda: secrets.token_hex(16)
    )

    def __repr__(self):
        return f"<Pipeline(id={self.id}, status='{self.status.name}', app_id='{self.app_id}')>"
