"""Tenant-facing views.

The only route from a user to a tenant is Membership. Nothing here may ever
touch ``Tenant.objects.all()`` -- on a shared platform that is the difference
between a dashboard and a data leak. In particular no view accepts a tenant
identifier and then looks it up: they all start from the acting user's
memberships and refuse to arrive anywhere the user was not already entitled to.
"""

import logging
import re

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.utils.text import slugify
from django.views.decorators.http import require_POST

from . import tunnel_worker
from .forms import ShopCreateForm
from .models import AgentActivationCode, AgentEnrollment, Membership, Tenant
from .signals import unique_slug

logger = logging.getLogger(__name__)

# The model's choice labels are English source strings and the project ships
# no Arabic .po yet, so the UI wording lives here instead of in the model.
ROLE_LABELS_AR = {
    Membership.Role.OWNER: "مالك",
    Membership.Role.STAFF: "موظف",
    Membership.Role.VIEWER: "مُطّلع",
}

STATUS_LABELS_AR = {
    AgentEnrollment.Status.PENDING: "بانتظار الموافقة",
    AgentEnrollment.Status.ACTIVE: "مفعّل",
    AgentEnrollment.Status.STOPPED: "متوقف",
    AgentEnrollment.Status.REVOKED: "ملغى",
}

# Issuing, approving and revoking a credential are owner acts. Staff and
# viewers can see that an agent exists; they cannot mint one that streams the
# shop's books to a machine of their choosing.
MANAGING_ROLES = {Membership.Role.OWNER}

# A slug is a public hostname, so minting one is not a free action. The ceiling
# is not a business rule about how many shops a merchant may run -- support can
# lift it from the admin -- it is here so a loose script holding one session
# cookie cannot walk off with an unbounded run of subdomains.
MAX_OWNED_SHOPS = 5

SUPPORT_EMAIL = "info@bytebalancetech.com"

# The on-premise agent is not built yet. Enrollment is deliberately still open
# -- the tokens and the approval step are the parts that have to be right, and
# an owner may as well prepare the pairing now -- but every surface that hands
# out a token has to say so, or the UI promises a download that does not exist.
AGENT_RELEASED = False


def tenant_host(tenant) -> str:
    return f"{tenant.slug}.{settings.PLATFORM_DOMAIN}"


def platform_base_url() -> str:
    """Where an agent calls home.

    Built from PLATFORM_DOMAIN rather than stored as its own setting, so it
    cannot drift out of sync with the zone the middleware resolves against.
    "app" is a reserved label, so this is always the platform host and never
    some shop's own subdomain.
    """
    return f"https://app.{settings.PLATFORM_DOMAIN}"


def _managed_tenant(user, slug):
    """Reach the tenant through the membership, never around it.

    Starting the query at Membership means a slug the user has no business
    seeing cannot even be loaded, let alone acted on -- there is no code path
    here that turns a request-supplied identifier into a Tenant on its own.
    404 rather than 403, for the same reason the middleware answers 404: a 403
    would confirm to a stranger that the shop exists.
    """
    membership = (
        Membership.objects.select_related("tenant")
        .filter(user=user, tenant__slug=slug, role__in=MANAGING_ROLES)
        .first()
    )
    if membership is None:
        raise Http404("Unknown shop")
    return membership.tenant


def _enrollment(tenant, pk):
    """Scope the lookup to the tenant, so a stray pk cannot cross shops."""
    enrollment = AgentEnrollment.objects.filter(tenant=tenant, pk=pk).first()
    if enrollment is None:
        raise Http404("Unknown agent")
    return enrollment


