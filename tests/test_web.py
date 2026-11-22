"""HTTP-level checks for the review board."""

from __future__ import annotations

import io

from app import db

from conftest import SAMPLE_CSV, coverage, exposure_id


def _upload(client, text, name="batch.csv"):
    return client.post(
        "/import",
        data={"csv": (io.BytesIO(text.encode("utf-8")), name)},
        content_type="multipart/form-data",
        follow_redirects=True,
    )


def test_upload_then_render_overview(client, app):
    response = _upload(client, SAMPLE_CSV)
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "导入完成" in body
    assert "G01" in body and "待补测" in body

    landing = client.get("/")
    assert landing.status_code == 200
    assert "G02" in landing.get_data(as_text=True)


def test_upload_reports_parse_errors(client):
    response = _upload(client, "grid,exposure,night,utc,band\nG1,X1,N1,bad,g\n")
    assert response.status_code == 200
    assert "第 2 行" in response.get_data(as_text=True)


def test_upload_rejects_conflicting_batch_without_partial_import(client, app):
    _upload(client, SAMPLE_CSV)
    response = _upload(
        client,
        "grid,exposure,night,utc,band\n"
        "G99,NEW,N99,2026-10-09T00:00:00Z,g\n"
        "G99,E1001,N1,2026-10-01T12:00:00Z,r\n",
        name="conflict.csv",
    )
    body = response.get_data(as_text=True)
    assert "导入被拒绝" in body
    conn = db.connect(app.config["DATABASE"])
    try:
        assert conn.execute(
            "SELECT COUNT(*) AS n FROM exposures WHERE night='N99'"
        ).fetchone()["n"] == 0
    finally:
        conn.close()


def test_toggle_route_updates_coverage_and_page(client, app):
    _upload(client, SAMPLE_CSV)
    conn = db.connect(app.config["DATABASE"])
    try:
        exp = exposure_id(conn, "N1", "E1002")
    finally:
        conn.close()

    response = client.post(
        f"/exposures/{exp}/validity",
        data={"valid": "0", "next": "/"},
        follow_redirects=True,
    )
    body = response.get_data(as_text=True)
    assert response.status_code == 200
    assert "已改为已剔除" in body
    assert "共影响 3 个网格" in body

    conn = db.connect(app.config["DATABASE"])
    try:
        assert conn.execute(
            "SELECT valid FROM exposures WHERE id=?", (exp,)
        ).fetchone()["valid"] == 0
        assert coverage(conn, "G02", "N1")["status"] == "unpaired"
    finally:
        conn.close()

    detail = client.get("/grids/G02").get_data(as_text=True)
    assert "不可用" in detail


def test_grid_page_lists_shared_coverage(client, app):
    _upload(client, SAMPLE_CSV)
    response = client.get("/grids/G01")
    assert response.status_code == 200
    page = response.get_data(as_text=True)
    assert "E1001" in page and "G01,G02,G03" in page


def test_csrf_required_when_enabled(app):
    app.config["CSRF_ENABLED"] = True
    client = app.test_client()
    response = client.post("/exposures/1/validity", data={"valid": "0"})
    assert response.status_code == 400


def test_api_json(client):
    _upload(client, SAMPLE_CSV)
    payload = client.get("/api/grids").get_json()
    by_grid = {g["grid_id"]: g for g in payload["grids"]}
    assert by_grid["G01"]["usable_nights"] == 2
    assert by_grid["G02"]["needs_more"] is True
    assert by_grid["G01"]["nights"][0]["g_exposure_no"] == "E1003"


def test_healthz(client):
    assert client.get("/healthz").get_json() == {"ok": True}
