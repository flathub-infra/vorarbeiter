from sqlalchemy import String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.webhook_event import Base


class ReprocheckIssue(Base):
    __tablename__ = "reprocheck_issue"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    app_id: Mapped[str] = mapped_column(
        String(255), unique=True, index=True, nullable=False
    )
    issue_number: Mapped[int] = mapped_column(nullable=False)

    def __repr__(self):
        return f"<ReprocheckIssue(id={self.id}, app_id='{self.app_id}', issue_number={self.issue_number})>"
