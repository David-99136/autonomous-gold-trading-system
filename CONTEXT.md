# Autonomous Gold CFD Trading

This context describes a user-owned system that analyses Capital.com Gold Spot CFD and manages trades inside an explicit risk envelope. It separates market interpretation from order execution so every decision can be reproduced and audited.

## Language

### Actors and decisions

**Analysis Agent**:
The decision-maker that combines point-in-time market evidence, deterministic scores, and a frozen AI model into a directional signal.
_Avoid_: Trading bot, order agent

**Trading Agent**:
The decision-maker that turns a valid directional signal into a position-management decision while remaining inside the risk envelope.
_Avoid_: Analysis agent, free-form AI trader

**Order Gateway**:
The sole boundary permitted to authenticate with the broker and submit, amend, cancel, or reconcile orders.
_Avoid_: Broker bot, AI executor

**Directional Signal**:
A versioned, expiring recommendation of `LONG`, `SHORT`, or `NO_TRADE` with evidence, strategy mode, confidence, and invalidation conditions.
_Avoid_: Prediction, order

**Trade Intent**:
An idempotent request from the Trading Agent describing a desired position change before broker submission.
_Avoid_: Signal, fill

### Market interpretation

**Left Reversal**:
A counter-move setup at a one-hour supply or demand zone that requires a confirmed five-minute structural reversal.
_Avoid_: Blind catch, averaging down

**Right Continuation**:
A trend-following setup that requires a one-hour trend and a confirmed five-minute breakout or continuation structure.
_Avoid_: Chase, momentum guess

**Market Quality**:
The combined state of tradability, quote freshness, spread, liquidity window, and volatility that determines whether new risk may be opened.
_Avoid_: Market mood

**Supply Zone**:
A reproducibly detected one-hour price area where selling pressure previously displaced price.
_Avoid_: Resistance line

**Demand Zone**:
A reproducibly detected one-hour price area where buying pressure previously displaced price.
_Avoid_: Support line

### Risk and lifecycle

**Risk Envelope**:
The immutable-at-runtime limits on loss, margin usage, market eligibility, position count, timing, and permitted order behavior.
_Avoid_: AI confidence, risk preference

**Broker Hard Stop**:
A broker-held stop attached when a position is created and which may only be tightened.
_Avoid_: Mental stop, account circuit breaker

**Soft Circuit Breaker**:
The daily realized-loss boundary that blocks new positions while preserving deterministic management of existing positions.
_Avoid_: Warning

**Hard Circuit Breaker**:
The daily realized-loss boundary that orders risk reduction, cancels working orders, and locks the system until human recovery.
_Avoid_: Soft stop

**Demo Mode**:
The execution mode that uses Capital.com's demo environment and can never submit to a live account.
_Avoid_: Backtest

**Live Locked**:
The default live-capable state in which existing live exposure may be made safer but no new live risk may be opened.
_Avoid_: Offline, Demo Mode

**Live Unlocked**:
A temporary, user-authorized state that permits new live risk after reconciliation and health checks succeed.
_Avoid_: Automatic live mode

**Realized Daily Loss**:
The current trading day's closed-trade loss measured against start-of-day account equity.
_Avoid_: Floating loss, drawdown

**Recovery Locked**:
A fail-closed state entered when broker orders, positions, account state, time, or local records cannot be reconciled.
_Avoid_: Retry mode
