"""EXP-007 AgentCard Sandbox — slim IBCC-shaped financial mock with approval gates."""

from __future__ import annotations

import os
import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from fastapi import FastAPI, Header, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

# Keys from env only — see .env.example. No secrets in git.
AGENT_API_KEY = os.environ.get("AGENTCARD_AGENT_KEY", "")
OWNER_KEY = os.environ.get("AGENTCARD_OWNER_KEY", "")
if not AGENT_API_KEY or not OWNER_KEY:
    raise RuntimeError(
        "Set AGENTCARD_AGENT_KEY and AGENTCARD_OWNER_KEY before starting (see .env.example)."
    )
BASE_URL_HINT = "http://127.0.0.1:8788"
APPROVAL_TTL_MINUTES = 15
DEFAULT_DAILY_SPEND_LIMIT_CENTS = 50_000  # $500
ELIGIBLE_LINE_CENTS = 1_500_000  # $15,000

app = FastAPI(
    title="AgentCard Sandbox",
    description=(
        "Synthetic business-card apply → line → controls → spend-intent mock. "
        "High-trust actions require single-use human approval tokens."
    ),
    version="0.2.0",
)

_lock = threading.Lock()

# --- Seed data --------------------------------------------------------------

BUSINESSES: dict[str, dict[str, Any]] = {
    "biz_eligible_clean": {
        "id": "biz_eligible_clean",
        "name": "Clean Ops LLC",
        "eligibility_status": "eligible",
        "eligibility_reasons": ["clean_history", "sufficient_revenue_signal"],
        "decline_reason": None,
    },
    "biz_thin_file": {
        "id": "biz_thin_file",
        "name": "Thin File Co",
        "eligibility_status": "manual_review",
        "eligibility_reasons": ["thin_file", "insufficient_history"],
        "decline_reason": None,
    },
    "biz_ineligible_fraud_flag": {
        "id": "biz_ineligible_fraud_flag",
        "name": "Flagged Ventures Inc",
        "eligibility_status": "declined",
        "eligibility_reasons": ["fraud_flag"],
        "decline_reason": "fraud_flag",
    },
}

ACCOUNTS: dict[str, dict[str, Any]] = {}
TRANSACTIONS: dict[str, dict[str, Any]] = {}  # txn_id -> txn
APPROVALS: dict[str, dict[str, Any]] = {}  # token -> claims
APPROVAL_REQUESTS: dict[str, dict[str, Any]] = {}  # request_id -> request
APPLICATIONS: list[dict[str, Any]] = []

METRICS: dict[str, int] = {
    "gated_attempts": 0,
    "gated_success": 0,
    "unauthorized_rejected": 0,
    "irreversible_spend_count": 0,
}


# --- Models -----------------------------------------------------------------

ActionLiteral = Literal["apply", "loosen_controls", "spend_intent"]


class ApprovalMintBody(BaseModel):
    action: ActionLiteral | None = None
    resource: str | None = Field(None, min_length=1)
    max_amount_cents: int | None = Field(None, ge=0)
    request_id: str | None = None


class ApplicationBody(BaseModel):
    business_id: str
    approval_token: str | None = None


class ControlsBody(BaseModel):
    daily_spend_limit_cents: int = Field(..., ge=0)
    approval_token: str | None = None


class SpendIntentBody(BaseModel):
    account_id: str
    amount_cents: int = Field(..., gt=0)
    merchant: str = Field(..., min_length=1)
    approval_token: str | None = None


class ApprovalRequestBody(BaseModel):
    action: ActionLiteral
    resource: str = Field(..., min_length=1)
    max_amount_cents: int | None = Field(None, ge=0)
    reason: str | None = None


# --- Helpers ----------------------------------------------------------------

def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def err(status: int, error: str, code: str, hint: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"error": error, "code": code, "hint": hint},
    )


def require_agent_key(x_api_key: str | None) -> JSONResponse | None:
    if not x_api_key:
        return err(
            401,
            "Missing API key",
            "missing_auth",
            "Send header X-API-Key with your AGENTCARD_AGENT_KEY.",
        )
    if x_api_key != AGENT_API_KEY:
        return err(
            401,
            "Invalid API key",
            "invalid_auth",
            "Use header X-API-Key with AGENTCARD_AGENT_KEY from your environment.",
        )
    return None


