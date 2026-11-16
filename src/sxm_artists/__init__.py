"""Collect artist names played on chosen SiriusXM stations.

Two sources feed one SQLite database:

* xmplaylist  - the public station page (https://xmplaylist.com/station/<slug>)
                lists the ~24 most recent plays (~1.5-2h). xmplaylist's
                robots.txt disallows /api/, so only the HTML page is read.
                Poll hourly.
* siriusxm    - SiriusXM's now-playing feed
                (https://www.siriusxm.com/api/mountain/<channel id>) returns
                only the song on air right now. Poll every ~2 minutes.

The same play seen by both sources is merged (same station, same normalized
title, start times within MATCH_WINDOW). CSV reports are rebuilt every run.
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
import time
import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = PROJECT_ROOT / "config.toml"
XMPLAYLIST_URL = "https://xmplaylist.com/station/{slug}"
SIRIUSXM_URL = "https://www.siriusxm.com/api/mountain/{channel}"
USER_AGENT = "sxm-artists/0.2 (personal playlist logger)"
REQUEST_DELAY_SECONDS = 3
# siriusxm.com start times run a consistent ~3 min behind xmplaylist's
# (observed 2026-11-08 on Liquid Metal), so the window must exceed that.
MATCH_WINDOW = timedelta(minutes=5)
SOURCES = ("xmplaylist", "siriusxm")

SCHEMA = """
CREATE TABLE IF NOT EXISTS plays (
    station   TEXT NOT NULL,  -- xmplaylist slug from config.toml
    played_at TEXT NOT NULL,  -- UTC, 'YYYY-MM-DDTHH:MM:SS.fffZ'
    title     TEXT NOT NULL,
    artists   TEXT NOT NULL,  -- artist names joined with ' / '
    source    TEXT,           -- source that first recorded the play
    PRIMARY KEY (station, played_at, title)
);
CREATE TABLE IF NOT EXISTS play_artists (
    station   TEXT NOT NULL,
    played_at TEXT NOT NULL,
    title     TEXT NOT NULL,
    artist    TEXT NOT NULL,
    PRIMARY KEY (station, played_at, title, artist)
);
CREATE INDEX IF NOT EXISTS plays_by_time ON plays (station, played_at);
-- Stations to scrape. Managed from the web viewer; seeded from config.toml
-- the first time the table is empty.
CREATE TABLE IF NOT EXISTS stations (
    slug        TEXT PRIMARY KEY,  -- xmplaylist slug, the station key everywhere
    name        TEXT,              -- display name from the xmplaylist page
    siriusxm_id TEXT,              -- SiriusXM contentId for the now-playing feed
    added_at    TEXT NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1  -- 0 = kept, but not scraped
);
-- Cover songs (e.g. Liquid Metal's "Corridor of Covers"): xmplaylist shows
-- them as one artist string '<covering band> vs <original artist>'.
CREATE TABLE IF NOT EXISTS covers (
    station         TEXT NOT NULL,
    played_at       TEXT NOT NULL,
    title           TEXT NOT NULL,
    cover_artist    TEXT NOT NULL,  -- band performing the cover (left of 'vs')
    original_artist TEXT NOT NULL,  -- original artist (right of 'vs')
    artist_string   TEXT NOT NULL,  -- full artist string as shown, as a backup
    PRIMARY KEY (station, played_at, title)
);
"""

# 'Blood Of The Earth vs Ministry' -> ('Blood Of The Earth', 'Ministry')
COVER_RE = re.compile(r"^(.+?)\s+vs\.?\s+(.+)$", re.IGNORECASE)


def iso_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def norm_title(title: str) -> str:
    """'Night Crawler (Remastered 2024)' -> 'night crawler'"""
    t = re.sub(r"[(\[].*?[)\]]", "", title.casefold())
    return re.sub(r"[^\w]+", " ", t).strip()


# --------------------------------------------------------------------------- sources


def fetch_xmplaylist(session: requests.Session, slug: str) -> list[dict]:
    """Plays currently listed on an xmplaylist station page, newest first."""
    resp = session.get(XMPLAYLIST_URL.format(slug=slug), timeout=30)
    resp.raise_for_status()
    return parse_xmplaylist_html(resp.text)


def parse_xmplaylist_html(html: str) -> list[dict]:
    """Plays in an xmplaylist station page's HTML (fetched, or saved from a browser)."""
    soup = BeautifulSoup(html, "html.parser")

    plays = []
    for article in soup.find_all("article"):
        time_el = article.find("time")
        title_el = article.find("h3")
        artists_el = article.find("ul", id=lambda v: bool(v) and v.endswith("-artists"))
        if not (time_el and title_el and artists_el):
            continue
        artists = [li.get_text(strip=True) for li in artists_el.find_all("li")]
        artists = [a for a in artists if a]
        if not artists:
            continue
        plays.append(
            {
                "played_at": iso_utc(parse_utc(time_el["datetime"])),
                "title": title_el.get_text(strip=True),
                "artists": artists,
            }
        )
    return plays


def fetch_siriusxm(session: requests.Session, channel: str) -> list[dict]:
    """The song on air right now (0 or 1 plays). Talk/ads/empty -> []."""
    resp = session.get(SIRIUSXM_URL.format(channel=channel), timeout=30)
    resp.raise_for_status()
    data = resp.json()
    ch = data.get("channels", {}).get(channel)
    if not ch:
        raise ValueError(f"channel {channel!r} missing from response")
    if "siriuschannelnumber" not in ch:
        # Unknown IDs come back as an all-empty stub; flag it so config gets fixed.
        raise ValueError(f"{channel!r} is not a valid SiriusXM channel id")
    content = ch.get("content") or {}
    artists = [a.get("name", "").strip() for a in content.get("artists") or []]
    artists = [a for a in artists if a]
    title = (content.get("title") or "").strip()
    if content.get("type") != "Song" or not title or not artists or not content.get("starttime"):
        return []
    started = datetime.fromtimestamp(content["starttime"] / 1000, UTC)
    return [{"played_at": iso_utc(started), "title": title, "artists": artists}]


SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def lookup_xmplaylist_station(session: requests.Session, slug: str) -> tuple[str, list[dict]]:
    """(display name, current plays) for a slug; ValueError if it is not a station."""
    if not SLUG_RE.match(slug):
        raise ValueError("slug may only contain lowercase letters, digits and dashes")
    resp = session.get(XMPLAYLIST_URL.format(slug=slug), timeout=30)
    if resp.status_code == 404:
        raise ValueError(f"xmplaylist has no station {slug!r}")
    resp.raise_for_status()
    plays = parse_xmplaylist_html(resp.text)
    if not plays:
        raise ValueError(f"no plays found on the xmplaylist page for {slug!r}")
    h1 = BeautifulSoup(resp.text, "html.parser").find("h1")
    return (h1.get_text(strip=True) if h1 else slug), plays


def check_siriusxm_id(session: requests.Session, channel: str) -> None:
    """ValueError unless `channel` is a valid SiriusXM contentId."""
    if not SLUG_RE.match(channel):
        raise ValueError("SiriusXM id may only contain lowercase letters, digits and dashes")
    fetch_siriusxm(session, channel)


# --------------------------------------------------------------------------- storage


def open_db(path: Path) -> sqlite3.Connection:
    db = sqlite3.connect(path, timeout=30)  # two schedules may overlap briefly
    db.executescript(SCHEMA)
    cols = {r[1] for r in db.execute("PRAGMA table_info(plays)")}
    if "source" not in cols:  # databases created by v0.1
        db.execute("ALTER TABLE plays ADD COLUMN source TEXT")
        db.execute("UPDATE plays SET source = 'xmplaylist'")
        db.commit()
    if "enabled" not in {r[1] for r in db.execute("PRAGMA table_info(stations)")}:
        db.execute("ALTER TABLE stations ADD COLUMN enabled INTEGER NOT NULL DEFAULT 1")
        db.commit()
    # Pick up covers among plays stored before the covers table existed.
    for row in db.execute("SELECT station, played_at, title, artist FROM play_artists").fetchall():
        record_cover(db, *row)
    db.commit()
    return db


def seed_stations(db: sqlite3.Connection, cfg: dict) -> None:
    """Fill an empty stations table from config.toml (first run only)."""
    if db.execute("SELECT 1 FROM stations LIMIT 1").fetchone():
        return
    now = iso_utc(datetime.now(UTC))
    db.executemany(
        "INSERT INTO stations (slug, siriusxm_id, added_at) VALUES (?, ?, ?)",
        [(s, cfg["siriusxm_ids"].get(s), now) for s in cfg.get("stations", [])],
    )
    db.commit()


def station_list(db: sqlite3.Connection) -> list[tuple[str, str | None]]:
    """(slug, siriusxm_id) for every enabled station."""
    return db.execute(
        "SELECT slug, siriusxm_id FROM stations WHERE enabled ORDER BY slug"
    ).fetchall()


def already_recorded(db: sqlite3.Connection, station: str, play: dict) -> bool:
    t = parse_utc(play["played_at"])
    rows = db.execute(
        "SELECT title FROM plays WHERE station = ? AND played_at BETWEEN ? AND ?",
        (station, iso_utc(t - MATCH_WINDOW), iso_utc(t + MATCH_WINDOW)),
    )
    want = norm_title(play["title"])
    return any(norm_title(title) == want for (title,) in rows)


def split_cover(artist_string: str) -> tuple[str, str] | None:
    m = COVER_RE.match(artist_string.strip())
    return (m[1].strip(), m[2].strip()) if m else None


def record_cover(db: sqlite3.Connection, station: str, played_at: str, title: str,
                 artist_string: str) -> None:
    parts = split_cover(artist_string)
    if parts:
        db.execute(
            "INSERT OR IGNORE INTO covers VALUES (?, ?, ?, ?, ?, ?)",
            (station, played_at, title, *parts, artist_string),
        )


def store(db: sqlite3.Connection, station: str, source: str, plays: list[dict]) -> int:
    new = 0
    for p in plays:
        if already_recorded(db, station, p):
            continue
        db.execute(
            "INSERT INTO plays VALUES (?, ?, ?, ?, ?)",
            (station, p["played_at"], p["title"], " / ".join(p["artists"]), source),
        )
        for a in p["artists"]:
            record_cover(db, station, p["played_at"], p["title"], a)
        db.executemany(
            "INSERT OR IGNORE INTO play_artists VALUES (?, ?, ?, ?)",
            [(station, p["played_at"], p["title"], a) for a in p["artists"]],
        )
        new += 1
    db.commit()
    return new


def write_reports(db: sqlite3.Connection, data_dir: Path, tz: ZoneInfo) -> None:
    """Rebuild artists.csv (all-time) and daily/<date>.csv (per local day)."""
    rows = db.execute(
        "SELECT station, played_at, artist FROM play_artists ORDER BY played_at"
    ).fetchall()

    alltime: dict[tuple[str, str], dict] = {}
    daily: dict[str, dict[tuple[str, str], int]] = {}
    for station, played_at, artist in rows:
        local = parse_utc(played_at).astimezone(tz)
        day = local.date().isoformat()
        stamp = local.isoformat(timespec="minutes")

        rec = alltime.setdefault(
            (station, artist), {"plays": 0, "first_seen": stamp, "last_seen": stamp}
        )
        rec["plays"] += 1
        rec["last_seen"] = stamp

        bucket = daily.setdefault(day, {})
        bucket[(station, artist)] = bucket.get((station, artist), 0) + 1

    with (data_dir / "artists.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["station", "artist", "plays", "first_seen", "last_seen"])
        for (station, artist), rec in sorted(
            alltime.items(), key=lambda kv: (kv[0][0], kv[0][1].casefold())
        ):
            w.writerow([station, artist, rec["plays"], rec["first_seen"], rec["last_seen"]])

    with (data_dir / "covers.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["station", "played_at", "title", "cover_artist", "original_artist",
                    "artist_string"])
        for station, played_at, *rest in db.execute(
            "SELECT * FROM covers ORDER BY played_at"
        ):
            local = parse_utc(played_at).astimezone(tz).isoformat(timespec="minutes")
            w.writerow([station, local, *rest])

    daily_dir = data_dir / "daily"
    daily_dir.mkdir(exist_ok=True)
    for day, counts in daily.items():
        with (daily_dir / f"{day}.csv").open("w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["station", "artist", "plays"])
            for (station, artist), n in sorted(
                counts.items(), key=lambda kv: (kv[0][0], -kv[1], kv[0][1].casefold())
            ):
                w.writerow([station, artist, n])


# --------------------------------------------------------------------------- cli


def load_config(path: Path) -> dict:
    with path.open("rb") as f:
        cfg = tomllib.load(f)
    cfg.setdefault("siriusxm_ids", {})
    cfg.setdefault("data_dir", "data")
    cfg.setdefault("timezone", "America/New_York")
    return cfg


def targets(db: sqlite3.Connection, source: str) -> list[tuple[str, str]]:
    """(station, id-for-this-source) pairs."""
    stations = station_list(db)
    if source == "xmplaylist":
        return [(s, s) for s, _ in stations]
    return [(s, sx) for s, sx in stations if sx]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--source",
        choices=(*SOURCES, "all"),
        default="all",
        help="which source to poll (default: all)",
    )
    parser.add_argument(
        "--import-html", type=Path, metavar="FILE",
        help="store plays from a station page saved from a browser (e.g. after clicking "
             "'Load more'), then report; use with --station if several are configured",
    )
    parser.add_argument("--station", help="station slug for --import-html")
    parser.add_argument(
        "--report-only", action="store_true", help="rebuild CSVs without fetching"
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    data_dir = Path(cfg["data_dir"])
    if not data_dir.is_absolute():
        data_dir = args.config.resolve().parent / data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    tz = ZoneInfo(cfg["timezone"])
    db = open_db(data_dir / "plays.db")
    seed_stations(db, cfg)

    fetchers = {"xmplaylist": fetch_xmplaylist, "siriusxm": fetch_siriusxm}
    if args.import_html:
        slugs = [s for s, _ in station_list(db)]
        station = args.station or (slugs[0] if len(slugs) == 1 else None)
        if not station:
            sys.exit("--import-html needs --station when several stations are configured")
        plays = parse_xmplaylist_html(args.import_html.read_text(errors="replace"))
        if not plays:
            sys.exit(f"no plays parsed from {args.import_html}")
        new = store(db, station, "xmplaylist", plays)
        print(f"[import:{station}] {len(plays)} in file "
              f"({plays[-1]['played_at']} .. {plays[0]['played_at']}), {new} new")

    sources = [] if args.report_only or args.import_html else SOURCES if args.source == "all" else [args.source]

    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    failures = 0
    first = True
    for source in sources:
        for station, ident in targets(db, source):
            if not first:
                time.sleep(REQUEST_DELAY_SECONDS)
            first = False
            tag = f"{datetime.now():%Y-%m-%d %H:%M} [{source}:{station}]"
            try:
                plays = fetchers[source](session, ident)
            except (requests.RequestException, ValueError) as e:
                print(f"{tag} fetch failed: {e}", file=sys.stderr)
                failures += 1
                continue
            if source == "xmplaylist" and not plays:
                print(f"{tag} no plays parsed (page layout changed?)", file=sys.stderr)
                failures += 1
                continue
            new = store(db, station, source, plays)
            if source == "xmplaylist":
                print(f"{tag} {len(plays)} on page, {new} new (back to {plays[-1]['played_at']})")
            elif plays:
                p = plays[0]
                print(f"{tag} now: {' / '.join(p['artists'])} - {p['title']}"
                      f"{' (new)' if new else ''}")
            else:
                print(f"{tag} no song on air")

    write_reports(db, data_dir, tz)
    db.close()
    sys.exit(1 if failures else 0)
