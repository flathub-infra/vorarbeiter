import pytest

from app.pipelines import CallbackData
from app.services.callback import CallbackValidator


@pytest.fixture
def validator():
    return CallbackValidator()


def test_validate_status_callback_success(validator):
    data = {"status": "success", "result": {"output": "Build completed"}}

    result = validator.validate_and_parse(data)

    assert isinstance(result, CallbackData)
    assert result.status == "success"
    assert result.log_url is None
    assert result.app_id is None
    assert result.is_extra_data is None
    assert result.end_of_life is None
    assert result.end_of_life_rebase is None


def test_validate_status_callback_failure(validator):
    data = {"status": "failure", "error": "Build failed"}

    result = validator.validate_and_parse(data)

    assert isinstance(result, CallbackData)
    assert result.status == "failure"


def test_validate_status_callback_invalid_status(validator):
    data = {"status": "invalid_status_value"}

    with pytest.raises(ValueError) as exc_info:
        validator.validate_and_parse(data)

    assert "Invalid status callback" in str(exc_info.value)


def test_validate_log_url_callback(validator):
    data = {"log_url": "https://example.com/logs/123"}

    result = validator.validate_and_parse(data)

    assert isinstance(result, CallbackData)
    assert result.log_url == "https://example.com/logs/123"
    assert result.status is None


def test_validate_log_url_callback_invalid_url(validator):
    data = {"log_url": "not-a-valid-url"}

    result = validator.validate_and_parse(data)

    assert isinstance(result, CallbackData)
    assert result.log_url == "not-a-valid-url"


def test_validate_app_id_callback(validator):
    data = {"app_id": "org.test.App"}

    result = validator.validate_and_parse(data)

    assert isinstance(result, CallbackData)
    assert result.app_id == "org.test.App"
    assert result.status is None
    assert result.log_url is None


def test_validate_is_extra_data_callback(validator):
    data = {"is_extra_data": True}

    result = validator.validate_and_parse(data)

    assert isinstance(result, CallbackData)
    assert result.is_extra_data is True


def test_validate_end_of_life_callback(validator):
    data = {"end_of_life": "This app is deprecated"}

    result = validator.validate_and_parse(data)

    assert isinstance(result, CallbackData)
    assert result.end_of_life == "This app is deprecated"
    assert result.end_of_life_rebase is None


def test_validate_end_of_life_rebase_callback(validator):
    data = {"end_of_life_rebase": "org.test.NewApp"}

    result = validator.validate_and_parse(data)

    assert isinstance(result, CallbackData)
    assert result.end_of_life_rebase == "org.test.NewApp"
    assert result.end_of_life is None


def test_validate_both_end_of_life_fields(validator):
    data = {"end_of_life": "App deprecated", "end_of_life_rebase": "org.test.NewApp"}

    result = validator.validate_and_parse(data)

    assert isinstance(result, CallbackData)
    assert result.end_of_life == "App deprecated"
    assert result.end_of_life_rebase == "org.test.NewApp"


def test_validate_mixed_fields(validator):
    data = {
        "status": "success",
        "log_url": "https://example.com/logs/123",
        "app_id": "org.test.App",
        "end_of_life": "Deprecated",
    }

    result = validator.validate_and_parse(data)

    assert isinstance(result, CallbackData)
    assert result.status == "success"
    assert result.log_url == "https://example.com/logs/123"
    assert result.app_id == "org.test.App"
    assert result.end_of_life == "Deprecated"


def test_validate_empty_data(validator):
    data = {}

    with pytest.raises(ValueError) as exc_info:
        validator.validate_and_parse(data)

    assert "Request must contain either" in str(exc_info.value)


def test_validate_unknown_fields_only(validator):
    data = {"unknown_field": "value", "another_unknown": 123}

    with pytest.raises(ValueError) as exc_info:
        validator.validate_and_parse(data)

    assert "Request must contain either" in str(exc_info.value)


def test_validate_status_with_extra_fields(validator):
    data = {"status": "success", "extra_field": "ignored", "another_extra": 123}

    result = validator.validate_and_parse(data)

    assert isinstance(result, CallbackData)
    assert result.status == "success"


def test_validate_none_values(validator):
    data = {"app_id": None, "is_extra_data": None, "end_of_life": None}

    result = validator.validate_and_parse(data)

    assert isinstance(result, CallbackData)
    assert result.app_id is None
    assert result.is_extra_data is None
    assert result.end_of_life is None


def test_validate_boolean_is_extra_data(validator):
    data_true = {"is_extra_data": True}
    data_false = {"is_extra_data": False}

    result_true = validator.validate_and_parse(data_true)
    result_false = validator.validate_and_parse(data_false)

    assert result_true.is_extra_data is True
    assert result_false.is_extra_data is False


def test_validate_preserves_all_fields(validator):
    data = {
        "status": "success",
        "log_url": "https://example.com/log",
        "app_id": "org.test.App",
        "is_extra_data": True,
        "end_of_life": "Deprecated",
        "end_of_life_rebase": "org.test.NewApp",
    }

    result = validator.validate_and_parse(data)

    assert result.status == data["status"]
    assert result.log_url == data["log_url"]
    assert result.app_id == data["app_id"]
    assert result.is_extra_data == data["is_extra_data"]
    assert result.end_of_life == data["end_of_life"]
    assert result.end_of_life_rebase == data["end_of_life_rebase"]
