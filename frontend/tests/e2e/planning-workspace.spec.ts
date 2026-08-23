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
  await expect(results).toContainText("当前产品 API 工作流尚未注入天气风险");

  await expect(page.getByRole("button", { name: "批准草案" })).toBeDisabled();
  await expect(page.getByRole("button", { name: "请求修改" })).toBeDisabled();

  await page.screenshot({
    path: "test-results/eztrip-planning-workspace.png",
    fullPage: true,
  });
});

test("does not present missing cost facts as a zero-cost trip", async ({ page }) => {
  await page.goto("/");

  await page.getByLabel("整趟预算（可选）").fill("2000");
  await page.getByTestId("submit-planning-task").click();

  const results = page.getByTestId("planning-results");
  await expect(results).toBeVisible({ timeout: 20_000 });
  await expect(results).toContainText("费用事实缺失，不等于 0 元");
  await expect(results).toContainText("待补：交通、餐饮、门票、活动");
  await expect(results).toContainText("存在硬冲突");
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
