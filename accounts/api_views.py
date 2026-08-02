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

from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import throttle, tunnel_worker
from .agent_auth import authenticate_agent, parse_json_body
from .models import AgentActivationCode, AgentCommand, AgentEnrollment
from .views import platform_base_url, tenant_host

#: Activation is throttled per source address; nothing else here is, because
#: nothing else here is reachable without a credential we issued.
THROTTLE_SCOPE = "agent-activate"


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
def agent_activate(request):
    """Trade a one-shot activation code for this enrollment's real token.

    The only agent route with no ``Authorization`` header, because the caller
    is a machine that has just been installed and has nothing to authenticate
    with yet -- the code in its pairing file is the credential, and this is
    where it stops being one. See accounts/models.py::AgentActivationCode for
    why the download carries a coupon instead of the token itself.

    Nothing about approval changes here. The enrollment stays ``pending`` until
    a human clicks approve in the dashboard, exactly as it does for an owner
    who set up the .env by hand: activation hands over a credential, not
    permission to use it.
    """
    ip = throttle.client_ip(request)
    if throttle.is_blocked(THROTTLE_SCOPE, ip):
        return JsonResponse(
            {"error": "too many failed activation attempts -- try again later"},
            status=429,
        )

    body = parse_json_body(request)
    if not isinstance(body, dict):
        return JsonResponse({"error": "malformed JSON body"}, status=400)

    raw = str(body.get("activation_code") or "").strip()
    code = AgentActivationCode.resolve(raw)

    # One message for "no such code", "expired" and "already used" alike. The
    # agent cannot act differently on any of them -- every one of them means
    # "download a fresh pairing file" -- and distinguishing them would confirm
    # to a prober that a code once existed.
    if code is None or not code.is_redeemable:
        throttle.record_failure(THROTTLE_SCOPE, ip)
        return JsonResponse(
            {"error": "activation code is unknown, expired, or already used"},
            status=403,
        )

    if code.enrollment.status == AgentEnrollment.Status.REVOKED:
        throttle.record_failure(THROTTLE_SCOPE, ip)
        return JsonResponse({"error": "enrollment has been revoked"}, status=403)

    token = code.consume(ip=ip)
    if token is None:
        # Claimed by a concurrent request between resolve and consume.
        throttle.record_failure(THROTTLE_SCOPE, ip)
        return JsonResponse(
            {"error": "activation code is unknown, expired, or already used"},
            status=403,
        )

    throttle.clear(THROTTLE_SCOPE, ip)
    enrollment = code.enrollment
    return JsonResponse(
        {
            "platform_token": token,
            "platform_url": platform_base_url(),
            "agent_id": enrollment.pk,
            "agent_label": enrollment.label,
            "tenant_slug": enrollment.tenant.slug,
            "tenant_host": tenant_host(enrollment.tenant),
            "status": enrollment.status,
        }
    )


@csrf_exempt
@require_POST
def agent_tunnel(request):
    """Provision (or reuse) a Cloudflare Tunnel and hand back its connector token.

    Delegates the actual Cloudflare API call to a small Worker
    (tunnel-provisioner) that holds the powerful, account-scoped Cloudflare
    token as its own secret -- this box only ever holds a narrow shared secret
    good for nothing but "ask that Worker for a tunnel". See tunnel_worker.py,
    which owns that call and is also what tears a tunnel down on delete. Until
    its credentials are set this answers 503 rather than guessing, so the
    contract is stable and testable end to end the moment they show up.
    """
    enrollment, error = authenticate_agent(request)
    if error:
        return error

    if enrollment.status == AgentEnrollment.Status.PENDING:
        return JsonResponse(
            {"error": "enrollment is still pending approval"}, status=409
        )

    try:
        result = tunnel_worker.ensure_tunnel(
            tunnel_id=enrollment.cf_tunnel_id,
            tenant_slug=enrollment.tenant.slug,
            enrollment_id=enrollment.pk,
        )
    except tunnel_worker.NotConfigured as exc:
        return JsonResponse({"error": str(exc)}, status=503)
    except tunnel_worker.WorkerError as exc:
        return JsonResponse({"error": f"tunnel provisioning failed: {exc}"}, status=502)

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
