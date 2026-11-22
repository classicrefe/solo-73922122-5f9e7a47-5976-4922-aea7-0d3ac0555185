"""Pre-delivery verification: exercise a multi-grid invalidation on a copy of the DB.

Usage:
    python3 scripts/verify_multi_grid_invalidation.py [--db PATH] [--csv PATH]

Defaults to a temporary database loaded with ``samples/survey_coverage.csv`` so
the real ``data/coverage.sqlite3`` is never touched unless ``--db`` is given.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402
from app.importer import parse_csv  # noqa: E402


def snapshot(conn, grid_ids, night):
    return {
        gid: dict(conn.execute(
            "SELECT status, pair_seconds FROM coverage WHERE grid_id=? AND night=?",
            (gid, night),
        ).fetchone())
        for gid in grid_ids
    }


def pair_names(conn, grid_id, night):
    cov = conn.execute(
        "SELECT c.g_exposure, c.r_exposure, g.exposure_no AS g, r.exposure_no AS r "
        "FROM coverage c LEFT JOIN exposures g ON g.id=c.g_exposure "
        "LEFT JOIN exposures r ON r.id=c.r_exposure "
        "WHERE c.grid_id=? AND c.night=?",
        (grid_id, night),
    ).fetchone()
    return f"{cov['g']}/{cov['r']}"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db")
    parser.add_argument("--csv", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "samples", "survey_coverage.csv"))
    args = parser.parse_args()

    tmpdir = None
    if args.db:
        path = args.db
    else:
        tmpdir = tempfile.TemporaryDirectory()
        path = os.path.join(tmpdir.name, "verify.sqlite3")
    db.init_db(path)

    conn = db.connect(path)
    try:
        if not args.db:
            text = open(args.csv, encoding="utf-8").read()
            parsed = parse_csv(text)
            if parsed.errors:
                print("CSV errors:", parsed.errors)
                return 2
            print("import:", db.import_rows(conn, parsed.rows, os.path.basename(args.csv)))

        night = "N2026-10-01"
        exp = conn.execute(
            "SELECT id, exposure_no FROM exposures WHERE night=? AND exposure_no='E1002'",
            (night,),
        ).fetchone()
        if exp is None:
            print(f"E1002 on {night} not present in {path}; nothing to verify")
            return 3
        grids = [
            r["grid_id"]
            for r in conn.execute(
                "SELECT grid_id FROM exposure_grids WHERE exposure_id=? ORDER BY grid_id",
                (exp["id"],),
            )
        ]
        print(f"exposure {exp['id']} E1002 ({night}) covers grids: {', '.join(grids)}")
        print("before:", {g: (v["status"], pair_names(conn, g, night))
                          for g, v in snapshot(conn, grids, night).items()})

        result = db.set_valid(conn, exp["id"], False)
        print("invalidate changed:", result["changed"], "affected grids:", result["grids"])
        print("after :", {g: (v["status"], v["pair_seconds"]) for g, v in
                          snapshot(conn, grids, night).items()})
        summary = {g["grid_id"]: g for g in db.grid_summary(conn)}
        for gid in grids:
            print(f"  {gid}: usable_nights={summary[gid]['usable_nights']} "
                  f"needs_more={summary[gid]['needs_more']}")

        db.set_valid(conn, exp["id"], True)
        print("restored:", {g: (v["status"], pair_names(conn, g, night))
                            for g, v in snapshot(conn, grids, night).items()})
        print("audit tail:")
        for row in conn.execute(
            "SELECT at, action, exposure_id, grid_id, night, detail "
            "FROM audit_log ORDER BY id DESC LIMIT 7"
        ):
            print(" ", dict(row))
    finally:
        conn.close()
        if tmpdir is not None:
            tmpdir.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
