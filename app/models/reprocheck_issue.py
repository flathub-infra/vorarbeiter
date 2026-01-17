import uuid
from datetime import datetime
from enum import Enum

import sqlalchemy
from sqlalchemy import String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.webhook_event import Base


class ReprocheckIssueStatus(Enum):
    OPEN = "open"
    CLOSED = "closed"


class ReprocheckIssue(Base):
    __tablename__ = "reprocheck_issue"

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, index=True
    )
    app_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    git_repo: Mapped[str] = mapped_column(String(255))
    issue_number: Mapped[int] = mapped_column()
    issue_url: Mapped[str] = mapped_column(Text)
    status: Mapped[ReprocheckIssueStatus] = mapped_column(
        index=True,
        default=ReprocheckIssueStatus.OPEN,
        type_=sqlalchemy.Enum(
            ReprocheckIssueStatus, values_callable=lambda x: [e.value for e in x]
        ),
    )
    last_status_code: Mapped[str] = mapped_column(String(10))
    last_pipeline_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=func.now())
    updated_at: Mapped[datetime] = mapped_column(default=func.now(), onupdate=func.now())
    closed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    def __repr__(self):
        return f"<ReprocheckIssue(id={self.id}, app_id='{self.app_id}', status='{self.status.name}')>"
