import io
import uuid
import zipfile
from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.models import Pipeline, PipelineStatus, PipelineTrigger
from app.services.diffoscope import DiffoscopeReport
from tests.conftest import create_mock_get_db


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def reprocheck_pipeline_with_result():
    return Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        params={
            "workflow_id": "reprocheck.yml",
            "reprocheck_result": {
                "status_code": "42",
                "result_url": "https://s3.example.com/diffoscope/result.zip",
            },
        },
        created_at=datetime.now(),
        triggered_by=PipelineTrigger.MANUAL,
        provider_data={},
        callback_token="test_token",
    )


@pytest.fixture
def reprocheck_pipeline_without_result():
    return Pipeline(
        id=uuid.uuid4(),
        app_id="org.test.App",
        status=PipelineStatus.SUCCEEDED,
        params={"workflow_id": "reprocheck.yml"},
        created_at=datetime.now(),
        triggered_by=PipelineTrigger.MANUAL,
        provider_data={},
        callback_token="test_token",
    )


def create_test_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("index.html", b'<html><link href="common.css"></html>')
        zf.writestr("common.css", b"body { color: red; }")
        zf.writestr("icon.png", b"\x89PNG\r\n\x1a\n")
    return buffer.getvalue()


class TestViewDiffoscope:
    def test_view_diffoscope_success(
        self, client, mock_db, reprocheck_pipeline_with_result
    ):
        mock_db.get.return_value = reprocheck_pipeline_with_result

        report = DiffoscopeReport(
            html=b'<html><link href="common.css"></html>',
            css=b"body { color: red; }",
            icon=b"\x89PNG",
        )

        with patch("app.routes.diffoscope.get_db", create_mock_get_db(mock_db)):
            with patch(
                "app.routes.diffoscope.fetch_diffoscope_report",
                return_value=report,
            ):
                response = client.get(
                    f"/diffoscope/{reprocheck_pipeline_with_result.id}"
                )

        assert response.status_code == 200
        assert response.headers["content-type"] == "text/html; charset=utf-8"
        assert b"/diffoscope/" in response.content
        assert b"/common.css" in response.content

    def test_view_diffoscope_not_found(self, client, mock_db):
        mock_db.get.return_value = None

        with patch("app.routes.diffoscope.get_db", create_mock_get_db(mock_db)):
            response = client.get(f"/diffoscope/{uuid.uuid4()}")

        assert response.status_code == 404
        assert "Pipeline not found" in response.json()["detail"]

    def test_view_diffoscope_no_result_url(
        self, client, mock_db, reprocheck_pipeline_without_result
    ):
        mock_db.get.return_value = reprocheck_pipeline_without_result

        with patch("app.routes.diffoscope.get_db", create_mock_get_db(mock_db)):
            response = client.get(
                f"/diffoscope/{reprocheck_pipeline_without_result.id}"
            )

        assert response.status_code == 404
        assert "No diffoscope report available" in response.json()["detail"]

    def test_view_diffoscope_fetch_fails(
        self, client, mock_db, reprocheck_pipeline_with_result
    ):
        mock_db.get.return_value = reprocheck_pipeline_with_result

        with patch("app.routes.diffoscope.get_db", create_mock_get_db(mock_db)):
            with patch(
                "app.routes.diffoscope.fetch_diffoscope_report",
                return_value=None,
            ):
                response = client.get(
                    f"/diffoscope/{reprocheck_pipeline_with_result.id}"
                )

        assert response.status_code == 502
        assert "Failed to fetch" in response.json()["detail"]


class TestDiffoscopeCss:
    def test_get_css_success(self, client, mock_db, reprocheck_pipeline_with_result):
        mock_db.get.return_value = reprocheck_pipeline_with_result

        report = DiffoscopeReport(
            html=b"<html></html>",
            css=b"body { color: red; }",
            icon=b"",
        )

        with patch("app.routes.diffoscope.get_db", create_mock_get_db(mock_db)):
            with patch(
                "app.routes.diffoscope.fetch_diffoscope_report",
                return_value=report,
            ):
                response = client.get(
                    f"/diffoscope/{reprocheck_pipeline_with_result.id}/common.css"
                )

        assert response.status_code == 200
        assert response.headers["content-type"] == "text/css; charset=utf-8"
        assert response.content == b"body { color: red; }"

    def test_get_css_not_available(
        self, client, mock_db, reprocheck_pipeline_with_result
    ):
        mock_db.get.return_value = reprocheck_pipeline_with_result

        report = DiffoscopeReport(html=b"<html></html>", css=b"", icon=b"")

        with patch("app.routes.diffoscope.get_db", create_mock_get_db(mock_db)):
            with patch(
                "app.routes.diffoscope.fetch_diffoscope_report",
                return_value=report,
            ):
                response = client.get(
                    f"/diffoscope/{reprocheck_pipeline_with_result.id}/common.css"
                )

        assert response.status_code == 404


class TestDiffoscopeIcon:
    def test_get_icon_success(self, client, mock_db, reprocheck_pipeline_with_result):
        mock_db.get.return_value = reprocheck_pipeline_with_result

        report = DiffoscopeReport(
            html=b"<html></html>",
            css=b"",
            icon=b"\x89PNG\r\n\x1a\n",
        )

        with patch("app.routes.diffoscope.get_db", create_mock_get_db(mock_db)):
            with patch(
                "app.routes.diffoscope.fetch_diffoscope_report",
                return_value=report,
            ):
                response = client.get(
                    f"/diffoscope/{reprocheck_pipeline_with_result.id}/icon.png"
                )

        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert response.content == b"\x89PNG\r\n\x1a\n"

    def test_get_icon_not_available(
        self, client, mock_db, reprocheck_pipeline_with_result
    ):
        mock_db.get.return_value = reprocheck_pipeline_with_result

        report = DiffoscopeReport(html=b"<html></html>", css=b"", icon=b"")

        with patch("app.routes.diffoscope.get_db", create_mock_get_db(mock_db)):
            with patch(
                "app.routes.diffoscope.fetch_diffoscope_report",
                return_value=report,
            ):
                response = client.get(
                    f"/diffoscope/{reprocheck_pipeline_with_result.id}/icon.png"
                )

        assert response.status_code == 404
