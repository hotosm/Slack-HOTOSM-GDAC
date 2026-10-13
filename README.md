# HOTOSM disaster alerts

Python scripts and GitHub Actions workflows that post new disasters and disease
outbreaks to the HOTOSM Slack `#disaster-alerts` channel.

`gdacs-slack.py` and `who-don-slack.py` are live. `montandon-slack.py` uses the
[IFRC Montandon](https://ifrcgo.org/monty-stac-extension/) STAC API to combine
alerts from GDACS, USGS, IBTrACS, Copernicus EMS, IFRC GO, GLIDE, EM-DAT and
IDMC.

## Montandon alerts

An alert requires at least one of:

- a GDACS Red alert
- a GDACS Orange earthquake, cyclone, flood, volcano or tsunami alert
- an earthquake of M7+, or M6.5+ in a named country where GDACS has not assessed it
- cyclone winds of 96 kt+ over a named country where GDACS has not assessed it
- at least 50 reported deaths or missing, 50,000 displaced or homeless, or
  500,000 affected
- a Copernicus EMS rapid mapping activation

Modelled impacts, technological incidents and precautionary evacuations do not
trigger alerts. An IFRC operation is noted but does not trigger an alert.

Records are grouped by hazard type, country, time (within 3 days), distance
(within 500 km) and cyclone name. Each group is anchored to one triggering
record; other records must match that anchor directly. Modelled exposure is
shown only as context.

Every qualifying alert is posted: GDACS Red, then Orange, then the rest, newest
first within each. If more than 10 new alerts and updates qualify in one run,
nothing is posted and the run fails, as this usually means bad data or
thresholds. Updates are sent
only for a higher GDACS level or a new reported death-toll order of magnitude
(10, 100, 1,000).

State is stored in `posted_montandon.json`. Live runs stop if state is missing
or invalid, or if a Montandon search fails. Delivery is at-least-once: an alert
may be reposted if Slack accepts it but the updated state is not pushed.

### Going live

1. Add the `MONTANDON_API_TOKEN` secret (an IFRC GO API token).
2. Run the workflow once with `seed_state` enabled to record the current alerts.
3. Set the `MONTANDON_DRY_RUN` repository variable to `false`.

```bash
pip install requests
python -m unittest test_montandon.py
MONTANDON_API_TOKEN='...' DRY_RUN=true python montandon-slack.py
```
