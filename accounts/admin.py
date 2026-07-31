"""Admin registrations for identity and tenancy.

Django's stock ``UserAdmin`` still names ``username`` in its fieldsets,
list_display, search_fields and ordering. Our User dropped that column, so
every one of those has to be restated here or the admin checks fail with
admin.E108/E116 before a single page renders.
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin
from django.db.models import Count
from django.utils.translation import gettext_lazy as _

from .models import AgentCommand, AgentEnrollment, Membership, Tenant, User


class MembershipInline(admin.TabularInline):
    """Memberships are only ever meaningful next to their tenant."""

    model = Membership
    extra = 0
    autocomplete_fields = ["user"]
    verbose_name = _("membership")
    verbose_name_plural = _("memberships")


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    ordering = ["email"]
    list_display = ["email", "full_name", "phone", "is_active", "is_staff"]
    list_filter = ["is_staff", "is_superuser", "is_active", "groups"]
    search_fields = ["email", "full_name", "phone"]

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        (_("Personal info"), {"fields": ("full_name", "phone")}),
        (
            _("Permissions"),
            {
                "fields": (
                    "is_active",
                    "is_staff",
                    "is_superuser",
                    "groups",
                    "user_permissions",
                )
            },
        ),
        (_("Important dates"), {"fields": ("last_login", "date_joined")}),
    )
    # ``usable_password`` is part of Django's AdminUserCreationForm since 5.1;
    # dropping it from the add form breaks the "no password" path.
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("email", "usable_password", "password1", "password2"),
            },
        ),
    )


@admin.register(Tenant)
class TenantAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "is_active", "member_count", "created_at"]
    list_filter = ["is_active"]
    search_fields = ["name", "slug"]
    ordering = ["name"]
    inlines = [MembershipInline]

    def get_queryset(self, request):
        return super().get_queryset(request).annotate(_member_count=Count("memberships"))

    @admin.display(description=_("members"), ordering="_member_count")
    def member_count(self, obj):
        return obj._member_count


@admin.register(Membership)
class MembershipAdmin(admin.ModelAdmin):
    list_display = ["user", "tenant", "role", "created_at"]
    list_filter = ["role", "tenant"]
    search_fields = ["user__email", "user__full_name", "tenant__name", "tenant__slug"]
    autocomplete_fields = ["user", "tenant"]
    list_select_related = ["user", "tenant"]
    ordering = ["tenant__name", "user__email"]


@admin.register(AgentEnrollment)
class AgentEnrollmentAdmin(admin.ModelAdmin):
    """Read-mostly: there is no way to add a credential here.

    Issuing one has to hand the plaintext back to whoever asked, and an admin
    add-form has nowhere to put it. Support staff approve, revoke and look --
    owners mint from the dashboard.
    """

    list_display = ["label", "tenant", "status", "token_prefix", "last_seen_at", "created_at"]
    list_filter = ["status", "tenant"]
    search_fields = ["label", "token_prefix", "tenant__name", "tenant__slug"]
    autocomplete_fields = ["tenant"]
    list_select_related = ["tenant"]
    readonly_fields = ["token_prefix", "created_at", "approved_at", "last_seen_at", "created_by"]
    ordering = ["-created_at"]

    def has_add_permission(self, request):
        return False


@admin.register(AgentCommand)
class AgentCommandAdmin(admin.ModelAdmin):
    """Manual command dispatch for now -- the dashboard UI is a later phase.

    Support staff can queue one (kind + params) against an active
    enrollment and watch it move queued -> sent -> done/failed as the
    agent's next couple of heartbeats land.
    """

    list_display = ["kind", "enrollment", "status", "created_at", "sent_at", "completed_at"]
    list_filter = ["status", "kind"]
    search_fields = ["enrollment__label", "enrollment__tenant__name"]
    autocomplete_fields = ["enrollment"]
    readonly_fields = ["status", "result", "error", "sent_at", "completed_at", "created_by"]
    ordering = ["-created_at"]

    def save_model(self, request, obj, form, change):
        if not change:
            obj.created_by = request.user
        super().save_model(request, obj, form, change)
