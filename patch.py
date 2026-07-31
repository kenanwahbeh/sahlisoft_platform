"""Idempotent, in-place edits to files too long to retype safely.

Run from the project root. Each step asserts its anchor exists exactly once and
does nothing if the edit is already present.
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent

BASE_CSS = """
    /* ---- agent enrollments (dashboard) ---- */
    .shop-card { margin-bottom: 1.25rem; }
    .shop-head {
      display: flex;
      flex-wrap: wrap;
      gap: .5rem 1rem;
      align-items: flex-start;
      justify-content: space-between;
      margin-bottom: 1.25rem;
    }
    .shop-card h3 { font-size: 1rem; color: var(--muted); margin-bottom: .6rem; }
    .actions { display: flex; flex-wrap: wrap; gap: .5rem; align-items: center; }
    .actions form { margin: 0; }
    .btn-small { padding: .25rem .7rem; font-size: .85rem; }
    .badge-wait { background: #fdf1dc; color: #8a5b12; }
    .new-agent { margin: 1.25rem 0 0; }
    .new-agent .actions input[type="text"] { flex: 1 1 15rem; width: auto; }

    /* The one-time token is loud on purpose: nothing can bring it back. */
    .token-panel { border-color: var(--accent); margin-bottom: 1.5rem; }
    .token-box {
      margin: 0 0 1rem;
      padding: .8rem 1rem;
      background: var(--bg);
      border: 1px dashed var(--border);
      border-radius: var(--radius);
      font-family: Consolas, "Courier New", monospace;
      font-size: .95rem;
      white-space: pre-wrap;
      word-break: break-all;
      user-select: all;
    }

    /* allauth's own pages render straight into <main> with no markup of ours
       to hang a card on, so the card is the main element itself. */
    main.auth-page {
      max-width: 480px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      padding: 1.5rem;
      margin-block: 2rem 3rem;
    }
    main.auth-page h1 { font-size: 1.35rem; }
    main.auth-page button, main.auth-page input[type="submit"] {
      font: inherit;
      font-weight: 600;
      padding: .5rem 1.1rem;
      border-radius: var(--radius);
      border: 1px solid transparent;
      cursor: pointer;
      background: var(--accent);
      color: #fff;
    }
    main.auth-page button:hover, main.auth-page input[type="submit"]:hover {
      background: var(--accent-dark);
    }
"""

ADMIN_IMPORT_OLD = "from .models import Membership, Tenant, User"
ADMIN_IMPORT_NEW = "from .models import AgentEnrollment, Membership, Tenant, User"

ADMIN_APPEND = '''

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
'''


def patch(path, steps):
    text = original = path.read_text(encoding="utf-8")
    for anchor, replacement, marker in steps:
        if marker in text:
            print(f"  skip (already applied): {marker[:48]}")
            continue
        count = text.count(anchor)
        if count != 1:
            sys.exit(f"ABORT: {path} has {count} occurrences of {anchor!r}")
        text = text.replace(anchor, replacement)
        print(f"  applied: {marker[:48]}")
    if text != original:
        path.write_text(text, encoding="utf-8")
        print(f"  wrote {path}")


print("templates/base.html")
patch(
    ROOT / "templates" / "base.html",
    [
        (
            "</head>",
            "  {% block extra_head %}{% endblock %}\n</head>",
            "{% block extra_head %}",
        ),
        (
            '<main class="wrap">',
            '<main class="wrap {% block main_class %}{% endblock %}">',
            "{% block main_class %}",
        ),
        (
            "</body>",
            "  {% block extra_body %}{% endblock %}\n</body>",
            "{% block extra_body %}",
        ),
        ("  </style>", BASE_CSS + "  </style>", ".token-panel"),
    ],
)

print("accounts/admin.py")
admin_path = ROOT / "accounts" / "admin.py"
admin_text = admin_path.read_text(encoding="utf-8")
if "AgentEnrollment" not in admin_text:
    assert admin_text.count(ADMIN_IMPORT_OLD) == 1, "admin import anchor missing"
    admin_text = admin_text.replace(ADMIN_IMPORT_OLD, ADMIN_IMPORT_NEW)
    admin_path.write_text(admin_text.rstrip("\n") + "\n" + ADMIN_APPEND, encoding="utf-8")
    print("  applied: AgentEnrollment admin")
else:
    print("  skip (already applied)")
