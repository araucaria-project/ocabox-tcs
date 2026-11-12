from unittest.mock import AsyncMock, MagicMock

import pytest
from serverish.base import MessengerNotConnected, MessengerNotPersisted
from serverish.messenger import Messenger

from ocabox_tcs.services.guiding_svc.nats_conn import NatsConn


@pytest.mark.parametrize("is_open", [True, False])
def test_availability_follows_messenger(monkeypatch, is_open):
    messenger = MagicMock(is_open=is_open)
    monkeypatch.setattr("ocabox_tcs.services.guiding_svc.nats_conn.Messenger", lambda: messenger)
    conn = NatsConn(manager=MagicMock(), instance="jk15.cam_test")

    assert conn.is_available is is_open
    messenger.connection.health_status.__getitem__.assert_not_called()


@pytest.mark.parametrize(
    ("method", "args", "subject"),
    [
        ("state_publisher", ("cam_test", "mon"), "pipeline.mon.state"),
        ("events_publisher", ("cam_test", "mon"), "pipeline.mon.events"),
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


async def test_journal_publisher_keeps_non_raising_defaults(monkeypatch):
    monkeypatch.setattr(NatsConn, "is_available", property(lambda self: True))
    check = AsyncMock(return_value=False)
    monkeypatch.setattr(Messenger, "is_persistent", check)
    conn = NatsConn(manager=MagicMock(), instance="jk15.cam_test")

    publisher = await conn.journal_publisher("cam_test", "mon")
    assert publisher.subject == "svc.publish.guider.jk15.cam_test.pipeline.mon.journal"
    assert publisher.is_open
    assert not publisher.persistent
    assert not publisher.raise_on_publish_error
    check.assert_not_awaited()
    monkeypatch.setattr(
        publisher, "_publish_with_ack_retries", AsyncMock(side_effect=MessengerNotConnected("offline"))
    )
    message = await publisher.publish(data={"message": "test"})
    assert "error" in message["meta"]["tags"]
    await conn.close()
    assert not publisher.is_open
