"""
Carrier package -- telephony providers behind one interface.

    from shuo.carrier import get_carrier
    carrier = get_carrier()          # honours CARRIER=vobiz|twilio

Vobiz is the production target (AWS ap-south-1, ~1-5ms to our EC2 box).
Twilio is retained as a working regression path: if CARRIER=twilio still
places a real call, the abstraction has not quietly grown Vobiz-shaped
assumptions.
"""

from __future__ import annotations

from typing import Dict, Optional

from ..config import carrier_name
from ..log import get_logger
from .base import Carrier, CarrierSession, OriginateResult

logger = get_logger("shuo.carrier")

_CACHE: Dict[str, Carrier] = {}


def get_carrier(name: Optional[str] = None) -> Carrier:
    """
    Return the configured carrier, constructed once per name.

    Carriers read credentials from the environment at construction, so
    the cache also means we do not re-read env on every call.
    """
    key = (name or carrier_name()).strip().lower()

    if key in _CACHE:
        return _CACHE[key]

    if key == "vobiz":
        from .vobiz import VobizCarrier
        carrier: Carrier = VobizCarrier()
    elif key == "twilio":
        from .twilio import TwilioCarrier
        carrier = TwilioCarrier()
    else:
        raise ValueError(
            f"Unknown CARRIER={key!r}. Supported: 'vobiz', 'twilio'."
        )

    _CACHE[key] = carrier
    logger.debug(f"Carrier initialised: {key}")
    return carrier


def reset_carrier_cache() -> None:
    """Drop cached carriers. Used by tests that patch the environment."""
    _CACHE.clear()


__all__ = [
    "Carrier",
    "CarrierSession",
    "OriginateResult",
    "get_carrier",
    "reset_carrier_cache",
]