def _shop_rows(user):
    memberships = (
        Membership.objects.filter(user=user)
        .select_related("tenant")
        .order_by("tenant__name")
    )
    tenants = [membership.tenant for membership in memberships]

    agents_by_tenant = {}
    for enrollment in AgentEnrollment.objects.filter(tenant__in=tenants):
        agents_by_tenant.setdefault(enrollment.tenant_id, []).append(enrollment)

    rows = []
    for membership in memberships:
        tenant = membership.tenant
        rows.append(
            {
                "tenant": tenant,
                "role": ROLE_LABELS_AR.get(
                    membership.role, membership.get_role_display()
                ),
                "can_manage": membership.role in MANAGING_ROLES,
                "host": tenant_host(tenant),
                "url": f"https://{tenant_host(tenant)}",
                "agents": [
                    {
                        "obj": enrollment,
                        "status_label": STATUS_LABELS_AR.get(
                            enrollment.status, enrollment.get_status_display()
                        ),
                        # Mirrors teardown_and_delete()'s own guard, so the
                        # button is absent rather than merely rejected.
                        "can_delete": enrollment.status
                        in AgentEnrollment.DELETABLE_STATUSES,
                        "can_stop": enrollment.status
                        not in AgentEnrollment.DELETABLE_STATUSES,
                        # A revoked credential is burned; handing out a pairing
                        # file for it would only mint a token that authenticates
                        # into a 403.
                        "can_pair": enrollment.status
                        != AgentEnrollment.Status.REVOKED,
                    }
                    for enrollment in agents_by_tenant.get(tenant.id, [])
                ],
            }
        )
    return rows


def _owned_shop_count(user) -> int:
    return Membership.objects.filter(user=user, role=Membership.Role.OWNER).count()


def _dashboard_context(user, shop_form=None, **extra):
    """Everything dashboard.html needs, from whichever view renders it.

    Three views render that template and only one of them is the dashboard, so
    the empty state's shop form has to be built here or it quietly vanishes
    from the other two.
    """
    context = {
        "shops": _shop_rows(user),
        "shop_form": shop_form if shop_form is not None else ShopCreateForm(user=user),
        "platform_domain": settings.PLATFORM_DOMAIN,
        "support_email": SUPPORT_EMAIL,
        "agent_released": AGENT_RELEASED,
    }
    context.update(extra)
    return context


@login_required
def dashboard(request):
    return render(request, "dashboard.html", _dashboard_context(request.user))


def _slug_source(desired: str, name: str, user) -> str:
    """What to hand ``unique_slug``.

    An all-Arabic shop name slugifies to the empty string, and every such shop
    would otherwise land on the same "shop-N" run. Falling back to the email
    local part gives those owners a label that at least means something, which
    is exactly what signals.py does for a brand new signup.
    """
    source = desired or name
    if slugify(source).strip("-"):
        return source
    return (user.email or "").partition("@")[0]


@login_required
@require_POST
def shop_create(request):
    """Let an owner open their own shop instead of waiting on an operator.

    POST-only: this allocates a live public subdomain, which is not something a
    GET may ever do. On failure the dashboard is re-rendered with the bound form
    rather than redirected to, so the typed values and the Arabic errors survive.

    ``unique_slug`` only reads, so the UNIQUE index is what actually decides;
    two owners racing on the same label both pass the check and one loses. Same
    retry as the signup path, for the same reason.
    """
    if _owned_shop_count(request.user) >= MAX_OWNED_SHOPS:
        messages.error(
            request,
            f"لا يمكن لحساب واحد إنشاء أكثر من {MAX_OWNED_SHOPS} متاجر. "
            f"إن كنت بحاجة إلى المزيد راسلنا على {SUPPORT_EMAIL}.",
        )
        return redirect("dashboard")

    form = ShopCreateForm(request.POST, user=request.user)
    if not form.is_valid():
        return render(
            request,
            "dashboard.html",
            _dashboard_context(request.user, shop_form=form),
            status=400,
        )

    name = form.cleaned_data["name"]
    source = _slug_source(form.cleaned_data["subdomain"], name, request.user)

    for _attempt in range(5):
        try:
            with transaction.atomic():
                tenant = Tenant.objects.create(name=name, slug=unique_slug(source))
                Membership.objects.create(
                    user=request.user, tenant=tenant, role=Membership.Role.OWNER
                )
        except IntegrityError:
            continue

        messages.success(
            request,
            f"تم إنشاء متجر «{tenant.name}» بنجاح. "
            f"عنوانه على المنصة: https://{tenant_host(tenant)}",
        )
        return redirect("dashboard")

    logger.error("Could not allocate a tenant slug for %s", request.user.pk)
    messages.error(
        request,
        "تعذّر إنشاء المتجر في الوقت الحالي. حاول مرة أخرى، "
        f"وإن تكرّر الأمر راسلنا على {SUPPORT_EMAIL}.",
    )
    return redirect("dashboard")


