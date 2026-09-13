from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Iterable

from .pipewire import PipeWireTarget
from .process import ProcessRunner


class RouteIsolationError(RuntimeError):
    pass


@dataclass(frozen=True, order=True)
class PipeWireLink:
    """One named PipeWire port-to-port link.

    Port names, not transient numeric object IDs, are retained. The names are
    discovered fresh for each session and are never treated as product constants.
    """

    output_port: str
    input_port: str


@dataclass(frozen=True)
class RouteIsolationSnapshot:
    removed_links: tuple[PipeWireLink, ...]


def _node_name(port_name: str) -> str:
    """Return the node part of a `node.name:port.name` pw-link port string."""
    return port_name.rsplit(":", 1)[0] if ":" in port_name else port_name


def parse_pw_link_listing(payload: bytes | str) -> set[PipeWireLink]:
    """Parse `pw-link -l` into a de-duplicated set of directed links.

    PipeWire prints each relationship from both endpoint perspectives. This parser
    accepts either form:

        source:port
          |-> sink:port

        sink:port
          |<- source:port

    Unknown/diagnostic lines are ignored rather than guessed.
    """

    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")

    links: set[PipeWireLink] = set()
    current_port: str | None = None

    for raw_line in payload.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue

        if stripped.startswith("|->"):
            if current_port is None:
                continue
            peer = stripped[3:].strip()
            if peer:
                links.add(PipeWireLink(current_port, peer))
            continue

        if stripped.startswith("|<-"):
            if current_port is None:
                continue
            peer = stripped[3:].strip()
            if peer:
                links.add(PipeWireLink(peer, current_port))
            continue

        # Top-level entries are port names. Indented relationship lines were
        # handled above; anything else becomes the new candidate root.
        if not raw_line[:1].isspace():
            current_port = stripped

    return links


