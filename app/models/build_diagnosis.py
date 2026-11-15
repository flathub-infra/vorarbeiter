import uuid
from datetime import datetime
from enum import Enum
from typing import Any

import sqlalchemy
from sqlalchemy import JSON, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BuildDiagnosisStatus(Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class BuildDiagnosis(Base):
    __tablename__ = "build_diagnosis"

    pipeline_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("pipeline.id"), primary_key=True
    )
    status: Mapped[BuildDiagnosisStatus] = mapped_column(
        default=BuildDiagnosisStatus.PENDING,
        type_=sqlalchemy.Enum(
            BuildDiagnosisStatus, values_callable=lambda x: [e.value for e in x]
        ),
    )
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    prompt_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    input_tokens: Mapped[int | None] = mapped_column(nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(nullable=True)
    request_ids: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(nullable=True)
    comment_posted_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)
