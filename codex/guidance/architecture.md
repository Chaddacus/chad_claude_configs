# Application architecture

## Spine, modules, and plugins

Use a spine, module, and plugin foundation for applications. Implement these responsibilities with ordinary functions, objects, or packages when sufficient; this standard does not require a runtime plugin loader or a new framework.

- The spine assembles the application: configuration, shared infrastructure, module and plugin wiring, and startup and shutdown coordination. Feature business rules belong in modules.
- Modules own coherent application capabilities, their business rules, their data operations, and their public contracts.
- Plugins provide replaceable integrations or optional extensions behind meaningful contracts. Introduce a plugin boundary where replaceability or optional behavior serves an actual requirement.

UI, API, and MCP entry points call the same module operations rather than duplicating business rules. Keep transport-specific parsing, presentation, and protocol handling in their adapters. Follow [development.md](development.md) for the required API and MCP surfaces and their access controls.

Declare dependencies and keep their direction understandable and free of cycles. Components use each other's public contracts rather than reaching into private files, mutable state, or owned database tables. Share data through explicit interfaces or documented shared contracts. Keep infrastructure details behind the boundary that needs them; do not spread vendor-specific behavior through business logic.

Apply this foundation to new applications and incrementally to existing applications within the authorized work. Document significant existing gaps in `spec.md`. Do not turn a small fix into an unrelated architecture rewrite; surface a substantial required expansion for a decision.

## Keep the spine small

Keep the application entry point a readable sequence of application-level steps. As complexity warrants, separate configuration loading, infrastructure construction, module assembly, plugin registration, and lifecycle coordination into files with clear responsibilities. The spine coordinates these parts rather than implementing them all itself.

Do not move a monolith into an `ApplicationManager`, bootstrap helper, or giant registry. Review both the entry point and the files it delegates to. Each should have a coherent responsibility and understandable dependencies.

## File and helper organization

Name files for their responsibility. Avoid general-purpose `helpers`, `utils`, or `common` files that accumulate unrelated behavior. Put feature-specific helpers beside their feature. Move code into shared locations only when it represents a genuinely shared responsibility with a clear owner.

Use file size and the number of unrelated reasons to change as review signals, not arbitrary line-count limits. Extract cohesive responsibilities when a file becomes difficult to understand or change safely. Avoid excessive fragmentation that makes one operation require navigating many trivial files.

Record major boundaries, dependency relationships, and important file locations in `spec.md`. Use [coding.md](coding.md) for implementation contracts and [lifecycle.md](lifecycle.md) for resource ownership and component activation or disposal.
