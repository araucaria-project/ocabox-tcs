"""Optics service: decisions (pure), kind-driven readings, proven actuation, setpath — on the jk15
graph of the reference solver's conformance fixtures and a fake telescope."""
import asyncio
import json
import time
from importlib.resources import files
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from ocaboxapi import Camera, CoverCalibrator, Dome, Mount
from ocaboxapi.tertiary import Tertiary
from obcom.comunication.error_policy import ValuePolicy
from obcom.data_colection.response_error import ResponseError
from obcom.data_colection.value import Value
from obcom.data_colection.value_call import ValueResponse
from obcom.optics import ProvenState, parse_graph, published_values, ribbon
from obcom.optics.conformance import JK15_WITH_MOUNT

from ocabox_tcs.services.optics.actuation import ActuationError, Actuator
from ocabox_tcs.services.optics.confirmation import ConfirmPolicy
from ocabox_tcs.services.optics.engine import MANUAL, decide, report, with_busy_holds, with_manual_holds
from ocabox_tcs.services.optics.readings import TelescopeReader
from ocabox_tcs.services.optics.service import OpticsService
from ocabox_tcs.services.optics.sun import Sun
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
        assert decide(GRAPH, night(), 'camera', 'object', M3).moves == {'tertiary': 'andor'}

    def test_active_returns_no_moves(self):
        assert decide(GRAPH, night(tertiary='andor'), 'camera', 'object', M3).moves == {}

    def test_prefers_an_alternative_the_service_can_realise(self):
        # dark_strict: closed cover at night first, M3 away second — the cover is manual
        assert decide(GRAPH, night(tertiary='andor'), 'camera', 'dark_strict', M3).moves == {'tertiary': 'beso'}

    def test_manual_move_is_refused_with_the_move_spelled_out(self):
        with pytest.raises(Refusal) as e:
            decide(GRAPH, night(tertiary='andor', flatscreen='on'), 'camera', 'domeflat', M3)
        assert e.value.code == 4009
        assert e.value.message == ('camera domeflat needs dome flat — the telescope at the flat screen: dome at 49°, '
                                   'telescope at az 229° alt 15°; it is open now, the optics service does not move it')

    def test_an_unlit_flat_screen_is_for_the_operator_to_confirm(self):
        with pytest.raises(Refusal) as e:
            decide(GRAPH, night(tertiary='andor'), 'camera', 'domeflat', M3)
        assert e.value.code == 4009 and 'flat screen is not known' in e.value.message
        assert e.value.message.endswith('if you know, confirm it by hand')

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
        state = ProvenState.build({'covercalibrator': 'open', 'covercalibrator.calibrator': 'off', 'tertiary': 'andor', 'flatscreen': 'on'},
                                  sun_alt_deg=-30, dome_shutter_open=True, dome_az_deg=120.0, mount_az_deg=10.0, mount_alt_deg=60.0)
        with pytest.raises(Refusal) as e:
            decide(GRAPH, state, 'camera', 'domeflat', M3)
        assert e.value.code == 4009 and 'camera domeflat needs dome flat — the telescope at the flat screen' in e.value.message

    def test_undefined_m3_proves_nothing(self):
        state = ProvenState.build({'dome': 'open', 'covercalibrator': 'open', 'covercalibrator.calibrator': 'off',
                                   'tertiary': None}, sun_alt_deg=-30, dome_shutter_open=True)
        assert decide(GRAPH, state, 'camera', 'object', M3).moves == {'tertiary': 'andor'}  # move it to a proven port

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
        functions = report(GRAPH, night(tertiary='andor', flatscreen='on'), M3)['camera']['functions']
        assert functions['dark_strict'] == {'kind': 'settable', 'see': 'dark', 'positions': {'tertiary': 'beso'},
                                            'moves': {'tertiary': 'beso'}}  # no confirm: defaults stay off the wire
        assert functions['domeflat']['kind'] == 'collision' and functions['domeflat']['holder'] == MANUAL
        assert functions['domeflat']['reason'] == ('needs dome flat — the telescope at the flat screen: dome at 49°, '
                                                   'telescope at az 229° alt 15°; it is open now, the optics service does not move it')

    @pytest.mark.parametrize('state', [night(), night(tertiary='andor'), night(covercalibrator='close'), broken_shutter(),
                                       with_busy_holds(GRAPH, night(tertiary='andor'), {'guider_beso': 'exposing'})])
    def test_published_verdict_and_setpath_never_disagree(self, state):
        for detector, entry in report(GRAPH, state, M3).items():
            for function, published in entry['functions'].items():
                try:
                    moves = decide(GRAPH, state, detector, function, M3).moves
                except Refusal as e:
                    assert published['kind'] in ('collision', 'impossible'), (detector, function, e)
                    assert e.code == (4008 if published.get('holder') not in (None, MANUAL) else 4009)
                else:
                    assert published['kind'] in ('active', 'settable'), (detector, function)
                    assert moves == published.get('moves', {})


