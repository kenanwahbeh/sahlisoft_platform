"""The assistant's tools, and the wire that carries them to a shop's PC.

Every tool here maps 1:1 onto a command kind the on-premise agent already
implements (``worker.py``'s ``_DISPATCH``). That is deliberate and load-bearing:
the agent enforces read-only twice -- a regex that admits a single SELECT/WITH,
and a Firebird READ transaction that makes the server itself reject a write
that somehow got past it. Adding a new command kind for the assistant would put
a second door in a wall with exactly one, so this module adds none. If the
assistant can do it, the dashboard's command surface could already do it.

The transport is unusual and shapes everything else: a tool call becomes an
``AgentCommand`` row, the shop's PC picks it up on its next heartbeat, runs it,
and posts the result back on the one after. That is why the SDK's tool runner
is not used -- it expects to call a local function, not to park a request in a
queue and wait for a machine in another country to answer.
"""

import logging
import time

from django.utils import timezone

from accounts.models import AgentCommand, AgentEnrollment

logger = logging.getLogger(__name__)

#: How long to wait for one command to come back. Generous because it spans a
#: heartbeat round trip over a tunnel, and a shop's connection is not fast --
#: but bounded, because a run that hangs forever holds a thread and tells the
#: owner nothing.
COMMAND_TIMEOUT = 90.0

#: How often to look for the result. The agent writes it from another request,
#: so there is nothing to wait on in-process; this is a poll by necessity.
POLL_INTERVAL = 0.4


class NoAgentAvailable(Exception):
    """No approved, reachable agent for this shop -- nothing can be answered."""


#: Tool definitions. Descriptions are deliberately prescriptive about *when* to
#: call each one, not just what it does: recent models reach for tools
#: conservatively, and "call this when..." measurably lifts the should-call
#: rate over a description that only states capability.
TOOLS = [
    {
        "name": "list_databases",
        "description": (
            "List the Sahlisoft database files on the shop's machine. Call this "
            "FIRST in any conversation that will read data, before any query: "
            "file names differ per shop, most shops keep one file per fiscal "
            "year, and you cannot guess them. Set include_fingerprint=true when "
            "the question names a year or a period, to see which file covers it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "include_fingerprint": {
                    "type": "boolean",
                    "description": (
                        "Also read each file's fiscal-year sites and bill-date "
                        "range. Slower -- it opens every file -- so only when "
                        "the question is period-specific."
                    ),
                }
            },
        },
    },
    {
        "name": "list_tables",
        "description": (
            "List the tables in a database. Call this when you need to confirm a "
            "table exists before querying it: customer databases hold a subset "
            "of the vendor's 413-table schema, and a table present in one shop "
            "may be absent in another."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name_filter": {
                    "type": "string",
                    "description": "Case-insensitive substring, e.g. 'Bill'.",
                }
            },
        },
    },
    {
        "name": "table_schema",
        "description": (
            "Column names, types and foreign keys for one table, with the "
            "vendor's Arabic captions. Call this before writing any query "
            "against a table you have not already inspected in this "
            "conversation -- guessing a column name costs a whole round trip to "
            "the shop's machine, and reading the schema costs none (it is served "
            "from a local dump, not the live database)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "table_name": {"type": "string", "description": "Exact table name."}
            },
            "required": ["table_name"],
        },
    },
    {
        "name": "query",
        "description": (
            "Run one read-only SELECT against a shop database and get rows back. "
            "This is how you actually answer a question about sales, stock, "
            "accounts or invoices. Read-only is enforced on the shop's machine, "
            "so a write cannot succeed even by accident. Prefer one well-aimed "
            "aggregate query over several exploratory ones: each call is a round "
            "trip to a PC in a shop, not a local database."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {
                    "type": "string",
                    "description": (
                        "A single SELECT (or WITH ... SELECT). Identifiers are "
                        'quoted CamelCase: select "MatName" from "Materials".'
                    ),
                },
                "database": {
                    "type": "string",
                    "description": (
                        "Bare file name from list_databases. Omit for the "
                        "default file."
                    ),
                },
                "max_rows": {
                    "type": "integer",
                    "description": "Row cap, 1-2000. Default 200.",
                },
            },
            "required": ["sql"],
        },
    },
    {
        "name": "fingerprint",
        "description": (
            "The fiscal-year sites and bill-date range inside one database file. "
            "Call this when a total has to be attributed to a specific year and "
            "list_databases left it ambiguous -- a single file can hold more "
            "than one fiscal year after an internal rollover."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "database": {"type": "string", "description": "Bare file name."}
            },
        },
    },
]

