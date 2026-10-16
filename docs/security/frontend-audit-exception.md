# Temporary frontend dependency audit exception

Approved on 2026-10-16, this exception is limited to
[GHSA-vfj7-8cjw-p6xm](https://github.com/advisories/GHSA-vfj7-8cjw-p6xm):
`braces@3.0.3`, severity **high**, used only through this development lint path:

```text
.>eslint-config-next>@next/eslint-plugin-next>fast-glob>micromatch>braces
```

The advisory concerns stack-exhaustion denial of service from deeply nested
patterns. At approval time, the registry listed no braces version after 3.0.3;
the official GitHub advisory lists no patched version. pnpm derives a
`>=3.0.4` patched range from the affected range, which does not establish that
3.0.4 is published or fixed. This is an accepted, time-limited development-tool
risk, not a claim that the vulnerability is fixed.
Avoid running lint tooling against untrusted patterns during the exception.

## Enforced limits

Run `pnpm audit:security` from `frontend/`. CI runs `pnpm test:audit` first.
The guard has no added dependencies and uses Node's built-in test runner.

- The exception expires at **2026-10-30 00:00:00 UTC**. At that exact instant
  and afterward, the next CI/local audit run fails. It does not stop an already
  running application. There is no environment-variable override.
- The full dependency scan remains in place, including development and optional
  dependencies. A separate production dependency scan has **no exceptions**.
- Both scans request all severities with `pnpm audit --json --audit-level info`
  (production adds `--prod`). The guard preserves the previous **high/critical**
  failure threshold; lower-severity findings remain included and validated in
  the captured raw report.
- Raw JSON structure, advisory findings, severity totals, dependency counts and
  the command's exit status must agree. Audit/network errors, timeouts, missing
  data, malformed JSON and unexpected statuses fail closed.
  This project has development and optional dependencies; reporting either scope
  as empty in the full scan also fails, including accidental pnpm scope settings.
- Only one finding for the exact advisory, package, version and path above is
  accepted, with `dev: true`, `optional: false`, and `bundled: false`. Any changed
  dependency path/scope, missing/duplicate advisory, or changed severity requires
  review/removal. Every other high/critical advisory still fails.
- Every run also checks `pnpm view braces versions --json
  --registry=https://registry.npmjs.org` against the official npm registry. Any
  version beyond 3.0.3, including a prerelease, blocks the exception early and
  requires an upgrade/review. This conservative check does not claim that every
  newer release fixes the advisory. Registry failures or malformed version data
  also fail closed.
- No persistent audit ignore configuration is written. In pinned pnpm 11.19.0,
  `pnpm audit --ignore` writes the ignore to `pnpm-workspace.yaml` rather than
  acting as a one-shot audit gate, so it is intentionally not used. Neither
  `--ignore-unfixable` nor `--ignore-registry-errors` is permitted by this guard.

## Removal

When a fix is published, the dependency path changes, or the deadline is reached:

1. Upgrade/remove the affected dependency chain and regenerate the lockfile.
2. Remove this exception and its guard/test scripts, restoring the CI command
   `pnpm audit --audit-level high`; retain a separate
   `pnpm audit --prod --audit-level high` scan.
3. Run the complete CI suite (dependency audits, lint/typecheck/build, backend
   tests/migrations and browser E2E) before merging.

If a fix still cannot be applied, obtain a fresh review rather than extending
this deadline silently or broadening the allowed dependency path.
