from __future__ import annotations

import pytest

from app import create_app
from app.db import connect, init_db


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "catalog.db"
    init_db(str(path))
    return path


@pytest.fixture
def conn(db_path):
    connection = connect(str(db_path))
    yield connection
    connection.close()


@pytest.fixture
def app(tmp_path):
    application = create_app(
        {"TESTING": True, "DATABASE": str(tmp_path / "web-catalog.db")}
    )
    yield application


@pytest.fixture
def client(app):
    return app.test_client()
