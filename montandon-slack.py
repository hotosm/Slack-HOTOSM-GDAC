#!/usr/bin/env python3
"""Find, group, and post high-priority Montandon disasters to Slack."""

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
    "MONTANDON_STAC_URL", "https://montandon-eoapi.ifrc.org/stac"
).rstrip("/")

API_TOKEN = os.environ.get("MONTANDON_API_TOKEN", "")
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL", "")

# Default to preview mode.
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() != "false"

SEED_STATE = os.environ.get("SEED_STATE", "").lower() == "true"

# More new alerts and updates than this in one run suggests bad data; post nothing.
MAX_PENDING_ALERTS = 10

# Forget alerts unseen for this long. Each run refreshes alerts still in the data.
STATE_RETENTION_DAYS = 90

PAGE_SIZE = 200
MAX_ITEMS_PER_QUERY = 5000

# -- Alert thresholds ----------------------------------------------------------

# GDACS Orange triggers only for these families: the hazards GDACS alerts on.
GDACS_ORANGE_FAMILIES = {"GH01", "GH02", "MH03", "MH04", "MH06", "MH07", "EN02"}

MAGNITUDE_ANYWHERE = 7.0
MAGNITUDE_ON_LAND = 6.5         # Requires a named country, not open ocean.
CYCLONE_WIND_KT = 96            # Saffir-Simpson category 3.

# Late records require a current GDACS Orange or Red alert.
STALE_AFTER_DAYS = 30

# Reported impact thresholds. Precautionary evacuations are excluded.
IMPACT_TRIGGERS = {
    "death":              50,
    "missing":            50,
    "displaced_internal": 50000,
    "displaced_total":    50000,
    "homeless":           50000,
    "affected_total":     500000,
}

# IFRC operations are reported but do not trigger alerts alone.
SUPPORTING_REASONS = {"ifrc"}

# -- Sources -------------------------------------------------------------------

TRIGGER_QUERIES = [
    (["gdacs-hazards"], {"op": "in", "args": [
        {"property": "monty:hazard_detail.severity_label"},
        ["Orange", "Red", "orange", "red"]]}),
    (["usgs-events"], {"op": ">=", "args": [
        {"property": "eq:magnitude"}, MAGNITUDE_ON_LAND]}),
    (["ibtracs-hazards"], {"op": ">=", "args": [
        {"property": "monty:hazard_detail.severity_value"}, CYCLONE_WIND_KT]}),
    (["gdacs-impacts", "emdat-impacts", "idmc-idu-impacts", "ifrcevent-impacts",
      "cems-impacts"],
     {"op": "or", "args": [
         {"op": "and", "args": [
             {"op": "=", "args": [{"property": "monty:impact_detail.type"}, kind]},
             {"op": ">=", "args": [{"property": "monty:impact_detail.value"}, minimum]},
         ]}
         for kind, minimum in IMPACT_TRIGGERS.items()]}),
    (["cems-events", "ifrcevent-events"], None),
]

# Sources used to enrich candidates. Noisy, modelled PDC data is excluded.
CONTEXT_COLLECTIONS = [
    "gdacs-events", "gdacs-impacts", "usgs-events", "usgs-impacts",
    "glide-events", "cems-events", "emdat-events", "emdat-impacts",
    "idmc-idu-events", "idmc-idu-impacts", "ifrcevent-events", "ifrcevent-impacts",
    "ibtracs-events",
]
CONTEXT_PADDING_DEG = 1.5
CONTEXT_MAX_SPAN_DEG = 25.0     # Skip continent-sized droughts.

SOURCE_NAMES = {
    "gdacs": "GDACS", "pdc": "PDC", "usgs": "USGS", "glide": "GLIDE",
    "emdat": "EM-DAT", "idmc": "IDMC", "ibtracs": "IBTrACS", "cems": "Copernicus EMS",
    "charter": "Disasters Charter", "ifrcevent": "IFRC", "desinventar": "DesInventar",
    "gfd": "Global Flood Database",
}

SOURCE_RANK = ["GDACS", "IFRC", "Copernicus EMS", "USGS", "GLIDE", "EM-DAT", "IDMC",
               "IBTrACS"]

# These figures are exposure estimates, even where tagged as primary.
MODELLED_SOURCES = {"PDC", "USGS"}
MODELLED_UNITS = {"GDACS TC", "GDACS WF"}
MODELLED_TYPES = {"potentially_affected"}

