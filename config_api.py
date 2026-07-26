#!/usr/bin/env python3
"""
shuo - configuration API entrypoint

Usage:
    python config_api.py                 # 127.0.0.1:3041

Runs the operator configuration REST API. **A separate process from
`main.py`, by design** -- `main.py` serves live calls on a 20ms player
deadline, and this serves an HTTP form that writes to disk. They share
nothing but the config file, so a save can never stall a call in progress.

Run both, in two terminals:

    python main.py            # call server        :3040
    python config_api.py      # configuration API  :3041

Environment:
    CONFIG_API_HOST           default 127.0.0.1 (loopback -- see below)
    CONFIG_API_PORT           default 3041
    SHUO_CONFIG_PATH          default <repo>/var/agent_config.json
    SHUO_CONFIG_API_TOKEN     required only to bind off-loopback

Binding anywhere other than loopback without a token is refused at startup,
not warned about: this API decides what the twin says on a live call.
"""

import os
import sys

import uvicorn
from dotenv import load_dotenv

from shuo.config_api import (
    app,
    config_api_token,
    enforce_exposure_policy,
)
from shuo.config_store import default_config_path
from shuo.log import setup_logging, get_logger

load_dotenv()
setup_logging()
logger = get_logger("shuo.config_api")

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3041


def main() -> None:
    host = os.getenv("CONFIG_API_HOST", DEFAULT_HOST).strip() or DEFAULT_HOST
    port = int(os.getenv("CONFIG_API_PORT", str(DEFAULT_PORT)))

    try:
        enforce_exposure_policy(host)
    except RuntimeError as exc:
        logger.error(str(exc))
        sys.exit(1)

    path = default_config_path()
    logger.info(f"Config API on http://{host}:{port}")
    logger.info(f"Storing configuration at {path}")
    logger.info(
        "Auth: token required"
        if config_api_token()
        else "Auth: none (loopback-only — set SHUO_CONFIG_API_TOKEN to require one)"
    )
    logger.info(
        "Point the panel at this with SHUO_API_URL=http://%s:%d in "
        "shuo-frontend/.env.local" % (host, port)
    )

    uvicorn.run(
        app,
        host=host,
        port=port,
        # Quiet, like main.py -- shuo's own logging carries the detail.
        log_level="warning",
    )


if __name__ == "__main__":
    main()