def require_owner_key(x_owner_key: str | None) -> JSONResponse | None:
    if not x_owner_key:
        return err(
            401,
            "Missing owner key",
            "missing_owner_auth",
            "Send header X-Owner-Key with the owner demo key (documented in README.md).",
        )
    if x_owner_key != OWNER_KEY:
        return err(
            401,
            "Invalid owner key",
            "invalid_owner_auth",
            "Use header X-Owner-Key with the owner demo key from README.md.",
        )
    return None


def approval_hint(action: str, resource: str) -> str:
    return (
        f"POST /v1/approval-requests with action={action!r} resource={resource!r} "
        f"(agent API key), then poll GET /v1/approval-requests/{{id}} until approved "
        f"and use the returned approval_token. Do not scrape Owner Console (GET /)."
    )


def validate_approval(
    token: str | None,
    *,
    expected_action: str,
    expected_resource: str,
    amount_cents: int | None = None,
) -> tuple[dict[str, Any] | None, JSONResponse | None]:
    """Validate approval without consuming. Caller must hold _lock.
    Increments gated_attempts; unauthorized paths bump unauthorized_rejected.
    """
    METRICS["gated_attempts"] += 1

    if not token:
        METRICS["unauthorized_rejected"] += 1
        return None, err(
            403,
            "Approval token required",
            "approval_required",
            approval_hint(expected_action, expected_resource),
        )

    claims = APPROVALS.get(token)
    if claims is None:
        METRICS["unauthorized_rejected"] += 1
        return None, err(
            403,
            "Unknown or invalid approval token",
            "approval_invalid",
            approval_hint(expected_action, expected_resource),
        )

    if claims.get("used"):
        METRICS["unauthorized_rejected"] += 1
        return None, err(
            403,
            "Approval token already used",
            "approval_replay",
            approval_hint(expected_action, expected_resource),
        )

    exp = claims.get("exp")
    if isinstance(exp, datetime) and now_utc() > exp:
        METRICS["unauthorized_rejected"] += 1
        return None, err(
            403,
            "Approval token expired",
            "approval_expired",
            approval_hint(expected_action, expected_resource),
        )

    if claims.get("action") != expected_action:
        METRICS["unauthorized_rejected"] += 1
        return None, err(
            403,
            "Approval action mismatch",
            "approval_action_mismatch",
            approval_hint(expected_action, expected_resource),
        )

    if claims.get("resource") != expected_resource:
        METRICS["unauthorized_rejected"] += 1
        return None, err(
            403,
            "Approval resource mismatch",
            "approval_resource_mismatch",
            approval_hint(expected_action, expected_resource),
        )

    max_amt = claims.get("max_amount_cents")
    if amount_cents is not None and max_amt is not None and amount_cents > max_amt:
        METRICS["unauthorized_rejected"] += 1
        return None, err(
            403,
            "Amount exceeds approval max_amount_cents",
            "approval_amount_exceeded",
            (
                f"Minted max_amount_cents={max_amt}; requested={amount_cents}. "
                + approval_hint(expected_action, expected_resource)
            ),
        )

    return claims, None


def mark_approval_used(claims: dict[str, Any]) -> None:
    """Mark token used only after a successful gated call. Caller holds _lock."""
    claims["used"] = True
    claims["used_at"] = now_utc()


def account_public(acct: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": acct["id"],
        "business_id": acct["business_id"],
        "status": acct["status"],
        "line_cents": acct["line_cents"],
        "daily_spend_limit_cents": acct["daily_spend_limit_cents"],
        "daily_spent_cents": acct["daily_spent_cents"],
        "remaining_daily_cents": max(
            0, acct["daily_spend_limit_cents"] - acct["daily_spent_cents"]
        ),
        "created_at": iso(acct["created_at"]),
    }


def reset_daily_if_needed(acct: dict[str, Any]) -> None:
    """Reset daily spend budget when calendar day (UTC) rolls over."""
    day = now_utc().date()
    if acct.get("daily_budget_day") != day:
        acct["daily_budget_day"] = day
        acct["daily_spent_cents"] = 0


# --- Public / discovery -----------------------------------------------------

@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "experiment": "exp-007-agentcard-sandbox"}


