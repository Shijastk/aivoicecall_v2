"""Synthetic, hardware/provider-free diagnostic and non-interference checks."""
import asyncio
import json
import math
import struct
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shuo.bluetooth.codec import AudioContractError
from shuo.bluetooth.diagnostics import AudioEnergy, BluetoothDiagnostics
from shuo.bluetooth.shuo_inbound import BluetoothInboundEvents
from shuo.bluetooth.routes import AiOnlyRouteIsolation, RouteIsolationError, parse_pw_link_listing
from shuo.services.flux import FluxService
from tests.test_bluetooth_routes import GraphRunner, PW_LINK_LISTING, _target
from tests.test_bluetooth_shuo_inbound import FakePhase3Capture


def test_energy_exact_statistics_and_arbitrary_fragmentation():
    samples = [0, 1, -32, 33, -32768, 32767]
    pcm = struct.pack('<6h', *samples)
    a, b = AudioEnergy(), AudioEnergy()
    a.feed(pcm)
    for value in pcm:
        b.feed(bytes([value]))
    assert a.snapshot() == b.snapshot()
    s = b.snapshot()
    assert s['bytes'] == 12 and s['samples'] == 6
    assert s['rms'] == pytest.approx(math.sqrt(sum(x*x for x in samples)/6))
    assert s['peak'] == 32768
    assert s['silence_ratio'] == 1/6
    assert s['near_silence_ratio'] == 3/6
    assert s['pending_sample_bytes'] == 0
    b.feed(b'\x01')
    assert b.snapshot()['pending_sample_bytes'] == 1


def test_mulaw_known_values_and_empty_silence():
    energy = AudioEnergy()
    assert energy.snapshot()['near_silence_ratio'] is None
    energy.feed(bytes([0xff, 0x7f, 0x80, 0x00]), mulaw=True)
    s = energy.snapshot()
    assert s['bytes'] == 4 and s['samples'] == 4
    assert s['peak'] == 32124
    assert s['rms'] == pytest.approx(32124 / math.sqrt(2))
    assert s['silence_ratio'] == s['near_silence_ratio'] == 0.5


@pytest.mark.asyncio
@pytest.mark.parametrize('amplitude', [0, 8000])
async def test_inbound_observation_preserves_bytes_and_measures_both_boundaries(amplitude):
    pcm = struct.pack('<320h', *[amplitude]*320)
    chunks = [pcm[:1], pcm[1:113], pcm[113:]]
    reports = []
    diag = BluetoothDiagnostics(emit=reports.append)
    observed = BluetoothInboundEvents(FakePhase3Capture(chunks), diagnostics=diag)
    normal = BluetoothInboundEvents(FakePhase3Capture(chunks))
    for _ in range(2):
        assert await observed.read_event() == await normal.read_event()
    assert observed.finish() == normal.finish() == b''
    s = reports[-1]
    assert s['raw_pcm']['bytes'] == 640
    assert s['raw_pcm']['rms'] == amplitude
    assert s['codec_mulaw']['bytes'] == 160
    assert s['codec_mulaw']['rms'] == pytest.approx(amplitude, abs=100)
    assert s['raw_pcm']['near_silence_ratio'] == (1 if amplitude == 0 else 0)
    assert set(s) == {'event', 'raw_pcm', 'codec_mulaw'}


def test_reports_are_rate_limited_and_flush_has_incomplete_sample_evidence():
    now = [0.0]
    reports = []
    diag = BluetoothDiagnostics(clock=lambda: now[0], emit=reports.append)
    for _ in range(100):
        diag.raw_pcm(b'\0\0')
        diag.codec_output(b'\xff')
    assert reports == []
    now[0] = 1.0
    diag.codec_output(b'')
    assert len(reports) == 1
    diag.raw_pcm(b'\x01')
    diag.flush()
    assert reports[-1]['raw_pcm']['pending_sample_bytes'] == 1
    assert len(diag.raw.tail) == 1


@pytest.mark.asyncio
async def test_diagnostic_sink_failure_does_not_change_audio():
    def broken(record):
        raise RuntimeError('sink unavailable')
    diag = BluetoothDiagnostics(emit=broken)
    bridge = BluetoothInboundEvents(FakePhase3Capture([b'\0\0'*320]), diagnostics=diag)
    assert (await bridge.read_event()).audio_bytes == b'\xff'*160
    bridge.finish()


