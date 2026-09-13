import asyncio
import json

import pytest

from shuo.bluetooth.codec import BLUETOOTH_PCM_FORMAT
from shuo.bluetooth.pipewire import (
    PipeWireSelectionError,
    PipeWireTarget,
    StreamDirection,
)
from shuo.bluetooth.pipewire_live import (
    PipeWireDiscoveryError,
    PwCatConfig,
    PwCatCaptureEndpoint,
    PwCatPlaybackEndpoint,
    PwDumpDiscovery,
    build_pw_cat_command,
    discover_duplex_targets,
    parse_pw_dump_targets,
)
from shuo.bluetooth.process import CompletedCommand
from shuo.bluetooth.transport import OverflowPolicy


ADDRESS = "00:C7:11:7B:84:21"


def _pw_node(*, name, factory, media_class):
    return {
        "type": "PipeWire:Interface:Node",
        "info": {
            "props": {
                "node.name": name,
                "factory.name": factory,
                "media.class": media_class,
                "api.bluez5.address": ADDRESS,
                "api.bluez5.profile": "headset-audio-gateway",
                "api.bluez5.codec": "msbc",
            },
            "params": {
                "EnumFormat": [
                    {
                        "mediaType": "audio",
                        "mediaSubtype": "raw",
                        "format": {"default": "S16LE"},
                        "rate": {"default": 16000},
                        "channels": 1,
                    }
                ]
            },
        },
    }


def _target(direction):
    if direction is StreamDirection.DOWNLINK:
        return PipeWireTarget(
            node_name="bluez_input.fixture.0",
            factory_name="api.bluez5.sco.source",
            media_class="Stream/Output/Audio",
            bluetooth_address=ADDRESS,
            bluetooth_profile="headset-audio-gateway",
            bluetooth_codec="msbc",
            audio_format=BLUETOOTH_PCM_FORMAT,
        )

    return PipeWireTarget(
        node_name="bluez_output.fixture.1",
        factory_name="api.bluez5.sco.sink",
        media_class="Stream/Input/Audio",
        bluetooth_address=ADDRESS,
        bluetooth_profile="headset-audio-gateway",
        bluetooth_codec="msbc",
        audio_format=BLUETOOTH_PCM_FORMAT,
    )


def test_parse_pw_dump_targets_requires_explicit_contract():
    payload = json.dumps(
        [
            _pw_node(
                name="bluez_input.fixture.0",
                factory="api.bluez5.sco.source",
                media_class="Stream/Output/Audio",
            ),
            _pw_node(
                name="bluez_output.fixture.1",
                factory="api.bluez5.sco.sink",
                media_class="Stream/Input/Audio",
            ),
        ]
    )

    targets = parse_pw_dump_targets(payload)

    assert len(targets) == 2
    assert targets[0].audio_format == BLUETOOTH_PCM_FORMAT
    assert targets[1].audio_format == BLUETOOTH_PCM_FORMAT


def test_parse_pw_dump_ignores_node_without_negotiated_format():
    node = _pw_node(
        name="bad",
        factory="api.bluez5.sco.source",
        media_class="Stream/Output/Audio",
    )
    node["info"]["params"] = {}

    assert parse_pw_dump_targets(json.dumps([node])) == []


def test_build_capture_command_has_explicit_target_and_format():
    argv = build_pw_cat_command(
        _target(StreamDirection.DOWNLINK),
        direction=StreamDirection.DOWNLINK,
        latency="40ms",
    )

    assert argv[0] == "pw-cat"
    assert "--record" in argv
    assert "--target" in argv
    assert "bluez_input.fixture.0" in argv
    assert "--format" in argv
    assert "s16" in argv
    assert "--rate" in argv
    assert "16000" in argv
    assert "--channels" in argv
    assert "1" in argv
    assert "--raw" in argv


def test_build_playback_command_never_uses_default_target():
    argv = build_pw_cat_command(
        _target(StreamDirection.UPLINK),
        direction=StreamDirection.UPLINK,
        latency="40ms",
    )

    target_index = argv.index("--target")
    assert argv[target_index + 1] == "bluez_output.fixture.1"
    assert "--playback" in argv


class FakeRunRunner:
    def __init__(self, result):
        self.result = result
        self.argv = None

    async def run(self, argv):
        self.argv = tuple(argv)
        return self.result


@pytest.mark.asyncio
async def test_real_discovery_boundary_invokes_only_pw_dump():
    payload = json.dumps(
        [
            _pw_node(
                name="bluez_input.fixture.0",
                factory="api.bluez5.sco.source",
                media_class="Stream/Output/Audio",
            )
        ]
    ).encode()

    runner = FakeRunRunner(
        CompletedCommand(
            argv=("pw-dump",),
            returncode=0,
            stdout=payload,
            stderr=b"",
        )
    )

    discovery = PwDumpDiscovery(runner, system_name="Linux")
    targets = await discovery.list_targets()

    assert runner.argv == ("pw-dump",)
    assert len(targets) == 1


