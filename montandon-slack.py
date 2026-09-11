#!/usr/bin/env python3
"""
Montandon Alert Notifier for HOTOSM Slack
-----------------------------------------
Queries the IFRC Montandon STAC API and posts significant new disasters to Slack
via a standard Incoming Webhook. Montandon already merges GDACS, PDC, USGS,
GLIDE, EM-DAT, IDMC and others behind one endpoint, so this script is intended
to eventually replace gdacs-slack.py rather than sit next to it.

How it works
  1. Fetch every event, hazard and impact item in the lookback window from the
     source collections listed in COLLECTIONS.
  2. Correlate them into one group per real-world disaster (see group_key), so a
     flood reported by GDACS, GLIDE and EM-DAT produces a single Slack message
     naming all three sources.
  3. Decide whether a group is worth posting using TRIGGERS -- a GDACS Orange or
     Red alert level, a per-hazard severity threshold, or a reported impact
     (deaths, displacement, people affected) above a floor.
  4. Post the survivors and record them in posted_montandon.json, which is
     committed back to the repo after each run.

Deduplication key: (group_key, alert_level, death_toll_bucket)
  - A newly triggered disaster is always posted.
  - An escalation in GDACS alert level reposts (Orange -> Red), as does a death
    toll crossing into a new order of magnitude, so a developing event gets a
    follow-up rather than going quiet.
  - An otherwise unchanged group is skipped.

Environment
  MONTANDON_API_TOKEN  required -- IFRC GO platform bearer token
  SLACK_WEBHOOK_URL    required unless DRY_RUN
  DRY_RUN              'true' (default) prints payloads instead of posting
  LOOKBACK_DAYS        rolling window, default 7
  START_DATE           e.g. 2026-01-01, overrides LOOKBACK_DAYS
  MONTANDON_STAC_URL   override the API root (defaults to the staging endpoint)
"""

import html
import json
import math
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone

import requests

# -- Configuration -------------------------------------------------------------

STATE_FILE = "posted_montandon.json"

STAC_API_URL = os.environ.get(
    "MONTANDON_STAC_URL", "https://montandon-eoapi-stage.ifrc.org/stac"
).rstrip("/")

