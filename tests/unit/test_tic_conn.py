"""Unit tests for TicConn — Observatory + LiveDocument wiring (D3)."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from ocabox_tcs.services.guiding_svc.tic_conn import TicConn, _as_dict


def _make_manager() -> MagicMock:
    m = MagicMock()
    m.svc_logger = logging.getLogger("test")
    return m


# ---------------------------------------------------------------------------
# Construction defaults
# ---------------------------------------------------------------------------


def test_tic_conn_defaults():
    tc = TicConn(manager=_make_manager(), telescope_id="jk15")
    assert tc.client_name == "CliClient"
    assert tc.software_id == "guider/jk15"
    assert tc.enable_observatory is True
    assert tc.service_mode is True


def test_tic_conn_explicit_overrides():
    tc = TicConn(
        manager=_make_manager(),
        telescope_id="jk15",
        client_name="GuiderService",
        software_id="custom/x",
        enable_observatory=False,
        service_mode=False,
    )
    assert tc.client_name == "GuiderService"
    assert tc.software_id == "custom/x"
    assert tc.enable_observatory is False
    assert tc.service_mode is False


# ---------------------------------------------------------------------------
# Camera handle accessor — service mode
# ---------------------------------------------------------------------------


def test_get_camera_handle_sets_special_permission_when_service_mode():
    tc = TicConn(manager=_make_manager(), telescope_id="jk15")
    cam = MagicMock()
    cam.request_special_permission = False  # default
    telescope = MagicMock()
    telescope.get_camera = MagicMock(return_value=cam)
    tc.telescope = telescope

    out = tc.get_camera_handle("guider_beso")

    assert out is cam
    assert cam.request_special_permission is True
    telescope.get_camera.assert_called_once_with(id="guider_beso")


def test_get_camera_handle_skips_special_permission_when_service_mode_off():
    tc = TicConn(manager=_make_manager(), telescope_id="jk15", service_mode=False)
    cam = MagicMock()
    cam.request_special_permission = False
    telescope = MagicMock()
    telescope.get_camera = MagicMock(return_value=cam)
    tc.telescope = telescope

    out = tc.get_camera_handle("guider_beso")

    assert out is cam
    assert cam.request_special_permission is False  # untouched


def test_get_camera_handle_caches():
    tc = TicConn(manager=_make_manager(), telescope_id="jk15")
    cam = MagicMock()
    cam.request_special_permission = False
    telescope = MagicMock()
    telescope.get_camera = MagicMock(return_value=cam)
    tc.telescope = telescope

    a = tc.get_camera_handle("guider_beso")
    b = tc.get_camera_handle("guider_beso")
    assert a is b
    telescope.get_camera.assert_called_once()  # cached


def test_get_camera_handle_returns_none_when_telescope_unavailable():
    tc = TicConn(manager=_make_manager(), telescope_id="jk15")
    tc.telescope = None
    assert tc.get_camera_handle("guider_beso") is None


# ---------------------------------------------------------------------------
# Mount handle (parallel structure)
# ---------------------------------------------------------------------------


def test_get_mount_handle_sets_special_permission_when_service_mode():
    tc = TicConn(manager=_make_manager(), telescope_id="jk15")
    mount = MagicMock()
    mount.request_special_permission = False
    telescope = MagicMock()
    telescope.get_mount = MagicMock(return_value=mount)
    tc.telescope = telescope

    out = tc.get_mount_handle()

    assert out is mount
    assert mount.request_special_permission is True


# ---------------------------------------------------------------------------
# resolve_alpaca_endpoint
# ---------------------------------------------------------------------------


def test_resolve_alpaca_endpoint_uses_component_address_when_present():
    tc = TicConn(manager=_make_manager(), telescope_id="jk15")
    tc.obs_cfg = {
        "config": {
            "telescopes": {
                "jk15": {
                    "observatory": {
                        "address": "http://jk15-tcu.oca.lan:11111/api/v1",
                        "components": {
                            "guider_beso": {
                                "kind": "camera",
                                "device_number": 1,
                                "address": "http://jk15-ccd.oca.lan:11111/api/v1",
                            }
                        },
                    }
                }
            }
        }
    }
    out = tc.resolve_alpaca_endpoint("guider_beso")
    assert out == ("http://jk15-ccd.oca.lan:11111", 1)


def test_resolve_alpaca_endpoint_falls_back_to_observatory_address():
    tc = TicConn(manager=_make_manager(), telescope_id="jk15")
    tc.obs_cfg = {
        "config": {
            "telescopes": {
                "jk15": {
                    "observatory": {
                        "address": "http://jk15-tcu.oca.lan:11111/api/v1",
                        "components": {
                            "guider": {"kind": "camera", "device_number": 1}
                        },
                    }
                }
            }
        }
    }
    out = tc.resolve_alpaca_endpoint("guider")
    assert out == ("http://jk15-tcu.oca.lan:11111", 1)


def test_resolve_alpaca_endpoint_strips_only_api_v1_suffix():
    tc = TicConn(manager=_make_manager(), telescope_id="jk15")
    tc.obs_cfg = {
        "config": {
            "telescopes": {
                "jk15": {
                    "observatory": {
                        "components": {
                            "x": {"device_number": 0, "address": "http://h:1/api/v1/"}
                        }
                    }
                }
            }
        }
    }
    out = tc.resolve_alpaca_endpoint("x")
    assert out == ("http://h:1", 0)


def test_resolve_alpaca_endpoint_returns_none_when_component_missing():
    tc = TicConn(manager=_make_manager(), telescope_id="jk15")
    tc.obs_cfg = {
        "config": {"telescopes": {"jk15": {"observatory": {"components": {}}}}}
    }
    assert tc.resolve_alpaca_endpoint("does_not_exist") is None


def test_resolve_alpaca_endpoint_returns_none_when_no_livedoc():
    tc = TicConn(manager=_make_manager(), telescope_id="jk15")
    tc.obs_cfg = None
    assert tc.resolve_alpaca_endpoint("anything") is None


# ---------------------------------------------------------------------------
# camera_info pass-through
# ---------------------------------------------------------------------------


def test_camera_info_returns_component_dict_when_available():
    tc = TicConn(manager=_make_manager(), telescope_id="jk15")
    tc.obs_cfg = {
        "config": {
            "telescopes": {
                "jk15": {
                    "observatory": {
                        "components": {
                            "guider_beso": {
                                "kind": "camera",
                                "tertiary_position": 1,
                                "device_number": 1,
                            }
                        }
                    }
                }
            }
        }
    }
    info = tc.camera_info("guider_beso")
    assert info["kind"] == "camera"
    assert info["device_number"] == 1
    assert info["tertiary_position"] == 1


def test_camera_info_returns_stub_when_component_missing():
    tc = TicConn(manager=_make_manager(), telescope_id="jk15")
    tc.obs_cfg = None
    info = tc.camera_info("ghost")
    assert "resolution" in info
    assert info["model"] == "unknown"


# ---------------------------------------------------------------------------
# _as_dict helper
# ---------------------------------------------------------------------------


def test_as_dict_passes_through_dict():
    assert _as_dict({"a": 1}) == {"a": 1}


def test_as_dict_uses_to_dict_method_when_present():
    class WithToDict:
        def to_dict(self):
            return {"k": "v"}

    assert _as_dict(WithToDict()) == {"k": "v"}


def test_as_dict_returns_none_for_unsupported():
    assert _as_dict(None) is None
    assert _as_dict(42) is None
