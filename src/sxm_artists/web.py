"""LAN web viewer for the plays database: Songs, Cover songs and Stations tabs.

Run with `uv run sxm-web` (default http://0.0.0.0:8642). Viewing is read-only.
The Stations tab writes only the `stations` table (and the plays already on a
new station's page); the scraper reads that table at the start of each run.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from datetime import UTC, datetime, timedelta

import requests

from . import (
    DEFAULT_CONFIG, USER_AGENT, check_siriusxm_id, fetch_siriusxm, fetch_xmplaylist,
    iso_utc, load_config, lookup_xmplaylist_station, open_db, parse_utc, seed_stations,
    split_cover, store, write_reports,
)

PAGE_SIZE = 200
# "Fetch now" may hit xmplaylist for a station at most this often.
FETCH_COOLDOWN_SECONDS = 60

# Sortable columns: the page's sort key -> SQL. Ties fall back to newest first.
PLAYS_SORT = {
    "time": "p.played_at",
    "title": "p.title COLLATE NOCASE",
    "artist": "p.artists COLLATE NOCASE",
    "station": "p.station",
    "source": "p.source",
}
COVERS_SORT = {
    "time": "played_at",
    "title": "title COLLATE NOCASE",
    "cover": "cover_artist COLLATE NOCASE",
    "original": "original_artist COLLATE NOCASE",
    "station": "station",
    "shown": "artist_string COLLATE NOCASE",
}


def order_by(columns: dict[str, str], sort: str, direction: str, time_col: str) -> str:
    col = columns.get(sort, time_col)
    d = "ASC" if direction == "asc" else "DESC"
    return f"ORDER BY {col} {d}, {time_col} DESC"


def like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


class Viewer:
    def __init__(self, db_path: Path, tz: ZoneInfo):
        self.db_path = db_path
        self.tz = tz
        self.write_lock = threading.Lock()
        self.last_fetch: dict[str, float] = {}  # slug -> monotonic time of last Fetch now
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def connect(self) -> sqlite3.Connection:
        return sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True, timeout=10)

    def local(self, played_at: str) -> str:
        return parse_utc(played_at).astimezone(self.tz).isoformat(timespec="seconds")

    def plays(self, q: str, station: str, offset: int, sort: str = "time",
              direction: str = "desc") -> dict:
        where, args = [], []
        if q:
            where.append("(title LIKE ? ESCAPE '\\' OR artists LIKE ? ESCAPE '\\')")
            args += [like(q), like(q)]
        if station:
            where.append("station = ?")
            args.append(station)
        sql_where = f"WHERE {' AND '.join(where)}" if where else ""
        with self.connect() as db:
            total = db.execute(f"SELECT COUNT(*) FROM plays {sql_where}", args).fetchone()[0]
            rows = db.execute(
                f"""SELECT p.station, p.played_at, p.title, p.artists, p.source,
                           c.played_at IS NOT NULL
                    FROM (SELECT * FROM plays {sql_where}) p
                    LEFT JOIN covers c USING (station, played_at, title)
                    {order_by(PLAYS_SORT, sort, direction, "p.played_at")}
                    LIMIT ? OFFSET ?""",
                [*args, PAGE_SIZE, offset],
            ).fetchall()
        return {
            "total": total,
            "rows": [
                {"station": s, "played_at": self.local(t), "title": ti, "artists": a,
                 "source": src, "cover": bool(cv)}
                for s, t, ti, a, src, cv in rows
            ],
        }

    def covers(self, q: str, station: str, sort: str = "time",
               direction: str = "desc") -> dict:
        where, args = [], []
        if q:
            where.append("(title LIKE ? ESCAPE '\\' OR artist_string LIKE ? ESCAPE '\\')")
            args += [like(q), like(q)]
        if station:
            where.append("station = ?")
            args.append(station)
        sql_where = f"WHERE {' AND '.join(where)}" if where else ""
        with self.connect() as db:
            rows = db.execute(
                f"""SELECT station, played_at, title, cover_artist, original_artist,
                           artist_string FROM covers {sql_where}
                    {order_by(COVERS_SORT, sort, direction, "played_at")}""",
                args,
            ).fetchall()
        return {
            "total": len(rows),
            "rows": [
                {"station": s, "played_at": self.local(t), "title": ti, "cover_artist": ca,
                 "original_artist": oa, "artist_string": a}
                for s, t, ti, ca, oa, a in rows
            ],
        }

    def summary(self) -> dict:
        with self.connect() as db:
            # Every station with plays or currently scraped, with display names.
            stations = [
                {"slug": slug, "name": name or slug}
                for slug, name in db.execute(
                    """SELECT slug, MAX(name) FROM (
                         SELECT slug, name FROM stations
                         UNION ALL SELECT DISTINCT station, NULL FROM plays)
                       GROUP BY slug ORDER BY COALESCE(MAX(name), slug)"""
                )
            ]
            plays, last = db.execute("SELECT COUNT(*), MAX(played_at) FROM plays").fetchone()
            covers = db.execute("SELECT COUNT(*) FROM covers").fetchone()[0]
            scraped = db.execute("SELECT COUNT(*) FROM stations").fetchone()[0]
        return {"stations": stations, "plays": plays, "covers": covers, "scraped": scraped,
                "last": self.local(last) if last else None}

    def stations(self) -> dict:
        with self.connect() as db:
            rows = db.execute(
                """SELECT s.slug, s.name, s.siriusxm_id, s.added_at, s.enabled,
                          (SELECT COUNT(*) FROM plays p WHERE p.station = s.slug),
                          (SELECT MAX(played_at) FROM plays p WHERE p.station = s.slug)
                   FROM stations s ORDER BY COALESCE(s.name, s.slug)"""
            ).fetchall()
        return {"rows": [
            {"slug": slug, "name": name or slug, "siriusxm_id": sx,
             "added_at": self.local(added), "enabled": bool(en), "plays": n,
             "last_play": self.local(last) if last else None}
            for slug, name, sx, added, en, n, last in rows
        ]}

    def add_station(self, slug: str, siriusxm_id: str) -> dict:
        """Validate a station against xmplaylist (and SiriusXM), then save it.

        Stores the plays already on the station's page so it shows up at once.
        With no SiriusXM id given, the slug itself is tried as one.
        """
        slug, siriusxm_id = slug.strip().lower(), siriusxm_id.strip().lower()
        name, plays = lookup_xmplaylist_station(self.session, slug)
        note = ""
        if siriusxm_id:
            check_siriusxm_id(self.session, siriusxm_id)
        else:
            try:
                check_siriusxm_id(self.session, slug)
                siriusxm_id = slug
            except (ValueError, requests.RequestException):
                siriusxm_id = None
                note = ("No SiriusXM id: the xmplaylist page will be scraped, but not "
                        "SiriusXM's now-playing feed.")
        with self.write_lock:
            db = open_db(self.db_path)
            try:
                if db.execute("SELECT 1 FROM stations WHERE slug = ?", (slug,)).fetchone():
                    raise ValueError(f"{name} is already being scraped")
                db.execute(
                    "INSERT INTO stations (slug, name, siriusxm_id, added_at) VALUES (?, ?, ?, ?)",
                    (slug, name, siriusxm_id, iso_utc(datetime.now(UTC))),
                )
                db.commit()
                new = store(db, slug, "xmplaylist", plays)
            finally:
                db.close()
        return {"slug": slug, "name": name, "siriusxm_id": siriusxm_id,
                "stored": new, "note": note}

    def remove_station(self, slug: str) -> dict:
        """Stop scraping a station. Its recorded plays are kept."""
        with self.write_lock:
            db = open_db(self.db_path)
            try:
                gone = db.execute("DELETE FROM stations WHERE slug = ?", (slug,)).rowcount
                db.commit()
            finally:
                db.close()
        if not gone:
            raise LookupError(f"no station {slug!r}")
        return {"removed": slug}

    def set_enabled(self, slug: str, enabled: bool) -> dict:
        """Turn scraping of a station on or off; it and its plays are kept."""
        with self.write_lock:
            db = open_db(self.db_path)
            try:
                n = db.execute("UPDATE stations SET enabled = ? WHERE slug = ?",
                               (int(enabled), slug)).rowcount
                db.commit()
            finally:
                db.close()
        if not n:
            raise LookupError(f"no station {slug!r}")
        return {"slug": slug, "enabled": enabled}

    def fetch_now(self, slug: str) -> dict:
        """Poll one station's sources right away (works for disabled ones too)."""
        with self.connect() as db:
            row = db.execute("SELECT name, siriusxm_id FROM stations WHERE slug = ?",
                             (slug,)).fetchone()
        if not row:
            raise LookupError(f"no station {slug!r}")
        name, sx_id = row
        wait = FETCH_COOLDOWN_SECONDS - (time.monotonic() - self.last_fetch.get(slug, -1e9))
        if wait > 0:
            raise ValueError(f"{name or slug} was just fetched; try again in {int(wait) + 1}s")
        self.last_fetch[slug] = time.monotonic()

        result: dict = {"slug": slug, "name": name or slug}
        xm_plays = fetch_xmplaylist(self.session, slug)
        if not xm_plays:
            raise ValueError("no songs found on the xmplaylist page (layout changed?)")
        now_playing = []
        if sx_id:
            try:
                now_playing = fetch_siriusxm(self.session, sx_id)
            except (ValueError, requests.RequestException) as e:
                result["siriusxm_error"] = str(e)
        with self.write_lock:
            db = open_db(self.db_path)
            try:
                result["xmplaylist_new"] = store(db, slug, "xmplaylist", xm_plays)
                result["siriusxm_new"] = store(db, slug, "siriusxm", now_playing)
                write_reports(db, self.db_path.parent, self.tz)
            finally:
                db.close()
        result["xmplaylist_seen"] = len(xm_plays)
        if now_playing:
            p = now_playing[0]
            result["now_playing"] = f"{' / '.join(p['artists'])} - {p['title']}"
        return result

    def homepage(self) -> dict:
        """Stats for a gethomepage.dev `customapi` widget.

        Artists are counted case-insensitively. For a cover ('X vs Y') the
        performing band X counts, not the original artist or the 'vs' string.
        New artists are those whose first play is in the last 7 days.
        """
        cutoff = iso_utc(datetime.now(UTC) - timedelta(days=7))
        first_seen: dict[str, str] = {}
        with self.connect() as db:
            for artist, played_at in db.execute(
                "SELECT artist, played_at FROM play_artists ORDER BY played_at"
            ):
                parts = split_cover(artist)
                first_seen.setdefault((parts[0] if parts else artist).casefold(), played_at)
            plays, covers = db.execute(
                "SELECT (SELECT COUNT(*) FROM plays), (SELECT COUNT(*) FROM covers)"
            ).fetchone()
        return {
            "total_artists": len(first_seen),
            "total_covers": covers,
            "new_artists_week": sum(t >= cutoff for t in first_seen.values()),
            "total_plays": plays,
        }


