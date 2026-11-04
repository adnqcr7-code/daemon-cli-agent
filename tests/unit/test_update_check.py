import httpx

from daemon.update_check import check_for_update


def _response(payload, status_code=200):
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("GET", "https://api.github.com/releases/latest"),
    )


def test_update_check_reports_new_release_and_caches_it(tmp_path, monkeypatch):
    calls = []

    def fake_get(*args, **kwargs):
        calls.append((args, kwargs))
        return _response({"tag_name": "v0.2.0"})

    monkeypatch.setattr("daemon.update_check.httpx.get", fake_get)

    assert check_for_update("0.1.0", tmp_path, now=1000) == "0.2.0"
    assert check_for_update("0.1.0", tmp_path, now=1001) == "0.2.0"
    assert len(calls) == 1
    assert calls[0][1]["timeout"].read == 2.0


def test_update_check_does_not_report_same_or_older_version(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "daemon.update_check.httpx.get",
        lambda *args, **kwargs: _response({"tag_name": "v0.1.0"}),
    )
    assert check_for_update("0.1.0", tmp_path, now=1000) is None


def test_update_check_normalizes_trailing_zero_components(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "daemon.update_check.httpx.get",
        lambda *args, **kwargs: _response({"tag_name": "v0.1"}),
    )
    assert check_for_update("0.1.0", tmp_path, now=1000) is None


def test_update_check_uses_cached_release_when_network_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "daemon.update_check.httpx.get",
        lambda *args, **kwargs: _response({"tag_name": "v0.3.0"}),
    )
    assert check_for_update("0.1.0", tmp_path, now=1000) == "0.3.0"

    def fail(*args, **kwargs):
        raise httpx.ConnectError("offline")

    monkeypatch.setattr("daemon.update_check.httpx.get", fail)
    assert check_for_update("0.1.0", tmp_path, now=1000 + 86401) == "0.3.0"


def test_update_check_ignores_invalid_release_tag(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "daemon.update_check.httpx.get",
        lambda *args, **kwargs: _response({"tag_name": "release-next"}),
    )
    assert check_for_update("0.1.0", tmp_path, now=1000) is None


def test_update_check_handles_missing_release(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "daemon.update_check.httpx.get",
        lambda *args, **kwargs: _response({}, status_code=404),
    )
    assert check_for_update("0.1.0", tmp_path, now=1000) is None
    assert check_for_update("0.1.0", tmp_path, now=1001) is None
