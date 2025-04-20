import uuid
from datetime import datetime

from app.models import Pipeline, PipelineStatus, PipelineTrigger


def test_pipeline_model_creation():
    now = datetime.now()
    pipeline = Pipeline(
        app_id="org.flathub.Test",
        params={"commit": "abc123"},
        status=PipelineStatus.PENDING,
        triggered_by=PipelineTrigger.WEBHOOK,
        created_at=now,
    )

    assert pipeline.app_id == "org.flathub.Test"
    assert pipeline.params == {"commit": "abc123"}
    assert pipeline.status == PipelineStatus.PENDING
    assert pipeline.triggered_by == PipelineTrigger.WEBHOOK
    assert pipeline.webhook_event_id is None
    assert pipeline.created_at is not None
    assert pipeline.started_at is None
    assert pipeline.finished_at is None
    assert pipeline.published_at is None


def test_pipeline_model_with_manual_trigger():
    """Test Pipeline model creation with manual trigger."""
    pipeline = Pipeline(
        app_id="org.flathub.Test",
        params={"commit": "abc123"},
        triggered_by=PipelineTrigger.MANUAL,
    )

    assert pipeline.triggered_by == PipelineTrigger.MANUAL


def test_pipeline_model_with_full_parameters():
    """Test Pipeline model creation with all parameters."""
    now = datetime.now()
    webhook_id = uuid.uuid4()

    pipeline = Pipeline(
        app_id="org.flathub.Test",
        params={"commit": "abc123", "repo": "stable"},
        status=PipelineStatus.RUNNING,
        triggered_by=PipelineTrigger.WEBHOOK,
        webhook_event_id=webhook_id,
        created_at=now,
        started_at=now,
    )

    assert pipeline.app_id == "org.flathub.Test"
    assert pipeline.params == {"commit": "abc123", "repo": "stable"}
    assert pipeline.status == PipelineStatus.RUNNING
    assert pipeline.triggered_by == PipelineTrigger.WEBHOOK
    assert pipeline.webhook_event_id == webhook_id
    assert pipeline.created_at == now
    assert pipeline.started_at == now
    assert pipeline.finished_at is None
    assert pipeline.published_at is None


def test_pipeline_model_with_provider_fields():
    """Test Pipeline model with provider fields."""
    now = datetime.now()

    pipeline = Pipeline(
        app_id="org.flathub.Test",
        params={"commit": "abc123"},
        status=PipelineStatus.RUNNING,
        provider="github",
        provider_data={"workflow_id": "build.yml"},
        created_at=now,
        started_at=now,
    )

    assert pipeline.provider == "github"
    assert pipeline.provider_data == {"workflow_id": "build.yml"}
