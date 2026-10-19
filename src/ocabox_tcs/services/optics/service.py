"""The optics service: what each instrument sees, and switching its light path on request.

One instance per telescope (variant = telescope id). Clients reach it through TIC
(``<tel>.optics.<detector>.<item>``, ocabox-server ``TreeNatsService``):

======================================  ===================================================
``status.optics.<tel>.<det>.sees``      what the detector sees now (``SeesRecord`` list)
``status.optics.<tel>.<det>.functions`` verdict per declared function (active / settable /
                                        collision / impossible)
``status.optics.<tel>.self.selectors``  proven selector positions (``null`` = undefined)
``status.optics.<tel>.self.busy``       detectors whose path must not change, and why
``status.optics.<tel>.self.operation``  the last path change: moving → done | failed
``rpc.optics.<tel>.<det>.setpath``      ``{function}``: switch so the detector can do it
======================================  ===================================================

``setpath`` answers at once — accepted with the moves, or refused with an obcom code (4007 no such
detector/function, 4008 a busy detector's path or another switch in progress, 4009 a manual move
needed or the light unavailable) — and the switch runs in the background, reported in
``self.operation``, like a slew. Graph: the telescope's ``components`` from the published
observatory configuration, or ``components_file`` (a config.yaml) to try a graph before deploying.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from datamodels.messages import Severity
from obcom.optics import GraphInvalid, OpticalGraph, parse_graph

from ocabox_tcs.base_service import BaseBlockingPermanentService, BaseServiceConfig, config, service
from ocabox_tcs.services.optics.actuation import ActuationError, Actuator
from ocabox_tcs.services.optics.engine import decide, report, with_busy_holds
from ocabox_tcs.services.optics.readings import Snapshot, TelescopeReader
from ocabox_tcs.services.optics.sun import SunAltitude
from ocabox_tcs.tic_endpoint import Call, Refusal, TicEndpoint


@config('optics')
@dataclass
class OpticsServiceConfig(BaseServiceConfig):
    telescope: str = ''  #: telescope id; default: the variant
    interval: float = 1.0  #: seconds between readings (also the TIC data tolerance)
    actuate: list[str] = field(default_factory=lambda: ['tertiary'])  #: state keys the service may move
    prove_timeout: float = 60.0  #: seconds a commanded move has to be proven
    components_file: str = ''  #: config.yaml to read the graph from instead of the published config
    keepalive: float = 60.0  #: seconds between repeats of published status
    client_name: str = 'CliClient'  #: ocabox client section of the observatory configuration


@service('optics')
class OpticsService(BaseBlockingPermanentService):

    def __init__(self):
        super().__init__()
        self.graph: OpticalGraph | None = None
        self.endpoint: TicEndpoint | None = None
        self._reader: TelescopeReader | None = None
        self._actuator: Actuator | None = None
        self._operation: asyncio.Task | None = None

    @property
    def telescope_id(self) -> str:
        return self.svc_config.telescope or self.svc_config.variant

    @property
    def _actuatable(self) -> frozenset[str]:
        return frozenset(self.svc_config.actuate)

    async def on_start(self):
        from ocaboxapi import Observatory

        tel = self.telescope_id
        observatory = Observatory(client_name=self.svc_config.client_name, software_id=f'optics/{tel}')
        await observatory.load_client_cfg()
        observatory.connect()
        published = observatory.get_telescopes_configuration().get(tel, {}).get('observatory', {})
        self.graph = self._load_graph(tel, published)
        telescope = observatory.get_telescope(tel)

        self._reader = TelescopeReader(telescope, self.graph, tolerance=self.svc_config.interval,
                                       sun_altitude=SunAltitude.from_config(published), logger=self.svc_logger)
        self._actuator = Actuator(telescope, self.graph, self._reader, prove_timeout=self.svc_config.prove_timeout,
                                  poll=self.svc_config.interval, logger=self.svc_logger)
        self.endpoint = TicEndpoint.for_service(self, keepalive=self.svc_config.keepalive)
        self.endpoint.method('setpath', self._setpath)
        await self.endpoint.publish('self', 'operation', {'state': 'idle'})
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
            return parse_graph(components, telescope=tel)
        except GraphInvalid as e:
            for error in e.errors:
                self.svc_logger.error(f'{tel} optics [{error.code}] {error.message}')
            raise RuntimeError(f'{tel}: the optical graph in {source} is invalid ({len(e.errors)} problems)') from e

    async def run_service(self):
        while self.is_running:
            try:
                await self._publish(await self._reader.read())
            except Exception:
                self.svc_logger.exception('optics refresh failed')
            if not await self.sleep(self.svc_config.interval):
                break

    async def on_stop(self):
        if self._operation is not None and not self._operation.done():
            self._operation.cancel()
        if self.endpoint is not None:
            await self.endpoint.stop()

    # ------------------------------------------------------------------ status

    async def _publish(self, snapshot: Snapshot) -> None:
        state = with_busy_holds(self.graph, snapshot.state, snapshot.busy)
        for detector, entry in report(self.graph, state, self._actuatable).items():
            await self.endpoint.publish(detector, 'sees', entry['sees'])
            await self.endpoint.publish(detector, 'functions', entry['functions'])
        await self.endpoint.publish('self', 'selectors', {
            key: {'position': s.position, 'moving': s.moving} for key, s in sorted(snapshot.state.selectors.items())})
        await self.endpoint.publish('self', 'busy', dict(sorted(snapshot.busy.items())))

    # ------------------------------------------------------------------ setpath

    async def _setpath(self, call: Call):
        function = call.parameters.get('function')
        if not isinstance(function, str):
            raise Refusal("setpath needs the parameter 'function' (e.g. object, dark, flat)", code=4007)
        if self._operation is not None and not self._operation.done():
            raise Refusal('another light-path change is in progress', code=4008, severity=Severity.TEMPORARY)
        snapshot = await self._reader.read()
        state = with_busy_holds(self.graph, snapshot.state, snapshot.busy)
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
        await self._publish(await self._reader.read())


if __name__ == '__main__':
    OpticsService.main()
