from typing import Any, TYPE_CHECKING

from pydantic import ValidationError

from app.schemas.pipelines import PipelineLogUrlCallback, PipelineStatusCallback

if TYPE_CHECKING:
    from app.pipelines import CallbackData


class CallbackValidator:
    """Validates and parses pipeline callback data."""

    def validate_and_parse(self, data: dict[str, Any]) -> "CallbackData":
        """
        Validate callback data and determine its type.

        Args:
            data: Raw callback data

        Returns:
            Parsed CallbackData

        Raises:
            ValueError: If validation fails
        """
        if "status" in data:
            try:
                PipelineStatusCallback(**data)
            except ValidationError as e:
                raise ValueError(f"Invalid status callback: {e}")
        elif "log_url" in data:
            try:
                PipelineLogUrlCallback(**data)
            except ValidationError as e:
                raise ValueError(f"Invalid log_url callback: {e}")
        elif not any(
            field in data
            for field in [
                "app_id",
                "is_extra_data",
                "end_of_life",
                "end_of_life_rebase",
            ]
        ):
            raise ValueError(
                "Request must contain either 'status', 'log_url', 'app_id', "
                "'is_extra_data', 'end_of_life', or 'end_of_life_rebase' field"
            )

        from app.pipelines import CallbackData

        return CallbackData(
            status=data.get("status"),
            log_url=data.get("log_url"),
            app_id=data.get("app_id"),
            is_extra_data=data.get("is_extra_data"),
            end_of_life=data.get("end_of_life"),
            end_of_life_rebase=data.get("end_of_life_rebase"),
        )
