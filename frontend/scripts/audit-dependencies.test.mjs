import assert from "node:assert/strict";
import test from "node:test";
import {
  EXCEPTION,
  auditDependencies,
  validateAudit,
  validateExpiry,
  validatePublishedVersions,
} from "./audit-dependencies.mjs";

const now = new Date("2026-10-16T00:00:00Z");
const allowed = () => ({
  id: 1240992,
  github_advisory_id: EXCEPTION.ghsa,
  module_name: "braces",
  severity: "high",
  findings: [{
    version: "3.0.3",
    paths: [EXCEPTION.path],
    dev: true,
    optional: false,
    bundled: false,
  }],
});
const other = (severity = "high") => ({ ...allowed(), id: 1240993, github_advisory_id: "GHSA-abcd-1234-5678", severity });
function report(advisories = [allowed()]) {
  const vulnerabilities = { info: 0, low: 0, moderate: 0, high: 0, critical: 0 };
  for (const advisory of advisories) vulnerabilities[advisory.severity] += 1;
  return {
    advisories: Object.fromEntries(advisories.map((advisory) => [advisory.id, advisory])),
    metadata: {
      vulnerabilities,
      dependencies: 62,
      devDependencies: 340,
      optionalDependencies: 88,
      totalDependencies: 440,
    },
  };
}
const result = (value, status = 0) => ({ stdout: JSON.stringify(value), stderr: "", status, signal: null });
const auditResult = (advisories = [allowed()]) => result(report(advisories), advisories.length > 0 ? 1 : 0);
const versionsResult = (versions = ["1.0.0", "2.3.2", "3.0.3"]) => result(versions);

