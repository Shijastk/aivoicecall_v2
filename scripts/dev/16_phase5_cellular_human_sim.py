#!/usr/bin/env python3
"""Phase-5 supplemental real cellular + Bluetooth synthetic-human benchmark.

The dialogue is automated, but handset answer/hangup remain manual by the locked
Phase-5 scope. No raw audio is written. Missing physical boundaries stay
NOT_MEASURED rather than being inferred from local timestamps.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import re
import secrets
import socket
import statistics
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx
import uvicorn
from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response
from starlette.websockets import WebSocketDisconnect

from shuo import config
from shuo.agent import Agent
from shuo.bluetooth.diagnostics import BluetoothDiagnostics
from shuo.bluetooth.phase3_session import build_phase3_ai_only_session
from shuo.bluetooth.pipewire import PipeWireSelectionError
from shuo.bluetooth.pipewire_live import PwCatConfig, PwDumpDiscovery
from shuo.bluetooth.process import AsyncioProcessRunner
from shuo.bluetooth.production import BluetoothProductionDeps, run_production_bluetooth_conversation
from shuo.carrier import get_carrier
from shuo.services.flux import FluxService
from shuo.services.tts_pocket import PocketTTSService, pocket_tts_available
from shuo.types import CallContext, CallDirection, MediaEvent, PlaybackMarkEvent, StreamStartEvent, StreamStopEvent

log = logging.getLogger("shuo.phase5_human_sim")
FRAME_BYTES = 160
MAX_LIVE_SECONDS = 300.0
DEFAULT_OUT_DIR = Path(tempfile.gettempdir()) / "shuo"


def _field(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def _ms(a, b):
    return None if a is None or b is None or b < a else (b - a) / 1_000_000.0


def _words(text):
    return set(re.findall(r"[a-z0-9]+", (text or "").casefold()))


def _has(text, *required):
    words = _words(text)
    return all(x.casefold() in words for x in required)


def _has_any(text, alternatives):
    return any(_has(text, *req) for req in alternatives)


def validate_phone(raw):
    value = (raw or "").strip().replace(" ", "").replace("-", "")
    if not re.fullmatch(r"\+[1-9]\d{7,14}", value):
        raise argparse.ArgumentTypeError("phone must be E.164, e.g. +919876543210")
    return value


def validate_duration(raw):
    value = float(raw)
    if not 30.0 <= value <= MAX_LIVE_SECONDS:
        raise argparse.ArgumentTypeError("Phase 5 live duration must be 30..300 seconds")
    return value


def _public_url(base, path, *, websocket=False):
    if not base or not base.startswith(("http://", "https://")):
        raise RuntimeError("PUBLIC_URL must be configured as http(s)")
    url = base.rstrip("/") + "/" + path.lstrip("/")
    if not websocket:
        return url
    p = urlsplit(url)
    return urlunsplit(("wss" if p.scheme == "https" else "ws", p.netloc, p.path, p.query, p.fragment))


def _frames(audio):
    if not audio:
        raise RuntimeError("synthetic caller audio is empty")
    out = []
    for i in range(0, len(audio), FRAME_BYTES):
        frame = audio[i:i + FRAME_BYTES]
        out.append(frame + bytes([0xFF]) * (FRAME_BYTES - len(frame)))
    return out


async def _pocket(text):
    chunks, done = [], asyncio.Event()

    async def audio(payload):
        chunks.append(base64.b64decode(payload))

    async def finished():
        done.set()

    tts = PocketTTSService(audio, finished)
    await tts.start()
    try:
        await tts.send(text)
        await tts.flush()
        await asyncio.wait_for(done.wait(), 30)
        if tts.fatal_error:
            raise RuntimeError(tts.fatal_error)
    finally:
        await tts.cancel()
    data = b"".join(chunks)
    if not data:
        raise RuntimeError("Pocket produced no caller audio")
    return data


class Probe:
    def __init__(self):
        self.sot, self.eot, self.agent, self.cancel_begin, self.cancel_end = [], [], [], [], []
        self.first_audio, self.first_write, self.clear = {}, {}, []
        self.turn = 0
        self.reader_ready = asyncio.Event()
        self.changed = asyncio.Event()

    def touch(self):
        self.changed.set()


class ObservedSession:
    def __init__(self, inner, probe):
        self.inner, self.probe = inner, probe

    @property
    def running(self):
        return self.inner.running

    async def start(self):
        await self.inner.start()

    async def read(self):
        data = await self.inner.read()
        self.probe.reader_ready.set()
        self.probe.touch()
        return data

    async def write(self, data):
        t = self.probe.turn
        if t and t not in self.probe.first_write:
            self.probe.first_write[t] = time.monotonic_ns()
            self.probe.touch()
        await self.inner.write(data)

    async def clear(self):
        self.probe.clear.append(time.monotonic_ns())
        self.probe.touch()
        await self.inner.clear()

    async def stop(self):
        await self.inner.stop()


def observed_flux(probe):
    class ObservedFlux(FluxService):
        async def _on_message(self, message, *args, **kwargs):
            if _field(message, "type") == "TurnInfo":
                event, now = _field(message, "event"), time.monotonic_ns()
                if event == "StartOfTurn":
                    probe.sot.append(now)
                    probe.touch()
                elif event == "EndOfTurn":
                    probe.eot.append(now)
                    probe.touch()
            await super()._on_message(message, *args, **kwargs)

    return ObservedFlux


def observed_agent(probe):
    class ObservedAgent(Agent):
        async def start_turn(self, transcript, prepared_response=None):
            probe.turn += 1
            self._human_sim_turn = probe.turn
            probe.agent.append(time.monotonic_ns())
            probe.touch()
            await super().start_turn(transcript, prepared_response)

        async def cancel_turn(self):
            active = self.is_turn_active
            if active:
                probe.cancel_begin.append(time.monotonic_ns())
                probe.touch()
            try:
                await super().cancel_turn()
            finally:
                if active:
                    probe.cancel_end.append(time.monotonic_ns())
                    probe.touch()

        async def _on_tts_audio(self, payload):
            t = getattr(self, "_human_sim_turn", probe.turn)
            if t and t not in probe.first_audio:
                probe.first_audio[t] = time.monotonic_ns()
                probe.touch()
            await super()._on_tts_audio(payload)

    return ObservedAgent


class RemoteSide:
    """Remote Vobiz caller leg: sends scripted caller audio and observes returned AI audio."""

    def __init__(self, carrier, threshold):
        self.carrier, self.threshold = carrier, threshold
        self.session = None
        self.stream_started, self.call_ended = asyncio.Event(), asyncio.Event()
        self.sot, self.turns = [], []
        self.pending_sot = None
        self.changed = asyncio.Event()
        self.marks, self.mark_times = {}, {}
        self.flux = None

    async def _sot(self):
        now = time.monotonic_ns()
        self.sot.append(now)
        self.pending_sot = now
        self.changed.set()

    async def _eot(self, transcript):
        now = time.monotonic_ns()
        self.turns.append((self.pending_sot or now, now, transcript))
        self.pending_sot = None
        self.changed.set()

    async def _interim(self, _):
        pass

    async def _ensure_flux(self):
        if self.flux is None:
            self.flux = FluxService(
                on_end_of_turn=self._eot,
                on_start_of_turn=self._sot,
                on_interim=self._interim,
                eot_threshold=self.threshold,
            )
            await self.flux.start()

    async def websocket_loop(self, websocket):
        context = CallContext(
            call_id="",
            direction=CallDirection.OUTBOUND,
            persona_id="phase5-human-sim",
            carrier=self.carrier.name,
        )
        self.session = self.carrier.new_session(websocket, context)
        try:
            while True:
                raw = await websocket.receive_text()
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                for event in self.session.parse_message(payload):
                    if isinstance(event, StreamStartEvent):
                        await self._ensure_flux()
                        self.stream_started.set()
                        self.changed.set()
                    elif isinstance(event, MediaEvent) and event.track == "inbound" and self.flux:
                        await self.flux.send(event.audio_bytes)
                    elif isinstance(event, PlaybackMarkEvent):
                        self.mark_times[event.name] = time.monotonic_ns()
                        if event.name in self.marks:
                            self.marks[event.name].set()
                    elif isinstance(event, StreamStopEvent):
                        return
        except WebSocketDisconnect as exc:
            log.info("Vobiz media WebSocket closed code=%s", exc.code)
        finally:
            self.call_ended.set()
            self.changed.set()

    async def stop(self):
        if self.flux:
            await self.flux.stop()
            self.flux = None

    async def wait_sot(self, count, timeout):
        async def wait():
            while len(self.sot) < count:
                self.changed.clear()
                if len(self.sot) >= count:
                    break
                await self.changed.wait()
            return self.sot[count - 1]

        return await asyncio.wait_for(wait(), timeout)

    async def wait_turn(self, count, timeout):
        async def wait():
            while len(self.turns) < count:
                self.changed.clear()
                if len(self.turns) >= count:
                    break
                await self.changed.wait()
            return self.turns[count - 1]

        return await asyncio.wait_for(wait(), timeout)

    async def send_audio(self, audio):
        if not self.session or not self.session.started:
            raise RuntimeError("carrier stream is not started")
        first = last = 0
        deadline = time.monotonic()
        for i, frame in enumerate(_frames(audio)):
            now = time.monotonic_ns()
            first = first or now
            last = now
            await self.session.play_audio(self.session.encode_payload(frame))
            deadline += 0.020
            delay = deadline - time.monotonic()
            if delay > 0:
                await asyncio.sleep(delay)
        return first, last

    async def checkpoint(self, label):
        name = f"human-sim-{label}-{secrets.token_hex(3)}"
        ev = asyncio.Event()
        self.marks[name] = ev
        try:
            await self.session.checkpoint(name)
            await asyncio.wait_for(ev.wait(), 5)
            return self.mark_times.get(name)
        except asyncio.TimeoutError:
            return None
        finally:
            self.marks.pop(name, None)

    async def prompt(self, label, audio):
        first, last = await self.send_audio(audio)
        return {
            "label": label,
            "first": first,
            "last": last,
            "checkpoint": await self.checkpoint(label),
        }


class Runner:
    def __init__(self, args):
        self.args = args
        self.carrier = get_carrier()
        self.public = config.public_url()
        self.token = secrets.token_urlsafe(24)
        self.probe = Probe()
        self.remote = RemoteSide(self.carrier, args.eot_threshold)
        self.app = FastAPI()
        self.server = self.server_task = self.bt_task = None
        self.checks, self.metrics, self.private, self.obs, self.barges = [], [], {}, [], []
        self.live_start = None
        self.error = None
        self._routes()

    def check(self, name, passed, note="", status=None):
        self.checks.append(
            {
                "name": name,
                "passed": passed,
                "status": status or ("PROVEN_REAL_CALL" if passed else "FAILED_REAL_CALL"),
                "note": note,
            }
        )

    async def _authenticate_carrier(self, request: Request):
        """Validate every carrier HTTP callback using the repository Vobiz rule."""
        body = await request.body()
        try:
            form = dict(await request.form()) if body else {}
        except Exception:
            form = {}

        public_url = _public_url(self.public, request.url.path)
        if request.url.query:
            public_url = f"{public_url}?{request.url.query}"

        ok = self.carrier.validate_signature(
            url=public_url,
            headers=dict(request.headers),
            body=body,
            form=form,
        )
        if not ok:
            return None, Response(status_code=403)
        return form, None

    def _routes(self):
        @self.app.get("/__shuo_phase5_human_sim/probe")
        async def probe(request: Request):
            if request.query_params.get("token") != self.token:
                return Response(status_code=403)
            return Response(status_code=204)

        @self.app.api_route("/__shuo_phase5_human_sim/answer", methods=["GET", "POST"])
        async def answer(request: Request):
            _, denied = await self._authenticate_carrier(request)
            if denied is not None:
                return denied
            if request.query_params.get("token") != self.token:
                return Response(status_code=403)
            ws = _public_url(
                self.public,
                f"/__shuo_phase5_human_sim/ws?token={self.token}",
                websocket=True,
            )
            return Response(self.carrier.answer_xml(ws, record=False), media_type="application/xml")

        @self.app.websocket("/__shuo_phase5_human_sim/ws")
        async def media(ws: WebSocket):
            if ws.query_params.get("token") != self.token:
                await ws.close(code=1008)
                return
            await ws.accept()
            await self.remote.websocket_loop(ws)

        @self.app.api_route("/__shuo_phase5_human_sim/hangup", methods=["GET", "POST"])
        async def hangup(request: Request):
            form, denied = await self._authenticate_carrier(request)
            if denied is not None:
                return denied
            if request.query_params.get("token") != self.token:
                return Response(status_code=403)

            expected = (
                self.remote.session.call_id
                if self.remote.session is not None
                else None
            )
            received = str((form or {}).get("CallUUID") or "")
            if expected and received != expected:
                log.warning(
                    "Rejected signed hangup callback with unexpected CallUUID"
                )
                return Response(status_code=403)

            self.remote.call_ended.set()
            return Response(status_code=204)

    def preflight(self):
        if not self.args.allow_real_call:
            raise RuntimeError("--allow-real-call is required")
        if self.carrier.name != "vobiz":
            raise RuntimeError("reference human-sim requires CARRIER=vobiz")
        if (
            (os.getenv("TTS_PROVIDER") or "").strip().lower() != "pocket"
            or (os.getenv("TTS_FALLBACK_PROVIDER") or "").strip()
        ):
            raise RuntimeError(
                "requires TTS_PROVIDER=pocket and empty TTS_FALLBACK_PROVIDER"
            )
        if self.args.latency != "120ms" or abs(self.args.eot_threshold - 0.8) > 1e-9:
            raise RuntimeError(
                "reference run is locked to --latency 120ms and --eot-threshold 0.8"
            )
        if not self.public:
            raise RuntimeError("PUBLIC_URL is required")
        public_host = (urlsplit(self.public).hostname or "").casefold()
        if public_host in {"localhost", "127.0.0.1", "::1"}:
            raise RuntimeError(
                "PUBLIC_URL must be a public tunnel/host that routes to this local harness"
            )
        if not pocket_tts_available():
            raise RuntimeError("Pocket TTS is not installed")
        required = (
            "VOBIZ_AUTH_ID",
            "VOBIZ_AUTH_TOKEN",
            "VOBIZ_PHONE_NUMBER",
            "DEEPGRAM_API_KEY",
            "GROQ_API_KEY",
        )
        missing = [x for x in required if not os.getenv(x, "").strip()]
        if missing:
            raise RuntimeError("Missing required environment variable(s): " + ", ".join(missing))
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", self.args.port))
        except OSError as exc:
            raise RuntimeError(
                f"port {self.args.port} is in use; stop main.py/other server"
            ) from exc
        finally:
            s.close()

    async def synthesize(self):
        text = {
            "arithmetic": "Hello. Controlled call test. Answer briefly: what is two plus three?",
            "code": "Remember this temporary test codeword for this call: blue seven. Repeat it once.",
            "sky": "In one short sentence, what color does the daytime sky usually appear?",
            "think_a": "I want to ask you something about",
            "think_b": "the temporary codeword I gave you. What was it?",
            "b1_long": "Explain how a petrol engine works in enough detail that you would normally speak for at least fifteen seconds.",
            "b1_stop": "Stop there. What temporary codeword did I give you? Answer briefly.",
            "weekday": "Answer briefly: what comes after Tuesday?",
            "b2_long": "Give me a detailed explanation of how rain forms, using several sentences.",
            "b2_stop": "Stop again. Say only: interruption received.",
            "final": "Final question: what temporary codeword did I ask you to remember earlier?",
        }
        return {k: await _pocket(v) for k, v in text.items()}

    async def start_server(self):
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                host="0.0.0.0",
                port=self.args.port,
                log_level="warning",
                proxy_headers=True,
                forwarded_allow_ips="*",
            )
        )
        self.server_task = asyncio.create_task(self.server.serve())
        for _ in range(200):
            if self.server.started:
                return
            if self.server_task.done():
                await self.server_task
            await asyncio.sleep(0.05)
        raise TimeoutError("temporary callback server did not start")

    async def verify_public_route(self):
        url = _public_url(
            self.public,
            f"/__shuo_phase5_human_sim/probe?token={self.token}",
        )
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(8.0),
                follow_redirects=True,
            ) as client:
                response = await client.get(url)
        except httpx.HTTPError as exc:
            raise RuntimeError(
                "PUBLIC_URL is not reaching the temporary local harness; "
                "start/point the public tunnel at this machine and port before a billable call"
            ) from exc
        if response.status_code != 204:
            raise RuntimeError(
                "PUBLIC_URL does not route to the temporary local harness "
                f"(probe status {response.status_code}); no call was placed"
            )

    async def wait_count(self, seq, count, timeout=5):
        async def wait():
            while len(seq) < count:
                self.probe.changed.clear()
                if len(seq) >= count:
                    break
                await self.probe.changed.wait()
            return seq[count - 1]

        return await asyncio.wait_for(wait(), timeout)

    async def start_bt(self):
        process_runner = AsyncioProcessRunner()
        discovery = PwDumpDiscovery(process_runner)
        deadline = time.monotonic() + self.args.answer_timeout
        while time.monotonic() < deadline:
            try:
                inner = await build_phase3_ai_only_session(
                    discovery=discovery,
                    runner=process_runner,
                    bluetooth_address=self.args.bluetooth_address,
                    config=PwCatConfig(latency=self.args.latency),
                    diagnostics=BluetoothDiagnostics(),
                )
                break
            except PipeWireSelectionError:
                await asyncio.sleep(0.25)
        else:
            raise RuntimeError(
                "HFP endpoints did not appear; answer itel manually and select Bluetooth"
            )

        observed = ObservedSession(inner, self.probe)
        deps = BluetoothProductionDeps(
            flux_cls=observed_flux(self.probe),
            agent_cls=observed_agent(self.probe),
        )
        self.bt_task = asyncio.create_task(
            run_production_bluetooth_conversation(
                observed,
                persona_id=self.args.persona,
                call_id=self.args.call_id,
                stream_id="phase5-human-sim",
                eot_threshold=0.8,
                diagnostics=BluetoothDiagnostics(),
                deps=deps,
            )
        )
        ready = asyncio.create_task(self.probe.reader_ready.wait())
        done, _ = await asyncio.wait(
            {ready, self.bt_task},
            timeout=15,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if ready in done and self.probe.reader_ready.is_set():
            return
        if self.bt_task.done():
            raise RuntimeError(
                f"Bluetooth runner stopped before ready: {self.bt_task.exception()}"
            )
        raise TimeoutError("Bluetooth reader did not become ready")

    async def normal(self, label, audio, expected=None):
        rs = len(self.remote.sot)
        rt = len(self.remote.turns)
        ls = len(self.probe.sot)
        le = len(self.probe.eot)
        agents = len(self.probe.agent)
        prompt = await self.remote.prompt(label, audio)
        await self.remote.wait_sot(rs + 1, self.args.response_timeout)
        turn = await self.remote.wait_turn(rt + 1, self.args.response_timeout)
        agent_turn = agents + 1 if len(self.probe.agent) > agents else None
        row = {
            "label": label,
            "prompt": prompt,
            "remote_sot": turn[0],
            "local_sot": self.probe.sot[ls] if len(self.probe.sot) > ls else None,
            "local_eot": self.probe.eot[le] if len(self.probe.eot) > le else None,
            "first_write": self.probe.first_write.get(agent_turn),
            "agent": agent_turn,
        }
        self.obs.append(row)
        self.private[label] = turn[2]
        if expected:
            ok = _has_any(turn[2], expected)
            self.check(
                f"semantic:{label}",
                ok,
                "deterministic transcript check",
                "PROVEN_REAL_CALL_TRANSCRIPT" if ok else "FAILED_REAL_CALL_TRANSCRIPT",
            )
        return turn

    async def thinking(self, a, b):
        before_eot = len(self.probe.eot)
        before_agent = len(self.probe.agent)
        before_remote = len(self.remote.sot)
        first, _ = await self.remote.send_audio(a)
        ack = await self.remote.checkpoint("thinking-a")
        t0 = time.monotonic_ns()
        await asyncio.sleep(self.args.thinking_pause)
        pause_ms = _ms(t0, time.monotonic_ns())
        premature = (
            len(self.probe.eot) > before_eot
            or len(self.probe.agent) > before_agent
            or len(self.remote.sot) > before_remote
        )
        if ack is None:
            self.checks.append(
                {
                    "name": "thinking_pause_no_premature_answer",
                    "passed": None,
                    "status": "NOT_MEASURED",
                    "note": "carrier checkpoint not acknowledged",
                }
            )
        else:
            self.check(
                "thinking_pause_no_premature_answer",
                not premature,
                f"pause_ms={pause_ms:.1f}",
            )

        rs = len(self.remote.sot)
        rt = len(self.remote.turns)
        _, last = await self.remote.send_audio(b)
        ack2 = await self.remote.checkpoint("thinking-b")
        await self.remote.wait_sot(rs + 1, self.args.response_timeout)
        turn = await self.remote.wait_turn(rt + 1, self.args.response_timeout)
        self.private["thinking"] = turn[2]
        ok = _has_any(turn[2], (("blue", "seven"), ("blue", "7")))
        self.check(
            "thinking_pause_continuity_answer",
            ok,
            "recall after deliberate pause",
            "PROVEN_REAL_CALL_TRANSCRIPT" if ok else "FAILED_REAL_CALL_TRANSCRIPT",
        )
        self.obs.append(
            {
                "label": "thinking",
                "prompt": {"first": first, "last": last, "checkpoint": ack2},
                "remote_sot": turn[0],
                "local_sot": self.probe.sot[-1] if self.probe.sot else None,
                "local_eot": self.probe.eot[-1] if self.probe.eot else None,
                "first_write": self.probe.first_write.get(self.probe.turn),
                "agent": self.probe.turn,
            }
        )

    async def barge(self, label, long_audio, interrupt, expected):
        rs = len(self.remote.sot)
        await self.remote.prompt(label + "-long", long_audio)
        old_sot = await self.remote.wait_sot(rs + 1, self.args.response_timeout)
        await asyncio.sleep(self.args.barge_delay)

        cancels = len(self.probe.cancel_end)
        clears = len(self.probe.clear)
        turns = len(self.remote.turns)
        sots = len(self.remote.sot)
        prompt = await self.remote.prompt(label + "-interrupt", interrupt)
        cancel_end = await self.wait_count(self.probe.cancel_end, cancels + 1)
        cancel_begin = (
            self.probe.cancel_begin[cancels]
            if len(self.probe.cancel_begin) > cancels
            else None
        )
        clear = (
            self.probe.clear[clears]
            if len(self.probe.clear) > clears
            else None
        )
        await self.remote.wait_sot(sots + 1, self.args.response_timeout)

        async def replacement():
            while True:
                for x in self.remote.turns[turns:]:
                    if x[0] > prompt["last"]:
                        return x
                self.remote.changed.clear()
                await self.remote.changed.wait()

        repl = await asyncio.wait_for(replacement(), self.args.response_timeout)
        self.private[label] = repl[2]
        ok = _has_any(repl[2], expected)
        self.check(
            label + "_cancel",
            cancel_end is not None,
            "interrupt reached live SHUO response",
        )
        self.check(label + "_playback_clear", clear is not None)
        self.check(
            "semantic:" + label,
            ok,
            "replacement response",
            "PROVEN_REAL_CALL_TRANSCRIPT" if ok else "FAILED_REAL_CALL_TRANSCRIPT",
        )
        self.barges.append(
            {
                "label": label,
                "old_remote_sot": old_sot,
                "interrupt": prompt,
                "cancel_begin": cancel_begin,
                "cancel_end": cancel_end,
                "clear": clear,
            }
        )

    async def scenario(self, a):
        await self.normal("arithmetic", a["arithmetic"], (("five",), ("5",)))
        await self.normal("code", a["code"], (("blue", "seven"), ("blue", "7")))
        await self.normal("sky", a["sky"])
        await self.thinking(a["think_a"], a["think_b"])
        await self.barge(
            "barge1",
            a["b1_long"],
            a["b1_stop"],
            (("blue", "seven"), ("blue", "7")),
        )
        await self.normal("weekday", a["weekday"], (("wednesday",),))
        await self.barge(
            "barge2",
            a["b2_long"],
            a["b2_stop"],
            (("interruption", "received"),),
        )
        await self.normal("final", a["final"], (("blue", "seven"), ("blue", "7")))

    def collect_metrics(self):
        def add(name, values, status, scope):
            vals = [x for x in values if x is not None]
            self.metrics.append(
                {
                    "name": name,
                    "samples_ms": vals,
                    "count": len(vals),
                    "median_ms": statistics.median(vals) if vals else None,
                    "p95_ms": (
                        sorted(vals)[
                            max(0, min(len(vals) - 1, round(0.95 * (len(vals) - 1))))
                        ]
                        if vals
                        else None
                    ),
                    "status": status,
                    "scope": scope,
                }
            )

        add(
            "caller_first_frame_send_to_local_flux_sot",
            [_ms(x["prompt"]["first"], x["local_sot"]) for x in self.obs],
            "PROVEN_REAL_INGRESS_WITH_STT",
            "Vobiz send -> cellular -> itel -> HFP downlink -> local Flux StartOfTurn",
        )
        add(
            "caller_last_frame_send_to_local_flux_eot",
            [_ms(x["prompt"]["last"], x["local_eot"]) for x in self.obs],
            "PROVEN_REAL_INGRESS_PLUS_EOT",
            "Vobiz caller final frame -> cellular/HFP -> local Flux EndOfTurn",
        )
        add(
            "caller_last_frame_send_to_returned_ai_sot",
            [_ms(x["prompt"]["last"], x["remote_sot"]) for x in self.obs],
            "PROVEN_REAL_ROUND_TRIP_WITH_REMOTE_STT",
            "remote caller stop -> real cellular/HFP/SHUO/providers/HFP/cellular -> remote Flux speech detection",
        )
        add(
            "local_bt_first_write_to_returned_ai_sot",
            [_ms(x["first_write"], x["remote_sot"]) for x in self.obs],
            "PROVEN_REAL_EGRESS_WITH_REMOTE_STT",
            "local HFP first write -> itel/cellular/carrier -> remote Flux speech detection; not isolated Bluetooth latency",
        )
        add(
            "agent_cancel_duration",
            [_ms(x["cancel_begin"], x["cancel_end"]) for x in self.barges],
            "PROVEN_LOCAL_MONOTONIC",
            "live SHUO Agent.cancel_turn",
        )
        add(
            "interrupt_last_frame_send_to_cancel_return",
            [_ms(x["interrupt"]["last"], x["cancel_end"]) for x in self.barges],
            "PROVEN_REAL_INGRESS_CORRELATION",
            "remote interrupt final frame -> cellular/HFP/Flux -> SHUO cancel return",
        )
        add(
            "isolated_bluetooth_one_way_latency",
            [],
            "NOT_MEASURED",
            "no HFP/handset acknowledgement isolates Bluetooth from phone/carrier buffering",
        )
        add(
            "physical_human_caller_heard_first_audio",
            [],
            "NOT_MEASURED",
            "digital carrier return is not a human-ear/handset-speaker measurement",
        )

    async def cleanup_check(self):
        p = await asyncio.create_subprocess_exec(
            "pgrep",
            "-a",
            "pw-cat",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await p.communicate()
        clean = p.returncode != 0 or not out.strip()
        self.check(
            "bounded_pw_cat_cleanup",
            clean,
            "no pw-cat remains" if clean else "pw-cat process remains",
            "PROVEN_LOCAL_PROCESS" if clean else "FAILED_LOCAL_PROCESS",
        )

    async def run(self):
        self.preflight()
        audio = await self.synthesize()
        await self.start_server()
        try:
            await self.verify_public_route()
            answer = _public_url(
                self.public,
                f"/__shuo_phase5_human_sim/answer?token={self.token}",
            )
            hangup = _public_url(
                self.public,
                f"/__shuo_phase5_human_sim/hangup?token={self.token}",
            )
            await self.carrier.originate(
                self.args.phone,
                answer_url=answer,
                persona_id="phase5-human-sim",
                record=False,
                hangup_url=hangup,
            )
            print(
                "REAL CALL PLACED: answer the itel P40+ manually and select Bluetooth audio. "
                "Do not speak; dialogue is automated.",
                flush=True,
            )
            await asyncio.wait_for(
                self.remote.stream_started.wait(),
                self.args.answer_timeout,
            )
            self.live_start = time.monotonic_ns()
            await self.start_bt()
            self.check("carrier_media_stream_started", True)
            self.check("bluetooth_hfp_targets_selected", True)
            self.check("bluetooth_reader_ready", True)

            remaining = self.args.duration - (
                time.monotonic_ns() - self.live_start
            ) / 1e9
            await asyncio.wait_for(self.scenario(audio), remaining)
            self.check(
                "target_ten_turns",
                len(self.probe.agent) >= 10,
                f"agent_starts={len(self.probe.agent)} target>=10",
            )
            self.check(
                "two_genuine_barge_ins",
                len(self.barges) >= 2
                and all(x["cancel_end"] for x in self.barges[:2]),
                "interrupts sent only after AI speech reached remote carrier side",
            )
            print(
                "AUTOMATED DIALOGUE COMPLETE: hang up the itel handset MANUALLY now.",
                flush=True,
            )
            wait = min(
                self.args.hangup_wait,
                max(
                    0,
                    self.args.duration
                    - (time.monotonic_ns() - self.live_start) / 1e9,
                ),
            )
            manual = False
            if wait:
                try:
                    await asyncio.wait_for(self.remote.call_ended.wait(), wait)
                    manual = True
                except asyncio.TimeoutError:
                    pass
            self.check(
                "manual_hangup_observed",
                manual,
                "no carrier/D-Bus hangup command is issued",
            )

            natural = False
            note = "manual hangup not observed"
            if manual and self.bt_task:
                try:
                    await asyncio.wait_for(asyncio.shield(self.bt_task), 5)
                    natural = True
                    note = "runner returned after handset hangup"
                except asyncio.TimeoutError:
                    note = (
                        "runner did not return within 5s; forced bounded teardown follows"
                    )
                except Exception as exc:
                    note = f"runner ended with {type(exc).__name__}: {exc}"
            self.check("runner_exited_after_manual_hangup", natural, note)
            self.check("scenario_completed", True)
        except Exception as exc:
            self.error = f"{type(exc).__name__}: {exc}"
            self.check("scenario_completed", False, self.error, "FAILED")
        finally:
            if self.bt_task and not self.bt_task.done():
                self.bt_task.cancel()
                try:
                    await self.bt_task
                except (asyncio.CancelledError, Exception):
                    pass
            await self.remote.stop()
            if self.server:
                self.server.should_exit = True
            if self.server_task:
                try:
                    await asyncio.wait_for(self.server_task, 10)
                except asyncio.TimeoutError:
                    self.server_task.cancel()

        await self.cleanup_check()
        self.collect_metrics()
        return self.report()

    def report(self):
        duration = (
            _ms(self.live_start, time.monotonic_ns()) / 1000
            if self.live_start
            else None
        )
        return {
            "schema": "shuo.phase5-cellular-human-sim/v1",
            "mode": "real_cellular_bluetooth_synthetic_human",
            "checks": self.checks,
            "metrics": self.metrics,
            "metadata": {
                "carrier": self.carrier.name,
                "bluetooth_address": self.args.bluetooth_address,
                "pw_cat_latency": self.args.latency,
                "eot_threshold": 0.8,
                "tts_provider": "pocket",
                "live_duration_seconds": duration,
                "max_live_seconds": self.args.duration,
                "raw_audio_recorded": False,
                "automated_handset_answer": False,
                "automated_handset_hangup": False,
                "automated_carrier_hangup": False,
            },
            "limitations": [
                "Synthetic Pocket caller is deterministic human-like stimulus, not a biological human or subjective naturalness test.",
                "Real path includes carrier/cellular and HFP Bluetooth, but current interfaces cannot isolate Bluetooth one-way latency from handset/carrier buffering.",
                "Returned carrier media is digital end-to-end evidence, not a human-ear/handset-speaker latency or intelligibility measurement.",
                "Handset answer/hangup remain manual; no D-Bus call control or carrier.hangup is used.",
                "No raw audio is persisted. Private response transcripts stay local only.",
                "A pass is supplemental evidence; it does not itself accept Phase 5 or authorize Phase 6.",
            ],
            "raw": {
                "observations": self.obs,
                "barges": self.barges,
                "private_responses": self.private,
                "scenario_error": self.error,
            },
        }


def _sanitise(report):
    out = json.loads(json.dumps(report))
    out["raw"].pop("private_responses", None)
    return out


def parser():
    p = argparse.ArgumentParser(
        description=(
            "Phase 5 real cellular+Bluetooth synthetic-human supplement; "
            "handset answer/hangup stay manual"
        )
    )
    p.add_argument("--phone", required=True, type=validate_phone)
    p.add_argument("--bluetooth-address", required=True)
    p.add_argument("--latency", required=True)
    p.add_argument("--eot-threshold", type=float, default=0.8)
    p.add_argument("--persona", default="default")
    p.add_argument("--call-id", default="phase5-cellular-human-sim")
    p.add_argument("--port", type=int, default=3040)
    p.add_argument(
        "--duration-seconds",
        dest="duration",
        type=validate_duration,
        default=300.0,
    )
    p.add_argument("--answer-timeout", type=float, default=45)
    p.add_argument("--response-timeout", type=float, default=20)
    p.add_argument("--hangup-wait", type=float, default=30)
    p.add_argument("--thinking-pause", type=float, default=1.0)
    p.add_argument("--barge-delay", type=float, default=0.7)
    p.add_argument("--allow-real-call", action="store_true")
    p.add_argument(
        "--json-out",
        type=Path,
        default=DEFAULT_OUT_DIR / "phase5-human-sim.json",
    )
    p.add_argument(
        "--private-json-out",
        type=Path,
        default=DEFAULT_OUT_DIR / "phase5-human-sim-private.json",
    )
    return p


async def _run(args):
    if abs(args.eot_threshold - 0.8) > 1e-9 or args.latency != "120ms":
        raise RuntimeError("reference run requires EOT 0.8 and latency 120ms")
    if not 0.5 <= args.thinking_pause <= 2:
        raise RuntimeError("thinking pause must be 0.5..2.0s")
    if not 0.2 <= args.barge_delay <= 2:
        raise RuntimeError("barge delay must be 0.2..2.0s")
    return await Runner(args).run()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = parser().parse_args()
    try:
        report = asyncio.run(_run(args))
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}")
        return 2

    args.private_json_out.parent.mkdir(parents=True, exist_ok=True)
    args.private_json_out.write_text(json.dumps(report, indent=2) + "\n")
    clean = _sanitise(report)
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(clean, indent=2) + "\n")

    print("\nchecks")
    for x in clean["checks"]:
        state = (
            "PASS"
            if x["passed"] is True
            else "FAIL"
            if x["passed"] is False
            else x["status"]
        )
        print(
            f"  {state:<18} {x['name']}"
            + (f" — {x['note']}" if x["note"] else "")
        )

    print("\nmetrics")
    for x in clean["metrics"]:
        if x["count"]:
            print(
                f"  {x['name']}: n={x['count']} "
                f"median={x['median_ms']:.1f}ms [{x['status']}]"
            )
        else:
            print(f"  {x['name']}: {x['status']}")

    print(
        f"\nsanitized json: {args.json_out}\n"
        f"private local json: {args.private_json_out}"
    )
    return 1 if any(x["passed"] is False for x in report["checks"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
