"""payloadwatch - check the payload, not the status flag.

A service can return 200 with a well-formed body and still be serving data that
stopped updating hours ago. payloadwatch asserts on the contents, tries the
cheap fix before waking anyone, caps how often it shouts, and stays quiet when
the market is closed.
"""

from .checks import (
    Assertion,
    Verdict,
    changed,
    describe,
    dig,
    fresh,
    matches,
    min_length,
    non_zero,
    truthy,
    within,
)
from .notify import Alert, Console, Fanout, Notifier, Ntfy, Webhook
from .quiet import QuietWindows, Window, market_weekend
from .state import CheckState, Store
from .watchdog import Check, Outcome, Watchdog

__version__ = "0.1.0"

__all__ = [
    "Alert", "Assertion", "Check", "CheckState", "Console", "Fanout", "Notifier",
    "Ntfy", "Outcome", "QuietWindows", "Store", "Verdict", "Watchdog", "Webhook",
    "Window", "changed", "describe", "dig", "fresh", "market_weekend", "matches",
    "min_length", "non_zero", "truthy", "within", "__version__",
]
