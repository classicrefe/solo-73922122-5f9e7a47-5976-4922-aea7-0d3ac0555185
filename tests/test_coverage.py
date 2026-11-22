"""Pairing, import and correction behaviour."""

from __future__ import annotations

import threading

import pytest

from app import db
from app.importer import parse_csv

from conftest import SAMPLE_CSV, coverage, exposure_id


def test_import_links_shared_exposure_to_multiple_grids(imported):
    rows = imported.execute("SELECT COUNT(*) AS n FROM exposures").fetchone()["n"]
    links = imported.execute("SELECT COUNT(*) AS n FROM exposure_grids").fetchone()["n"]
    # 8 distinct (night, exposure_no) pairs; 12 CSV rows.
    assert rows == 8
    assert links == 12


def test_closest_pair_wins(imported):
    cov = coverage(imported, "G01", "N1")
    assert cov["status"] == "paired"
    assert cov["pair_seconds"] == 300
    g_no = imported.execute(
        "SELECT exposure_no FROM exposures WHERE id=?", (cov["g_exposure"],)
    ).fetchone()["exposure_no"]
    assert g_no == "E1003"


def test_thirty_minute_limit_and_needs_retest(imported):
    cov = coverage(imported, "G02", "N2")
    assert cov["status"] == "unpaired"
    assert "40.0 分钟" in cov["reason"]

    summary = {g["grid_id"]: g for g in db.grid_summary(imported)}
    assert summary["G01"]["usable_nights"] == 2
    assert summary["G01"]["needs_more"] is False
    assert summary["G02"]["usable_nights"] == 1
    assert summary["G02"]["needs_more"] is True
    assert summary["G03"]["needs_more"] is True


def test_exactly_thirty_minutes_is_paired(conn):
    parsed = parse_csv(
        "grid,exposure,night,utc,band\n"
        "GX,A1,N1,2026-10-01T00:00:00Z,g\n"
        "GX,A2,N1,2026-10-01T00:30:00Z,r\n"
    )
    db.import_rows(conn, parsed.rows, "edge.csv")
    assert coverage(conn, "GX", "N1")["status"] == "paired"
    assert coverage(conn, "GX", "N1")["pair_seconds"] == 1800


def test_thirty_minutes_plus_one_second_is_not_paired(conn):
    parsed = parse_csv(
        "grid,exposure,night,utc,band\n"
        "GX,A1,N1,2026-10-01T00:00:00Z,g\n"
        "GX,A2,N1,2026-10-01T00:30:01Z,r\n"
    )
    db.import_rows(conn, parsed.rows, "edge.csv")
    assert coverage(conn, "GX", "N1")["status"] == "unpaired"


def test_invalidate_switches_to_alternate_pair(imported):
    alt_g = exposure_id(imported, "N1", "E1003")  # currently the chosen pair for G01/N1
    result = db.set_valid(imported, alt_g, False)
    assert result["changed"] is True
    cov = coverage(imported, "G01", "N1")
    assert cov["status"] == "paired"
    g_no = imported.execute(
        "SELECT exposure_no FROM exposures WHERE id=?", (cov["g_exposure"],)
    ).fetchone()["exposure_no"]
    assert g_no == "E1001"
    assert cov["pair_seconds"] == 600


def test_losing_all_pairs_drops_the_night(imported):
    for no in ("E1001", "E1003", "E1004"):
        db.set_valid(imported, exposure_id(imported, "N1", no), False)
    cov = coverage(imported, "G01", "N1")
    assert cov["status"] == "unpaired"
    assert "没有有效 g" in cov["reason"]
    summary = {g["grid_id"]: g for g in db.grid_summary(imported)}
    assert summary["G01"]["usable_nights"] == 1
    assert summary["G01"]["needs_more"] is True


def test_restore_brings_the_original_pair_back(imported):
    exp = exposure_id(imported, "N1", "E1003")
    db.set_valid(imported, exp, False)
    # G01/N1 is still usable, but through the alternate E1001/E1002 pair.
    cov = coverage(imported, "G01", "N1")
    assert cov["status"] == "paired"
    assert cov["pair_seconds"] == 600
    db.set_valid(imported, exp, True)
    cov = coverage(imported, "G01", "N1")
    assert cov["status"] == "paired"
    assert cov["pair_seconds"] == 300


