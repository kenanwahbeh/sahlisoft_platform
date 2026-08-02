"""Routes for the on-premise agent's HTTP contract.

Included from project/urls.py under ``api/agent/``. See accounts/api_views.py
for why these views are English/JSON rather than Arabic.

``activate`` is listed first because it comes first chronologically, and
because it is the one route here that takes no bearer token -- it is where a
freshly installed machine gets one.
"""

from django.urls import path

from . import api_views

urlpatterns = [
    path("activate/", api_views.agent_activate, name="agent_activate"),
    path("register/", api_views.agent_register, name="agent_register"),
    path("heartbeat/", api_views.agent_heartbeat, name="agent_heartbeat"),
    path("tunnel/", api_views.agent_tunnel, name="agent_tunnel"),
]
