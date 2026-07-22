#!/usr/bin/env python3
"""
shuo - Voice Agent Framework

Usage:
    python main.py                              # server-only (inbound calls)
    python main.py +919876543210                # outbound call
    python main.py +919876543210 candidate      # outbound with an explicit persona

Server-only mode starts the server and waits for inbound calls.
Outbound mode additionally initiates a call to the specified number.
"""

import os
import sys
import signal
import asyncio
import threading
import time

import uvicorn
from dotenv import load_dotenv

from shuo import config
from shuo.server import app
from shuo.carrier import get_carrier
from shuo.log import setup_logging, Logger, get_logger
import shuo.server as server_module

# Load environment variables
load_dotenv()

# Setup logging
setup_logging()
logger = get_logger("shuo")


# Credentials each carrier needs before it can place a call.
CARRIER_REQUIRED_VARS = {
    "vobiz": ["VOBIZ_AUTH_ID", "VOBIZ_AUTH_TOKEN", "VOBIZ_PHONE_NUMBER"],
    "twilio": ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"],
}

# Needed regardless of carrier.
PIPELINE_REQUIRED_VARS = [
    "DEEPGRAM_API_KEY",
    "GROQ_API_KEY",
    "ELEVENLABS_API_KEY",
]


def check_environment() -> bool:
    """Check that the variables this carrier and pipeline need are set."""
    carrier = config.carrier_name()

    if carrier not in CARRIER_REQUIRED_VARS:
        logger.error(
            f"Unknown CARRIER={carrier!r}. Supported: "
            f"{', '.join(sorted(CARRIER_REQUIRED_VARS))}"
        )
        return False

    required = CARRIER_REQUIRED_VARS[carrier] + PIPELINE_REQUIRED_VARS
    missing = [var for var in required if not os.getenv(var)]

    if not config.public_url():
        missing.append("PUBLIC_URL")

    if missing:
        logger.error(f"Missing environment variables: {', '.join(missing)}")
        logger.error("Copy .env.example to .env and fill it in.")
        return False

    return True


# Max time (seconds) to wait for active calls to finish before forced exit.
DRAIN_TIMEOUT = int(os.getenv("DRAIN_TIMEOUT", "300"))

_uvicorn_server: uvicorn.Server = None


def start_server(port: int) -> None:
    """Start the FastAPI server."""
    global _uvicorn_server
    config_ = uvicorn.Config(
        app,
        host="0.0.0.0",
        port=port,
        log_level="warning",  # Quiet uvicorn, we have our own logging
        # Behind ngrok / an ALB, trust X-Forwarded-Proto so webhook
        # signature validation sees the public https URL the carrier signed.
        proxy_headers=True,
        forwarded_allow_ips=os.getenv("FORWARDED_ALLOW_IPS", "*"),
    )
    _uvicorn_server = uvicorn.Server(config_)
    _uvicorn_server.run()


def main():
    """Main entry point."""
    phone_number = None
    persona = None

    if len(sys.argv) >= 2:
        phone_number = sys.argv[1]
        if not phone_number.startswith("+"):
            print("Error: Phone number must start with + (E.164 format)")
            sys.exit(1)
    if len(sys.argv) >= 3:
        persona = sys.argv[2]

    if not check_environment():
        sys.exit(1)

    port = int(os.getenv("PORT", "3040"))
    public_url = config.public_url()
    carrier = get_carrier()
    persona_id = persona or config.default_persona()

    logger.info(
        f"carrier={carrier.name}  persona={persona_id}  "
        f"recording={'dual-channel' if config.record_calls() else 'off'}"
    )

    # Start server in background thread
    Logger.server_starting(port)
    server_thread = threading.Thread(target=start_server, args=(port,), daemon=True)
    server_thread.start()

    # Wait for server to start
    time.sleep(2)
    Logger.server_ready(public_url)

    # ── Graceful shutdown on SIGTERM ────────────────────────────────
    def _handle_sigterm(signum, frame):
        """
        Railway (and Docker) send SIGTERM before killing the container.
        We stop accepting new calls and wait for active ones to finish.
        """
        logger.info("SIGTERM received — starting graceful drain")
        server_module._draining = True

        if server_module._active_calls <= 0:
            logger.info("No active calls — shutting down now")
            if _uvicorn_server:
                _uvicorn_server.should_exit = True
            return

        logger.info(
            f"Waiting up to {DRAIN_TIMEOUT}s for {server_module._active_calls} "
            f"active call(s) to finish..."
        )

        deadline = time.monotonic() + DRAIN_TIMEOUT
        while server_module._active_calls > 0 and time.monotonic() < deadline:
            time.sleep(1)

        remaining = server_module._active_calls
        if remaining > 0:
            logger.warning(f"Drain timeout — {remaining} call(s) still active, forcing exit")
        else:
            logger.info("All calls drained — shutting down cleanly")

        if _uvicorn_server:
            _uvicorn_server.should_exit = True

    signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        if phone_number:
            Logger.call_initiating(phone_number)
            result = asyncio.run(
                carrier.originate(
                    phone_number,
                    answer_url=config.answer_url(
                        persona_id=persona_id, direction="outbound"
                    ),
                    persona_id=persona_id,
                    record=config.record_calls(),
                )
            )
            Logger.call_initiated(result.call_id)
            logger.info("Waiting for call to connect... (Ctrl+C to end)")
        else:
            logger.info("Server-only mode — waiting for inbound calls (Ctrl+C to end)")

        # Exit once the server has been told to stop, rather than looping
        # forever after a SIGTERM drain completes.
        while not (_uvicorn_server and _uvicorn_server.should_exit):
            time.sleep(1)

        logger.info("Server shutting down — waiting for the HTTP thread")
        server_thread.join(timeout=30)

    except KeyboardInterrupt:
        Logger.shutdown()
    except Exception as e:
        logger.error(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
