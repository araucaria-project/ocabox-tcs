"""Optics service: decisions (pure), kind-driven readings, proven actuation, setpath — on the jk15
graph of the reference solver's conformance fixtures and a fake telescope."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from obcom.optics import ProvenState, parse_graph
from obcom.optics.conformance import JK15_WITH_MOUNT

from ocabox_tcs.services.optics.actuation import ActuationError, Actuator
from ocabox_tcs.services.optics.engine import MANUAL, decide, report, with_busy_holds, with_manual_holds
from ocabox_tcs.services.optics.readings import TelescopeReader
from ocabox_tcs.services.optics.service import OpticsService
from ocabox_tcs.tic_endpoint import Call, Refusal

GRAPH = parse_graph(JK15_WITH_MOUNT, telescope='jk15')
M3 = frozenset({'tertiary'})


def night(**positions):
    base = {'dome': 'open', 'covercalibrator': 'open', 'covercalibrator.calibrator': 'off', 'tertiary': 'beso'}
    return ProvenState.build({**base, **positions}, sun_alt_deg=-30, dome_shutter_open=True)


# ---------------------------------------------------------------------------------------- engine

class TestDecide:

    def test_settable_returns_the_moves(self):
        assert decide(GRAPH, night(), 'camera', 'object', M3) == {'tertiary': 'andor'}

    def test_active_returns_no_moves(self):
        assert decide(GRAPH, night(tertiary='andor'), 'camera', 'object', M3) == {}

    def test_prefers_an_alternative_the_service_can_realise(self):
        # dark_strict: closed cover at night first, M3 away second — the cover is manual
        assert decide(GRAPH, night(tertiary='andor'), 'camera', 'dark_strict', M3) == {'tertiary': 'beso'}

    def test_manual_move_is_refused_with_the_move_spelled_out(self):
        with pytest.raises(Refusal) as e:
            decide(GRAPH, night(tertiary='andor'), 'camera', 'domeflat', M3)
        assert e.value.code == 4009 and 'dome → flat' in e.value.message and 'own controls' in e.value.message

    def test_busy_detector_path_is_a_temporary_refusal_naming_it(self):
        state = with_busy_holds(GRAPH, night(), {'guider_beso': 'exposing'})
        with pytest.raises(Refusal) as e:
            decide(GRAPH, state, 'camera', 'object', M3)
        assert (e.value.code, e.value.severity.value) == (4008, 'TEMPORARY')
        assert 'guider_beso (exposing)' in e.value.message

    def test_unknown_detector_and_function(self):
        for detector, function in (('beso', 'object'), ('camera', 'arc')):
            with pytest.raises(Refusal) as e:
                decide(GRAPH, night(), detector, function, M3)
            assert e.value.code == 4007

    def test_derived_dome_is_never_moved(self):
        # no dome telemetry: its position is derived from shutter + pointing (the live case)
        state = ProvenState.build({'covercalibrator': 'open', 'covercalibrator.calibrator': 'off', 'tertiary': 'andor'},
                                  sun_alt_deg=-30, dome_shutter_open=True, dome_az_deg=120.0, mount_az_deg=10.0, mount_alt_deg=60.0)
        with pytest.raises(Refusal) as e:
            decide(GRAPH, state, 'camera', 'domeflat', M3)
        assert e.value.code == 4009 and 'dome → flat' in e.value.message

    def test_undefined_m3_proves_nothing(self):
        state = ProvenState.build({'dome': 'open', 'covercalibrator': 'open', 'covercalibrator.calibrator': 'off',
                                   'tertiary': None}, sun_alt_deg=-30, dome_shutter_open=True)
        assert decide(GRAPH, state, 'camera', 'object', M3) == {'tertiary': 'andor'}  # move it to a proven port

    def test_holds_compose(self):
        # the andor-side guider sees the back of M3 (dark @ tertiary): taking a dark, it holds M3 only
        state = with_manual_holds(GRAPH, with_busy_holds(GRAPH, night(), {'guider': 'exposing'}), M3)
        assert state.holds['tertiary'].holder == 'guider (exposing)'  # the busy hold wins
        assert state.holds['dome'].holder == MANUAL


class TestReport:

    def test_sees_and_function_verdicts_per_detector(self):
        r = report(GRAPH, night(), M3)
        assert set(r) == set(GRAPH.detectors)
        assert r['camera']['sees'] == [{'class': 'dark', 'terminal': 'tertiary', 'via': ['derotator', 'pickoff', 'filterwheel']}]
        assert r['camera']['functions']['object']['kind'] == 'settable'
        assert r['guider_beso']['sees'][0]['class'] == 'sky.science'

    def test_verdicts_announce_what_setpath_does(self):
        # dark_strict: the closed cover comes first physically, but the service moves only M3 —
        # the panel must announce M3 → beso, and domeflat as the operator's move, not a SET PATH
        functions = report(GRAPH, night(tertiary='andor'), M3)['camera']['functions']
        assert functions['dark_strict'] == {'kind': 'settable', 'see': 'dark', 'positions': {'tertiary': 'beso'},
                                            'moves': {'tertiary': 'beso'}}
        assert functions['domeflat']['kind'] == 'collision' and functions['domeflat']['holder'] == MANUAL
        assert "dome is held at 'open' by its own controls" in functions['domeflat']['reason']

    @pytest.mark.parametrize('state', [night(), night(tertiary='andor'), night(covercalibrator='close'),
                                       with_busy_holds(GRAPH, night(tertiary='andor'), {'guider_beso': 'exposing'})])
    def test_published_verdict_and_setpath_never_disagree(self, state):
        for detector, entry in report(GRAPH, state, M3).items():
            for function, published in entry['functions'].items():
                try:
                    moves = decide(GRAPH, state, detector, function, M3)
                except Refusal as e:
                    assert published['kind'] in ('collision', 'impossible'), (detector, function, e)
                    assert e.code == (4008 if published.get('holder') not in (None, MANUAL) else 4009)
                else:
                    assert published['kind'] in ('active', 'settable'), (detector, function)
                    assert moves == published.get('moves', {})


# ---------------------------------------------------------------------------------------- fakes

class FakeComponent:
    def __init__(self, values):
        self.values = values
        self.request_special_permission = False
        self.aput_selectnasmythport = AsyncMock(side_effect=self._select)

    async def aget_value(self, attribute, time_of_data_tolerance=None):
        value = self.values[attribute]
        if isinstance(value, Exception):
            raise value
        return value

    async def _select(self, port):
        self.values['tertiarystatus'] = {'NasmythPort': port, 'Moving': False}


class FakeTelescope:
    def __init__(self, port=1, moving=False, camerastate=0, guider_beso_state=0):
        self.c = {
            'tertiary': FakeComponent({'tertiarystatus': {'NasmythPort': port, 'Moving': moving}}),
            'covercalibrator': FakeComponent({'coverstate': 3, 'calibratorstate': 1}),
            'dome': FakeComponent({'shutterstatus': 0, 'az': 120.0}),
            'mount': FakeComponent({'az': 10.0, 'alt': 60.0}),
            'camera': FakeComponent({'camerastate': camerastate}),
            'guider': FakeComponent({'camerastate': 0}),
            'guider_beso': FakeComponent({'camerastate': guider_beso_state}),
        }

    def __getattr__(self, name):
        if name.startswith('get_'):
            return lambda component: self.c[component]
        raise AttributeError(name)


def reader(tel):
    return TelescopeReader(tel, GRAPH, tolerance=1.0, sun_altitude=lambda: -30.0)


# ---------------------------------------------------------------------------------------- readings

class TestReadings:

    async def test_reads_by_kind(self):
        snap = await reader(FakeTelescope(port=2)).read()
        s = snap.state.selectors
        assert s['tertiary'].position == 'andor' and s['covercalibrator'].position == 'open'
        assert s['covercalibrator.calibrator'].position == 'off'
        env = snap.state.environment
        assert (env.dome_shutter_open, env.dome_az_deg, env.mount_alt_deg, env.sun_alt_deg) == (True, 120.0, 60.0, -30.0)
        assert snap.busy == {} and snap.failed == frozenset()

    async def test_unmapped_port_and_moving(self):
        s = (await reader(FakeTelescope(port=7, moving=True)).read()).state.selectors
        assert s['tertiary'].position is None and s['tertiary'].moving

    async def test_failed_read_is_undefined_and_an_unread_camera_counts_as_busy(self):
        tel = FakeTelescope()
        tel.c['tertiary'].values['tertiarystatus'] = TimeoutError('TIC did not answer')
        tel.c['camera'].values['camerastate'] = ConnectionError('down')
        snap = await reader(tel).read()
        assert 'tertiary' not in snap.state.selectors
        assert snap.busy == {'camera': 'state unknown'}
        assert snap.failed == {'tertiary', 'camera'}

    async def test_exposing_camera_is_busy(self):
        assert (await reader(FakeTelescope(guider_beso_state=2)).read()).busy == {'guider_beso': 'exposing'}


# ---------------------------------------------------------------------------------------- actuation

class TestActuation:

    async def test_moves_m3_with_special_permission_and_proves_it(self):
        tel = FakeTelescope(port=1)
        await Actuator(tel, GRAPH, reader(tel), prove_timeout=2, poll=0.01).apply({'tertiary': 'andor'})
        tel.c['tertiary'].aput_selectnasmythport.assert_awaited_once_with(2)
        assert tel.c['tertiary'].request_special_permission

    async def test_not_proven_in_time_fails_with_what_it_reads(self):
        tel = FakeTelescope(port=1)
        tel.c['tertiary'].aput_selectnasmythport = AsyncMock()  # TIC accepts, M3 never arrives
        with pytest.raises(ActuationError) as e:
            await Actuator(tel, GRAPH, reader(tel), prove_timeout=0.05, poll=0.01).apply({'tertiary': 'andor'})
        assert 'tertiary reads beso, wanted andor' in str(e.value)

    async def test_no_actuator_for_a_kind(self):
        tel = FakeTelescope()
        with pytest.raises(ActuationError) as e:
            await Actuator(tel, GRAPH, reader(tel)).apply({'covercalibrator': 'close'})
        assert "no actuator for kind 'covercalibrator'" in str(e.value)


# ---------------------------------------------------------------------------------------- service

def service(tel):
    svc = OpticsService()
    svc.svc_config = SimpleNamespace(actuate=['tertiary'], interval=0.01)
    svc.svc_logger = MagicMock()
    svc.graph = GRAPH
    svc._reader = reader(tel)
    svc._actuator = Actuator(tel, GRAPH, svc._reader, prove_timeout=2, poll=0.01)
    svc.endpoint = MagicMock(publish=AsyncMock())
    return svc


def operations(svc):
    return [c.args[2] for c in svc.endpoint.publish.await_args_list if c.args[:2] == ('self', 'operation')]


class TestSetpath:

    async def test_accepts_switches_and_reports_the_operation(self):
        tel = FakeTelescope(port=1)
        svc = service(tel)
        answer = await svc._setpath(Call('camera', 'setpath', {'function': 'object'}, None, 5))
        assert answer == {'accepted': True, 'moves': {'tertiary': 'andor'}}
        await svc._operation
        assert [o['state'] for o in operations(svc)] == ['moving', 'done']
        assert tel.c['tertiary'].values['tertiarystatus']['NasmythPort'] == 2

    async def test_already_there_moves_nothing(self):
        svc = service(FakeTelescope(port=2))
        assert await svc._setpath(Call('camera', 'setpath', {'function': 'object'}, None, 5)) == {'accepted': True, 'moves': {}}
        assert svc._operation is None

    async def test_one_switch_at_a_time(self):
        tel = FakeTelescope(port=1)
        svc = service(tel)
        svc._operation = asyncio.get_running_loop().create_future()
        with pytest.raises(Refusal) as e:
            await svc._setpath(Call('camera', 'setpath', {'function': 'object'}, None, 5))
        assert e.value.code == 4008
        svc._operation.cancel()

    async def test_function_parameter_required(self):
        with pytest.raises(Refusal) as e:
            await service(FakeTelescope())._setpath(Call('camera', 'setpath', {}, None, 5))
        assert e.value.code == 4007

    async def test_failed_switch_is_reported(self):
        tel = FakeTelescope(port=1)
        tel.c['tertiary'].aput_selectnasmythport = AsyncMock(side_effect=RuntimeError('1005 safety cutoff'))
        svc = service(tel)
        await svc._setpath(Call('camera', 'setpath', {'function': 'object'}, None, 5))
        await svc._operation
        failed = operations(svc)[-1]
        assert failed['state'] == 'failed' and 'safety cutoff' in failed['message']


class FakeObservatory:
    """``ocaboxapi.Observatory`` as ``on_start`` uses it; ``published`` = the TIC config stream."""
    published: dict = {}

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        FakeObservatory.instance = self

    async def load_client_cfg(self):
        pass

    def get_telescopes_configuration(self):
        return self.published

    def connect(self):
        pass

    def get_telescope(self, tel):
        return FakeTelescope()


class TestStart:

    @pytest.fixture
    def svc(self, monkeypatch):
        import ocaboxapi
        import ocabox_tcs.services.optics.service as module
        monkeypatch.setattr(ocaboxapi, 'Observatory', FakeObservatory)
        monkeypatch.setattr(module.TicEndpoint, 'for_service',
                            classmethod(lambda cls, svc, **kw: MagicMock(publish=AsyncMock(), start=AsyncMock())))
        svc = OpticsService()
        svc.svc_config = SimpleNamespace(telescope='jk15', variant='jk15', client_name='CliClient', components_file='',
                                         config_stream='tic.config.observatory', actuate=['tertiary'], interval=1.0,
                                         prove_timeout=60.0, keepalive=60.0)
        svc.svc_logger = MagicMock()
        return svc

    async def test_reads_the_graph_published_by_tic(self, svc):
        FakeObservatory.published = {'jk15': {'observatory': {'components': JK15_WITH_MOUNT}}}
        await svc.on_start()
        assert FakeObservatory.instance.kwargs['config_stream'] == 'tic.config.observatory'
        assert FakeObservatory.instance.kwargs['policy'] == 'service'
        assert set(svc.graph.detectors) == set(GRAPH.detectors)

    async def test_telescope_missing_from_the_stream_stops_the_start(self, svc):
        FakeObservatory.published = {}
        with pytest.raises(RuntimeError, match="jk15: not in the observatory configuration from 'tic.config.observatory'"):
            await svc.on_start()

    async def test_a_graph_without_detectors_stops_the_start(self, svc):
        FakeObservatory.published = {'jk15': {'observatory': {'components': {'telescope': {'kind': 'telescope'}}}}}
        with pytest.raises(RuntimeError, match='no detector with `paths:`'):
            await svc.on_start()