# ---------------------------------------------------------------------------------------- fakes

class FakeTIC:
    """TIC as the reader uses it: subscriptions that deliver the current value at once and every
    value set later — the way a cycle query delivers changes. A value that is an exception arrives
    as the Staleness Contract's ``None`` with its reason."""

    def __init__(self):
        self.components: dict[str, FakeComponent] = {}
        self.callbacks: dict[str, object] = {}
        self.subscriptions: list[dict] = []

    async def subscribe_async(self, address, *, callback_method, time_of_data_tolerance=None, max_data_age=None, delay=None, policy=None):
        component, attribute = address.rsplit('.', 1)
        assert attribute in self.components[component].spec.param, f'{address}: no such attribute'
        self.subscriptions.append({'address': address, 'tolerance': time_of_data_tolerance, 'max_age': max_data_age, 'delay': delay, 'policy': policy})
        self.callbacks[address] = callback_method
        self.deliver(address, self.components[component].values[attribute])
        return address

    def unsubscribe(self, cq):
        del self.callbacks[cq]

    def deliver(self, address, value):
        if address not in self.callbacks:
            return
        if isinstance(value, TicError):
            response = ValueResponse(address, value=None, status=False, error=ResponseError(code=value.code, message=str(value), component_name=address.rsplit('.', 1)[0]))
        elif isinstance(value, Exception):
            response = ValueResponse(address, value=Value(None, time.time(), tags={'reason': f'{type(value).__name__}: {value}'}))
        else:
            response = ValueResponse(address, value=Value(value, time.time()))
        self.callbacks[address]([response])


class TicError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class FakeComponent:
    """A component of ``spec`` (its ocaboxapi class): subscribing to an attribute the real class
    does not have fails the test, so the reader cannot drift from the device API (``az`` vs
    ``azimuth``); a GET would bypass the subscriptions and fails it too."""

    def __init__(self, spec, values, name, tic):
        unknown = set(values) - set(spec.param)
        assert not unknown, f'{spec.__name__} has no {sorted(unknown)}'
        self.spec, self.values, self.name, self.observatory = spec, values, name, tic
        tic.components[self.my_address()] = self
        self.request_special_permission = False
        effects = {  # commands of the real class, answered the way the device would end up reading
            'aput_selectnasmythport': self._select,
            'aput_opencover': lambda: self.set(coverstate=3),
            'aput_closecover': lambda: self.set(coverstate=1),
            'aput_calibratoroff': lambda: self.set(calibratorstate=1),
            'aput_calibratoron': lambda brightness: self.set(calibratorstate=3),
            'aput_tracking': lambda track: None,
            'aput_slewtoaltaz_async': lambda az, alt: self.set(azimuth=az, altitude=alt),
            'aput_slewtoazimuth': lambda az: self.set(azimuth=az),
            'aput_abortslew': lambda: None,
        }
        for command, effect in effects.items():
            if hasattr(spec, command):
                setattr(self, command, AsyncMock(side_effect=effect))

    def my_address(self):
        return f'jk15.{self.name}'

    def set(self, **values):
        """The device now reads ``values``: delivered to every subscription of them."""
        self.values.update(values)
        for attribute, value in values.items():
            self.observatory.deliver(f'{self.my_address()}.{attribute}', value)

    async def aget_value(self, attribute, **kwargs):
        raise AssertionError(f'{self.name}.{attribute}: a GET bypasses the subscriptions')

    def _select(self, port):
        self.set(tertiarystatus={'NasmythPort': port, 'Moving': False})


class FakeTelescope:
    def __init__(self, port=1, moving=False, camerastate=0, guider_beso_state=0):
        self.tic = FakeTIC()
        self.c = {name: FakeComponent(spec, values, name, self.tic) for name, spec, values in (
            ('tertiary', Tertiary, {'tertiarystatus': {'NasmythPort': port, 'Moving': moving}}),
            ('covercalibrator', CoverCalibrator, {'coverstate': 3, 'calibratorstate': 1}),
            ('dome', Dome, {'shutterstatus': 0, 'azimuth': 120.0}),
            ('mount', Mount, {'azimuth': 10.0, 'altitude': 60.0}),
            ('camera', Camera, {'camerastate': camerastate}),
            ('guider', Camera, {'camerastate': 0}),
            ('guider_beso', Camera, {'camerastate': guider_beso_state}),
        )}

    def __getattr__(self, name):
        if name.startswith('get_'):
            return lambda component: self.c[component]
        raise AttributeError(name)


async def started(tel):
    """The service's reader of ``tel``, subscribed."""
    reader = TelescopeReader(tel, GRAPH, tolerance=1.0, sun=lambda: Sun(-30.0, rising=False))
    await reader.start()
    return reader


