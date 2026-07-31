"""Give every new signup a shop of its own.

Signing up *is* the onboarding flow. Wildcard DNS and the tunnel already answer
for *.PLATFORM_DOMAIN and one Apache vhost hands every unclaimed subdomain to
this app, so a Tenant row is the only thing standing between a new account and a
live shop URL -- no infrastructure step, nothing for an operator to do.

This hangs off allauth's ``user_signed_up`` rather than off a view so it holds
for every entry point that can ever create an account (the signup form today,
social or headless later). Note that it fires at signup, before the mandatory
email verification completes: the shop exists from the first moment, but its
owner cannot sign in to see it until the address is confirmed.
"""

import logging
import re

from allauth.account.signals import user_signed_up
from django.conf import settings
from django.db import IntegrityError, transaction
from django.dispatch import receiver
from django.utils.text import slugify

from .models import Membership, Tenant

logger = logging.getLogger(__name__)

# A slug here is not decoration, it is a DNS label, so it is stricter than what
# slugify alone guarantees: lowercase a-z, digits, single inner hyphens, and no
# hyphen at either end.
_NON_LABEL = re.compile(r"[^a-z0-9-]+")
_RUN_OF_DASHES = re.compile(r"-{2,}")

SLUG_MAX_LENGTH = Tenant._meta.get_field("slug").max_length
FALLBACK_SLUG = "shop"


def slug_candidate(source: str) -> str:
    """Best-effort DNS label from arbitrary text. May still collide."""
    label = slugify(source).lower()
    label = _RUN_OF_DASHES.sub("-", _NON_LABEL.sub("-", label)).strip("-")
    # An all-Arabic local part slugifies to nothing at all, hence the fallback.
    return label[:SLUG_MAX_LENGTH].strip("-") or FALLBACK_SLUG


def unique_slug(source: str) -> str:
    """A label no tenant owns and no platform hostname shadows.

    Reserved names are treated exactly like taken ones: a shop called "admin"
    would be unreachable anyway, since the middleware refuses to resolve any
    reserved label as a tenant.
    """
    base = slug_candidate(source)
    candidate = base
    suffix = 1
    while (
        candidate in settings.RESERVED_SUBDOMAINS
        or Tenant.objects.filter(slug=candidate).exists()
    ):
        suffix += 1
        tail = f"-{suffix}"
        candidate = f"{base[: SLUG_MAX_LENGTH - len(tail)].rstrip('-')}{tail}"
    return candidate


@receiver(user_signed_up)
def create_tenant_for_new_user(sender, request, user, **kwargs):
    # allauth re-fires this signal when an existing account gains a new login
    # method. An owner who already has a shop must not quietly collect a second.
    if Membership.objects.filter(user=user).exists():
        return

    local_part = (user.email or "").partition("@")[0]
    display = (user.full_name or local_part).strip() or FALLBACK_SLUG

    # unique_slug only reads; the UNIQUE index is what actually decides. Two
    # signups racing on the same local part both pass the check and one loses,
    # so retry rather than hand the loser a 500 on their first ever request.
    for _attempt in range(5):
        try:
            with transaction.atomic():
                tenant = Tenant.objects.create(
                    name=f"متجر {display}", slug=unique_slug(local_part)
                )
                Membership.objects.create(
                    user=user, tenant=tenant, role=Membership.Role.OWNER
                )
        except IntegrityError:
            continue
        return

    # Signup itself still succeeds: an account with no shop is recoverable from
    # the admin, a failed signup with a verification mail already sent is not.
    logger.error("Could not allocate a tenant slug for %s", user.pk)
