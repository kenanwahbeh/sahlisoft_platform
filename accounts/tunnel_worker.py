"""The one place this box talks to the tunnel-provisioner Cloudflare Worker.

The real Cloudflare API token (Tunnel:Edit) never touches this box. It lives as
a Worker secret at TUNNEL_WORKER_URL, which this box reaches with a narrow,
single-purpose shared secret (TUNNEL_WORKER_TOKEN) that can only ever ask
"make or reuse a tunnel for this shop" or "tear that one down" -- nothing
account-wide. If the VPS is ever compromised, the worst this credential yields
is churn on tunnels; it cannot touch DNS, zones, other Workers, or billing.

The Worker's source is checked in at cloudflare/tunnel-provisioner/worker.js.
It is deployed to Cloudflare, not from this repo -- keep the two in step by
hand.
"""

import json
import urllib.error
import urllib.request
from os import environ

WORKER_URL = environ.get("TUNNEL_WORKER_URL", "")
WORKER_TOKEN = environ.get("TUNNEL_WORKER_TOKEN", "")

TIMEOUT = 15


class NotConfigured(Exception):
    """No Worker credentials on this box -- answer 503 rather than guessing."""


class WorkerError(Exception):
    """The Worker refused or could not complete the request.

    `retryable` distinguishes "try again in a moment" (a tunnel whose connector
    has not finished dropping) from a hard failure. Callers must not treat a
    retryable failure as permission to proceed as if the tunnel were gone.
    """

    def __init__(self, message, retryable=False):
        super().__init__(message)
        self.retryable = retryable


def is_configured() -> bool:
    return bool(WORKER_URL and WORKER_TOKEN)


def call(payload: dict) -> dict:
    """POST to the Worker and decode its JSON reply."""
    if not is_configured():
        raise NotConfigured("tunnel provisioning is not configured on the platform yet")

    request = urllib.request.Request(
        WORKER_URL,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {WORKER_TOKEN}",
            "Content-Type": "application/json",
            # Without an explicit UA, urllib sends "Python-urllib/x.y", which
            # Cloudflare Bot Fight Mode blocks with a 1010 on any zone-hosted
            # route -- including this platform's own Worker.
            "User-Agent": "SahlisoftPlatform-tunnel-client/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {}
        raise WorkerError(
            f"tunnel worker failed ({exc.code}): {body[:300]}",
            retryable=bool(parsed.get("retryable")),
        ) from exc
    except (urllib.error.URLError, ValueError) as exc:
        # The Worker being unreachable is transient by nature -- a delete that
        # fails this way must leave the enrollment intact for another attempt.
        raise WorkerError(f"tunnel worker unreachable: {exc}", retryable=True) from exc


def ensure_tunnel(*, tunnel_id: str = "", tenant_slug: str = "", enrollment_id: str = "") -> dict:
    """Create this shop's tunnel, or hand back the existing one's token."""
    payload = (
        {"tunnel_id": tunnel_id}
        if tunnel_id
        else {"tenant_slug": tenant_slug, "enrollment_id": str(enrollment_id)}
    )
    result = call(payload)
    if not result.get("tunnel_token"):
        raise WorkerError("tunnel worker returned no token")
    return result


def delete_tunnel(tunnel_id: str) -> dict:
    """Tear a tunnel down. Idempotent: an already-gone tunnel is a success.

    Raises WorkerError (possibly retryable) if the tunnel is still standing.
    """
    result = call({"action": "delete", "tunnel_id": tunnel_id})
    if not result.get("deleted"):
        raise WorkerError(
            f"tunnel delete failed: {str(result.get('detail'))[:300]}",
            retryable=bool(result.get("retryable")),
        )
    return result
