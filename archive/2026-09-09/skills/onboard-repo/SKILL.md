---
name: onboard-repo
description: Onboard a new repository into DevRelay's spine environment — push to Gitea, build the test-env Docker image, write the environmentRef, and pre-pull. Use when a repo needs to run on the fleet for the first time.
---

# Onboard Repo — DevRelay Spine Environment Setup

Four things make a repo runnable on the DevRelay spine. This skill produces all four
in order, validating each before moving to the next.

## Inputs

The operator provides:
- **repo** — a clone URL or local path to the repository
- **gitea org** — the Gitea org to push into (default: `devrelay-test`)
- **registry** — the Docker registry (default: `127.0.0.1:5001/devrelay-test`)

## Sequence

### 1. Push the repo to Gitea

Clone (or locate) the repo. Push the full history to Gitea at `:3000` so agent
boxes clone from the tailnet rather than the internet.

```bash
git remote add gitea https://<gitea-host>:3000/<org>/<repo>.git
git push gitea main
```

If push-to-create is disabled, the repo must be created through the Gitea API or
UI first. Verify with `git ls-remote` after pushing.

### 2. Build the test-environment image

Create `docker/test-env/<repo>/` in either the target repo or the devrelay repo,
containing three files:

**Dockerfile** — carries the dependency layer and toolchain only. Requirements:

- **Must not contain the source.** The worktree is bind-mounted at run time so the
  agent edits on the host and tests run inside against those exact files.
- **Must stamp `/etc/devrelay-env-sha`** with the commit its dependencies came from
  (passed as `BASE_SHA` build arg). This is what makes `environment_drifted`
  detectable.
- **Must stamp `/etc/devrelay-env-deps-sha256`** with a fingerprint of the dependency
  files. Dependency drift is the gate; commit sha drift is not (a defect patch
  changes the sha while the dependency layer stays valid).
- **Must stamp `/etc/devrelay-env-manifest`** with human-readable provenance.
- **Must match the production runtime.** Same base image, same language version, same
  package manager. Drift between what the developer tests and what the image
  provides is the class of bug this environment exists to eliminate.

**entrypoint.sh** — refuses to run against a missing or wrong mount, then `exec "$@"`.
Checks: `/work` mounted, not empty, expected project marker file exists (e.g.
`requirements.txt`, `package.json`), source directory exists. Exit code 78
(EX_CONFIG) for environment errors, not 1.

**build.sh** — convenience script that takes repo path and optional registry,
resolves `BASE_SHA` from `git rev-parse HEAD`, and runs `docker build`.

#### Detect project type and apply the right pattern

| Marker | Base image | Dependency install | Mount concerns |
|--------|-----------|-------------------|----------------|
| `package.json` + `package-lock.json` | `node:<version>` | `npm ci` at `/work`, anonymous volume at `/work/node_modules` | node_modules hidden by mount; anonymous volume seeds from image |
| `requirements.txt` | language-matched Python image | `pip install` into site-packages | site-packages is outside `/work`, not hidden by mount |
| `Cargo.toml` | `rust:<version>` | `cargo fetch` | `target/` handling |
| `go.mod` | `golang:<version>` | `go mod download` | module cache outside `/work` |

For Python projects: if the production Dockerfile uses a Playwright base image,
use the same one — browser binaries and OS libraries must match.

For Node projects: use an anonymous volume at `/work/node_modules` so the
image's installed dependencies are not hidden by the bind mount.

### 3. Write the environmentRef

The environmentRef declares how the spine runs tests against this repo:

```json
{
  "ref": "<registry>/<repo>:baseline",
  "mountPath": "/work",
  "validationCommands": ["<test command>"],
  "testCommand": "<test command>",
  "testFilePattern": "<regex matching test files only>",
  "dependencyFiles": ["<files that change the dependency layer>"]
}
```

**`testFilePattern` matters more than it looks.** Step 4 of the spine uses it to
decide whether the agent authored a test or merely edited code. Without it, a
`helpers.ts` or `conftest.py` counts as proof. Set it to match only actual test
files:

| Language | Pattern |
|----------|---------|
| Python (pytest) | `test_[^/]+\\.py$` |
| TypeScript/JS (vitest/jest) | `\\.(test\|spec)\\.[tj]sx?$` |
| Rust | `#\\[test\\]` (content match, not filename) |

Optionally include `buildCommand` (e.g. `npx tsc --noEmit`) and `testRanPattern`
(e.g. `Tests\\s+\\d+` for vitest, `passed` for pytest).

### 4. Pre-pull the image on runner boxes

Budget for the first pull. Playwright images are ~1.5 GB; Node images ~1 GB.
A 3.69 GB image cost Jerrad's box 20 minutes.

```bash
docker pull <registry>/<repo>:baseline
```

Do this on every runner box that will execute tasks for this repo, or accept the
cold-start cost on the first run.

## Verification

After all four steps, confirm:

1. `git ls-remote <gitea-url>` returns refs
2. `docker run --entrypoint cat <image> /etc/devrelay-env-sha` returns the expected commit
3. `docker run --entrypoint cat <image> /etc/devrelay-env-manifest` shows correct provenance
4. A test run inside the image succeeds:
   ```bash
   docker run --rm -v /path/to/repo:/work <image>
   ```

## Sending to Doug

If the Docker build and registry push must happen on another machine (Doug's box),
send the instructions via the spine messaging API:

```bash
curl -X POST https://<host>:8787/api/spine/agents/<participant>/messages \
  -H 'Content-Type: application/json' \
  -d '{"body": "<instructions>", "from": "chad-work"}'
```

The `devrelay-diag` channel's `devrelay_tell` is the preferred path when the diag
plane is correctly configured.
