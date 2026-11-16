# siriusxm-scraper

Logs which artists play on chosen SiriusXM stations into SQLite, writes CSV
reports, and serves a small web page to browse them.

## Sources

- **xmplaylist.com:** the public station page (`/station/<slug>`), which lists
  the last ~2 hours of songs. Only the HTML page is read, because xmplaylist's
  `robots.txt` disallows its `/api/`.
- **siriusxm.com:** the now-playing feed for a channel, which returns only the
  song on air now.

The same song seen by both sources is stored once.

## Setup

Requires [uv](https://docs.astral.sh/uv/) and Python 3.13+.

```sh
uv sync
```

`config.toml` sets the data folder, the timezone used for daily reports, and
the starting station list. After the first run, stations are managed from the
web page.

## Usage

```sh
uv run sxm-artists                      # poll all sources once, store new songs, rebuild reports
uv run sxm-artists --source xmplaylist  # poll one source (xmplaylist or siriusxm)
uv run sxm-artists --report-only        # rebuild the CSVs without fetching
uv run sxm-web                          # web viewer on http://0.0.0.0:8642
```

To keep a full log, run `sxm-artists --source xmplaylist` at least every 90
minutes, and `--source siriusxm` every couple of minutes if you want it too,
with cron, launchd or similar.

## Output

Everything goes in `data/`:

- `plays.db`: every song, with time, title, artists and source
- `artists.csv`: play counts per station and artist
- `covers.csv`: cover songs, shown as "Band vs Original Artist", split into the
  covering band and the original artist
- `daily/YYYY-MM-DD.csv`: per-day artist counts

## Web viewer

`sxm-web` has three tabs:

- **Songs:** search and sort everything logged
- **Cover songs:** the covers list
- **Stations:** add, remove, pause, or fetch a station right away

`/api/homepage` returns totals as JSON for a
[Homepage](https://gethomepage.dev) `customapi` widget.

The viewer has no login. Anyone who can reach it can change stations, so run
it only on a trusted network.
