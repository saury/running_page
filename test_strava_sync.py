import datetime
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest
import yaml


@pytest.fixture
def sync_modules(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(Path(__file__).parent / "run_page"))
    monkeypatch.delenv("STRAVA_JWT", raising=False)
    monkeypatch.delenv("STRAVA_SESSION_COOKIE", raising=False)
    monkeypatch.delenv("STRAVA_WEB_DAYS", raising=False)
    api = importlib.import_module("strava_sync")
    web = importlib.import_module("strava_web_sync")
    monkeypatch.setattr(api, "JSON_FILE", tmp_path / "activities.json")
    return api, web


def _generator(api, monkeypatch, error=None):
    app = Mock()
    app.sync.side_effect = error
    app.load.return_value = [{"run_id": 1}]
    app.session.query.return_value.scalar.return_value = (
        datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=100)
    ).isoformat()
    monkeypatch.setattr(api, "Generator", Mock(return_value=app))
    return app


def test_inactive_without_jwt_fails_without_overwriting_data(sync_modules, monkeypatch):
    api, _ = sync_modules
    app = _generator(api, monkeypatch, api.stravalib.exc.Fault("Application Inactive"))
    api.JSON_FILE.write_text("existing data")

    with pytest.raises(RuntimeError, match="New activities were NOT synced"):
        api.run_strava_sync("id", "secret", "refresh", only_run=True)

    assert api.JSON_FILE.read_text() == "existing data"
    app.load.assert_not_called()
    app.session.rollback.assert_called_once()
    app.session.close.assert_called_once()


@pytest.mark.parametrize("configured_days,expected_days", [(None, 107), ("365", 365)])
def test_inactive_falls_back_to_web_and_covers_cached_gap(
    sync_modules, monkeypatch, configured_days, expected_days
):
    api, web = sync_modules
    app = _generator(api, monkeypatch, api.stravalib.exc.Fault("Application Inactive"))
    monkeypatch.setenv("STRAVA_JWT", "test-session-cookie")
    if configured_days:
        monkeypatch.setenv("STRAVA_WEB_DAYS", configured_days)
    fallback = Mock()
    monkeypatch.setattr(web, "run_strava_web_sync", fallback)

    api.run_strava_sync("id", "secret", "refresh", only_run=True)

    fallback.assert_called_once_with(
        "test-session-cookie", days=expected_days, only_run=True
    )
    app.load.assert_not_called()
    app.session.close.assert_called_once()


def test_other_api_fault_does_not_fall_back(sync_modules, monkeypatch):
    api, web = sync_modules
    _generator(api, monkeypatch, api.stravalib.exc.Fault("Rate Limit Exceeded"))
    monkeypatch.setenv("STRAVA_JWT", "test-session-cookie")
    fallback = Mock()
    monkeypatch.setattr(web, "run_strava_web_sync", fallback)

    with pytest.raises(api.stravalib.exc.Fault, match="Rate Limit"):
        api.run_strava_sync("id", "secret", "refresh")

    fallback.assert_not_called()


def test_web_failure_is_not_reported_as_success(sync_modules, monkeypatch):
    api, web = sync_modules
    _generator(api, monkeypatch, api.stravalib.exc.Fault("Application Inactive"))
    monkeypatch.setenv("STRAVA_JWT", "expired-test-cookie")
    monkeypatch.setattr(
        web, "run_strava_web_sync", Mock(side_effect=RuntimeError("Web login failed"))
    )

    with pytest.raises(RuntimeError, match="Web login failed"):
        api.run_strava_sync("id", "secret", "refresh")


def test_normal_api_sync_exports_data(sync_modules, monkeypatch):
    api, _ = sync_modules
    app = _generator(api, monkeypatch)

    api.run_strava_sync("id", "secret", "refresh", only_run=True)

    assert api.JSON_FILE.read_text() == '[{"run_id": 1}]'
    assert app.only_run is True
    app.session.close.assert_called_once()


