"""SQLite storage for survey exposures and pairing coverage.

Data model
----------
An *exposure* is identified by ``(night, exposure_no)`` and carries one UTC
timestamp and one band.  The same exposure can cover several grid cells, so
``exposure_grids`` is a many-to-many link.  A CSV row is an (exposure, grid)
association plus the exposure's own attributes.

Pairing rule
------------
A (grid, night) is usable when the grid has at least one *valid* g exposure and
one *valid* r exposure on that night whose UTC timestamps differ by at most
30 minutes.  When several pairs qualify the closest one is chosen, with a
deterministic tie-break on exposure id.

Consistency
-----------
* Every mutation runs inside ``BEGIN IMMEDIATE``, so concurrent writers are
  serialised by SQLite itself; there is no read-modify-write race between
  fetching and updating an exposure.
* Coverage is derived data: ``recompute_grid_night`` rebuilds it from the
  authoritative ``exposures``/``exposure_grids`` rows in the same transaction.
  Toggling one exposure re-derives every (grid, night) that exposure covers on
  that night — and nothing else.
* Readers use their own connections (WAL), so a rolled-back correction never
  hides previously committed exposure state or coverage.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterable, Iterator

PAIR_WINDOW_SECONDS = 30 * 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS exposures (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    night        TEXT    NOT NULL,
    exposure_no  TEXT    NOT NULL,
    utc_time     TEXT    NOT NULL,          -- canonical ISO-8601 UTC
    band         TEXT    NOT NULL CHECK (band IN ('g', 'r')),
    valid        INTEGER NOT NULL DEFAULT 1 CHECK (valid IN (0, 1)),
    updated_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (night, exposure_no)
);

CREATE INDEX IF NOT EXISTS idx_exposures_band ON exposures (band, valid);

-- An exposure can cover more than one grid cell.
CREATE TABLE IF NOT EXISTS exposure_grids (
    exposure_id INTEGER NOT NULL REFERENCES exposures (id) ON DELETE CASCADE,
    grid_id     TEXT    NOT NULL,
    PRIMARY KEY (exposure_id, grid_id)
);

CREATE INDEX IF NOT EXISTS idx_exposure_grids_grid ON exposure_grids (grid_id);

-- Cached pairing outcome for one (grid, night); rebuilt transactionally.
CREATE TABLE IF NOT EXISTS coverage (
    grid_id      TEXT    NOT NULL,
    night        TEXT    NOT NULL,
    g_exposure   INTEGER REFERENCES exposures (id),
    r_exposure   INTEGER REFERENCES exposures (id),
    pair_seconds INTEGER,
    status       TEXT    NOT NULL CHECK (status IN ('paired', 'unpaired')),
    reason       TEXT,
    PRIMARY KEY (grid_id, night)
);

CREATE TABLE IF NOT EXISTS imports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT    NOT NULL,
    imported_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    inserted    INTEGER NOT NULL,
    linked      INTEGER NOT NULL,
    skipped     INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    actor       TEXT    NOT NULL DEFAULT 'data-steward',
    action      TEXT    NOT NULL,
    exposure_id INTEGER,
    grid_id     TEXT,
    night       TEXT,
    detail      TEXT
);
"""


def db_path() -> str:
    return os.environ.get("COVERAGE_DB", os.path.join(os.getcwd(), "data", "coverage.sqlite3"))


def _sqlite_uri(path: str, mode: str) -> str:
    # ``mode=rw`` refuses to create a missing file, so a lost database surfaces
    # as an error instead of an empty board.  ``rwc`` is used only at startup.
    return f"file:{os.path.abspath(path)}?mode={mode}"


def connect(path: str | None = None, mode: str = "rwc") -> sqlite3.Connection:
    """Open the database.

    ``mode='rwc'`` (default) creates the file and parent directory.
    ``mode='rw'`` is used by request handlers: a missing file is an error
    rather than a silently empty board.
    """
    target = path or db_path()
    if mode == "rwc":
        os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    conn = sqlite3.connect(
        _sqlite_uri(target, mode), uri=True, timeout=30.0, isolation_level=None
    )
    conn.row_factory = sqlite3.Row
    # busy_timeout first: switching journal mode can itself contend for a lock
    # when several gunicorn workers initialise concurrently.
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = FULL")
    return conn


