"""Tenancy and identity for the Sahlisoft platform.

A Tenant is one shop. Every row of business data that arrives from an
on-premise agent belongs to exactly one tenant, and a signed-in user may
only ever see the tenants they hold a Membership for.
"""

import hashlib
import secrets

from django.contrib.auth.models import AbstractUser, BaseUserManager
from django.db import models
from django.utils import timezone
from django.utils.translation import gettext_lazy as _


class UserManager(BaseUserManager):
    """Manager for a user identified by email instead of username."""

    use_in_migrations = True

    def _create_user(self, email, password, **extra):
        if not email:
            raise ValueError("Users must have an email address")
        user = self.model(email=self.normalize_email(email), **extra)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_user(self, email, password=None, **extra):
        extra.setdefault("is_staff", False)
        extra.setdefault("is_superuser", False)
        return self._create_user(email, password, **extra)

    def create_superuser(self, email, password=None, **extra):
        extra.setdefault("is_staff", True)
        extra.setdefault("is_superuser", True)
        if not extra["is_staff"] or not extra["is_superuser"]:
            raise ValueError("Superuser must have is_staff and is_superuser set")
        return self._create_user(email, password, **extra)


class User(AbstractUser):
    """Email is the login credential; username is dropped entirely."""

    username = None
    email = models.EmailField(_("email address"), unique=True)
    full_name = models.CharField(_("full name"), max_length=150, blank=True)
    phone = models.CharField(_("phone"), max_length=32, blank=True)

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Meta:
        verbose_name = _("user")
        verbose_name_plural = _("users")

    def __str__(self):
        return self.full_name or self.email


class Tenant(models.Model):
    """One customer shop. The unit of data isolation."""

    name = models.CharField(_("shop name"), max_length=200)
    slug = models.SlugField(_("slug"), max_length=60, unique=True)
    is_active = models.BooleanField(_("active"), default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("tenant")
        verbose_name_plural = _("tenants")
        ordering = ["name"]

    def __str__(self):
        return self.name


class Membership(models.Model):
    """Grants one user access to one tenant. Access is never implicit."""

    class Role(models.TextChoices):
        OWNER = "owner", _("owner")
        STAFF = "staff", _("staff")
        VIEWER = "viewer", _("viewer")

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="memberships")
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, related_name="memberships")
    role = models.CharField(max_length=16, choices=Role.choices, default=Role.OWNER)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = _("membership")
        verbose_name_plural = _("memberships")
        constraints = [
            models.UniqueConstraint(fields=["user", "tenant"], name="uniq_user_tenant")
        ]

    def __str__(self):
        return f"{self.user} @ {self.tenant} ({self.role})"