# ---------------------------------------------------------------------------------------- readings

class TestReadings:

    async def test_reads_by_kind(self):
        snap = (await started(FakeTelescope(port=2))).snapshot()
        s = snap.state.selectors
        assert s['tertiary'].position == 'andor' and s['covercalibrator'].position == 'open'
        assert s['covercalibrator.calibrator'].position == 'off'
        env = snap.state.environment
        assert (env.dome_shutter_open, env.dome_az_deg, env.mount_alt_deg, env.sun_alt_deg) == (True, 120.0, 60.0, -30.0)
        assert snap.busy == {} and snap.failed == frozenset()

    async def test_subscribes_under_the_staleness_contract_and_never_gets(self):
        tel = FakeTelescope()
        await started(tel)
        addresses = {s['address'] for s in tel.tic.subscriptions}
        assert {'jk15.tertiary.tertiarystatus', 'jk15.covercalibrator.coverstate', 'jk15.dome.shutterstatus',
                'jk15.mount.altitude', 'jk15.camera.camerastate'} <= addresses
        # T0 is not given: by the contract it defaults to T1 (ocabox-common >= 1.11)
        assert all(s['tolerance'] == 1.0 and s['delay'] is None and s['policy'].value_policy is ValuePolicy.NONE
                   for s in tel.tic.subscriptions)

    async def test_follows_deliveries(self):
        tel = FakeTelescope(port=1)
        reader = await started(tel)
        version = reader.version
        tel.c['tertiary'].set(tertiarystatus={'NasmythPort': 2, 'Moving': False})
        assert await reader.changed(version, 1.0) > version
        assert reader.snapshot().state.selectors['tertiary'].position == 'andor'

    async def test_nothing_delivered_wait_ends_at_the_timeout(self):
        reader = await started(FakeTelescope())
        assert await reader.changed(reader.version, 0.01) == reader.version

    async def test_stopping_unsubscribes_and_wakes_the_waiters(self):
        tel = FakeTelescope()
        reader = await started(tel)
        waiting = asyncio.create_task(reader.changed(reader.version, 5.0))
        await asyncio.sleep(0)
        reader.stop()
        await asyncio.wait_for(waiting, 1.0)
        assert tel.tic.callbacks == {}

    async def test_unmapped_port_and_moving(self):
        s = (await started(FakeTelescope(port=7, moving=True))).snapshot().state.selectors
        assert s['tertiary'].position is None and s['tertiary'].moving

    async def test_out_of_date_is_undefined_and_an_unread_camera_counts_as_busy(self):
        tel = FakeTelescope()
        reader = await started(tel)
        tel.c['tertiary'].set(tertiarystatus=TimeoutError('TIC did not answer'))
        tel.c['camera'].set(camerastate=ConnectionError('down'))
        snap = reader.snapshot()
        assert 'tertiary' not in snap.state.selectors
        assert snap.state.causes['tertiary'] == 'out of date (TimeoutError: TIC did not answer)'
        assert snap.busy == {'camera': 'state unknown'}
        assert snap.failed == {'tertiary', 'camera'}

    async def test_a_tic_error_proves_nothing_and_says_so(self):
        tel = FakeTelescope()
        reader = await started(tel)
        tel.c['covercalibrator'].set(coverstate=TicError(2003, 'device not responding'))
        snap = reader.snapshot()
        assert snap.state.causes['covercalibrator'] == 'out of date (TIC error 2003: device not responding)'
        assert snap.state.selectors['covercalibrator.calibrator'].position == 'off'  # the other aspect still reads

    async def test_before_the_first_delivery_nothing_is_proven(self):
        reader = TelescopeReader(FakeTelescope(), GRAPH, tolerance=1.0)
        snap = reader.snapshot()
        assert snap.state.causes['tertiary'] == 'not read yet'
        assert set(snap.busy.values()) == {'state unknown'}

    async def test_exposing_camera_is_busy(self):
        assert (await started(FakeTelescope(guider_beso_state=2))).snapshot().busy == {'guider_beso': 'exposing'}


# ---------------------------------------------------------------------------------------- actuation