# Exclude demographic and asset breakdowns from population totals.
PEOPLE_CATEGORIES = {"people", "total_affected", None}

IMPACT_LABELS = [
    ("death", "Deaths"), ("missing", "Missing"), ("injured", "Injured"),
    ("displaced_internal", "Displaced"), ("displaced_total", "Displaced"),
    ("evacuated", "Evacuated"), ("homeless", "Homeless"),
    ("relocated", "Relocated"), ("affected_total", "Affected"),
    ("potentially_affected", "Exposed"),
]

GDACS_LEVELS = ("red", "orange", "green")
LEVEL_RANK = {"red": 2, "orange": 1}
ALERT_EMOJI = {"red": ":red_circle:", "orange": ":large_orange_circle:"}
DEFAULT_EMOJI = ":large_blue_circle:"

MAX_DESCRIPTION_CHARS = 500

# UNDRR family: the first four characters of a code such as GH0101.
HAZARD_LABELS = {
    "GH01": (":earth_americas:", "Earthquake"),
    "GH02": (":volcano:", "Volcanic activity"),
    "GH03": (":mountain:", "Landslide"),
    "MH01": (":cloud_with_rain:", "Severe weather"),
    "MH03": (":cyclone:", "Storm"),
    "MH04": (":sun_with_face:", "Drought"),
    "MH05": (":thermometer:", "Extreme temperature"),
    "MH06": (":ocean:", "Flood"),
    "MH07": (":ocean:", "Tsunami / coastal"),
    "MH08": (":snowflake:", "Snow and ice"),
    "EN02": (":fire:", "Wildfire"),
    "BI01": (":microbe:", "Epidemic"),
    "SO02": (":warning:", "Conflict / unrest"),
}
GLIDE_FAMILIES = {
    "EQ": "GH01", "VO": "GH02", "LS": "GH03", "MS": "GH03", "TC": "MH03",
    "ST": "MH03", "VW": "MH03", "DR": "MH04", "HT": "MH05", "CW": "MH05",
    "FL": "MH06", "FF": "MH06", "TS": "MH07", "WF": "EN02", "EP": "BI01",
}

MERGE_RADIUS_KM = 500.0
MERGE_WINDOW_DAYS = 3

UNDRR_CODE = re.compile(r"^[A-Z]{2}\d{4}$")
GDACS_LEVEL_IN_DESCRIPTION = re.compile(r"^\s*(green|orange|red)\b", re.IGNORECASE)
EMSR_CODE = re.compile(r"EMSR\d+")
STORM_NAME = re.compile(
    r"\b(?:Tropical Cyclone|Tropical Storm|Tropical Depression|Hurricane|Typhoon|Cyclone)"
    r"\s+([A-Za-z]+(?:-[A-Za-z]+)*)", re.IGNORECASE)

MIN_MODELLED_FIGURE = 10


# -- Helpers -------------------------------------------------------------------

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


