"""Bind each request to the shop named by its subdomain.

One Apache vhost answers for *.bytebalancetech.com, so the Host header is the
only thing distinguishing one shop from another. That makes this middleware
the boundary of the whole tenancy model: if it lets a request through with the
wrong tenant, every view behind it leaks another shop-s data.

Two separate checks, deliberately not merged:
  1. Does this subdomain name an existing, active shop?  -> 404 if not
  2. Is the signed-in user a member of that shop?        -> 404 if not

Check 2 answers 404 rather than 403 on purpose: a 403 would confirm that the
shop exists to someone who has no business knowing it.
"""

from django.conf import settings
from django.http import Http404


class TenantResolutionMiddleware:
    """Sets request.tenant, or None on platform-level hosts."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request.tenant = self._resolve(request)
        return self.get_response(request)

    def _resolve(self, request):
        from .models import Membership, Tenant

        host = request.get_host().partition(":")[0].lower().rstrip(".")
        domain = settings.PLATFORM_DOMAIN.lower()

        # Local/dev hosts and anything outside the platform zone are not
        # tenant-scoped. ALLOWED_HOSTS is what keeps this from being a hole.
        if host == domain or not host.endswith("." + domain):
            return None

        label = host[: -(len(domain) + 1)]
        # Only a single label is a shop; deeper names are never valid.
        if "." in label or label in settings.RESERVED_SUBDOMAINS:
            return None

        tenant = Tenant.objects.filter(slug=label, is_active=True).first()
        if tenant is None:
            raise Http404("Unknown shop")

        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            member = Membership.objects.filter(user=user, tenant=tenant).exists()
            if not member and not user.is_superuser:
                # Signed in, but not for this shop. Same answer as a shop that
                # does not exist, so the response reveals nothing either way.
                raise Http404("Unknown shop")

        return tenant