def make_handler(viewer: Viewer):
    page = (Path(__file__).parent / "web.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            qs = {k: v[0] for k, v in parse_qs(url.query).items()}
            q, station = qs.get("q", "").strip(), qs.get("station", "")
            sort, direction = qs.get("sort", "time"), qs.get("dir", "desc")
            try:
                if url.path == "/":
                    return self.send(200, page, "text/html; charset=utf-8")
                if url.path == "/api/plays":
                    data = viewer.plays(q, station, max(0, int(qs.get("offset", 0))),
                                        sort, direction)
                elif url.path == "/api/covers":
                    data = viewer.covers(q, station, sort, direction)
                elif url.path == "/api/homepage":
                    data = viewer.homepage()
                elif url.path == "/api/stations":
                    data = viewer.stations()
                elif url.path == "/api/summary":
                    data = viewer.summary()
                else:
                    return self.send(404, b"not found", "text/plain")
            except (ValueError, sqlite3.Error) as e:
                return self.send(500, json.dumps({"error": str(e)}).encode(),
                                 "application/json")
            self.send(200, json.dumps(data).encode(), "application/json")

        def json_body(self) -> dict:
            # Requiring a JSON content type keeps other websites from posting
            # here with a plain HTML form (browsers preflight JSON requests).
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise ValueError("expected application/json")
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(min(n, 10_000)) or b"{}")

        def write(self, fn) -> None:
            try:
                data = fn()
            except LookupError as e:
                return self.send(404, json.dumps({"error": str(e)}).encode(), "application/json")
            except (ValueError, requests.RequestException) as e:
                return self.send(400, json.dumps({"error": str(e)}).encode(), "application/json")
            except sqlite3.Error as e:
                return self.send(500, json.dumps({"error": str(e)}).encode(), "application/json")
            self.send(200, json.dumps(data).encode(), "application/json")

        def station_path(self, suffix: str = "") -> str | None:
            """The slug in /api/stations/<slug><suffix>, or None if it doesn't match."""
            path, prefix = urlparse(self.path).path, "/api/stations/"
            if not (path.startswith(prefix) and path.endswith(suffix)):
                return None
            slug = path[len(prefix):len(path) - len(suffix)]
            return slug if slug and "/" not in slug else None

        def do_PATCH(self) -> None:
            slug = self.station_path()
            if not slug:
                return self.send(404, b"not found", "text/plain")

            def patch():
                body = self.json_body()
                if not isinstance(body.get("enabled"), bool):
                    raise ValueError("expected {\"enabled\": true|false}")
                return viewer.set_enabled(slug, body["enabled"])
            self.write(patch)

        def do_POST(self) -> None:
            if slug := self.station_path("/fetch"):
                def fetch():
                    self.json_body()  # content-type check only
                    return viewer.fetch_now(slug)
                return self.write(fetch)
            if urlparse(self.path).path != "/api/stations":
                return self.send(404, b"not found", "text/plain")

            def add():
                body = self.json_body()
                return viewer.add_station(str(body.get("slug", "")),
                                          str(body.get("siriusxm_id", "")))
            self.write(add)

        def do_DELETE(self) -> None:
            slug = self.station_path()
            if not slug:
                return self.send(404, b"not found", "text/plain")
            self.write(lambda: viewer.remove_station(slug))

        def log_message(self, *args) -> None:  # keep the log quiet
            pass

    return Handler


def main() -> None:
    parser = argparse.ArgumentParser(description="LAN web viewer for sxm-artists")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--host", default="0.0.0.0", help="bind address (default: all)")
    parser.add_argument("--port", type=int, default=8642)
    args = parser.parse_args()

    cfg = load_config(args.config)
    data_dir = Path(cfg["data_dir"])
    if not data_dir.is_absolute():
        data_dir = args.config.resolve().parent / data_dir
    db = open_db(data_dir / "plays.db")  # create/migrate before read-only use
    seed_stations(db, cfg)
    db.close()
    viewer = Viewer(data_dir / "plays.db", ZoneInfo(cfg["timezone"]))
    server = ThreadingHTTPServer((args.host, args.port), make_handler(viewer))
    print(f"sxm-web serving {viewer.db_path} on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()
