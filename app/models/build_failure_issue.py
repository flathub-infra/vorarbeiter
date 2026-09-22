from sqlalchemy import Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.webhook_event import Base


class BuildFailureIssue(Base):
    __tablename__ = "build_failure_issue"
    __table_args__ = (
        Index(
            "ix_build_failure_issue_app_id_flat_manager_repo",
            "app_id",
            "flat_manager_repo",
            unique=True,
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    app_id: Mapped[str] = mapped_column(String(255), nullable=False)
    flat_manager_repo: Mapped[str] = mapped_column(String(64), nullable=False)
    git_repo: Mapped[str] = mapped_column(String(255), nullable=False)
    issue_number: Mapped[int | None] = mapped_column(nullable=True)
    latest_sha: Mapped[str | None] = mapped_column(String(64), nullable=True)
    latest_build_url: Mapped[str | None] = mapped_column(Text, nullable=True)