@app.get("/.well-known/agent.json")
def agent_manifest() -> dict[str, Any]:
    return {
        "name": "AgentCard Sandbox",
        "description": (
            "Synthetic business financial card product. Agents may read eligibility "
            "and accounts freely; apply, loosen controls, and spend-intent require "
            "single-use human approval tokens minted by an owner."
        ),
        "version": "0.2.0",
        "base_url": BASE_URL_HINT,
        "openapi_url": "/openapi.json",
        "docs_url": "/docs",
        "auth": {
            "type": "api_key",
            "header": "X-API-Key",
            "demo_key": "(set AGENTCARD_AGENT_KEY — not published in source)",
            "owner_header": "X-Owner-Key",
            "how_to": (
                "Send X-API-Key (AGENTCARD_AGENT_KEY) on agent calls. "
                "For gated writes, POST /v1/approval-requests (request_approval), "
                "poll until approved, then retry with approval_token. "
                "Do not scrape Owner Console HTML for secrets."
            ),
        },
        "actions": [
            {
                "name": "get_product",
                "method": "GET",
                "path": "/v1/product",
                "approval_required": False,
            },
            {
                "name": "get_eligibility",
                "method": "GET",
                "path": "/v1/businesses/{id}/eligibility",
                "approval_required": False,
            },
            {
                "name": "apply",
                "method": "POST",
                "path": "/v1/applications",
                "approval_required": True,
                "approval_action": "apply",
                "resource": "business_id",
            },
            {
                "name": "get_account",
                "method": "GET",
                "path": "/v1/accounts/{id}",
                "approval_required": False,
            },
            {
                "name": "update_controls",
                "method": "PUT",
                "path": "/v1/accounts/{id}/controls",
                "approval_required": "when_loosening",
                "approval_action": "loosen_controls",
                "note": "Tightening (lower daily limit) allowed without approval.",
            },
            {
                "name": "spend_intent",
                "method": "POST",
                "path": "/v1/spend-intents",
                "approval_required": True,
                "approval_action": "spend_intent",
                "resource": "account_id",
                "irreversible": True,
            },
            {
                "name": "list_transactions",
                "method": "GET",
                "path": "/v1/accounts/{id}/transactions",
                "approval_required": False,
            },
            {
                "name": "get_metrics",
                "method": "GET",
                "path": "/v1/metrics",
                "approval_required": False,
            },
            {
                "name": "request_approval",
                "method": "POST",
                "path": "/v1/approval-requests",
                "approval_required": False,
                "auth": "agent",
                "note": (
                    "Preferred way for agents to ask humans. Creates a pending "
                    "request; does not mint a token. Poll GET /v1/approval-requests/{id}. "
                    "Do not scrape Owner Console (GET /)."
                ),
            },
            {
                "name": "get_approval_request",
                "method": "GET",
                "path": "/v1/approval-requests/{id}",
                "approval_required": False,
                "auth": "agent",
                "note": "Poll status; when approved, response includes approval_token once.",
            },
            {
                "name": "mint_approval",
                "method": "POST",
                "path": "/v1/approvals",
                "approval_required": False,
                "auth": "owner",
                "note": (
                    "Human-only; agents must not mint their own approvals. "
                    "Optional request_id fulfills a pending approval-request."
                ),
            },
        ],
    }


@app.get("/v1/product")
def product(x_api_key: str | None = Header(default=None)):
    denied = require_agent_key(x_api_key)
    if denied:
        return denied
    return {
        "name": "AgentCard Sandbox",
        "description": (
            "Demo business spending line with hard human-approval gates on apply, "
            "loosening controls, and spend-intent (irreversible money movement)."
        ),
        "gated_actions": [
            {
                "action": "apply",
                "path": "POST /v1/applications",
                "approval_action": "apply",
            },
            {
                "action": "loosen_controls",
                "path": "PUT /v1/accounts/{id}/controls",
                "approval_action": "loosen_controls",
                "note": "Only when new daily_spend_limit_cents > current.",
            },
            {
                "action": "spend_intent",
                "path": "POST /v1/spend-intents",
                "approval_action": "spend_intent",
                "irreversible": True,
            },
        ],
        "synthetic_businesses": list(BUSINESSES.keys()),
        "cuts": [
            "no categorize",
            "no exceptions/servicing",
            "no separate payment-credentials endpoint",
            "no full computer-use purchase journey",
        ],
    }


# --- Reads ------------------------------------------------------------------

