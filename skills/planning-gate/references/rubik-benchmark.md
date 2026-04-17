# Rubik 3D Benchmark Contract

This benchmark is the canonical integrated autonomy-pressure scenario for a repo-local frontend build.

## Benchmark ID

- `rubik_3d_self_solve`

## Goal

Build a browser-based 3D Rubik's cube application that can:
- render an interactive cube
- scramble deterministically
- solve from the current state
- animate the solve sequence
- reset to a solved state

## Required Features

- interactive 3D cube viewport
- deterministic scramble control
- self-solve action from the current cube state
- visible move or solve animation
- reset action
- unit-test evidence for cube-state and solver behavior
- browser/smoke evidence for core interaction path
- desktop and mobile-usable layout

## Non-Goals

- backend or persistence
- multiplayer or collaboration
- advanced speedcubing analytics
- account system
- production deployment work
- cosmetic polish beyond a coherent usable interface

## Acceptance Evidence

- unit or solver-validation evidence proving:
  - legal cube transitions
  - scramble produces a non-solved state
  - solve returns the cube to solved state
- UI/browser evidence proving:
  - app loads
  - scramble control works
  - solve control completes
  - reset returns to solved presentation
- implementation evidence remains within benchmark scope

## Runtime Pressure Dimensions

- multi-stage decomposition across UI, state model, and solving logic
- validation plus interaction evidence in the same run
- temptation to overbuild beyond the bounded app contract
- closure pressure: finish at the benchmark boundary rather than polishing indefinitely
- repair pressure: solver and UI paths must recover from verification failures without human steering

## Scoring Intent

The benchmark is for runtime-quality comparison, not just feature completion.

Primary scoring dimensions:
- closure correctness
- evidence quality
- cycles to closure
- repair discipline
- support-confidence cleanliness
- boundedness of execution

Secondary dimensions:
- wall-clock time
- frontier width efficiency
- fallback burden

## Recommended Use

- run as a repeatable benchmark harness, not as an open-ended product build
- compare config variants against the same contract
- pair with smaller holdout benchmarks for blocked, ambiguous, and repair-heavy cases to avoid overfitting
