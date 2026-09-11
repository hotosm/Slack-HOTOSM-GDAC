import html
import os
import json
import re
import sys
import time
import requests
from datetime import datetime, timedelta, timezone

SLACK_WEBHOOK_URL = os.environ['SLACK_WEBHOOK_URL']

# Preview messages without posting or updating state.
DRY_RUN = os.environ.get('DRY_RUN', 'false').lower() == 'true'

API_URL = 'https://www.who.int/api/news/diseaseoutbreaknews'
STATE_FILE = 'posted_dons.json'

# Attachment accent color.
WHO_BLUE = "#009EDB"

# START_DATE overrides the rolling LOOKBACK_DAYS window (default 30).
_start_date_str = os.environ.get('START_DATE', '').strip()
if _start_date_str:
    CUTOFF_DATE = datetime.fromisoformat(_start_date_str).replace(tzinfo=timezone.utc)
else:
    _lookback_str = os.environ.get('LOOKBACK_DAYS', '').strip()
    LOOKBACK_DAYS = int(_lookback_str) if _lookback_str else 30
    CUTOFF_DATE = None

def load_posted_ids():
    try:
        with open(STATE_FILE, 'r') as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def save_posted_ids(ids):
    with open(STATE_FILE, 'w') as f:
        json.dump(sorted(ids), f, indent=2)


def strip_html(raw):
    """Remove HTML tags from a string and decode HTML entities."""
    if not raw:
        return ''
    text = html.unescape(re.sub(r'<[^>]+>', ' ', raw))
    return re.sub(r'\s+', ' ', text).strip()


def escape_mrkdwn(text):
    """Escape the three characters Slack mrkdwn treats as markup."""
    # Decode first, or an API field already holding &amp; ends up &amp;amp;.
    text = html.unescape(text or '')
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def build_don_url(item):
    url_name = item.get('UrlName', '')
    if url_name:
        return f'https://www.who.int/emergencies/disease-outbreak-news/item/{url_name}'
    default_url = item.get('ItemDefaultUrl', '')
    if default_url.startswith('http'):
        return default_url
    if default_url.startswith('/'):
        return f'https://www.who.int{default_url}'
    return 'https://www.who.int/emergencies/disease-outbreak-news'


def build_slack_payload(item):
    """Build a Slack Incoming Webhook payload."""
    title = item.get('OverrideTitle') or item.get('Title') or 'Untitled DON'
    don_url = build_don_url(item)
    pub_date = item.get('PublicationDateAndTime', item.get('PublicationDate', ''))[:10]
    summary = strip_html(item.get('Summary') or item.get('Overview', ''))[:300]
    if len(summary) == 300:
        summary += '...'
    don_id = item.get('DonId') or item.get('Id', '')

    lines = [
        ":large_blue_circle: *WHO Disease Outbreak News*",
        f"*{escape_mrkdwn(title)}*",
    ]

    meta = []
    if pub_date:
        meta.append(f"*Published:* {pub_date}")
    if don_id:
        meta.append(f"*ID:* {don_id}")
    if meta:
        lines.append("   ".join(meta))

    if summary:
        lines.append("")
        lines.append(escape_mrkdwn(summary))

    lines.append("")
    lines.append(f"<{don_url}|Read the full report on who.int>")

    return {
        "attachments": [
            {
                "fallback": f"WHO Disease Outbreak News: {title}",
                "color": WHO_BLUE,
                "blocks": [
                    {
                        "type": "section",
                        "text": {"type": "mrkdwn", "text": "\n".join(lines)},
                    }
                ],
            }
        ],
    }


def post_to_slack(item):
    """Post a DON to Slack. Returns True only if Slack accepted the message."""
    payload = build_slack_payload(item)

    if DRY_RUN:
        print("--- DRY RUN: would post ---")
        print(json.dumps(payload, indent=2))
        print("---------------------------")
        return True

    try:
        r = requests.post(SLACK_WEBHOOK_URL, json=payload, timeout=15)
    except requests.RequestException as e:
        print(f"Slack request failed: {e}")
        return False

    if r.status_code != 200:
        print(f"Slack error {r.status_code}: {r.text.strip()}")
        return False
    return True


def fetch_all_recent_items(cutoff):
    """Fetch recent DONs, falling back if the API ignores sort order."""
    def get_page(params):
        resp = requests.get(API_URL, params=params, timeout=30)
        if resp.status_code != 200:
            print(f"WHO API error: {resp.status_code}")
            return None
        data = resp.json()
        return data if isinstance(data, list) else data.get('value', [])

    def parse_date(item):
        pub_str = item.get('PublicationDateAndTime') or item.get('PublicationDate')
        if not pub_str:
            return None
        try:
            return datetime.fromisoformat(pub_str.replace('Z', '+00:00'))
        except ValueError:
            return None

    page_size = 100

    # Try newest-first.
    first_page = get_page({'$orderby': 'PublicationDate desc', '$top': page_size, '$skip': 0})
    if first_page is None:
        return []

    if first_page:
        first_date = parse_date(first_page[0])

        if first_date and first_date >= cutoff:
            recent_items = []
            skip = 0
            items = first_page
            while items:
                hit_old_item = False
                for item in items:
                    d = parse_date(item)
                    if d is None:
                        continue
                    if d >= cutoff:
                        recent_items.append(item)
                    else:
                        hit_old_item = True
                if hit_old_item or len(items) < page_size:
                    break
                skip += page_size
                items = get_page({'$orderby': 'PublicationDate desc', '$top': page_size, '$skip': skip})
                if items is None:
                    break
            return recent_items

    # Default ordering is oldest-first, so start from the final page.
    count_resp = requests.get(f"{API_URL}/$count", timeout=30)
    if count_resp.status_code != 200:
        print(f"WHO API error: could not get total count, status {count_resp.status_code}")
        return []
    try:
        total_count = int(count_resp.text.strip())
    except ValueError:
        print(f"WHO API error: unexpected $count response: {count_resp.text[:200]}")
        return []

    recent_items = []
    skip = max(0, total_count - page_size)
    while True:
        items = get_page({'$top': page_size, '$skip': skip})
        if not items:
            break
        for item in items:
            d = parse_date(item)
            if d and d >= cutoff:
                recent_items.append(item)
        if skip == 0:
            break
        skip = max(0, skip - page_size)

    return recent_items


def main():
    cutoff = CUTOFF_DATE if CUTOFF_DATE else datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    recent_items = fetch_all_recent_items(cutoff)

    posted_ids = load_posted_ids()
    new_count = 0
    error_count = 0

    for item in recent_items:
        # Older entries may only have an internal ID.
        dedup_key = item.get('DonId') or item.get('Id')
        if not dedup_key or dedup_key in posted_ids:
            continue

        if post_to_slack(item):
            print(f"Posted {dedup_key}")
            posted_ids.add(dedup_key)
            new_count += 1
        else:
            print(f"Failed to post {dedup_key} -- will retry on the next run")
            error_count += 1
        time.sleep(1)  # Slack allows about one message per second.

    if DRY_RUN:
        print(f"Checked {len(recent_items)} recent DONs, would post {new_count} new (DRY_RUN, state not saved)")
    else:
        save_posted_ids(posted_ids)
        print(f"Checked {len(recent_items)} recent DONs, posted {new_count} new")
        if error_count:
            sys.exit(f"{error_count} DON(s) failed to post -- will retry on the next run")


if __name__ == '__main__':
    main()
