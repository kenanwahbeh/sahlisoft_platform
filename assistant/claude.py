"""The turn loop: an LLM, plus a queue that reaches a PC in a shop.

A manual loop rather than an SDK tool runner, for one concrete reason: a
runner calls a local function and waits for it to return, and there is no
local function here. A tool call becomes a database row that a Windows
machine picks up on its next heartbeat and answers on the one after. Owning
the loop also means every step is persisted as it happens, so a page that
reloads mid-run still shows what the assistant has done so far.

Runs on a daemon thread. That is the right size for this: a single VPS, a
handful of concurrent owners, and work that is almost entirely waiting on a
shop's connection. If this ever needs to survive a restart mid-turn, the state
is already in the database and this becomes a queue consumer without the
callers noticing when that matters.
"""

import json
import logging
import threading

import openai
from django.conf import settings
from django.db import connection

from . import agent_tools, prompts
from .models import Message, Run

logger = logging.getLogger(__name__)

#: OpenCode Zen's free "stealth" model (believed GLM-4.6-based). Chosen
#: because the platform's owners are in Syria -- on Anthropic's and Google's
#: sanctions-restricted country lists, so Claude/Gemini are unreachable
#: regardless of payment method, and there is no bank card to pay with
#: anyway. Marketed free "for a limited time" during a beta; it may start
#: charging or disappear later, at which point this needs a new home, not
#: just a new model string.
MODEL = "big-pickle"

#: OpenCode Zen's OpenAI-compatible gateway.
ZEN_BASE_URL = "https://opencode.ai/zen/v1"

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
    key = (settings.OPENCODE_ZEN_API_KEY or "").strip()
    if not key:
        raise NotConfigured(
            "لم تُفعَّل خدمة المساعد الذكي على هذه المنصة بعد. راسل مسؤول المنصة."
        )
    return openai.OpenAI(api_key=key, base_url=ZEN_BASE_URL)


def _system_messages(tenant, enrollment) -> list[dict]:
    """Frozen block first, shop-specific second -- same split as before.

    There is no cache_control equivalent on this wire format, but SYSTEM
    stays byte-identical across tenants regardless: whatever caching Zen does
    on its side gets to see the same stable prefix either way, and the split
    means prompts.py never has to change again if that does start mattering.
    """
    return [
        {"role": "system", "content": prompts.SYSTEM},
        {"role": "system", "content": prompts.shop_context(tenant, enrollment)},
    ]


