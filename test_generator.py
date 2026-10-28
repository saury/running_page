import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import polyline
import pytest


@pytest.fixture
def generator(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parent / "run_page"))
    monkeypatch.delenv("GENERATE_INDOOR_ROUTES", raising=False)
    return importlib.import_module("generator")


def _activities():
    return [
        {
            "run_id": 1,
            "distance": 2000,
            "type": "Run",
            "subtype": "Run",
            "summary_polyline": polyline.encode(
                [(30.0 + i * 0.005, 120.0) for i in range(5)]
            ),
            "location_country": "Outdoor location",
        },
        {
            "run_id": 2,
            "distance": 1000,
            "type": "Run",
            "subtype": "Run",
            "summary_polyline": None,
            "location_country": None,
        },
    ]


@pytest.mark.parametrize("subtype", ["Run", "treadmill", "indoor"])
@pytest.mark.parametrize("filter_before_saving", [False, True])
def test_load_preserves_source_routes_and_missing_gps(
    generator, monkeypatch, tmp_path, subtype, filter_before_saving
):
    monkeypatch.setattr(generator, "IGNORE_BEFORE_SAVING", filter_before_saving)
    activities = _activities()
    activities[1]["subtype"] = subtype
    app = generator.Generator(tmp_path / "activities.db")
    try:
        for activity in activities:
            date = f"2026-01-0{activity['run_id']} 12:00:00"
            app.session.add(
                generator.Activity(**activity, start_date=date, start_date_local=date)
            )
        app.session.commit()

        for _ in range(2):
            result = app.load()
            app.session.expire_all()
            outdoor = app.session.get(generator.Activity, 1)
            indoor = app.session.get(generator.Activity, 2)
            assert outdoor.summary_polyline == activities[0]["summary_polyline"]
            expected_route = activities[0]["summary_polyline"]
            if not filter_before_saving:
                expected_route = generator.filter_out(expected_route)
            assert result[0]["summary_polyline"] == expected_route
            assert result[1]["distance"] == indoor.distance == 1000
            assert result[1]["summary_polyline"] is None
            assert indoor.summary_polyline is None
            assert result[1]["subtype"] == indoor.subtype == subtype
            assert result[1]["location_country"] is None
            assert indoor.location_country is None
    finally:
        app.session.close()
        app.session.bind.dispose()


@pytest.mark.parametrize("api_fault", [True, False])
def test_sync_preserves_cached_data_on_strava_fault(
    generator, monkeypatch, tmp_path, capsys, api_fault
):
    app = generator.Generator(tmp_path / "activities.db")
    try:
        app.session.add(
            generator.Activity(
                **_activities()[0],
                start_date="2026-01-01 12:00:00",
                start_date_local="2026-01-01 12:00:00",
            )
        )
        app.session.commit()
        monkeypatch.setattr(app, "check_access", lambda: None)
        error = (
            generator.stravalib.exc.Fault("Application Inactive")
            if api_fault
            else RuntimeError("Unexpected error")
        )

        def failed_activities(**filters):
            yield from ()
            raise error

        monkeypatch.setattr(app.client, "get_activities", failed_activities)
        if api_fault:
            app.sync(False)
            output = capsys.readouterr().out
            assert "Warning: Failed to fetch activities from Strava:" in output
            assert "Continuing with existing data." in output
        else:
            with pytest.raises(RuntimeError, match="Unexpected error"):
                app.sync(False)
        assert app.session.query(generator.Activity).count() == 1
        assert app.session.get(generator.Activity, 1).distance == 2000
    finally:
        app.session.close()
        app.session.bind.dispose()


def test_sync_only_run_keeps_upstream_activity_fields(generator, monkeypatch, tmp_path):
    app = generator.Generator(tmp_path / "activities.db")
    try:
        app.only_run = True
        monkeypatch.setattr(app, "check_access", lambda: None)
        run = SimpleNamespace(type="Run", map=None, total_elevation_gain=42)
        ride = SimpleNamespace(type="Ride")
        monkeypatch.setattr(app.client, "get_activities", lambda **filters: [ride, run])
        save_activity = Mock(return_value=True)
        monkeypatch.setattr(generator, "update_or_create_activity", save_activity)

        app.sync(True)

        save_activity.assert_called_once_with(app.session, run)
        assert run.elevation_gain == 42
        assert run.subtype == "Run"
    finally:
        app.session.close()
        app.session.bind.dispose()
