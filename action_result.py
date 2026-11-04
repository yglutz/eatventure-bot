"""Input delivery outcomes, never confirmation of a game action or purchase.

DENIED: refused before button-down. CANCELLED: cancelled before button-down.
SENT: requested input completed (or a held-context press/check is valid).
INTERRUPTED: button-down occurred, but a later guard failed; partial effects
are possible. Only SENT is truthy. Callers must invalidate observations for
every non-DENIED outcome, including CANCELLED, and for input exceptions.
"""
from enum import Enum


class ActionResult(Enum):
    DENIED = "denied"
    SENT = "sent"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"

    def __bool__(self):
        return self is ActionResult.SENT
