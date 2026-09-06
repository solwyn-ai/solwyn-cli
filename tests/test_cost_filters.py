"""Query windows and attribution filters are shared across cost reads."""

import pytest

from solwyn_cli.commands._cost_filters import cost_params, window_params


def test_custom_window_replaces_range_and_routes_storage_run():
    params = cost_params(
        time_range="7d",
        from_="2026-08-01T00:00:00Z",
        to="2026-08-02T00:00:00+00:00",
        agent_run="run_" + "a" * 32,
        tag=["customer=a=b", "empty="],
        model_fallback=False,
    )
    assert "range" not in params
    assert params["agent_run_id"] == "run_" + "a" * 32
    assert params["tag.customer"] == "a=b"
    assert params["tag.empty"] == ""
    assert params["is_model_fallback"] is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"from_": "2026-01-01T00:00:00Z"},
        {"from_": "2026-01-01", "to": "2026-01-02"},
        {"from_": "2026-01-02T00:00:00Z", "to": "2026-01-01T00:00:00Z"},
    ],
)
def test_bad_windows_raise_usage(kwargs):
    with pytest.raises(ValueError):
        window_params(**kwargs)


@pytest.mark.parametrize(
    "tag", [["x"], ["=value"], ["a=1", "a=2"], ["x=" + "v" * 257], ["x=\0"]]
)
def test_invalid_tag_filters(tag):
    with pytest.raises(ValueError):
        cost_params(tag=tag)
