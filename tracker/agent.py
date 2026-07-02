"""Groq-backed finance chat agent. Answers natural-language questions about the
money-tracker data (spending, categories, balances) using read-only SQL tools.
Mirrors the homelab-hub System Status agent; conversations are logged to the same
homelab-hub AgentCall table so the hub can monitor them."""
import json
import logging
import re
import time

from django.conf import settings
from django.db import connection, connections

_log = logging.getLogger("tracker.agent")

_BASE_URL = "https://api.groq.com/openai/v1"
_MODEL = "llama-3.3-70b-versatile"
_MAX_TOOL_ROUNDS = 8
_SQL_ROW_LIMIT = 100

_FORBIDDEN_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|grant|revoke|create|comment|copy|"
    r"vacuum|reindex|merge|call|do|set|begin|commit|rollback)\b",
    re.IGNORECASE,
)

_SYSTEM_PROMPT = """You are the Money Tracker finance assistant. You help the owner \
understand their personal finances from a self-hosted Quicken-style app.

You can query the application's PostgreSQL database read-only: list tables, describe a \
table, and run SELECT queries. Use list_tables / describe_table to discover the schema \
before writing SQL.

Key tables (Django app 'tracker'):
- tracker_transaction: id, date, amount (signed; negative = spend, positive = income), \
payee, memo, status (uncleared/cleared/reconciled), account_id, category_id.
- tracker_category: id, name, parent_id (top-level category has parent_id NULL). \
Spending is grouped under a category and its subcategories.
- tracker_account: id, name, type, online_balance, current_balance, in_portfolio, \
tax_treatment, institution_id, is_active.
- tracker_institution: id, name.
- tracker_transactionsplit: transaction_id, category_id, amount (a transaction can be \
split across multiple categories).
- tracker_portfoliosnapshot / tracker_networthsnapshot: point-in-time balances.

Net worth / totals — match the app's pages exactly:
- NET WORTH = SUM(online_balance) over active accounts (tracker_account WHERE is_active = true). \
Use online_balance, NOT current_balance, and NOT current_balance summed. Credit cards are \
already negative. This equals the figure on the Net Worth and dashboard pages.
- PORTFOLIO total = SUM(online_balance) over active accounts WHERE in_portfolio = true.
- PROPERTY total = SUM(online_balance) over active accounts WHERE type = 'property'.
- The latest saved Net Worth snapshot is the newest row in tracker_networthsnapshot \
(net_worth column); "current"/"live" net worth is the SUM(online_balance) above.

Spend questions (e.g. "how much did I spend on groceries last year"):
- ALWAYS use the category_spend tool. Do NOT write SQL for category spend — a raw \
SUM(amount) over tracker_transaction MISSES split transactions and gives wrong totals. \
category_spend is splits-aware and includes subcategories, matching the Category Report.
- Pass year=YYYY for a full calendar year ("last year" = previous calendar year, "this \
year" = current year), or start/end (end EXCLUSIVE) for custom ranges.
- Report the 'spend' field as a positive dollar figure and name the category and period.

Be concise and factual. Lead with the answer (a dollar amount and the period). If you \
lack data, say so rather than guessing. Keep SQL to simple read-only SELECTs."""


def _client():
    key = getattr(settings, "GROQ_API_KEY", "")
    if not key:
        return None
    from openai import OpenAI
    return OpenAI(api_key=key, base_url=_BASE_URL)


# ---------------- Tools ----------------

def _tool_list_tables(args):
    with connection.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' ORDER BY table_name"
        )
        return {"tables": [r[0] for r in cur.fetchall()]}


def _tool_describe_table(args):
    table = (args or {}).get("table", "")
    if not re.fullmatch(r"[A-Za-z0-9_]+", table or ""):
        return {"error": "Invalid table name."}
    with connection.cursor() as cur:
        cur.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = 'public' AND table_name = %s ORDER BY ordinal_position",
            [table],
        )
        cols = [{"column": r[0], "type": r[1]} for r in cur.fetchall()]
    if not cols:
        return {"error": f"Table '{table}' not found."}
    return {"table": table, "columns": cols}