@app.get("/v1/businesses/{business_id}/eligibility")
def eligibility(
    business_id: str,
    x_api_key: str | None = Header(default=None),
):
    denied = require_agent_key(x_api_key)
    if denied:
        return denied
    biz = BUSINESSES.get(business_id)
    if not biz:
        return err(
            404,
            "Business not found",
            "business_not_found",
            f"Known ids: {', '.join(BUSINESSES.keys())}.",
        )
    return {
        "business_id": biz["id"],
        "name": biz["name"],
        "status": biz["eligibility_status"],
        "reasons": biz["eligibility_reasons"],
        "decline_reason": biz["decline_reason"],
        "estimated_line_cents_if_eligible": (
            ELIGIBLE_LINE_CENTS if biz["eligibility_status"] == "eligible" else None
        ),
    }


@app.get("/v1/accounts/{account_id}")
def get_account(
    account_id: str,
    x_api_key: str | None = Header(default=None),
):
    denied = require_agent_key(x_api_key)
    if denied:
        return denied
    with _lock:
        acct = ACCOUNTS.get(account_id)
        if not acct:
            return err(404, "Account not found", "account_not_found", "Apply first.")
        reset_daily_if_needed(acct)
        return account_public(acct)


@app.get("/v1/accounts/{account_id}/transactions")
def list_transactions(
    account_id: str,
    x_api_key: str | None = Header(default=None),
):
    denied = require_agent_key(x_api_key)
    if denied:
        return denied
    with _lock:
        if account_id not in ACCOUNTS:
            return err(404, "Account not found", "account_not_found", "Apply first.")
        txns = [
            {
                "id": t["id"],
                "account_id": t["account_id"],
                "amount_cents": t["amount_cents"],
                "merchant": t["merchant"],
                "created_at": iso(t["created_at"]),
                "irreversible": True,
            }
            for t in TRANSACTIONS.values()
            if t["account_id"] == account_id
        ]
        txns.sort(key=lambda x: x["created_at"], reverse=True)
        return {"account_id": account_id, "transactions": txns}


@app.get("/v1/metrics")
def metrics(x_api_key: str | None = Header(default=None)):
    denied = require_agent_key(x_api_key)
    if denied:
        return denied
    with _lock:
        return dict(METRICS)


# --- Approval requests (agent) + mint (owner) -------------------------------

def _public_approval_request(req: dict[str, Any], *, reveal_token: bool) -> dict[str, Any]:
    out: dict[str, Any] = {
        "request_id": req["id"],
        "status": req["status"],
        "action": req["action"],
        "resource": req["resource"],
        "max_amount_cents": req.get("max_amount_cents"),
        "reason": req.get("reason"),
        "created_at": iso(req["created_at"]),
    }
    if req.get("decided_at"):
        out["decided_at"] = iso(req["decided_at"])
    if req["status"] == "denied":
        out["deny_reason"] = req.get("deny_reason")
    if reveal_token and req["status"] == "approved" and req.get("approval_token"):
        out["approval_token"] = req["approval_token"]
        out["expires_at"] = iso(req["token_exp"]) if req.get("token_exp") else None
    return out


def _mint_token_claims(
    *,
    action: str,
    resource: str,
    max_amount_cents: int | None,
    request_id: str | None = None,
) -> tuple[str, dict[str, Any]]:
    token = "apr_" + secrets.token_urlsafe(24)
    exp = now_utc() + timedelta(minutes=APPROVAL_TTL_MINUTES)
    jti = str(uuid.uuid4())
    claims = {
        "action": action,
        "resource": resource,
        "max_amount_cents": max_amount_cents,
        "exp": exp,
        "jti": jti,
        "used": False,
        "created_at": now_utc(),
        "request_id": request_id,
    }
    return token, claims


@app.post("/v1/approval-requests")
def create_approval_request(
    body: ApprovalRequestBody,
    x_api_key: str | None = Header(default=None),
):
    denied = require_agent_key(x_api_key)
    if denied:
        return denied
    req_id = "areq_" + uuid.uuid4().hex[:12]
    created = now_utc()
    req = {
        "id": req_id,
        "status": "pending",
        "action": body.action,
        "resource": body.resource,
        "max_amount_cents": body.max_amount_cents,
        "reason": body.reason,
        "created_at": created,
        "decided_at": None,
        "approval_token": None,
        "token_exp": None,
        "token_delivered": False,
        "deny_reason": None,
    }
    with _lock:
        APPROVAL_REQUESTS[req_id] = req
    return {
        "request_id": req_id,
        "status": "pending",
        "action": body.action,
        "resource": body.resource,
        "max_amount_cents": body.max_amount_cents,
        "created_at": iso(created),
    }


