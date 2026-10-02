import uuid
from datetime import datetime

from sqlalchemy import JSON, BigInteger, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.webhook_event import Base


class SmokeResult(Base):
    """Application-check state kept separate from publication transactions."""

    __tablename__ = "smoke_result"

    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline.id"), primary_key=True
    )
    run_id: Mapped[int] = mapped_column(BigInteger, index=True)
    run_attempt: Mapped[int]
    source_attempt: Mapped[int]
    artifact_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    summary: Mapped[dict] = mapped_column(JSON)
    notified: Mapped[bool] = mapped_column(default=False)
    updated_at: Mapped[datetime] = mapped_column(
        default=func.now(), onupdate=func.now()
    )