def stac_time(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def haversine_km(a: tuple, b: tuple) -> float:
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 6371.0 * 2 * math.asin(min(1.0, math.sqrt(h)))


def bbox_distance_km(a: list, b: list) -> float:
    """Distance between the nearest points of two bounding boxes."""
    def nearest(lo_a, hi_a, lo_b, hi_b):
        if hi_a < lo_b:
            return hi_a, lo_b
        if hi_b < lo_a:
            return lo_a, hi_b
        overlap = max(lo_a, lo_b)
        return overlap, overlap

    lon_a, lon_b = nearest(a[0], a[2], b[0], b[2])
    lat_a, lat_b = nearest(a[1], a[3], b[1], b[3])
    return haversine_km((lon_a, lat_a), (lon_b, lat_b))


def country_name(code: str) -> str:
    return COUNTRY_NAMES.get(code, code)


def source_of(collection_id: str) -> str:
    prefix = (collection_id or "").split("-")[0]
    return SOURCE_NAMES.get(prefix, prefix.upper() or "Unknown")


def format_number(value: float) -> str:
    return f"{int(value):,}"


def death_bucket(deaths: float) -> int:
    """Bucket deaths at 10, 100, 1,000 and so on."""
    return int(math.log10(deaths)) if deaths and deaths >= 1 else -1


# -- State ---------------------------------------------------------------------

def load_posted() -> list:
    """Load state, failing closed for live runs."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        if SEED_STATE or DRY_RUN:
            return []
        sys.exit(f"[error] {STATE_FILE} is missing. Run once with SEED_STATE=true first.")
    except json.JSONDecodeError as e:
        sys.exit(f"[error] {STATE_FILE} is not valid JSON ({e}); fix it before posting.")


def save_posted(records: list) -> None:
    """One alert per line keeps the file short and its diffs readable."""
    lines = [json.dumps(r, ensure_ascii=False) for r in records]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        f.write("[\n" + ",\n".join(lines) + "\n]\n")


def prune(records: list, now: datetime) -> list:
    """Drop alerts no run has seen within STATE_RETENTION_DAYS."""
    cutoff = (now - timedelta(days=STATE_RETENTION_DAYS)).date().isoformat()
    return [r for r in records if (r.get("last_seen") or r.get("posted_at") or cutoff) >= cutoff]


def find_posted(alert: dict, posted: list) -> list:
    """Records sharing a token, unless both name different GDACS Orange/Red events."""
    tokens, gdacs = set(alert["tokens"]), set(alert["gdacs_alerts"])
    return [record for record in posted if tokens & set(record["tokens"])
            and not (gdacs and record.get("gdacs_alerts")
                     and not gdacs & set(record["gdacs_alerts"]))]


def remember(alert: dict, matches: list, posted: list) -> dict:
    """Merge newly matched source IDs into state."""
    if matches:
        record = matches[0]
    else:
        record = {"tokens": [], "gdacs_alerts": [], "level_rank": 0, "death_bucket": -1}
        posted.append(record)
    record["tokens"] = sorted(set(record["tokens"]).union(
        alert["tokens"], *(m["tokens"] for m in matches)))
    record["gdacs_alerts"] = sorted(set(record.get("gdacs_alerts", [])).union(
        alert["gdacs_alerts"], *(m.get("gdacs_alerts", []) for m in matches)))
    record["level_rank"] = max([LEVEL_RANK.get(alert["level"], 0)]
                               + [m["level_rank"] for m in matches])
    record["death_bucket"] = max([death_bucket(alert["reported"].get("death", 0))]
                                 + [m["death_bucket"] for m in matches])
    record["title"] = record.get("title") or alert["title"]
    record["last_seen"] = datetime.now(timezone.utc).date().isoformat()
    return record


# -- Montandon API -------------------------------------------------------------

def api_session() -> requests.Session:
    if not API_TOKEN:
        raise EnvironmentError(
            "MONTANDON_API_TOKEN is not set. Generate a token from your IFRC GO "
            "account settings (https://go.ifrc.org/)."
        )
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Bearer {API_TOKEN}",
        "Accept": "application/geo+json, application/json",
    })
    return session


def search(session: requests.Session, body: dict, label: str) -> list:
    """Search all pages; abort rather than return partial data."""
    body = {"limit": PAGE_SIZE, **body}
    if "filter" in body:
        body["filter-lang"] = "cql2-json"
    url = f"{STAC_API_URL}/search"
    items = []

    while True:
        response = None
        for attempt in range(3):
            try:
                response = session.post(url, json=body, timeout=120)
                if response.status_code < 500:
                    break
            except requests.RequestException as e:
                print(f"[warn] {label}: {e}", file=sys.stderr)
            time.sleep(5 * (attempt + 1))
        if response is None or response.status_code != 200:
            detail = response.text[:300] if response is not None else "no response"
            sys.exit(f"[error] {label}: search failed, nothing posted: {detail}")

        payload = response.json()
        features = payload.get("features", [])
        items.extend(features)

        if len(items) > MAX_ITEMS_PER_QUERY:
            sys.exit(f"[error] {label}: over {MAX_ITEMS_PER_QUERY} items, nothing posted")

        # A STAC next link may replace or extend the POST body.
        nxt = next((l for l in payload.get("links", []) if l.get("rel") == "next"), None)
        if not nxt or not features:
            break
        url = nxt.get("href", url)
        body = {**body, **nxt.get("body", {})} if nxt.get("merge") else nxt.get("body", body)

    return items


# -- Parsing -------------------------------------------------------------------

def hazard_family(codes: list) -> str:
    """Prefer GLIDE because some sources assign inconsistent UNDRR codes."""
    codes = [c for c in (codes or []) if isinstance(c, str)]
    for code in codes:
        if code in GLIDE_FAMILIES:
            return GLIDE_FAMILIES[code]
    for code in codes:
        if UNDRR_CODE.match(code):
            return code[:4]
    return "UNKNOWN"


def storm_name(title: str) -> str:
    """Cyclone name without the GDACS year suffix, e.g. KOGUMA-26 -> KOGUMA."""
    match = STORM_NAME.search(title)
    if not match:
        return ""
    name = re.sub(r"-\d+$", "", match.group(1).upper())
    return "" if name in ("UNNAMED", "INVEST") or name.isdigit() else name


def gdacs_level(props: dict) -> str:
    """Read the event colour before falling back to the episode label."""
    match = GDACS_LEVEL_IN_DESCRIPTION.match(props.get("description") or "")
    if match:
        return match.group(1).lower()
    label = ((props.get("monty:hazard_detail") or {}).get("severity_label") or "").lower()
    return label if label in GDACS_LEVELS else ""


def report_url(item: dict, source: str, event_id: str) -> str:
    links = item.get("links") or []
    via = next((l.get("href", "") for l in links if l.get("rel") == "via"), "")

    if source == "USGS":
        return f"https://earthquake.usgs.gov/earthquakes/eventpage/{event_id}"
    if source == "IFRC":
        return f"https://go.ifrc.org/emergencies/{event_id}"
    if source in ("Copernicus EMS", "EM-DAT"):
        return via
    if source in ("GDACS", "GLIDE"):
        return ((item.get("assets") or {}).get("report") or {}).get("href", "")
    return ""


def parse_item(item: dict) -> dict:
    props = item.get("properties", {})
    collection = item.get("collection") or ""
    source = source_of(collection)
    roles = props.get("roles") or []
    event_id = str(props.get("monty:src_event_id") or props.get("monty:corr_id")
                   or item.get("id", ""))
    family = hazard_family(props.get("monty:hazard_codes"))

    start = parse_dt(props.get("start_datetime")) or parse_dt(props.get("datetime"))
    end = parse_dt(props.get("end_datetime")) or start

    bbox = item.get("bbox")
    if not bbox or len(bbox) < 4:
        coords = ((item.get("geometry") or {}).get("coordinates") or [None, None])[:2]
        bbox = [coords[0], coords[1], coords[0], coords[1]] if None not in coords else None
    elif len(bbox) == 6:
        bbox = [bbox[0], bbox[1], bbox[3], bbox[4]]

    detail = props.get("monty:impact_detail") or {}
    hazard = props.get("monty:hazard_detail") or {}
    magnitude = props.get("eq:magnitude")
    if source == "GDACS" and (props.get("severitydata") or {}).get("severityunit") == "M":
        magnitude = props["severitydata"].get("severity")

    impact = None
    if detail.get("type") and isinstance(detail.get("value"), (int, float)):
        impact = {
            "type": detail["type"],
            "value": detail["value"],
            "people": detail.get("category") in PEOPLE_CATEGORIES,
            "modelled": (detail.get("estimate_type") == "modelled"
                         or source in MODELLED_SOURCES
                         or detail.get("unit") in MODELLED_UNITS
                         or detail["type"] in MODELLED_TYPES),
        }

    return {
        # GDACS reuses event IDs across hazard types.
        "token": f"{source}:{family}:{event_id}",
        "source": source,
        "collection": collection,
        "event_id": event_id,
        "role": "impact" if "impact" in roles else "hazard" if "hazard" in roles else "event",
        "episode": props.get("monty:episode_number") or 0,
        "title": strip_html(props.get("title") or ""),
        "description": strip_html(props.get("description") or ""),
        "start": start,
        "end": end,
        "countries": {c for c in (props.get("monty:country_codes") or []) if c and c != "UNK"},
        "family": family,
        "storm": storm_name(props.get("title") or ""),
        "gdacs_level": gdacs_level(props) if source == "GDACS" else "",
        "magnitude": magnitude if isinstance(magnitude, (int, float)) else None,
        "wind_kt": (hazard.get("severity_value")
                    if source == "IBTrACS" and hazard.get("severity_unit") == "knots" else None),
        "impact": impact,
        "bbox": bbox,
        "report_url": report_url(item, source, event_id),
    }


# -- Grouping ------------------------------------------------------------------

def new_group(records: list) -> dict:
    boxes = [r["bbox"] for r in records if r["bbox"]]
    return {
        "records": list(records),
        "family": next((r["family"] for r in records if r["family"] != "UNKNOWN"), "UNKNOWN"),
        "countries": set().union(*(r["countries"] for r in records)),
        "storms": {r["storm"] for r in records if r["storm"]},
        "gdacs_alerts": {r["token"] for r in records
                         if r["source"] == "GDACS" and r["gdacs_level"] in LEVEL_RANK},
        "bbox": [min(b[0] for b in boxes), min(b[1] for b in boxes),
                 max(b[2] for b in boxes), max(b[3] for b in boxes)] if boxes else None,
        "start": min((r["start"] for r in records if r["start"]), default=None),
        "end": max((r["end"] for r in records if r["end"]), default=None),
    }


def same_disaster(a: dict, b: dict) -> bool:
    """Missing fields are neutral; all available ones must agree."""
    if a["family"] != b["family"]:
        return False
    # Cyclone tracks are long and overlap, so names must agree when known.
    if a["storms"] and b["storms"] and not (a["storms"] & b["storms"]):
        return False
    # Separate GDACS Orange or Red events are separate alerts, e.g. nearby eruptions.
    if a["gdacs_alerts"] and b["gdacs_alerts"] and not (a["gdacs_alerts"] & b["gdacs_alerts"]):
        return False
    if a["countries"] and b["countries"] and not (a["countries"] & b["countries"]):
        return False
    if a["start"] and b["start"]:
        window = timedelta(days=MERGE_WINDOW_DAYS)
        if a["start"] > b["end"] + window or b["start"] > a["end"] + window:
            return False
    if a["bbox"] and b["bbox"]:
        if bbox_distance_km(a["bbox"], b["bbox"]) > MERGE_RADIUS_KM:
            return False
    return True


def group_records(records: list, anchor_tokens: set) -> list:
    """Group records by ranked anchor, avoiding transitive A-B-C merges."""
    by_event = {}
    for record in records:
        by_event.setdefault(record["token"], []).append(record)
    units = {token: new_group(members) for token, members in by_event.items()}

    # Strongest anchor first, e.g. a main shock before its aftershocks.
    def strength(token):
        alert = summarise(units[token])
        return (LEVEL_RANK.get(alert["level"], 0), alert["magnitude"] or 0,
                alert["wind_kt"] or 0)

    anchors = sorted((t for t in anchor_tokens if t in units),
                     key=lambda t: (strength(t), t), reverse=True)
    claimed, groups = set(), []
    for anchor in anchors:
        if anchor in claimed:
            continue
        members = [t for t, unit in units.items() if t not in claimed
                   and (t == anchor or same_disaster(units[anchor], unit))]
        claimed.update(members)
        groups.append(new_group([r for t in members for r in units[t]["records"]]))
    return groups


def merge_impacts(records: list, modelled: bool) -> dict:
    """Take the largest figure per type to avoid summing duplicate reports."""
    totals = {}
    for r in records:
        impact = r["impact"]
        if not impact or not impact["people"] or impact["modelled"] != modelled:
            continue
        totals[impact["type"]] = max(totals.get(impact["type"], 0), impact["value"])
    return totals


def summarise(group: dict) -> dict:
    records = group["records"]
    tokens = sorted({r["token"] for r in records})

    # GDACS: the latest episode carries the current event level.
    gdacs_latest = {}
    for r in records:
        if r["source"] == "GDACS" and r["gdacs_level"]:
            current = gdacs_latest.get(r["event_id"])
            if not current or r["episode"] > current["episode"]:
                gdacs_latest[r["event_id"]] = r
    levels = {r["gdacs_level"] for r in gdacs_latest.values()}
    level = next((c for c in GDACS_LEVELS if c in levels), "")

    # Prefer trusted event sources, then severity and the latest episode.
    def rank(r):
        source = SOURCE_RANK.index(r["source"]) if r["source"] in SOURCE_RANK else 99
        return (source, r["role"] != "event", -LEVEL_RANK.get(r["gdacs_level"], 0),
                -(r["magnitude"] or 0), -r["episode"], not r["title"])

    ranked = sorted(records, key=rank)
    lead = ranked[0]

    reports = {}
    for r in ranked:
        if r["report_url"] and r["source"] not in reports:
            reports[r["source"]] = r["report_url"]

    cems = sorted({m for r in records if r["source"] == "Copernicus EMS"
                   for m in EMSR_CODE.findall(r["report_url"] + " " + r["title"])})
    ifrc = sorted({r["event_id"] for r in records
                   if r["source"] == "IFRC" and r["role"] == "event"})

    descriptions = [r["description"] for r in ranked if r["description"] and r["role"] == "event"]

    return {
        "tokens": tokens,
        "gdacs_alerts": sorted({r["token"] for r in records
                                if r["source"] == "GDACS" and r["gdacs_level"] in LEVEL_RANK}),
        "title": lead["title"] or "Unnamed event",
        "family": group["family"],
        "countries": sorted(group["countries"]),
        "start": group["start"],
        "end": group["end"],
        "level": level,
        "magnitude": max((r["magnitude"] for r in records if r["magnitude"]), default=None),
        "wind_kt": max((r["wind_kt"] for r in records if r["wind_kt"]), default=None),
        "reported": merge_impacts(records, modelled=False),
        "modelled": merge_impacts(records, modelled=True),
        "cems": cems,
        "ifrc": ifrc,
        "sources": sorted({r["source"] for r in records},
                          key=lambda s: SOURCE_RANK.index(s) if s in SOURCE_RANK else 99),
        "reports": reports,
        "description": descriptions[0] if descriptions else "",
    }


# -- Alert rules ---------------------------------------------------------------

def triggers(alert: dict, now: datetime) -> list:
    """Return matched (kind, reason) pairs."""
    reasons = []
    level, family = alert["level"], alert["family"]
    technological = family.startswith("TL")
    stale = alert["start"] and alert["start"] < now - timedelta(days=STALE_AFTER_DAYS)

    if level == "red":
        reasons.append(("gdacs_red", "GDACS Red alert"))
    elif level == "orange" and family in GDACS_ORANGE_FAMILIES:
        reasons.append(("gdacs_orange", "GDACS Orange alert"))

    # Use raw readings only when GDACS has no assessment; Green is authoritative.
    magnitude = alert["magnitude"]
    if magnitude and (magnitude >= MAGNITUDE_ANYWHERE
                      or (magnitude >= MAGNITUDE_ON_LAND and alert["countries"] and not level)):
        reasons.append(("magnitude", f"M{magnitude:.1f} earthquake"))

    wind = alert["wind_kt"]
    if wind and wind >= CYCLONE_WIND_KT and alert["countries"] and not level:
        reasons.append(("wind", f"{wind:.0f} kt winds"))

    if not technological:
        labels = dict(IMPACT_LABELS)
        for kind, minimum in IMPACT_TRIGGERS.items():
            value = alert["reported"].get(kind)
            if value and value >= minimum:
                key = ("deaths" if kind in ("death", "missing")
                       else "affected" if kind == "affected_total" else "displaced")
                reasons.append((key, f"{format_number(value)} {labels[kind].lower()} reported"))

    if alert["cems"]:
        reasons.append(("cems", f"Copernicus EMS mapping activation ({', '.join(alert['cems'])})"))
    if stale and not any(key.startswith("gdacs") for key, _ in reasons):
        return []

    if alert["ifrc"] and any(key not in SUPPORTING_REASONS for key, _ in reasons):
        reasons.append(("ifrc", "IFRC emergency operation"))

    # List each kind once.
    unique = {}
    for key, text in reasons:
        unique.setdefault(key, []).append(text)
    return [(key, "; ".join(texts)) for key, texts in unique.items()]


def escalation(alert: dict, matches: list):
    """None if already posted at this level, else 'new' or 'update'."""
    if not matches:
        return "new"
    if LEVEL_RANK.get(alert["level"], 0) > max(m["level_rank"] for m in matches):
        return "update"
    # Death-toll updates start at 10.
    deaths = death_bucket(alert["reported"].get("death", 0))
    if deaths >= 1 and deaths > max(m["death_bucket"] for m in matches):
        return "update"
    return None


# -- Slack ---------------------------------------------------------------------

def build_slack_payload(alert: dict, reasons: list, is_update: bool) -> dict:
    level = alert["level"]
    emoji = ALERT_EMOJI.get(level, DEFAULT_EMOJI)
    hazard_emoji, hazard_label = HAZARD_LABELS.get(alert["family"], (":warning:", "Hazard"))
    heading = f"{hazard_label} {'Update' if is_update else 'Alert'}"
    if level in LEVEL_RANK:
        heading = f"{level.capitalize()} {heading}"

    lines = [
        f"{emoji} *{heading}: {escape_mrkdwn(alert['title'])}*",
        f"{hazard_emoji} *Hazard:* {hazard_label}",
    ]
    if alert["countries"]:
        names = ", ".join(country_name(c) for c in alert["countries"])
        lines.append(f"*Countries:* {escape_mrkdwn(names)}")
    if alert["start"]:
        lines.append(f"*Started:* {alert['start'].strftime('%Y-%m-%d')}")

    lines.append(f"*Why this alert:* {escape_mrkdwn('; '.join(text for _, text in reasons))}")

    for title, figures, prefix in (("Reported impact", alert["reported"], ""),
                                   ("Modelled estimate", alert["modelled"], "~")):
        shown, seen_labels = [], set()
        for kind, label in IMPACT_LABELS:
            if prefix and figures.get(kind, 0) < MIN_MODELLED_FIGURE:
                continue
            if kind in figures and label not in seen_labels:
                shown.append(f"*{label}:* {prefix}{format_number(figures[kind])}")
                seen_labels.add(label)
        if shown:
            lines.append(f"*{title}:* " + "   ".join(shown))

    description = escape_mrkdwn(alert["description"]).strip()
    if len(description) > MAX_DESCRIPTION_CHARS:
        description = description[:MAX_DESCRIPTION_CHARS].rstrip() + "..."
    if description:
        lines.append("")
        lines.append(description)

    lines.append("")
    lines.append(f"*Sources:* {escape_mrkdwn(', '.join(alert['sources']))} (via IFRC Montandon)")
    links = [f"<{url}|{source}>" for source, url in alert["reports"].items()]
    if links:
        lines.append(" | ".join(links))

    countries = ", ".join(country_name(c) for c in alert["countries"]) or "unknown location"
    return {
        "text": f"{heading}: {alert['title']} ({countries})",
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "\n".join(lines)},
            }
        ],
    }


def post_to_slack(payload: dict) -> bool:
    if DRY_RUN:
        print("--- DRY RUN: would post ---")
        print(payload["blocks"][0]["text"]["text"])
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

def resolve_window() -> tuple:
    now = datetime.now(timezone.utc)
    start_date = os.environ.get("START_DATE", "").strip()
    end_date = os.environ.get("END_DATE", "").strip()
    lookback = os.environ.get("LOOKBACK_DAYS", "").strip()

    try:
        end = (datetime.fromisoformat(end_date).replace(tzinfo=timezone.utc)
               if end_date else now)
        if start_date:
            start = datetime.fromisoformat(start_date).replace(tzinfo=timezone.utc)
        else:
            start = end - timedelta(days=int(lookback) if lookback else 14)
    except ValueError:
        sys.exit(f"[error] Bad window: START_DATE={start_date!r} and END_DATE={end_date!r} "
                 f"must be dates like 2026-01-01, LOOKBACK_DAYS={lookback!r} a whole number")
    return start, end


def collect(session: requests.Session, start: datetime, end: datetime) -> tuple:
    """Find candidate records, then add corroborating records from nearby."""
    window = f"{stac_time(start)}/{stac_time(end)}"

    candidates = []
    for collections, cql in TRIGGER_QUERIES:
        body = {"collections": collections, "datetime": window}
        if cql:
            body["filter"] = cql
        items = search(session, body, ",".join(collections))
        print(f"[stac] {', '.join(collections)}: {len(items)} candidate items")
        candidates.extend(parse_item(i) for i in items)

    # Deduplicate overlapping searches.
    records = {}

    def keep(record):
        key = (record["collection"], record["token"], record["episode"], record["title"],
               str(record["impact"]))
        records.setdefault(key, record)

    def add(items, wanted=lambda r: True):
        for r in map(parse_item, items):
            if wanted(r):
                keep(r)

    for r in candidates:
        keep(r)
    seeds = group_records(candidates, {r["token"] for r in candidates})
    print(f"[group] {len(candidates)} candidates form {len(seeds)} possible alerts")

    # Fetch all GDACS episodes to find the current alert level.
    gdacs = {r["token"] for r in candidates if r["source"] == "GDACS"}
    gdacs_ids = sorted({r["event_id"] for r in candidates if r["source"] == "GDACS"})
    for i in range(0, len(gdacs_ids), 50):
        add(search(session, {"collections": ["gdacs-events", "gdacs-hazards"], "filter": {
            "op": "in", "args": [{"property": "monty:src_event_id"}, gdacs_ids[i:i + 50]]}},
            "gdacs episodes"), wanted=lambda r: r["token"] in gdacs)

    for seed in seeds:
        since = (seed["start"] or start) - timedelta(days=MERGE_WINDOW_DAYS)
        period = f"{stac_time(since)}/{stac_time(end)}"

        # Match long cyclone tracks to GDACS by name.
        if seed["storms"]:
            add(search(session, {
                "collections": ["gdacs-events"], "datetime": period,
                "filter": {"op": "or", "args": [
                    {"op": "like", "args": [{"property": "title"}, f"%{name}%"]}
                    for name in sorted(seed["storms"])]},
            }, "storm names"))

        box = seed["bbox"]
        if not box or max(box[2] - box[0], box[3] - box[1]) > CONTEXT_MAX_SPAN_DEG:
            continue
        pad = CONTEXT_PADDING_DEG
        add(search(session, {
            "collections": CONTEXT_COLLECTIONS,
            "bbox": [max(-180, box[0] - pad), max(-90, box[1] - pad),
                     min(180, box[2] + pad), min(90, box[3] + pad)],
            "datetime": period,
        }, "context"))

    print(f"[stac] {len(records)} records after adding context")
    return list(records.values()), {r["token"] for r in candidates}


def main() -> None:
    start, end = resolve_window()
    mode = "SEED" if SEED_STATE else "DRY RUN" if DRY_RUN else "LIVE"
    print(f"[run] {mode} -- querying {STAC_API_URL} over {stac_time(start)}/{stac_time(end)}")

    session = api_session()
    records, candidate_tokens = collect(session, start, end)

    alerts = []
    for group in group_records(records, candidate_tokens):
        alert = summarise(group)
        reasons = triggers(alert, end)
        if reasons:
            alerts.append((alert, reasons))
    # GDACS Red, then Orange, then the rest; newest first within each.
    alerts.sort(key=lambda a: (LEVEL_RANK.get(a[0]["level"], 0),
                               a[0]["start"].timestamp() if a[0]["start"] else 0),
                reverse=True)
    print(f"[group] {len(alerts)} alerts meet the thresholds")

    loaded = load_posted()
    posted = prune(loaded, end)
    print(f"[state] Loaded {len(loaded)} previously posted alerts, "
          f"{len(loaded) - len(posted)} expired")
    save = not DRY_RUN or SEED_STATE

    pending = []
    for alert, reasons in alerts:
        matches = find_posted(alert, posted)
        kind = escalation(alert, matches)
        if kind is None:
            remember(alert, matches, posted)
        else:
            pending.append((alert, reasons, matches, kind))
    skipped_count = len(alerts) - len(pending)

    if len(pending) > MAX_PENDING_ALERTS and not SEED_STATE:
        for alert, reasons, _, _ in pending:
            print(f"[held] {alert['title']} -- {'; '.join(t for _, t in reasons)}")
        message = (f"{len(pending)} new alerts and updates exceeds MAX_PENDING_ALERTS "
                   f"({MAX_PENDING_ALERTS}); check the data or thresholds, then seed state or post manually.")
        if DRY_RUN:
            print(f"[warning] {message}")
        else:
            sys.exit(f"[error] {message} Nothing was posted.")

    posted_count = error_count = 0

    for alert, reasons, matches, kind in pending:
        summary = f"{alert['title']} -- {'; '.join(t for _, t in reasons)}"
        if SEED_STATE or post_to_slack(build_slack_payload(alert, reasons, kind == "update")):
            record = remember(alert, matches, posted)
            record["posted_at"] = record["last_seen"]
            # Preserve earlier successes if a later post fails.
            if save:
                save_posted(posted)
            posted_count += 1
            print(f"[{'seeded' if SEED_STATE else 'posted'}] "
                  f"{'[update] ' if kind == 'update' else ''}{summary}")
            if not SEED_STATE:
                time.sleep(1)  # Respect Slack's one-message-per-second limit.
        else:
            error_count += 1
            print(f"[error] Failed to post {alert['title']} -- will retry on the next run",
                  file=sys.stderr)

    counts = f"already posted: {skipped_count}"
    if not save:
        print(f"\n[done] DRY RUN -- would post: {posted_count} | {counts} (state not saved)")
        return

    save_posted(posted)
    print(f"\n[done] {'Seeded' if SEED_STATE else 'Posted'}: {posted_count} | {counts} | "
          f"errors: {error_count} | {len(posted)} alerts in {STATE_FILE}")

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