#: Arabic labels for the progress line the owner sees while a run is in flight.
ACTIVITY_LABELS = {
    "list_databases": "يقرأ قائمة قواعد بيانات متجرك…",
    "list_tables": "يستعرض جداول قاعدة البيانات…",
    "table_schema": "يقرأ بنية الجدول…",
    "query": "ينفّذ استعلاماً على بيانات متجرك…",
    "fingerprint": "يتحقّق من السنة المالية…",
}


def pick_enrollment(tenant) -> AgentEnrollment:
    """The agent that will answer for this shop.

    Prefers one that is demonstrably reachable. An approved-but-dark agent is
    accepted as a fallback rather than refused outright: ``last_seen_at`` can be
    a couple of minutes stale for a machine that is perfectly fine, and failing
    the whole conversation on that would be worse than waiting out one timeout.
    """
    active = list(
        AgentEnrollment.objects.filter(
            tenant=tenant, status=AgentEnrollment.Status.ACTIVE
        ).order_by("-last_seen_at")
    )
    if not active:
        raise NoAgentAvailable(
            "لا يوجد جهاز مفعّل لهذا المتجر. أنشئ عميل ربط واعتمده من لوحة "
            "التحكم أولاً، ثم عد إلى المحادثة."
        )
    for enrollment in active:
        if enrollment.is_online:
            return enrollment
    return active[0]


def run_command(enrollment, kind: str, params: dict, created_by=None) -> dict:
    """Queue one command, wait for the shop's PC to answer, return the result.

    Returns ``{"ok": bool, "result": ..., "error": str}``. Never raises on a
    failed command: a tool that raises ends the run, whereas a tool that reports
    "that table does not exist" lets the model correct itself and carry on,
    which is the whole point of returning errors to the model as content.
    """
    command = AgentCommand.objects.create(
        enrollment=enrollment, kind=kind, params=params or {}, created_by=created_by
    )

    deadline = time.monotonic() + COMMAND_TIMEOUT
    while time.monotonic() < deadline:
        time.sleep(POLL_INTERVAL)
        command.refresh_from_db(fields=["status", "result", "error"])
        if command.status == AgentCommand.Status.DONE:
            return {"ok": True, "result": command.result, "error": ""}
        if command.status == AgentCommand.Status.FAILED:
            return {"ok": False, "result": None, "error": command.error or "فشل الأمر"}

    logger.warning("command %s (%s) timed out waiting for agent", command.pk, kind)
    return {
        "ok": False,
        "result": None,
        "error": (
            "لم يستجب جهاز المتجر خلال المهلة المحددة. تأكّد أنه يعمل ومتصل "
            "بالإنترنت، ثم أعد المحاولة."
        ),
    }


def touch_activity(run, kind: str) -> None:
    """Tell the waiting page what is happening, in words a shop owner reads."""
    run.activity = ACTIVITY_LABELS.get(kind, "يعمل…")
    run.save(update_fields=["activity"])


def finish(run, status, error: str = "") -> None:
    run.status = status
    run.error = error[:600]
    run.activity = ""
    run.finished_at = timezone.now()
    run.save(update_fields=["status", "error", "activity", "finished_at"])
