import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath, pathToFileURL } from "node:url";

export const EXCEPTION = Object.freeze({
  ghsa: "GHSA-vfj7-8cjw-p6xm",
  package: "braces",
  version: "3.0.3",
  path: ".>eslint-config-next>@next/eslint-plugin-next>fast-glob>micromatch>braces",
  expires: "2026-10-30T00:00:00Z",
});
const SEVERITIES = ["info", "low", "moderate", "high", "critical"];
const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const isCount = (value) => Number.isSafeInteger(value) && value >= 0;
const isText = (value) => typeof value === "string" && value.length > 0;

function readResult(result, label) {
  assert(!result.error && !result.signal, `${label}: command failed or timed out`);
  assert([0, 1].includes(result.status), `${label}: unexpected exit status ${result.status}`);
  assert(isText(result.stdout), `${label}: empty response`);
  const report = JSON.parse(result.stdout);
  assert(report !== null, `${label}: empty JSON response`);
  if (isObject(report)) {
    assert(!("error" in report) && !("errors" in report), `${label}: error response`);
  }
  return report;
}

// Read every severity so the advisory list and metadata must agree. The gate
// below still uses the existing high/critical threshold for both scans.
export function validateAudit(result, allowException = false) {
  const label = allowException ? "Full dependency audit" : "Production dependency audit";
  const report = readResult(result, label);
  assert(isObject(report) && isObject(report.advisories) && isObject(report.metadata), `${label}: malformed report`);
  const counts = report.metadata.vulnerabilities;
  assert(isObject(counts), `${label}: missing vulnerability counts`);
  assert.deepEqual(Object.keys(counts).sort(), [...SEVERITIES].sort(), `${label}: unknown or missing severity`);
  for (const field of ["dependencies", "devDependencies", "optionalDependencies", "totalDependencies"]) {
    assert(isCount(report.metadata[field]), `${label}: invalid ${field}`);
  }
  assert(report.metadata.totalDependencies > 0, `${label}: empty dependency scan`);
  if (allowException) {
    assert(report.metadata.devDependencies > 0, `${label}: development scope was excluded`);
    assert(report.metadata.optionalDependencies > 0, `${label}: optional scope was excluded`);
  }
  const actualCounts = Object.fromEntries(SEVERITIES.map((severity) => [severity, 0]));
  const advisories = Object.entries(report.advisories);
  for (const [id, advisory] of advisories) {
    assert(isObject(advisory), `${label}: malformed advisory`);
    assert(isCount(advisory.id) && String(advisory.id) === id, `${label}: invalid advisory ID`);
    assert(/^GHSA-[a-z0-9]{4}-[a-z0-9]{4}-[a-z0-9]{4}$/.test(advisory.github_advisory_id), `${label}: invalid GHSA`);
    assert(isText(advisory.module_name) && SEVERITIES.includes(advisory.severity), `${label}: invalid advisory package/severity`);
    assert(Array.isArray(advisory.findings) && advisory.findings.length > 0, `${label}: missing findings`);
    for (const finding of advisory.findings) {
      assert(isObject(finding) && isText(finding.version), `${label}: invalid finding`);
      assert(Array.isArray(finding.paths) && finding.paths.length > 0 && finding.paths.every(isText), `${label}: invalid paths`);
      for (const field of ["dev", "optional", "bundled"]) {
        assert(typeof finding[field] === "boolean", `${label}: invalid finding ${field}`);
      }
    }
    actualCounts[advisory.severity] += 1;
  }
  for (const severity of SEVERITIES) {
    assert(isCount(counts[severity]) && counts[severity] === actualCounts[severity], `${label}: inconsistent ${severity} count`);
  }
  assert.equal(result.status, advisories.length > 0 ? 1 : 0, `${label}: exit status disagrees with report`);

  let excepted = 0;
  for (const [, advisory] of advisories) {
    if (allowException && advisory.github_advisory_id === EXCEPTION.ghsa) {
      assert.equal(advisory.module_name, EXCEPTION.package, "Exception package changed; review/remove the exception");
      assert.equal(advisory.severity, "high", "Exception severity changed; review/remove the exception");
      assert.equal(advisory.findings.length, 1, "Exception findings changed; review/remove the exception");
      assert.deepEqual(advisory.findings[0], {
        version: EXCEPTION.version,
        paths: [EXCEPTION.path],
        dev: true,
        optional: false,
        bundled: false,
      }, "Exception dependency path or scope changed; review/remove the exception");
      excepted += 1;
    } else {
      assert(!["high", "critical"].includes(advisory.severity), `${label}: blocked ${advisory.github_advisory_id} (${advisory.severity}, ${advisory.module_name})`);
    }
  }
  if (allowException) {
    assert.equal(excepted, 1, "Exception missing or duplicated; review/remove the exception");
  }
}

export function validateExpiry(now) {
  assert(now instanceof Date && Number.isFinite(now.getTime()), "Invalid audit date");
  assert(now.getTime() < Date.parse(EXCEPTION.expires), `Audit exception expired at ${EXCEPTION.expires}; remove it or obtain a new review`);
}

export function validatePublishedVersions(result) {
  const versions = readResult(result, "braces registry lookup");
  assert.equal(result.status, 0, "braces registry lookup failed");
  assert(Array.isArray(versions) && versions.length > 0 && versions.includes(EXCEPTION.version), "Malformed braces versions response");
  for (const version of versions) {
    assert(typeof version === "string", "Malformed braces version");
    const parts = /^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?$/.exec(version);
    assert(parts, `Malformed braces version: ${version}`);
    const numbers = parts.slice(1, 4).map(Number);
    assert(numbers.every(Number.isSafeInteger), `Malformed braces version: ${version}`);
    for (const identifier of parts[4]?.split(".") ?? []) {
      assert(identifier && (!/^\d+$/.test(identifier) || /^(0|[1-9]\d*)$/.test(identifier)), `Malformed braces prerelease: ${version}`);
    }
    assert(!parts[5] || parts[5].split(".").every(isText), `Malformed braces build version: ${version}`);
    const [major, minor, patch] = numbers;
    // Conservatively require review for any release (including a prerelease)
    // beyond 3.0.3. No semver dependency or mutable registry tag is needed.
    assert(major < 3 || (major === 3 && minor === 0 && patch <= 3), `braces ${version} is published; upgrade/review and remove the exception`);
  }
}

function runPnpm(args) {
  assert(process.env.npm_execpath, "Run this guard with pnpm audit:security");
  return spawnSync(process.execPath, [process.env.npm_execpath, ...args], {
    cwd: fileURLToPath(new URL("..", import.meta.url)),
    encoding: "utf8",
    timeout: 120_000,
    maxBuffer: 10 * 1024 * 1024,
  });
}

export function auditDependencies(run = runPnpm, clock = () => new Date()) {
  validateExpiry(clock());
  validateAudit(run(["audit", "--json", "--audit-level", "info"]), true);
  validateAudit(run(["audit", "--prod", "--json", "--audit-level", "info"]));
  validatePublishedVersions(run(["view", "braces", "versions", "--json", "--registry=https://registry.npmjs.org"]));
  validateExpiry(clock());
  console.log(`Dependency audits passed with the single dev-only ${EXCEPTION.ghsa} exception until ${EXCEPTION.expires}. Production has no high/critical exceptions.`);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    auditDependencies();
  } catch (error) {
    console.error(`Dependency audit failed: ${error.message}`);
    process.exitCode = 1;
  }
}
