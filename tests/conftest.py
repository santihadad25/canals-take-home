import json
import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

ROOT = Path(__file__).resolve().parent.parent
DEMO_DATA_SQL = ROOT / "db/init/02-demo-data.sql"
os.environ.setdefault("ADDRESSES_FILE", str(ROOT / "mocks/geocoding/addresses.json"))
os.environ.setdefault("SCENARIOS_FILE", str(ROOT / "mocks/payments/scenarios.json"))
sys.path[:0] = [str(ROOT / "mocks/geocoding"), str(ROOT / "mocks/payments")]

import geocoding_mock  # noqa: E402
import payments_mock  # noqa: E402

from app.adapters.persistence.engine import create_database_engine  # noqa: E402
from app.config import Settings  # noqa: E402
from tests.support import FakeClock, open_system, serve, warm_up  # noqa: E402

# The database for the tests: Postgres creates its schema the first time it starts (db/init).
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://canals:canals@localhost:5432/canals_test"
)

# The mocks answer slowly where a scenario says so; the API client below times out sooner.
SLOW_SECONDS = 1.2
PAYMENTS_TIMEOUT_SECONDS = 0.4


@pytest.fixture(scope="session")
def database_url() -> str:
    return TEST_DATABASE_URL


@pytest.fixture(scope="session")
def engine(database_url):
    engine = create_database_engine(Settings(database_url=database_url))
    warm_up(engine)
    yield engine
    engine.dispose()


@pytest.fixture(autouse=True)
def reset_database(engine):
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE order_events, order_items, orders, stock, warehouses, products "
                "RESTART IDENTITY CASCADE"
            )
        )
        connection.exec_driver_sql(DEMO_DATA_SQL.read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def geocoding_url():
    with serve(geocoding_mock.create_app(os.environ["ADDRESSES_FILE"])) as url:
        yield url


def _load_scenarios() -> dict:
    return json.loads(Path(os.environ["SCENARIOS_FILE"]).read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def payments_url(tmp_path_factory):
    scenarios = _load_scenarios()
    for card in scenarios["cards"].values():
        for operation in card.values():
            if isinstance(operation, dict) and "delay_seconds" in operation:
                operation["delay_seconds"] = SLOW_SECONDS
    path = tmp_path_factory.mktemp("payments-mock") / "scenarios.json"
    path.write_text(json.dumps(scenarios), encoding="utf-8")
    with serve(payments_mock.create_app(str(path))) as url:
        yield url


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def system(engine, database_url, geocoding_url, payments_url, clock):
    with open_system(
        engine=engine,
        database_url=database_url,
        geocoding_url=geocoding_url,
        payments_url=payments_url,
        clock=clock,
        payments_timeout=PAYMENTS_TIMEOUT_SECONDS,
    ) as system:
        yield system
