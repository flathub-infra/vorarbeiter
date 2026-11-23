from datetime import datetime
from typing import Any

from sqlalchemy import JSON, BigInteger, Index, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class FlatManagerWebhookDelivery(Base):
    __tablename__ = "flat_manager_webhook_deliveries"
    __table_args__ = (
        Index(
            "ix_flat_manager_webhook_deliveries_unprocessed",
            "received_at",
            postgresql_where="processed_at IS NULL",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    event_type: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql")
    )
    received_at: Mapped[datetime] = mapped_column(default=func.now(), index=True)
    processed_at: Mapped[datetime | None] = mapped_column(nullable=True)
