# HOTOSM disaster alerts

Python scripts and GitHub Actions workflows that post new disasters and disease
outbreaks to the HOTOSM Slack `#disaster-alerts` channel.

| Script | Source | Workflow | Status |
| --- | --- | --- | --- |
| `montandon-slack.py` | [IFRC Montandon](https://ifrcgo.org/monty-stac-extension/) STAC API (merges GDACS, PDC, USGS, GLIDE, EM-DAT, IDMC, Copernicus EMS and more) | `montandon-alerts.yml` | **Preview** — dry run by default |
| `gdacs-slack.py` | [GDACS](https://gdacs.org/) event API | `gdacs-alerts.yml` | Live |
| `who-don-slack.py` | [WHO Disease Outbreak News](https://www.who.int/emergencies/disease-outbreak-news) | `gdacs-alerts.yml` | Live |

Montandon is a single, standards-based endpoint that already aggregates GDACS
and the other sources, so `montandon-slack.py` is intended to replace
`gdacs-slack.py` once its output has been checked against the live feed.

## Montandon alerts

Each Slack message covers one *correlated disaster* rather than one API record:
a flood reported by GDACS, GLIDE and EM-DAT becomes a single message naming all
three, with the impact figures merged.

### What gets posted

A disaster is posted when any of these holds (the message says which fired):

- GDACS rates it **Orange** or **Red**
- magnitude **M6.0+**, sustained winds of **64 kt / 119 km/h** or more
- a reported impact clears a floor — 10 deaths or missing, 1,000 displaced,
  evacuated or homeless, or 10,000 affected

Thresholds live in `SEVERITY_TRIGGERS` and `IMPACT_TRIGGERS` at the top of the
script. Modelled figures (USGS PAGER publishes a fatality estimate within
minutes of a quake, long before any body count) trigger alerts but are shown in
a separate, labelled block and prefixed with `~`, never as a reported toll.

### Reposting

State lives in `posted_montandon.json`, committed back to the repo after each
run. A disaster reposts as an **Update** when its GDACS alert level escalates,
or when its death toll crosses an order of magnitude — otherwise it stays quiet.
Matching is done on the correlation ID as well as the derived group key, so a
late source widening the country list does not cause a duplicate.

### Configuration

Set as GitHub Actions secrets:

| Secret | Purpose |
| --- | --- |
| `MONTANDON_API_TOKEN` | Bearer token from [IFRC GO](https://goadmin-stage.ifrc.org/) → Account settings → API Tokens |
| `SLACK_WEBHOOK_URL` | Slack Incoming Webhook for the target channel |

The workflow **previews by default**: it logs the messages it would send without
posting them or writing state. To go live, set the repository variable
`MONTANDON_DRY_RUN` to `false` under Settings → Secrets and variables → Actions
→ Variables. Running it from the Actions tab also lets you override the dry run,
the lookback window (`lookback_days`, default 7) and the cutoff (`start_date`).

### Running locally

```bash
pip install requests
export MONTANDON_API_TOKEN='...'
DRY_RUN=true LOOKBACK_DAYS=3 python montandon-slack.py
```

`MONTANDON_STAC_URL` overrides the API root, which currently defaults to the
IFRC staging endpoint.