def parse_pw_link_ports(payload: bytes | str) -> set[str]:
    """Return all top-level port names reported by ``pw-link -l``."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8", errors="replace")

    ports: set[str] = set()
    for raw_line in payload.splitlines():
        stripped = raw_line.strip()
        if not stripped or raw_line[:1].isspace():
            continue
        if stripped.startswith("|->") or stripped.startswith("|<-"):
            continue
        ports.add(stripped)

    return ports


def find_ai_only_forbidden_links(
    links: Iterable[PipeWireLink],
    *,
    downlink: PipeWireTarget,
    uplink: PipeWireTarget,
) -> tuple[PipeWireLink, ...]:
    """Find only the physical ALSA routes that contaminate an AI-only call.

    Removed during the Bluetooth AI session:
      * physical ALSA capture -> selected Bluetooth uplink
      * selected Bluetooth downlink -> physical ALSA playback

    Unrelated routes (for example Firefox -> laptop speaker) remain untouched.
    Bluetooth/pw-cat links are also untouched.
    """

    forbidden: list[PipeWireLink] = []

    for link in links:
        out_node = _node_name(link.output_port)
        in_node = _node_name(link.input_port)

        mic_into_phone = (
            out_node.startswith("alsa_input.")
            and in_node == uplink.node_name
        )
        phone_into_speaker = (
            out_node == downlink.node_name
            and in_node.startswith("alsa_output.")
        )

        if mic_into_phone or phone_into_speaker:
            forbidden.append(link)

    return tuple(sorted(set(forbidden)))


class AiOnlyRouteIsolation:
    """Temporarily isolate Bluetooth call audio from physical ALSA I/O.

    This does NOT disable microphone/speaker hardware globally. It removes only
    the current PipeWire links that connect the selected Bluetooth call streams
    to physical ALSA capture/playback nodes, then restores only links this object
    actually removed.

    Start is transactional: if one unlink fails, any links already removed are
    restored before the original error is raised.
    """

    def __init__(
        self,
        *,
        downlink: PipeWireTarget,
        uplink: PipeWireTarget,
        runner: ProcessRunner,
        restore_retry_attempts: int = 5,
        restore_retry_delay_seconds: float = 0.10,
    ) -> None:
        if restore_retry_attempts <= 0:
            raise ValueError("restore_retry_attempts must be positive")
        if restore_retry_delay_seconds < 0:
            raise ValueError("restore_retry_delay_seconds must be non-negative")

        self._downlink = downlink
        self._uplink = uplink
        self._runner = runner
        self._restore_retry_attempts = restore_retry_attempts
        self._restore_retry_delay_seconds = restore_retry_delay_seconds
        self._removed: list[PipeWireLink] = []
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    @property
    def snapshot(self) -> RouteIsolationSnapshot:
        return RouteIsolationSnapshot(tuple(self._removed))

    async def _list_links(self) -> set[PipeWireLink]:
        result = await self._runner.run(("pw-link", "-l"))
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise RouteIsolationError(
                f"pw-link -l failed with exit {result.returncode}: {stderr}"
            )
        return parse_pw_link_listing(result.stdout)

    async def _disconnect(self, link: PipeWireLink) -> None:
        result = await self._runner.run(
            ("pw-link", "-d", link.output_port, link.input_port)
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise RouteIsolationError(
                "failed to disconnect PipeWire link "
                f"{link.output_port!r} -> {link.input_port!r}: {stderr}"
            )

    async def _connect(self, link: PipeWireLink) -> None:
        result = await self._runner.run(
            ("pw-link", link.output_port, link.input_port)
        )
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            raise RouteIsolationError(
                "failed to restore PipeWire link "
                f"{link.output_port!r} -> {link.input_port!r}: {stderr}"
            )

    async def start(self) -> None:
        if self._running:
            return

        # A previous failed stop may have left restoration work pending. Do not
        # overwrite that ownership record with a new snapshot.
        if self._removed:
            raise RouteIsolationError(
                "cannot start route isolation while prior removed links remain"
            )

        links = await self._list_links()
        targets = find_ai_only_forbidden_links(
            links,
            downlink=self._downlink,
            uplink=self._uplink,
        )

        try:
            for link in targets:
                await self._disconnect(link)
                self._removed.append(link)

            # Fail closed: prove the forbidden links are actually gone before
            # capture/playback is allowed to start.
            remaining = find_ai_only_forbidden_links(
                await self._list_links(),
                downlink=self._downlink,
                uplink=self._uplink,
            )
            if remaining:
                names = ", ".join(
                    f"{item.output_port} -> {item.input_port}" for item in remaining
                )
                raise RouteIsolationError(
                    f"AI-only route isolation verification failed: {names}"
                )

        except BaseException:
            # ResourceGroup cannot roll this object back until start() returns,
            # so partial-start rollback is owned here.
            await self._restore_best_effort()
            raise

        self._running = True

    async def _restore_best_effort(self) -> None:
        if not self._removed:
            self._running = False
            return

        try:
            existing = await self._list_links()
        except Exception:
            # Preserve ownership for a later retry. Do not pretend restoration
            # happened when the graph cannot even be inspected.
            self._running = False
            return

        still_pending: list[PipeWireLink] = []

        for link in reversed(self._removed):
            if link in existing:
                continue
            try:
                await self._connect(link)
                existing.add(link)
            except Exception:
                still_pending.append(link)

        self._removed = list(reversed(still_pending))
        self._running = False

    async def stop(self) -> None:
        if not self._running and not self._removed:
            return

        self._running = False
        pending = list(self._removed)
        last_errors: dict[PipeWireLink, str] = {}

        for attempt in range(self._restore_retry_attempts):
            result = await self._runner.run(("pw-link", "-l"))
            if result.returncode != 0:
                stderr = result.stderr.decode("utf-8", errors="replace").strip()
                self._removed = pending
                raise RouteIsolationError(
                    "could not inspect PipeWire graph during restore: "
                    f"pw-link -l failed with exit {result.returncode}: {stderr}"
                )

            existing = parse_pw_link_listing(result.stdout)
            ports = parse_pw_link_ports(result.stdout)
            next_pending: list[PipeWireLink] = []

            for link in pending:
                if link in existing:
                    last_errors.pop(link, None)
                    continue

                out_node = _node_name(link.output_port)
                in_node = _node_name(link.input_port)

                if out_node == self._downlink.node_name:
                    bluetooth_port = link.output_port
                    physical_port = link.input_port
                elif in_node == self._uplink.node_name:
                    bluetooth_port = link.input_port
                    physical_port = link.output_port
                else:
                    next_pending.append(link)
                    last_errors[link] = (
                        "owned link no longer matches selected Bluetooth targets"
                    )
                    continue

                if bluetooth_port in ports and physical_port in ports:
                    try:
                        await self._connect(link)
                        last_errors.pop(link, None)
                        continue
                    except Exception as exc:
                        next_pending.append(link)
                        last_errors[link] = str(exc)
                        continue

                if bluetooth_port not in ports:
                    next_pending.append(link)
                    last_errors[link] = "selected Bluetooth port is not present"
                    continue

                next_pending.append(link)
                last_errors[link] = (
                    f"physical PipeWire port is not present: {physical_port}"
                )

            pending = next_pending
            if not pending:
                self._removed = []
                return

            if attempt + 1 < self._restore_retry_attempts:
                await asyncio.sleep(self._restore_retry_delay_seconds)

        # Final fresh graph: if the selected BlueZ endpoint stayed gone for the
        # entire bounded retry window, the old call route is obsolete and there
        # is nothing valid left to reconnect. Other failures remain errors.
        result = await self._runner.run(("pw-link", "-l"))
        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            self._removed = pending
            raise RouteIsolationError(
                "could not inspect PipeWire graph during final restore check: "
                f"pw-link -l failed with exit {result.returncode}: {stderr}"
            )

        existing = parse_pw_link_listing(result.stdout)
        ports = parse_pw_link_ports(result.stdout)
        still_pending: list[PipeWireLink] = []
        errors: list[str] = []

        for link in pending:
            if link in existing:
                continue

            out_node = _node_name(link.output_port)
            in_node = _node_name(link.input_port)
            if out_node == self._downlink.node_name:
                bluetooth_port = link.output_port
            elif in_node == self._uplink.node_name:
                bluetooth_port = link.input_port
            else:
                bluetooth_port = None

            if bluetooth_port is not None and bluetooth_port not in ports:
                continue

            still_pending.append(link)
            errors.append(
                f"{link.output_port!r} -> {link.input_port!r}: "
                f"{last_errors.get(link, 'restore did not complete')}"
            )

        self._removed = still_pending

        if errors:
            raise RouteIsolationError(
                "one or more PipeWire links could not be restored: "
                + "; ".join(errors)
            )