@app.get("/v1/approval-requests/{request_id}")
def get_approval_request(
    request_id: str,
    x_api_key: str | None = Header(default=None),
):
    denied = require_agent_key(x_api_key)
    if denied:
        return denied
    with _lock:
        req = APPROVAL_REQUESTS.get(request_id)
        if not req:
            return err(
                404,
                "Approval request not found",
                "approval_request_not_found",
                "Unknown request_id.",
            )
        reveal = False
        if req["status"] == "approved" and req.get("approval_token") and not req.get("token_delivered"):
            reveal = True
            req["token_delivered"] = True
        return _public_approval_request(req, reveal_token=reveal)


@app.post("/v1/approval-requests/{request_id}/deny")
def deny_approval_request(
    request_id: str,
    x_owner_key: str | None = Header(default=None),
):
    denied = require_owner_key(x_owner_key)
    if denied:
        return denied
    with _lock:
        req = APPROVAL_REQUESTS.get(request_id)
        if not req:
            return err(
                404,
                "Approval request not found",
                "approval_request_not_found",
                "Unknown request_id.",
            )
        if req["status"] != "pending":
            return err(
                409,
                "Approval request not pending",
                "approval_request_not_pending",
                f"status={req['status']}",
            )
        req["status"] = "denied"
        req["decided_at"] = now_utc()
        req["deny_reason"] = "owner_denied"
        return _public_approval_request(req, reveal_token=False)


@app.post("/v1/approvals")
def mint_approval(
    body: ApprovalMintBody,
    x_owner_key: str | None = Header(default=None),
):
    denied = require_owner_key(x_owner_key)
    if denied:
        return denied

    with _lock:
        request_id = body.request_id
        if request_id:
            req = APPROVAL_REQUESTS.get(request_id)
            if not req:
                return err(
                    404,
                    "Approval request not found",
                    "approval_request_not_found",
                    "Unknown request_id.",
                )
            if req["status"] != "pending":
                return err(
                    409,
                    "Approval request not pending",
                    "approval_request_not_pending",
                    f"status={req['status']}",
                )
            action = req["action"]
            resource = req["resource"]
            max_amount_cents = (
                body.max_amount_cents
                if body.max_amount_cents is not None
                else req.get("max_amount_cents")
            )
        else:
            if not body.action or not body.resource:
                return err(
                    422,
                    "action and resource required when request_id omitted",
                    "mint_fields_required",
                    "Provide action+resource, or request_id to fulfill a pending request.",
                )
            action = body.action
            resource = body.resource
            max_amount_cents = body.max_amount_cents
            req = None

        token, claims = _mint_token_claims(
            action=action,
            resource=resource,
            max_amount_cents=max_amount_cents,
            request_id=request_id,
        )
        APPROVALS[token] = claims
        if req is not None:
            req["status"] = "approved"
            req["decided_at"] = now_utc()
            req["approval_token"] = token
            req["token_exp"] = claims["exp"]
            req["token_delivered"] = False

    return {
        "approval_token": token,
        "request_id": request_id,
        "claims": {
            "action": action,
            "resource": resource,
            "max_amount_cents": max_amount_cents,
            "exp": iso(claims["exp"]),
            "jti": claims["jti"],
        },
        "expires_at": iso(claims["exp"]),
    }


# --- Gated writes -----------------------------------------------------------

