from django.urls import path

from . import views

# The slug is a claim, not a credential: every view resolves it through the
# acting user's Membership before doing anything (see views._tenant).
urlpatterns = [
    path("shops/<slug:slug>/assistant/", views.chat, name="assistant_chat"),
    path(
        "shops/<slug:slug>/assistant/new/",
        views.new_conversation,
        name="assistant_new",
    ),
    path(
        "shops/<slug:slug>/assistant/<int:pk>/",
        views.chat_detail,
        name="assistant_chat_detail",
    ),
    path(
        "shops/<slug:slug>/assistant/<int:pk>/ask/",
        views.ask,
        name="assistant_ask",
    ),
    path(
        "shops/<slug:slug>/assistant/<int:pk>/status/",
        views.run_status,
        name="assistant_run_status",
    ),
]
