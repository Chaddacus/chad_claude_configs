# Development and delivery

## Fast iteration

Optimize feature and development branches for small changes and a short edit-to-feedback cycle. Use hot reload, watch mode, incremental builds, and source-code volume mounts where appropriate. Routine code edits should not require full rebuilds, container image builds, or redeployments when a suitable development workflow can avoid them.

Provide a simple way to start the application and dependencies, repeatable development data and configuration, and immediately accessible logs and errors. Keep development behavior representative of production; document intentional differences in `spec.md`. Use reproducible built artifacts for production.

Run focused checks during iteration. Before merging into main or deploying to production, run the full required test suite, regression checks, and critical user workflows against the integrated changes. Do not postpone these checks until after promotion.

Keep branches short-lived and integrate independently usable slices frequently within the authorized delivery scope. Measure and improve slow feedback loops rather than waiving required verification. A merge to main need not trigger an immediate production release.

Provide documented fast-check and full-check commands and enforce promotion requirements through the application's existing CI and branch protections. Confirm required checks actually executed against the relevant current integrated revision; stale results, skipped tests, or a green wrapper job do not prove the required behavior. Prefer ordinary repository tooling over a separate agent approval framework.

## API and MCP in every application

Every application must provide both an API and an MCP interface. Design application capabilities for programmatic use, with shared business logic behind UI, API, and MCP entry points. Keep contracts and applicable permissions consistent across interfaces. Verify meaningful operations through both API and MCP.

For existing applications missing an interface, record the gap in `spec.md` and address it as part of applicable feature or architecture work. If completing it would substantially expand the current task, surface the required work and ask rather than silently performing an unrelated rewrite.

Maintain machine-readable interface contracts, using OpenAPI for HTTP APIs where appropriate and explicit MCP input/output schemas. Validate external inputs, return useful structured results and actionable errors, and check caller compatibility when contracts change. Follow the current MCP requirements for the chosen transport; shared permissions do not imply interchangeable credentials.

Use sensible timeouts and bounded retries for transient failures. Before retrying a write with an uncertain outcome, use supported idempotency or reconcile whether it already succeeded. Reuse established client/server mechanisms; do not add a universal retry framework without a concrete need.

## Development access through Tailscale

Include an explicit development mode that bypasses the normal login flow for authorized access through a trusted Tailscale path. Map access to a defined development identity and preserve application authorization checks. Verify tailnet access at a trusted boundary; never trust a client-supplied identity header by itself.

Confine this bypass to development, keep it disabled in production, and ensure ordinary production configuration cannot accidentally activate it. Document its setup, identity, and boundaries in `spec.md`. Do not expose development access publicly.

Verify ordinary login and rejection of unauthorized API/MCP calls before production, including rejection outside the trusted development entry point. Tests using only the development identity do not verify production authentication. When using Tailscale Serve identity headers, prevent direct network access to the backend, typically through localhost binding; this still assumes trusted local processes. Use an equivalent verified boundary for other topologies.

## Observe the running application

Actively use relevant diagnostic tools to ground implementation, investigate failures, and verify changes. Depending on the task, inspect browser console and network activity, rendered state and user interactions, application logs, request traces, debugger state, database queries, container output, service health, and build or reload errors. Exercise API and MCP operations directly when relevant.

Make diagnostics easy to access and document how to use them in `spec.md`. Choose tools that answer concrete questions; do not load every tool or add an observability stack to every small task.

## Tests that earn their place

Organize a compact, coherent test suite around application behavior and meaningful failure risks. Each new test file should have a clear purpose that existing coverage does not adequately serve. Avoid fragmented files, duplicate assertions, and tests that merely restate implementation details. There is no arbitrary target file count.

Test actual user workflows, including important failure paths and integration boundaries. Passing checks must demonstrate intended behavior; do not weaken assertions or substitute stubs merely to make a suite pass. Use focused checks during development and the full required verification before promotion.

Combine fast logic and integration tests with a small set of high-value end-to-end journeys. Isolate test data and investigate flaky failures instead of repeatedly retrying until green. Test doubles can make external-dependency tests repeatable, but mocked results do not prove that a real external integration works; verify that integration separately when it is part of the acceptance criteria.

