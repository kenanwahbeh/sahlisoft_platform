"""Tenant-facing views.

The only route from a user to a tenant is Membership. Nothing here may ever
touch ``Tenant.objects.all()`` -- on a shared platform that is the difference
between a dashboard and a data leak. In particular no view accepts a tenant
identifier and then looks it up: they all start from the acting user's
memberships and refuse to arrive anywhere the user was not already entitled to.
"""

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from .models import AgentEnrollment, Membership

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
    AgentEnrollment.Status.REVOKED: "ملغى",
}

# Issuing, approving and revoking a credential are owner acts. Staff and
# viewers can see that an agent exists; they cannot mint one that streams the
# shop's books to a machine of their choosing.
MANAGING_ROLES = {Membership.Role.OWNER}


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
                    }
                    for enrollment in agents_by_tenant.get(tenant.id, [])
                ],
            }
        )
    return rows


@login_required
def dashboard(request):
    return render(request, "dashboard.html", {"shops": _shop_rows(request.user)})


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
        {
            "shops": _shop_rows(request.user),
            "issued": {"tenant": tenant, "enrollment": enrollment, "token": token},
        },
    )


@login_required
@require_POST
def enrollment_approve(request, slug, pk):
    """The human half of pairing: a token that nobody vouched for stays inert."""
    tenant = _managed_tenant(request.user, slug)
    enrollment = _enrollment(tenant, pk)

    if enrollment.status == AgentEnrollment.Status.PENDING:
        enrollment.approve()
        messages.success(
            request,
            f"تم اعتماد العميل «{enrollment.label or enrollment.masked_token}»، "
            "ويمكنه الآن إرسال بيانات المتجر.",
        )
    else:
        messages.info(request, "هذا العميل ليس بانتظار الموافقة.")
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


def render_agent_config(tenant, enrollment, token) -> str:
    """The .env the on-premise agent reads. CRLF: it lands on a Windows box."""
    host = tenant_host(tenant)
    label = (enrollment.label or "").replace('"', "")

    lines = [
        "# ملف إعداد عميل سهل سوفت (Sahlisoft Agent)",
        "# ضعه بجانب برنامج العميل على جهاز نقطة البيع باسم: .env",
        "# تحذير: هذا الملف يحتوي على رمز سري. لا تشاركه ولا ترسله عبر البريد أو واتساب.",
        "",
        "# عنوان المنصة التي يتصل بها العميل",
        f"PLATFORM_BASE_URL={platform_base_url()}",
        "",
        "# معرّف المتجر ونطاقه الفرعي على المنصة",
        f"TENANT_SLUG={tenant.slug}",
        f"TENANT_HOST={host}",
        f"TENANT_URL=https://{host}",
        "",
        "# اسم الجهاز كما يظهر في لوحة التحكم",
        f'AGENT_LABEL="{label}"',
        f"AGENT_ID={enrollment.pk}",
        "",
    ]

    if token:
        lines += [
            "# رمز الربط: يظهر مرة واحدة فقط عند الإنشاء ولا يمكن استرجاعه لاحقاً.",
            f"AGENT_TOKEN={token}",
        ]
    else:
        # Reaching this branch means the plaintext is already gone for good.
        lines += [
            "# رمز الربط غير متوفر: المنصة لا تحتفظ به، ولا يمكن استرجاعه بعد إنشائه.",
            "# أنشئ رمز ربط جديداً من لوحة التحكم، ثم انسخه إلى السطر التالي.",
            "AGENT_TOKEN=",
        ]

    lines += [
        "",
        "# بعد تشغيل العميل بهذا الملف يسجّل نفسه لدى المنصة بحالة «بانتظار الموافقة».",
        "# لن يبدأ بإرسال أي بيانات قبل أن تعتمده يدوياً من لوحة التحكم:",
        "# الرمز وحده لا يكفي، والموافقة البشرية هي العامل الثاني للربط.",
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
    session, to the same owner. It is checked against the stored digest first,
    so the download can never carry a token this enrollment does not own -- and
    when no valid token comes with the request the file is still useful, just
    with the secret line blank and an Arabic note saying why.
    """
    tenant = _managed_tenant(request.user, slug)
    enrollment = _enrollment(tenant, pk)

    raw = (request.POST.get("token") or "").strip()
    token = raw if enrollment.check_token(raw) else ""

    response = HttpResponse(
        render_agent_config(tenant, enrollment, token),
        content_type="text/plain; charset=utf-8",
    )
    response["Content-Disposition"] = (
        f'attachment; filename="sahlisoft-agent-{tenant.slug}.env"'
    )
    return response


def home(request):
    """Entry point: send people wherever they can actually go."""
    if request.user.is_authenticated:
        return redirect("dashboard")
    return redirect("account_login")
