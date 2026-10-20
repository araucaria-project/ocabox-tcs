"""Optics service: decisions (pure), kind-driven readings, proven actuation, setpath — on the jk15
graph of the reference solver's conformance fixtures and a fake telescope."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from ocaboxapi import Camera, CoverCalibrator, Dome, Mount
from ocaboxapi.tertiary import Tertiary
from obcom.optics import ProvenState, parse_graph, published_values, ribbon
from obcom.optics.conformance import JK15_WITH_MOUNT

from ocabox_tcs.services.optics.actuation import ActuationError, Actuator
from ocabox_tcs.services.optics.engine import MANUAL, decide, report, with_busy_holds, with_manual_holds
from ocabox_tcs.services.optics.readings import TelescopeReader
from ocabox_tcs.services.optics.service import OpticsService
from ocabox_tcs.tic_endpoint import Call, Refusal

GRAPH = parse_graph(JK15_WITH_MOUNT, telescope='jk15')
M3 = frozenset({'tertiary'})


def broken_shutter(**positions):
    """jk15 on 2026-10-19: the shutter reports an error (ShutterStatus 4), so the derived dome is
    undefined; the operator opened the cover by hand."""
    base = {'covercalibrator': 'open', 'covercalibrator.calibrator': 'off', 'tertiary': 'andor'}
    return ProvenState.build({**base, **positions}, sun_alt_deg=-30, dome_shutter_open=None,
                             dome_az_deg=180.0, mount_az_deg=0.0, mount_alt_deg=45.0)


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
        assert e.value.code == 4009 and 'needs dome flat, it is open: set it with its own controls' in e.value.message and 'domeflat' in e.value.message

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
        assert e.value.code == 4009 and 'camera domeflat needs dome flat, it is open' in e.value.message

    def test_undefined_m3_proves_nothing(self):
        state = ProvenState.build({'dome': 'open', 'covercalibrator': 'open', 'covercalibrator.calibrator': 'off',
                                   'tertiary': None}, sun_alt_deg=-30, dome_shutter_open=True)
        assert decide(GRAPH, state, 'camera', 'object', M3) == {'tertiary': 'andor'}  # move it to a proven port

    def test_holds_compose(self):
        # the andor-side guider sees the back of M3 (dark @ tertiary): taking a dark, it holds M3 only
        state = with_manual_holds(GRAPH, with_busy_holds(GRAPH, night(), {'guider': 'exposing'}), M3)
        assert state.holds['tertiary'].holder == 'guider (exposing)'  # the busy hold wins
        assert state.holds['dome'].holder == MANUAL


class TestBrokenShutter:

    def test_dark_goes_around_the_unknown_dome(self):
        functions = report(GRAPH, broken_shutter(), M3)['camera']['functions']
        for function in ('dark', 'zero', 'dark_strict'):
            assert functions[function]['kind'] == 'settable', function
            assert functions[function]['moves'] == {'tertiary': 'beso'}, function

    def test_sky_cannot_be_proven_and_is_refused_naming_the_dome(self):
        functions = report(GRAPH, broken_shutter(), M3)['camera']['functions']
        assert functions['object']['kind'] == 'impossible'
        assert functions['object']['reason'] == 'dome not known (shutter state unknown) — confirm it by hand if you know where it is'
        with pytest.raises(Refusal) as e:
            decide(GRAPH, broken_shutter(), 'camera', 'object', M3)
        assert e.value.code == 4009 and 'dome' in e.value.message


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
        assert functions['domeflat']['reason'] == ('needs dome flat, it is open: set it with its own controls '
                                                   '(the optics service does not move it)')

    @pytest.mark.parametrize('state', [night(), night(tertiary='andor'), night(covercalibrator='close'), broken_shutter(),
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
    """A component of ``spec`` (its ocaboxapi class): reading an attribute the real class does not
    have fails the test, so the reader cannot drift from the device API (``az`` vs ``azimuth``)."""

    def __init__(self, spec, values):
        unknown = set(values) - set(spec.param)
        assert not unknown, f'{spec.__name__} has no {sorted(unknown)}'
        self.values = values
        self.request_special_permission = False
        effects = {  # commands of the real class, answered the way the device would end up reading
            'aput_selectnasmythport': self._select,
            'aput_opencover': lambda: self.values.update(coverstate=3),
            'aput_closecover': lambda: self.values.update(coverstate=1),
            'aput_calibratoroff': lambda: self.values.update(calibratorstate=1),
            'aput_calibratoron': lambda brightness: self.values.update(calibratorstate=3),
        }
        for command, effect in effects.items():
            if hasattr(spec, command):
                setattr(self, command, AsyncMock(side_effect=effect))

    async def aget_value(self, attribute, time_of_data_tolerance=None):
        value = self.values[attribute]
        if isinstance(value, Exception):
            raise value
        return value

    def _select(self, port):
        self.values['tertiarystatus'] = {'NasmythPort': port, 'Moving': False}


class FakeTelescope:
    def __init__(self, port=1, moving=False, camerastate=0, guider_beso_state=0):
        self.c = {
            'tertiary': FakeComponent(Tertiary, {'tertiarystatus': {'NasmythPort': port, 'Moving': moving}}),
            'covercalibrator': FakeComponent(CoverCalibrator, {'coverstate': 3, 'calibratorstate': 1}),
            'dome': FakeComponent(Dome, {'shutterstatus': 0, 'azimuth': 120.0}),
            'mount': FakeComponent(Mount, {'azimuth': 10.0, 'altitude': 60.0}),
            'camera': FakeComponent(Camera, {'camerastate': camerastate}),
            'guider': FakeComponent(Camera, {'camerastate': 0}),
            'guider_beso': FakeComponent(Camera, {'camerastate': guider_beso_state}),
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
        assert 'M3 reads beso, wanted andor' in str(e.value)

    async def test_closes_the_mirror_covers_and_proves_it(self):
        tel = FakeTelescope()
        await Actuator(tel, GRAPH, reader(tel), prove_timeout=2, poll=0.01).apply({'covercalibrator': 'close'})
        tel.c['covercalibrator'].aput_closecover.assert_awaited_once()
        assert tel.c['covercalibrator'].request_special_permission

    async def test_switches_the_calibration_lamp_off(self):
        tel = FakeTelescope()
        tel.c['covercalibrator'].values['calibratorstate'] = 3
        await Actuator(tel, GRAPH, reader(tel), prove_timeout=2, poll=0.01).apply({'covercalibrator.calibrator': 'off'})
        tel.c['covercalibrator'].aput_calibratoroff.assert_awaited_once()

    async def test_never_lights_the_lamp_at_a_guessed_brightness(self):
        tel = FakeTelescope()
        with pytest.raises(ActuationError, match='no calibrator_brightness configured'):
            await Actuator(tel, GRAPH, reader(tel)).apply({'covercalibrator.calibrator': 'on'})
        tel.c['covercalibrator'].aput_calibratoron.assert_not_awaited()

    async def test_the_dome_is_no_optical_lever(self):
        tel = FakeTelescope()
        with pytest.raises(ActuationError, match="dome → flat: no actuator for kind 'dome'"):
            await Actuator(tel, GRAPH, reader(tel)).apply({'dome': 'flat'})


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

    async def test_unknown_function_is_4007_even_during_a_switch(self):
        svc = service(FakeTelescope(port=1))
        svc._operation = asyncio.get_running_loop().create_future()
        with pytest.raises(Refusal) as e:
            await svc._setpath(Call('camera', 'setpath', {'function': 'nonexistent'}, None, 5))
        assert e.value.code == 4007 and "declares no function 'nonexistent'" in e.value.message
        svc._operation.cancel()

    async def test_concurrent_requests_start_one_switch(self):
        tel = FakeTelescope(port=1)
        svc = service(tel)
        results = await asyncio.gather(*(svc._setpath(Call('camera', 'setpath', {'function': 'object'}, None, 5))
                                         for _ in range(3)), return_exceptions=True)
        accepted = [r for r in results if isinstance(r, dict)]
        refused = [r for r in results if isinstance(r, Refusal)]
        assert len(accepted) == 1 and len(refused) == 2 and all(r.code == 4008 for r in refused)
        await svc._operation
        assert tel.c['tertiary'].aput_selectnasmythport.await_count == 1

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

    async def test_old_ocabox_common_stops_the_start(self, svc, monkeypatch):
        import ocabox_tcs.services.optics.service as module
        monkeypatch.setattr(module, 'version', lambda package: '1.7.0')
        with pytest.raises(RuntimeError, match='ocabox-common 1.7.0 is too old'):
            await svc.on_start()

    async def test_a_graph_without_detectors_stops_the_start(self, svc):
        FakeObservatory.published = {'jk15': {'observatory': {'components': {'telescope': {'kind': 'telescope'}}}}}
        with pytest.raises(RuntimeError, match='no detector with `paths:`'):
            await svc.on_start()


# ---------------------------------------------------------------------------------------- causes

class TestCauses:

    async def test_a_broken_shutter_is_named_as_the_shutter(self):
        tel = FakeTelescope(port=2)
        tel.c['dome'].values['shutterstatus'] = 4
        snap = await reader(tel).read()
        assert snap.state.causes['dome'] == 'shutter reports Error (ShutterState 4)'

    async def test_a_faulted_m3_says_so(self):
        tel = FakeTelescope(port=2)
        tel.c['tertiary'].values['tertiarystatus'] = {'NasmythPort': 2, 'Moving': False, 'ErrorRaised': True}
        snap = await reader(tel).read()
        assert snap.state.selectors['tertiary'].position is None
        assert snap.state.causes['tertiary'] == 'controller reports an error (ErrorRaised)'

    async def test_a_failed_read_explains_every_axis_of_the_component(self):
        tel = FakeTelescope(port=2)
        tel.c['covercalibrator'].values['coverstate'] = TimeoutError('TIC did not answer')
        snap = await reader(tel).read()
        assert snap.state.causes['covercalibrator'] == 'not read (TimeoutError: TIC did not answer)'
        assert snap.state.causes['covercalibrator.calibrator'] == 'not read (TimeoutError: TIC did not answer)'


# ---------------------------------------------------------------------------------------- declarations

def caller(name='mka'):
    return SimpleNamespace(name=name)


class TestDeclarations:

    def broken_shutter_service(self):
        tel = FakeTelescope(port=2)
        tel.c['dome'].values['shutterstatus'] = 4
        svc = service(tel)
        svc.svc_config.declaration_hours = 12.0
        return svc, tel

    async def test_the_operator_vouches_for_the_dome_and_object_becomes_active(self):
        svc, _ = self.broken_shutter_service()
        await svc._publish(await svc._reader.read())
        assert published(svc, 'camera', 'functions')['object']['kind'] == 'impossible'
        answer = await svc._declare(Call('self', 'declare', {'key': 'dome', 'position': 'open'}, caller(), 5))
        assert answer['declared']['dome']['position'] == 'open' and answer['declared']['dome']['by'] == 'mka'
        assert published(svc, 'camera', 'functions')['object']['kind'] == 'active'
        dome = published(svc, 'self', 'selectors')['dome']
        assert (dome['position'], dome['origin'], dome['by']) == ('open', 'declared', 'mka')
        assert dome['cause'] == 'shutter reports Error (ShutterState 4)' and dome['label'] == 'dome'

    async def test_published_axes_draw_the_same_path_as_the_service_sees(self):
        svc, _ = self.broken_shutter_service()
        snapshot = await svc._reader.read()
        await svc._publish(snapshot)
        axes = published(svc, 'self', 'selectors')
        assert axes['sky']['origin'] == 'derived'  # sources too: a client draws where the light starts
        positions = {key: entry['position'] for key, entry in axes.items()}
        assert ribbon(svc.graph, published_values(positions), 'camera') == ribbon(svc.graph, snapshot.state, 'camera')

    async def test_a_reading_withdraws_the_declaration(self):
        svc, tel = self.broken_shutter_service()
        await svc._declare(Call('self', 'declare', {'key': 'dome', 'position': 'open'}, caller(), 5))
        tel.c['dome'].values['shutterstatus'] = 0  # the sensor works again
        await svc._publish(await svc._reader.read())
        assert published(svc, 'self', 'declared') == {}
        assert published(svc, 'self', 'selectors')['dome']['origin'] == 'derived'

    async def test_a_declaration_expires(self):
        svc, _ = self.broken_shutter_service()
        await svc._declare(Call('self', 'declare', {'key': 'dome', 'position': 'open'}, caller(), 5))
        svc._declared['dome']['at'] -= 13 * 3600
        await svc._publish(await svc._reader.read())
        assert published(svc, 'self', 'declared') == {}

    async def test_withdrawn_on_request(self):
        svc, _ = self.broken_shutter_service()
        await svc._declare(Call('self', 'declare', {'key': 'dome', 'position': 'open'}, caller(), 5))
        await svc._declare(Call('self', 'declare', {'key': 'dome', 'position': None}, caller(), 5))
        assert published(svc, 'self', 'declared') == {}

    async def test_what_a_reading_proves_needs_no_declaration(self):
        svc = service(FakeTelescope(port=2))
        with pytest.raises(Refusal, match='M3 reads andor: a reading needs no declaration'):
            await svc._declare(Call('self', 'declare', {'key': 'tertiary', 'position': 'beso'}, caller(), 5))

    async def test_a_lamp_switched_on_by_hand(self):
        svc, _ = self.broken_shutter_service()
        svc.graph = parse_graph({**JK15_WITH_MOUNT, 'thar_lamp': {'kind': 'lamp', 'light': 'lamp.thar'}}, telescope='jk15')
        await svc._declare(Call('self', 'declare', {'key': 'thar_lamp', 'position': 'on'}, caller(), 5))
        lamp = published(svc, 'self', 'selectors')['thar_lamp']
        assert (lamp['position'], lamp['origin'], lamp['by']) == ('on', 'declared', 'mka')

    async def test_a_derived_source_needs_no_declaration(self):
        svc, _ = self.broken_shutter_service()
        with pytest.raises(Refusal, match='a reading needs no declaration'):
            await svc._declare(Call('self', 'declare', {'key': 'sky', 'position': 'day'}, caller(), 5))

    async def test_unknown_key_or_position(self):
        svc, _ = self.broken_shutter_service()
        for parameters in ({'key': 'sky', 'position': 'open'}, {'key': 'dome', 'position': 'ajar'}):
            with pytest.raises(Refusal) as e:
                await svc._declare(Call('self', 'declare', parameters, caller(), 5))
            assert e.value.code == 4007


def published(svc, element, item):
    """The last value the service published for ``element.item``."""
    return [c.args[2] for c in svc.endpoint.publish.await_args_list if c.args[:2] == (element, item)][-1]
