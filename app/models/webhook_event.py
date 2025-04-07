import uuid
from datetime import datetime
from enum import Enum as PyEnum
from typing import Any

from sqlalchemy import DateTime, String, Boolean, JSON, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import mapped_column, DeclarativeBase, Mapped


class Base(DeclarativeBase):
    type_annotation_map = {
        datetime: DateTime(timezone=True),
        uuid.UUID: UUID(as_uuid=True),
    }


class WebhookSource(PyEnum):
    GITHUB = "github"


class WebhookEvent(Base):
    __tablename__ = "webhookevent"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, index=True
    )
    source: Mapped[WebhookSource] = mapped_column(index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    repository: Mapped[str] = mapped_column(String(255), index=True)
    actor: Mapped[str] = mapped_column(String(255), index=True)
    received_at: Mapped[datetime] = mapped_column(default=func.now(), index=True)
    processed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    def __repr__(self):
        return f"<WebhookEvent(id={self.id}, source='{self.source.name}', repository='{self.repository}')>"