class TestActuation:

    async def test_moves_m3_with_special_permission_and_proves_it(self):
        tel = FakeTelescope(port=1)
        await Actuator(tel, GRAPH, await started(tel), prove_timeout=2).apply({'tertiary': 'andor'})
        tel.c['tertiary'].aput_selectnasmythport.assert_awaited_once_with(2)
        assert tel.c['tertiary'].request_special_permission

    async def test_not_proven_in_time_fails_with_what_it_reads(self):
        tel = FakeTelescope(port=1)
        tel.c['tertiary'].aput_selectnasmythport = AsyncMock()  # TIC accepts, M3 never arrives
        with pytest.raises(ActuationError) as e:
            await Actuator(tel, GRAPH, await started(tel), prove_timeout=0.05).apply({'tertiary': 'andor'})
        assert 'M3 reads beso, wanted andor' in str(e.value)

    async def test_closes_the_mirror_covers_and_proves_it(self):
        tel = FakeTelescope()
        await Actuator(tel, GRAPH, await started(tel), prove_timeout=2).apply({'covercalibrator': 'close'})
        tel.c['covercalibrator'].aput_closecover.assert_awaited_once()
        assert tel.c['covercalibrator'].request_special_permission

    async def test_switches_the_calibration_lamp_off(self):
        tel = FakeTelescope()
        tel.c['covercalibrator'].set(calibratorstate=3)
        await Actuator(tel, GRAPH, await started(tel), prove_timeout=2).apply({'covercalibrator.calibrator': 'off'})
        tel.c['covercalibrator'].aput_calibratoroff.assert_awaited_once()

    async def test_never_lights_the_lamp_at_a_guessed_brightness(self):
        tel = FakeTelescope()
        with pytest.raises(ActuationError, match='no calibrator_brightness configured'):
            await Actuator(tel, GRAPH, await started(tel)).apply({'covercalibrator.calibrator': 'on'})
        tel.c['covercalibrator'].aput_calibratoron.assert_not_awaited()

    async def test_the_dome_goes_to_the_flat_screen_with_the_telescope(self):
        tel = FakeTelescope(port=2)
        await Actuator(tel, GRAPH, await started(tel)).apply({'dome': 'flat'})
        mount, dome = tel.c['mount'], tel.c['dome']
        mount.aput_tracking.assert_awaited_once_with(False)
        mount.aput_slewtoaltaz_async.assert_awaited_once_with(229.0, 15.0)
        dome.aput_slewtoazimuth.assert_awaited_once_with(49.0)
        assert mount.request_special_permission and dome.request_special_permission

    async def test_the_dome_reaches_only_the_flat_screen(self):
        tel = FakeTelescope()
        actuator = Actuator(tel, GRAPH, await started(tel))
        assert actuator.reach == {'dome': frozenset({'flat'})}
        with pytest.raises(ActuationError, match="dome → open: no actuator for kind 'dome'"):
            await actuator.apply({'dome': 'open'})  # the shutter is weather protection: never ours

    async def test_abort_stops_the_telescope_and_the_dome(self):
        tel = FakeTelescope()
        await Actuator(tel, GRAPH, await started(tel)).abort({'dome': 'flat'})
        tel.c['mount'].aput_abortslew.assert_awaited_once()
        tel.c['dome'].aput_abortslew.assert_awaited_once()


# ---------------------------------------------------------------------------------------- service

async def service(tel):
    svc = OpticsService()
    svc.svc_config = SimpleNamespace(telescope='jk15', actuate=['tertiary'], interval=0.01, confirm_timeout=120.0, declaration_hours=12.0)
    svc.svc_logger = MagicMock()
    svc.graph = GRAPH
    svc._reader = await started(tel)
    svc._actuator = Actuator(tel, GRAPH, svc._reader, prove_timeout=2)
    svc.endpoint = MagicMock(publish=AsyncMock())
    return svc


def operations(svc):
    return [c.args[2] for c in svc.endpoint.publish.await_args_list if c.args[:2] == ('self', 'operation')]


class TestSetpath:

    async def test_accepts_switches_and_reports_the_operation(self):
        tel = FakeTelescope(port=1)
        svc = await service(tel)
        answer = await svc._setpath(Call('camera', 'setpath', {'function': 'object'}, None, 5))
        assert answer == {'accepted': True, 'moves': {'tertiary': 'andor'}}
        await svc._operation
        assert [o['state'] for o in operations(svc)] == ['moving', 'done']
        assert tel.c['tertiary'].values['tertiarystatus']['NasmythPort'] == 2

    async def test_already_there_moves_nothing(self):
        svc = await service(FakeTelescope(port=2))
        assert await svc._setpath(Call('camera', 'setpath', {'function': 'object'}, None, 5)) == {'accepted': True, 'moves': {}}
        assert svc._operation is None

    async def test_one_switch_at_a_time(self):
        tel = FakeTelescope(port=1)
        svc = await service(tel)
        svc._operation = asyncio.get_running_loop().create_future()
        with pytest.raises(Refusal) as e:
            await svc._setpath(Call('camera', 'setpath', {'function': 'object'}, None, 5))
        assert e.value.code == 4008
        svc._operation.cancel()

    async def test_unknown_function_is_4007_even_during_a_switch(self):
        svc = await service(FakeTelescope(port=1))
        svc._operation = asyncio.get_running_loop().create_future()
        with pytest.raises(Refusal) as e:
            await svc._setpath(Call('camera', 'setpath', {'function': 'nonexistent'}, None, 5))
        assert e.value.code == 4007 and "declares no function 'nonexistent'" in e.value.message
        svc._operation.cancel()

    async def test_concurrent_requests_start_one_switch(self):
        tel = FakeTelescope(port=1)
        svc = await service(tel)
        results = await asyncio.gather(*(svc._setpath(Call('camera', 'setpath', {'function': 'object'}, None, 5))
                                         for _ in range(3)), return_exceptions=True)
        accepted = [r for r in results if isinstance(r, dict)]
        refused = [r for r in results if isinstance(r, Refusal)]
        assert len(accepted) == 1 and len(refused) == 2 and all(r.code == 4008 for r in refused)
        await svc._operation
        assert tel.c['tertiary'].aput_selectnasmythport.await_count == 1

    async def test_function_parameter_required(self):
        with pytest.raises(Refusal) as e:
            await (await service(FakeTelescope()))._setpath(Call('camera', 'setpath', {}, None, 5))
        assert e.value.code == 4007

    async def test_failed_switch_is_reported(self):
        tel = FakeTelescope(port=1)
        tel.c['tertiary'].aput_selectnasmythport = AsyncMock(side_effect=RuntimeError('1005 safety cutoff'))
        svc = await service(tel)
        await svc._setpath(Call('camera', 'setpath', {'function': 'object'}, None, 5))
        await svc._operation
        failed = operations(svc)[-1]
        assert failed['state'] == 'failed' and 'safety cutoff' in failed['message']


