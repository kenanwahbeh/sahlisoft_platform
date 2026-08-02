"""Conversations between a shop owner and the assistant.

A conversation belongs to exactly one tenant, and reaching one always goes
through the acting user's Membership -- same rule as every other view in this
project. The assistant can read a shop's books; letting a conversation be
addressed by id alone would be the whole isolation model undone.

**Messages store the API's own content blocks, not just text.** The Messages
API is stateless: every turn resends the full history, and a tool_use block
must come back paired with its tool_result or the next request is rejected.
Keeping ``blocks`` verbatim means a conversation replays exactly as it
happened; ``text`` is a flattened copy for the template, derived once on save
rather than re-walked on every render.
"""

from django.db import models
from django.utils.translation import gettext_lazy as _

from accounts.models import Tenant, User


class Conversation(models.Model):
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="conversations"
    )
    user = models.ForeignKey(
        User, on_delete=models.CASCADE, related_name="conversations"
    )
    title = models.CharField(_("title"), max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _("conversation")
        verbose_name_plural = _("conversations")
        ordering = ["-updated_at"]

    def __str__(self):
        return self.title or f"محادثة {self.pk}"

    def api_messages(self) -> list[dict]:
        """The full history in the shape the Messages API expects."""
        return [message.as_api_message() for message in self.messages.all()]


class Message(models.Model):
    """One turn. Tool results are `user` turns carrying tool_result blocks.

    That is the API's own convention, not a modelling choice -- results are
    fed back as a user message. :attr:`is_tool_result` exists so the template
    can tell those apart from something the owner actually typed.
    """

    class Role(models.TextChoices):
        USER = "user", _("user")
        ASSISTANT = "assistant", _("assistant")

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="messages"
    )
    role = models.CharField(max_length=16, choices=Role.choices)
    #: Verbatim Anthropic content blocks. Never edit these after the fact --
    #: a modified thinking or tool_use block makes the next request invalid.
    blocks = models.JSONField(default=list)
    #: Flattened text for display only. Never sent to the API.
    text = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("message")
        verbose_name_plural = _("messages")
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.role}: {self.text[:60]}"

    def as_api_message(self) -> dict:
        return {"role": self.role, "content": self.blocks}

    @staticmethod
    def flatten(blocks) -> str:
        """The readable text inside a block list, in order.

        Deliberately ignores thinking blocks: they are the model's scratchpad,
        and a dashboard that printed them next to the answer would be showing
        the shop owner reasoning that was never meant to be the answer.
        """
        if isinstance(blocks, str):
            return blocks
        parts = [
            block.get("text", "")
            for block in blocks or []
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        return "\n\n".join(part for part in parts if part).strip()

    @property
    def tool_uses(self) -> list[dict]:
        """The tool_use blocks in this message, for the "what it ran" panel."""
        return [
            block
            for block in self.blocks or []
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]

    @property
    def is_tool_result(self) -> bool:
        return bool(self.blocks) and all(
            isinstance(block, dict) and block.get("type") == "tool_result"
            for block in self.blocks
        )


class Run(models.Model):
    """One user turn being worked on, so the browser has something to poll.

    A turn can take a while: each tool call is a round trip out to a PC in a
    shop, and the model may make several. The request that starts a run
    returns immediately and the page polls this row, rather than holding an
    HTTP connection open for a minute across a tunnel.
    """

    class Status(models.TextChoices):
        RUNNING = "running", _("running")
        DONE = "done", _("done")
        FAILED = "failed", _("failed")

    conversation = models.ForeignKey(
        Conversation, on_delete=models.CASCADE, related_name="runs"
    )
    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.RUNNING
    )
    #: Arabic, and shown to the owner -- this is the one place a failure
    #: becomes visible, so it has to say something they can act on.
    error = models.CharField(max_length=600, blank=True)
    #: What the assistant is doing right now ("ينفّذ استعلاماً..."), updated as
    #: the loop advances so the page can show progress instead of a spinner.
    activity = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = _("run")
        verbose_name_plural = _("runs")
        ordering = ["-created_at"]

    def __str__(self):
        return f"run {self.pk} ({self.status})"