@pytest.mark.asyncio
async def test_flux_all_message_shapes_content_free_and_callbacks_unchanged(caplog):
    reports = []
    diag = BluetoothDiagnostics(emit=reports.append)
    end, start, interim = AsyncMock(), AsyncMock(), AsyncMock()
    service = FluxService(end, start, interim, message_observer=diag.flux_message)
    secret = 'PRIVATE_TRANSCRIPT_SENTINEL'
    await service._on_message({'type': 'Connected'})
    await service._on_message(SimpleNamespace(type='TurnInfo', event='StartOfTurn'))
    for event in ('Update', 'EndOfTurn'):
        await service._on_message({'type':'TurnInfo', 'event':event, 'transcript':secret})
    await service._on_message({'type':'FatalError', 'description':secret})
    await service._on_message({'type':secret, 'event':secret, 'transcript':secret})
    await service._on_error(secret)
    assert len(reports) == 6
    assert [r['message_type'] for r in reports] == ['Connected','TurnInfo','TurnInfo','TurnInfo','FatalError','unknown']
    assert reports[2]['transcript_chars'] == len(secret)
    assert reports[3]['provider_event'] == 'EndOfTurn'
    assert secret not in json.dumps(reports) + caplog.text
    start.assert_awaited_once_with()
    end.assert_awaited_once_with(secret)
    interim.assert_awaited_once_with(secret)


@pytest.mark.asyncio
async def test_flux_broken_observer_does_not_drop_callback():
    def broken(*args):
        raise RuntimeError('observer unavailable')
    end = AsyncMock()
    service = FluxService(end, AsyncMock(), message_observer=broken)
    await service._on_message({'type':'TurnInfo','event':'EndOfTurn','transcript':'synthetic'})
    end.assert_awaited_once_with('synthetic')


def test_selected_node_metadata_excludes_transient_ids():
    reports = []
    diag = BluetoothDiagnostics(emit=reports.append)
    diag.selected('downlink', _target(downlink=True))
    assert reports[0] == dict(event='selected_node', direction='downlink',
        node_name=_target(downlink=True).node_name, factory='api.bluez5.sco.source',
        media_class='Stream/Output/Audio', profile='headset-audio-gateway',
        codec='msbc', format='s16le', rate=16000, channels=1)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure', [False, True])
async def test_route_diagnostics_observe_restore_without_changing_commands(failure):
    original = parse_pw_link_listing(PW_LINK_LISTING)
    reports = []
    runners = [GraphRunner(original), GraphRunner(original)]
    for index, runner in enumerate(runners):
        isolation = AiOnlyRouteIsolation(downlink=_target(downlink=True),
            uplink=_target(downlink=False), runner=runner,
            restore_retry_attempts=2, restore_retry_delay_seconds=0,
            diagnostics=BluetoothDiagnostics(emit=reports.append) if index else None)
        await isolation.start()
        runner.fail_restore = failure
        if failure:
            with pytest.raises(RouteIsolationError):
                await isolation.stop()
        else:
            await isolation.stop()
    assert runners[0].calls == runners[1].calls
    assert runners[0].links == runners[1].links
    assert sum(r['event']=='route_owned_removed' for r in reports) == 4
    results = [r for r in reports if r['event']=='route_connect_result']
    assert results and all(r['returncode'] == int(failure) for r in results)
    assert len(reports[-1]['pending_links']) == (4 if failure else 0)


@pytest.mark.asyncio
async def test_route_reports_obsolete_decision_even_when_pending_becomes_empty():
    reports = []
    runner = GraphRunner(parse_pw_link_listing(PW_LINK_LISTING), extra_ports=set())
    isolation = AiOnlyRouteIsolation(downlink=_target(downlink=True),
        uplink=_target(downlink=False), runner=runner,
        restore_retry_attempts=1, restore_retry_delay_seconds=0,
        diagnostics=BluetoothDiagnostics(emit=reports.append))
    await isolation.start()
    await isolation.stop()
    assert sum(r['event']=='route_dropped_as_obsolete' for r in reports) == 4
    assert not any(r['event']=='route_connect_attempt' for r in reports)
    assert reports[-1]['pending_links'] == []


@pytest.mark.asyncio
async def test_route_partial_start_rollback_is_observed():
    reports = []
    runner = GraphRunner(parse_pw_link_listing(PW_LINK_LISTING), fail_disconnect_at=2)
    isolation = AiOnlyRouteIsolation(downlink=_target(downlink=True),
        uplink=_target(downlink=False), runner=runner,
        diagnostics=BluetoothDiagnostics(emit=reports.append))
    with pytest.raises(RouteIsolationError):
        await isolation.start()
    assert reports[-1] == {'event':'route_rollback_final','pending_links':[]}
    assert any(r['event']=='route_connect_result' and r['returncode']==0 for r in reports)
