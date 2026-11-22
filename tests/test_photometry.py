"""Photometry import, key handling and two-night colour comparison."""

from __future__ import annotations

import io

import pytest

from app import db
from app.importer import parse_photometry_csv

from conftest import SAMPLE_CSV, coverage, exposure_id

HEADER = "grid,target,night,exposure,version,magnitude\n"


def photo(*rows: tuple) -> str:
    return HEADER + "".join(",".join(str(c) for c in row) + "\n" for row in rows)


# G01 pairs in SAMPLE_CSV: N1 = E1003(g)/E1004(r), N2 = E1101(g)/E1102(r).
V1_ROWS = (
    ("G01", "T1", "N1", "E1003", 1, 20.00),
    ("G01", "T1", "N1", "E1004", 1, 19.50),  # N1 g-r = 0.50
    ("G01", "T1", "N2", "E1101", 1, 19.90),
    ("G01", "T1", "N2", "E1102", 1, 19.30),  # N2 g-r = 0.60 -> delta +0.10
)
V2_ROWS = (
    ("G01", "T1", "N1", "E1003", 2, 20.20),
    ("G01", "T1", "N1", "E1004", 2, 19.50),  # 0.70
    ("G01", "T1", "N2", "E1101", 2, 20.00),
    ("G01", "T1", "N2", "E1102", 2, 19.10),  # 0.90 -> delta +0.20
)


def load(conn, text: str, name: str = "photo.csv") -> dict:
    parsed = parse_photometry_csv(text)
    assert not parsed.errors, parsed.errors
    return db.import_photometry(conn, parsed.rows, name)


@pytest.fixture
def survey(imported):
    """Exposure coverage plus one complete round of photometry."""
    load(imported, photo(*V1_ROWS))
    return imported


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def test_parser_accepts_chinese_headers():
    parsed = parse_photometry_csv(
        "网格号,目标号,观测夜,曝光号,测光版号,星等\n"
        "G01,T1,N1,E1003,2,20.5\n"
    )
    assert not parsed.errors, parsed.errors
    assert parsed.rows[0]["version"] == 2
    assert parsed.rows[0]["magnitude"] == 20.5


@pytest.mark.parametrize("bad_version", ["0", "-1", "1.5", "abc", ""])
def test_parser_rejects_non_positive_or_non_integer_version(bad_version):
    parsed = parse_photometry_csv(
        f"grid,target,night,exposure,version,magnitude\nG01,T1,N1,E1003,{bad_version},20.0\n"
    )
    assert parsed.errors
    assert "版号" in parsed.errors[0]


@pytest.mark.parametrize("bad_mag", ["nan", "NaN", "inf", "-Infinity", "abc", ""])
def test_parser_rejects_non_finite_or_non_numeric_magnitude(bad_mag):
    parsed = parse_photometry_csv(
        f"grid,target,night,exposure,version,magnitude\nG01,T1,N1,E1003,2,{bad_mag}\n"
    )
    assert parsed.errors
    assert "星等" in parsed.errors[0]


def test_parser_missing_column_is_reported():
    parsed = parse_photometry_csv("grid,target,night,exposure,version\nG01,T1,N1,E1003,2\n")
    assert parsed.errors and "magnitude" in parsed.errors[0]


def test_parser_keeps_one_row_for_same_key_same_value():
    text = photo(
        ("G01", "T1", "N1", "E1003", 2, 20.30),
        ("G01", "T1", "N1", "E1003", 2, 20.3),  # same numeric value
    )
    parsed = parse_photometry_csv(text)
    assert not parsed.errors
    assert len(parsed.rows) == 1


def test_parser_rejects_same_key_different_value():
    parsed = parse_photometry_csv(
        photo(
            ("G01", "T1", "N1", "E1003", 2, 20.30),
            ("G01", "T1", "N1", "E1003", 2, 20.31),
        )
    )
    assert parsed.errors
    assert "不一致" in parsed.errors[0]


def test_parser_requires_grid_target_night_exposure():
    parsed = parse_photometry_csv(photo(("G01", "", "N1", "E1003", 2, 20.3)))
    assert parsed.errors and "不能为空" in parsed.errors[0]


# ---------------------------------------------------------------------------
# Import against stored coverage / magnitudes
# ---------------------------------------------------------------------------