def _tool_run_sql(args):
    query = ((args or {}).get("query") or "").strip().rstrip(";")
    if not query:
        return {"error": "Empty query."}
    if not re.match(r"^\s*(select|with)\b", query, re.IGNORECASE):
        return {"error": "Only SELECT queries are allowed."}
    if ";" in query:
        return {"error": "Multiple statements are not allowed."}
    if _FORBIDDEN_SQL.search(query):
        return {"error": "Query contains a disallowed keyword. Read-only SELECT only."}
    if not re.search(r"\blimit\b", query, re.IGNORECASE):
        query = f"{query} LIMIT {_SQL_ROW_LIMIT}"
    try:
        with connection.cursor() as cur:
            cur.execute("SET LOCAL statement_timeout = 8000")
            cur.execute(query)
            columns = [c[0] for c in cur.description]
            rows = cur.fetchmany(_SQL_ROW_LIMIT)
        data = [dict(zip(columns, row)) for row in rows]
        return {"columns": columns, "row_count": len(data), "rows": data}
    except Exception as e:
        return {"error": str(e)}


def _tool_category_spend(args):
    """Splits-aware net + spend for a category (and its subcategories) over [start, end).
    Mirrors the Category Report exactly. Prefer this over run_sql for spend questions."""
    from decimal import Decimal
    from django.db.models import Sum
    from .models import Category, Transaction, TransactionSplit
    a = args or {}
    name = (a.get("category") or "").strip()
    if not name:
        return {"error": "category is required"}
    cat = (Category.objects.filter(name__iexact=name).first()
           or Category.objects.filter(name__icontains=name).order_by("parent_id").first())
    if not cat:
        return {"error": f"No category matching '{name}'."}
    # descendant ids (this category + all children, recursively)
    ids, stack = [cat.id], list(cat.children.all())
    while stack:
        c = stack.pop()
        ids.append(c.id)
        stack.extend(c.children.all())

    start, end, year = a.get("start"), a.get("end"), a.get("year")
    if year and not start and not end:
        start, end = f"{int(year)}-01-01", f"{int(year) + 1}-01-01"

    direct = Transaction.objects.filter(category_id__in=ids, splits__isnull=True)
    split = TransactionSplit.objects.filter(category_id__in=ids)
    if start:
        direct = direct.filter(date__gte=start)
        split = split.filter(transaction__date__gte=start)
    if end:  # end-exclusive, like the report
        direct = direct.filter(date__lt=end)
        split = split.filter(transaction__date__lt=end)
    net = (direct.aggregate(s=Sum("amount"))["s"] or Decimal("0")) + \
          (split.aggregate(s=Sum("amount"))["s"] or Decimal("0"))
    return {
        "category": cat.name,
        "included_subcategories": [Category.objects.get(id=i).name for i in ids],
        "start": start, "end": end,
        "net": float(net),                    # signed (spend is negative)
        "spend": float(-net) if net < 0 else 0.0,   # positive dollars spent
        "income": float(net) if net > 0 else 0.0,
    }


_TOOLS = {
    "category_spend": _tool_category_spend,
    "list_tables": _tool_list_tables,
    "describe_table": _tool_describe_table,
    "run_sql": _tool_run_sql,
}

_TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "category_spend",
        "description": "Splits-aware total spend/income for a category and ALL its subcategories over a date range. Matches the Category Report exactly. ALWAYS use this for 'how much did I spend on <category>' questions instead of run_sql — raw SUM(amount) misses split transactions. Returns net (signed), spend (positive dollars), and the subcategories included.",
        "parameters": {"type": "object", "properties": {
            "category": {"type": "string", "description": "Category name, e.g. 'Groceries' (case-insensitive; subcategories are included automatically)"},
            "year": {"type": "integer", "description": "Calendar year, e.g. 2025. Convenience for a full year."},
            "start": {"type": "string", "description": "Start date YYYY-MM-DD, inclusive (overrides year)."},
            "end": {"type": "string", "description": "End date YYYY-MM-DD, EXCLUSIVE (overrides year)."},
        }, "required": ["category"]},
    }},
    {"type": "function", "function": {
        "name": "list_tables",
        "description": "List all tables in the application PostgreSQL database (public schema).",
        "parameters": {"type": "object", "properties": {}},
    }},
    {"type": "function", "function": {
        "name": "describe_table",
        "description": "Return the column names and types for a given database table.",
        "parameters": {"type": "object",
                       "properties": {"table": {"type": "string", "description": "Table name"}},
                       "required": ["table"]},
    }},
    {"type": "function", "function": {
        "name": "run_sql",
        "description": "Run a single read-only SELECT query against the PostgreSQL database and return rows. No INSERT/UPDATE/DELETE/DDL. Results capped at 100 rows.",
        "parameters": {"type": "object",
                       "properties": {"query": {"type": "string", "description": "A single SELECT statement"}},
                       "required": ["query"]},
    }},
]