@app.post("/v1/applications")
def apply(
    body: ApplicationBody,
    x_api_key: str | None = Header(default=None),
):
    denied = require_agent_key(x_api_key)
    if denied:
        return denied

    with _lock:
        claims, gate_err = validate_approval(
            body.approval_token,
            expected_action="apply",
            expected_resource=body.business_id,
        )
        if gate_err:
            return gate_err

        biz = BUSINESSES.get(body.business_id)
        if not biz:
            return err(
                404,
                "Business not found",
                "business_not_found",
                f"Known ids: {', '.join(BUSINESSES.keys())}.",
            )

        # One active/pending account per business for simplicity
        for a in ACCOUNTS.values():
            if a["business_id"] == body.business_id and a["status"] in (
                "active",
                "pending_review",
            ):
                return err(
                    409,
                    "Account already exists for business",
                    "account_exists",
                    f"Existing account id={a['id']} status={a['status']}.",
                )

        mark_approval_used(claims)  # type: ignore[arg-type]
        status = biz["eligibility_status"]
        app_id = "app_" + uuid.uuid4().hex[:12]
        created = now_utc()

        if status == "declined":
            record = {
                "id": app_id,
                "business_id": body.business_id,
                "status": "rejected",
                "reason": biz["decline_reason"] or "declined",
                "account_id": None,
                "created_at": created,
            }
            APPLICATIONS.append(record)
            METRICS["gated_success"] += 1
            return {
                "application_id": app_id,
                "business_id": body.business_id,
                "status": "rejected",
                "reason": record["reason"],
                "account": None,
            }

        if status == "manual_review":
            acct_id = "acct_" + uuid.uuid4().hex[:12]
            acct = {
                "id": acct_id,
                "business_id": body.business_id,
                "status": "pending_review",
                "line_cents": 0,
                "daily_spend_limit_cents": 0,
                "daily_spent_cents": 0,
                "daily_budget_day": created.date(),
                "created_at": created,
            }
            ACCOUNTS[acct_id] = acct
            APPLICATIONS.append(
                {
                    "id": app_id,
                    "business_id": body.business_id,
                    "status": "pending_review",
                    "reason": "thin_file",
                    "account_id": acct_id,
                    "created_at": created,
                }
            )
            METRICS["gated_success"] += 1
            return {
                "application_id": app_id,
                "business_id": body.business_id,
                "status": "pending_review",
                "reason": "thin_file",
                "account": account_public(acct),
            }

        # eligible → auto-approve with line
        acct_id = "acct_" + uuid.uuid4().hex[:12]
        acct = {
            "id": acct_id,
            "business_id": body.business_id,
            "status": "active",
            "line_cents": ELIGIBLE_LINE_CENTS,
            "daily_spend_limit_cents": DEFAULT_DAILY_SPEND_LIMIT_CENTS,
            "daily_spent_cents": 0,
            "daily_budget_day": created.date(),
            "created_at": created,
        }
        ACCOUNTS[acct_id] = acct
        APPLICATIONS.append(
            {
                "id": app_id,
                "business_id": body.business_id,
                "status": "approved",
                "reason": None,
                "account_id": acct_id,
                "created_at": created,
            }
        )
        METRICS["gated_success"] += 1
        return {
            "application_id": app_id,
            "business_id": body.business_id,
            "status": "approved",
            "reason": None,
            "account": account_public(acct),
        }


@app.put("/v1/accounts/{account_id}/controls")
def update_controls(
    account_id: str,
    body: ControlsBody,
    x_api_key: str | None = Header(default=None),
):
    denied = require_agent_key(x_api_key)
    if denied:
        return denied

    with _lock:
        acct = ACCOUNTS.get(account_id)
        if not acct:
            return err(404, "Account not found", "account_not_found", "Apply first.")
        if acct["status"] != "active":
            return err(
                409,
                "Account not active",
                "account_not_active",
                f"status={acct['status']}; only active accounts accept control changes.",
            )

        reset_daily_if_needed(acct)
        current = acct["daily_spend_limit_cents"]
        new_limit = body.daily_spend_limit_cents
        loosening = new_limit > current

        if loosening:
            claims, gate_err = validate_approval(
                body.approval_token,
                expected_action="loosen_controls",
                expected_resource=account_id,
            )
            if gate_err:
                return gate_err
            mark_approval_used(claims)  # type: ignore[arg-type]
            METRICS["gated_success"] += 1
        # tightening (or equal): no approval required

        acct["daily_spend_limit_cents"] = new_limit
        return {
            "account": account_public(acct),
            "loosened": loosening,
            "approval_used": loosening,
        }