@login_required
@require_POST
def enrollment_create(request, slug):
    """Issue a token. The plaintext lives only in this one response.

    Deliberately no redirect-after-post: a PRG round trip has to park the
    plaintext somewhere in the meantime, and both candidates are worse than the
    problem -- the session backend is a database table, and the message
    framework falls back to a cookie. Rendering it straight into the response
    keeps it out of both. The cost is a re-POST if the owner refreshes, which
    only ever mints a second pending enrollment: visible, and revocable.
    """
    tenant = _managed_tenant(request.user, slug)
    label = (request.POST.get("label") or "").strip()[:120]
    enrollment, token = AgentEnrollment.issue(
        tenant=tenant, label=label, created_by=request.user
    )
    return render(
        request,
        "dashboard.html",
        _dashboard_context(
            request.user,
            issued={"tenant": tenant, "enrollment": enrollment, "token": token},
        ),
    )


@login_required
@require_POST
def enrollment_approve(request, slug, pk):
    """The human half of pairing: a token that nobody vouched for stays inert.

    Also the way back from ``stopped`` -- that state exists precisely to be
    reversible, and the agent resumes on its own once this lands. ``revoked``
    is deliberately not reachable from here: undoing a burned credential by
    clicking "start" is exactly what revoking is supposed to prevent.
    """
    tenant = _managed_tenant(request.user, slug)
    enrollment = _enrollment(tenant, pk)

    resuming = enrollment.status == AgentEnrollment.Status.STOPPED
    if enrollment.status in (AgentEnrollment.Status.PENDING, AgentEnrollment.Status.STOPPED):
        enrollment.approve()
        name = enrollment.label or enrollment.masked_token
        messages.success(
            request,
            f"تم تشغيل العميل «{name}» من جديد."
            if resuming
            else f"تم اعتماد العميل «{name}»، ويمكنه الآن إرسال بيانات المتجر.",
        )
    elif enrollment.status == AgentEnrollment.Status.REVOKED:
        messages.info(request, "هذا العميل ملغى نهائياً؛ أنشئ عميلاً جديداً بدلاً منه.")
    else:
        messages.info(request, "هذا العميل مفعّل أصلاً.")
    return redirect("dashboard")


@login_required
@require_POST
def enrollment_revoke(request, slug, pk):
    tenant = _managed_tenant(request.user, slug)
    enrollment = _enrollment(tenant, pk)

    if enrollment.status != AgentEnrollment.Status.REVOKED:
        enrollment.revoke()
        messages.success(
            request,
            f"تم إلغاء العميل «{enrollment.label or enrollment.masked_token}». "
            "لن يُقبل منه أي اتصال بعد الآن.",
        )
    else:
        messages.info(request, "هذا العميل ملغى أصلاً.")
    return redirect("dashboard")


@login_required
@require_POST
def enrollment_stop(request, slug, pk):
    """Pause an agent without burning its credential.

    Unlike revoke, this is meant to be undone: the agent keeps checking in and
    resumes on its own the moment it is approved again.
    """
    tenant = _managed_tenant(request.user, slug)
    enrollment = _enrollment(tenant, pk)

    if enrollment.status == AgentEnrollment.Status.STOPPED:
        messages.info(request, "هذا العميل متوقف أصلاً.")
    elif enrollment.status == AgentEnrollment.Status.REVOKED:
        messages.info(request, "هذا العميل ملغى، ولا حاجة لإيقافه.")
    else:
        enrollment.stop()
        messages.success(
            request,
            f"تم إيقاف العميل «{enrollment.label or enrollment.masked_token}». "
            "يمكن حذفه الآن، أو اعتماده من جديد لاستئناف العمل.",
        )
    return redirect("dashboard")


