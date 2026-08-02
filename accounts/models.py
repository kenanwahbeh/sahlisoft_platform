"""Tenancy and identity for the Sahlisoft platform.

A Tenant is one shop. Every row of business data that arrives from an
on-premise agent belongs to exactly one tenant, and a signed-in user may
only ever see the tenants they hold a Membership for.
"""

import hashlib
import secrets
from datetime import timedelta

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
        # A routine, reversible pause -- the shop is closed, the PC is being
        # replaced, the enrollment is on its way to being deleted. Distinct
        # from REVOKED on purpose: revoking says a credential is burned and
        # must never work again, and nothing should be able to undo that by
        # clicking "start". The agent keeps checking in while stopped, so an
        # operator can flip it back to active and have it resume by itself.
        STOPPED = "stopped", _("stopped")
        REVOKED = "revoked", _("revoked")

    #: Deleting is only offered once an agent is demonstrably not in service.
    DELETABLE_STATUSES = frozenset({Status.STOPPED, Status.REVOKED})

    #: How stale ``last_seen_at`` may get before the dashboard stops calling an
    #: agent reachable. The agent heartbeats once a minute by default, so this
    #: is three missed beats -- long enough that one dropped request on a shop's
    #: mobile connection does not flicker the light, short enough that a machine
    #: someone unplugged this morning is not still shown as online at noon.
    ONLINE_WINDOW = timedelta(minutes=3)

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

    def rotate_token(self) -> str:
        """Mint a fresh token for an enrollment that already exists.

        :meth:`issue` cannot serve the activation flow: it *creates* a row, and
        by activation time the row is already there -- the owner made it in the
        dashboard and downloaded a pairing file naming it. So the token is
        replaced in place, which is also what makes an activation code safe to
        ship inside an installer: the real credential does not exist anywhere
        until the machine that will use it asks for one.

        Rotating invalidates whatever token the enrollment had before, which is
        the point on a reinstall. It deliberately does *not* clear ``hostname``:
        the one-machine binding belongs to the enrollment, not to the token, and
        letting a re-download quietly unbind it would turn the anti-reuse guard
        in agent_register into a formality.
        """
        raw = secrets.token_urlsafe(32)
        self.token_prefix = raw[: self.TOKEN_PREFIX_LENGTH]
        self.token_hash = self.hash_token(raw)
        self.save(update_fields=["token_prefix", "token_hash"])
        return raw

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

    @property
    def is_online(self) -> bool:
        """Has this machine checked in recently enough to be called reachable?

        Purely a statement about the clock, deliberately indifferent to status:
        a stopped agent keeps heartbeating (that is what makes stopping
        reversible), and the dashboard wants to say "متوقف، لكنه متصل" rather
        than pretend a machine that is plainly answering is dark. The two facts
        are combined at the point of display, not conflated here.
        """
        if self.last_seen_at is None:
            return False
        return timezone.now() - self.last_seen_at <= self.ONLINE_WINDOW

    def approve(self):
        self.status = self.Status.ACTIVE
        self.approved_at = timezone.now()
        self.save(update_fields=["status", "approved_at"])

    def revoke(self):
        self.status = self.Status.REVOKED
        self.save(update_fields=["status"])

    def stop(self):
        self.status = self.Status.STOPPED
        self.save(update_fields=["status"])

    def teardown_and_delete(self):
        """Delete this enrollment, taking its Cloudflare tunnel with it.

        The only supported way to delete an enrollment. A plain ``.delete()``
        removes the row and silently strands the tunnel in the Cloudflare
        account, where nothing afterwards knows it exists -- so the dashboard
        view and the admin action both come through here, and the admin's stock
        bulk-delete is switched off (see admin.py).

        Order matters: the tunnel goes first. If it cannot be torn down the row
        stays exactly as it was, so the operator can retry, rather than the
        tunnel outliving the only record of it.

        Raises ValueError if the enrollment has not been stopped or revoked,
        and tunnel_worker.WorkerError if Cloudflare would not let go.
        """
        from . import tunnel_worker

        if self.status not in self.DELETABLE_STATUSES:
            raise ValueError("stop the agent before deleting it")

        if self.cf_tunnel_id:
            tunnel_worker.delete_tunnel(self.cf_tunnel_id)

        self.delete()

    def touch(self):
        """Record a check-in. Called by the agent API, not by the dashboard."""
        self.last_seen_at = timezone.now()
        self.save(update_fields=["last_seen_at"])


