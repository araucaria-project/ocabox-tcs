"""The optics service: what each instrument sees, and switching its light path on request.

One instance per telescope (variant = telescope id). Clients reach it through TIC
(``<tel>.optics.<detector>.<item>``, ocabox-server ``TreeNatsService``):

======================================  ===================================================
``status.optics.<tel>.<det>.sees``      what the detector sees now (``SeesRecord`` list)
``status.optics.<tel>.<det>.functions`` verdict per declared function (active / settable /
                                        collision / impossible)
``status.optics.<tel>.self.selectors``  every selector axis: position (``null`` = undefined),
                                        origin, moving, why undefined, who declared it, label
``status.optics.<tel>.self.declared``   positions people vouch for: ``{key: {position, by, at}}``
``status.optics.<tel>.self.busy``       detectors whose path must not change, and why
``status.optics.<tel>.self.operation``  the last path change: moving → done | failed
``rpc.optics.<tel>.<det>.setpath``      ``{function}``: switch so the detector can do it
``rpc.optics.<tel>.self.declare``       ``{key, position}``: vouch for an axis no reading proves
                                        (``position: null`` withdraws)
======================================  ===================================================

``setpath`` answers at once — accepted with the moves, or refused with an obcom code (4007 no such
detector/function, 4008 a busy detector's path or another switch in progress, 4009 a manual move
needed or the light unavailable) — and the switch runs in the background, reported in
``self.operation``, like a slew. Graph: the telescope's ``components`` from the published
observatory configuration, or ``components_file`` (a config.yaml) to try a graph before deploying.

The telescope is followed by TIC subscriptions (:mod:`.readings`): the service declares how fresh
it wants every value (``interval``, T1) and when it stops being the truth (``max_age``, T2), TIC
polls each device once for all its subscribers; the status is recomputed when a value is delivered,
and every ``recompute`` seconds for what changes without a delivery (the sky with the sun,
declarations expiring).

A declaration is the operator's word for what no reading can prove — the shutter opened by hand
while its sensor reports an error. It fills only an undefined axis, is withdrawn as soon as a
reading proves the axis again, expires after ``declaration_hours``, and is visible everywhere as
``declared`` by whom. Only people declare: the request passes TIC's blocker like ``setpath``.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, replace
from importlib.metadata import version
from pathlib import Path

import yaml
from datamodels.messages import Severity
from datamodels.optics import state_key
from obcom.optics import Declaration, GraphInvalid, OpticalGraph, ProvenState, label, parse_graph, resolve_axes

from ocabox_tcs.base_service import BaseBlockingPermanentService, BaseServiceConfig, config, service
from ocabox_tcs.services.optics.actuation import ActuationError, Actuator
from ocabox_tcs.services.optics.engine import decide, report, require_function, with_busy_holds
from ocabox_tcs.services.optics.readings import Snapshot, TelescopeReader
from ocabox_tcs.services.optics.sun import SunAltitude
from ocabox_tcs.tic_endpoint import Call, Refusal, TicEndpoint

#: ocabox-common with the dome flat at the screen whatever the shutter and ``Kind.meaning`` (1.12.0)
REQUIRED_OCABOX_COMMON = (1, 12, 0)


@config('optics')
@dataclass
class OpticsServiceConfig(BaseServiceConfig):
    telescope: str = ''  #: telescope id; default: the variant
    interval: float = 1.0  #: T1 of every subscription: TIC refreshes the devices this often for us, s
    max_age: float | None = None  #: T2: a value older than this proves nothing, s (default 2·T1, at least 5)
    recompute: float = 10.0  #: s between recomputations without a delivery (the sky, declarations expiring)
    actuate: list[str] = field(default_factory=lambda: ['tertiary'])  #: state keys the service may move
    prove_timeout: float = 60.0  #: seconds a commanded move has to be proven
    components_file: str = ''  #: config.yaml to read the graph from instead of the published config
    keepalive: float = 60.0  #: seconds between repeats of published status
    client_name: str = 'CliClient'  #: ocabox client section of the observatory configuration
    config_stream: str = 'tic.config.observatory'  #: NATS stream TIC publishes the observatory configuration on
    declaration_hours: float = 12.0  #: a declaration nobody withdrew expires after this (one night)


@service('optics')
class OpticsService(BaseBlockingPermanentService):

    def __init__(self):
        super().__init__()
        self.graph: OpticalGraph | None = None
        self.endpoint: TicEndpoint | None = None
        self._reader: TelescopeReader | None = None
        self._actuator: Actuator | None = None
        self._operation: asyncio.Task | None = None
        self._declared: dict[str, dict] = {}  #: key -> {position, by, at}

    @property
    def telescope_id(self) -> str:
        return self.svc_config.telescope or self.svc_config.variant

    @property
    def _actuatable(self) -> frozenset[str]:
        return frozenset(self.svc_config.actuate)

    async def on_start(self):
        from ocaboxapi import Observatory

        installed = version('ocabox-common')
        if tuple(int(part) for part in installed.split('.')[:3]) < REQUIRED_OCABOX_COMMON:
            raise RuntimeError(f'ocabox-common {installed} is too old for the optics service '
                               f'(needs >= {".".join(map(str, REQUIRED_OCABOX_COMMON))}): poetry update')

        tel = self.telescope_id
        observatory = Observatory(client_name=self.svc_config.client_name, software_id=f'optics/{tel}',
                                  config_stream=self.svc_config.config_stream, policy='service')
        await observatory.load_client_cfg()
        telescopes = observatory.get_telescopes_configuration()
        if tel not in telescopes:
            raise RuntimeError(f'{tel}: not in the observatory configuration from {self.svc_config.config_stream!r} '
                               f'(telescopes: {", ".join(telescopes) or "none — is the stream published?"})')
        observatory.connect()
        published = telescopes[tel].get('observatory', {})
        self.graph = self._load_graph(tel, published)
        telescope = observatory.get_telescope(tel)

        self._reader = TelescopeReader(telescope, self.graph, tolerance=self.svc_config.interval,
                                       max_age=self.svc_config.max_age, sun_altitude=SunAltitude.from_config(published),
                                       logger=self.svc_logger)
        self._actuator = Actuator(telescope, self.graph, self._reader, prove_timeout=self.svc_config.prove_timeout,
                                  logger=self.svc_logger)
        await self._reader.start()
        self.endpoint = TicEndpoint.for_service(self, keepalive=self.svc_config.keepalive)
        self.endpoint.method('setpath', self._setpath)
        self.endpoint.method('declare', self._declare, element='self')
        await self.endpoint.publish('self', 'operation', {'state': 'idle'})
        await self.endpoint.publish('self', 'declared', {})
        await self.endpoint.start()
        self.svc_logger.info(f'{tel}: {len(self.graph.detectors)} detectors ({", ".join(self.graph.detectors)}), '
                             f'automatic actuation of {", ".join(self.svc_config.actuate) or "nothing"}')

    def _load_graph(self, tel: str, published: dict) -> OpticalGraph:
        if self.svc_config.components_file:
            source = self.svc_config.components_file
            components = yaml.safe_load(Path(source).read_text())['tree'][tel]['observatory']['components']
        else:
            source, components = 'the published observatory configuration', published.get('components', {})
        try:
            graph = parse_graph(components, telescope=tel)
        except GraphInvalid as e:
            for error in e.errors:
                self.svc_logger.error(f'{tel} optics [{error.code}] {error.message}')
            raise RuntimeError(f'{tel}: the optical graph in {source} is invalid ({len(e.errors)} problems)') from e
        if not graph.detectors:
            raise RuntimeError(f'{tel}: no detector with `paths:` in {source} — nothing to serve')
        return graph

    async def run_service(self):
        version = self._reader.version
        while self.is_running:
            computed = time.monotonic()
            try:
                await self._publish(self._reader.snapshot())
            except Exception:
                self.svc_logger.exception('optics refresh failed')
            version = await self._reader.changed(version, self.svc_config.recompute)
            # at most once per T1: the mount's and M3's encoders deliver noise several times a second
            await asyncio.sleep(max(0.0, self.svc_config.interval - (time.monotonic() - computed)))

    async def on_stop(self):
        if self._operation is not None and not self._operation.done():
            self._operation.cancel()
        if self._reader is not None:
            self._reader.stop()  # also wakes run_service
        if self.endpoint is not None:
            await self.endpoint.stop()

    # ------------------------------------------------------------------ status

    async def _publish(self, snapshot: Snapshot) -> None:
        proven = await self._with_declarations(snapshot.state)
        state = with_busy_holds(self.graph, proven, snapshot.busy)
        for detector, entry in report(self.graph, state, self._actuatable).items():
            await self.endpoint.publish(detector, 'sees', entry['sees'])
            await self.endpoint.publish(detector, 'functions', entry['functions'])
        await self.endpoint.publish('self', 'selectors', self._selectors(proven))
        await self.endpoint.publish('self', 'busy', dict(sorted(snapshot.busy.items())))

    def _selectors(self, state: ProvenState) -> dict[str, dict]:
        """Every state axis — selectors and sources (the sky, lamps); read, derived or declared — with
        why it is undefined and its label: all a client needs to draw the path (``obcom.optics.ribbon``)."""
        out = {}
        for node in self.graph.nodes.values():
            for aspect, resolved in resolve_axes(self.graph, state, node).items():
                key = state_key(node.name, aspect)
                reading = state.selectors.get(key)
                out[key] = {'position': resolved.value, 'origin': resolved.origin,
                            'moving': bool(reading and reading.moving), 'cause': resolved.cause,
                            'by': resolved.by, 'label': label(self.graph, key)}
        return dict(sorted(out.items()))

    # ------------------------------------------------------------------ declarations

    async def _with_declarations(self, state: ProvenState) -> ProvenState:
        """``state`` with the live declarations; withdraws those a reading proves again and those
        past ``declaration_hours``."""
        if not self._declared:
            return state
        now, changed = time.time(), False
        for key, entry in list(self._declared.items()):
            component, _, aspect = key.partition('.')
            read = resolve_axes(self.graph, state, self.graph.node(component)).get(aspect or None)
            if read is not None and read.value is not None:
                self.svc_logger.info(f'{label(self.graph, key)} reads {read.value} again: '
                                     f'declaration {entry["position"]} by {entry["by"]} withdrawn')
            elif now - entry['at'] > self.svc_config.declaration_hours * 3600:
                self.svc_logger.info(f'{label(self.graph, key)}: declaration {entry["position"]} by {entry["by"]} expired')
            else:
                continue
            del self._declared[key]
            changed = True
        if changed:
            await self.endpoint.publish('self', 'declared', self._declared)
        return replace(state, declared={k: Declaration(e['position'], e['by']) for k, e in self._declared.items()})

    async def _declare(self, call: Call):
        key, position = call.parameters.get('key'), call.parameters.get('position')
        axes = self.graph.state_axes  # every published axis: selectors, and sources (a lamp switched on by hand)
        if key not in axes:
            raise Refusal(f'declare needs a state key (one of: {", ".join(sorted(axes))})', code=4007)
        who = call.caller.name if call.caller else 'unknown'
        if position is None:
            withdrawn = self._declared.pop(key, None)
            self.svc_logger.info(f'{who}: declaration of {label(self.graph, key)} withdrawn ({withdrawn and withdrawn["position"]})')
        else:
            if position not in axes[key]:
                raise Refusal(f'{label(self.graph, key)} has no position {position!r} (positions: {", ".join(axes[key])})', code=4007)
            read = resolve_axes(self.graph, self._reader.snapshot().state, self.graph.node(key.partition('.')[0]))
            proven = read.get(key.partition('.')[2] or None)
            if proven is not None and proven.value is not None:
                raise Refusal(f'{label(self.graph, key)} reads {proven.value}: a reading needs no declaration')
            self._declared[key] = {'position': position, 'by': who, 'at': time.time()}
            self.svc_logger.info(f'{who}: declares {label(self.graph, key)} {position}')
        await self.endpoint.publish('self', 'declared', self._declared)
        await self._publish(self._reader.snapshot())
        return {'declared': dict(self._declared)}

    # ------------------------------------------------------------------ setpath

    async def _setpath(self, call: Call):
        function = call.parameters.get('function')
        if not isinstance(function, str):
            raise Refusal("setpath needs the parameter 'function' (e.g. object, dark, flat)", code=4007)
        require_function(self.graph, call.element, function)
        snapshot = self._reader.snapshot()
        proven = await self._with_declarations(snapshot.state)
        # checked after the last await: two concurrent requests can never both start a switch
        if self._operation is not None and not self._operation.done():
            raise Refusal('another light-path change is in progress', code=4008, severity=Severity.TEMPORARY)
        state = with_busy_holds(self.graph, proven, snapshot.busy)
        moves = decide(self.graph, state, call.element, function, self._actuatable)
        if moves:
            who = call.caller.name if call.caller else None
            self._operation = asyncio.create_task(self._operate(call.element, function, moves, who),
                                                  name=f'optics.{call.element}.{function}')
        return {'accepted': True, 'moves': moves}

    async def _operate(self, detector: str, function: str, moves: dict[str, str], caller: str | None) -> None:
        operation = {'detector': detector, 'function': function, 'moves': moves, 'caller': caller}
        await self.endpoint.publish('self', 'operation', {**operation, 'state': 'moving'})
        try:
            await self._actuator.apply(moves)
        except ActuationError as e:
            self.svc_logger.error(f'{detector}.{function}: {e}')
            await self.endpoint.publish('self', 'operation', {**operation, 'state': 'failed', 'message': str(e)})
        else:
            self.svc_logger.info(f'{detector}.{function}: {moves} proven')
            await self.endpoint.publish('self', 'operation', {**operation, 'state': 'done'})
        await self._publish(self._reader.snapshot())


if __name__ == '__main__':
    OpticsService.main()