@login_required
@require_POST
def enrollment_delete(request, slug, pk):
    """Delete a stopped agent, and its Cloudflare tunnel with it.

    Refusing on a retryable failure is the point: the alternative is dropping
    the row while the tunnel lives on in the Cloudflare account with nothing
    left pointing at it.
    """
    tenant = _managed_tenant(request.user, slug)
    enrollment = _enrollment(tenant, pk)
    name = enrollment.label or enrollment.masked_token

    try:
        enrollment.teardown_and_delete()
    except ValueError:
        messages.error(request, "أوقف العميل أولاً قبل حذفه.")
    except tunnel_worker.NotConfigured:
        messages.error(
            request,
            "تعذّر حذف العميل: إعدادات أنفاق Cloudflare غير مكتملة على المنصة. "
            "لم يُحذف شيء.",
        )
    except tunnel_worker.WorkerError as exc:
        if exc.retryable:
            messages.error(
                request,
                "النفق لا يزال متصلاً ولم يُحذف بعد. لم يُحذف العميل — "
                "أعد المحاولة بعد قليل.",
            )
        else:
            messages.error(
                request,
                f"تعذّر حذف نفق Cloudflare، ولذلك لم يُحذف العميل «{name}». "
                "راجع سجلّات المنصة.",
            )
        logger.warning("tunnel teardown failed for enrollment %s: %s", enrollment.pk, exc)
    else:
        messages.success(request, f"تم حذف العميل «{name}» ونفقه.")
    return redirect("dashboard")


# Anything outside this set is not worth the risk of an odd byte reaching a
# Content-Disposition header or a Windows file name.
_UNSAFE_IN_FILENAME = re.compile(r"[^A-Za-z0-9]+")


def config_filename(tenant, enrollment, stem="sahlisoft-agent") -> str:
    """A name that can only ever belong to this one enrollment.

    Every enrollment for a shop used to produce a byte-identical file name, so
    a second download landed in Downloads as "… (1).env" beside the first --
    two files that look the same, only one of which works. Folding the
    enrollment id (and its label, when it survives the trip to ASCII) into the
    name makes the pair distinguishable at a glance in the file manager.

    ``stem`` exists for the same reason one step further out: the pairing file
    and the hand-configured .env are both .env files for the same enrollment,
    so they need to be told apart in Downloads too.
    """
    parts = [stem, _UNSAFE_IN_FILENAME.sub("-", tenant.slug).strip("-")]
    parts.append(str(enrollment.pk))
    label = _UNSAFE_IN_FILENAME.sub("-", enrollment.label or "").strip("-").lower()
    if label:
        parts.append(label)
    return "-".join(part for part in parts if part) + ".env"


def env_value(raw: str) -> str:
    """Quote only when a naive .env parser would otherwise get it wrong.

    Unconditional quoting is how ``AGENT_LABEL="lenovo"`` ended up meaning a
    label with quote characters in it for anything that just splits on the
    first "=". Arabic and spaces need no quoting; a "#" does, because that is
    where a comment would start.
    """
    value = (raw or "").replace('"', "").replace("\r", " ").replace("\n", " ").strip()
    return f'"{value}"' if "#" in value else value