def test_toggle_rechecks_every_grid_the_exposure_covers(imported):
    """E1002 is the r exposure for G01/G02/G03 on N1; dropping it hits all three."""
    exp = exposure_id(imported, "N1", "E1002")
    result = db.set_valid(imported, exp, False)
    assert sorted(result["grids"]) == ["G01", "G02", "G03"]
    # G01 has a spare close pair and stays usable; G02/G03 lose the night.
    assert {c["grid_id"]: c["status"] for c in result["coverage"]} == {
        "G01": "paired",
        "G02": "unpaired",
        "G03": "unpaired",
    }
    for grid in ("G02", "G03"):
        assert coverage(imported, grid, "N1")["status"] == "unpaired"
    assert coverage(imported, "G01", "N1")["pair_seconds"] == 300
    # G01/N1 keeps its g exposures untouched (both still valid).
    valid_g = imported.execute(
        "SELECT COUNT(*) AS n FROM exposures WHERE night='N1' AND band='g' AND valid=1"
    ).fetchone()["n"]
    assert valid_g == 2


def test_shared_exposure_dropping_three_grids_at_once(conn):
    parsed = parse_csv(
        "grid,exposure,night,utc,band\n"
        "G7,S1,N7,2026-10-09T00:00:00Z,g\n"
        "G7,S2,N7,2026-10-09T00:05:00Z,r\n"
        "G8,S3,N7,2026-10-09T00:01:00Z,g\n"
        "G8,S2,N7,2026-10-09T00:05:00Z,r\n"
        "G9,S4,N7,2026-10-09T00:10:00Z,g\n"
        "G9,S2,N7,2026-10-09T00:05:00Z,r\n"
    )
    assert not parsed.errors, parsed.errors
    db.import_rows(conn, parsed.rows, "shared.csv")
    assert all(coverage(conn, g, "N7")["status"] == "paired" for g in ("G7", "G8", "G9"))

    result = db.set_valid(conn, exposure_id(conn, "N7", "S2"), False)
    assert sorted(result["grids"]) == ["G7", "G8", "G9"]
    assert {c["status"] for c in result["coverage"]} == {"unpaired"}
    assert {g["grid_id"] for g in db.grid_summary(conn) if g["needs_more"]} == {
        "G7",
        "G8",
        "G9",
    }

    db.set_valid(conn, exposure_id(conn, "N7", "S2"), True)
    assert all(coverage(conn, g, "N7")["status"] == "paired" for g in ("G7", "G8", "G9"))


def test_untouched_grid_night_is_not_recomputed(imported):
    before = dict(coverage(imported, "G02", "N1"))
    db.set_valid(imported, exposure_id(imported, "N1", "E1002"), False)
    # G02/N1 is affected; G01/N2 must stay identical.
    after_other = dict(coverage(imported, "G01", "N2"))
    db.set_valid(imported, exposure_id(imported, "N1", "E1002"), True)
    assert dict(coverage(imported, "G01", "N2")) == after_other
    # G02/N1 pairs through E1201/E1202 on N2 only; N1 has nothing close left.
    assert before["status"] == "paired"


def test_import_conflict_rolls_back_entire_batch(imported):
    bad = parse_csv(
        "grid,exposure,night,utc,band\n"
        "G09,NEW1,N9,2026-10-05T00:00:00Z,g\n"
        "G09,E1001,N1,2026-10-01T12:00:00Z,r\n"  # E1001 is g in existing data
    )
    assert not bad.errors
    with pytest.raises(db.ImportConflict):
        db.import_rows(imported, bad.rows, "bad.csv")
    assert imported.execute(
        "SELECT COUNT(*) AS n FROM exposures WHERE night='N9'"
    ).fetchone()["n"] == 0


def test_duplicate_rows_are_skipped_not_errors(imported):
    parsed = parse_csv(SAMPLE_CSV)
    outcome = db.import_rows(imported, parsed.rows, "again.csv")
    assert outcome["inserted"] == 0
    assert outcome["linked"] == 0
    assert outcome["skipped"] == 12


def test_parser_rejects_conflicting_duplicate_exposure():
    parsed = parse_csv(
        "grid,exposure,night,utc,band\n"
        "G1,X1,N1,2026-10-01T00:00:00Z,g\n"
        "G2,X1,N1,2026-10-01T00:05:00Z,g\n"
    )
    assert parsed.errors and "不一致" in parsed.errors[0]


def test_parser_accepts_chinese_headers_and_offsets():
    parsed = parse_csv(
        "网格号,曝光号,观测夜编号,UTC时刻,波段\n"
        "G1,X1,N1,2026-10-01T08:00:00+08:00,g\n"
    )
    assert not parsed.errors
    assert parsed.rows[0]["utc_time"] == "2026-10-01T00:00:00Z"


