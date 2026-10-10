# BOM warnings archive

A running log of weather warnings issued by the Australian Bureau of Meteorology:
when each warning starts and when it ends. It's checked twice a day (07:17 and
19:17 UTC) by a GitHub Action.

Warnings are read from the Bureau's public anonymous FTP feed
(`ftp://ftp.bom.gov.au/anon/gen/fwo/`). Every product XML there is fetched, and
the ones with product type `W` (warning) are compared against the stored set of
active warnings:

- a warning **starts** when a new warning ID appears;
- a warning **ends** when its file disappears from the feed, or it is reissued
  as cancelled (`CAN`, `FIN` or `EXP` phase).

Routine updates and reissues of a warning that is already active are ignored.
Because the feed is only checked twice a day, short-lived warnings that start
and end between checks won't be recorded, and start/end times are when the
change was detected, not when it happened.

## Data layout

- `data/active.json` — warnings currently active, keyed by product ID (e.g.
  `IDV20600`). Each record holds the state, title, headline, service, hazards
  and areas as they were when the warning first appeared, plus `issued` (the
  issue time of that first product) and `started` (when it was detected).
- `log/YYYY/MM.jsonl` — one JSON object per line for each event, with
  `"event": "start"` or `"event": "end"`. End events include a `reason`
  (`removed` or `cancelled`).

States are derived from the third letter of the product ID: N=NSW, V=VIC,
Q=QLD, S=SA, W=WA, T=TAS, D=NT, and Y/Z=national (`AUS`).

## Running locally

Python 3.9+ with no dependencies.

```sh
python3 bom_alerts.py            # update data/ and log/ in place
python3 bom_alerts.py --commit   # same, and record each event as a git commit
python3 -m unittest discover -s tests
```

If the FTP listing can't be fetched, or too many files fail to download, the
script exits without changing anything rather than assuming every warning has
ended.

## Attribution

Warning data is from the [Australian Bureau of Meteorology](http://www.bom.gov.au/)
and is subject to the Bureau's
[copyright notice](http://www.bom.gov.au/other/copyright.shtml). This project
is not affiliated with or endorsed by the Bureau. Don't rely on it for safety
information; always check current warnings directly with the Bureau.