def open_db(path: str | None = None) -> sqlite3.Connection:
    """Connection for request handling; requires an initialised database."""
    return connect(path, mode="rw")


def init_db(path: str | None = None) -> None:
    conn = connect(path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Serialisable write transaction.  Rolls back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


class ImportConflict(ValueError):
    """A CSV row contradicts an already stored exposure."""


def parse_utc(raw: str) -> str:
    """Accept a few common CSV time spellings and return canonical UTC ISO."""
    text = raw.strip()
    if not text:
        raise ValueError("empty UTC time")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y/%m/%d %H:%M:%S"):
            try:
                dt = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"unparseable UTC time: {raw!r}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _seconds_between(a: str, b: str) -> int:
    da = datetime.fromisoformat(a.replace("Z", "+00:00"))
    db = datetime.fromisoformat(b.replace("Z", "+00:00"))
    return int(abs((da - db).total_seconds()))


def _valid_exposures(conn: sqlite3.Connection, grid_id: str, night: str, band: str):
    return conn.execute(
        "SELECT e.id, e.utc_time, e.exposure_no "
        "FROM exposures e "
        "JOIN exposure_grids eg ON eg.exposure_id = e.id "
        "WHERE eg.grid_id = ? AND e.night = ? AND e.band = ? AND e.valid = 1 "
        "ORDER BY e.utc_time, e.id",
        (grid_id, night, band),
    ).fetchall()


def _pair_for_grid_night(conn: sqlite3.Connection, grid_id: str, night: str) -> dict:
    g_rows = _valid_exposures(conn, grid_id, night, "g")
    r_rows = _valid_exposures(conn, grid_id, night, "r")
    if not g_rows and not r_rows:
        return {"status": "unpaired", "reason": "当夜没有有效曝光", "g": None, "r": None, "seconds": None}
    if not g_rows:
        return {"status": "unpaired", "reason": "当夜没有有效 g 曝光", "g": None, "r": None, "seconds": None}
    if not r_rows:
        return {"status": "unpaired", "reason": "当夜没有有效 r 曝光", "g": None, "r": None, "seconds": None}

    best = None
    for g in g_rows:
        for r in r_rows:
            delta = _seconds_between(g["utc_time"], r["utc_time"])
            if delta > PAIR_WINDOW_SECONDS:
                continue
            key = (delta, g["id"], r["id"])
            if best is None or key < best[0]:
                best = (key, g, r, delta)
    if best is None:
        closest = min(
            (_seconds_between(g["utc_time"], r["utc_time"]), g, r)
            for g in g_rows
            for r in r_rows
        )
        return {
            "status": "unpaired",
            "reason": f"最近的有效 g/r 相隔 {closest[0] / 60:.1f} 分钟（上限 30 分钟）",
            "g": None,
            "r": None,
            "seconds": closest[0],
        }
    _, g, r, delta = best
    return {"status": "paired", "reason": None, "g": g, "r": r, "seconds": delta}


def recompute_grid_night(conn: sqlite3.Connection, grid_id: str, night: str) -> None:
    """Rebuild the cached coverage row for exactly one grid+night."""
    outcome = _pair_for_grid_night(conn, grid_id, night)
    conn.execute(
        "INSERT INTO coverage (grid_id, night, g_exposure, r_exposure, pair_seconds, status, reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (grid_id, night) DO UPDATE SET "
        "  g_exposure=excluded.g_exposure, r_exposure=excluded.r_exposure, "
        "  pair_seconds=excluded.pair_seconds, status=excluded.status, reason=excluded.reason",
        (
            grid_id,
            night,
            outcome["g"]["id"] if outcome["g"] else None,
            outcome["r"]["id"] if outcome["r"] else None,
            outcome["seconds"],
            outcome["status"],
            outcome["reason"],
        ),
    )


def recompute_all(conn: sqlite3.Connection) -> None:
    """Rebuild every coverage row and re-derive grid usage (repair entry point)."""
    pairs = conn.execute(
        "SELECT DISTINCT eg.grid_id, e.night FROM exposures e "
        "JOIN exposure_grids eg ON eg.exposure_id = e.id"
    ).fetchall()
    for row in pairs:
        recompute_grid_night(conn, row["grid_id"], row["night"])
    conn.execute(
        "DELETE FROM coverage WHERE (grid_id, night) NOT IN "
        "(SELECT eg.grid_id, e.night FROM exposures e "
        " JOIN exposure_grids eg ON eg.exposure_id = e.id)"
    )


def repair_views(conn: sqlite3.Connection) -> dict:
    """Ensure each stored exposure agrees with its coverage links, then rebuild.

    Returns a summary of anomalies found.  This is the offline repair path for
    a database written by an older schema or edited by hand.
    """
    with transaction(conn):
        orphan_links = conn.execute(
            "SELECT COUNT(*) AS n FROM exposure_grids WHERE exposure_id NOT IN "
            "(SELECT id FROM exposures)"
        ).fetchone()["n"]
        conn.execute(
            "DELETE FROM exposure_grids WHERE exposure_id NOT IN (SELECT id FROM exposures)"
        )
        recompute_all(conn)
        _audit(conn, "repair", detail=f"orphan_links_removed={orphan_links}")
    return {"orphan_links_removed": orphan_links}


def _audit(conn, action, exposure_id=None, grid_id=None, night=None, detail=None) -> None:
    conn.execute(
        "INSERT INTO audit_log (action, exposure_id, grid_id, night, detail) VALUES (?,?,?,?,?)",
        (action, exposure_id, grid_id, night, detail),
    )


def import_rows(conn: sqlite3.Connection, rows: Iterable[dict], filename: str) -> dict:
    """Import parsed CSV rows atomically, then recompute touched coverage.

    * A new ``(night, exposure_no)`` creates the exposure.
    * An existing exposure must agree on band and UTC time; otherwise the whole
      import is rejected with :class:`ImportConflict` and nothing changes.
    * An (exposure, grid) association already present counts as skipped.
    """
    inserted = 0
    linked = 0
    skipped = 0
    touched: set[tuple[str, str]] = set()
    with transaction(conn):
        for row in rows:
            existing = conn.execute(
                "SELECT id, utc_time, band FROM exposures WHERE night=? AND exposure_no=?",
                (row["night"], row["exposure_no"]),
            ).fetchone()
            if existing is None:
                cur = conn.execute(
                    "INSERT INTO exposures (night, exposure_no, utc_time, band, valid) "
                    "VALUES (?, ?, ?, ?, 1)",
                    (row["night"], row["exposure_no"], row["utc_time"], row["band"]),
                )
                exposure_id = cur.lastrowid
                inserted += 1
            else:
                exposure_id = existing["id"]
                if existing["band"] != row["band"] or existing["utc_time"] != row["utc_time"]:
                    raise ImportConflict(
                        f"第 {row.get('line', '?')} 行：曝光 {row['night']}/{row['exposure_no']} "
                        f"与已有记录不一致（已有 {existing['band']} {existing['utc_time']}，"
                        f"CSV 为 {row['band']} {row['utc_time']}）"
                    )
            cur = conn.execute(
                "INSERT OR IGNORE INTO exposure_grids (exposure_id, grid_id) VALUES (?, ?)",
                (exposure_id, row["grid_id"]),
            )
            if cur.rowcount:
                linked += 1
            else:
                skipped += 1
            touched.add((row["grid_id"], row["night"]))
        conn.execute(
            "INSERT INTO imports (filename, inserted, linked, skipped) VALUES (?, ?, ?, ?)",
            (filename, inserted, linked, skipped),
        )
        _audit(
            conn,
            "import",
            detail=f"{filename}: exposures={inserted} links={linked} skipped={skipped}",
        )
        for grid_id, night in sorted(touched):
            recompute_grid_night(conn, grid_id, night)
    return {"inserted": inserted, "linked": linked, "skipped": skipped}


def set_valid(conn: sqlite3.Connection, exposure_id: int, valid: bool) -> dict:
    """Toggle an exposure's validity and recheck every grid it covers that night.

    Raises ``LookupError`` if the exposure does not exist.  The whole update and
    all affected coverage rows commit together or not at all.
    """
    with transaction(conn):
        row = conn.execute(
            "SELECT id, night, exposure_no, band, utc_time, valid FROM exposures WHERE id=?",
            (exposure_id,),
        ).fetchone()
        if row is None:
            raise LookupError(f"exposure {exposure_id} not found")
        grids = [
            r["grid_id"]
            for r in conn.execute(
                "SELECT grid_id FROM exposure_grids WHERE exposure_id=? ORDER BY grid_id",
                (exposure_id,),
            ).fetchall()
        ]
        if bool(row["valid"]) == bool(valid):
            return {
                "changed": False,
                "exposure": dict(row),
                "grids": grids,
                "coverage": [_coverage_row(conn, g, row["night"]) for g in grids],
            }
        conn.execute(
            "UPDATE exposures SET valid=?, updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
            (1 if valid else 0, exposure_id),
        )
        for grid_id in grids:
            recompute_grid_night(conn, grid_id, row["night"])
            _audit(
                conn,
                "revalidate" if valid else "invalidate",
                exposure_id=exposure_id,
                grid_id=grid_id,
                night=row["night"],
                detail=f"{row['band']} {row['exposure_no']}",
            )
        return {
            "changed": True,
            "exposure": dict(row) | {"valid": 1 if valid else 0},
            "grids": grids,
            "coverage": [_coverage_row(conn, g, row["night"]) for g in grids],
        }


def _coverage_row(conn: sqlite3.Connection, grid_id: str, night: str) -> dict | None:
    row = conn.execute(
        "SELECT c.*, "
        " g.exposure_no AS g_no, g.utc_time AS g_time, "
        " r.exposure_no AS r_no, r.utc_time AS r_time "
        "FROM coverage c "
        "LEFT JOIN exposures g ON g.id = c.g_exposure "
        "LEFT JOIN exposures r ON r.id = c.r_exposure "
        "WHERE c.grid_id=? AND c.night=?",
        (grid_id, night),
    ).fetchone()
    return dict(row) if row else None


def _nights_for_grid(conn: sqlite3.Connection, grid_id: str) -> list[dict]:
    rows = conn.execute(
        "SELECT c.*, "
        " g.exposure_no AS g_no, g.utc_time AS g_time, "
        " r.exposure_no AS r_no, r.utc_time AS r_time "
        "FROM coverage c "
        "LEFT JOIN exposures g ON g.id = c.g_exposure "
        "LEFT JOIN exposures r ON r.id = c.r_exposure "
        "WHERE c.grid_id=? ORDER BY c.night",
        (grid_id,),
    ).fetchall()
    nights = []
    for row in rows:
        nights.append(
            {
                "night": row["night"],
                "usable": row["status"] == "paired",
                "g_exposure": row["g_exposure"],
                "r_exposure": row["r_exposure"],
                "g_no": row["g_no"],
                "r_no": row["r_no"],
                "g_time": row["g_time"],
                "r_time": row["r_time"],
                "seconds": row["pair_seconds"],
                "reason": row["reason"],
            }
        )
    return nights


def grid_summary(conn: sqlite3.Connection) -> list[dict]:
    """One row per grid with its usable nights and pair members."""
    grids = conn.execute(
        "SELECT DISTINCT grid_id FROM exposure_grids ORDER BY grid_id"
    ).fetchall()
    out = []
    for grid in grids:
        gid = grid["grid_id"]
        nights = _nights_for_grid(conn, gid)
        usable = sum(1 for n in nights if n["usable"])
        out.append(
            {
                "grid_id": gid,
                "nights": nights,
                "usable_nights": usable,
                "needs_more": usable < 2,
            }
        )
    return out


def grid_detail(conn: sqlite3.Connection, grid_id: str) -> dict | None:
    exposures = conn.execute(
        "SELECT e.*, "
        " (SELECT group_concat(grid_id, ',') FROM "
        "   (SELECT grid_id FROM exposure_grids WHERE exposure_id = e.id ORDER BY grid_id)"
        " ) AS grids "
        "FROM exposures e "
        "JOIN exposure_grids eg ON eg.exposure_id = e.id AND eg.grid_id = ? "
        "ORDER BY e.night, e.utc_time, e.band, e.id",
        (grid_id,),
    ).fetchall()
    if not exposures:
        return None
    for summary in grid_summary(conn):
        if summary["grid_id"] == grid_id:
            summary["exposures"] = [dict(e) for e in exposures]
            summary["audit"] = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM audit_log WHERE grid_id=? ORDER BY id DESC LIMIT 30",
                    (grid_id,),
                ).fetchall()
            ]
            return summary
    return None