def _wire_tools() -> list[dict]:
    """``agent_tools.TOOLS``, translated into Big Pickle's function-calling
    shape. Lives here, not in agent_tools.py: that module has no reason to
    know which wire format is in use -- this is the one place that does.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        }
        for tool in agent_tools.TOOLS
    ]


def _to_wire_messages(history: list[dict]) -> list[dict]:
    """Translate this app's own stored block shape into the wire format.

    ``history`` is exactly what :meth:`Conversation.api_messages` returns --
    the same internal shape every other part of this app reads and writes.
    Only this function needs to know that the wire format underneath it is
    OpenAI-style chat completions rather than Anthropic's Messages API: a
    tool_result turn becomes N separate ``role: "tool"`` messages instead of
    one user message carrying N blocks, and an assistant turn's tool calls
    move from inline content blocks to a sibling ``tool_calls`` field.
    """
    wire = []
    for turn in history:
        role = turn["role"]
        blocks = turn["content"]

        if role == "user" and blocks and all(
            b.get("type") == "tool_result" for b in blocks
        ):
            for block in blocks:
                wire.append(
                    {
                        "role": "tool",
                        "tool_call_id": block.get("tool_use_id"),
                        # is_error has no wire-level side channel here -- the
                        # error text is already in content, and the model
                        # reads the string either way.
                        "content": block.get("content", ""),
                    }
                )
            continue

        if role == "assistant":
            text = "\n\n".join(
                b.get("text", "") for b in blocks if b.get("type") == "text"
            )
            tool_calls = [
                {
                    "id": b.get("id"),
                    "type": "function",
                    "function": {
                        "name": b.get("name"),
                        "arguments": json.dumps(
                            b.get("input") or {}, ensure_ascii=False
                        ),
                    },
                }
                for b in blocks
                if b.get("type") == "tool_use"
            ]
            message = {"role": "assistant", "content": text or None}
            if tool_calls:
                message["tool_calls"] = tool_calls
            wire.append(message)
            continue

        # A typed question: role == "user", blocks == [{"type": "text", ...}]
        text = "\n\n".join(
            b.get("text", "") for b in blocks if b.get("type") == "text"
        )
        wire.append({"role": role, "content": text})
    return wire


def _execute_tools(blocks, run, enrollment, user) -> list[dict]:
    """Run every tool_use block and return the matching tool_result blocks.

    All results go back for the same turn, and every tool_use gets exactly
    one tool_result -- an unpaired one makes the next request invalid, so a
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
    system = _system_messages(conversation.tenant, enrollment)
    tools = _wire_tools()
    messages = conversation.api_messages()

    for _ in range(MAX_ITERATIONS):
        run.activity = "يفكّر…"
        run.save(update_fields=["activity"])

        response = api.chat.completions.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            messages=system + _to_wire_messages(messages),
            tools=tools,
        )

        # model_dump, not typed attribute access: reasoning_content is not a
        # standard chat-completions field, and this is the one shape that
        # reliably surfaces it (confirmed live) regardless of whether the
        # SDK's typed response model happens to declare it.
        payload = response.model_dump(mode="json")
        choice = payload["choices"][0]
        wire_message = choice["message"]
        finish_reason = choice.get("finish_reason")

        blocks = []
        if wire_message.get("reasoning_content"):
            blocks.append(
                {"type": "thinking", "thinking": wire_message["reasoning_content"]}
            )
        if wire_message.get("content"):
            blocks.append({"type": "text", "text": wire_message["content"]})
        for call in wire_message.get("tool_calls") or []:
            fn = call.get("function") or {}
            try:
                tool_input = json.loads(fn.get("arguments") or "{}")
            except ValueError:
                # A malformed arguments string is new territory for a beta
                # model -- fall back to {} rather than dropping the block: an
                # orphaned tool_calls entry with no reply next turn is a wire
                # error, whereas a tool call with the wrong input just comes
                # back as an ordinary tool_result error the model can read
                # and correct from.
                tool_input = {}
                logger.warning(
                    "run %s: %s returned unparseable tool arguments: %r",
                    run.pk,
                    fn.get("name"),
                    fn.get("arguments"),
                )
            blocks.append(
                {
                    "type": "tool_use",
                    "id": call.get("id"),
                    "name": fn.get("name"),
                    "input": tool_input,
                }
            )

        Message.objects.create(
            conversation=conversation,
            role=Message.Role.ASSISTANT,
            blocks=blocks,
            text=Message.flatten(blocks),
        )
        messages.append({"role": "assistant", "content": blocks})

        if finish_reason == "content_filter":
            agent_tools.finish(
                run,
                Run.Status.FAILED,
                "تعذّر الرد على هذا الطلب. جرّب صياغة السؤال بشكل مختلف.",
            )
            return

        if finish_reason == "tool_calls":
            results = _execute_tools(blocks, run, enrollment, user)
            Message.objects.create(
                conversation=conversation,
                role=Message.Role.USER,
                blocks=results,
                text="",
            )
            messages.append({"role": "user", "content": results})
            continue

        if finish_reason == "length":
            agent_tools.finish(
                run,
                Run.Status.DONE,
                "",
            )
            return

        if finish_reason != "stop":
            # Big Pickle's exact finish_reason vocabulary is not fully
            # documented -- fall back to done rather than fail, but log it so
            # an unrecognised value doesn't just disappear.
            logger.warning(
                "run %s: unexpected finish_reason %r", run.pk, finish_reason
            )

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
    except openai.RateLimitError:
        agent_tools.finish(
            run, Run.Status.FAILED, "الخدمة مزدحمة حالياً. أعد المحاولة بعد قليل."
        )
    except openai.AuthenticationError:
        logger.error("OpenCode Zen rejected the platform's API key")
        agent_tools.finish(
            run,
            Run.Status.FAILED,
            "إعدادات المساعد الذكي غير صحيحة على المنصة. راسل مسؤول المنصة.",
        )
    except openai.APIError as exc:
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