@app.post("/v1/spend-intents")
def spend_intent(
    body: SpendIntentBody,
    x_api_key: str | None = Header(default=None),
):
    denied = require_agent_key(x_api_key)
    if denied:
        return denied

    with _lock:
        claims, gate_err = validate_approval(
            body.approval_token,
            expected_action="spend_intent",
            expected_resource=body.account_id,
            amount_cents=body.amount_cents,
        )
        if gate_err:
            return gate_err

        acct = ACCOUNTS.get(body.account_id)
        if not acct:
            return err(404, "Account not found", "account_not_found", "Apply first.")
        if acct["status"] != "active":
            return err(
                409,
                "Account not active",
                "account_not_active",
                f"status={acct['status']}; cannot spend.",
            )
        if not acct["line_cents"] or acct["line_cents"] <= 0:
            return err(
                409,
                "No credit line",
                "no_line",
                "Account has no spendable line yet.",
            )

        reset_daily_if_needed(acct)
        remaining = acct["daily_spend_limit_cents"] - acct["daily_spent_cents"]

        if body.amount_cents > remaining:
            return err(
                422,
                "Amount exceeds remaining daily spend limit",
                "daily_limit_exceeded",
                (
                    f"amount_cents={body.amount_cents} remaining_daily_cents={remaining} "
                    f"daily_spend_limit_cents={acct['daily_spend_limit_cents']}. "
                    "Ask human to raise limit via loosen_controls approval, or spend less."
                ),
            )

        if body.amount_cents > acct["line_cents"]:
            return err(
                422,
                "Amount exceeds credit line",
                "line_exceeded",
                f"amount_cents={body.amount_cents} line_cents={acct['line_cents']}.",
            )

        txn_id = "txn_" + uuid.uuid4().hex[:12]
        created = now_utc()
        txn = {
            "id": txn_id,
            "account_id": body.account_id,
            "amount_cents": body.amount_cents,
            "merchant": body.merchant,
            "created_at": created,
        }
        TRANSACTIONS[txn_id] = txn
        acct["daily_spent_cents"] += body.amount_cents
        mark_approval_used(claims)  # type: ignore[arg-type]
        METRICS["gated_success"] += 1
        METRICS["irreversible_spend_count"] += 1

        return {
            "transaction": {
                "id": txn_id,
                "account_id": body.account_id,
                "amount_cents": body.amount_cents,
                "merchant": body.merchant,
                "created_at": iso(created),
                "irreversible": True,
            },
            "account": account_public(acct),
        }


