from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.webhook_event import Base


class BuildFailureIssue(Base):
    __tablename__ = "build_failure_issue"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    app_id: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    git_repo: Mapped[str] = mapped_column(String(255), nullable=False)
    issue_number: Mapped[int | None] = mapped_column(nullable=True)
    latest_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latest_build_url: Mapped[str | None] = mapped_column(Text, nullable=True)
