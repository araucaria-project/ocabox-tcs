from unittest.mock import AsyncMock, MagicMock

import pytest
from serverish.base import MessengerNotPersisted
from serverish.messenger import Messenger

from ocabox_tcs.services.guiding_svc.nats_conn import NatsConn


async def test_connected_messenger_with_no_streams_still_checks_persistence(monkeypatch):
    messenger = MagicMock(is_open=False)
    messenger.connection.health_status = {"is_connected": True}
    monkeypatch.setattr("ocabox_tcs.services.guiding_svc.nats_conn.Messenger", lambda: messenger)
    monkeypatch.setattr(Messenger, "is_persistent", AsyncMock(return_value=False))
    conn = NatsConn(manager=MagicMock(), instance="jk15.cam_test")

    with pytest.raises(RuntimeError, match="oca_nats_config"):
        await conn.state_publisher("cam_test", "mon")


@pytest.mark.parametrize(
    ("method", "args", "subject"),
    [
        ("state_publisher", ("cam_test", "mon"), "pipeline.mon.state"),
        ("events_publisher", ("cam_test", "mon"), "pipeline.mon.events"),
        ("journal_publisher", ("cam_test", "mon"), "pipeline.mon.journal"),
        ("thumbnail_notification_publisher", ("cam_test",), "frame.thumbnail.ready"),
    ],
)
async def test_publishers_require_persistence(monkeypatch, method, args, subject):
    monkeypatch.setattr(NatsConn, "is_available", property(lambda self: True))
    check = AsyncMock(return_value=True)
    monkeypatch.setattr(Messenger, "is_persistent", check)
    conn = NatsConn(manager=MagicMock(), instance="jk15.cam_test")

    publisher = await getattr(conn, method)(*args)
    full_subject = f"svc.publish.guider.jk15.cam_test.{subject}"
    check.assert_awaited_once_with(full_subject)
    assert publisher.is_open and publisher.persistent and publisher.raise_on_publish_error
    await conn.close()
    assert not publisher.is_open


@pytest.mark.parametrize(
    ("method", "args"),
    [
        ("state_publisher", ("cam_test", "mon")),
        ("events_publisher", ("cam_test", "mon")),
        ("journal_publisher", ("cam_test", "mon")),
        ("thumbnail_notification_publisher", ("cam_test",)),
    ],
)
async def test_publisher_open_reports_missing_persistence(monkeypatch, method, args):
    monkeypatch.setattr(NatsConn, "is_available", property(lambda self: True))
    monkeypatch.setattr(Messenger, "is_persistent", AsyncMock(return_value=False))
    conn = NatsConn(manager=MagicMock(), instance="jk15.cam_test")

    with pytest.raises(RuntimeError, match="oca_nats_config") as exc:
        await getattr(conn, method)(*args)
    assert isinstance(exc.value.__cause__, MessengerNotPersisted)
    assert exc.value.__cause__.subject in str(exc.value)
    assert not conn._publishers
