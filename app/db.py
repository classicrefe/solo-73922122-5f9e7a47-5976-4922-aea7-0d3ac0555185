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

-- One photometric measurement of one target in one exposure.
--
-- The first four columns plus ``version`` are the logical key: the same
-- photometry *round* (version) is shared by every night and band, so a
-- comparison may only combine g/r magnitudes carrying the same version.
-- ``exposure_no`` is resolved to an exposure and must cover ``grid_id`` at
-- import time.
CREATE TABLE IF NOT EXISTS photometry (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    grid_id     TEXT    NOT NULL,
    target_id   TEXT    NOT NULL,
    night       TEXT    NOT NULL,
    exposure_no TEXT    NOT NULL,
    version     INTEGER NOT NULL CHECK (version > 0),
    magnitude   REAL    NOT NULL,
    imported_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (grid_id, target_id, night, exposure_no, version)
);

CREATE INDEX IF NOT EXISTS idx_photometry_lookup
    ON photometry (grid_id, target_id, night, exposure_no);

CREATE TABLE IF NOT EXISTS photometry_imports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    filename    TEXT    NOT NULL,
    imported_at TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    inserted    INTEGER NOT NULL,
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


@contextmanager
def read_snapshot(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One consistent read snapshot, so coverage and photometry agree.

    If the caller already owns a transaction this is a no-op and that
    transaction's snapshot is reused.
    """
    owns = not conn.in_transaction
    if owns:
        conn.execute("BEGIN")
    try:
        yield conn
    except BaseException:
        if owns:
            conn.execute("ROLLBACK")
        raise
    else:
        if owns:
            conn.execute("COMMIT")


class ImportConflict(ValueError):
    """A CSV row contradicts an already stored exposure."""


class PhotometryConflict(ValueError):
    """A photometry CSV batch contradicts stored photometry or coverage."""


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
            summary["targets"] = [
                r["target_id"]
                for r in conn.execute(
                    "SELECT DISTINCT target_id FROM photometry WHERE grid_id=? "
                    "ORDER BY target_id",
                    (grid_id,),
                ).fetchall()
            ]
            summary["photometry_imports"] = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM photometry_imports ORDER BY id DESC LIMIT 5"
                ).fetchall()
            ]
            return summary
    return None


# ---------------------------------------------------------------------------
# Photometry import
# ---------------------------------------------------------------------------

PHOTOMETRY_KEY = ("grid_id", "target_id", "night", "exposure_no", "version")


def import_photometry(
    conn: sqlite3.Connection, rows: Iterable[dict], filename: str
) -> dict:
    """Import photometry rows atomically.

    The parser has already validated numbers and deduplicated identical rows.
    Here every row is checked against the stored data:

    * the ``(night, exposure_no)`` exposure must exist **and** cover the grid;
    * a key already stored with the same value is skipped;
    * a key already stored with a different value rejects the whole batch and
      leaves every stored magnitude untouched.

    Returns ``{"inserted": n, "skipped": n}``.
    """
    inserted = 0
    skipped = 0
    with transaction(conn):
        for row in rows:
            link = conn.execute(
                "SELECT e.id FROM exposures e "
                "JOIN exposure_grids eg ON eg.exposure_id = e.id "
                "WHERE e.night = ? AND e.exposure_no = ? AND eg.grid_id = ?",
                (row["night"], row["exposure_no"], row["grid_id"]),
            ).fetchone()
            if link is None:
                raise PhotometryConflict(
                    f"第 {row.get('line', '?')} 行：曝光 {row['night']}/{row['exposure_no']} "
                    f"不存在或未覆盖网格 {row['grid_id']}"
                )
            existing = conn.execute(
                "SELECT id, magnitude FROM photometry WHERE grid_id=? AND target_id=? "
                "AND night=? AND exposure_no=? AND version=?",
                (
                    row["grid_id"],
                    row["target_id"],
                    row["night"],
                    row["exposure_no"],
                    row["version"],
                ),
            ).fetchone()
            if existing is not None:
                if existing["magnitude"] != row["magnitude"]:
                    raise PhotometryConflict(
                        f"第 {row.get('line', '?')} 行：网格 {row['grid_id']} 目标 "
                        f"{row['target_id']} {row['night']}/{row['exposure_no']} 版本 "
                        f"{row['version']} 已入库星等 {existing['magnitude']!r}，"
                        f"与本次 {row['magnitude']!r} 不同"
                    )
                skipped += 1
                continue
            conn.execute(
                "INSERT INTO photometry "
                "(grid_id, target_id, night, exposure_no, version, magnitude) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    row["grid_id"],
                    row["target_id"],
                    row["night"],
                    row["exposure_no"],
                    row["version"],
                    row["magnitude"],
                ),
            )
            inserted += 1
        conn.execute(
            "INSERT INTO photometry_imports (filename, inserted, skipped) VALUES (?, ?, ?)",
            (filename, inserted, skipped),
        )
        _audit(
            conn,
            "photometry_import",
            detail=f"{filename}: inserted={inserted} skipped={skipped}",
        )
    return {"inserted": inserted, "skipped": skipped}


# ---------------------------------------------------------------------------
# Night-to-night colour comparison
# ---------------------------------------------------------------------------


def _current_pair(conn: sqlite3.Connection, grid_id: str, night: str) -> dict | None:
    """The pairing currently on record, or ``None`` when the night is unusable."""
    row = conn.execute(
        "SELECT c.status, c.reason, "
        " g.id AS g_id, g.exposure_no AS g_no, g.valid AS g_valid, "
        " r.id AS r_id, r.exposure_no AS r_no, r.valid AS r_valid "
        "FROM coverage c "
        "LEFT JOIN exposures g ON g.id = c.g_exposure "
        "LEFT JOIN exposures r ON r.id = c.r_exposure "
        "WHERE c.grid_id=? AND c.night=?",
        (grid_id, night),
    ).fetchone()
    if row is None:
        return None
    if row["status"] != "paired" or row["g_no"] is None or row["r_no"] is None:
        return None
    if not row["g_valid"] or not row["r_valid"]:
        # A cached pair must never be used after one of its members was
        # invalidated; recompute would have replaced it, but stay defensive.
        return None
    return {"g_no": row["g_no"], "r_no": row["r_no"]}


def _versions_for_exposure(
    conn: sqlite3.Connection,
    grid_id: str,
    target_id: str,
    night: str,
    exposure_no: str,
) -> dict[int, float]:
    return {
        row["version"]: row["magnitude"]
        for row in conn.execute(
            "SELECT version, magnitude FROM photometry "
            "WHERE grid_id=? AND target_id=? AND night=? AND exposure_no=?",
            (grid_id, target_id, night, exposure_no),
        ).fetchall()
    }


def _night_colour(
    conn: sqlite3.Connection,
    grid_id: str,
    target_id: str,
    night: str,
    pair: dict,
    version: int,
) -> dict | None:
    g_mags = _versions_for_exposure(conn, grid_id, target_id, night, pair["g_no"])
    r_mags = _versions_for_exposure(conn, grid_id, target_id, night, pair["r_no"])
    if version not in g_mags or version not in r_mags:
        return None
    g_mag = g_mags[version]
    r_mag = r_mags[version]
    return {
        "night": night,
        "g_exposure_no": pair["g_no"],
        "r_exposure_no": pair["r_no"],
        "g_magnitude": g_mag,
        "r_magnitude": r_mag,
        "g_minus_r": g_mag - r_mag,
    }


def compare_colour(
    conn: sqlite3.Connection,
    grid_id: str,
    target_id: str,
    first_night: str,
    second_night: str,
) -> dict:
    """Compare one target's g−r on two nights using the current pairings.

    Both nights are read from one snapshot.  A photometry *version* is one
    round shared by every night and band, so the two nights are only
    comparable when the same version has all four magnitudes (g and r on each
    night) for the exposures currently paired.  The highest such version wins;
    when a newer round is still incomplete the newest complete older round is
    used.  Fetching always re-reads coverage, so switching the pair re-runs the
    version choice.

    Returns a dict with ``available`` False and a Chinese ``reason`` when the
    pair is missing or no shared version is complete.
    """
    result = {
        "grid_id": grid_id,
        "target_id": target_id,
        "first_night": first_night,
        "second_night": second_night,
        "available": False,
        "reason": None,
        "version": None,
        "first": None,
        "second": None,
        "colour_delta": None,
    }
    if not target_id:
        result["reason"] = "请选择一个目标号"
        return result
    if not first_night or not second_night:
        result["reason"] = "请选择两个观测夜"
        return result
    if first_night == second_night:
        result["reason"] = "两个观测夜必须不同，暂不可比"
        return result

    with read_snapshot(conn):
        # A pair is only defined for a night this grid actually has exposure
        # coverage on; anything else is a bad request rather than "not comparable".
        known = {
            row["night"]
            for row in conn.execute(
                "SELECT DISTINCT e.night FROM exposures e "
                "JOIN exposure_grids eg ON eg.exposure_id = e.id "
                "WHERE eg.grid_id=?",
                (grid_id,),
            ).fetchall()
        }
        unknown = [n for n in (first_night, second_night) if n not in known]
        if unknown:
            result["reason"] = f"{'、'.join(unknown)} 不在本网格的观测夜内，暂不可比"
            return result

        has_target = conn.execute(
            "SELECT 1 FROM photometry WHERE grid_id=? AND target_id=? LIMIT 1",
            (grid_id, target_id),
        ).fetchone()
        if has_target is None:
            result["reason"] = f"本网格没有目标 {target_id} 的测光数据，暂不可比"
            return result

        first_pair = _current_pair(conn, grid_id, first_night)
        second_pair = _current_pair(conn, grid_id, second_night)
        if first_pair is None or second_pair is None:
            missing = [
                night
                for night, pair in ((first_night, first_pair), (second_night, second_pair))
                if pair is None
            ]
            result["reason"] = (
                f"{'、'.join(missing)} 没有现行的 g/r 配对，暂不可比"
            )
            return result

        first_versions = set(
            _versions_for_exposure(conn, grid_id, target_id, first_night, first_pair["g_no"])
        ) & set(
            _versions_for_exposure(conn, grid_id, target_id, first_night, first_pair["r_no"])
        )
        second_versions = set(
            _versions_for_exposure(conn, grid_id, target_id, second_night, second_pair["g_no"])
        ) & set(
            _versions_for_exposure(conn, grid_id, target_id, second_night, second_pair["r_no"])
        )
        common = first_versions & second_versions
        if not common:
            result["reason"] = "找不到可用共同版号（四次曝光的星等不齐），暂不可比"
            return result

        # Highest shared version whose four magnitudes are all present.
        version = max(common)
        first = _night_colour(conn, grid_id, target_id, first_night, first_pair, version)
        second = _night_colour(conn, grid_id, target_id, second_night, second_pair, version)
        if first is None or second is None:
            result["reason"] = "找不到可用共同版号（四次曝光的星等不齐），暂不可比"
            return result

        result.update(
            {
                "available": True,
                "version": version,
                "first": first,
                "second": second,
                "colour_delta": second["g_minus_r"] - first["g_minus_r"],
            }
        )
        return result
