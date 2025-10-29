from typing import TYPE_CHECKING, Any

from pydantic import ValidationError

if TYPE_CHECKING:
    from app.pipelines import CallbackData
from app.schemas.pipelines import PipelineLogUrlCallback, PipelineStatusCallback


class CallbackValidator:
    def validate_and_parse(self, data: dict[str, Any]) -> "CallbackData":
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
            build_pipeline_id=data.get("build_pipeline_id"),
        )


class MetadataCallbackValidator:
    def validate_and_parse(self, data: dict[str, Any]) -> "CallbackData":
        from app.pipelines import CallbackData

        return CallbackData(
            app_id=data.get("app_id"),
            is_extra_data=data.get("is_extra_data"),
            end_of_life=data.get("end_of_life"),
            end_of_life_rebase=data.get("end_of_life_rebase"),
        )


class LogUrlCallbackValidator:
    def validate_and_parse(self, data: dict[str, Any]) -> "CallbackData":
        if "log_url" not in data:
            raise ValueError("log_url is required")

        try:
            PipelineLogUrlCallback(**data)
        except ValidationError as e:
            raise ValueError(f"Invalid log_url callback: {e}")

        from app.pipelines import CallbackData

        return CallbackData(log_url=data.get("log_url"))


class StatusCallbackValidator:
    def validate_and_parse(self, data: dict[str, Any]) -> "CallbackData":
        if "status" not in data:
            raise ValueError("status is required")

        try:
            PipelineStatusCallback(**data)
        except ValidationError as e:
            raise ValueError(f"Invalid status callback: {e}")

        from app.pipelines import CallbackData

        return CallbackData(status=data.get("status"))


class ReprochecKCallbackValidator:
    def validate_and_parse(self, data: dict[str, Any]) -> "CallbackData":
        if "status" not in data:
            raise ValueError("status is required")

        try:
            PipelineStatusCallback(**data)
        except ValidationError as e:
            raise ValueError(f"Invalid status callback: {e}")

        from app.pipelines import CallbackData

        return CallbackData(
            status=data.get("status"),
            build_pipeline_id=data.get("build_pipeline_id"),
        )
