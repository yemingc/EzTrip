import { expect, test } from "@playwright/test";

test("submits a real fixture planning task and renders its evidence", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "一份旅行计划，一条完整证据链。" })).toBeVisible();
  await page.getByTestId("submit-planning-task").click();

  const trace = page.getByTestId("event-trace");
  await expect(trace).toContainText("任务已入队");
  await expect(trace).toContainText("等待人工审核");

  const results = page.getByTestId("planning-results");
  await expect(results).toBeVisible({ timeout: 20_000 });
  await expect(results).toContainText("北京 · 2 日规划草案");
  await expect(results).toContainText("故宫博物院");
  await expect(results).toContainText("天坛公园");
  await expect(results).toContainText("Fixture 数据");
  await expect(results).toContainText("Product Graph V2");
  await expect(results).toContainText("explore");
  await expect(results).toContainText("stay");
  await expect(results).toContainText("weather");
  await expect(results).toContainText("首日优先室内或混合型活动");
  await expect(results).toContainText("价格与可订状态未验证，不提供预订");

  const approve = page.getByRole("button", { name: "批准草案" });
  await expect(approve).toBeEnabled();
  await approve.click();
  await expect(trace).toContainText("审核决定已接收");
  await expect(trace).toContainText("规划已完成");
  await expect(results).toContainText("审核已完成");
  await expect(results).toContainText("已批准草案");
  await expect(results).toContainText("v1 → v1");
  await expect(results).toContainText("计划未修改 · 0 个受影响日期");

  await page.screenshot({
    path: "test-results/eztrip-planning-workspace.png",
    fullPage: true,
  });
});

test("does not present missing cost facts as a zero-cost trip", async ({ page }) => {
  await page.goto("/");

  await page.getByLabel("整趟预算目标").fill("2000");
  await page.getByTestId("submit-planning-task").click();

  const results = page.getByTestId("planning-results");
  await expect(results).toBeVisible({ timeout: 20_000 });
  await expect(results).toContainText("费用事实缺失，不等于 0 元");
  await expect(results).toContainText("待补：交通、餐饮、门票、活动");
  await expect(results).toContainText("校验有提醒");
  const approve = page.getByRole("button", { name: "批准草案" });
  await expect(approve).toBeEnabled();
  await approve.click();
  await expect(results).toContainText("审核已完成");
  await expect(results).toContainText("已批准草案");
});

test("applies a structured day-scoped revision and renders plan version v2", async ({ page }) => {
  await page.goto("/");
  await page.getByTestId("submit-planning-task").click();
  const results = page.getByTestId("planning-results");
  await expect(results).toBeVisible({ timeout: 20_000 });

  await page.getByRole("button", { name: "局部修改" }).click();
  await page.getByLabel("活动延后时间").selectOption("120");
  await page.getByLabel("修改说明").fill("第二天想晚一点出发。");
  await page.getByRole("button", { name: "生成局部修改草案" }).click();

  await expect(page.getByTestId("event-trace")).toContainText("应用局部修改");
  await expect(results).toContainText("已生成修改草案");
  await expect(results).toContainText("v2 修改草案 · 尚未再次审核");
  await expect(results).toContainText("v1 → v2");
  await expect(results).toContainText("计划已修改 · 1 个受影响日期");
  await expect(results).toContainText("11:00 — 13:30");

  await page.screenshot({
    path: "test-results/eztrip-structured-revision-v2.png",
    fullPage: true,
  });
});

test("keeps the planning flow usable on a mobile viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");

  await expect(page.getByTestId("submit-planning-task")).toBeVisible();
  await page.getByTestId("submit-planning-task").click();
  await expect(page.getByTestId("planning-results")).toBeVisible({ timeout: 20_000 });

  await page.screenshot({
    path: "test-results/eztrip-planning-workspace-mobile.png",
    fullPage: true,
  });
});
