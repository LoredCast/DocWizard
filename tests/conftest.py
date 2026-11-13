import pytest


@pytest.fixture
def db(tmp_path):
    from docwizard.db.database import Database

    database = Database(tmp_path / "test.db")
    database.open()
    yield database
    database.close()