def test_inactive_prefers_session_cookie_to_jwt(sync_modules, monkeypatch):
    api, web = sync_modules
    app = _generator(api, monkeypatch, api.stravalib.exc.Fault("Application Inactive"))
    monkeypatch.setenv("STRAVA_SESSION_COOKIE", "test-session-cookie")
    monkeypatch.setenv("STRAVA_JWT", "old-test-jwt")
    monkeypatch.setenv("STRAVA_WEB_DAYS", "365")
    fallback = Mock()
    monkeypatch.setattr(web, "run_strava_web_sync", fallback)

    api.run_strava_sync("id", "secret", "refresh", only_run=True)

    fallback.assert_called_once_with(
        days=365, only_run=True, session_cookie="test-session-cookie"
    )
    app.load.assert_not_called()


def _session_responses(location="/athletes/123", token="test-csrf-token"):
    login = Mock(
        url="https://www.strava.com/me",
        headers={"Location": location},
        is_redirect=True,
    )
    training = Mock(
        is_redirect=False,
        text=f'<html><head><meta content="{token}" name="csrf-token"></head></html>',
    )
    return login, training


def test_session_cookie_login_and_csrf(sync_modules, monkeypatch):
    _, web = sync_modules
    session = Mock()
    session.get.side_effect = _session_responses()
    monkeypatch.setattr(web.requests, "Session", Mock(return_value=session))

    client = web.SessionWebClient("test-session-cookie")

    session.cookies.set.assert_called_once_with(
        "_strava4_session",
        "test-session-cookie",
        domain="www.strava.com",
        path="/",
        secure=True,
    )
    assert client.csrf == {"authenticity_token": "test-csrf-token"}
    assert session.get.call_args_list == [
        call("https://www.strava.com/me", allow_redirects=False, timeout=30),
        call(
            "https://www.strava.com/athlete/training",
            allow_redirects=False,
            timeout=30,
        ),
    ]


@pytest.mark.parametrize(
    "location",
    ["/login", "https://example.com/athletes/123", "/athletes/not-a-number"],
)
def test_session_cookie_rejects_invalid_login(sync_modules, monkeypatch, location):
    _, web = sync_modules
    session = Mock()
    session.get.side_effect = _session_responses(location)
    monkeypatch.setattr(web.requests, "Session", Mock(return_value=session))

    with pytest.raises(ValueError, match="Refresh STRAVA_SESSION_COOKIE") as error:
        web.SessionWebClient("private-test-value")

    assert "private-test-value" not in str(error.value)
    session.close.assert_called_once()


@pytest.mark.parametrize("redirect", [True, False])
def test_session_cookie_requires_authenticated_csrf(
    sync_modules, monkeypatch, redirect
):
    _, web = sync_modules
    session = Mock()
    login, training = _session_responses(token="")
    training.is_redirect = redirect
    session.get.side_effect = [login, training]
    monkeypatch.setattr(web.requests, "Session", Mock(return_value=session))

    with pytest.raises(ValueError, match="CSRF token"):
        web.SessionWebClient("test-session-cookie")

    session.close.assert_called_once()


def test_session_cookie_uses_existing_web_sync(sync_modules, monkeypatch):
    _, web = sync_modules
    monkeypatch.setenv("STRAVA_SESSION_COOKIE", "test-session-cookie")
    client = Mock()
    factory = Mock(return_value=client)
    jwt_factory = Mock()
    monkeypatch.setattr(web, "SessionWebClient", factory)
    monkeypatch.setattr(web, "WebClient", jwt_factory)
    monkeypatch.setattr(web, "init_db", Mock())
    monkeypatch.setattr(web, "_fetch_json", Mock(return_value={"models": []}))
    finalize = Mock(return_value=0)
    monkeypatch.setattr(web, "_finalize", finalize)

    assert web.run_strava_web_sync(only_run=True) == 0

    factory.assert_called_once_with("test-session-cookie")
    jwt_factory.assert_not_called()
    finalize.assert_called_once()


