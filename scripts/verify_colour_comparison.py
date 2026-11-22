"""End-to-end check of photometry import and two-night colour comparison.

Usage:
    python3 scripts/verify_colour_comparison.py [--db PATH]

Defaults to a temporary database loaded with ``samples/survey_coverage.csv`` so
the real database is never touched unless ``--db`` is given.
"""
from __future__ import annotations

import argparse
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import db  # noqa: E402
from app.importer import parse_csv, parse_photometry_csv  # noqa: E402

SAMPLES = os.path.join(ROOT, "samples")
FIRST, SECOND = "N2026-10-01", "N2026-10-02"
TARGET = "T001"


def load_photometry(conn, name: str) -> dict:
    text = open(os.path.join(SAMPLES, name), encoding="utf-8").read()
    parsed = parse_photometry_csv(text)
    if parsed.errors:
        raise SystemExit(f"{name}: {parsed.errors}")
    return db.import_photometry(conn, parsed.rows, name)


def show(conn, label: str) -> None:
    out = db.compare_colour(conn, "G01", TARGET, FIRST, SECOND)
    print(f"\n[{label}]")
    if not out["available"]:
        print("  暂不可比:", out["reason"])
        return
    for key in ("first", "second"):
        c = out[key]
        print(
            f"  {c['night']}: g={c['g_exposure_no']} r={c['r_exposure_no']} "
            f"version={out['version']} g-r={c['g_minus_r']:.4f}"
        )
    print(f"  Δ(g-r) = {out['colour_delta']:+.4f}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db")
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
            coverage = parse_csv(
                open(os.path.join(SAMPLES, "survey_coverage.csv"), encoding="utf-8").read()
            )
            assert not coverage.errors, coverage.errors
            print("coverage:", db.import_rows(conn, coverage.rows, "survey_coverage.csv"))

        print("import v1+partial v2:", load_photometry(conn, "photometry.csv"))
        show(conn, "v2 未齐 -> 应回退 v1")

        print("import complete v2:", load_photometry(conn, "photometry_complete_v2.csv"))
        show(conn, "v2 已齐 -> 应用 v2")

        # Switch the first night's pair to E1001/E1002 by invalidating E1003.
        exp = conn.execute(
            "SELECT id FROM exposures WHERE night=? AND exposure_no='E1003'", (FIRST,)
        ).fetchone()
        db.set_valid(conn, exp["id"], False)
        show(conn, "换配对后（新配对无测光）-> 应暂不可比")

        print("import v3 for new pair:", load_photometry(conn, "photometry_alt_pair.csv"))
        show(conn, "新配对补齐 v3 -> 应可用")

        print("\nconflict batch:")
        bad = parse_photometry_csv(
            open(os.path.join(SAMPLES, "photometry_conflict.csv"), encoding="utf-8").read()
        )
        try:
            db.import_photometry(conn, bad.rows, "photometry_conflict.csv")
            print("  ERROR: conflict was accepted")
            return 4
        except db.PhotometryConflict as exc:
            print("  整批退回:", exc)
        stored = conn.execute(
            "SELECT magnitude FROM photometry WHERE grid_id='G01' AND target_id=? "
            "AND night=? AND exposure_no='E1003' AND version=1",
            (TARGET, FIRST),
        ).fetchone()
        print("  已入库星等保持:", stored["magnitude"])
        show(conn, "冲突退回后仍可用")

        print("\nphotometry rows:", conn.execute(
            "SELECT COUNT(*) AS n FROM photometry"
        ).fetchone()["n"])
    finally:
        conn.close()
        if tmpdir is not None:
            tmpdir.cleanup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
