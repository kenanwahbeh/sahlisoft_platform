from django.urls import path

from . import views

# The slug in the path is a claim, not a credential: every one of these views
# resolves it through the acting user's Membership before doing anything.
urlpatterns = [
    path("", views.home, name="home"),
    path("dashboard/", views.dashboard, name="dashboard"),
    # No slug to resolve here -- this one *creates* the membership, so the only
    # thing it trusts is request.user.
    path("shops/new/", views.shop_create, name="shop_create"),
    path(
        "shops/<slug:slug>/agents/new/",
        views.enrollment_create,
        name="enrollment_create",
    ),
    path(
        "shops/<slug:slug>/agents/<int:pk>/approve/",
        views.enrollment_approve,
        name="enrollment_approve",
    ),
    path(
        "shops/<slug:slug>/agents/<int:pk>/revoke/",
        views.enrollment_revoke,
        name="enrollment_revoke",
    ),
    path(
        "shops/<slug:slug>/agents/<int:pk>/stop/",
        views.enrollment_stop,
        name="enrollment_stop",
    ),
    # Deleting also tears down the shop's Cloudflare tunnel, and refuses if it
    # cannot -- see AgentEnrollment.teardown_and_delete.
    path(
        "shops/<slug:slug>/agents/<int:pk>/delete/",
        views.enrollment_delete,
        name="enrollment_delete",
    ),
    path(
        "shops/<slug:slug>/agents/<int:pk>/config/",
        views.enrollment_config,
        name="enrollment_config",
    ),
]