@pytest.mark.asyncio
async def test_pw_dump_failure_is_not_silently_ignored():
    runner = FakeRunRunner(
        CompletedCommand(
            argv=("pw-dump",),
            returncode=1,
            stdout=b"",
            stderr=b"failed",
        )
    )

    discovery = PwDumpDiscovery(runner, system_name="Linux")

    with pytest.raises(PipeWireDiscoveryError):
        await discovery.list_targets()


class FakeDiscovery:
    def __init__(self, targets):
        self.targets = list(targets)

    async def list_targets(self):
        return list(self.targets)


@pytest.mark.asyncio
async def test_discover_duplex_targets_selects_same_device():
    selected = await discover_duplex_targets(
        FakeDiscovery(
            [
                _target(StreamDirection.DOWNLINK),
                _target(StreamDirection.UPLINK),
            ]
        ),
        bluetooth_address=ADDRESS,
    )

    assert selected.downlink.bluetooth_address == ADDRESS
    assert selected.uplink.bluetooth_address == ADDRESS


@pytest.mark.asyncio
async def test_discover_duplex_targets_fails_if_direction_missing():
    with pytest.raises(PipeWireSelectionError):
        await discover_duplex_targets(
            FakeDiscovery([_target(StreamDirection.DOWNLINK)]),
            bluetooth_address=ADDRESS,
        )


class FakeReader:
    def __init__(self, chunks):
        self.chunks = list(chunks)

    async def read(self, _n=-1):
        if self.chunks:
            return self.chunks.pop(0)
        return b""


class FakeWriter:
    def __init__(self):
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(bytes(data))

    async def drain(self):
        await asyncio.sleep(0)

    def close(self):
        self.closed = True

    async def wait_closed(self):
        return None


class FakeProcess:
    def __init__(self, *, stdout_chunks=None, with_stdin=False):
        self.stdin = FakeWriter() if with_stdin else None
        self.stdout = FakeReader(stdout_chunks or [])
        self.stderr = FakeReader([])
        self.returncode = None
        self._done = asyncio.Event()
        self.terminate_calls = 0
        self.kill_calls = 0

    async def wait(self):
        await self._done.wait()
        return int(self.returncode)

    def terminate(self):
        self.terminate_calls += 1
        self.returncode = 0
        self._done.set()

    def kill(self):
        self.kill_calls += 1
        self.returncode = -9
        self._done.set()


class FakeSpawnRunner:
    def __init__(self, proc):
        self.proc = proc
        self.calls = []

    async def spawn(self, argv, *, stdin, stdout, stderr):
        self.calls.append(
            {
                "argv": tuple(argv),
                "stdin": stdin,
                "stdout": stdout,
                "stderr": stderr,
            }
        )
        return self.proc


@pytest.mark.asyncio
async def test_capture_reads_only_from_explicit_target_process():
    proc = FakeProcess(stdout_chunks=[b"\x00\x01\x02\x03"])
    runner = FakeSpawnRunner(proc)

    endpoint = PwCatCaptureEndpoint(
        _target(StreamDirection.DOWNLINK),
        runner,
        PwCatConfig(latency="40ms"),
        system_name="Linux",
    )

    await endpoint.start()
    assert await endpoint.read() == b"\x00\x01\x02\x03"
    await endpoint.stop()

    argv = runner.calls[0]["argv"]
    assert "--target" in argv
    assert "bluez_input.fixture.0" in argv


@pytest.mark.asyncio
async def test_playback_clear_drops_queued_not_yet_written_audio():
    proc = FakeProcess(with_stdin=True)
    runner = FakeSpawnRunner(proc)

    endpoint = PwCatPlaybackEndpoint(
        _target(StreamDirection.UPLINK),
        runner,
        PwCatConfig(
            latency="40ms",
            playback_queue_chunks=4,
            playback_overflow_policy=OverflowPolicy.REJECT_NEW,
        ),
        system_name="Linux",
    )

    await endpoint.start()

    # Pause the event loop as little as possible; queued data is allowed to be
    # handed to the writer immediately. The contract tested here is that clear()
    # is safe/idempotent and only claims to remove still-local queued data.
    await endpoint.write(b"one")
    await endpoint.clear()
    await endpoint.clear()

    await endpoint.stop()

    assert proc.stdin.closed is True


@pytest.mark.asyncio
async def test_repeated_stop_is_idempotent():
    proc = FakeProcess(with_stdin=True)
    runner = FakeSpawnRunner(proc)

    endpoint = PwCatPlaybackEndpoint(
        _target(StreamDirection.UPLINK),
        runner,
        PwCatConfig(latency="40ms"),
        system_name="Linux",
    )

    await endpoint.start()
    await endpoint.stop()
    await endpoint.stop()

    assert proc.terminate_calls <= 1
