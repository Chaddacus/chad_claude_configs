# Code construction

Apply these principles to improve correctness, comprehension, and ease of change. Keep complexity proportional to the problem. Follow the repository's language conventions, formatter, linter, and type checker; use existing designs unless a different approach solves an identified problem.

## Clean, straightforward code

Write code that is easy to understand, modify, and debug. Keep responsibilities clear, use meaningful names, and follow established project conventions. Choose the simplest design that fully meets the requirements. Introduce abstractions or dependencies when they address a concrete need.

## Explain the code

For each source file you create or modify, maintain a top-of-file comment explaining its purpose, responsibilities, and connections to other parts of the application.

Document each function you create or modify with a comment or docstring explaining why it exists, what it connects to, and how it works at an appropriate level of detail. Include relevant inputs, outputs, side effects, and non-obvious behavior. Keep simple function explanations brief; give complex behavior enough context to be understood.

Use inline comments where implementation details or reasoning need explanation. Keep comments accurate as the code changes. Use the language's conventional documentation format, and do not edit generated or vendored code just to add comments.

## Contracts and valid state

Make inputs, outputs, side effects, failure behavior, and ownership clear at function and component boundaries. Use meaningful types and explicit state models where they prevent mistakes. Validate untrusted data at entry boundaries; do not scatter redundant validation throughout already validated internal flows. Make invalid states difficult to represent without building elaborate type machinery for trivial cases.

Inspect actual callers and relevant tests before changing a public interface, schema, or observable behavior. Preserve required compatibility or make the intended migration explicit. A locally passing test does not establish compatibility with other consumers.

## Responsibilities and dependencies

Group behavior that changes for the same reason; separate independent responsibilities. Keep business rules with the module that owns the relevant data and operations. Use clear names, direct control flow, and composition. Prefer explicit dependencies and state ownership to hidden globals or ambient service lookup.

Extract a function, object, or shared abstraction when it gives a coherent responsibility a useful name, protects a contract, or removes demonstrated duplication. Do not introduce speculative extension points, excessive indirection, or a separate helper for every statement. Similar-looking code does not always represent the same responsibility.

Use [architecture.md](architecture.md) for application composition, module and plugin boundaries, and file organization. Use [lifecycle.md](lifecycle.md) when code acquires resources, starts background work, or responds to dependency changes.

## Errors and changes

Handle failures deliberately. Preserve useful error context, distinguish expected outcomes from unexpected failures, and avoid swallowing errors or returning success-shaped fallback data that hides a broken operation. Use the project's established error conventions.

Keep changes focused and reviewable. Refactor in small behavior-preserving steps; identify intentional behavior changes separately so their effects can be verified. Remove code made obsolete by the change and update its callers and documentation. Avoid unrelated cleanup that expands the task without improving its outcome.

Reproduce defects before fixing them when practical. Test observable behavior, contracts, important failure paths, and compatibility using the existing suite. Tests should detect a meaningful incorrect implementation. Follow [development.md](development.md) for verification depth, actual user workflows, and delivery requirements.
