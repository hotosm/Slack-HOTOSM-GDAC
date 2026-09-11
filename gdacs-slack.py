#!/usr/bin/env python3
"""
GDACS Alert Notifier for HOTOSM Slack
--------------------------------------
Fetches orange and red disaster alerts from the GDACS API and posts new ones
to Slack via a standard Incoming Webhook. All posted events are tracked in
posted_events.json, which is committed back to the repo after each run.

Deduplication key: (event_id, alert_level)
  - A brand-new orange or red event is always posted.
  - If an existing event escalates from orange to red, it is posted again.
  - If an event is unchanged (same event_id, same alert_level), it is skipped.

Both the initial run and the nightly run query from INITIAL_FROM_DATE to today.
Date filtering is never used to decide what to post -- only the posted_events.json
record determines whether an event has already been sent to Slack.
"""

import html
import json
import os
import re
import sys
import time
import requests
from datetime import date

# -- Configuration -------------------------------------------------------------

POSTED_EVENTS_FILE = "posted_events.json"
GDACS_API_URL = "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# All runs query from this date; the state file determines what is new.
INITIAL_FROM_DATE = "2026-01-01"

ALERT_LEVELS = {"orange", "red"}

# Attachment accent colors.
ALERT_COLOURS = {
    "red":    "#CC0000",
    "orange": "#FF9900",
}

# Keep descriptions readable and within Slack's block limit.
MAX_DESCRIPTION_CHARS = 500

# -- Helpers -------------------------------------------------------------------

def posted_key(event_id: str, alert_level: str) -> str:
    """Unique key for a (event_id, alert_level) pair."""
    return f"{event_id}|{alert_level.lower()}"


def strip_html(text: str) -> str:
    """Remove HTML tags from a string and decode HTML entities."""
    return html.unescape(re.sub(r"<[^>]+>", "", text or "")).strip()


def escape_mrkdwn(text: str) -> str:
    """Escape the three characters Slack mrkdwn treats as markup."""
    # Decode first, or an API field already holding &amp; ends up &amp;amp;.
    text = html.unescape(text or "")
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# -- State persistence ---------------------------------------------------------

def load_posted_events() -> list:
    """Load the list of previously posted events from the JSON file."""
    if os.path.exists(POSTED_EVENTS_FILE):
        with open(POSTED_EVENTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_posted_events(events: list) -> None:
    """Write the updated list of posted events back to the JSON file."""
    with open(POSTED_EVENTS_FILE, "w", encoding="utf-8") as f:
        json.dump(events, f, indent=2, ensure_ascii=False)
    print(f"[state] Saved {len(events)} total posted events to {POSTED_EVENTS_FILE}")


# -- GDACS API -----------------------------------------------------------------

def fetch_gdacs_events(from_date: str, to_date: str) -> list:
    """Fetch all pages of orange and red GDACS alerts."""
    all_features = []
    page = 1

    while True:
        params = {
            "alertlevel": "Orange,Red",
            "fromdate": from_date,
            "todate": to_date,
            "eventtypes": "EQ,TC,FL,VO,DR,WF",
            "pagesize": 100,
            "pagenumber": page,
        }

        try:
            response = requests.get(GDACS_API_URL, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()
        except Exception as e:
            print(f"[error] GDACS API request failed (page {page}): {e}", file=sys.stderr)
            break

        features = data.get("features", [])
        if not features:
            break

        all_features.extend(features)
        print(f"[gdacs] Fetched page {page}: {len(features)} events")

        if len(features) < 100:
            break

        page += 1

    print(f"[gdacs] Total events fetched: {len(all_features)}")
    return all_features


def parse_event(feature: dict) -> dict:
    """Extract and normalise relevant fields from a GDACS GeoJSON feature."""
    props = feature.get("properties", {})

    description = props.get("description") or strip_html(props.get("htmldescription", ""))

    return {
        "event_id":     str(props.get("eventid", "")),
        "alert_level":  (props.get("alertlevel") or "").capitalize(),
        "event_name":   props.get("name") or props.get("eventname") or "Unknown event",
        "country":      props.get("country", "Unknown"),
        "description":  description,
        "fromdate":     props.get("fromdate", ""),
        "todate":       props.get("todate", ""),
        "datemodified": props.get("datemodified", ""),
        "event_url":    f"https://www.gdacs.org/report.aspx?eventtype={props.get('eventtype', '')}&eventid={props.get('eventid', '')}",
    }


# -- Slack ---------------------------------------------------------------------

def build_slack_payload(event: dict) -> dict:
    """Build a Slack Incoming Webhook payload."""
    level = event["alert_level"].lower()
    emoji = ":red_circle:" if level == "red" else ":large_orange_circle:"

    lines = [
        f"{emoji} *GDACS {event['alert_level']} Alert: {escape_mrkdwn(event['event_name'])}*",
        f"*Country:* {escape_mrkdwn(event['country'])}",
        f"*Event ID:* {escape_mrkdwn(event['event_id'])}",
    ]

    dates = []
    if event["fromdate"]:
        dates.append(f"*From:* {event['fromdate'][:10]}")
    if event["todate"]:
        dates.append(f"*To:* {event['todate'][:10]}")
    if dates:
        lines.append("   ".join(dates))
    if event["datemodified"]:
        lines.append(f"*Last updated:* {event['datemodified'][:10]}")

    description = escape_mrkdwn(event["description"]).strip()
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS].rstrip() + "..."
    if description:
        lines.append("")
        lines.append(description)

    lines.append("")
    lines.append(f"<{event['event_url']}|View the full GDACS report>")

    # Used for notifications when the attachment is not shown.
    return {
        "attachments": [
            {
                "fallback": f"GDACS {event['alert_level']} alert: {event['event_name']} ({event['country']})",
                "color": ALERT_COLOURS.get(level, "#CCCCCC"),
                "blocks": [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": "\n".join(lines)},
                    }
                ],
            }
        ],
    }


