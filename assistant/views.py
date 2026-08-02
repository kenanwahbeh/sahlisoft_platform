"""The chat surface.

Reaching a conversation always starts from the acting user's Membership, never
from the id in the URL — same rule as accounts/views.py, and for the same
reason: this feature can read a shop's entire books, so "which shop is this"
must not be answerable by editing a number in the address bar.

**Owner-only, deliberately.** The dashboard shows agents to every member of a
shop, but *querying the books* is a different capability from *seeing that a
machine is online*. A viewer who could ask the assistant for the chart of
accounts would have more access through this page than through any other, so
the assistant sits behind the same role gate as issuing a credential.
"""

import json

from django.contrib import messages as django_messages
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_POST

from accounts.models import Membership

from . import claude, markup
from .models import Conversation, Message, Run

#: Same gate as issuing an agent credential. See the module docstring.
ALLOWED_ROLES = {Membership.Role.OWNER}

MAX_QUESTION_LENGTH = 4000


def _tenant(user, slug):
    membership = (
        Membership.objects.select_related("tenant")
        .filter(user=user, tenant__slug=slug, role__in=ALLOWED_ROLES)
        .first()
    )
    if membership is None:
        # 404 rather than 403, like the rest of the project: a 403 would
        # confirm to a stranger that the shop exists.
        raise Http404("Unknown shop")
    return membership.tenant


def _conversation(tenant, user, pk):
    return get_object_or_404(Conversation, pk=pk, tenant=tenant, user=user)


def _visible_messages(conversation):
    """What the owner sees, in order.

    Tool-result turns are dropped: they are `user` messages only because the
    API says results come back that way, and rendering them as though the
    owner had typed a wall of JSON would be nonsense. The tool *calls* are kept
    — they are the audit trail of what ran against the shop's data.
    """
    rows = []
    for message in conversation.messages.all():
        if message.role == Message.Role.USER:
            if message.is_tool_result:
                continue
            # Left as plain text and escaped by the template: this is what the
            # owner typed, and it has no reason to become markup.
            rows.append({"role": "user", "text": message.text, "tools": []})
            continue

        tools = [
            {
                "name": block.get("name", ""),
                # The SQL is the point of showing this at all -- the owner can
                # see exactly what ran against their books.
                "sql": (block.get("input") or {}).get("sql", ""),
                "input": json.dumps(
                    block.get("input") or {}, ensure_ascii=False, indent=2
                ),
            }
            for block in message.tool_uses
        ]
        if message.text or tools:
            rows.append(
                {
                    "role": "assistant",
                    "html": markup.render(message.text),
                    "tools": tools,
                }
            )
    return rows


def _active_run(conversation):
    return conversation.runs.filter(status=Run.Status.RUNNING).first()


@login_required
def chat(request, slug):
    """The shop's assistant page. Opens the latest conversation, or a fresh one."""
    tenant = _tenant(request.user, slug)
    conversation = (
        Conversation.objects.filter(tenant=tenant, user=request.user)
        .order_by("-updated_at")
        .first()
    )
    if conversation is None:
        conversation = Conversation.objects.create(tenant=tenant, user=request.user)
    return redirect("assistant_chat_detail", slug=slug, pk=conversation.pk)


@login_required
def chat_detail(request, slug, pk):
    tenant = _tenant(request.user, slug)
    conversation = _conversation(tenant, request.user, pk)

    # The page reloads itself the moment a run stops, so a failure that only
    # lived on the Run row would flash past and vanish. Surface the last one
    # if it failed -- otherwise the assistant just silently does nothing.
    latest = conversation.runs.first()
    failed = latest if latest and latest.status == Run.Status.FAILED else None

    return render(
        request,
        "assistant/chat.html",
        {
            "tenant": tenant,
            "conversation": conversation,
            "rows": _visible_messages(conversation),
            "run": _active_run(conversation),
            "failed_run": failed,
            "conversations": Conversation.objects.filter(
                tenant=tenant, user=request.user
            )[:20],
        },
    )


@login_required
@require_POST
def ask(request, slug, pk):
    """Record the question, start the turn, and get out of the way.

    Deliberately does not wait for the answer: a turn spans one or more round
    trips to a PC in a shop, and holding the HTTP connection open for that
    would tie up a gunicorn worker and time out at the tunnel. The page polls
    :func:`run_status` instead.
    """
    tenant = _tenant(request.user, slug)
    conversation = _conversation(tenant, request.user, pk)

    question = (request.POST.get("question") or "").strip()[:MAX_QUESTION_LENGTH]
    if not question:
        return redirect("assistant_chat_detail", slug=slug, pk=pk)

    if _active_run(conversation) is not None:
        django_messages.info(request, "المساعد ما زال يعمل على سؤالك السابق.")
        return redirect("assistant_chat_detail", slug=slug, pk=pk)

    Message.objects.create(
        conversation=conversation,
        role=Message.Role.USER,
        blocks=[{"type": "text", "text": question}],
        text=question,
    )
    # The first question names the conversation, so the sidebar is readable
    # without a second model call just to title it.
    if not conversation.title:
        conversation.title = question[:80]
    conversation.save()

    run = Run.objects.create(conversation=conversation, activity="يفكّر…")
    claude.start(run)

    return redirect("assistant_chat_detail", slug=slug, pk=pk)


@login_required
def run_status(request, slug, pk):
    """Poll target: is the turn still going, and what is it doing?"""
    tenant = _tenant(request.user, slug)
    conversation = _conversation(tenant, request.user, pk)
    run = conversation.runs.first()

    if run is None:
        return JsonResponse({"status": "idle", "activity": "", "error": ""})
    return JsonResponse(
        {
            "status": run.status,
            "activity": run.activity,
            "error": run.error,
        }
    )


@login_required
@require_POST
def new_conversation(request, slug):
    tenant = _tenant(request.user, slug)
    conversation = Conversation.objects.create(tenant=tenant, user=request.user)
    return redirect("assistant_chat_detail", slug=slug, pk=conversation.pk)
