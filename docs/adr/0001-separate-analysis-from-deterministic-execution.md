---
status: accepted
---

# Separate AI analysis from deterministic execution

The Analysis Agent may use a frozen AI model to produce a versioned, expiring Directional Signal, but the Trading Agent and Order Gateway are deterministic and enforce the Risk Envelope in code. A language model never sees broker credentials or creates broker payloads directly; this costs some flexibility but makes order behavior reproducible, testable, and fail-closed.