For UI changes, check relevant keyboard interactions, responsive layouts, and loading, empty, and error states. Include applicable accessibility checks in existing workflows; automated scans alone do not establish complete accessibility.

## Own discovered defects

Investigate problems encountered during the work and fix their root causes, including pre-existing defects. Do not dismiss a problem because you did not introduce it. Add meaningful regression coverage when appropriate to the defect.

If a repair requires a substantial scope expansion, a consequential decision, or unavailable access, report the evidence and ask a focused question. Continue independent authorized work where possible, and make unresolved defects visible in the handoff.

## Parallel work and dependencies

Proactively delegate bounded, independent work when parallel execution can improve speed or quality. Size the team to the task; small changes may be faster with one agent. Use a swarm for distinct work streams with useful parallelism, not as a mandatory ceremony.

Choose conversation inheritance deliberately. For bounded independent reviews, follow the fresh-session and self-contained handoff guidance in [review.md](review.md). For other workers, pass the context needed for their assignment; do not inherit the entire conversation by default merely because delegation supports it.

Before delegating, establish each assignment's output, ownership, interfaces, dependencies, acceptance criteria, and relevant context, including `spec.md`. Avoid concurrent edits to the same files; use separate ownership or isolated worktrees and deliberate integration.

Isolate or namespace mutable runtime resources for concurrent tasks, including ports, databases, queues, containers, browser sessions, and test data as applicable. Verify each agent is observing its intended application instance. Track resource ownership and clean up only the task's resources; separate worktrees alone do not isolate running environments.

For work with meaningful dependencies, keep a lightweight directed acyclic graph (DAG) showing prerequisites and tasks ready to run. A short checklist suffices for simple work. Agree on shared contracts before dependent implementations; run independent implementations in parallel, then integrate and verify the combined workflow.

One lead remains accountable for the outcome: coordinate agents, resolve conflicts, integrate changes, and verify the application. Treat agent reports as evidence to assess, not automatic proof of completion. Keep plans current during execution and capture lasting application knowledge in `spec.md`.

Choose a concurrency limit appropriate to the task and available resources. When a worker stalls, preserve useful progress, investigate or reassign its blocker, and continue independent work. Reassess the team when coordination or duplicated work costs more than it saves.

## Independent supervision

Use the read-only `reviewer` role as the independent supervisor at meaningful feature checkpoints and before promotion to main or production. Group small related edits into one review. Follow [review.md](review.md) for review inputs, evidence, findings, resolution, and authority boundaries. The lead remains accountable for integration and completion.

## Deployment and recovery

For authorized deployment work, identify the deployed revision, verify a critical workflow and relevant health signals, and establish how to restore service if the release fails. Document the recovery procedure in `spec.md` or a linked operational reference. Scale rollout precautions to impact; a small app may need a smoke check and a reliable revert command, while higher-impact changes may warrant staged rollout.

Treat persistent-data changes separately from code rollback. Rehearse migrations with suitable representative data, protect data with an appropriate backup and verified recovery path, and assess compatibility with both old and new application versions. Reverting an image does not restore lost data. Use phased schema changes when compatibility requires them; do not assume every migration can safely be reversed.

## Definitions of done

Establish concrete acceptance criteria before implementation, using the request and current spec. Make routine choices without an approval checkpoint; ask only when a consequential uncertainty remains.

- **Agent assignment:** The agreed output is implemented, relevant checks pass, and the agent reports evidence, dependencies, and remaining limitations.
- **Development feature:** The intended behavior works in the running application; affected UI, API, and MCP behavior agree where applicable; focused tests and relevant user workflows pass; affected comments and `spec.md` are current.
- **Main or production promotion:** Integrated changes pass the full required suite, regression checks, and critical user workflows, and applicable deployment requirements are satisfied.
- **Deployment completion:** The intended version is running, a critical workflow and relevant health signals have been checked, and the recovery procedure is known. Include migration and data-recovery evidence when persistent data changes.

Do not call an unverified result complete. State which acceptance criteria remain unmet when execution or verification is blocked. Successful checks alone do not authorize a merge or deployment beyond the user's requested scope.