class AgentEnrollment(models.Model):
    """One on-premise agent's permission to speak for one tenant.

    The agent runs unattended on a shop's Windows machine, so its token is a
    password in everything but name -- and passwords are not ours to keep. Only
    a SHA-256 digest is stored, next to a short non-secret ``token_prefix`` that
    exists purely so the dashboard can name a credential without revealing it.
    The plaintext is returned exactly once, by :meth:`issue`, and if the owner
    loses it the only cure is a new enrollment.

    Why a bare digest rather than ``make_password``: the slow KDFs exist to make
    guessing a *human-chosen* secret expensive. This token is 32 bytes straight
    from ``secrets``, so there is nothing to guess, and a deterministic digest
    buys two things a salted hash cannot -- a UNIQUE index, which turns a token
    collision into a database error instead of an assumption, and a single
    indexed lookup when an agent authenticates, instead of re-hashing the
    candidate against every row in the table.

    Status is the second factor of pairing. An agent that holds a valid token is
    still only ``pending``: a human has to approve it in the dashboard before it
    becomes ``active``. Possession of the token alone never streams data.
    """

    class Status(models.TextChoices):
        PENDING = "pending", _("pending")
        ACTIVE = "active", _("active")
        REVOKED = "revoked", _("revoked")

    # Enough of the token to tell two credentials apart in a table, far too
    # little to be worth anything to whoever reads that table.
    TOKEN_PREFIX_LENGTH = 8

    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, related_name="agent_enrollments"
    )
    label = models.CharField(_("label"), max_length=120, blank=True)
    token_prefix = models.CharField(_("token prefix"), max_length=16, db_index=True)
    token_hash = models.CharField(
        _("token hash"), max_length=64, unique=True, editable=False
    )
    status = models.CharField(
        _("status"), max_length=16, choices=Status.choices, default=Status.PENDING
    )
    created_at = models.DateTimeField(auto_now_add=True)
    approved_at = models.DateTimeField(_("approved at"), null=True, blank=True)
    last_seen_at = models.DateTimeField(_("last seen at"), null=True, blank=True)
    created_by = models.ForeignKey(
        User,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="issued_enrollments",
    )

    # The following are filled in by the agent itself, not by the dashboard --
    # see accounts/api_views.py::agent_register. They describe the one machine
    # this token is bound to, not a credential, so there is nothing here to
    # hash or hide.
    hostname = models.CharField(_("hostname"), max_length=255, blank=True)
    os_name = models.CharField(_("operating system"), max_length=100, blank=True)
    agent_version = models.CharField(_("agent version"), max_length=30, blank=True)
    db_folder = models.CharField(_("database folder"), max_length=500, blank=True)
    registered_at = models.DateTimeField(_("registered at"), null=True, blank=True)
    # A Cloudflare tunnel UUID, not a secret -- the connector token it is
    # exchanged for is never stored, only handed back to the agent that asked.
    cf_tunnel_id = models.CharField(_("Cloudflare tunnel id"), max_length=64, blank=True)

    class Meta:
        verbose_name = _("agent enrollment")
        verbose_name_plural = _("agent enrollments")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.label or self.token_prefix} @ {self.tenant} ({self.status})"

    @staticmethod
    def hash_token(raw: str) -> str:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @classmethod
    def issue(cls, *, tenant, label="", created_by=None):
        """Mint a pending enrollment. Returns ``(enrollment, plaintext token)``.

        The second element of that tuple is the only copy of the token that will
        ever exist; put it in the response and let it go.
        """
        raw = secrets.token_urlsafe(32)
        enrollment = cls.objects.create(
            tenant=tenant,
            label=label,
            created_by=created_by,
            token_prefix=raw[: cls.TOKEN_PREFIX_LENGTH],
            token_hash=cls.hash_token(raw),
        )
        return enrollment, raw

    @classmethod
    def resolve(cls, raw):
        """The enrollment a raw token belongs to, or None. For the agent API.

        Deliberately indifferent to status: the caller decides what an
        unapproved or revoked agent may do, and needs the row in hand to say so
        intelligibly rather than answering every case with "unknown token".
        """
        if not raw:
            return None
        return (
            cls.objects.select_related("tenant")
            .filter(token_hash=cls.hash_token(raw))
            .first()
        )

    def check_token(self, raw) -> bool:
        return bool(raw) and secrets.compare_digest(self.token_hash, self.hash_token(raw))

    @property
    def masked_token(self) -> str:
        return f"{self.token_prefix}…"

    @property
    def is_usable(self) -> bool:
        """Approved, not revoked, and the shop itself still switched on."""
        return self.status == self.Status.ACTIVE and self.tenant.is_active

    def approve(self):
        self.status = self.Status.ACTIVE
        self.approved_at = timezone.now()
        self.save(update_fields=["status", "approved_at"])

    def revoke(self):
        self.status = self.Status.REVOKED
        self.save(update_fields=["status"])

    def touch(self):
        """Record a check-in. Called by the agent API, not by the dashboard."""
        self.last_seen_at = timezone.now()
        self.save(update_fields=["last_seen_at"])


class AgentCommand(models.Model):
    """One read-only instruction queued for an agent, run on its next heartbeat.

    The agent never receives a push -- it always initiates by calling
    /api/agent/heartbeat/, and this table is what that endpoint hands back.
    `kind` matches a handler name in the agent's own worker.py (ping, query,
    list_databases, fingerprint, list_tables, table_schema, agent_status);
    `params` rides along verbatim as the rest of that command's JSON body, so
    adding a new kind never needs a migration here.

    A command is SENT the moment it is handed to the agent, not on creation,
    so "how long has this actually been sitting on the shop's machine" stays
    answerable. The agent posts its result back inside the *next*
    heartbeat's `results` list -- there is no separate results endpoint.
    """

    class Status(models.TextChoices):
        QUEUED = "queued", _("queued")
        SENT = "sent", _("sent")
        DONE = "done", _("done")
        FAILED = "failed", _("failed")

    enrollment = models.ForeignKey(
        AgentEnrollment, on_delete=models.CASCADE, related_name="commands"
    )
    kind = models.CharField(_("kind"), max_length=32)
    params = models.JSONField(_("parameters"), default=dict, blank=True)
    status = models.CharField(
        _("status"), max_length=16, choices=Status.choices, default=Status.QUEUED
    )
    result = models.JSONField(_("result"), null=True, blank=True)
    error = models.CharField(_("error"), max_length=600, blank=True)
    created_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(_("sent at"), null=True, blank=True)
    completed_at = models.DateTimeField(_("completed at"), null=True, blank=True)

    class Meta:
        verbose_name = _("agent command")
        verbose_name_plural = _("agent commands")
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.kind} -> {self.enrollment} ({self.status})"

    def mark_sent(self):
        self.status = self.Status.SENT
        self.sent_at = timezone.now()

    def as_payload(self) -> dict:
        """What the agent receives: its own params plus id/type on top."""
        payload = {"id": self.pk, "type": self.kind}
        payload.update(self.params or {})
        return payload

    def record_result(self, item: dict) -> None:
        """Absorb one entry from the agent's heartbeat `results` list."""
        self.result = item.get("result")
        self.error = str(item.get("error") or "")[:600]
        self.status = self.Status.DONE if item.get("ok") else self.Status.FAILED
        self.completed_at = timezone.now()
        self.save(update_fields=["result", "error", "status", "completed_at"])
