# Frontend, Accessibility & Human Interface (SPEC.md Standard 10)

## Governing rule

**Build a clean, modular, accessible, fully functional frontend foundation first.** Visual polish layers on top without compromising ownership, semantics, accessibility, behavior, authority, or testability.

## Architecture

The spine/module contract applies to frontend. UI spine owns only true global concerns: bootstrap, routing composition, global providers, error boundaries, top-level layout, theme/design-system wiring. Feature modules own their UI state, components, adapters/hooks, validation/presentation logic, styling, and UI proof. Cross-feature interaction uses public contracts. Shared primitives must demonstrate genuine reusable semantics before promotion.

## Design system

Foundation base template: `templates/frontend/foundation.css` (reset, semantic tokens, typography/spacing scales, color roles, focus styles, form/control states, layout primitives, breakpoints, motion/reduced-motion). Feature styling stays with its module — no giant uncontrolled global stylesheets. Keep a small number of intentional templates; **template changes have large blast radius and require broad visual/accessibility regression.** A later design-polish pass may add branding/hierarchy/animation but may not silently rewrite business logic, authorization, state ownership, module boundaries, contracts, accessibility semantics, or required UI states.

## Accessibility

WCAG 2.2 AA is the default web baseline. Native semantics first; ARIA only when necessary and then the COMPLETE interaction/keyboard pattern. Keyboard operation, visible focus, accessible forms, understandable status/error states, practical target sizing, password-manager compatibility, and required announcements are implementation work, not polish.

## UI states and UX

- Every meaningful capability deliberately evaluates its applicable states: loading, ready, empty, partial, disabled, submitting, success, error, unauthorized, dependency/offline failure (checklist: `templates/frontend/ui-capability.md`).
- Long-running work shows honest progress — never invented percentages. Server/background job state survives navigation.
- Same operation, same terms, same interaction everywhere. Risk determines friction: reversible actions stay cheap; consequential/irreversible ones require stronger confirmation.
- User-facing errors explain impact and recovery in plain language and preserve an internal trace/reference id. Responsive behavior is intentional — never hide critical capability because the screen is smaller. Browser support is declared and tested.

## AI interfaces

Where material, visually distinguish user/source data, grounded information, and AI-generated interpretation. Consequential AI conclusions expose evidence/citations; material ambiguity changes UI behavior rather than silently guessing; consequential AI actions show what will happen and to whom before confirmation. Never present queued work as completed or invent tool activity.

## Completion

**Meaningful frontend work is never declared complete from source inspection alone.** Real browser interaction + inspected evidence via the `ui-proof` skill; significant work additionally gets fresh-context review from the `frontend-verifier` agent.
