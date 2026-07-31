"""HTTP contract for the on-premise Windows agent.

Every other view in this app renders Arabic for a human sitting at a browser.
These three do not -- the caller is an unattended process on a shop's POS
machine with no session and no CSRF token to send, so responses here are
deliberately English/JSON, and every view is @csrf_exempt on purpose. Do not
"fix" either of those to match the rest of the codebase.

Route -> contract (exact, agreed with the separate Windows-agent codebase):
    POST /api/agent/register/    bind this token to one machine
    POST /api/agent/heartbeat/   check in, learn current status
    POST /api/agent/tunnel/      fetch a Cloudflare Tunnel connector token
"""

import json
import urllib.error
import urllib.request
from os import environ

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from .agent_auth import authenticate_agent, parse_json_body
from .models import AgentCommand, AgentEnrollment
from .views import tenant_host

# The real Cloudflare API token (Tunnel:Edit) never touches this box. It lives
# as a Worker secret at TUNNEL_WORKER_URL, which this box reaches with a
# narrow, single-purpose shared secret (TUNNEL_WORKER_TOKEN) that can only ever
# ask "make or reuse a tunnel for this shop" -- nothing account-wide. If the
# VPS is ever compromised, the worst this credential yields is more tunnels;
# it cannot touch DNS, zones, other Workers, or billing.
TUNNEL_WORKER_URL = environ.get("TUNNEL_WORKER_URL", "")
TUNNEL_WORKER_TOKEN = environ.get("TUNNEL_WORKER_TOKEN", "")


def _str_field(body, name, max_length):
    return str(body.get(name) or "").strip()[:max_length]


@csrf_exempt
@require_POST
def agent_register(request):
    """First contact from a freshly enrolled agent, and every restart after.

    The anti-reuse guard is the whole point: a token is meant for one machine,
    so once ``hostname`` has been set, a *different* hostname showing up with
    the same token means the token leaked or was copied, not that the shop
    reinstalled -- hence 409 rather than silently re-binding.
    """
    enrollment, error = authenticate_agent(request)
    if error:
        return error

    body = parse_json_body(request)
    if not isinstance(body, dict):
        return JsonResponse({"error": "malformed JSON body"}, status=400)

    hostname = _str_field(body, "hostname", 255)

    if not enrollment.hostname:
        enrollment.hostname = hostname
        enrollment.os_name = _str_field(body, "os", 100)
        enrollment.agent_version = _str_field(body, "agent_version", 30)
        enrollment.db_folder = _str_field(body, "db_folder", 500)
        enrollment.registered_at = timezone.now()
        enrollment.save(
            update_fields=[
                "hostname",
                "os_name",
                "agent_version",
                "db_folder",
                "registered_at",
            ]
        )
    elif enrollment.hostname != hostname:
        return JsonResponse(
            {"error": "this enrollment is already bound to a different hostname"},
            status=409,
        )

    enrollment.touch()

    return JsonResponse(
        {
            "agent_id": enrollment.pk,
            "tenant_slug": enrollment.tenant.slug,
            "tenant_host": tenant_host(enrollment.tenant),
            "status": enrollment.status,
        }
    )


def _ingest_results(enrollment, results):
    """Absorb the agent's report of commands it already ran.

    Silently skips anything that does not point at a real, still-open
    command on *this* enrollment -- a stale or forged command_id must not
    raise, since the agent has no way to fix a heartbeat body and retrying
    forever would just spin.
    """
    if not isinstance(results, list):
        return
    open_ids = {
        item.get("command_id")
        for item in results
        if isinstance(item, dict) and item.get("command_id") is not None
    }
    if not open_ids:
        return
    commands = {
        c.pk: c
        for c in enrollment.commands.filter(
            pk__in=open_ids, status=AgentCommand.Status.SENT
        )
    }
    for item in results:
        if not isinstance(item, dict):
            continue
        command = commands.get(item.get("command_id"))
        if command is not None:
            command.record_result(item)


@csrf_exempt
@require_POST
def agent_heartbeat(request):
    """Check in, deliver results for commands already sent, collect new ones.

    Commands are only ever handed out while the enrollment is ``active`` --
    worker.py already discards them otherwise, but the platform must not
    rely on the client to enforce that.
    """
    enrollment, error = authenticate_agent(request)
    if error:
        return error

    body = parse_json_body(request)
    if isinstance(body, dict):
        _ingest_results(enrollment, body.get("results"))

    enrollment.touch()

    commands = []
    if enrollment.status == AgentEnrollment.Status.ACTIVE:
        queued = list(
            enrollment.commands.filter(status=AgentCommand.Status.QUEUED)[:20]
        )
        for command in queued:
            command.mark_sent()
        AgentCommand.objects.bulk_update(queued, ["status", "sent_at"])
        commands = [c.as_payload() for c in queued]

    return JsonResponse({"status": enrollment.status, "commands": commands})


@csrf_exempt
@require_POST
def agent_tunnel(request):
    """Provision (or reuse) a Cloudflare Tunnel and hand back its connector token.

    Delegates the actual Cloudflare API call to a small Worker
    (tunnel-provisioner) that holds the powerful, account-scoped Cloudflare
    token as its own secret -- this view only ever holds a narrow shared
    secret good for nothing but "ask that Worker for a tunnel". Requires
    TUNNEL_WORKER_URL and TUNNEL_WORKER_TOKEN in the environment; until they
    are set, this answers 503 rather than guessing, so the contract is
    stable and testable end to end the moment they show up.
    """
    enrollment, error = authenticate_agent(request)
    if error:
        return error

    if enrollment.status == AgentEnrollment.Status.PENDING:
        return JsonResponse(
            {"error": "enrollment is still pending approval"}, status=409
        )

    if not TUNNEL_WORKER_URL or not TUNNEL_WORKER_TOKEN:
        return JsonResponse(
            {"error": "tunnel provisioning is not configured on the platform yet"},
            status=503,
        )

    payload = {"tunnel_id": enrollment.cf_tunnel_id} if enrollment.cf_tunnel_id else {
        "tenant_slug": enrollment.tenant.slug,
        "enrollment_id": str(enrollment.pk),
    }
    try:
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            TUNNEL_WORKER_URL,
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {TUNNEL_WORKER_TOKEN}",
                "Content-Type": "application/json",
                # Without an explicit UA, urllib sends "Python-urllib/x.y",
                # which Cloudflare Bot Fight Mode blocks with a 1010 on any
                # zone-hosted route -- including this platform's own Worker.
                "User-Agent": "SahlisoftPlatform-tunnel-client/1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        return JsonResponse(
            {"error": f"tunnel provisioning failed ({exc.code}): {detail}"},
            status=502,
        )
    except (urllib.error.URLError, ValueError) as exc:
        return JsonResponse(
            {"error": f"tunnel provisioning failed: {exc}"}, status=502
        )

    if not result.get("tunnel_token"):
        return JsonResponse(
            {"error": "tunnel worker returned no token"}, status=502
        )

    if not enrollment.cf_tunnel_id and result.get("tunnel_id"):
        enrollment.cf_tunnel_id = result["tunnel_id"]
        enrollment.save(update_fields=["cf_tunnel_id"])

    return JsonResponse(
        {
            "tunnel_token": result["tunnel_token"],
            # No public hostname is configured for this tunnel -- it is a bare
            # connector with no ingress rule. A made-up domain-shaped string
            # here previously made the agent display it to the shop owner as
            # "your shop's address" (main.py cmd_status), which was a lie.
            # Empty is honest; the agent already skips printing a blank one.
            "hostname": "",
        }
    )