def render_agent_config(tenant, enrollment, token) -> str:
    """The .env the on-premise agent will read. CRLF: it lands on a Windows box.

    Only ever called with a token that has already been checked against this
    enrollment's digest -- a file with the secret line blank looks exactly like
    a working one and is worse than no file at all, so that case never gets
    this far (see :func:`enrollment_config`).

    ``PLATFORM_URL`` and ``PLATFORM_TOKEN`` are the names the agent's worker.py
    actually reads (its .env.example is the contract). They were once written
    here as ``PLATFORM_BASE_URL`` and ``AGENT_TOKEN``, which the agent ignores
    -- and since it treats a missing token as "platform check-in not wanted"
    and exits that thread silently, the result was a downloaded file that
    looked complete and produced an agent that simply never called home. Keep
    these two names in step with .env.example; the rest of the file is
    informational and nothing parses it.
    """
    host = tenant_host(tenant)

    lines = [
        "# ملف ربط متجرك بمنصة سهل سوفت",
        "# هذا الملف يحمل هوية متجرك ورمز الربط الخاص به.",
        "#",
        "# ملاحظة مهمة: برنامج الوكيل (الذي يقرأ بيانات متجرك ويرسلها إلى المنصة)",
        "# ما زال قيد التجهيز ولم يصدر بعد، لذلك لا يمكن استخدام هذا الملف حالياً.",
        "# عند صدوره ستتمكّن من تنزيله من لوحة التحكم، وسيطلب منك هذا الملف.",
        "#",
        "# احتفظ بالملف في مكان آمن وخاص إلى ذلك الحين.",
        "# تحذير: هذا الملف يحتوي على رمز سري. لا تشاركه ولا ترسله عبر البريد أو واتساب.",
        "",
        "# عنوان المنصة",
        f"PLATFORM_URL={platform_base_url()}",
        "",
        "# معرّف المتجر ونطاقه الفرعي على المنصة",
        f"TENANT_SLUG={tenant.slug}",
        f"TENANT_HOST={host}",
        f"TENANT_URL=https://{host}",
        "",
        "# اسم الجهاز ورقمه كما يظهران في لوحة التحكم",
        f"AGENT_LABEL={env_value(enrollment.label)}",
        f"AGENT_ID={enrollment.pk}",
        "",
        "# رمز الربط: يظهر مرة واحدة فقط عند الإنشاء ولا يمكن استرجاعه لاحقاً.",
        f"PLATFORM_TOKEN={token}",
        "",
        "# بعد صدور برنامج الوكيل وتشغيله بهذا الملف يسجّل نفسه لدى المنصة",
        "# بحالة «بانتظار الموافقة»، ولن يبدأ بإرسال أي بيانات قبل أن تعتمده يدوياً",
        "# من لوحة التحكم: الرمز وحده لا يكفي، والموافقة البشرية هي العامل الثاني للربط.",
        "",
    ]
    return "\r\n".join(lines)


@login_required
@require_POST
def enrollment_config(request, slug, pk):
    """Download the agent's .env.

    POST-only because the token has to travel *in*: the platform cannot look up
    a plaintext it never kept, so the only way this file can carry a real token
    is for the creation page to hand it back from the same browser, in the same
    session, to the same owner.

    When no valid token comes with the request this answers with a page, not a
    file. It used to send the file anyway with ``AGENT_TOKEN=`` left blank and a
    comment explaining why -- which in practice meant a second download sitting
    in Downloads as "… (1).env", indistinguishable from the working copy until
    the agent failed for no visible reason. A broken artifact that looks whole
    is worse than a refusal, so the refusal is what the owner gets, next to the
    one button that actually fixes it.
    """
    tenant = _managed_tenant(request.user, slug)
    enrollment = _enrollment(tenant, pk)

    raw = (request.POST.get("token") or "").strip()
    if not enrollment.check_token(raw):
        return render(
            request,
            "agent_config_unavailable.html",
            {
                "tenant": tenant,
                "enrollment": enrollment,
                "status_label": STATUS_LABELS_AR.get(
                    enrollment.status, enrollment.get_status_display()
                ),
                "support_email": SUPPORT_EMAIL,
                "agent_released": AGENT_RELEASED,
            },
        )

    response = HttpResponse(
        render_agent_config(tenant, enrollment, raw),
        content_type="text/plain; charset=utf-8",
    )
    response["Content-Disposition"] = (
        f'attachment; filename="{config_filename(tenant, enrollment)}"'
    )
    return response