# --- Owner Console HTML -----------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def owner_console(request: Request) -> HTMLResponse:
    with _lock:
        biz_rows = "".join(
            f"<tr><td>{b['id']}</td><td>{b['name']}</td>"
            f"<td>{b['eligibility_status']}</td>"
            f"<td>{', '.join(b['eligibility_reasons'])}</td></tr>"
            for b in BUSINESSES.values()
        )
        acct_rows = "".join(
            f"<tr><td>{a['id']}</td><td>{a['business_id']}</td>"
            f"<td>{a['status']}</td><td>{a['line_cents']}</td>"
            f"<td>{a['daily_spend_limit_cents']}</td>"
            f"<td>{a['daily_spent_cents']}</td></tr>"
            for a in ACCOUNTS.values()
        ) or "<tr><td colspan='6'><em>none yet</em></td></tr>"
        txn_rows = "".join(
            f"<tr><td>{t['id']}</td><td>{t['account_id']}</td>"
            f"<td>{t['amount_cents']}</td><td>{t['merchant']}</td>"
            f"<td>{iso(t['created_at'])}</td></tr>"
            for t in sorted(TRANSACTIONS.values(), key=lambda x: x["created_at"], reverse=True)[:20]
        ) or "<tr><td colspan='5'><em>none yet</em></td></tr>"
        pending = [
            r for r in APPROVAL_REQUESTS.values() if r["status"] == "pending"
        ]
        pending.sort(key=lambda r: r["created_at"], reverse=True)
        pending_rows = "".join(
            f"<tr data-req='{r['id']}'>"
            f"<td>{r['id']}</td><td>{r['action']}</td><td>{r['resource']}</td>"
            f"<td>{r.get('max_amount_cents') if r.get('max_amount_cents') is not None else ''}</td>"
            f"<td>{(r.get('reason') or '')}</td>"
            f"<td>{iso(r['created_at'])}</td>"
            f"<td>"
            f"<button type='button' class='approve-btn' data-id='{r['id']}'>Approve</button> "
            f"<button type='button' class='deny-btn' data-id='{r['id']}'>Deny</button>"
            f"</td></tr>"
            for r in pending
        ) or "<tr><td colspan='7'><em>no pending requests</em></td></tr>"
        metrics_snapshot = dict(METRICS)

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>AgentCard Owner Console</title>
<style>
body {{ font-family: monospace; margin: 1.5rem; max-width: 960px; }}
table {{ border-collapse: collapse; width: 100%; margin-bottom: 1.5rem; }}
th, td {{ border: 1px solid #999; padding: 4px 8px; text-align: left; }}
th {{ background: #eee; }}
label {{ display: block; margin: 0.4rem 0; }}
input, select, button {{ font-family: monospace; padding: 4px; }}
#token-out, #req-out {{ background: #f5f5f5; padding: 0.75rem; white-space: pre-wrap; word-break: break-all; }}
.note {{ color: #333; margin: 1rem 0; }}
</style></head><body>
<h1>AgentCard Owner Console (EXP-007b)</h1>
<p class="note"><strong>Agents should use POST /v1/approval-requests (not scrape this page).</strong>
Owner key is documented in README.md — type it below; it is never prefilled.</p>

<h2>Pending approval requests</h2>
<table id="pending-table">
<tr><th>request_id</th><th>action</th><th>resource</th><th>max_amount_cents</th><th>reason</th><th>created_at</th><th>decide</th></tr>
{pending_rows}
</table>
<pre id="req-out">Approve/deny result appears here.</pre>

<h2>Businesses / eligibility</h2>
<table>
<tr><th>id</th><th>name</th><th>status</th><th>reasons</th></tr>
{biz_rows}
</table>

<h2>Mint approval token (manual)</h2>
<form id="mint-form">
<label>action
  <select name="action" id="action">
    <option value="apply">apply</option>
    <option value="loosen_controls">loosen_controls</option>
    <option value="spend_intent">spend_intent</option>
  </select>
</label>
<label>resource (business_id or account_id)
  <input name="resource" id="resource" size="40" placeholder="biz_eligible_clean" required>
</label>
<label>max_amount_cents (optional, for spend_intent)
  <input name="max_amount_cents" id="max_amount_cents" type="number" min="0" placeholder="20000">
</label>
<label>X-Owner-Key
  <input name="owner_key" id="owner_key" type="password" size="40" value="" placeholder="Owner key" autocomplete="off">
</label>
<button type="submit">Mint approval</button>
</form>
<pre id="token-out">Token will appear here.</pre>

<h2>Accounts</h2>
<table>
<tr><th>id</th><th>business</th><th>status</th><th>line_cents</th><th>daily_limit</th><th>daily_spent</th></tr>
{acct_rows}
</table>

<h2>Recent transactions</h2>
<table>
<tr><th>id</th><th>account</th><th>amount_cents</th><th>merchant</th><th>created_at</th></tr>
{txn_rows}
</table>

<h2>Metrics</h2>
<pre>{metrics_snapshot}</pre>

<script>
function ownerKey() {{
  return document.getElementById('owner_key').value.trim();
}}
document.getElementById('mint-form').addEventListener('submit', async (e) => {{
  e.preventDefault();
  const action = document.getElementById('action').value;
  const resource = document.getElementById('resource').value.trim();
  const maxRaw = document.getElementById('max_amount_cents').value.trim();
  const body = {{ action, resource }};
  if (maxRaw !== '') body.max_amount_cents = parseInt(maxRaw, 10);
  const out = document.getElementById('token-out');
  out.textContent = 'Minting…';
  try {{
    const r = await fetch('/v1/approvals', {{
      method: 'POST',
      headers: {{
        'Content-Type': 'application/json',
        'X-Owner-Key': ownerKey(),
      }},
      body: JSON.stringify(body),
    }});
    const data = await r.json();
    out.textContent = JSON.stringify(data, null, 2);
  }} catch (err) {{
    out.textContent = String(err);
  }}
}});
async function decide(id, approve) {{
  const out = document.getElementById('req-out');
  out.textContent = (approve ? 'Approving ' : 'Denying ') + id + '…';
  try {{
    let r;
    if (approve) {{
      r = await fetch('/v1/approvals', {{
        method: 'POST',
        headers: {{
          'Content-Type': 'application/json',
          'X-Owner-Key': ownerKey(),
        }},
        body: JSON.stringify({{ request_id: id }}),
      }});
    }} else {{
      r = await fetch('/v1/approval-requests/' + id + '/deny', {{
        method: 'POST',
        headers: {{ 'X-Owner-Key': ownerKey() }},
      }});
    }}
    const data = await r.json();
    out.textContent = JSON.stringify(data, null, 2);
    if (r.ok) {{
      const row = document.querySelector("tr[data-req='" + id + "']");
      if (row) row.remove();
    }}
  }} catch (err) {{
    out.textContent = String(err);
  }}
}}
document.querySelectorAll('.approve-btn').forEach((btn) => {{
  btn.addEventListener('click', () => decide(btn.dataset.id, true));
}});
document.querySelectorAll('.deny-btn').forEach((btn) => {{
  btn.addEventListener('click', () => decide(btn.dataset.id, false));
}});
</script>
</body></html>"""
    return HTMLResponse(html)
