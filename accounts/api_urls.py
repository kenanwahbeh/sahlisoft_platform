"""Routes for the on-premise agent's HTTP contract.

Included from project/urls.py under ``api/agent/``. See accounts/api_views.py
for why these three views are English/JSON rather than Arabic.
"""

from django.urls import path

from . import api_views

urlpatterns = [
    path("register/", api_views.agent_register, name="agent_register"),
    path("heartbeat/", api_views.agent_heartbeat, name="agent_heartbeat"),
    path("tunnel/", api_views.agent_tunnel, name="agent_tunnel"),
]