def post_to_slack(event: dict) -> None:
    """Post an alert to the Slack Incoming Webhook."""
    if not SLACK_WEBHOOK_URL:
        raise EnvironmentError("SLACK_WEBHOOK_URL environment variable is not set.")

    response = requests.post(SLACK_WEBHOOK_URL, json=build_slack_payload(event), timeout=15)
    if response.status_code != 200:
        raise RuntimeError(f"Slack returned {response.status_code}: {response.text.strip()}")


# -- Main ----------------------------------------------------------------------

def main() -> None:
    today = date.today().strftime("%Y-%m-%d")

    initial_run = (
        "--initial" in sys.argv
        or os.environ.get("INITIAL_RUN", "").lower() == "true"
    )

    if initial_run:
        print(f"[run] INITIAL RUN -- fetching all orange/red alerts from {INITIAL_FROM_DATE} to {today}")
    else:
        print(f"[run] Nightly run -- fetching all orange/red alerts from {INITIAL_FROM_DATE} to {today}")

    features = fetch_gdacs_events(INITIAL_FROM_DATE, today)

    posted_events = load_posted_events()
    posted_keys = {
        posted_key(e["event_id"], e["alert_level"])
        for e in posted_events
    }
    print(f"[state] Loaded {len(posted_events)} previously posted events")

    posted_count = 0
    skipped_count = 0
    error_count = 0

    for feature in features:
        event = parse_event(feature)

        if not event["event_id"]:
            continue
        if event["alert_level"].lower() not in ALERT_LEVELS:
            continue

        key = posted_key(event["event_id"], event["alert_level"])

        if key in posted_keys:
            skipped_count += 1
            continue

        try:
            post_to_slack(event)
            time.sleep(1)  # Slack allows about one message per second.

            record = {
                "event_id":     event["event_id"],
                "alert_level":  event["alert_level"],
                "event_name":   event["event_name"],
                "country":      event["country"],
                "fromdate":     event["fromdate"],
                "todate":       event["todate"],
                "datemodified": event["datemodified"],
                "posted_at":    date.today().isoformat(),
            }
            posted_events.append(record)
            posted_keys.add(key)
            posted_count += 1
            print(
                f"[posted] [{event['alert_level']}] "
                f"ID {event['event_id']} -- {event['event_name']} ({event['country']})"
            )

        except Exception as e:
            error_count += 1
            print(
                f"[error] Failed to post event {event['event_id']}: {e}",
                file=sys.stderr,
            )

    save_posted_events(posted_events)

    print(
        f"\n[done] Posted: {posted_count} | "
        f"Already seen (skipped): {skipped_count} | "
        f"Errors: {error_count}"
    )

    if error_count > 0:
        sys.exit(f"[error] {error_count} event(s) failed to post -- will retry on the next run")


if __name__ == "__main__":
    main()
