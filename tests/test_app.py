"""The HTTP face, exercised end to end through the app itself."""

from datetime import timedelta

from fastapi.testclient import TestClient

from feeder_station.app import create_app
from feeder_station.domain.traps import Registry

TAG = bytes([0x03, 0x84, 0x19, 0x9C, 0x87, 0xEC, 0x06])


def client() -> tuple[TestClient, Registry]:
    registry = Registry(silence_after=timedelta(minutes=5))
    return TestClient(create_app(registry)), registry


def test_health_answers() -> None:
    c, _ = client()
    assert c.get("/health").json()["ok"] is True


def test_the_page_loads_with_no_traps() -> None:
    c, _ = client()
    page = c.get("/")
    assert page.status_code == 200
    assert "Estación CICG" in page.text


def test_a_capture_is_visible_to_a_browser_that_arrives_later() -> None:
    """S3: the notification is a state, so being late does not miss it."""
    c, registry = client()
    from feeder_station.app import now

    registry.captured(node_id=7, tag=TAG, at=now())

    rows = c.get("/api/traps").json()
    assert rows[0]["capture"]["tag"] == "0384199C87EC06"

    c.post("/api/traps/7/acknowledge")
    assert c.get("/api/traps").json()[0]["capture"] is None
