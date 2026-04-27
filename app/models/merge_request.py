import secrets
import uuid
from datetime import datetime
from enum import Enum
from typing import Any

import sqlalchemy
from sqlalchemy import JSON, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.webhook_event import Base


class MergeStatus(Enum):
    CREATING = "creating"
    PUSHING = "pushing"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"


ACTIVE_MERGE_STATUS_VALUES = (
    MergeStatus.CREATING.value,
    MergeStatus.PUSHING.value,
    MergeStatus.FINALIZING.value,
)


class MergeRequest(Base):
    __tablename__ = "merge_request"
    __table_args__ = (
        sqlalchemy.Index(
            "uq_merge_request_active_pr_number",
            "pr_number",
            unique=True,
            sqlite_where=sqlalchemy.text(
                "status IN ('creating', 'pushing', 'finalizing')"
            ),
        ),
        sqlalchemy.Index(
            "uq_merge_request_active_app_id",
            "app_id",
            unique=True,
            sqlite_where=sqlalchemy.text(
                "status IN ('creating', 'pushing', 'finalizing')"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        primary_key=True, default=uuid.uuid4, index=True
    )
    pr_number: Mapped[int] = mapped_column(index=True)
    app_id: Mapped[str] = mapped_column(String(255), index=True)
    target_branch: Mapped[str] = mapped_column(String(255))
    pr_head_sha: Mapped[str] = mapped_column(String(40))
    collaborators: Mapped[list[str]] = mapped_column(JSON)
    status: Mapped[MergeStatus] = mapped_column(
        index=True,
        default=MergeStatus.CREATING,
        type_=sqlalchemy.Enum(
            MergeStatus, values_callable=lambda x: [e.value for e in x]
        ),
    )
    callback_token: Mapped[str] = mapped_column(
        String(32), default=lambda: secrets.token_hex(16)
    )
    comment_author: Mapped[str] = mapped_column(String(255))
    fork_url: Mapped[str] = mapped_column(Text)
    fork_branch: Mapped[str] = mapped_column(String(255))
    repo_html_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    provider_data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(default=func.now(), index=True)
    completed_at: Mapped[datetime | None] = mapped_column(nullable=True)

    def __repr__(self):
        return f"<MergeRequest(id={self.id}, status='{self.status.name}', app_id='{self.app_id}', pr={self.pr_number})>"
