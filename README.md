# HOTOSM disaster alerts

Python scripts and GitHub Actions workflows that post new disasters and disease
outbreaks to the HOTOSM Slack `#disaster-alerts` channel.

`gdacs-slack.py` and `who-don-slack.py` are live. `montandon-slack.py` is a
preview of replacing the GDACS feed with the aggregated [IFRC Montandon](https://ifrcgo.org/monty-stac-extension/)
STAC API.

## Montandon alerts

Records from different sources are correlated into one disaster. An alert is
created for a GDACS Orange/Red level, an earthquake of M6+, winds of at least 64
kt/119 km/h, or an impact above the thresholds in `IMPACT_TRIGGERS`. Modelled
figures are labelled separately from reported impacts.

Posted alerts are tracked in `posted_montandon.json`. A new GDACS level or death
toll order of magnitude produces an update; unchanged alerts are skipped.

The workflow is a dry run by default. It requires the `MONTANDON_API_TOKEN`
secret and, when posting, `SLACK_WEBHOOK_URL`. Set the `MONTANDON_DRY_RUN`
repository variable to `false` to enable posting.

```bash
pip install requests
MONTANDON_API_TOKEN='...' DRY_RUN=true python montandon-slack.py
```
