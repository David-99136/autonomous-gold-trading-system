---
status: accepted
---

# Live trading is user-unlocked and fail-closed

Every process start, account change, strategy-version change, or material fault returns the system to Live Locked. Only the user can enable new live risk after reconciliation and health checks; agents may autonomously reduce risk or fall back to Demo Mode, and uncertain broker outcomes enter Recovery Locked rather than being retried blindly.
