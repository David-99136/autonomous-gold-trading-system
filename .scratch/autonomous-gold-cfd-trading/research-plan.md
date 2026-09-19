# Research Plan: Autonomous Gold CFD Trading

Status: `needs-triage`

Cutoff: 2026-09-06

This file lists facts that must be established from primary sources or the user's Capital.com Demo account before implementation or Live qualification. `UNKNOWN` is an acceptable result; invention is not.

Primary-source baseline: [Capital.com Gold Spot CFD 自動交易介面研究](../../docs/research/capital-com-gold-spot-api.md)

| ID | Question | Required evidence | Status | Blocks |
|---|---|---|---|---|
| R-01 | What is the account-specific Gold Spot epic and full contract specification? | Demo `/markets` and `/markets/{epic}` responses with secrets removed | GOLD observed in runtime/discovery.json and gold-details.json; point-value verification pending | Broker adapter, sizing |
| R-02 | Which Capital.com legal entity serves the account, and what terms govern unattended Public API automation for a Taiwan resident? | Account legal documents and written first-party support confirmation | Public sources unavailable; hard Live blocker | Live eligibility |
| R-03 | What are current minimum size, size increment, margin factor, guaranteed/regular stop constraints, and margin close-out behavior? | Demo market details plus entity-specific official terms | Demo size 0.01, increment 0.01, margin 5% observed; entity close-out rules pending | Risk engine |
| R-04 | What are the exact daily/weekly/holiday trading sessions and overnight-funding boundary for this account's Gold Spot? | Demo market details and first-party fee/session pages | Current UTC schedule/funding read; adapter tested; holiday coverage and refresh service pending | Session manager |
| R-05 | How much Capital.com one-minute bid/offer history is retrievable, and are there gaps or account-specific limits beyond 1,000 rows per response? | Controlled Demo API probes and captured metadata | 2026-09-04 probe: 1258 rows, missing 12:31 UTC; longer coverage and bar timestamp semantics pending | Backtest data |
| R-06 | What does `lastTradedVolume` represent for Gold Spot CFD? | Explicit first-party definition or `UNAVAILABLE` | Unavailable from public official sources | Volume feature eligibility |
| R-07 | What are the exact confirmation, activity, position, working-order, partial-close, partial-fill, and reject semantics? | Official endpoint schemas plus minimum-size Demo fault/replay tests | Empty-account read-only inspection verified; synthetic confirmation checks pass; real fills/partial close and latency unresolved | Idempotent order state machine |
| R-08 | Which licensed sources provide point-in-time macro releases and sufficiently timely qualitative news? | Provider documentation, terms, latency samples, and cost | Pending | Analysis Agent |
| R-09 | Which sources provide point-in-time DXY, nominal/real yields, Treasury yields, silver, COT, ETF flows, and central-bank purchases? | Primary-source/API documentation and vintage policy | Pending | Cross-market features |
| R-10 | Which AI model and prompt produce the best version-frozen evidence synthesis under the latency and cost budget? | Fixed evaluation set, error analysis, latency, and token cost | Pending | Model selection, cost cap |
| R-11 | Which spread, slippage, volatility, trend-efficiency, and supply/demand parameters survive walk-forward validation? | Venue-native data, documented search space, untouched out-of-sample results | Pending | Strategy parameters |
| R-12 | Which always-on cloud host and secret store meet measured latency, availability, and cost needs? | Prototype benchmark and threat-model review | Deferred until Demo | Cloud Live deployment |

## Research rules

2026-09-07 R-05 update: [timestamp semantics review](../../docs/research/capital-history-semantics.md)
found no explicit public proof of open/close timestamp endpoints or finality. Offline `history-audit`
verified the existing two-page, 1258-row capture is internally consistent, retaining the 12:31 UTC
unclassified gap. This does not resolve timestamp or point-in-time eligibility; no broker calls made.

- Prefer the owner of the fact: Capital.com for its product/API, official statistical agencies for releases, and licensed provider documentation for news.
- Record observation time, publication time, receive time, coverage period, units, revision/vintage behavior, and known caveats.
- A failed request is not proof that a valid source or value does not exist. Record `VALID_SOURCE_VALUE_NOT_FETCHED` when appropriate.
- Search snippets, cached values, or inferred contract terms cannot satisfy a gate.
- Do not use Live credentials or place orders while resolving research items.
