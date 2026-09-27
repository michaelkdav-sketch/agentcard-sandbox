#!/usr/bin/env python3
"""Smoke test for EXP-007b AgentCard Sandbox. Server must be on BASE_URL."""

from __future__ import annotations

import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8788"
AGENT_KEY = __import__("os").environ["AGENTCARD_AGENT_KEY"]
OWNER_KEY = __import__("os").environ["AGENTCARD_OWNER_KEY"]
passed = 0
failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f"PASS  {name}" + (f" — {detail}" if detail else ""))
    else:
        failed += 1
        print(f"FAIL  {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    client = httpx.Client(base_url=BASE, timeout=10.0)
    agent = {"X-API-Key": AGENT_KEY}
    owner = {"X-Owner-Key": OWNER_KEY}

    # 1. health + agent.json + product
    r = client.get("/health")
    check("1a health", r.status_code == 200 and r.json().get("status") == "ok", r.text[:120])

    r = client.get("/.well-known/agent.json")
    try:
        manifest = r.json()
    except Exception:
        manifest = {}
    action_names = {a.get("name") for a in manifest.get("actions", [])}
    check(
        "1b agent.json",
        r.status_code == 200
        and "actions" in manifest
        and "auth" in manifest
        and "openapi_url" in manifest
        and "request_approval" in action_names
        and "owner_demo_key" not in manifest.get("auth", {}),
        f"actions={sorted(action_names)}",
    )

    r = client.get("/v1/product", headers=agent)
    product = r.json() if r.status_code == 200 else {}
    check(
        "1c product",
        r.status_code == 200 and "gated_actions" in product,
        f"gated={len(product.get('gated_actions', []))}",
    )

    # 2. eligibility for all 3 businesses
    for biz_id, expected_status in [
        ("biz_eligible_clean", "eligible"),
        ("biz_thin_file", "manual_review"),
        ("biz_ineligible_fraud_flag", "declined"),
    ]:
        r = client.get(f"/v1/businesses/{biz_id}/eligibility", headers=agent)
        body = r.json() if r.status_code == 200 else {}
        check(
            f"2 eligibility {biz_id}",
            r.status_code == 200 and body.get("status") == expected_status,
            f"status={body.get('status')}",
        )

    # 3a. apply OMIT token → 403 (not 422)
    r = client.post(
        "/v1/applications",
        headers=agent,
        json={"business_id": "biz_eligible_clean"},
    )
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    check(
        "3a apply omit token → 403",
        r.status_code == 403
        and body.get("code") == "approval_required"
        and "error" in body
        and "hint" in body,
        f"status={r.status_code} code={body.get('code')}",
    )

    # 3b. apply EMPTY token → 403 same shape
    r = client.post(
        "/v1/applications",
        headers=agent,
        json={"business_id": "biz_eligible_clean", "approval_token": ""},
    )
    body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    check(
        "3b apply empty token → 403",
        r.status_code == 403
        and body.get("code") == "approval_required"
        and "error" in body
        and "hint" in body,
        f"status={r.status_code} code={body.get('code')}",
    )

    # 4. owner mints apply token; agent applies → account with line
    r = client.post(
        "/v1/approvals",
        headers=owner,
        json={"action": "apply", "resource": "biz_eligible_clean"},
    )
    mint = r.json() if r.status_code == 200 else {}
    apply_token = mint.get("approval_token")
    check("4a mint apply token", r.status_code == 200 and bool(apply_token), str(mint)[:160])

    r = client.post(
        "/v1/applications",
        headers=agent,
        json={"business_id": "biz_eligible_clean", "approval_token": apply_token},
    )
    app_body = r.json() if r.status_code == 200 else {}
    acct = app_body.get("account") or {}
    account_id = acct.get("id")
    check(
        "4b apply eligible → active account with line",
        r.status_code == 200
        and app_body.get("status") == "approved"
        and acct.get("status") == "active"
        and acct.get("line_cents") == 1_500_000
        and acct.get("daily_spend_limit_cents") == 50_000,
        f"account_id={account_id} line={acct.get('line_cents')}",
    )

    # 5a. spend OMIT → 403
    r = client.post(
        "/v1/spend-intents",
        headers=agent,
        json={
            "account_id": account_id or "acct_missing",
            "amount_cents": 20000,
            "merchant": "CloudTools Inc",
        },
    )
    body = r.json() if r.status_code == 403 else {}
    check(
        "5a spend omit token → 403",
        r.status_code == 403 and body.get("code") == "approval_required",
        f"code={body.get('code')}",
    )

    # 5b. spend EMPTY → 403
    r = client.post(
        "/v1/spend-intents",
        headers=agent,
        json={
            "account_id": account_id or "acct_missing",
            "amount_cents": 20000,
            "merchant": "CloudTools Inc",
            "approval_token": "",
        },
    )
    body = r.json() if r.status_code == 403 else {}
    check(
        "5b spend empty token → 403",
        r.status_code == 403 and body.get("code") == "approval_required",
        f"code={body.get('code')}",
    )

    # 5c. spend bogus → 403
    r = client.post(
        "/v1/spend-intents",
        headers=agent,
        json={
            "account_id": account_id or "acct_missing",
            "amount_cents": 20000,
            "merchant": "CloudTools Inc",
            "approval_token": "bogus",
        },
    )
    body = r.json() if r.status_code == 403 else {}
    check(
        "5c spend bogus token → 403",
        r.status_code == 403,
        f"code={body.get('code')}",
    )

    # 6. mint spend_intent with max_amount; spend within max → success
    r = client.post(
        "/v1/approvals",
        headers=owner,
        json={
            "action": "spend_intent",
            "resource": account_id,
            "max_amount_cents": 25000,
        },
    )
    spend_token = (r.json() or {}).get("approval_token") if r.status_code == 200 else None
    check("6a mint spend_intent", bool(spend_token), f"token_prefix={(spend_token or '')[:12]}")

    r = client.post(
        "/v1/spend-intents",
        headers=agent,
        json={
            "account_id": account_id,
            "amount_cents": 20000,
            "merchant": "CloudTools Inc",
            "approval_token": spend_token,
        },
    )
    spend_body = r.json() if r.status_code == 200 else {}
    txn = spend_body.get("transaction") or {}
    check(
        "6b spend within max → txn",
        r.status_code == 200
        and txn.get("amount_cents") == 20000
        and txn.get("merchant") == "CloudTools Inc"
        and txn.get("irreversible") is True,
        f"txn_id={txn.get('id')}",
    )

    # 7. replay same token → 403
    r = client.post(
        "/v1/spend-intents",
        headers=agent,
        json={
            "account_id": account_id,
            "amount_cents": 1000,
            "merchant": "Replay Shop",
            "approval_token": spend_token,
        },
    )
    body = r.json() if r.status_code == 403 else {}
    check(
        "7 replay token → 403",
        r.status_code == 403 and body.get("code") == "approval_replay",
        f"code={body.get('code')}",
    )

    # 8. spend exceeding daily limit even with fresh token → structured error
    r = client.post(
        "/v1/approvals",
        headers=owner,
        json={
            "action": "spend_intent",
            "resource": account_id,
            "max_amount_cents": 100000,
        },
    )
    over_token = (r.json() or {}).get("approval_token") if r.status_code == 200 else None
    r = client.post(
        "/v1/spend-intents",
        headers=agent,
        json={
            "account_id": account_id,
            "amount_cents": 40000,
            "merchant": "Big Purchase Co",
            "approval_token": over_token,
        },
    )
    body = r.json() if r.status_code in (422, 403) else {}
    check(
        "8 spend over daily limit → structured error",
        r.status_code == 422 and body.get("code") == "daily_limit_exceeded",
        f"status={r.status_code} code={body.get('code')} hint={str(body.get('hint', ''))[:80]}",
    )

    # 9. biz_ineligible_fraud_flag apply with token → rejected
    r = client.post(
        "/v1/approvals",
        headers=owner,
        json={"action": "apply", "resource": "biz_ineligible_fraud_flag"},
    )
    fraud_token = (r.json() or {}).get("approval_token") if r.status_code == 200 else None
    r = client.post(
        "/v1/applications",
        headers=agent,
        json={
            "business_id": "biz_ineligible_fraud_flag",
            "approval_token": fraud_token,
        },
    )
    fraud_body = r.json() if r.status_code == 200 else {}
    check(
        "9 fraud_flag apply → rejected",
        r.status_code == 200
        and fraud_body.get("status") == "rejected"
        and fraud_body.get("reason") == "fraud_flag"
        and fraud_body.get("account") is None,
        f"status={fraud_body.get('status')} reason={fraud_body.get('reason')}",
    )

    # 10. metrics unauthorized_rejected > 0
    r = client.get("/v1/metrics", headers=agent)
    metrics = r.json() if r.status_code == 200 else {}
    check(
        "10 metrics unauthorized_rejected > 0",
        r.status_code == 200 and metrics.get("unauthorized_rejected", 0) > 0,
        str(metrics),
    )

    # 11. HTML / returns 200, NO owner key embedded
    r = client.get("/")
    html = r.text
    check(
        "11a HTML owner console",
        r.status_code == 200 and "Owner Console" in html and "Mint approval" in html,
        f"len={len(html)}",
    )
    check(
        "11b HTML has no owner key",
        OWNER_KEY not in html,
        "secret leaked in HTML" if OWNER_KEY in html else "clean",
    )
    check(
        "11c HTML pending requests section",
        "Pending approval requests" in html,
        "",
    )

    # 12. request_approval happy path (agent request → owner fulfill → poll token → spend)
    r = client.post(
        "/v1/approval-requests",
        headers=agent,
        json={
            "action": "spend_intent",
            "resource": account_id,
            "max_amount_cents": 5000,
            "reason": "smoke small purchase",
        },
    )
    areq = r.json() if r.status_code == 200 else {}
    request_id = areq.get("request_id")
    check(
        "12a create approval-request",
        r.status_code == 200
        and areq.get("status") == "pending"
        and bool(request_id)
        and areq.get("action") == "spend_intent",
        str(areq)[:160],
    )

    # poll while pending
    r = client.get(f"/v1/approval-requests/{request_id}", headers=agent)
    pending_body = r.json() if r.status_code == 200 else {}
    check(
        "12b poll pending",
        r.status_code == 200
        and pending_body.get("status") == "pending"
        and "approval_token" not in pending_body,
        str(pending_body)[:120],
    )

    # agent cannot mint
    r = client.post(
        "/v1/approvals",
        headers=agent,
        json={"request_id": request_id},
    )
    check(
        "12c agent mint with agent key → 401",
        r.status_code == 401,
        f"status={r.status_code} body={r.text[:80]}",
    )

    # owner fulfills via request_id
    r = client.post(
        "/v1/approvals",
        headers=owner,
        json={"request_id": request_id},
    )
    fulfill = r.json() if r.status_code == 200 else {}
    check(
        "12d owner fulfill request_id",
        r.status_code == 200
        and bool(fulfill.get("approval_token"))
        and fulfill.get("request_id") == request_id,
        f"token_prefix={(fulfill.get('approval_token') or '')[:12]}",
    )

    # poll approved — token once
    r = client.get(f"/v1/approval-requests/{request_id}", headers=agent)
    approved = r.json() if r.status_code == 200 else {}
    once_token = approved.get("approval_token")
    check(
        "12e poll approved includes token once",
        r.status_code == 200
        and approved.get("status") == "approved"
        and bool(once_token)
        and once_token == fulfill.get("approval_token"),
        f"status={approved.get('status')} token={(once_token or '')[:12]}",
    )

    # second poll — no token re-delivery
    r = client.get(f"/v1/approval-requests/{request_id}", headers=agent)
    second = r.json() if r.status_code == 200 else {}
    check(
        "12f second poll no token re-delivery",
        r.status_code == 200
        and second.get("status") == "approved"
        and "approval_token" not in second,
        str(second)[:120],
    )

    # use once_token for a small spend
    r = client.post(
        "/v1/spend-intents",
        headers=agent,
        json={
            "account_id": account_id,
            "amount_cents": 5000,
            "merchant": "Smoke RequestFlow",
            "approval_token": once_token,
        },
    )
    req_spend = r.json() if r.status_code == 200 else {}
    check(
        "12g spend via request_approval token",
        r.status_code == 200
        and (req_spend.get("transaction") or {}).get("amount_cents") == 5000,
        f"status={r.status_code} txn={(req_spend.get('transaction') or {}).get('id')}",
    )

    # loosen omit → 403
    r = client.put(
        f"/v1/accounts/{account_id}/controls",
        headers=agent,
        json={"daily_spend_limit_cents": 200000},
    )
    body = r.json() if r.status_code == 403 else {}
    check(
        "12h loosen omit token → 403",
        r.status_code == 403 and body.get("code") == "approval_required",
        f"code={body.get('code')}",
    )

    print()
    print(f"Result: {passed} passed, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
