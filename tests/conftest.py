"""Test fixtures: an app pointed at a throwaway SQLite file."""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app import db  # noqa: E402
from app.importer import parse_csv  # noqa: E402

SAMPLE_CSV = """grid,exposure,night,utc,band
G01,E1001,N1,2026-10-01T12:00:00Z,g
G01,E1002,N1,2026-10-01T12:10:00Z,r
G01,E1003,N1,2026-10-01T12:50:00Z,g
G01,E1004,N1,2026-10-01T12:55:00Z,r
G01,E1101,N2,2026-10-02T11:00:00Z,g
G01,E1102,N2,2026-10-02T11:07:00Z,r
G02,E1001,N1,2026-10-01T12:00:00Z,g
G02,E1002,N1,2026-10-01T12:10:00Z,r
G02,E1201,N2,2026-10-02T13:00:00Z,g
G02,E1202,N2,2026-10-02T13:40:00Z,r
G03,E1001,N1,2026-10-01T12:00:00Z,g
G03,E1002,N1,2026-10-01T12:10:00Z,r
"""


@pytest.fixture
def db_file(tmp_path):
    return str(tmp_path / "coverage.sqlite3")


@pytest.fixture
def conn(db_file):
    db.init_db(db_file)
    connection = db.connect(db_file)
    yield connection
    connection.close()


@pytest.fixture
def imported(conn):
    parsed = parse_csv(SAMPLE_CSV)
    assert not parsed.errors, parsed.errors
    db.import_rows(conn, parsed.rows, "sample.csv")
    return conn


@pytest.fixture
def app(db_file):
    application = create_app(
        {"TESTING": True, "DATABASE": db_file, "CSRF_ENABLED": False, "SECRET_KEY": "test"}
    )
    yield application


@pytest.fixture
def client(app):
    return app.test_client()


def exposure_id(conn, night, exposure_no):
    return conn.execute(
        "SELECT id FROM exposures WHERE night=? AND exposure_no=?", (night, exposure_no)
    ).fetchone()["id"]


def coverage(conn, grid_id, night):
    return conn.execute(
        "SELECT * FROM coverage WHERE grid_id=? AND night=?", (grid_id, night)
    ).fetchone()