def render_pairing_file(tenant, enrollment, code) -> str:
    """The same .env shape, carrying a coupon where the token would be.

    Deliberately the agent's own .env format rather than a new one: the agent
    already loads it, the installer will already write it, and a bespoke
    pairing format would be a third thing to keep in step for no gain. The
    agent trades ``PLATFORM_ACTIVATION_CODE`` for a real ``PLATFORM_TOKEN`` on
    first run and rewrites this file in place.
    """
    host = tenant_host(tenant)

    lines = [
        "# ملف ربط متجرك بمنصة سهل سوفت",
        "#",
        "# ضع هذا الملف بجانب برنامج الوكيل باسم .env ثم شغّل البرنامج.",
        "# عند أول تشغيل يستبدل البرنامج رمز التفعيل أدناه برمز الربط الدائم",
        "# ويحدّث الملف تلقائياً — لا حاجة لأي خطوة يدوية منك.",
        "#",
        "# رمز التفعيل صالح لمدة ٣٠ دقيقة ولمرة واحدة فقط. إن انتهت صلاحيته",
        "# فنزّل الملف من جديد من لوحة التحكم؛ لا يوجد ما يُفقد.",
        "",
        "# عنوان المنصة",
        f"PLATFORM_URL={platform_base_url()}",
        "",
        "# معرّف المتجر ونطاقه الفرعي على المنصة",
        f"TENANT_SLUG={tenant.slug}",
        f"TENANT_HOST={host}",
        f"TENANT_URL=https://{host}",
        "",
        "# اسم الجهاز ورقمه كما يظهران في لوحة التحكم",
        f"AGENT_LABEL={env_value(enrollment.label)}",
        f"AGENT_ID={enrollment.pk}",
        "",
        "# رمز التفعيل — مؤقت، ويُستبدل تلقائياً عند أول تشغيل",
        f"PLATFORM_ACTIVATION_CODE={code}",
        "",
        "# بعد التفعيل يسجّل البرنامج نفسه بحالة «بانتظار الموافقة»، ولن يرسل",
        "# أي بيانات قبل أن تعتمده يدوياً من لوحة التحكم: الرمز وحده لا يكفي،",
        "# والموافقة البشرية هي العامل الثاني للربط.",
        "",
    ]
    return "\r\n".join(lines)


@login_required
@require_POST
def enrollment_installer(request, slug, pk):
    """Download a pairing file for an agent, at any time, as often as needed.

    A sibling of :func:`enrollment_config`, not a replacement -- that one hands
    over the long-lived token itself and therefore only works in the single
    response that minted it. This one mints a fresh short-lived activation code
    per download, so an owner who closed the tab, or is setting the PC up next
    week, is never told their only cure is a whole new enrollment.

    That the artifact is a text file today and a signed installer later is a
    packaging detail: the bytes handed over are the same either way, which is
    the point of putting a coupon in them rather than a credential.

    Downloading again burns the previous code (see AgentActivationCode.issue).
    The enrollment's status is untouched -- a pairing file is not approval.
    """
    tenant = _managed_tenant(request.user, slug)
    enrollment = _enrollment(tenant, pk)

    if enrollment.status == AgentEnrollment.Status.REVOKED:
        messages.error(
            request,
            "هذا العميل ملغى، ولا يمكن إصدار ملف ربط له. أنشئ عميلاً جديداً.",
        )
        return redirect("dashboard")

    _, code = AgentActivationCode.issue(enrollment)

    response = HttpResponse(
        render_pairing_file(tenant, enrollment, code),
        content_type="text/plain; charset=utf-8",
    )
    response["Content-Disposition"] = (
        f'attachment; filename="{config_filename(tenant, enrollment, "sahlisoft-pairing")}"'
    )
    return response


def home(request):
    """Entry point: send people wherever they can actually go.

    The marketing page belongs to the platform host alone. On a shop's own
    subdomain "/" is that shop's front door, and whoever lands there has already
    chosen a shop -- pitching the product at them would be noise, so that host
    keeps the straight-to-login behaviour it has always had.
    """
    if request.user.is_authenticated:
        return redirect("dashboard")
    if getattr(request, "tenant", None) is not None:
        return redirect("account_login")
    return render(request, "landing.html")
