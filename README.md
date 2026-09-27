# AgentCard Sandbox (EXP-007)

Synthetic agent-native business-card mock: eligibility → apply → controls → spend-intent, with **opaque single-use human approval tokens** and a first-class `POST /v1/approval-requests` flow.

This is a **lab fixture** for studying agent approval gates — not a real card product. No real money, PANs, or KYC.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # set AGENTCARD_AGENT_KEY and AGENTCARD_OWNER_KEY
set -a && source .env && set +a
uvicorn app:app --host 0.0.0.0 --port 8788
```

## Agent contract

- Discover: `GET /.well-known/agent.json`
- Ask human: `POST /v1/approval-requests` then poll `GET /v1/approval-requests/{id}`
- Owner mints/fulfills via Owner Console (`GET /`) or `POST /v1/approvals` with `X-Owner-Key`
- Gated: apply, loosen_controls, spend_intent

Do **not** scrape Owner Console HTML for secrets.

## Smoke

```bash
set -a && source .env && set +a
python scripts/smoke_test.py
```

## Lab notes

See `EXPERIMENTS.md`. Belief from council evals: hard tokens keep unauthorized_spend ≈ 0 under improvisation; approval-requests is the product surface to keep.
