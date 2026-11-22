"""Offline repair: rebuild the coverage cache from the exposure tables.

    python3 scripts/repair_coverage.py --db data/coverage.sqlite3 [--check]

``--check`` reports anomalies without writing anything (exit 1 when the cache
stored in the file disagrees with a from-scratch recomputation).
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db  # noqa: E402


def stored_coverage(conn):
    return {
        (r["grid_id"], r["night"]): (r["status"], r["g_exposure"], r["r_exposure"])
        for r in conn.execute(
            "SELECT grid_id, night, status, g_exposure, r_exposure FROM coverage"
        )
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    db.init_db(args.db)
    conn = db.connect(args.db)
    try:
        if args.check:
            before = stored_coverage(conn)
            with tempfile.TemporaryDirectory() as tmp:
                clone = os.path.join(tmp, "clone.sqlite3")
                conn.execute("VACUUM INTO ?", (clone,))
                twin = db.connect(clone)
                try:
                    db.recompute_all(twin)
                    after = stored_coverage(twin)
                finally:
                    twin.close()
            if before == after:
                print(f"coverage cache OK ({len(before)} rows)")
                return 0
            missing = sorted(set(after) - set(before))
            extra = sorted(set(before) - set(after))
            wrong = sorted(k for k in set(before) & set(after) if before[k] != after[k])
            print(f"coverage cache STALE: missing={missing} extra={extra} wrong={wrong}")
            return 1
        summary = db.repair_views(conn)
        print(f"repair done: {summary}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
