# Security and operating boundaries — module 1

This is a **local read-only foundation**, not a production internet-facing service.

## Controls present
- No Binance exchange API keys or signed order calls.
- The live trading flag actively rejects any truthy value.
- No order placement, cancellation, leverage, or account mutation routes.
- Localhost-only external port binds for dashboard and Prometheus in Docker Compose.
- DB/Redis/NATS restricted to internal Compose networking.
- MFA + scrypt password hashing, short-lived JWTs, username/IP login throttling.
- Operator enrollment via CLI, not public HTTP.
- DB-enforced roles and active-account checks on every protected API request.
- Persistent audit events for authentication and enrollment.
- Secrets loaded from non-committed `.env`.

## Gaps / pre-production blockers
- No TLS reverse proxy, hardware-backed secrets, automated secret rotation, independent penetration testing, WAF or full production IAM.
- JWT has no immediate revocation list (disabling account blocks future API requests).
- No password recovery mechanism, no admin MFA recovery runbook, no password breach list checking.
- Outbound endpoints and dependencies require security review / pinning before internet deployment.
- Redis local service is not password protected; assumes private Compose network.
- Prometheus and dashboard ports must not be forwarded to the public network.
- DB default local password must not be reused in shared/prod environments.
- Credentials can leak from interactive terminal histories, screenshots or logs; keep authenticator enrollment URI private.
- Network ingress, compliance, operator permissions and regulated futures eligibility must be independently approved before any live trading phase.

## Reporting
Report suspected security issues privately. Do not commit secrets or screenshots of MFA QR codes. Regenerate compromised development credentials immediately.