class TestFollowing:

    async def test_a_delivery_is_published_without_polling(self, monkeypatch):
        monkeypatch.setattr(OpticsService, 'is_running', property(lambda self: True))
        tel = FakeTelescope(port=1)
        svc = await service(tel)
        svc.svc_config.recompute, svc.svc_config.interval = 30.0, 0.0
        running = asyncio.create_task(svc.run_service())
        try:
            await asyncio.sleep(0.01)
            assert published(svc, 'camera', 'sees')[0]['terminal'] == 'tertiary'  # M3 at beso: the camera sees its back
            tel.c['tertiary'].set(tertiarystatus={'NasmythPort': 2, 'Moving': False})
            await asyncio.sleep(0.01)
            assert published(svc, 'camera', 'sees')[0]['terminal'] == 'sky'
        finally:
            running.cancel()


# ---------------------------------------------------------------------------------------- confirmation

DOME = frozenset({'tertiary', 'covercalibrator', 'dome'})
REACH = {'dome': frozenset({'flat'})}


def by_day(**positions):
    base = {'covercalibrator': 'close', 'covercalibrator.calibrator': 'off', 'tertiary': 'andor', 'flatscreen': 'on'}
    return ProvenState.build({**base, **positions}, sun_alt_deg=20.0, sun_rising=False, dome_shutter_open=False,
                             dome_az_deg=120.0, mount_az_deg=10.0, mount_alt_deg=60.0)


class TestConfirmPolicy:

    def test_the_floor(self):
        policy = ConfirmPolicy()
        assert policy.waiting(GRAPH, night(), {'dome': 'flat', 'covercalibrator': 'close', 'tertiary': 'andor'}) == ('dome',)
        assert policy.waiting(GRAPH, by_day(), {'covercalibrator': 'open'}) == ('covercalibrator',)  # by day, both ways
        assert policy.waiting(GRAPH, by_day(), {'covercalibrator': 'close'}) == ('covercalibrator',)
        assert policy.waiting(GRAPH, by_day(), {'covercalibrator.calibrator': 'off'}) == ()  # no moving part

    def test_an_unknown_sky_counts_as_day(self):
        unknown = ProvenState.build({'covercalibrator': 'open'}, dome_shutter_open=True)
        assert ConfirmPolicy().waiting(GRAPH, unknown, {'covercalibrator': 'close'}) == ('covercalibrator',)

    def test_configuration_adds_never_removes(self):
        policy = ConfirmPolicy.from_config(GRAPH, {'tertiary': 'always', 'dome': 'day'})
        assert policy.waiting(GRAPH, night(), {'tertiary': 'andor', 'dome': 'flat'}) == ('tertiary', 'dome')

    def test_configuration_errors_stop_the_start(self):
        with pytest.raises(ValueError, match="'m9' is no state key"):
            ConfirmPolicy.from_config(GRAPH, {'m9': 'always'})
        with pytest.raises(ValueError, match="'sometimes' is not one of always, day"):
            ConfirmPolicy.from_config(GRAPH, {'tertiary': 'sometimes'})


