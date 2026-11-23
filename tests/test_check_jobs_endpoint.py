import pytest


def test_check_jobs_endpoint_removed(client):
    response = client.post(
        "/api/pipelines/check-jobs",
        headers={"Authorization": "Bearer wrong_token"},
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_check_jobs_no_pipelines(
    db_session_maker, run_check_all_active_pipelines
):
    result = await run_check_all_active_pipelines(db_session_maker)

    assert result["checked_pipelines"] == 0
    assert result["updated_pipelines"] == 0
