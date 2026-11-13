import pytest


@pytest.fixture
def db(tmp_path):
    from docwizard.db.database import Database

    database = Database(tmp_path / "test.db")
    database.open()
    yield database
    database.close()


@pytest.fixture(autouse=True)
def fast_scrypt(monkeypatch):
    monkeypatch.setenv("DOCWIZARD_SCRYPT_LOG_N", "10")


@pytest.fixture
def cfg(tmp_path):
    from docwizard import config

    c = config.load(None, env={})
    c.server.data_dir = str(tmp_path / "app")
    c.server.jobs_dir = str(tmp_path / "jobs")
    c.server.cache_dir = str(tmp_path / "cache")
    c.server.publish_dir = str(tmp_path / "publish")
    c.accounts.registration = "open"
    return c


@pytest.fixture
async def app(cfg):
    from docwizard.asgi import create_app

    application = create_app(cfg)
    await application.startup()
    yield application
    await application.shutdown()


@pytest.fixture
async def client(app):
    from tests.asgi_client import Client
    from docwizard.auth import passwords, users

    pw = passwords.hash_password("correct horse battery")
    await app.db.write(lambda c: users.create(c, "jane", pw, display_name="Jane Doe", role="admin"))
    cl = Client(app)
    await cl.login("jane", "correct horse battery")
    return cl