class TestDecideWithPeople:

    def test_domeflat_by_day_waits_for_the_telescope_the_dome_and_the_covers(self):
        decision = decide(GRAPH, by_day(), 'camera', 'domeflat', DOME, ConfirmPolicy(), REACH)
        assert decision.moves == {'dome': 'flat', 'covercalibrator': 'open'}
        assert set(decision.waiting) == {'dome', 'covercalibrator'}

    def test_the_verdict_says_it_in_advance(self):
        functions = report(GRAPH, by_day(), DOME, ConfirmPolicy(), REACH)['camera']['functions']
        assert functions['domeflat']['kind'] == 'settable' and set(functions['domeflat']['confirm']) == {'dome', 'covercalibrator'}

    def test_the_shutter_stays_the_operators(self):
        # object needs the dome open: out of the actuator's reach, so decided again with the dome held
        with pytest.raises(Refusal) as e:
            decide(GRAPH, by_day(), 'camera', 'object', DOME, ConfirmPolicy(), REACH)
        assert e.value.code == 4009

    def test_dusk_prepares_for_the_night(self):
        dusk = night(covercalibrator='close').with_environment(sun_alt_deg=-12.0, sun_rising=False)
        decision = decide(GRAPH, dusk, 'camera', 'object', DOME, ConfirmPolicy(), REACH)
        assert (decision.moves, decision.waiting, decision.awaits) == ({'covercalibrator': 'open', 'tertiary': 'andor'}, (), 'sky.science')


#: what serverish's journal schema accepts as ``op`` — the fake publisher above takes anything
JOURNAL_OPS = set(json.loads(files('serverish').joinpath('schema/journal.schema.json').read_text())['properties']['op']['enum'])


async def confirmable(tel, timeout=120.0):
    """A service that moves M3, the covers and the dome (to the flat screen), the flat screen lit by
    a person's declaration."""
    svc = await service(tel)
    svc.svc_config.actuate, svc.svc_config.confirm_timeout = sorted(DOME), timeout
    svc._journal = MagicMock(warning=AsyncMock(side_effect=lambda message, **kw: SimpleNamespace(message=message, actions=kw['actions'])),
                             publish_journal_operation=AsyncMock())
    svc._declared = {'flatscreen': {'position': 'on', 'by': 'observer', 'at': time.time()}}
    return svc


def domeflat(who='observer'):
    return Call('camera', 'setpath', {'function': 'domeflat'}, SimpleNamespace(name=who), 5)


def answer(svc, method, operation, who='observer', **parameters):
    return getattr(svc, f'_{method}')(Call('self', method, {'operation': operation, **parameters}, SimpleNamespace(name=who), 5))