def _serialize(obj):
    return json.dumps(obj, default=str)


def _record(question, result, rounds, trace, duration_ms):
    """Persist locally, then mirror in real time into homelab-hub's dashboard_agentcall
    (if configured) so the hub telemetry page shows it the instant the call finishes."""
    local_id = None
    try:
        from .models import AgentCall
        call = AgentCall.objects.create(
            question=question or "",
            reply=result.get("reply") or "",
            error=result.get("error") or "",
            rounds=rounds,
            tool_calls=trace,
            duration_ms=duration_ms,
        )
        local_id = call.id
    except Exception:
        _log.exception("failed to persist AgentCall")

    if "homelab" not in connections.databases:
        return
    from django.utils import timezone
    try:
        with connections["homelab"].cursor() as cur:
            cur.execute(
                "INSERT INTO dashboard_agentcall "
                "(created_at, source, source_id, question, reply, error, rounds, tool_calls, duration_ms) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (source, source_id) DO NOTHING",
                [timezone.now(), "money-tracker", local_id, question or "",
                 result.get("reply") or "", result.get("error") or "",
                 rounds, _serialize(trace), duration_ms],
            )
    except Exception:
        _log.exception("failed to mirror AgentCall to homelab-hub")


def answer_question(history):
    """history: list of {role, content}. Returns {'reply': str, 'error': str|None}."""
    client = _client()
    if client is None:
        return {"reply": "", "error": "Agent is not configured (missing GROQ_API_KEY)."}

    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    for turn in history[-12:]:
        role = turn.get("role")
        content = turn.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})

    last_user = next((t.get("content") for t in reversed(history) if t.get("role") == "user"), "")
    _log.info("agent start: %r", last_user)

    trace = []
    rounds = 0
    retried_malformed = False
    start = time.monotonic()

    try:
        for round_idx in range(_MAX_TOOL_ROUNDS):
            rounds = round_idx + 1
            try:
                resp = client.chat.completions.create(
                    model=_MODEL, messages=messages, tools=_TOOL_SPECS,
                    tool_choice="auto", temperature=0.2, max_tokens=900,
                )
            except Exception as e:
                if "tool_use_failed" in str(e) and not retried_malformed:
                    retried_malformed = True
                    _log.warning("malformed tool call, retrying once: %s", str(e)[:200])
                    trace.append({"round": rounds, "name": "_error", "args": {}, "note": "malformed tool call, retried"})
                    messages.append({
                        "role": "system",
                        "content": "Your previous tool call was malformed. Call exactly ONE tool "
                                   "with a small JSON argument object, or answer in plain text.",
                    })
                    continue
                raise

            msg = resp.choices[0].message

            if not msg.tool_calls:
                result = {"reply": msg.content or "", "error": None}
                _log.info("agent done in %d round(s): %r", rounds, (msg.content or "")[:200])
                _record(last_user, result, rounds, trace, int((time.monotonic() - start) * 1000))
                return result

            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                    for tc in msg.tool_calls
                ],
            })

            for tc in msg.tool_calls:
                fn = _TOOLS.get(tc.function.name)
                try:
                    cargs = json.loads(tc.function.arguments or "{}")
                except Exception:
                    cargs = {}
                _log.info("round %d tool: %s args=%s", rounds, tc.function.name, _serialize(cargs)[:300])
                trace.append({"round": rounds, "name": tc.function.name, "args": cargs})
                tresult = fn(cargs) if fn else {"error": f"Unknown tool {tc.function.name}"}
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": _serialize(tresult)})

        result = {"reply": "I wasn't able to finish answering that — too many tool steps.", "error": None}
        _log.warning("agent hit max rounds (%d) for %r", _MAX_TOOL_ROUNDS, last_user)
        _record(last_user, result, rounds, trace, int((time.monotonic() - start) * 1000))
        return result
    except Exception as e:
        result = {"reply": "", "error": str(e)}
        _log.exception("agent error for %r", last_user)
        _record(last_user, result, rounds, trace, int((time.monotonic() - start) * 1000))
        return result