test("only the exact reviewed development finding is excepted", () => {
  validateAudit(auditResult(), true);
  validateAudit(auditResult([]));
});
for (const severity of ["info", "low", "moderate"]) {
  test(`the original threshold still permits ${severity} findings`, () => {
    validateAudit(auditResult([allowed(), other(severity)]), true);
    validateAudit(auditResult([other(severity)]));
  });
}
for (const severity of ["high", "critical"]) {
  test(`another ${severity} advisory blocks both scans`, () => {
    assert.throws(() => validateAudit(auditResult([allowed(), other(severity)]), true), /blocked/);
    assert.throws(() => validateAudit(auditResult([other(severity)])), /blocked/);
  });
}
test("production has no exception, even for the approved GHSA", () => {
  assert.throws(() => validateAudit(auditResult()), /blocked/);
});
for (const field of ["devDependencies", "optionalDependencies"]) {
  test(`the full scan cannot silently exclude ${field}`, () => {
    const body = report();
    body.metadata[field] = 0;
    assert.throws(() => validateAudit(result(body, 1), true), /scope was excluded/);
  });
}
for (const [label, change] of [
  ["runtime dependency", (advisory) => { advisory.findings[0].dev = false; }],
  ["different version", (advisory) => { advisory.findings[0].version = "3.0.2"; }],
  ["different path", (advisory) => { advisory.findings[0].paths = [".>some-runtime-package>braces"]; }],
  ["additional path", (advisory) => { advisory.findings[0].paths.push(".>braces"); }],
  ["additional finding", (advisory) => { advisory.findings.push({ ...advisory.findings[0] }); }],
  ["optional dependency", (advisory) => { advisory.findings[0].optional = true; }],
  ["bundled dependency", (advisory) => { advisory.findings[0].bundled = true; }],
  ["changed package", (advisory) => { advisory.module_name = "another-package"; }],
  ["escalated severity", (advisory) => { advisory.severity = "critical"; }],
]) {
  test(`${label} invalidates the exception`, () => {
    const advisory = allowed();
    change(advisory);
    assert.throws(() => validateAudit(auditResult([advisory]), true), /Exception/);
  });
}
test("missing or duplicated exception requires removal/review", () => {
  assert.throws(() => validateAudit(auditResult([]), true), /Exception missing/);
  assert.throws(() => validateAudit(auditResult([allowed(), { ...allowed(), id: 1240993 }]), true), /Exception missing or duplicated/);
});
for (const [label, change] of [
  ["null body", () => null],
  ["array body", () => []],
  ["error body", () => ({ error: { code: "ERR_PNPM_AUDIT_BAD_RESPONSE" } })],
  ["error alongside report", (body) => ({ ...body, error: "registry failed" })],
  ["errors alongside report", (body) => ({ ...body, errors: [] })],
  ["missing metadata", (body) => { delete body.metadata; return body; }],
  ["missing advisories", (body) => { delete body.advisories; return body; }],
  ["inconsistent counts", (body) => { body.metadata.vulnerabilities.high = 0; return body; }],
  ["negative counts", (body) => { body.metadata.vulnerabilities.low = -1; return body; }],
  ["string counts", (body) => { body.metadata.vulnerabilities.high = "1"; return body; }],
  ["unknown severity count", (body) => { body.metadata.vulnerabilities.unknown = 0; return body; }],
  ["empty scan", (body) => { body.metadata.totalDependencies = 0; return body; }],
  ["invalid dependency count", (body) => { body.metadata.dependencies = null; return body; }],
  ["unknown advisory severity", (body) => { body.advisories[1240992].severity = "severe"; return body; }],
  ["invalid advisory ID", (body) => { body.advisories[1240992].id = 0; return body; }],
  ["missing GHSA", (body) => { delete body.advisories[1240992].github_advisory_id; return body; }],
  ["empty findings", (body) => { body.advisories[1240992].findings = []; return body; }],
  ["empty paths", (body) => { body.advisories[1240992].findings[0].paths = []; return body; }],
  ["invalid dev flag", (body) => { body.advisories[1240992].findings[0].dev = "true"; return body; }],
]) {
  test(`${label} fails closed`, () => {
    assert.throws(() => validateAudit(result(change(report()), 1), true));
  });
}
for (const [label, change] of [
  ["non-JSON", { stdout: "registry unavailable" }],
  ["empty output", { stdout: "" }],
  ["network/command error", { error: new Error("network unavailable") }],
  ["timeout", { signal: "SIGTERM", status: null }],
  ["unexpected exit code", { status: 2 }],
  ["success with a finding", { status: 0 }],
]) {
  test(`${label} cannot be mistaken for the allowed advisory`, () => {
    assert.throws(() => validateAudit({ ...auditResult(), ...change }, true));
  });
}
test("nonzero status with a clean report is an error", () => {
  assert.throws(() => validateAudit({ ...auditResult([]), status: 1 }), /exit status/);
});
test("expiry is an exclusive UTC boundary", () => {
  validateExpiry(new Date("2026-10-29T23:59:59.999Z"));
  assert.throws(() => validateExpiry(new Date(EXCEPTION.expires)), /expired/);
  assert.throws(() => validateExpiry(new Date("2026-10-30T00:00:00.001Z")), /expired/);
  assert.throws(() => validateExpiry(new Date("invalid")), /Invalid audit date/);
});
test("known published versions permit the temporary exception", () => {
  validatePublishedVersions(versionsResult());
});
for (const version of ["3.0.4", "3.0.10", "3.1.0", "4.0.0", "3.0.4-rc.1"]) {
  test(`publication of ${version} blocks the exception early`, () => {
    assert.throws(() => validatePublishedVersions(versionsResult(["3.0.3", version])), /published/);
  });
}
for (const value of [null, {}, [], ["3.0.4"], ["3.0.3", "invalid"], ["3.0.3", "3.0.3-."], ["3.0.3", "3.0.3-01"], ["3.0.3", "3.0.3+.."], ["3.0.3", "1.0.9999999999999999999"], ["3.0.3", 4], { error: "registry unavailable" }]) {
  test(`malformed version response ${JSON.stringify(value)} fails closed`, () => {
    assert.throws(() => validatePublishedVersions(result(value)));
  });
}
test("registry failure never grants an exception", () => {
  assert.throws(() => validatePublishedVersions({ ...versionsResult(), status: 1 }), /lookup failed/);
  assert.throws(() => validatePublishedVersions({ ...versionsResult(), error: new Error("network") }), /command failed/);
  assert.throws(() => validatePublishedVersions({ ...versionsResult(), stdout: "<html>error</html>" }));
});
test("orchestration scans all dependencies, then production with no ignore, then versions", () => {
  const calls = [];
  const responses = [auditResult(), auditResult([]), versionsResult()];
  auditDependencies((args) => { calls.push(args); return responses.shift(); }, () => now);
  assert.deepEqual(calls, [
    ["audit", "--json", "--audit-level", "info"],
    ["audit", "--prod", "--json", "--audit-level", "info"],
    ["view", "braces", "versions", "--json", "--registry=https://registry.npmjs.org"],
  ]);
});
test("orchestration cannot waive a production advisory", () => {
  const responses = [auditResult(), auditResult()];
  assert.throws(() => auditDependencies(() => responses.shift(), () => now), /Production dependency audit: blocked/);
});
test("orchestration stops before commands when the exception expires", () => {
  assert.throws(() => auditDependencies(() => assert.fail("must not run"), () => new Date(EXCEPTION.expires)), /expired/);
});

test("a scan crossing the expiry boundary fails before success", () => {
  const responses = [auditResult(), auditResult([]), versionsResult()];
  const dates = [new Date("2026-10-29T23:59:59.999Z"), new Date(EXCEPTION.expires)];
  assert.throws(() => auditDependencies(() => responses.shift(), () => dates.shift()), /expired/);
  assert.equal(responses.length, 0);
  assert.equal(dates.length, 0);
});
