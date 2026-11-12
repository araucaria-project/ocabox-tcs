import logging

import pytest
from serverish.base.exceptions import MessengerJetStreamError, MessengerReaderConfigError

from tcsctl.client import ServiceControlClient


@pytest.mark.parametrize(
    ("subject", "label"),
    [
        ("registry.>", "Registry"),
        ("status.>", "Status"),
        ("heartbeat.>", "Heartbeat"),
        ("registry.crashed.>", None),
    ],
)
@pytest.mark.parametrize("stage", ["open", "read"])
@pytest.mark.parametrize("error", ["config", "404", "503"])
async def test_snapshot_reader_errors(monkeypatch, caplog, subject, label, stage, error):
    class Reader:
        def __init__(self, subject, **kwargs):
            self.subject = subject

        def fail(self):
            if self.subject != f"svc.{subject}":
                return
            if error == "config":
                raise MessengerReaderConfigError("consumer rejected")
            raise MessengerJetStreamError("broker error", code=int(error))

        async def __aenter__(self):
            if stage == "open":
                self.fail()
            return self

        async def __aexit__(self, *args):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            if stage == "read":
                self.fail()
            raise StopAsyncIteration

    monkeypatch.setattr("tcsctl.client.MsgReader", Reader)
    with caplog.at_level(logging.WARNING, logger="tcsctl.client"):
        assert await ServiceControlClient(messenger=None)._collect_snapshot() == []

    if error == "503":
        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.ERROR
        assert "broker error" in caplog.text
    elif label:
        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.WARNING
        assert f"{label} stream not found or reader rejected" in caplog.text
    else:
        assert not caplog.records