class AgentActivationCode(models.Model):
    """A short-lived, single-use coupon that a shop PC trades for a real token.

    It exists so the artifact the owner downloads -- today a pairing file,
    tomorrow the bytes appended to a signed installer -- is not itself the
    credential. A downloaded installer sits in a Downloads folder, gets copied
    to a USB stick, gets forwarded on WhatsApp; a long-lived bearer token
    embedded in it would be all of those things too, indefinitely. A code that
    dies in thirty minutes and works exactly once is not worth stealing, and
    the token it is exchanged for is minted only when the machine that will
    actually use it asks (see :meth:`AgentEnrollment.rotate_token`).

    Same hash-only storage as the token itself: the plaintext is returned once,
    by :meth:`issue`, and never stored. That is also why redemption looks the
    code up by digest rather than scanning rows -- one indexed lookup, and a
    UNIQUE index that turns a collision into a database error.

    Codes are not cleaned up on redemption. A consumed row is the record that a
    particular download was used, and by whom, which is worth more than the
    handful of bytes it costs.
    """

    #: Long enough that a saved download still works after the owner walks
    #: away and comes back, short enough that a leaked installer is inert by
    #: the time anyone finds it interesting.
    LIFETIME = timedelta(minutes=30)

    #: Same non-secret naming trick as AgentEnrollment.token_prefix.
    CODE_PREFIX_LENGTH = 8

    enrollment = models.ForeignKey(
        AgentEnrollment, on_delete=models.CASCADE, related_name="activation_codes"
    )
    code_prefix = models.CharField(_("code prefix"), max_length=16, db_index=True)
    code_hash = models.CharField(
        _("code hash"), max_length=64, unique=True, editable=False
    )
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(_("expires at"))
    consumed_at = models.DateTimeField(_("consumed at"), null=True, blank=True)
    #: Which machine redeemed it, for the same reason agent_register records a
    #: hostname: so an unexpected redemption is visible after the fact.
    consumed_from = models.GenericIPAddressField(
        _("consumed from"), null=True, blank=True
    )

    class Meta:
        verbose_name = _("agent activation code")
        verbose_name_plural = _("agent activation codes")
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.code_prefix}… -> {self.enrollment_id}"

    @staticmethod
    def hash_code(raw: str) -> str:
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @classmethod
    def issue(cls, enrollment):
        """Mint a code for this enrollment. Returns ``(code, plaintext)``.

        32 bytes from ``secrets`` -- the same entropy as the long-lived token,
        deliberately, since this one travels inside a downloadable file and
        over the wire on first run rather than being shown once to an operator.
        Nothing here is meant to be typed by a human, so there is no reason to
        shorten it.

        Any codes still outstanding for this enrollment are burned first: two
        live pairing files for one agent means the second download silently
        leaves the first working, and "I downloaded it again to be safe" should
        make the older copy useless, not keep both.
        """
        cls.objects.filter(enrollment=enrollment, consumed_at__isnull=True).update(
            consumed_at=timezone.now()
        )
        raw = secrets.token_urlsafe(32)
        code = cls.objects.create(
            enrollment=enrollment,
            code_prefix=raw[: cls.CODE_PREFIX_LENGTH],
            code_hash=cls.hash_code(raw),
            expires_at=timezone.now() + cls.LIFETIME,
        )
        return code, raw

    @classmethod
    def resolve(cls, raw):
        """The code row a plaintext belongs to, or None.

        Indifferent to expiry and redemption, exactly like
        ``AgentEnrollment.resolve`` is indifferent to status: the caller needs
        the row in hand to answer "expired" rather than "unknown", which is the
        difference between an owner re-downloading and an owner filing a bug.
        """
        if not raw:
            return None
        return (
            cls.objects.select_related("enrollment", "enrollment__tenant")
            .filter(code_hash=cls.hash_code(raw))
            .first()
        )

    @property
    def is_redeemable(self) -> bool:
        return self.consumed_at is None and timezone.now() < self.expires_at

    def consume(self, ip=None):
        """Claim the code, then mint a token for it. None if already claimed.

        The claim is a conditional UPDATE rather than a check followed by a
        save, because "single use" has to survive two machines redeeming the
        same leaked file at the same moment. Both would pass an
        :attr:`is_redeemable` check; only one wins a
        ``filter(consumed_at__isnull=True).update(...)``, which SQLite executes
        as one statement.

        Claiming before minting is the deliberate order. If minting then failed
        the code would be spent with nothing handed back -- recoverable, since
        the owner just downloads a new pairing file. Minting first and claiming
        after would instead let both racers mint, and the first one's token
        would be silently overwritten by the second: an agent holding a
        credential that stopped working for no visible reason. A dead code
        beats a dead token.
        """
        claimed = type(self).objects.filter(pk=self.pk, consumed_at__isnull=True).update(
            consumed_at=timezone.now(), consumed_from=ip or None
        )
        if not claimed:
            return None
        self.refresh_from_db(fields=["consumed_at", "consumed_from"])
        return self.enrollment.rotate_token()


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
