from datetime import datetime

from sqlalchemy import JSON, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.webhook_event import Base


class InactiveRepoSnapshot(Base):
    __tablename__ = "inactive_repo_snapshot"

    organization: Mapped[str] = mapped_column(String(255), primary_key=True)
    scan_started_at: Mapped[datetime]
    scan_completed_at: Mapped[datetime]
    automatic_candidates: Mapped[list[str]] = mapped_column(JSON)
