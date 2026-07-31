"""Root URL configuration.

allauth owns everything under /accounts/ (login, logout, signup, password
reset); our own app claims the root so "/" can route people to the right
place. It is included last so allauth's patterns always win.

api/agent/ is the on-premise agent's machine-to-machine contract -- see
accounts/api_urls.py and accounts/api_views.py.
"""

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    path("accounts/", include("allauth.urls")),
    path("api/agent/", include("accounts.api_urls")),
    path("", include("accounts.urls")),
]
