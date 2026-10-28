#!/usr/bin/env python3
import argparse
import datetime
import json
import os

import stravalib
from config import JSON_FILE, SQL_FILE
from generator import Generator
from generator.db import Activity
from sqlalchemy import func


def _web_sync_days(generator):
    configured_days = os.getenv("STRAVA_WEB_DAYS")
    if configured_days:
        days = int(configured_days)
        if days <= 0:
            raise ValueError("STRAVA_WEB_DAYS must be a positive integer")
        return days
    last_date = generator.session.query(func.max(Activity.start_date)).scalar()
    if not last_date:
        raise RuntimeError(
            "No cached activities found. Set STRAVA_WEB_DAYS to cover "
            "the history you want to import."
        )
    last_date = datetime.datetime.fromisoformat(last_date)
    if last_date.tzinfo is None:
        last_date = last_date.replace(tzinfo=datetime.UTC)
    return max(7, (datetime.datetime.now(datetime.UTC) - last_date).days + 7)


# for only run type, we use the same logic as garmin_sync
def run_strava_sync(
    client_id,
    client_secret,
    refresh_token,
    sync_types: list | None = None,
    only_run=False,
):
    if sync_types is None:
        sync_types = []
    generator = Generator(SQL_FILE)
    generator.set_strava_config(client_id, client_secret, refresh_token)
    # judge sync types is only running or not
    if not only_run and len(sync_types) == 1 and sync_types[0] == "running":
        only_run = True
    # if you want to refresh data change False to True
    generator.only_run = only_run
    try:
        try:
            generator.sync(False)
        except stravalib.exc.Fault as error:
            generator.session.rollback()
            if "Inactive" not in str(error):
                raise
            jwt = os.getenv("STRAVA_JWT")
            if not jwt:
                raise RuntimeError(
                    "Strava API application is inactive. New activities were NOT "
                    "synced. Reactivate the Strava API application, or set the "
                    "STRAVA_JWT repository secret to the strava_remember_token "
                    "cookie from your own Strava browser session. "
                    "Existing cached data is preserved."
                ) from error
            from strava_web_sync import run_strava_web_sync

            days = _web_sync_days(generator)
            print(f"Strava API inactive; trying web sync for the last {days} days.")
            run_strava_web_sync(jwt, days=days, only_run=only_run)
            return

        activities_list = generator.load()
        with open(JSON_FILE, "w") as f:
            json.dump(activities_list, f)
    finally:
        generator.session.close()
        generator.session.bind.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("client_id", help="strava client id")
    parser.add_argument("client_secret", help="strava client secret")
    parser.add_argument("refresh_token", help="strava refresh token")
    parser.add_argument(
        "--only-run",
        dest="only_run",
        action="store_true",
        help="if is only for running",
    )
    options = parser.parse_args()
    run_strava_sync(
        options.client_id,
        options.client_secret,
        options.refresh_token,
        only_run=options.only_run,
    )
