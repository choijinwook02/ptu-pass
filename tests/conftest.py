import os
import sys
import tempfile

import pytest

# app.py import 시점에 레포의 commute.db를 건드리지 않도록 임시 경로를 먼저 지정
_IMPORT_DB = os.path.join(tempfile.mkdtemp(prefix="ptu-import-"), "import.db")
os.environ["PTU_DB_PATH"] = _IMPORT_DB
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import auth  # noqa: E402
import db  # noqa: E402
from app import app as flask_app  # noqa: E402


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test.db")


@pytest.fixture
def app(db_path):
    flask_app.config.update(TESTING=True, DB_PATH=db_path)
    db.init_db(db_path, demo_password_hash=auth.demo_password_hash())
    yield flask_app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def raw_db(app, db_path):
    conn = db.connect(db_path)
    yield conn
    conn.close()