@pytest.mark.parametrize(
    "redirect,payload",
    [(True, {"models": []}), (False, {}), (False, []), (False, {"models": None})],
)
def test_invalid_activity_response_fails(sync_modules, redirect, payload):
    _, web = sync_modules
    client = Mock()
    response = client._session.get.return_value
    response.is_redirect = redirect
    response.json.return_value = payload

    with pytest.raises((ValueError, TypeError)):
        web._fetch_json(client, "https://www.strava.com/athlete/training_activities")


def test_session_csrf_is_sent_with_stream_request(sync_modules):
    _, web = sync_modules
    client = Mock(csrf={"authenticity_token": "test-csrf-token"})
    client._session.get.return_value.is_redirect = False
    client._session.get.return_value.json.return_value = {"latlng": [[30, 120]]}

    assert web._fetch_streams(client, 123) == {"latlng": [[30, 120]]}
    request = client._session.get.call_args
    assert request.kwargs["headers"]["x-csrf-token"] == "test-csrf-token"
    assert request.kwargs["allow_redirects"] is False
    assert request.kwargs["timeout"] == 30


@pytest.mark.parametrize("days", ["0", "-1", "invalid"])
def test_invalid_web_lookback_fails(sync_modules, monkeypatch, days):
    api, _ = sync_modules
    app = _generator(api, monkeypatch)
    monkeypatch.setenv("STRAVA_WEB_DAYS", days)

    with pytest.raises(ValueError):
        api._web_sync_days(app)


def test_empty_cache_requires_explicit_lookback(sync_modules, monkeypatch):
    api, _ = sync_modules
    app = _generator(api, monkeypatch)
    app.session.query.return_value.scalar.return_value = None

    with pytest.raises(RuntimeError, match="Set STRAVA_WEB_DAYS"):
        api._web_sync_days(app)


def test_web_sync_fetches_partial_last_page(sync_modules, monkeypatch):
    _, web = sync_modules
    monkeypatch.setattr(web, "WebClient", Mock(return_value=SimpleNamespace()))
    monkeypatch.setattr(web, "init_db", Mock())
    monkeypatch.setattr(web.time, "sleep", lambda _: None)
    rows = [
        {"id": i, "sport_type": "Run", "start_date_local_raw": web.time.time()}
        for i in range(21)
    ]
    fetch = Mock(
        side_effect=[
            {"models": rows[:20], "total": 21},
            {"models": rows[20:], "total": 21},
        ]
    )
    monkeypatch.setattr(web, "_fetch_json", fetch)
    sync = Mock()
    monkeypatch.setattr(web, "_sync_one", sync)
    monkeypatch.setattr(web, "_finalize", lambda session, count: count)

    assert web.run_strava_web_sync("test-session-cookie", only_run=True) == 21
    assert fetch.call_count == 2
    assert sync.call_count == 21


@pytest.mark.parametrize("jwt,days", [("", 7), ("test-session-cookie", 0)])
def test_web_sync_validates_credentials_and_lookback(sync_modules, jwt, days):
    _, web = sync_modules
    with pytest.raises(ValueError):
        web.run_strava_web_sync(jwt, days=days)


@pytest.mark.parametrize("workflow", ["run_data_sync.yml", "gh-pages.yml"])
def test_cache_restore_preserves_current_asset_module(workflow):
    root = Path(__file__).parent
    config = yaml.safe_load((root / ".github" / "workflows" / workflow).read_text())
    job = "sync" if workflow == "run_data_sync.yml" else "build_and_deploy"
    steps = config["jobs"][job]["steps"]
    cache_index = next(
        i for i, step in enumerate(steps) if step.get("name") == "Cache Data Files"
    )
    restore = steps[cache_index + 1]

    assert restore["run"] == "git restore --source=HEAD -- assets/index.tsx"
    assert restore["if"] == steps[cache_index]["if"]