def test_import_reuses_key_dedupe_across_batches(imported):
    first = load(imported, photo(*V1_ROWS), "v1.csv")
    assert first == {"inserted": 4, "skipped": 0}
    again = load(imported, photo(*V1_ROWS), "v1-again.csv")
    assert again == {"inserted": 0, "skipped": 4}
    assert imported.execute("SELECT COUNT(*) AS n FROM photometry").fetchone()["n"] == 4


def test_init_db_adds_photometry_tables_to_an_existing_database(db_file):
    db.init_db(db_file)
    conn = db.connect(db_file)
    try:
        conn.execute("DROP TABLE photometry")
        conn.execute("DROP TABLE photometry_imports")
    finally:
        conn.close()
    # Startup runs the idempotent schema against the volume database.
    db.init_db(db_file)
    conn = db.connect(db_file)
    try:
        names = {
            r["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()
    assert {"photometry", "photometry_imports"} <= names


def test_import_rejects_exposure_not_covering_grid(imported):
    # E1101 only covers G01 on N2; it does not cover G02.
    bad = photo(("G02", "T1", "N2", "E1101", 1, 20.0))
    parsed = parse_photometry_csv(bad)
    assert not parsed.errors
    with pytest.raises(db.PhotometryConflict) as exc:
        db.import_photometry(imported, parsed.rows, "bad.csv")
    assert "未覆盖网格" in str(exc.value)
    assert imported.execute("SELECT COUNT(*) AS n FROM photometry").fetchone()["n"] == 0


def test_import_rejects_unknown_exposure(imported):
    parsed = parse_photometry_csv(photo(("G01", "T1", "N1", "NOPE", 1, 20.0)))
    with pytest.raises(db.PhotometryConflict):
        db.import_photometry(imported, parsed.rows, "bad.csv")


def test_uncovered_grid_mid_batch_rolls_back_earlier_valid_rows(imported):
    parsed = parse_photometry_csv(
        photo(
            ("G01", "T1", "N1", "E1003", 1, 20.0),
            ("G01", "T1", "N1", "E1004", 1, 19.5),
            ("G02", "T1", "N2", "E1101", 1, 20.0),  # E1101 does not cover G02
        )
    )
    assert not parsed.errors
    with pytest.raises(db.PhotometryConflict) as exc:
        db.import_photometry(imported, parsed.rows, "bad.csv")
    assert "未覆盖网格" in str(exc.value)
    assert imported.execute("SELECT COUNT(*) AS n FROM photometry").fetchone()["n"] == 0


def test_conflicting_magnitude_rolls_back_whole_batch(imported):
    load(imported, photo(*V1_ROWS))
    before = {
        tuple(r)
        for r in imported.execute(
            "SELECT grid_id, target_id, night, exposure_no, version, magnitude FROM photometry"
        )
    }
    # First row is new (T9) and would succeed on its own; second contradicts T1.
    parsed = parse_photometry_csv(
        photo(
            ("G01", "T9", "N1", "E1003", 1, 21.0),
            ("G01", "T1", "N2", "E1102", 1, 18.00),
        )
    )
    assert not parsed.errors
    with pytest.raises(db.PhotometryConflict) as exc:
        db.import_photometry(imported, parsed.rows, "conflict.csv")
    assert "已入库星等" in str(exc.value)
    after = {
        tuple(r)
        for r in imported.execute(
            "SELECT grid_id, target_id, night, exposure_no, version, magnitude FROM photometry"
        )
    }
    assert after == before
    assert imported.execute(
        "SELECT COUNT(*) AS n FROM photometry WHERE target_id='T9'"
    ).fetchone()["n"] == 0


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def test_compare_uses_current_pairs_and_reports_delta(survey):
    out = db.compare_colour(survey, "G01", "T1", "N1", "N2")
    assert out["available"] is True
    assert out["version"] == 1
    assert out["first"] == {
        "night": "N1",
        "g_exposure_no": "E1003",
        "r_exposure_no": "E1004",
        "g_magnitude": 20.00,
        "r_magnitude": 19.50,
        "g_minus_r": pytest.approx(0.50),
    }
    assert out["second"]["g_exposure_no"] == "E1101"
    assert out["second"]["r_exposure_no"] == "E1102"
    assert out["second"]["g_minus_r"] == pytest.approx(0.60)
    assert out["colour_delta"] == pytest.approx(0.10)


def test_highest_complete_common_version_wins(survey):
    load(survey, photo(*V2_ROWS))
    out = db.compare_colour(survey, "G01", "T1", "N1", "N2")
    assert out["version"] == 2
    assert out["first"]["g_minus_r"] == pytest.approx(0.70)
    assert out["second"]["g_minus_r"] == pytest.approx(0.90)
    assert out["colour_delta"] == pytest.approx(0.20)


def test_incomplete_new_version_falls_back_to_old(survey):
    load(survey, photo(*V2_ROWS))
    # v3 has both N1 magnitudes and only the N2 g magnitude: not four complete.
    load(
        survey,
        photo(
            ("G01", "T1", "N1", "E1003", 3, 25.0),
            ("G01", "T1", "N1", "E1004", 3, 19.5),
            ("G01", "T1", "N2", "E1101", 3, 25.0),
        ),
    )
    out = db.compare_colour(survey, "G01", "T1", "N1", "N2")
    assert out["version"] == 2
    assert out["first"]["g_magnitude"] == 20.20


def test_no_common_version_is_not_comparable(imported):
    load(
        imported,
        photo(
            ("G01", "T2", "N1", "E1003", 1, 20.0),
            ("G01", "T2", "N1", "E1004", 1, 19.5),
            ("G01", "T2", "N2", "E1101", 2, 20.0),
            ("G01", "T2", "N2", "E1102", 2, 19.0),
        ),
    )
    out = db.compare_colour(imported, "G01", "T2", "N1", "N2")
    assert out["available"] is False
    assert "共同版号" in out["reason"]

    # Only one night's g/r pair carries a version: still no common version.
    out = db.compare_colour(imported, "G01", "T3", "N1", "N2")
    assert out["available"] is False


def test_unpaired_night_is_not_comparable(imported):
    out = db.compare_colour(imported, "G01", "T1", "N1", "N2")
    # No photometry at all -> covered by no-common-version path.
    assert out["available"] is False

    # G02/N2 is unpaired because its g/r are 40 minutes apart.
    load(
        imported,
        photo(
            ("G02", "T1", "N1", "E1001", 1, 20.0),
            ("G02", "T1", "N1", "E1002", 1, 19.5),
        ),
    )
    out = db.compare_colour(imported, "G02", "T1", "N1", "N2")
    assert out["available"] is False
    assert "配对" in out["reason"]


def test_same_night_is_rejected(survey):
    out = db.compare_colour(survey, "G01", "T1", "N1", "N1")
    assert out["available"] is False
    assert "必须不同" in out["reason"]


def test_pair_switch_reevaluates_and_can_become_not_comparable(imported):
    load(imported, photo(*V1_ROWS))
    before = db.compare_colour(imported, "G01", "T1", "N1", "N2")
    assert before["available"] is True and before["first"]["g_exposure_no"] == "E1003"

    # Drop the primary g: G01/N1 falls back to the E1001/E1002 pair, which has
    # no photometry yet.
    db.set_valid(imported, exposure_id(imported, "N1", "E1003"), False)
    assert coverage(imported, "G01", "N1")["g_exposure"] == exposure_id(imported, "N1", "E1001")
    switched = db.compare_colour(imported, "G01", "T1", "N1", "N2")
    assert switched["available"] is False
    assert "共同版号" in switched["reason"]


def test_photometry_for_new_pair_makes_it_comparable_again(imported):
    load(imported, photo(*V1_ROWS))
    db.set_valid(imported, exposure_id(imported, "N1", "E1003"), False)
    # New N1 pair is E1001/E1002; add v4 for it and N2, plus a partial v5 on N2.
    load(
        imported,
        photo(
            ("G01", "T1", "N1", "E1001", 4, 20.40),
            ("G01", "T1", "N1", "E1002", 4, 19.60),  # 0.80
            ("G01", "T1", "N2", "E1101", 4, 20.00),
            ("G01", "T1", "N2", "E1102", 4, 19.40),  # 0.60 -> delta -0.20
            ("G01", "T1", "N2", "E1101", 5, 99.00),  # incomplete new round
        ),
    )
    out = db.compare_colour(imported, "G01", "T1", "N1", "N2")
    assert out["available"] is True
    assert out["version"] == 4
    assert out["first"]["g_exposure_no"] == "E1001"
    assert out["first"]["r_exposure_no"] == "E1002"
    assert out["second"]["g_minus_r"] == pytest.approx(0.60)
    assert out["colour_delta"] == pytest.approx(-0.20)


def test_compare_is_scoped_to_target_and_grid(imported):
    load(imported, photo(*V1_ROWS))
    load(
        imported,
        photo(
            ("G02", "T1", "N1", "E1001", 9, 30.0),
            ("G02", "T1", "N1", "E1002", 9, 20.0),
        ),
    )
    out = db.compare_colour(imported, "G01", "T1", "N1", "N2")
    assert out["version"] == 1
    assert out["first"]["g_magnitude"] == 20.00


def test_compare_requires_target_and_nights(survey):
    assert "目标" in db.compare_colour(survey, "G01", "", "N1", "N2")["reason"]
    assert "观测夜" in db.compare_colour(survey, "G01", "T1", "", "N2")["reason"]
    unknown = db.compare_colour(survey, "G01", "T404", "N1", "N2")
    assert unknown["available"] is False
    assert "T404" in unknown["reason"]
    outside = db.compare_colour(survey, "G01", "T1", "N1", "N99")
    assert outside["available"] is False
    assert "N99" in outside["reason"]


# ---------------------------------------------------------------------------
# Web routes
# ---------------------------------------------------------------------------


def _upload_photometry(client, grid_id, text, name="photo.csv"):
    return client.post(
        f"/grids/{grid_id}/photometry",
        data={"csv": (io.BytesIO(text.encode("utf-8")), name)},
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def _upload_coverage(client):
    response = client.post(
        "/import",
        data={"csv": (io.BytesIO(SAMPLE_CSV.encode("utf-8")), "coverage.csv")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    assert "导入完成" in response.get_data(as_text=True)


def test_grid_page_shows_compare_entry_and_upload(client):
    _upload_coverage(client)
    page = client.get("/grids/G01").get_data(as_text=True)
    assert "两晚颜色对比" in page
    assert "导入测光 CSV" in page
    assert "还没有测光数据" in page

    response = _upload_photometry(client, "G01", photo(*V1_ROWS))
    page = response.get_data(as_text=True)
    assert "测光导入完成：新增 4 行" in page
    assert 'name="target"' in page
    assert "<option value=\"T1\"" in page


def test_web_compare_renders_exposures_version_and_delta(client):
    _upload_coverage(client)
    _upload_photometry(client, "G01", photo(*V1_ROWS))
    page = client.get(
        "/grids/G01?target=T1&first=N1&second=N2"
    ).get_data(as_text=True)
    assert "E1003" in page and "E1004" in page
    assert "E1101" in page and "E1102" in page
    assert "0.5000" in page and "0.6000" in page
    assert "+0.1000" in page


def test_web_compare_reports_not_comparable(client):
    _upload_coverage(client)
    _upload_photometry(client, "G01", photo(*V1_ROWS))
    page = client.get(
        "/grids/G01?target=T1&first=N1&second=N1"
    ).get_data(as_text=True)
    assert "两个观测夜必须不同" in page

    page = client.get(
        "/grids/G01?target=T1&first=N1&second=N2026-10-01"
    ).get_data(as_text=True)
    assert "暂不可比" in page


def test_web_upload_conflict_keeps_old_magnitudes(client, app):
    _upload_coverage(client)
    _upload_photometry(client, "G01", photo(*V1_ROWS))
    response = _upload_photometry(
        client,
        "G01",
        photo(("G01", "T1", "N1", "E1003", 1, 99.99)),
        name="conflict.csv",
    )
    page = response.get_data(as_text=True)
    assert "测光导入被拒绝" in page
    assert "99.99" in page
    conn = db.connect(app.config["DATABASE"])
    try:
        mag = conn.execute(
            "SELECT magnitude FROM photometry WHERE grid_id='G01' AND target_id='T1' "
            "AND night='N1' AND exposure_no='E1003' AND version=1"
        ).fetchone()["magnitude"]
        assert mag == 20.00
    finally:
        conn.close()
