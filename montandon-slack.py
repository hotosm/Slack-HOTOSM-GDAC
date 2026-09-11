#!/usr/bin/env python3
"""Post significant disasters from the Montandon STAC API to Slack."""

import html
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

STATE_FILE = "posted_montandon.json"

STAC_API_URL = os.environ.get(
    "MONTANDON_STAC_URL", "https://montandon-eoapi-stage.ifrc.org/stac"
).rstrip("/")

API_TOKEN = os.environ.get("MONTANDON_API_TOKEN", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# Preview until the workflow is explicitly enabled.
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"

COLLECTIONS = [
    "gdacs-events", "pdc-events", "usgs-events", "glide-events",
    "emdat-events", "ifrcevent-events", "idmc-idu-events",
    "cems-events", "charter-events",
    "gdacs-hazards", "pdc-hazards", "usgs-hazards", "ibtracs-hazards",
    "glide-hazards", "ifrcevent-hazards",
    "gdacs-impacts", "pdc-impacts", "emdat-impacts",
    "idmc-idu-impacts", "ifrcevent-impacts",
]

PAGE_SIZE = 200
MAX_ITEMS_PER_COLLECTION = 3000

GDACS_ALERT_LEVELS = {"orange", "red"}

# Units are matched exactly: sources spell magnitude several ways ("mww", "mb"),
# and a substring test would read the "m" in "km/h" as a moment magnitude.
SEVERITY_TRIGGERS = [
    ({"m", "mw", "mww", "mwr", "mwc", "mwb", "mwp", "mb", "ms", "ml", "md"},
     6.0, "M{value:.1f} earthquake"),
    ({"knots", "knot", "kt"}, 64.0, "{value:.0f} kt winds"),
    ({"km/h", "kph"}, 119.0, "{value:.0f} km/h winds"),
]

IMPACT_TRIGGERS = {
    "death":              10,
    "missing":            10,
    "displaced_internal": 1000,
    "displaced_total":    1000,
    "displaced_external": 1000,
    "evacuated":          1000,
    "homeless":           1000,
    "affected_total":     10000,
}

IMPACT_DISPLAY_ORDER = [
    ("death", "Deaths"), ("missing", "Missing"), ("injured", "Injured"),
    ("displaced_internal", "Displaced"), ("displaced_total", "Displaced"),
    ("displaced_external", "Displaced (cross-border)"),
    ("evacuated", "Evacuated"), ("homeless", "Homeless"),
    ("relocated", "Relocated"), ("shelter_emergency", "In emergency shelter"),
    ("affected_total", "Affected"), ("destroyed", "Destroyed"),
    ("damaged", "Damaged"),
]

GDACS_LEVEL_NAMES = ("red", "orange", "green")
# Green events triggered by impact use the neutral colour.
ALERT_COLOURS = {"red": "#CC0000", "orange": "#FF9900"}
DEFAULT_COLOUR = "#4A90D9"

ALERT_EMOJI = {"red": ":red_circle:", "orange": ":large_orange_circle:"}
DEFAULT_EMOJI = ":large_blue_circle:"

MAX_DESCRIPTION_CHARS = 500

SOURCE_NAMES = {
    "gdacs": "GDACS", "pdc": "PDC", "usgs": "USGS", "glide": "GLIDE",
    "emdat": "EM-DAT", "idmc": "IDMC", "ibtracs": "IBTrACS", "cems": "Copernicus EMS",
    "charter": "Disasters Charter", "ifrcevent": "IFRC", "desinventar": "DesInventar",
    "gfd": "Global Flood Database", "alerthub": "AlertHub", "reference": "Montandon",
}

# Match at hazard-group level, e.g. both GH0101 and GH0102 are earthquakes.
HAZARD_LABELS = {
    "GH01": (":earth_americas:", "Earthquake"),
    "GH02": (":volcano:", "Volcanic activity"),
    "GH03": (":mountain:", "Landslide"),
    "MH03": (":cyclone:", "Cyclone"),
    "MH04": (":sun_with_face:", "Drought"),
    "MH05": (":thermometer:", "Extreme temperature"),
    "MH06": (":ocean:", "Flood"),
    "MH07": (":ocean:", "Tsunami / coastal"),
    "MH08": (":snowflake:", "Snow and ice"),
    "EN01": (":fire:", "Wildfire"),
    "BI01": (":microbe:", "Epidemic"),
    "SO02": (":warning:", "Conflict / unrest"),
    "TL00": (":warning:", "Technological"),
}
GLIDE_LABELS = {
    "EQ": (":earth_americas:", "Earthquake"), "VO": (":volcano:", "Volcanic activity"),
    "LS": (":mountain:", "Landslide"), "TC": (":cyclone:", "Cyclone"),
    "DR": (":sun_with_face:", "Drought"), "FL": (":ocean:", "Flood"),
    "TS": (":ocean:", "Tsunami"), "WF": (":fire:", "Wildfire"),
    "EP": (":microbe:", "Epidemic"), "ST": (":cloud_with_lightning:", "Storm"),
    "CW": (":snowflake:", "Cold wave"), "HT": (":thermometer:", "Heat wave"),
}

MERGE_RADIUS_KM = 500.0
MERGE_WINDOW_HOURS = 36

# Placeholder units do not represent a comparable severity reading.
NULL_SEVERITY_UNITS = {"glide", "count"}


def escape_mrkdwn(text: str) -> str:
    text = html.unescape(text or "")
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def strip_html(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()


def parse_dt(value: str):
    """Parse ISO 8601, treating a missing offset as UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def haversine_km(a: tuple, b: tuple) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def format_figure(figure: dict) -> str:
    return f"{'~' if figure['modelled'] else ''}{int(figure['value']):,}"


def country_name(code: str) -> str:
    return COUNTRY_NAMES.get(code, code)


def source_of(collection_id: str) -> str:
    prefix = (collection_id or "").split("-")[0]
    return SOURCE_NAMES.get(prefix, prefix.upper() or "Unknown")


def load_posted() -> list:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_posted(records: list) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
    print(f"[state] Saved {len(records)} total posted alerts to {STATE_FILE}")


def death_bucket(deaths: int) -> str:
    if deaths <= 0:
        return "0"
    return f"1e{int(math.log10(deaths))}"


def group_tokens(record: dict) -> set:
    return {record.get("key") or record.get("group_key", "")} | set(record.get("corr_ids") or [])


def state_signature(group: dict) -> str:
    """Repost when the alert level or death-toll magnitude changes."""
    return f"{group['alert_level'] or '-'}|{death_bucket(int(impact_value(group, 'death') or 0))}"


def index_posted(records: list) -> dict:
    index = {}
    for record in records:
        for token in group_tokens(record):
            if token:
                index.setdefault(token, set()).add(record.get("signature", ""))
    return index


def api_session() -> requests.Session:
    if not API_TOKEN:
        raise EnvironmentError(
            "MONTANDON_API_TOKEN is not set. Generate a token from your IFRC GO "
            "account settings (https://goadmin-stage.ifrc.org/)."
        )
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {API_TOKEN}",
        "Accept": "application/geo+json, application/json",
    })
    return session


def available_collections(session: requests.Session) -> set:
    """Return all collection IDs, including paginated results."""
    url, params = f"{STAC_API_URL}/collections", {"limit": 1000}
    collections = set()

    while url and len(collections) < 1000:
        response = session.get(url, params=params, timeout=60)
        response.raise_for_status()
        payload = response.json()
        collections |= {c["id"] for c in payload.get("collections", [])}

        nxt = next((l for l in payload.get("links", []) if l.get("rel") == "next"), None)
        url, params = (nxt or {}).get("href"), None

    return collections


def search_collection(session: requests.Session, collection: str, window: str) -> list:
    body = {"collections": [collection], "datetime": window, "limit": PAGE_SIZE}
    url = f"{STAC_API_URL}/search"
    items = []

    while True:
        try:
            response = session.post(url, json=body, timeout=120)
            response.raise_for_status()
            payload = response.json()
        except Exception as e:
            print(f"[error] {collection}: search failed: {e}", file=sys.stderr)
            break

        features = payload.get("features", [])
        items.extend(features)

        if len(items) >= MAX_ITEMS_PER_COLLECTION:
            print(f"[warn] {collection}: hit the {MAX_ITEMS_PER_COLLECTION}-item "
                  f"ceiling, narrow the window", file=sys.stderr)
            break

        # A STAC next link may replace or extend the POST body.
        nxt = next((l for l in payload.get("links", []) if l.get("rel") == "next"), None)
        if not nxt or not features:
            break
        url = nxt.get("href", url)
        if nxt.get("merge", False):
            body = {**body, **nxt.get("body", {})}
        else:
            body = nxt.get("body", body)

    print(f"[stac] {collection}: {len(items)} items")
    return items


UNDRR_CODE = re.compile(r"^[A-Z]{2}\d{4}$")
GLIDE_CODE = re.compile(r"^[A-Z]{2}$")

GDACS_LEVEL_IN_DESCRIPTION = re.compile(r"^\s*(green|orange|red)\b", re.IGNORECASE)
GDACS_LEVEL_IN_ICON = re.compile(r"/maps/(green|orange|red)/", re.IGNORECASE)


def alert_level_of(item: dict) -> str:
    """Read a GDACS colour from the structured field or legacy fallbacks."""
    props = item.get("properties", {})

    label = (props.get("monty:hazard_detail") or {}).get("severity_label", "")
    if isinstance(label, str) and label.lower() in GDACS_LEVEL_NAMES:
        return label.lower()

    match = GDACS_LEVEL_IN_DESCRIPTION.match(props.get("description") or "")
    if match:
        return match.group(1).lower()

    icon = (item.get("assets", {}).get("icon") or {}).get("href", "")
    match = GDACS_LEVEL_IN_ICON.search(icon)
    if match:
        return match.group(1).lower()

    return ""


def hazard_identity(hazard_codes: list) -> str:
    """Prefer a UNDRR code, falling back to a legacy GLIDE code."""
    codes = [c for c in (hazard_codes or []) if isinstance(c, str)]

    for code in codes:
        if UNDRR_CODE.match(code):
            return code[:4]
    for code in codes:
        if GLIDE_CODE.match(code) and code in GLIDE_LABELS:
            return code

    return codes[0] if codes else "UNKNOWN"


def label_for_code(hazard_code: str) -> tuple:
    return (HAZARD_LABELS.get(hazard_code)
            or GLIDE_LABELS.get(hazard_code)
            or (":warning:", "Hazard"))


def representative_point(item: dict):
    geometry = item.get("geometry") or {}
    if geometry.get("type") == "Point" and geometry.get("coordinates"):
        lon, lat = geometry["coordinates"][:2]
        return (lon, lat)

    bbox = item.get("bbox")
    if bbox and len(bbox) >= 4:
        return ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)

    return None


def parse_item(item: dict) -> dict:
    props = item.get("properties", {})
    collection = item.get("collection") or ""
    roles = props.get("roles") or []

    hazard_code = hazard_identity(props.get("monty:hazard_codes"))
    when = parse_dt(props.get("datetime")) or parse_dt(props.get("start_datetime"))

    assets = item.get("assets") or {}
    report = (assets.get("report") or {}).get("href", "")
    if not report:
        report = next((l.get("href", "") for l in item.get("links", [])
                       if l.get("rel") == "via" and l.get("type") == "text/html"), "")

    return {
        "id": item.get("id", ""),
        "collection": collection,
        "source": source_of(collection),
        "role": "impact" if "impact" in roles else ("hazard" if "hazard" in roles else "event"),
        "corr_id": props.get("monty:corr_id") or "",
        "title": (props.get("title") or "").strip(),
        "description": strip_html(props.get("description") or ""),
        "datetime": when,
        "countries": [c for c in (props.get("monty:country_codes") or []) if c],
        "hazard_code": hazard_code,
        "alert_level": alert_level_of(item),
        "hazard_detail": props.get("monty:hazard_detail") or {},
        "impact_detail": props.get("monty:impact_detail") or {},
        "point": representative_point(item),
        "report_url": report,
        "magnitude": props.get("eq:magnitude"),
    }


def group_items(items: list) -> list:
    """Group by corr_id, which can vary by source, then merge by place and time."""
    buckets = {}
    for item in items:
        key = item["corr_id"] or f"{item['collection']}:{item['id']}"
        buckets.setdefault(key, []).append(item)

    by_hazard = {}
    for members in buckets.values():
        hazard = next((m["hazard_code"] for m in members if m["hazard_code"] != "UNKNOWN"),
                      "UNKNOWN")
        candidate = {
            "hazard": hazard,
            "members": list(members),
            "countries": {c for m in members for c in m["countries"]},
            "points": [m["point"] for m in members if m["point"]],
            "dates": [m["datetime"] for m in members if m["datetime"]],
        }
        for existing in by_hazard.setdefault(hazard, []):
            if same_disaster(existing, candidate):
                existing["members"].extend(candidate["members"])
                existing["countries"] |= candidate["countries"]
                existing["points"].extend(candidate["points"])
                existing["dates"].extend(candidate["dates"])
                break
        else:
            by_hazard[hazard].append(candidate)

    return [summarise(g) for groups in by_hazard.values() for g in groups]


def same_disaster(a: dict, b: dict) -> bool:
    """Match available country, time and location data; missing fields are neutral."""
    if a["countries"] and b["countries"] and not (a["countries"] & b["countries"]):
        return False

    if a["dates"] and b["dates"]:
        if abs((min(a["dates"]) - min(b["dates"])).total_seconds()) > MERGE_WINDOW_HOURS * 3600:
            return False

    if a["points"] and b["points"]:
        if not any(haversine_km(p, q) <= MERGE_RADIUS_KM
                   for p in a["points"] for q in b["points"]):
            return False

    return True


def merge_impacts(impacts: list) -> dict:
    """Sum within a source, take the maximum across sources, and prefer observed data."""
    per_source = {}
    for m in impacts:
        detail = m["impact_detail"]
        kind, value = detail.get("type"), detail.get("value")
        estimate = detail.get("estimate_type") or "primary"
        if not kind or not isinstance(value, (int, float)):
            continue
        bucket = per_source.setdefault((kind, estimate), {})
        bucket[m["source"]] = bucket.get(m["source"], 0) + value

    totals = {}
    for kind in {k for k, _ in per_source}:
        observed = [max(per_source[(kind, e)].values())
                    for e in ("primary", "secondary") if (kind, e) in per_source]
        if observed:
            totals[kind] = {"value": max(observed), "modelled": False}
        elif (kind, "modelled") in per_source:
            totals[kind] = {"value": max(per_source[(kind, "modelled")].values()),
                            "modelled": True}

    return totals


def impact_value(group: dict, kind: str):
    return (group["impacts"].get(kind) or {}).get("value")


def summarise(group: dict) -> dict:
    members = group["members"]
    countries = sorted(group["countries"])
    emoji, hazard_label = label_for_code(group["hazard"])

    events = [m for m in members if m["role"] == "event"]
    hazards = [m for m in members if m["role"] == "hazard"]
    impacts = [m for m in members if m["role"] == "impact"]
    ranked = sorted(events or members,
                    key=lambda m: (m["source"] != "GDACS", not m["title"]))
    lead = ranked[0]

    levels = [m["alert_level"] for m in members if m["alert_level"]]
    alert_level = next((c for c in GDACS_LEVEL_NAMES if c in levels), "")

    severities = {}
    for m in hazards + events:
        detail = m["hazard_detail"]
        value, unit = detail.get("severity_value"), (detail.get("severity_unit") or "")
        if isinstance(value, (int, float)) and unit and value and unit not in NULL_SEVERITY_UNITS:
            severities[unit] = max(severities.get(unit, value), value)
        if isinstance(m["magnitude"], (int, float)):
            severities["mww"] = max(severities.get("mww", m["magnitude"]), m["magnitude"])

    dates = group["dates"]
    day = min(dates).strftime("%Y-%m-%d") if dates else "unknown"
    descriptions = sorted((m["description"] for m in events if m["description"]),
                          key=len, reverse=True)
    reports = {m["source"]: m["report_url"] for m in members if m["report_url"]}
    # The reference collection correlates data; it is not a reporting source.
    sources = sorted({m["source"] for m in members} - {SOURCE_NAMES["reference"]})

    return {
        "key": f"{day}|{'+'.join(countries) or 'unknown'}|{group['hazard']}",
        "corr_ids": sorted({m["corr_id"] for m in members if m["corr_id"]}),
        "title": lead["title"] or lead["description"][:80] or "Unnamed event",
        "emoji": emoji,
        "hazard_label": hazard_label,
        "countries": countries,
        "alert_level": alert_level,
        "severities": severities,
        "impacts": merge_impacts(impacts),
        "sources": sources or [lead["source"]],
        "start": min(dates) if dates else None,
        "end": max(dates) if dates else None,
        "description": descriptions[0] if descriptions else "",
        "reports": reports,
        "item_count": len(members),
    }


def triggers(group: dict) -> list:
    reasons = []

    if group["alert_level"] in GDACS_ALERT_LEVELS:
        reasons.append(f"GDACS {group['alert_level'].capitalize()} alert")

    for units, minimum, template in SEVERITY_TRIGGERS:
        for unit, value in group["severities"].items():
            if unit.lower().strip() in units and value >= minimum:
                reasons.append(template.format(value=value))
                break

    for kind, minimum in IMPACT_TRIGGERS.items():
        figure = group["impacts"].get(kind)
        if figure and figure["value"] >= minimum:
            label = dict(IMPACT_DISPLAY_ORDER).get(kind, kind.replace("_", " ")).lower()
            qualifier = " (modelled)" if figure["modelled"] else ""
            reasons.append(f"{format_figure(figure)} {label}{qualifier}")

    return list(dict.fromkeys(reasons))


def build_slack_payload(group: dict, reasons: list, is_update: bool) -> dict:
    level = group["alert_level"]
    emoji = ALERT_EMOJI.get(level, DEFAULT_EMOJI)
    prefix = "Update" if is_update else "Alert"
    heading = f"{group['hazard_label']} {prefix}"
    if level in GDACS_ALERT_LEVELS:
        heading = f"{level.capitalize()} {heading}"

    lines = [
        f"{emoji} *{heading}: {escape_mrkdwn(group['title'])}*",
        f"{group['emoji']} *Hazard:* {group['hazard_label']}",
    ]

    if group["countries"]:
        names = ", ".join(country_name(c) for c in group["countries"])
        lines.append(f"*Countries:* {escape_mrkdwn(names)}")

    dates = []
    if group["start"]:
        dates.append(f"*From:* {group['start'].strftime('%Y-%m-%d')}")
    if group["start"] and group["end"] and group["end"].date() != group["start"].date():
        dates.append(f"*To:* {group['end'].strftime('%Y-%m-%d')}")
    if dates:
        lines.append("   ".join(dates))

    lines.append(f"*Why this fired:* {escape_mrkdwn('; '.join(reasons))}")

    observed, modelled = [], []
    seen_labels = set()
    for kind, label in IMPACT_DISPLAY_ORDER:
        figure = group["impacts"].get(kind)
        if not figure or label in seen_labels:
            continue
        (modelled if figure["modelled"] else observed).append(
            f"*{label}:* {format_figure(figure)}")
        seen_labels.add(label)
    if observed:
        lines.append("")
        lines.append("*Reported impact*")
        lines.append("   ".join(observed))
    if modelled:
        lines.append("")
        lines.append("*Modelled estimate* (not a reported figure)")
        lines.append("   ".join(modelled))

    severity_bits = [f"{value:g} {unit}" for unit, value in sorted(group["severities"].items())]
    if severity_bits:
        lines.append(f"*Severity:* {escape_mrkdwn(', '.join(severity_bits))}")

    description = escape_mrkdwn(group["description"]).strip()
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS].rstrip() + "..."
    if description:
        lines.append("")
        lines.append(description)

    lines.append("")
    record_word = "record" if group["item_count"] == 1 else "records"
    lines.append(f"*Reported by:* {escape_mrkdwn(', '.join(group['sources']))} "
                 f"({group['item_count']} {record_word} via IFRC Montandon)")

    links = [f"<{url}|{source} report>" for source, url in sorted(group["reports"].items())]
    if links:
        lines.append(" | ".join(links))

    countries_fallback = ", ".join(country_name(c) for c in group["countries"]) or "unknown"
    return {
        "attachments": [
            {
                "fallback": f"{group['hazard_label']} alert: {group['title']} ({countries_fallback})",
                "color": ALERT_COLOURS.get(level, DEFAULT_COLOUR),
                "blocks": [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": "\n".join(lines)},
                    }
                ],
            }
        ],
    }


def post_to_slack(payload: dict) -> bool:
    if DRY_RUN:
        print("--- DRY RUN: would post ---")
        print(payload["attachments"][0]["blocks"][0]["text"]["text"])
        print("---------------------------")
        return True

    if not SLACK_WEBHOOK_URL:
        raise EnvironmentError("SLACK_WEBHOOK_URL environment variable is not set.")

    try:
        response = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=15)
    except requests.RequestException as e:
        print(f"[error] Slack request failed: {e}", file=sys.stderr)
        return False

    if response.status_code != 200:
        print(f"[error] Slack returned {response.status_code}: {response.text.strip()}",
              file=sys.stderr)
        return False
    return True


def resolve_window() -> str:
    now = datetime.now(timezone.utc)

    start_date = os.environ.get("START_DATE", "").strip()
    lookback = os.environ.get("LOOKBACK_DAYS", "").strip()

    try:
        if start_date:
            start = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
        else:
            start = now - timedelta(days=int(lookback) if lookback else 7)
    except ValueError:
        sys.exit(f"[error] Bad window: START_DATE={start_date!r} must be a date "
                 f"like 2026-01-01, LOOKBACK_DAYS={lookback!r} must be a whole number")

    return f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')}/{now.strftime('%Y-%m-%dT%H:%M:%SZ')}"


def main() -> None:
    window = resolve_window()
    print(f"[run] {'DRY RUN -- ' if DRY_RUN else ''}querying Montandon over {window}")
    print(f"[run] Endpoint: {STAC_API_URL}")

    session = api_session()

    try:
        served = available_collections(session)
    except Exception as e:
        sys.exit(f"[error] Could not list Montandon collections: {e}")

    wanted = [c for c in COLLECTIONS if c in served]
    missing = [c for c in COLLECTIONS if c not in served]
    if missing:
        print(f"[stac] Not served by this endpoint, skipping: {', '.join(missing)}")

    raw_items = []
    for collection in wanted:
        raw_items.extend(search_collection(session, collection, window))
    print(f"[stac] {len(raw_items)} items across {len(wanted)} collections")

    groups = group_items([parse_item(i) for i in raw_items])
    print(f"[group] Correlated into {len(groups)} distinct events")

    posted = load_posted()
    seen = index_posted(posted)
    print(f"[state] Loaded {len(posted)} previously posted alerts")

    severity_rank = {"red": 0, "orange": 1, "green": 2, "": 3}
    groups.sort(key=lambda g: (severity_rank.get(g["alert_level"], 3),
                               -(impact_value(g, "death") or 0),
                               -(g["start"].timestamp() if g["start"] else 0)))

    posted_count = skipped_count = quiet_count = error_count = 0

    for group in groups:
        reasons = triggers(group)
        if not reasons:
            quiet_count += 1
            continue

        signature = state_signature(group)
        tokens = group_tokens(group)
        if any(signature in seen.get(token, ()) for token in tokens):
            skipped_count += 1
            continue

        is_update = any(token in seen for token in tokens)

        if post_to_slack(build_slack_payload(group, reasons, is_update)):
            posted.append({
                "group_key":   group["key"],
                "corr_ids":    group["corr_ids"],
                "signature":   signature,
                "title":       group["title"],
                "hazard":      group["hazard_label"],
                "countries":   group["countries"],
                "alert_level": group["alert_level"],
                "sources":     group["sources"],
                "reasons":     reasons,
                "posted_at":   datetime.now(timezone.utc).date().isoformat(),
            })
            for token in tokens:
                if token:
                    seen.setdefault(token, set()).add(signature)
            posted_count += 1
            print(f"[posted] {'[update] ' if is_update else ''}{group['title']} "
                  f"({', '.join(group['sources'])}) -- {'; '.join(reasons)}")
        else:
            error_count += 1
            print(f"[error] Failed to post {group['key']} -- will retry on the next run",
                  file=sys.stderr)

        time.sleep(1)  # Respect Slack's one-message-per-second limit.

    if DRY_RUN:
        print(f"\n[done] DRY RUN -- would post: {posted_count} | "
              f"already seen: {skipped_count} | below threshold: {quiet_count} "
              f"(state not saved)")
        return

    save_posted(posted)
    print(f"\n[done] Posted: {posted_count} | Already seen: {skipped_count} | "
          f"Below threshold: {quiet_count} | Errors: {error_count}")

    if error_count:
        sys.exit(f"[error] {error_count} alert(s) failed to post -- will retry next run")


COUNTRY_NAMES = {
    "ABW": "Aruba", "AFG": "Afghanistan", "AGO": "Angola", "AIA": "Anguilla",
    "ALA": "Åland Islands", "ALB": "Albania", "AND": "Andorra", "ARE": "United Arab Emirates",
    "ARG": "Argentina", "ARM": "Armenia", "ASM": "American Samoa", "ATA": "Antarctica",
    "ATF": "French Southern Territories", "ATG": "Antigua and Barbuda", "AUS": "Australia",
    "AUT": "Austria", "AZE": "Azerbaijan", "BDI": "Burundi", "BEL": "Belgium", "BEN": "Benin",
    "BES": "Bonaire and Saba", "BFA": "Burkina Faso", "BGD": "Bangladesh", "BGR": "Bulgaria",
    "BHR": "Bahrain", "BHS": "Bahamas", "BIH": "Bosnia and Herzegovina",
    "BLM": "Saint Barthélemy", "BLR": "Belarus", "BLZ": "Belize", "BMU": "Bermuda",
    "BOL": "Bolivia", "BRA": "Brazil", "BRB": "Barbados", "BRN": "Brunei", "BTN": "Bhutan",
    "BVT": "Bouvet Island", "BWA": "Botswana", "CAF": "Central African Republic",
    "CAN": "Canada", "CCK": "Cocos Islands", "CHE": "Switzerland", "CHL": "Chile",
    "CHN": "China", "CIV": "Cote d'Ivoire", "CMR": "Cameroon", "COD": "DR Congo",
    "COG": "Republic of the Congo", "COK": "Cook Islands", "COL": "Colombia", "COM": "Comoros",
    "CPV": "Cabo Verde", "CRI": "Costa Rica", "CUB": "Cuba", "CUW": "Curaçao",
    "CXR": "Christmas Island", "CYM": "Cayman Islands", "CYP": "Cyprus", "CZE": "Czechia",
    "DEU": "Germany", "DJI": "Djibouti", "DMA": "Dominica", "DNK": "Denmark",
    "DOM": "Dominican Republic", "DZA": "Algeria", "ECU": "Ecuador", "EGY": "Egypt",
    "ERI": "Eritrea", "ESH": "Western Sahara", "ESP": "Spain", "EST": "Estonia",
    "ETH": "Ethiopia", "FIN": "Finland", "FJI": "Fiji", "FLK": "Falkland Islands",
    "FRA": "France", "FRO": "Faroe Islands", "FSM": "Micronesia", "GAB": "Gabon",
    "GBR": "United Kingdom", "GEO": "Georgia", "GGY": "Guernsey", "GHA": "Ghana",
    "GIB": "Gibraltar", "GIN": "Guinea", "GLP": "Guadeloupe", "GMB": "Gambia",
    "GNB": "Guinea-Bissau", "GNQ": "Equatorial Guinea", "GRC": "Greece", "GRD": "Grenada",
    "GRL": "Greenland", "GTM": "Guatemala", "GUF": "French Guiana", "GUM": "Guam",
    "GUY": "Guyana", "HKG": "Hong Kong", "HMD": "Heard and McDonald Islands", "HND": "Honduras",
    "HRV": "Croatia", "HTI": "Haiti", "HUN": "Hungary", "IDN": "Indonesia",
    "IMN": "Isle of Man", "IND": "India", "IOT": "British Indian Ocean Territory",
    "IRL": "Ireland", "IRN": "Iran", "IRQ": "Iraq", "ISL": "Iceland", "ISR": "Israel",
    "ITA": "Italy", "JAM": "Jamaica", "JEY": "Jersey", "JOR": "Jordan", "JPN": "Japan",
    "KAZ": "Kazakhstan", "KEN": "Kenya", "KGZ": "Kyrgyzstan", "KHM": "Cambodia",
    "KIR": "Kiribati", "KNA": "Saint Kitts and Nevis", "KOR": "South Korea", "KWT": "Kuwait",
    "LAO": "Laos", "LBN": "Lebanon", "LBR": "Liberia", "LBY": "Libya", "LCA": "Saint Lucia",
    "LIE": "Liechtenstein", "LKA": "Sri Lanka", "LSO": "Lesotho", "LTU": "Lithuania",
    "LUX": "Luxembourg", "LVA": "Latvia", "MAC": "Macao", "MAF": "Saint Martin (French part)",
    "MAR": "Morocco", "MCO": "Monaco", "MDA": "Moldova", "MDG": "Madagascar", "MDV": "Maldives",
    "MEX": "Mexico", "MHL": "Marshall Islands", "MKD": "North Macedonia", "MLI": "Mali",
    "MLT": "Malta", "MMR": "Myanmar", "MNE": "Montenegro", "MNG": "Mongolia",
    "MNP": "Northern Mariana Islands", "MOZ": "Mozambique", "MRT": "Mauritania",
    "MSR": "Montserrat", "MTQ": "Martinique", "MUS": "Mauritius", "MWI": "Malawi",
    "MYS": "Malaysia", "MYT": "Mayotte", "NAM": "Namibia", "NCL": "New Caledonia",
    "NER": "Niger", "NFK": "Norfolk Island", "NGA": "Nigeria", "NIC": "Nicaragua",
    "NIU": "Niue", "NLD": "Netherlands", "NOR": "Norway", "NPL": "Nepal", "NRU": "Nauru",
    "NZL": "New Zealand", "OMN": "Oman", "PAK": "Pakistan", "PAN": "Panama", "PCN": "Pitcairn",
    "PER": "Peru", "PHL": "Philippines", "PLW": "Palau", "PNG": "Papua New Guinea",
    "POL": "Poland", "PRI": "Puerto Rico", "PRK": "North Korea", "PRT": "Portugal",
    "PRY": "Paraguay", "PSE": "Palestine", "PYF": "French Polynesia", "QAT": "Qatar",
    "REU": "Réunion", "ROU": "Romania", "RUS": "Russia", "RWA": "Rwanda", "SAU": "Saudi Arabia",
    "SDN": "Sudan", "SEN": "Senegal", "SGP": "Singapore", "SGS": "South Georgia",
    "SHN": "Saint Helena", "SJM": "Svalbard and Jan Mayen", "SLB": "Solomon Islands",
    "SLE": "Sierra Leone", "SLV": "El Salvador", "SMR": "San Marino", "SOM": "Somalia",
    "SPM": "Saint Pierre and Miquelon", "SRB": "Serbia", "SSD": "South Sudan",
    "STP": "Sao Tome and Principe", "SUR": "Suriname", "SVK": "Slovakia", "SVN": "Slovenia",
    "SWE": "Sweden", "SWZ": "Eswatini", "SXM": "Sint Maarten (Dutch part)", "SYC": "Seychelles",
    "SYR": "Syria", "TCA": "Turks and Caicos Islands", "TCD": "Chad", "TGO": "Togo",
    "THA": "Thailand", "TJK": "Tajikistan", "TKL": "Tokelau", "TKM": "Turkmenistan",
    "TLS": "Timor-Leste", "TON": "Tonga", "TTO": "Trinidad and Tobago", "TUN": "Tunisia",
    "TUR": "Türkiye", "TUV": "Tuvalu", "TWN": "Taiwan", "TZA": "Tanzania", "UGA": "Uganda",
    "UKR": "Ukraine", "UMI": "US Minor Outlying Islands", "URY": "Uruguay",
    "USA": "United States", "UZB": "Uzbekistan", "VAT": "Vatican City",
    "VCT": "Saint Vincent and the Grenadines", "VEN": "Venezuela",
    "VGB": "British Virgin Islands", "VIR": "US Virgin Islands", "VNM": "Vietnam",
    "VUT": "Vanuatu", "WLF": "Wallis and Futuna", "WSM": "Samoa", "YEM": "Yemen",
    "ZAF": "South Africa", "ZMB": "Zambia", "ZWE": "Zimbabwe"
}


if __name__ == "__main__":
    main()
