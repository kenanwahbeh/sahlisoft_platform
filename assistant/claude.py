"""The turn loop: Claude, plus a queue that reaches a PC in a shop.

A manual loop rather than the SDK's tool runner, for one concrete reason: the
runner calls a local function and waits for it to return, and there is no local
function here. A tool call becomes a database row that a Windows machine picks
up on its next heartbeat and answers on the one after. Owning the loop also
means every step is persisted as it happens, so a page that reloads mid-run
still shows what the assistant has done so far.

Runs on a daemon thread. That is the right size for this: a single VPS, a
handful of concurrent owners, and work that is almost entirely waiting on a
shop's connection. If this ever needs to survive a restart mid-turn, the state
is already in the database and this becomes a queue consumer without the
callers noticing.
"""

import json
import logging
import threading

import anthropic
from django.conf import settings
from django.db import connection

from . import agent_tools, prompts
from .models import Message, Run

logger = logging.getLogger(__name__)

MODEL = "claude-opus-4-8"

#: Under the ~16K non-streaming guidance, so no streaming machinery is needed.
#: An answer here is a paragraph and a table, not a document.
MAX_TOKENS = 8000

#: A ceiling on tool calls per turn. Reached only if the model gets stuck in a
#: retry loop against a database that keeps rejecting its query -- at which
#: point saying so beats spending another minute of the owner's time.
MAX_ITERATIONS = 12


class NotConfigured(Exception):
    """No API key on this deployment -- the feature is off, not broken."""


def client():
    key = (settings.ANTHROPIC_API_KEY or "").strip()
    if not key:
        raise NotConfigured(
            "لم تُفعَّل خدمة المساعد الذكي على هذه المنصة بعد. راسل مسؤول المنصة."
        )
    return anthropic.Anthropic(api_key=key)


def _system(tenant, enrollment) -> list[dict]:
    """Frozen block first (cached), shop-specific second (not).

    Order matters: caching is a prefix match, so the volatile half has to come
    after the breakpoint or every tenant gets its own cache entry and the
    breakpoint buys nothing.
    """
    return [
        {
            "type": "text",
            "text": prompts.SYSTEM,
            "cache_control": {"type": "ephemeral"},
        },
        {"type": "text", "text": prompts.shop_context(tenant, enrollment)},
    ]


def _execute_tools(blocks, run, enrollment, user) -> list[dict]:
    """Run every tool_use block and return the matching tool_result blocks.

    All results go back in one user message, and every tool_use gets exactly
    one tool_result -- an unpaired block makes the next request invalid, so a
    failure has to come back as an error result rather than be dropped.
    """
    results = []
    for block in blocks:
        if block.get("type") != "tool_use":
            continue

        name = block.get("name") or ""
        agent_tools.touch_activity(run, name)
        outcome = agent_tools.run_command(
            enrollment, name, block.get("input") or {}, created_by=user
        )

        if outcome["ok"]:
            content = outcome["result"]
            # Tool results must be text (or blocks); the agent hands back
            # already-serialised JSON strings for the tool-backed commands and
            # dicts for the rest, so normalise here rather than in five places.
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False, default=str)
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.get("id"),
                    "content": content,
                }
            )
        else:
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.get("id"),
                    "content": outcome["error"],
                    "is_error": True,
                }
            )
    return results


def advance(run: Run) -> None:
    """Drive one user turn to completion, persisting every step."""
    conversation = run.conversation
    user = conversation.user
    enrollment = agent_tools.pick_enrollment(conversation.tenant)

    api = client()
    system = _system(conversation.tenant, enrollment)
    messages = conversation.api_messages()

    for _ in range(MAX_ITERATIONS):
        run.activity = "يفكّر…"
        run.save(update_fields=["activity"])

        response = api.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            tools=agent_tools.TOOLS,
            messages=messages,
        )

        # mode="json" keeps thinking signatures and tool_use ids intact, which
        # is what makes the history replayable on the next iteration.
        blocks = response.model_dump(mode="json")["content"]
        Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            blocks=blocks,
            text=Message.flatten(blocks),
        )
        messages.append({"role": "assistant", "content": blocks})

        if response.stop_reason == "refusal":
            agent_tools.finish(
                run,
                Run.Status.FAILED,
                "تعذّر الرد على هذا الطلب. جرّب صياغة السؤال بشكل مختلف.",
            )
            return

        if response.stop_reason == "tool_use":
            results = _execute_tools(blocks, run, enrollment, user)
            Message.objects.create(
                conversation=conversation,
                role=Message.Role.USER,
                blocks=results,
                text="",
            )
            messages.append({"role": "user", "content": results})
            continue

        if response.stop_reason == "max_tokens":
            agent_tools.finish(
                run,
                Run.Status.DONE,
                "",
            )
            return

        agent_tools.finish(run, Run.Status.DONE)
        return

    agent_tools.finish(
        run,
        Run.Status.FAILED,
        "توقّف المساعد بعد عدد كبير من المحاولات دون الوصول إلى نتيجة. "
        "جرّب سؤالاً أضيق.",
    )


def _worker(run_id: int) -> None:
    """Thread body. Owns its database connection and always closes it."""
    try:
        run = Run.objects.select_related("conversation__tenant").get(pk=run_id)
    except Run.DoesNotExist:
        return

    try:
        advance(run)
    except agent_tools.NoAgentAvailable as exc:
        agent_tools.finish(run, Run.Status.FAILED, str(exc))
    except NotConfigured as exc:
        agent_tools.finish(run, Run.Status.FAILED, str(exc))
    except anthropic.RateLimitError:
        agent_tools.finish(
            run, Run.Status.FAILED, "الخدمة مزدحمة حالياً. أعد المحاولة بعد قليل."
        )
    except anthropic.AuthenticationError:
        logger.error("Anthropic rejected the platform's API key")
        agent_tools.finish(
            run,
            Run.Status.FAILED,
            "إعدادات المساعد الذكي غير صحيحة على المنصة. راسل مسؤول المنصة.",
        )
    except anthropic.APIError as exc:
        logger.warning("assistant run %s failed: %s", run_id, exc)
        agent_tools.finish(
            run, Run.Status.FAILED, "تعذّر الوصول إلى خدمة المساعد. أعد المحاولة."
        )
    except Exception:
        # Last line of defence: this thread has no supervisor, and a run left
        # in "running" would spin the page's poller forever.
        logger.exception("assistant run %s crashed", run_id)
        agent_tools.finish(run, Run.Status.FAILED, "حدث خطأ غير متوقّع.")
    finally:
        connection.close()


def start(run: Run) -> None:
    """Hand the turn to a background thread and return immediately."""
    threading.Thread(target=_worker, args=(run.pk,), daemon=True).start()
