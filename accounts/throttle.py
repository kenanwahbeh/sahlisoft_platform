"""A small failure counter for the one endpoint that has no bearer token.

Every other agent route authenticates with a credential the platform already
knows about, so an attacker with a wrong token gets a 401 and learns nothing.
``/api/agent/activate/`` is different: the code *is* the credential, it arrives
from a machine that has never been seen before, and there is no session to
attach a limit to.

To be clear about what this does and does not buy: an activation code is 32
bytes from ``secrets``, so this is not what stops the code from being guessed --
nothing could guess it in the thirty minutes it lives. What this stops is the
endpoint being a free amplifier: an unauthenticated route that runs a database
lookup per request is worth rate-limiting on its own merits, and if the entropy
ever gets shortened for some product reason, the limit is already here rather
than being remembered at the wrong moment.

Counters live in the cache, which settings.py deliberately backs with files so
every gunicorn worker shares one tally (see the comment there). The cache is
allowed to be lossy: a wiped cache means an attacker gets their attempts back,
which is a fair trade against ever locking out a real shop because a counter
could not be written.
"""

from django.core.cache import cache

#: Failures from one source address before it is turned away, and for how long.
#: Generous enough that a shop retrying a genuinely expired code is never
#: caught by it, small enough that scripted probing stops being free.
MAX_FAILURES = 10
WINDOW_SECONDS = 15 * 60


def client_ip(request) -> str:
    """The address to count against.

    X-Forwarded-For's *first* entry, because the request reaches Django through
    Apache and a cloudflared connector -- REMOTE_ADDR is one of those, identical
    for every shop in the country, and counting against it would let one
    misbehaving client lock out all of them.
    """
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if forwarded:
        return forwarded.split(",")[0].strip()[:45]
    return request.META.get("REMOTE_ADDR", "") or "unknown"


def _key(scope: str, identifier: str) -> str:
    return f"throttle:{scope}:{identifier}"


def is_blocked(scope: str, identifier: str) -> bool:
    return (cache.get(_key(scope, identifier)) or 0) >= MAX_FAILURES


def record_failure(scope: str, identifier: str) -> int:
    """Count one failure and return the running total.

    ``add`` then ``incr`` rather than a read-modify-write: two workers failing
    at once should count twice, and incr is the only operation the cache API
    offers that is atomic. The window is set once, when the key is created, so
    it is a fixed window from the first failure rather than one an attacker can
    keep pushing forward by failing again.
    """
    key = _key(scope, identifier)
    cache.add(key, 0, WINDOW_SECONDS)
    try:
        return cache.incr(key)
    except ValueError:
        # The key expired between add and incr. Nothing to do but let this one
        # through -- the next failure starts a fresh window.
        return 0


def clear(scope: str, identifier: str) -> None:
    """Forget this source's failures. Called on success."""
    cache.delete(_key(scope, identifier))
