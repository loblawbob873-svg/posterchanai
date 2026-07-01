"""Client for the self-hosted Budget Manager finance app (Flask, /api/v1/*).

The finance app is multi-user: each PosterChanAI user connects their own finance
account by pasting that account's API key (User.finance_api_key). The base URL is a
single global admin setting (finance_api_base, default http://localhost:5001) since all
finance accounts live on the same instance.

Shared by the web UI, Telegram, and Matrix bots via command_service. Routers/bots call
the high-level helpers (summary/bills/add/pay) which return a common dict shape; the
text/HTML formatting helpers render that for plain-text channels (web + Matrix), while
Telegram builds its own inline-button UI from the raw dicts.
"""
import logging
from typing import Optional

import httpx
from sqlalchemy.orm import Session

from app.models import User
from app.services import settings_store

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://localhost:5001"
_TIMEOUT = 15.0


class FinanceError(Exception):
    """User-facing finance error (bad config, API error, not found, ambiguous match)."""


def _base_url(db: Session) -> str:
    return (settings_store.get("finance_api_base") or "").strip() or DEFAULT_BASE_URL


def get_config(db: Session, user: User) -> tuple[str, str]:
    """Resolve (base_url, api_key) for this user, or raise FinanceError with guidance."""
    api_key = (getattr(user, "finance_api_key", None) or "").strip()
    if not api_key:
        raise FinanceError(
            "Your finance account isn't connected yet. Open Settings → Finance in the web "
            "UI and paste your Budget Manager API key (Finance → API Keys)."
        )
    return _base_url(db).rstrip("/"), api_key


async def _request(method: str, base_url: str, api_key: str, path: str, json: dict = None) -> dict:
    headers = {"X-API-Key": api_key}
    url = f"{base_url}{path}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.request(method, url, headers=headers, json=json)
    except httpx.RequestError as e:
        logger.warning(f"[finance] request error {method} {path}: {e}")
        raise FinanceError("Couldn't reach the finance service. Is it running?")
    if resp.status_code == 401:
        raise FinanceError("Finance API key is invalid. Re-paste it in Settings → Finance.")
    try:
        data = resp.json()
    except ValueError:
        raise FinanceError(f"Finance service returned an unexpected response ({resp.status_code}).")
    if resp.status_code >= 400 or data.get("error"):
        raise FinanceError(data.get("error") or f"Finance request failed ({resp.status_code}).")
    return data


# --- high-level operations (return raw dicts/lists) -------------------------

async def get_summary(base_url: str, api_key: str) -> dict:
    return await _request("GET", base_url, api_key, "/api/v1/summary")


async def get_bills(base_url: str, api_key: str, status: Optional[str] = None) -> list[dict]:
    path = "/api/v1/bills"
    if status:
        path += f"?status={status}"
    data = await _request("GET", base_url, api_key, path)
    return data.get("bills", [])


async def add_bill(base_url: str, api_key: str, name: str, amount: float,
                   is_income: bool = False, is_recurring: bool = True) -> dict:
    return await _request("POST", base_url, api_key, "/api/v1/bill", json={
        "name": name,
        "amount": amount,
        "is_income": is_income,
        "is_recurring": is_recurring,
    })


async def pay_bill(base_url: str, api_key: str, name: str) -> dict:
    return await _request("POST", base_url, api_key, "/api/v1/bill/pay", json={"name": name})


# --- formatting (plain text for web UI + Matrix) ----------------------------

def format_summary(s: dict, unpaid_count: int | None = None) -> str:
    # `bills_count` from the API counts ALL bills, not just unpaid ones — so the "(N)" beside Unpaid
    # bills was wrong (e.g. showed 8 when only 4 were unpaid). When the caller passes the real unpaid
    # count (derived from the unpaid-bills list), use it; else fall back to the API field.
    count = unpaid_count if unpaid_count is not None else s.get('bills_count', 0)
    return (
        f"💰 Budget — {s.get('month', '')}\n"
        f"━━━━━━━━━━━━━━\n"
        f"Income:        ${s.get('total_income', 0):,.2f}\n"
        f"Unpaid bills:  ${s.get('unpaid_bills', 0):,.2f} ({count})\n"
        f"Remaining:     ${s.get('remaining', 0):,.2f}"
    )


def format_bills(bills: list[dict], header: str = "Bills") -> str:
    if not bills:
        return f"{header}: none."
    lines = [f"📋 {header}"]
    for b in bills:
        mark = "✅" if b.get("paid") else "⬜"
        kind = "＋" if b.get("is_income") else "－"
        lines.append(f"{mark} {kind}${abs(b.get('amount', 0)):,.2f}  {b.get('name', '?')}")
    return "\n".join(lines)


def parse_add_bill_arg(arg: str) -> tuple[str, float, bool]:
    """Parse 'addbill <name> <amount> [income]' → (name, amount, is_income).

    The amount is the last numeric token; everything before it is the name. A trailing
    'income' (or '+') flag marks it as income. Raises FinanceError on bad input.
    """
    tokens = arg.strip().split()
    if not tokens:
        raise FinanceError("Usage: addbill <name> <amount> [income]")
    is_income = False
    if tokens[-1].lower() in ("income", "+", "in"):
        is_income = True
        tokens = tokens[:-1]
    if len(tokens) < 2:
        raise FinanceError("Usage: addbill <name> <amount> [income]")
    amount_raw = tokens[-1].lstrip("$").replace(",", "")
    try:
        amount = float(amount_raw)
    except ValueError:
        raise FinanceError(f"'{tokens[-1]}' isn't a valid amount. Usage: addbill <name> <amount> [income]")
    name = " ".join(tokens[:-1]).strip()
    if not name:
        raise FinanceError("Bill needs a name. Usage: addbill <name> <amount> [income]")
    return name, amount, is_income
