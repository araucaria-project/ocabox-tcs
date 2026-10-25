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
``status.optics.<tel>.self.operation``  the last path change: awaiting_confirmation → moving →
                                        done | failed, or rejected | expired | superseded
``rpc.optics.<tel>.<det>.setpath``      ``{function}``: switch so the detector can do it
``rpc.optics.<tel>.self.declare``       ``{key, position}``: vouch for an axis no reading proves
                                        (``position: null`` withdraws)
``rpc.optics.<tel>.self.confirm``       ``{operation}``: a person confirms the waiting change
``rpc.optics.<tel>.self.reject``        ``{operation, reason?}``: a person turns it down
``rpc.optics.<tel>.self.abort``         ``{operation}``: stop a waiting or running change
``tic.journal.<tel>.optics``            a waiting change announced, with confirm/reject actions
======================================  ===================================================

``setpath`` answers at once — accepted with the moves, or refused with an obcom code (4007 no such
detector/function, 4008 a busy detector's path or another switch in progress, 4009 a manual move
needed or the light unavailable) — and the switch runs in the background, reported in
``self.operation``, like a slew. Graph: the telescope's ``components`` from the published
observatory configuration, or ``components_file`` (a config.yaml) to try a graph before deploying.

Moves that wait for a person (:mod:`.confirmation`: the telescope and the dome to the flat screen,
the covers by day) do not start at ``setpath``: the operation waits, announced in ``self.operation``
and the journal, until a person confirms it (``confirm_timeout``, else *expired*). At confirmation
the service decides again; if the moves or the state they start from differ, the request is
*superseded* — a person confirms exactly what they were shown. One change at a time: a waiting one
counts, and asking again for the same detector and function returns it. Light still to come (the
sky at dusk) is prepared for: ``setpath`` makes the moves that make the path ready for it.

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
import uuid
from dataclasses import dataclass, field, replace
from importlib.metadata import version
from pathlib import Path

import yaml
from datamodels.messages import Severity
from datamodels.optics import state_key
from obcom.optics import Declaration, GraphInvalid, OpticalGraph, ProvenState, label, parse_graph, resolve_axes
from serverish.messenger import get_journalpublisher

from ocabox_tcs.base_service import BaseBlockingPermanentService, BaseServiceConfig, config, service
from ocabox_tcs.services.optics.actuation import ActuationError, Actuator
from ocabox_tcs.services.optics.confirmation import ConfirmPolicy, Pending, fingerprint, says
from ocabox_tcs.services.optics.engine import Decision, decide, report, require_function, with_busy_holds
from ocabox_tcs.services.optics.readings import Snapshot, TelescopeReader
from ocabox_tcs.services.optics.sun import SunAltitude
from ocabox_tcs.tic_endpoint import Call, Refusal, TicEndpoint

#: ocabox-common with ``awaits``/``prepare``, ``also`` and ``DomeKind.screen`` (1.13.0)
REQUIRED_OCABOX_COMMON = (1, 13, 0)


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
    confirm: dict[str, str] = field(default_factory=dict)  #: more moves that wait for a person: {state key: always | day}
    confirm_timeout: float = 120.0  #: s a change waits for a person's confirmation before it expires