class TestConfirmation:

    async def test_a_move_that_needs_a_person_waits_and_is_announced(self):
        tel = FakeTelescope(port=2)
        svc = await confirmable(tel)
        reply = await svc._setpath(domeflat())
        assert reply['awaiting_confirmation'] and reply['moves'] == {'dome': 'flat'} and reply['confirm'] == ['dome']
        assert reply['says'] == ['the telescope at the flat screen: dome at 49°, telescope at az 229° alt 15°']
        assert operations(svc)[-1]['state'] == 'awaiting_confirmation'
        kwargs = svc._journal.warning.await_args.kwargs
        assert kwargs['actions'] == ['confirm', 'reject']
        assert kwargs['meta']['actions_rpc']['confirm'] == {'address': 'jk15.optics.self.confirm', 'parameters': {'operation': reply['operation']}}
        tel.c['mount'].aput_slewtoaltaz_async.assert_not_awaited()
        svc._clear(svc._pending)

    async def test_asking_again_returns_the_waiting_change_and_others_wait(self):
        svc = await confirmable(FakeTelescope(port=2))
        first = await svc._setpath(domeflat())
        assert (await svc._setpath(domeflat('someone else')))['operation'] == first['operation']
        with pytest.raises(Refusal) as e:
            await svc._setpath(Call('camera', 'setpath', {'function': 'dark_strict'}, None, 5))
        assert e.value.code == 4008
        assert svc._journal.warning.await_count == 1
        svc._clear(svc._pending)

    async def test_confirmed_it_moves_and_closes_the_journal_entry(self):
        tel = FakeTelescope(port=2)
        svc = await confirmable(tel)
        operation = (await svc._setpath(domeflat()))['operation']
        assert await answer(svc, 'confirm', operation, who='mikolaj') == {'confirmed': True, 'operation': operation, 'moves': {'dome': 'flat'}}
        await svc._operation
        states = [o['state'] for o in operations(svc)]
        assert states[-2:] == ['moving', 'done'] and operations(svc)[-1]['confirmed_by'] == 'mikolaj'
        tel.c['mount'].aput_slewtoaltaz_async.assert_awaited_once_with(229.0, 15.0)
        ops = [c.kwargs['op'] for c in svc._journal.publish_journal_operation.await_args_list]
        assert ops == ['change'] and set(ops) <= JOURNAL_OPS
        assert svc._pending is None

    async def test_rejected_nothing_moves(self):
        tel = FakeTelescope(port=2)
        svc = await confirmable(tel)
        operation = (await svc._setpath(domeflat()))['operation']
        await answer(svc, 'reject', operation, reason='people in the dome')
        last = operations(svc)[-1]
        assert (last['state'], last['message'], last['decided_by']) == ('rejected', 'rejected by observer: people in the dome', 'observer')
        tel.c['dome'].aput_slewtoazimuth.assert_not_awaited()
        assert svc._pending is None and not svc._in_progress()

    async def test_nobody_confirms_it_expires(self):
        svc = await confirmable(FakeTelescope(port=2), timeout=0.05)
        await svc._setpath(domeflat())
        await asyncio.sleep(0.1)
        assert operations(svc)[-1]['state'] == 'expired' and svc._pending is None

    async def test_a_changed_state_supersedes_it(self):
        tel = FakeTelescope(port=2)
        svc = await confirmable(tel)
        operation = (await svc._setpath(domeflat()))['operation']
        tel.c['tertiary'].set(tertiarystatus={'NasmythPort': 1, 'Moving': False})  # someone turned M3 meanwhile
        with pytest.raises(Refusal) as e:
            await answer(svc, 'confirm', operation)
        assert e.value.code == 4008 and 'ask again' in e.value.message
        assert operations(svc)[-1]['state'] == 'superseded'
        tel.c['mount'].aput_slewtoaltaz_async.assert_not_awaited()

    async def test_only_the_waiting_change_can_be_answered(self):
        svc = await confirmable(FakeTelescope(port=2))
        with pytest.raises(Refusal) as e:
            await answer(svc, 'confirm', 'nosuch')
        assert e.value.code == 4007 and 'no light-path change waits' in e.value.message
        operation = (await svc._setpath(domeflat()))['operation']
        with pytest.raises(Refusal) as e:
            await answer(svc, 'confirm', 'nosuch')
        assert e.value.code == 4007 and operation in e.value.message
        svc._clear(svc._pending)

    async def test_abort_stops_a_running_change(self):
        tel = FakeTelescope(port=2)
        tel.c['mount'].aput_slewtoaltaz_async = AsyncMock()  # the telescope never gets there
        svc = await confirmable(tel)
        operation = (await svc._setpath(domeflat()))['operation']
        await answer(svc, 'confirm', operation)
        await asyncio.sleep(0.01)
        assert await answer(svc, 'abort', operation, who='night assistant') == {'aborted': True, 'operation': operation}
        tel.c['mount'].aput_abortslew.assert_awaited_once()
        tel.c['dome'].aput_abortslew.assert_awaited_once()
        last = operations(svc)[-1]
        assert (last['state'], last['message']) == ('failed', 'aborted by night assistant')

    async def test_a_move_without_people_starts_at_once(self):
        svc = await confirmable(FakeTelescope(port=1))
        reply = await svc._setpath(Call('camera', 'setpath', {'function': 'object'}, None, 5))
        assert reply == {'accepted': True, 'moves': {'tertiary': 'andor'}}
        await svc._operation
        svc._journal.warning.assert_not_awaited()


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
        monkeypatch.setattr(module, 'get_journalpublisher', lambda subject: MagicMock(subject=subject))
        monkeypatch.setattr(module.TicEndpoint, 'for_service',
                            classmethod(lambda cls, svc, **kw: MagicMock(publish=AsyncMock(), start=AsyncMock())))
        svc = OpticsService()
        svc.svc_config = SimpleNamespace(telescope='jk15', variant='jk15', client_name='CliClient', components_file='',
                                         config_stream='tic.config.observatory', actuate=['tertiary'], interval=1.0, max_age=None,
                                         prove_timeout=60.0, keepalive=60.0, confirm={}, confirm_timeout=120.0)
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
        tel.c['dome'].set(shutterstatus=4)
        snap = (await started(tel)).snapshot()
        assert snap.state.causes['dome'] == 'shutter reports Error (ShutterState 4)'

    async def test_a_faulted_m3_says_so(self):
        tel = FakeTelescope(port=2)
        tel.c['tertiary'].set(tertiarystatus={'NasmythPort': 2, 'Moving': False, 'ErrorRaised': True})
        snap = (await started(tel)).snapshot()
        assert snap.state.selectors['tertiary'].position is None
        assert snap.state.causes['tertiary'] == 'controller reports an error (ErrorRaised)'

    async def test_out_of_date_explains_every_axis_it_left_unproven(self):
        tel = FakeTelescope(port=2)
        tel.c['covercalibrator'].set(coverstate=TimeoutError('TIC did not answer'), calibratorstate=TimeoutError('TIC did not answer'))
        snap = (await started(tel)).snapshot()
        assert snap.state.causes['covercalibrator'] == 'out of date (TimeoutError: TIC did not answer)'
        assert snap.state.causes['covercalibrator.calibrator'] == 'out of date (TimeoutError: TIC did not answer)'


