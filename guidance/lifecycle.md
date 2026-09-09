# Lifecycle and cleanup

## Explicit ownership

Identify who owns each acquired resource and how its lifetime ends. Pair acquisition with cleanup for subscriptions, timers, workers, connections, files, registrations, temporary state, and other managed resources. Prefer established language and framework mechanisms for scoped cleanup.

Make startup, shutdown, cancellation, and failure behavior explicit. If startup fails partway through, release resources already acquired. Tear down in dependency-safe order, commonly the reverse of acquisition. Prevent repeated cleanup from corrupting state, and ensure one cleanup failure does not silently prevent the remaining required cleanup. Preserve useful failure evidence.

Own asynchronous work: stop accepting new work when appropriate, cancel or drain in-flight work, and await termination with suitable bounds. Avoid orphaned tasks and callbacks that continue using disposed state. Clean up only resources owned by the component or task.

## Dependency availability

Define what happens when a required dependency is missing, fails, or is replaced. Do not expose a component as ready before its requirements are satisfied. Make degraded operation explicit where supported, and avoid silently continuing with stale references.

When dynamic activation, unloading, or replacement is required, give each component a scoped lifetime. Withdraw its registrations and effects when deactivated, and reinitialize affected dependents when their contracts or instances change. Define how in-flight operations are handled. Static applications can use ordinary startup and shutdown; do not build hot replacement infrastructure without a concrete requirement.

## Managed effects and external actions

Cleanup can undo only effects with a valid, implemented reversal within the component's control. Sending a message, charging a payment, or committing an external write is not undone by disposing its caller. Use explicit idempotency, reconciliation, or compensating operations where needed; follow [development.md](development.md) for retries with uncertain outcomes.

These lifecycle principles are informed by Cordis's [A Programming Paradigm for Spatiotemporal Composability](https://arxiv.org/pdf/2608.25512). They do not require adopting Cordis. Resource tracking does not prove cleanup is correct, reverse every external effect, or provide a security sandbox.

## Verification

For affected lifecycle behavior, test successful startup and shutdown, partial initialization failure, cancellation, and dependency loss or replacement where supported. Check that owned resources are released and unrelated components remain operational. Use deterministic tests and relevant runtime evidence; do not rely only on a happy-path construction test or add speculative lifecycle cases to code without that behavior.