API_TOKEN = os.environ.get("MONTANDON_API_TOKEN", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# Default to a preview so a misconfigured run cannot spam the channel.
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"

# Collections to sweep. Anything the API does not currently serve is dropped at
# startup, so new sources can be added here before their ETL goes live.
COLLECTIONS = [
    # Events -- the disaster occurrence itself.
    "gdacs-events", "pdc-events", "usgs-events", "glide-events",
    "emdat-events", "ifrcevent-events", "idmc-idu-events",
    "cems-events", "charter-events",
    # Hazards -- physical severity (GDACS alert level, magnitude, wind speed).
    "gdacs-hazards", "pdc-hazards", "usgs-hazards", "ibtracs-hazards",
    "glide-hazards", "ifrcevent-hazards",
    # Impacts -- deaths, displacement, people affected.
    "gdacs-impacts", "pdc-impacts", "emdat-impacts",
    "idmc-idu-impacts", "ifrcevent-impacts",
]

# Per-page size and a safety ceiling, in case a window pulls a backfill.
PAGE_SIZE = 200
MAX_ITEMS_PER_COLLECTION = 3000

# -- What counts as alert-worthy -----------------------------------------------

# GDACS is the only source carrying a traffic-light alert level.
GDACS_ALERT_LEVELS = {"orange", "red"}

# Severity floors, matched against a lowercased monty:hazard_detail.severity_unit.
# Units are matched exactly: sources spell magnitude several ways ("mww", "mb"),
# and a substring test would read the "m" in "km/h" as a moment magnitude.
SEVERITY_TRIGGERS = [
    # (accepted units, minimum value, label template)
    ({"m", "mw", "mww", "mwr", "mwc", "mwb", "mwp", "mb", "ms", "ml", "md"},
     6.0, "M{value:.1f} earthquake"),
    ({"knots", "knot", "kt"}, 64.0, "{value:.0f} kt winds"),      # hurricane force
    ({"km/h", "kph"}, 119.0, "{value:.0f} km/h winds"),           # Saffir-Simpson 1
]

# Impact floors. A group clears the bar if any one of these is met.
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

# Impact types worth showing in the message even when they did not trigger it.
IMPACT_DISPLAY_ORDER = [
    ("death", "Deaths"), ("missing", "Missing"), ("injured", "Injured"),
    ("displaced_internal", "Displaced"), ("displaced_total", "Displaced"),
    ("displaced_external", "Displaced (cross-border)"),
    ("evacuated", "Evacuated"), ("homeless", "Homeless"),
    ("relocated", "Relocated"), ("shelter_emergency", "In emergency shelter"),
    ("affected_total", "Affected"), ("destroyed", "Destroyed"),
    ("damaged", "Damaged"),
]

# -- Presentation --------------------------------------------------------------

GDACS_LEVEL_NAMES = ("red", "orange", "green")
# Green is deliberately absent: a Green event that fires on its impact figures
# should not be painted all-clear. It falls through to the neutral colour.
ALERT_COLOURS = {"red": "#CC0000", "orange": "#FF9900"}
DEFAULT_COLOUR = "#4A90D9"  # No alerting GDACS level -- fired on severity or impact.

ALERT_EMOJI = {"red": ":red_circle:", "orange": ":large_orange_circle:"}
DEFAULT_EMOJI = ":large_blue_circle:"

MAX_DESCRIPTION_CHARS = 500

# Human-readable names for the source prefix of a collection id.
SOURCE_NAMES = {
    "gdacs": "GDACS", "pdc": "PDC", "usgs": "USGS", "glide": "GLIDE",
    "emdat": "EM-DAT", "idmc": "IDMC", "ibtracs": "IBTrACS", "cems": "Copernicus EMS",
    "charter": "Disasters Charter", "ifrcevent": "IFRC", "desinventar": "DesInventar",
    "gfd": "Global Flood Database", "alerthub": "AlertHub", "reference": "Montandon",
}

# UNDRR-ISC 2025 hazard codes -> label and emoji. Keyed on the two-letter family
# plus the two-digit group, so e.g. GH0101 and GH0102 both read as Earthquake.
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
# Legacy GLIDE codes, used when no UNDRR-ISC code is present on the item.
GLIDE_LABELS = {
    "EQ": (":earth_americas:", "Earthquake"), "VO": (":volcano:", "Volcanic activity"),
    "LS": (":mountain:", "Landslide"), "TC": (":cyclone:", "Cyclone"),
    "DR": (":sun_with_face:", "Drought"), "FL": (":ocean:", "Flood"),
    "TS": (":ocean:", "Tsunami"), "WF": (":fire:", "Wildfire"),
    "EP": (":microbe:", "Epidemic"), "ST": (":cloud_with_lightning:", "Storm"),
    "CW": (":snowflake:", "Cold wave"), "HT": (":thermometer:", "Heat wave"),
}

# How far apart in space and time two reports of the same hazard can be and
# still be treated as one disaster. See same_disaster().
MERGE_RADIUS_KM = 500.0
MERGE_WINDOW_HOURS = 36

# Units that carry no reading -- GLIDE files a severity of 0 in unit "glide" on
# every hazard, which is an absence of data rather than a measurement of zero.
# "count" joins them because a bare count means nothing without the label that
# gives it a subject, and that label is not a queryable field.
NULL_SEVERITY_UNITS = {"glide", "count"}


# -- Small helpers -------------------------------------------------------------

def escape_mrkdwn(text: str) -> str:
    """Escape the three characters Slack mrkdwn treats as markup."""
    # Decode first, or an API field already holding &amp; ends up &amp;amp;.
    text = html.unescape(text or "")
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def strip_html(text: str) -> str:
    """Remove HTML tags from a string and collapse whitespace."""
    if not text:
        return ""
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()


def parse_dt(value: str):
    """Parse an ISO 8601 timestamp as UTC, tolerating the trailing Z form.

    Sources are inconsistent about including an offset at all, and comparing a
    naive datetime against an aware one raises, so a missing offset is read as
    UTC rather than left to blow up correlation later.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def haversine_km(a: tuple, b: tuple) -> float:
    """Great-circle distance in kilometres between two (lon, lat) points."""
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def format_figure(figure: dict) -> str:
    """Render an impact figure, marking modelled estimates with a tilde."""
    return f"{'~' if figure['modelled'] else ''}{int(figure['value']):,}"


def country_name(code: str) -> str:
    return COUNTRY_NAMES.get(code, code)


def source_of(collection_id: str) -> str:
    """'gdacs-events' -> 'GDACS'."""
    prefix = (collection_id or "").split("-")[0]
    return SOURCE_NAMES.get(prefix, prefix.upper() or "Unknown")


# -- State persistence ---------------------------------------------------------

def load_posted() -> list:
    """Load the list of previously posted alerts from the JSON file."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_posted(records: list) -> None:
    """Write the updated list of posted alerts back to the JSON file."""
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
    print(f"[state] Saved {len(records)} total posted alerts to {STATE_FILE}")


def death_bucket(deaths: int) -> str:
    """Coarse order-of-magnitude bucket, so a rising toll reposts once per decade."""
    if deaths <= 0:
        return "0"
    return f"1e{int(math.log10(deaths))}"


def group_tokens(record: dict) -> set:
    """Every identifier a disaster can be recognised by across runs."""
    return {record.get("key") or record.get("group_key", "")} | set(record.get("corr_ids") or [])


def state_signature(group: dict) -> str:
    """What has been said about a disaster: its alert level and toll magnitude.

    A repeat run producing the same signature is silent; an escalation from
    Orange to Red, or a death toll crossing an order of magnitude, is a new
    signature and posts a follow-up.
    """
    return f"{group['alert_level'] or '-'}|{death_bucket(int(impact_value(group, 'death') or 0))}"


def index_posted(records: list) -> dict:
    """token -> signatures already posted under it."""
    index = {}
    for record in records:
        for token in group_tokens(record):
            if token:
                index.setdefault(token, set()).add(record.get("signature", ""))
    return index


# -- Montandon STAC API --------------------------------------------------------

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
    """Collection ids the API currently serves, so we never ask for a dead one.

    Paged through rather than read from one response: the endpoint applies a
    default page size, and quietly dropping gdacs-events off the end of page one
    would silence the alerts this script exists to send.
    """
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
    """Fetch every item in one collection over the datetime window, following
    the STAC `next` links until the API stops paging."""
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

        # stac-fastapi returns the next page as a POST link carrying a body to
        # merge into the current one (usually just a token).
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


# -- Item parsing --------------------------------------------------------------

UNDRR_CODE = re.compile(r"^[A-Z]{2}\d{4}$")
GLIDE_CODE = re.compile(r"^[A-Z]{2}$")

# GDACS puts the alert level at the front of the description, e.g.
# "Red Flood in Spain from: 27 Oct 2024 15 to: 04 Nov 2024 11."
GDACS_LEVEL_IN_DESCRIPTION = re.compile(r"^\s*(green|orange|red)\b", re.IGNORECASE)
# ...and in the icon asset path, e.g. .../gdacs_icons/maps/Red/FL.png
GDACS_LEVEL_IN_ICON = re.compile(r"/maps/(green|orange|red)/", re.IGNORECASE)


def alert_level_of(item: dict) -> str:
    """GDACS traffic-light level for an item, or '' if it does not carry one.

    severity_label is the authoritative field but is only populated on GDACS
    hazard items, and other sources reuse it for unrelated labels ("Area
    radius"), so a value is only accepted when it is an actual GDACS colour.
    """
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
    """Pick the canonical hazard code for an item.

    The UNDRR-ISC 2025 code is the Monty reference classification, so it wins;
    the GLIDE two-letter code is the fallback for items that predate it.
    """
    codes = [c for c in (hazard_codes or []) if isinstance(c, str)]

    for code in codes:
        if UNDRR_CODE.match(code):
            return code[:4]
    for code in codes:
        if GLIDE_CODE.match(code) and code in GLIDE_LABELS:
            return code

    return codes[0] if codes else "UNKNOWN"


def label_for_code(hazard_code: str) -> tuple:
    """(emoji, label) presentation for a canonical hazard code."""
    return (HAZARD_LABELS.get(hazard_code)
            or GLIDE_LABELS.get(hazard_code)
            or (":warning:", "Hazard"))


def representative_point(item: dict):
    """A single (lon, lat) for an item, from a Point geometry or the bbox centre."""
    geometry = item.get("geometry") or {}
    if geometry.get("type") == "Point" and geometry.get("coordinates"):
        lon, lat = geometry["coordinates"][:2]
        return (lon, lat)

    bbox = item.get("bbox")
    if bbox and len(bbox) >= 4:
        return ((bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2)

    return None


def parse_item(item: dict) -> dict:
    """Flatten one STAC item into the fields this script cares about."""
    props = item.get("properties", {})
    collection = item.get("collection") or ""
    roles = props.get("roles") or []

    hazard_code = hazard_identity(props.get("monty:hazard_codes"))
    when = parse_dt(props.get("datetime")) or parse_dt(props.get("start_datetime"))

    assets = item.get("assets") or {}
    report = (assets.get("report") or {}).get("href", "")
    if not report:
        # Some transformers record the upstream page as a `via` link instead.
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


# -- Correlation ---------------------------------------------------------------
def group_items(items: list) -> list:
    """Correlate items into one group per real-world disaster.

    monty:corr_id is the intended join key, but on the staging API it varies by
    episode and by which hazard classification the transformer happened to use
    (20241113-ESP-MH0600-1-GCDB vs 20241113-ESP-NAT-HYD-FLO-FLO-1-GCDB describe
    the same flood). So corr_id groups are formed first, then merged on the
    identity corr_id encodes anyway -- hazard, place and time.
    """
    buckets = {}
    for item in items:
        key = item["corr_id"] or f"{item['collection']}:{item['id']}"
        buckets.setdefault(key, []).append(item)

    # Merge within a hazard type only, so a flood never absorbs an earthquake.
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
    """Whether two candidate groups of the same hazard type are one disaster.

    All three tests are permissive when a side has nothing to compare: EM-DAT and
    IDMC records routinely carry no geometry, and a few sources omit country
    codes, and neither absence should block a match the other fields support.
    """
    # Same country. Multi-country events list different subsets per source
    # (GDACS names every country in a cyclone track, USGS only the epicentre's),
    # so any overlap counts.
    if a["countries"] and b["countries"] and not (a["countries"] & b["countries"]):
        return False

    # Same time, give or take a day. Sources disagree on whether a disaster
    # started at first landfall or first report, and a UTC day boundary should
    # not split one event in two.
    if a["dates"] and b["dates"]:
        if abs((min(a["dates"]) - min(b["dates"])).total_seconds()) > MERGE_WINDOW_HOURS * 3600:
            return False

    # Same place. Guards against two unrelated quakes in one country on one day.
    if a["points"] and b["points"]:
        if not any(haversine_km(p, q) <= MERGE_RADIUS_KM
                   for p in a["points"] for q in b["points"]):
            return False

    return True


def merge_impacts(impacts: list) -> dict:
    """Best available figure per impact type, as {type: {value, modelled}}.

    Sources overlap heavily, so summing across them would double-count a toll
    every source reports. But a single source often splits one toll by admin
    region -- GDACS files Spain's flood deaths province by province -- so
    summing within a source and taking the maximum across sources gets both
    cases right.

    Observed figures always beat modelled ones. USGS PAGER publishes a fatality
    estimate within minutes of a quake, long before any body count exists; it is
    worth alerting on but must never be presented as a reported death toll.
    """
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
    """The figure reported for one impact type, or None."""
    return (group["impacts"].get(kind) or {}).get("value")


def summarise(group: dict) -> dict:
    """Collapse the items of one disaster into the record used for posting."""
    members = group["members"]
    countries = sorted(group["countries"])
    emoji, hazard_label = label_for_code(group["hazard"])

    events = [m for m in members if m["role"] == "event"]
    hazards = [m for m in members if m["role"] == "hazard"]
    impacts = [m for m in members if m["role"] == "impact"]
    # Prefer an event item for the headline; GDACS writes the fullest ones.
    ranked = sorted(events or members,
                    key=lambda m: (m["source"] != "GDACS", not m["title"]))
    lead = ranked[0]

    # Highest GDACS level seen anywhere in the group.
    levels = [m["alert_level"] for m in members if m["alert_level"]]
    alert_level = next((c for c in GDACS_LEVEL_NAMES if c in levels), "")

    # Best (largest) severity reading, keyed by unit so magnitudes and wind
    # speeds are only ever compared against their own kind.
    severities = {}
    for m in hazards + events:
        detail = m["hazard_detail"]
        value, unit = detail.get("severity_value"), (detail.get("severity_unit") or "")
        # A zero in a placeholder unit ("glide") is an absence, not a reading.
        if isinstance(value, (int, float)) and unit and value and unit not in NULL_SEVERITY_UNITS:
            severities[unit] = max(severities.get(unit, value), value)
        if isinstance(m["magnitude"], (int, float)):
            severities["mww"] = max(severities.get("mww", m["magnitude"]), m["magnitude"])

    dates = group["dates"]
    day = min(dates).strftime("%Y-%m-%d") if dates else "unknown"
    descriptions = sorted((m["description"] for m in events if m["description"]),
                          key=len, reverse=True)
    reports = {m["source"]: m["report_url"] for m in members if m["report_url"]}
    # The reference collection is Montandon's own correlation record, not a
    # body that reported anything, so it does not belong in a source list.
    sources = sorted({m["source"] for m in members} - {SOURCE_NAMES["reference"]})

    return {
        # The group key is stable for a given day, place and hazard. It can still
        # shift if a late source widens the country list, so dedup also matches
        # on corr_id -- see index_posted().
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


# -- Trigger rules -------------------------------------------------------------

def triggers(group: dict) -> list:
    """Reasons this group should be posted; empty means it stays quiet."""
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

    # Dedupe while keeping the order, so the alert level leads the summary.
    return list(dict.fromkeys(reasons))


# -- Slack ---------------------------------------------------------------------

def build_slack_payload(group: dict, reasons: list, is_update: bool) -> dict:
    """Build a Slack Incoming Webhook payload for one correlated disaster."""
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
        # Kept visually separate from reported figures. A PAGER estimate arrives
        # minutes after a quake and is nobody's casualty count.
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
    """Post one alert to Slack. Returns True only if Slack accepted the message."""
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


# -- Main ----------------------------------------------------------------------

def resolve_window() -> str:
    """The ISO 8601 interval to query, from START_DATE or LOOKBACK_DAYS."""
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

    # Most severe and most recent first, so a long backfill reads sensibly.
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

        # Known under some other signature -- this is a follow-up, not a new alert.
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

        time.sleep(1)  # Slack allows about one message per second.

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


# -- Reference data ------------------------------------------------------------

# ISO 3166-1 alpha-3 -> display name, generated from pycountry with the longer
# official forms shortened for readability in a one-line Slack field.
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