def test_concurrent_toggles_of_different_exposures_both_land(db_file):
    """Two writers, separate connections, different exposures, both committed."""
    db.init_db(db_file)
    setup = db.connect(db_file)
    parsed = parse_csv(SAMPLE_CSV)
    db.import_rows(setup, parsed.rows, "sample.csv")
    ids = {
        "g01_r": exposure_id(setup, "N2", "E1102"),
        "g02_r": exposure_id(setup, "N1", "E1002"),
    }
    setup.close()

    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def worker(name: str, exposure: int):
        connection = db.connect(db_file)
        try:
            barrier.wait(timeout=10)
            results[name] = db.set_valid(connection, exposure, False)
        except Exception as exc:  # pragma: no cover - surfaced in assertion
            results[name] = exc
        finally:
            connection.close()

    threads = [
        threading.Thread(target=worker, args=("g01_r", ids["g01_r"])),
        threading.Thread(target=worker, args=("g02_r", ids["g02_r"])),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert all(not isinstance(v, Exception) for v in results.values()), results

    check = db.connect(db_file)
    try:
        assert check.execute(
            "SELECT valid FROM exposures WHERE id=?", (ids["g01_r"],)
        ).fetchone()["valid"] == 0
        assert check.execute(
            "SELECT valid FROM exposures WHERE id=?", (ids["g02_r"],)
        ).fetchone()["valid"] == 0
        # First toggle: G01/N2 loses its r -> unpaired and drops to 1 usable night.
        assert coverage(check, "G01", "N2")["status"] == "unpaired"
        assert coverage(check, "G02", "N1")["status"] == "unpaired"
        summary = {g["grid_id"]: g for g in db.grid_summary(check)}
        assert summary["G01"]["usable_nights"] == 1
    finally:
        check.close()


def test_failed_toggle_leaves_prior_state_visible(imported):
    """A correction that raises must not disturb committed data for readers."""
    exp = exposure_id(imported, "N1", "E1003")
    before = dict(coverage(imported, "G01", "N1"))
    with pytest.raises(LookupError):
        db.set_valid(imported, 999999, False)
    assert dict(coverage(imported, "G01", "N1")) == before
    assert imported.execute(
        "SELECT valid FROM exposures WHERE id=?", (exp,)
    ).fetchone()["valid"] == 1


def test_one_failed_correction_does_not_block_the_other(db_file):
    """Concurrent: one correction fails, the other still commits and is readable."""
    db.init_db(db_file)
    setup = db.connect(db_file)
    parsed = parse_csv(SAMPLE_CSV)
    db.import_rows(setup, parsed.rows, "sample.csv")
    good = exposure_id(setup, "N2", "E1102")
    setup.close()

    barrier = threading.Barrier(2)
    results: dict[str, object] = {}

    def worker(name: str, exposure: int):
        connection = db.connect(db_file)
        try:
            barrier.wait(timeout=10)
            results[name] = db.set_valid(connection, exposure, False)
        except Exception as exc:
            results[name] = exc
        finally:
            connection.close()

    threads = [
        threading.Thread(target=worker, args=("good", good)),
        threading.Thread(target=worker, args=("missing", 10_000_000)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert isinstance(results["missing"], LookupError)
    assert not isinstance(results["good"], Exception)

    reader = db.connect(db_file)
    try:
        assert reader.execute(
            "SELECT valid FROM exposures WHERE id=?", (good,)
        ).fetchone()["valid"] == 0
        assert coverage(reader, "G01", "N2")["status"] == "unpaired"
        # Everything else is still exactly as imported.
        assert coverage(reader, "G02", "N1")["status"] == "paired"
        assert {g["grid_id"]: g["usable_nights"] for g in db.grid_summary(reader)} == {
            "G01": 1,
            "G02": 1,
            "G03": 1,
        }
    finally:
        reader.close()


def test_audit_log_records_invalidate_and_restore(imported):
    exp = exposure_id(imported, "N1", "E1002")
    db.set_valid(imported, exp, False)
    db.set_valid(imported, exp, True)
    rows = imported.execute(
        "SELECT action, grid_id FROM audit_log WHERE exposure_id=? ORDER BY id", (exp,)
    ).fetchall()
    # E1002 covers three grids, so each correction leaves one row per grid.
    assert [r["action"] for r in rows] == ["invalidate"] * 3 + ["revalidate"] * 3
    assert {r["grid_id"] for r in rows} == {"G01", "G02", "G03"}
