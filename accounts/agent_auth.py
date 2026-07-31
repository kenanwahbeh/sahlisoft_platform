"""Shared authentication for the on-premise agent's HTTP API.

Every agent endpoint (register, heartbeat, tunnel) starts with the same
question -- which AgentEnrollment does this bearer token belong to, and is it
still allowed to talk to us -- so that question is answered once here instead
of three times in accounts/api_views.py. Callers get back a
``(enrollment, error_response)`` pair and only ever need one line:

    enrollment, error = authenticate_agent(request)
    if error:
        return error

This re-derives the digest exactly the way ``AgentEnrollment.resolve`` and
``AgentEnrollment.check_token`` already do (see accounts/models.py) rather
than reinventing token hashing here.
"""

import json

from django.http import JsonResponse

from .models import AgentEnrollment


def authenticate_agent(request):
    """Resolve the ``Authorization: Bearer <token>`` header to an enrollment.

    Returns ``(enrollment, None)`` on success or ``(None, error_response)`` on
    failure -- the revoked check lives here too, since every endpoint in the
    contract treats a revoked enrollment identically (403, before doing
    anything else).
    """
    header = request.META.get("HTTP_AUTHORIZATION", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None, JsonResponse(
            {"error": "missing or malformed Authorization header"}, status=401
        )

    enrollment = AgentEnrollment.resolve(token)
    if enrollment is None:
        return None, JsonResponse({"error": "unknown token"}, status=401)

    if enrollment.status == AgentEnrollment.Status.REVOKED:
        return None, JsonResponse({"error": "enrollment has been revoked"}, status=403)

    return enrollment, None


def parse_json_body(request):
    """Empty body -> ``{}``; malformed JSON -> ``None`` for the caller to reject.

    The contract allows an empty or ``{}`` heartbeat body, so "no body" is not
    an error -- only body bytes that fail to parse are.
    """
    if not request.body:
        return {}
    try:
        return json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
