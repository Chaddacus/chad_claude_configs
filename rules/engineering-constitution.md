# Engineering Constitution (SPEC.md Standard 1)

**Every meaningful line of code must be defensible** — with inspectable evidence and architecture, not persuasive prose: why it exists, why this solution, why this layer/module/file, why existing code cannot satisfy the need, why smaller is insufficient, what failure it addresses, how it was verified.

## Required behavior

You MUST:

- understand before modifying: inspect relevant architecture, contracts, tests, and current patterns;
- solve root causes, not symptoms;
- prefer the smallest correct, maintainable change; minimize blast radius;
- preserve architectural boundaries; avoid unrelated refactors during scoped work;
- avoid speculative abstractions and speculative reuse; follow DRY and separation of concerns;
- preserve useful diagnostic information; handle meaningful error/failure paths;
- validate assumptions at boundaries;
- reuse approved dependencies; justify meaningful new ones;
- never claim a check passed unless it actually ran and passed.

You MUST NOT:

- shotgun code until something passes;
- add wrappers, shims, fallbacks, factories, generic utilities, or layers without evidence they are required;
- hide errors to make a build/test green; disable, weaken, delete, or skip legitimate tests for a green result;
- create broad `utils`/`common` dumping grounds without demonstrated cross-module reuse;
- treat code volume as quality.

## Commenting

The repository must be self-describing for humans and LLMs.

- Meaningful source files begin with a concise header: purpose, responsibility, place in the system, important boundaries.
- Meaningful functions carry a useful docstring: purpose, contract, assumptions/invariants, side effects, non-obvious rationale.
- Comments explain intent and contract. They do not narrate syntax.

## Definition of Done

A task is complete only when, as applicable: requested behavior is implemented; it fits the accepted architecture; edge/failure cases are handled; appropriate verification exists and actually passes; the final diff is self-reviewed; unrelated changes are absent; unresolved risks are disclosed; completion evidence is current for the final diff.