# ---------------------------------------------------------------------------------------- declarations

def caller(name='mka'):
    return SimpleNamespace(name=name)


class TestDeclarations:

    async def broken_shutter_service(self):
        tel = FakeTelescope(port=2)
        tel.c['dome'].set(shutterstatus=4)
        svc = await service(tel)
        svc.svc_config.declaration_hours = 12.0
        return svc, tel

    async def test_the_operator_vouches_for_the_dome_and_object_becomes_active(self):
        svc, _ = await self.broken_shutter_service()
        await svc._publish(svc._reader.snapshot())
        assert published(svc, 'camera', 'functions')['object']['kind'] == 'impossible'
        answer = await svc._declare(Call('self', 'declare', {'key': 'dome', 'position': 'open'}, caller(), 5))
        assert answer['declared']['dome']['position'] == 'open' and answer['declared']['dome']['by'] == 'mka'
        assert published(svc, 'camera', 'functions')['object']['kind'] == 'active'
        dome = published(svc, 'self', 'selectors')['dome']
        assert (dome['position'], dome['origin'], dome['by']) == ('open', 'declared', 'mka')
        assert dome['cause'] == 'shutter reports Error (ShutterState 4)' and dome['label'] == 'dome'

    async def test_published_axes_draw_the_same_path_as_the_service_sees(self):
        svc, _ = await self.broken_shutter_service()
        snapshot = svc._reader.snapshot()
        await svc._publish(snapshot)
        axes = published(svc, 'self', 'selectors')
        assert axes['sky']['origin'] == 'derived'  # sources too: a client draws where the light starts
        positions = {key: entry['position'] for key, entry in axes.items()}
        assert ribbon(svc.graph, published_values(positions), 'camera') == ribbon(svc.graph, snapshot.state, 'camera')

    async def test_a_reading_withdraws_the_declaration(self):
        svc, tel = await self.broken_shutter_service()
        await svc._declare(Call('self', 'declare', {'key': 'dome', 'position': 'open'}, caller(), 5))
        tel.c['dome'].set(shutterstatus=0)  # the sensor works again
        await svc._publish(svc._reader.snapshot())
        assert published(svc, 'self', 'declared') == {}
        assert published(svc, 'self', 'selectors')['dome']['origin'] == 'derived'

    async def test_a_declaration_expires(self):
        svc, _ = await self.broken_shutter_service()
        await svc._declare(Call('self', 'declare', {'key': 'dome', 'position': 'open'}, caller(), 5))
        svc._declared['dome']['at'] -= 13 * 3600
        await svc._publish(svc._reader.snapshot())
        assert published(svc, 'self', 'declared') == {}

    async def test_withdrawn_on_request(self):
        svc, _ = await self.broken_shutter_service()
        await svc._declare(Call('self', 'declare', {'key': 'dome', 'position': 'open'}, caller(), 5))
        await svc._declare(Call('self', 'declare', {'key': 'dome', 'position': None}, caller(), 5))
        assert published(svc, 'self', 'declared') == {}

    async def test_what_a_reading_proves_needs_no_declaration(self):
        svc = await service(FakeTelescope(port=2))
        with pytest.raises(Refusal, match='M3 reads andor: a reading needs no declaration'):
            await svc._declare(Call('self', 'declare', {'key': 'tertiary', 'position': 'beso'}, caller(), 5))

    async def test_a_lamp_switched_on_by_hand(self):
        svc, _ = await self.broken_shutter_service()
        svc.graph = parse_graph({**JK15_WITH_MOUNT, 'thar_lamp': {'kind': 'lamp', 'light': 'lamp.thar'}}, telescope='jk15')
        await svc._declare(Call('self', 'declare', {'key': 'thar_lamp', 'position': 'on'}, caller(), 5))
        lamp = published(svc, 'self', 'selectors')['thar_lamp']
        assert (lamp['position'], lamp['origin'], lamp['by']) == ('on', 'declared', 'mka')

    async def test_a_derived_source_needs_no_declaration(self):
        svc, _ = await self.broken_shutter_service()
        with pytest.raises(Refusal, match='a reading needs no declaration'):
            await svc._declare(Call('self', 'declare', {'key': 'sky', 'position': 'day'}, caller(), 5))

    async def test_unknown_key_or_position(self):
        svc, _ = await self.broken_shutter_service()
        for parameters in ({'key': 'sky', 'position': 'open'}, {'key': 'dome', 'position': 'ajar'}):
            with pytest.raises(Refusal) as e:
                await svc._declare(Call('self', 'declare', parameters, caller(), 5))
            assert e.value.code == 4007


def published(svc, element, item):
    """The last value the service published for ``element.item``."""
    return [c.args[2] for c in svc.endpoint.publish.await_args_list if c.args[:2] == (element, item)][-1]