@service('optics')
class OpticsService(BaseBlockingPermanentService):

    def __init__(self):
        super().__init__()
        self.graph: OpticalGraph | None = None
        self.endpoint: TicEndpoint | None = None
        self._reader: TelescopeReader | None = None
        self._actuator: Actuator | None = None
        self._operation: asyncio.Task | None = None
        self._running: dict | None = None  #: the operation document of the change in progress
        self._pending: Pending | None = None  #: the change that waits for a person
        self._expiry: asyncio.Task | None = None
        self._policy = ConfirmPolicy()
        self._journal = None  #: serverish journal publisher of ``tic.journal.<tel>.optics``
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
        try:
            self._policy = ConfirmPolicy.from_config(self.graph, self.svc_config.confirm)
        except ValueError as e:
            raise RuntimeError(f'{tel}: {e}') from e
        telescope = observatory.get_telescope(tel)

        self._reader = TelescopeReader(telescope, self.graph, tolerance=self.svc_config.interval,
                                       max_age=self.svc_config.max_age, sun=SunAltitude.from_config(published),
                                       logger=self.svc_logger)
        self._actuator = Actuator(telescope, self.graph, self._reader, prove_timeout=self.svc_config.prove_timeout,
                                  logger=self.svc_logger)
        await self._reader.start()
        self.endpoint = TicEndpoint.for_service(self, keepalive=self.svc_config.keepalive)
        self.endpoint.method('setpath', self._setpath)
        self.endpoint.method('declare', self._declare, element='self')
        self.endpoint.method('confirm', self._confirm, element='self')
        self.endpoint.method('reject', self._reject, element='self')
        self.endpoint.method('abort', self._abort, element='self')
        self._journal = get_journalpublisher(f'tic.journal.{tel}.optics')
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
        if self._pending is not None:
            await self._settle(self._pending, 'expired', 'the optics service stopped')
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
        for detector, entry in report(self.graph, state, self._actuatable, self._policy, self._actuator.reach).items():
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
        # checked after the last await: two concurrent requests can never both start a change
        pending = self._pending
        if pending is not None and (pending.detector, pending.function) == (call.element, function):
            return self._answer(Decision(pending.moves, pending.waiting), pending)  # asked again: the same request
        if self._in_progress():
            raise Refusal('another light-path change is in progress', code=4008, severity=Severity.TEMPORARY)
        state = with_busy_holds(self.graph, proven, snapshot.busy)
        decision = decide(self.graph, state, call.element, function, self._actuatable, self._policy, self._actuator.reach)
        who = call.caller.name if call.caller else None
        if decision.waiting:
            pending = Pending(call.element, function, decision.moves, decision.waiting,
                              says(self.graph, decision.moves, decision.waiting),
                              fingerprint(self.graph, state, decision.moves, snapshot.busy), who,
                              self.svc_config.confirm_timeout)
            self._pending = pending
            self._expiry = asyncio.create_task(self._expire(pending), name=f'optics.confirm.{pending.id}')
            await self._announce(pending)
        elif decision.moves:
            self._start(call.element, function, decision.moves, who)
        return self._answer(decision, pending if decision.waiting else None)

    def _in_progress(self) -> bool:
        return self._pending is not None or (self._operation is not None and not self._operation.done())

    @staticmethod
    def _answer(decision: Decision, pending: Pending | None) -> dict:
        """What ``setpath`` answers: the moves; the light they prepare for; the change that waits."""
        answer = {'accepted': True, 'moves': decision.moves}
        if decision.awaits is not None:
            answer['awaits'] = decision.awaits
        if pending is not None:
            answer.update(operation=pending.id, awaiting_confirmation=True, confirm=list(pending.waiting),
                          says=pending.says, expires_at=pending.expires_at)
        return answer

    def _start(self, detector: str, function: str, moves: dict[str, str], caller: str | None,
               operation_id: str | None = None, confirmed_by: str | None = None) -> None:
        operation = {'id': operation_id or uuid.uuid4().hex[:12], 'detector': detector, 'function': function,
                     'moves': moves, 'caller': caller}
        if confirmed_by is not None:
            operation['confirmed_by'] = confirmed_by
        self._running = operation
        self._operation = asyncio.create_task(self._operate(operation), name=f'optics.{detector}.{function}')

    async def _operate(self, operation: dict) -> None:
        detector, function, moves = operation['detector'], operation['function'], operation['moves']
        await self.endpoint.publish('self', 'operation', {**operation, 'state': 'moving'})
        try:
            await self._actuator.apply(moves)
        except ActuationError as e:
            self.svc_logger.error(f'{detector}.{function}: {e}')
            await self.endpoint.publish('self', 'operation', {**operation, 'state': 'failed', 'message': str(e)})
        else:
            self.svc_logger.info(f'{detector}.{function}: {moves} proven')
            await self.endpoint.publish('self', 'operation', {**operation, 'state': 'done'})
        finally:
            self._running = None
        await self._publish(self._reader.snapshot())

    # ------------------------------------------------------------------ confirmation

    def _waiting_for(self, call: Call) -> Pending:
        """The waiting change ``call`` names — or 4007 saying what waits instead."""
        wanted, pending = call.parameters.get('operation'), self._pending
        if pending is None:
            raise Refusal('no light-path change waits for a confirmation', code=4007)
        if wanted != pending.id:
            raise Refusal(f'operation {wanted!r} does not wait; {pending.id} does '
                          f'({pending.detector} {pending.function}: {"; ".join(pending.says)})', code=4007)
        return pending

    async def _confirm(self, call: Call):
        pending = self._waiting_for(call)
        who = call.caller.name if call.caller else 'unknown'
        snapshot = self._reader.snapshot()
        proven = await self._with_declarations(snapshot.state)
        if self._pending is not pending:  # settled while we read the declarations
            raise Refusal(f'operation {pending.id} no longer waits', code=4007)
        if pending.expired():
            await self._settle(pending, 'expired', f'confirmed by {who} too late')
            raise Refusal(f'operation {pending.id} expired — ask again', code=4008, severity=Severity.TEMPORARY)
        state = with_busy_holds(self.graph, proven, snapshot.busy)
        try:
            decision = decide(self.graph, state, pending.detector, pending.function, self._actuatable,
                              self._policy, self._actuator.reach)
        except Refusal as e:
            await self._settle(pending, 'superseded', f'not possible any more: {e.message}', who)
            raise
        changed = self._changes(pending, decision, fingerprint(self.graph, state, decision.moves, snapshot.busy))
        if changed:
            await self._settle(pending, 'superseded', f'the state changed since it was asked: {changed} — ask again', who)
            raise Refusal(f'{pending.detector} {pending.function}: the state changed since it was asked ({changed}) — ask again',
                          code=4008, severity=Severity.TEMPORARY)
        self._clear(pending)
        self.svc_logger.info(f'{who}: confirms {pending.detector}.{pending.function} ({"; ".join(pending.says)})')
        await self._journal_close(pending, f'{pending.detector} {pending.function}: confirmed by {who}')
        self._start(pending.detector, pending.function, pending.moves, pending.asked_by, pending.id, confirmed_by=who)
        return {'confirmed': True, 'operation': pending.id, 'moves': pending.moves}

    def _changes(self, pending: Pending, decision: Decision, now: tuple) -> str:
        """What differs between the change as asked and as it would be made now, in words; empty
        when nothing does."""
        if decision.moves != pending.moves or decision.waiting != pending.waiting:
            return f'it would now move {", ".join(f"{label(self.graph, k)} {v}" for k, v in decision.moves.items()) or "nothing"}'
        (_, then_positions, then_busy), (_, now_positions, now_busy) = pending.fingerprint, now
        moved = [f'{label(self.graph, k)} reads {p or "nothing"}' for (k, p), (_, q) in zip(now_positions, then_positions) if p != q]
        if now_busy != then_busy:
            moved.append(f'busy now: {", ".join(now_busy) or "none"}')
        return '; '.join(moved)

    async def _reject(self, call: Call):
        pending = self._waiting_for(call)
        who = call.caller.name if call.caller else 'unknown'
        reason = call.parameters.get('reason')
        await self._settle(pending, 'rejected', f'rejected by {who}' + (f': {reason}' if reason else ''), who)
        return {'rejected': True, 'operation': pending.id}

    async def _abort(self, call: Call):
        """Stop the waiting change (as a reject) or the running one (the mount and the dome stop)."""
        wanted = call.parameters.get('operation')
        who = call.caller.name if call.caller else 'unknown'
        if self._pending is not None and wanted == self._pending.id:
            pending = self._pending
            await self._settle(pending, 'rejected', f'aborted by {who}', who)
            return {'aborted': True, 'operation': pending.id}
        running, task = self._running, self._operation
        if running is None or task is None or task.done() or wanted != running['id']:
            raise Refusal(f'operation {wanted!r} is neither waiting nor running', code=4007)
        task.cancel()
        self.svc_logger.warning(f'{who}: aborts {running["detector"]}.{running["function"]}')
        message = f'aborted by {who}'
        try:
            await self._actuator.abort(running['moves'])
        except ActuationError as e:
            message += f' — {e}'
        self._running = None
        await self.endpoint.publish('self', 'operation', {**running, 'state': 'failed', 'message': message})
        await self._publish(self._reader.snapshot())
        return {'aborted': True, 'operation': running['id']}

    async def _expire(self, pending: Pending) -> None:
        await asyncio.sleep(max(0.0, pending.expires_at - time.time()))
        if self._pending is pending:
            self._expiry = None  # this task settles it: nothing to cancel
            await self._settle(pending, 'expired', f'nobody confirmed within {pending.timeout:.0f} s')

    def _clear(self, pending: Pending) -> None:
        if self._pending is pending:
            self._pending = None
        if self._expiry is not None and not self._expiry.done() and self._expiry is not asyncio.current_task():
            self._expiry.cancel()
        self._expiry = None

    async def _settle(self, pending: Pending, outcome: str, message: str, who: str | None = None) -> None:
        """A waiting change ends without moving anything: rejected, expired or superseded."""
        self._clear(pending)
        self.svc_logger.info(f'{pending.detector}.{pending.function} {outcome}: {message}')
        document = {**pending.published(), 'state': outcome, 'message': message}
        if who is not None:
            document['decided_by'] = who
        await self.endpoint.publish('self', 'operation', document)
        await self._journal_close(pending, f'{pending.detector} {pending.function}: {message}')

    # ------------------------------------------------------------------ journal

    async def _announce(self, pending: Pending) -> None:
        """``self.operation`` and a journal entry with the confirm/reject actions: any client that
        shows the journal sees that a change waits; one that can act does the PUT the meta names."""
        await self.endpoint.publish('self', 'operation', pending.published())
        tel = self.telescope_id
        self.svc_logger.warning(f'{pending.detector}.{pending.function} waits for a person: {"; ".join(pending.says)}')
        if self._journal is None:
            return
        address = f'{tel}.optics.self'
        try:
            pending.conversation = await self._journal.warning(
                f'{tel} {pending.detector} {pending.function} waits for a person: {"; ".join(pending.says)}',
                explanation=(f'asked by {pending.asked_by or "unknown"}; confirm within {pending.timeout:.0f} s '
                             f'(until {time.strftime("%H:%M:%S", time.gmtime(pending.expires_at))} UTC), else it expires'),
                actions=['confirm', 'reject'],
                meta={'operation': pending.id, 'actions_rpc': {
                    action: {'address': f'{address}.{action}', 'parameters': {'operation': pending.id}}
                    for action in ('confirm', 'reject')}})
        except Exception:
            self.svc_logger.exception('optics journal: the waiting change was not announced')

    async def _journal_close(self, pending: Pending, message: str) -> None:
        entry = pending.conversation
        if self._journal is None or entry is None:
            return
        try:
            entry.message, entry.actions = message, []
            await self._journal.publish_journal_operation(entry, op='update')
            await self._journal.publish_journal_operation(entry, op='close')
        except Exception:
            self.svc_logger.exception('optics journal: the outcome was not published')

if __name__ == '__main__':
    OpticsService.main()
