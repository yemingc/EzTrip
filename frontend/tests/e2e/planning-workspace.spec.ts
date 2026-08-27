import { expect, test, type Page } from "@playwright/test";

async function understandAndStart(
  page: Page,
  selection: "proposal" | "form" = "proposal",
) {
  await page.getByTestId("submit-planning-task").click();
  const confirmation = page.getByTestId("request-intake-confirmation");
  await expect(confirmation).toBeVisible({ timeout: 20_000 });
  if (selection === "form") {
    await confirmation.getByText("保留表单中的差异值", { exact: true }).click();
  }
  await page.getByTestId("confirm-request-intake").click();
}

function dateInChinaAfter(days: number) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date(Date.now() + days * 24 * 60 * 60 * 1000));
  const values = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  return `${values.year}-${values.month}-${values.day}`;
}

function addCalendarDays(value: string, days: number) {
  const result = new Date(`${value}T00:00:00Z`);
  result.setUTCDate(result.getUTCDate() + days);
  return result.toISOString().slice(0, 10);
}

function collectPageErrors(page: Page) {
  const errors: string[] = [];
  page.on("console", (message) => {
    if (message.type() === "error") errors.push(`console: ${message.text()}`);
  });
  page.on("pageerror", (error) => errors.push(`pageerror: ${error.message}`));
  return errors;
}

test("allows trips starting today while defaulting to one week later", async ({ page }) => {
  const todayBeforeNavigation = dateInChinaAfter(0);
  await page.goto("/");

  const startDate = page.getByLabel("出发日期");
  const todayAfterNavigation = dateInChinaAfter(0);
  const earliestStartDate = await startDate.getAttribute("min");
  expect([todayBeforeNavigation, todayAfterNavigation]).toContain(earliestStartDate);
  expect(earliestStartDate).not.toBeNull();
  await expect(startDate).toHaveValue(addCalendarDays(earliestStartDate!, 7));

  await startDate.fill(earliestStartDate!);
  await expect(startDate).toHaveValue(earliestStartDate!);
});

test("merges matching request details without showing a redundant source chooser", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByTestId("submit-planning-task").click();

  const confirmation = page.getByTestId("request-intake-confirmation");
  await expect(confirmation).toBeVisible({ timeout: 20_000 });
  await expect(confirmation.getByTestId("merged-intake-notice")).toContainText(
    "未发现冲突",
  );
  await expect(confirmation.getByTestId("request-conflict-selection")).toHaveCount(0);
  await expect(confirmation).not.toContainText("采用旅行需求");
  await expect(confirmation).not.toContainText("使用当前表单");
});

test("keeps the current page position when checking a travel request", async ({ page }) => {
  await page.goto("/");
  const submitButton = page.getByTestId("submit-planning-task");
  await submitButton.scrollIntoViewIfNeeded();
  await page.evaluate(() => {
    const browserWindow = window as Window & {
      __eztripReloadProbe?: string;
      __eztripReplaceStateCalls?: number;
    };
    browserWindow.__eztripReloadProbe = "present";
    browserWindow.__eztripReplaceStateCalls = 0;
    const originalReplaceState = window.history.replaceState.bind(window.history);
    window.history.replaceState = (data, unused, url) => {
      browserWindow.__eztripReplaceStateCalls =
        (browserWindow.__eztripReplaceStateCalls ?? 0) + 1;
      originalReplaceState(data, unused, url);
    };
  });
  const scrollBefore = await page.evaluate(() => window.scrollY);

  await submitButton.click();
  await expect(page.getByTestId("request-intake-confirmation")).toBeVisible({ timeout: 20_000 });

  const pageState = await page.evaluate(() => ({
    marker: (window as Window & { __eztripReloadProbe?: string }).__eztripReloadProbe,
    replaceStateCalls: (
      window as Window & { __eztripReplaceStateCalls?: number }
    ).__eztripReplaceStateCalls,
    scrollY: window.scrollY,
  }));
  expect(pageState.marker).toBe("present");
  expect(pageState.replaceStateCalls).toBe(0);
  expect(Math.abs(pageState.scrollY - scrollBefore)).toBeLessThan(40);
  await expect(page.getByRole("status")).toContainText("需求已整理");
});

test("generates a complete sample itinerary with user-facing copy", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "把想去的地方，变成一份好用的行程。" })).toBeVisible();
  await understandAndStart(page);

  const results = page.getByTestId("planning-results");
  await expect(results).toBeVisible({ timeout: 20_000 });
  await expect(results).toBeInViewport();
  await expect(page.getByTestId("planning-task-summary")).toContainText("行程已生成");
  await expect(page.getByTestId("request-intake-confirmation")).toHaveCount(0);
  const trace = page.getByTestId("event-trace");
  await expect(trace).toContainText("已收到旅行需求");
  await expect(trace).toContainText("调整不合理安排");
  await expect(trace).toContainText("行程等待确认");

  await expect(results).toContainText("北京 · 2 天行程");
  await expect(results).toContainText("故宫博物院");
  await expect(results).toContainText("天坛公园");
  await expect(results).toContainText("中国国家博物馆");
  await expect(results).toContainText("景山公园");
  await expect(results.getByTestId("itinerary-item")).toHaveCount(4);
  await expect(results).toContainText("从住宿地点出发前往首站");
  await expect(results).toContainText("附近用餐建议");
  await expect(results).toContainText("首日优先室内或混合型活动");
  const weather = results.getByTestId("weather-outlook");
  await expect(weather).toContainText("逐日天气提醒");
  expect(await weather.getByTestId("weather-day-card").count()).toBeGreaterThan(0);
  await expect(weather).toContainText("降雨");
  await expect(weather).toContainText("当天怎么安排");
  await expect(weather.getByTestId("weather-affected-plans").first()).toContainText("优先检查");
  const weatherProposal = weather.getByTestId("weather-replacement-proposal").first();
  await expect(weatherProposal).toContainText("全天室内方案");
  await expect(weatherProposal).toContainText(/天坛公园\s*→\s*首都博物馆/);
  await expect(weatherProposal).toContainText(/景山公园\s*→\s*北京天文馆/);
  await expect(
    weatherProposal.getByRole("button", { name: "采用全天室内方案" }),
  ).toBeVisible();
  await weatherProposal.getByRole("button", { name: "保留原安排" }).click();
  await expect(weatherProposal).not.toBeVisible();
  await expect(results.getByTestId("itinerary-item").filter({ hasText: "天坛公园" })).toBeVisible();
  await expect(
    results.getByTestId("itinerary-item").filter({ hasText: "景山公园" }),
  ).toBeVisible();
  expect(await weather.innerText()).not.toMatch(/\b(?:rain|heat|low|medium|high|extreme)\b/);
  await expect(results.getByTestId("stay-recommendation")).toContainText("住宿推荐");
  await expect(results.getByTestId("stay-recommendation")).toContainText("推荐理由");
  await expect(results.getByTestId("stay-recommendation")).toContainText("价格和空房情况以预订平台为准");
  await expect(results.getByTestId("activity-description")).toHaveCount(4);
  await expect(results.getByTestId("activity-reason")).toHaveCount(4);
  await expect(results.getByTestId("activity-source")).toHaveCount(4);
  await expect(results.getByTestId("validation-summary")).toBeVisible();
  await expect(results.getByTestId("data-disclosure")).toBeVisible();
  await expect(results).not.toContainText("信息来源");
  await expect(results).toContainText("示例体验暂不显示地图");
  await expect(results.getByTestId("budget-estimate")).toContainText("基于当前行程估算");
  await expect(results.getByTestId("budget-estimate")).toContainText("住宿");
  await expect(results.getByTestId("budget-estimate")).toContainText("不代表实时成交价");
  const repair = page.getByTestId("product-repair-summary");
  await expect(repair).toContainText("行程已自动调整");
  await expect(repair).toContainText("已调整 1 次");
  await expect(repair).toContainText("营业时间冲突已解决");

  const visiblePageText = await page.locator("body").innerText();
  for (const forbiddenTerm of [
    "Agent",
    "Provider",
    "Fixture",
    "HITL",
    "SSE",
    "LangGraph",
    "Product Graph",
    "可回放",
    "不占活动名额",
    "模型调用",
    "工具调用",
    "checkpoint",
    "API:",
    "结构化",
    "确定性",
    "工作流",
    "可追溯",
    "住宿锚点",
    "evidence",
    "validator",
    "schema",
  ]) {
    expect(visiblePageText).not.toContain(forbiddenTerm);
  }

  const approve = page.getByRole("button", { name: "确认行程" });
  await expect(approve).toBeEnabled();
  await approve.click();
  await expect(trace).toContainText("已收到你的选择");
  await expect(trace).toContainText("行程已完成");
  await expect(results).toContainText("本次选择已保存");
  await expect(results).toContainText("行程已确认");
  await expect(results).toContainText("v1 → v1");
  await expect(results).toContainText("计划未修改 · 0 个受影响日期");

  await page.screenshot({
    path: "test-results/eztrip-planning-workspace.png",
    fullPage: true,
  });
});

test("confirms a grounded full-day indoor plan and only revises the affected day", async ({
  page,
}) => {
  const pageErrors = collectPageErrors(page);
  await page.goto("/");
  await understandAndStart(page);
  const results = page.getByTestId("planning-results");
  await expect(results).toBeVisible({ timeout: 20_000 });
  const protectedDayBefore = await results
    .getByTestId("itinerary-item")
    .filter({ hasText: "中国国家博物馆" })
    .textContent();

  const proposal = results.getByTestId("weather-replacement-proposal").first();
  await expect(proposal).toContainText(/天坛公园\s*→\s*首都博物馆/);
  await expect(proposal).toContainText(/景山公园\s*→\s*北京天文馆/);
  await page.screenshot({
    path: "test-results/eztrip-weather-replacement-hitl.png",
    fullPage: true,
  });
  await proposal.screenshot({
    path: "test-results/eztrip-weather-replacement-card.png",
  });
  await proposal.getByRole("button", { name: "采用全天室内方案" }).click();

  await expect(page.getByTestId("event-trace")).toContainText("更新所选行程");
  await expect(results).toContainText("修改版 · 等待再次确认");
  await expect(
    results.getByTestId("itinerary-item").filter({ hasText: "首都博物馆" }),
  ).toBeVisible();
  await expect(
    results.getByTestId("itinerary-item").filter({ hasText: "北京天文馆" }),
  ).toBeVisible();
  await expect(
    results.getByTestId("itinerary-item").filter({ hasText: "天坛公园" }),
  ).toHaveCount(0);
  await expect(
    results.getByTestId("itinerary-item").filter({ hasText: "景山公园" }),
  ).toHaveCount(0);
  await expect(results).toContainText("计划已修改 · 1 个受影响日期");
  await expect(
    results.getByTestId("itinerary-item").filter({ hasText: "中国国家博物馆" }),
  ).toHaveText(protectedDayBefore ?? "");
  expect(pageErrors).toEqual([]);
});

test("restores one task across in-flight, review, and completed refreshes", async ({ page }) => {
  await page.goto("/");
  await understandAndStart(page);

  await expect(page).toHaveURL(/\?task_id=planning-task-[a-f0-9]{32}$/);
  const taskUrl = page.url();
  await page.reload();
  await expect(page).toHaveURL(taskUrl);

  const trace = page.getByTestId("event-trace");
  const results = page.getByTestId("planning-results");
  await expect(results).toBeVisible({ timeout: 20_000 });
  await expect(trace).toContainText("已收到旅行需求");
  await expect(trace).toContainText("行程等待确认");
  await expect(page.getByRole("button", { name: "确认行程" })).toBeEnabled();

  await page.reload();
  await expect(page).toHaveURL(taskUrl);
  await expect(results).toBeVisible({ timeout: 20_000 });
  await expect(page.getByRole("button", { name: "确认行程" })).toBeEnabled();
  await page.getByRole("button", { name: "确认行程" }).click();
  await expect(results).toContainText("本次选择已保存");
  await expect(trace).toContainText("行程已完成");

  await page.reload();
  await expect(page).toHaveURL(taskUrl);
  await expect(results).toContainText("本次选择已保存", { timeout: 20_000 });
  await expect(results).toContainText("行程已确认");
  await expect(trace).toContainText("已收到你的选择");
  await expect(trace).toContainText("行程已完成");
});

test("does not present missing weather coverage as risk-free", async ({ page }) => {
  await page.route(/\/api\/planning-tasks\/planning-task-[^/]+$/, async (route) => {
    const response = await route.fetch();
    const payload = (await response.json()) as {
      result?: {
        state?: {
          plan?: { weather_risks?: unknown[]; days?: Array<{ weather_risk_ids?: unknown[] }> };
          plan_agent?: {
            plan?: { weather_risks?: unknown[]; days?: Array<{ weather_risk_ids?: unknown[] }> };
          };
          repair?: {
            final_plan?: {
              weather_risks?: unknown[];
              days?: Array<{ weather_risk_ids?: unknown[] }>;
            };
          };
        };
      };
      plan_versions?: Array<{
        plan?: { weather_risks?: unknown[]; days?: Array<{ weather_risk_ids?: unknown[] }> };
      }>;
    };
    const plans = [
      payload.result?.state?.plan,
      payload.result?.state?.plan_agent?.plan,
      payload.result?.state?.repair?.final_plan,
      ...(payload.plan_versions ?? []).map((version) => version.plan),
    ];
    for (const plan of plans) {
      if (!plan) continue;
      plan.weather_risks = [];
      for (const day of plan.days ?? []) day.weather_risk_ids = [];
    }
    await route.fulfill({ response, json: payload });
  });

  await page.goto("/");
  await understandAndStart(page);

  const weather = page.getByTestId("weather-outlook");
  await expect(weather).toContainText("当前没有逐日风险信息", { timeout: 20_000 });
  await expect(weather).toContainText("这不代表天气一定适宜");
  await expect(weather).toContainText("可能尚未进入短期预报范围");
  await expect(weather).not.toContainText("暂未发现需要特别提醒的天气风险");
});

test("keeps low rain as a notice and reveals indoor backups on demand", async ({ page }) => {
  const pageErrors = collectPageErrors(page);
  await page.route(/\/api\/planning-tasks\/planning-task-[^/]+$/, async (route) => {
    const response = await route.fetch();
    type MutablePlan = {
      end_date?: string;
      weather_risks?: Array<{ severity?: string }>;
      days?: Array<{
        date: string;
        items?: Array<{ item_id: string; start_at: string; end_at: string }>;
      }>;
    };
    const payload = (await response.json()) as {
      result?: {
        state?: {
          plan?: MutablePlan;
          plan_agent?: { plan?: MutablePlan };
          repair?: { final_plan?: MutablePlan };
          revision_result?: { revised_plan?: MutablePlan };
        };
      };
      plan_versions?: Array<{ plan?: MutablePlan }>;
    };
    const plans = [
      payload.result?.state?.plan,
      payload.result?.state?.plan_agent?.plan,
      payload.result?.state?.repair?.final_plan,
      payload.result?.state?.revision_result?.revised_plan,
      ...(payload.plan_versions ?? []).map((version) => version.plan),
    ];
    for (const plan of plans) {
      for (const risk of plan?.weather_risks ?? []) risk.severity = "low";
      const sourceDay = plan?.days?.at(-1);
      if (!plan?.days || plan.days.length !== 2 || !sourceDay) continue;
      const duplicateDate = addCalendarDays(sourceDay.date, 1);
      plan.days.push({
        ...structuredClone(sourceDay),
        date: duplicateDate,
        items: sourceDay.items?.map((item) => ({
          ...item,
          item_id: `${item.item_id}-duplicate-day`,
          start_at: item.start_at.replace(sourceDay.date, duplicateDate),
          end_at: item.end_at.replace(sourceDay.date, duplicateDate),
        })),
      });
      plan.end_date = duplicateDate;
    }
    await route.fulfill({ response, json: payload });
  });

  await page.goto("/");
  await understandAndStart(page);

  const weather = page.getByTestId("weather-outlook");
  await expect(weather).toContainText("3 天有天气提醒", { timeout: 20_000 });
  await expect(weather).toContainText("小雨通常不必调整全天行程");
  await expect(weather).not.toContainText("建议调整安排");
  await expect(weather.getByTestId("weather-replacement-proposal")).toHaveCount(0);

  const backupButtons = weather.getByRole("button", { name: "查看雨天备选" });
  await expect(backupButtons).toHaveCount(2);
  await backupButtons.first().click();
  await weather.getByRole("button", { name: "查看雨天备选" }).first().click();
  const backups = weather.getByTestId("low-risk-weather-backup-panel");
  await expect(backups).toHaveCount(2);
  await expect(backups).toContainText(["雨天可以这样换", "雨天可以这样换"]);
  await expect(weather).toContainText("首都博物馆");
  await expect(weather).toContainText("北京天文馆");
  await expect(weather.getByTestId("weather-replacement-option")).toHaveCount(2);
  await expect(weather.getByRole("button", { name: "使用这个备选" })).toHaveCount(2);
  await expect(weather).not.toContainText("当前找到");
  await expect(weather).not.toContainText("不足以覆盖");
  await expect(weather).not.toContainText("原行程保持不变");
  await weather.screenshot({ path: "test-results/eztrip-low-rain-unique-backups.png" });
  expect(pageErrors).toEqual([]);
});

test("uses one low-rain backup as a scoped revision", async ({ page }) => {
  await page.route(/\/api\/planning-tasks\/planning-task-[^/]+$/, async (route) => {
    const response = await route.fetch();
    const payload = (await response.json()) as {
      result?: {
        state?: {
          plan?: { weather_risks?: Array<{ severity?: string }> };
          plan_agent?: { plan?: { weather_risks?: Array<{ severity?: string }> } };
          repair?: { final_plan?: { weather_risks?: Array<{ severity?: string }> } };
          revision_result?: { revised_plan?: { weather_risks?: Array<{ severity?: string }> } };
        };
      };
      plan_versions?: Array<{ plan?: { weather_risks?: Array<{ severity?: string }> } }>;
    };
    const plans = [
      payload.result?.state?.plan,
      payload.result?.state?.plan_agent?.plan,
      payload.result?.state?.repair?.final_plan,
      payload.result?.state?.revision_result?.revised_plan,
      ...(payload.plan_versions ?? []).map((version) => version.plan),
    ];
    for (const plan of plans) {
      for (const risk of plan?.weather_risks ?? []) risk.severity = "low";
    }
    await route.fulfill({ response, json: payload });
  });

  await page.goto("/");
  await understandAndStart(page);

  const weather = page.getByTestId("weather-outlook");
  await weather.getByRole("button", { name: "查看雨天备选" }).click();
  await weather.getByRole("button", { name: "使用这个备选" }).first().click();
  await expect(page.getByTestId("event-trace")).toContainText("更新所选行程", {
    timeout: 20_000,
  });
  await expect(page.getByTestId("planning-results")).toContainText(
    "修改版 · 等待再次确认",
  );
});

test("renders three main activities per day in standard pace without counting meals", async ({ page }) => {
  await page.goto("/");

  await page.getByLabel("行程节奏").selectOption("standard");
  await understandAndStart(page, "form");

  const results = page.getByTestId("planning-results");
  await expect(results).toBeVisible({ timeout: 20_000 });
  await expect(results.getByTestId("itinerary-item")).toHaveCount(6);
  await expect(results.getByTestId("meal-recommendations")).toHaveCount(2);
  await expect(results).not.toContainText("不占活动名额");
});

test("renders a plan when the API omits empty meal recommendations", async ({ page }) => {
  await page.route(/\/api\/planning-tasks\/planning-task-[^/]+$/, async (route) => {
    const response = await route.fetch();
    const payload = (await response.json()) as {
      result?: {
        state?: {
          plan?: {
            days?: Array<{ meal_recommendations?: unknown }>;
          };
          plan_agent?: {
            plan?: {
              days?: Array<{ meal_recommendations?: unknown }>;
            };
          };
          repair?: {
            final_plan?: {
              days?: Array<{ meal_recommendations?: unknown }>;
            };
          };
        };
      };
      plan_versions?: Array<{
        plan?: {
          days?: Array<{ meal_recommendations?: unknown }>;
        };
      }>;
    };
    const plans = [
      payload.result?.state?.plan,
      payload.result?.state?.plan_agent?.plan,
      payload.result?.state?.repair?.final_plan,
      ...(payload.plan_versions ?? []).map((version) => version.plan),
    ];
    for (const plan of plans) {
      for (const day of plan?.days ?? []) {
        delete day.meal_recommendations;
      }
    }
    await route.fulfill({ response, json: payload });
  });

  await page.goto("/");
  await understandAndStart(page);

  const results = page.getByTestId("planning-results");
  await expect(results).toBeVisible({ timeout: 20_000 });
  await expect(
    results.getByText("附近暂时没有合适的用餐推荐。"),
  ).toHaveCount(2);
});

test("does not present missing cost facts as a zero-cost trip", async ({ page }) => {
  const pageErrors = collectPageErrors(page);
  await page.goto("/");

  await page.getByLabel("整趟预算目标").fill("1000");
  await understandAndStart(page);

  const results = page.getByTestId("planning-results");
  await expect(results).toBeVisible({ timeout: 20_000 });
  const estimate = results.getByTestId("budget-estimate");
  await expect(estimate).toContainText("你的预算：¥1,000");
  await expect(estimate.getByTestId("budget-estimate-total")).toContainText("约 ¥1,370");
  await expect(estimate.getByTestId("budget-estimate-range")).toContainText(
    "参考范围 ¥924–¥1,822",
  );
  await expect(estimate.getByTestId("budget-estimate-breakdown")).toBeVisible();
  await expect(estimate).toContainText("住宿");
  await expect(estimate).toContainText("交通");
  await expect(estimate).toContainText("餐饮");
  await expect(estimate).toContainText("前门示例住宿 · 1 间夜");
  await expect(estimate).toContainText("当前 4 个活动 · 按每人 6 段市内出行估算");
  await expect(estimate).toContainText("12 人餐 · 按每天三餐的常规消费估算");
  await expect(estimate).toContainText("4 个活动 · 2 人");
  await expect(estimate).toContainText("不代表实时成交价");
  await expect(estimate).toContainText("最多可能超出约 ¥822");
  await expect(estimate.getByTestId("budget-advice")).toContainText("调整建议");
  await expect(results).toContainText("行程可用 · 请留意提示");
  const approve = page.getByRole("button", { name: "确认行程" });
  await expect(approve).toBeEnabled();
  await approve.click();
  await expect(results).toContainText("本次选择已保存");
  await expect(results).toContainText("行程已确认");
  expect(pageErrors).toEqual([]);
});

test("resolves Shanghai and plans a three-day fixture trip", async ({ page }) => {
  await page.goto("/");

  await page.getByLabel("目的城市").fill("上海");
  await page.getByLabel("行程天数").selectOption("3");
  await understandAndStart(page);

  await expect(page.getByTestId("destination-resolution")).toContainText("已确认目的地：上海市");
  const results = page.getByTestId("planning-results");
  await expect(results).toBeVisible({ timeout: 20_000 });
  await expect(results).toContainText("上海 · 3 天行程");
  await expect(results).toContainText("上海博物馆");
  await expect(results).toContainText("豫园");
});

test("requires confirmation when a destination name is ambiguous", async ({ page }) => {
  await page.goto("/");

  await page.getByLabel("目的城市").fill("朝阳");
  await page.getByTestId("submit-planning-task").click();

  const ambiguity = page.getByTestId("destination-ambiguity");
  await expect(ambiguity).toBeVisible();
  await expect(ambiguity).toContainText("北京市朝阳区");
  await expect(ambiguity).toContainText("辽宁省朝阳市");
  await expect(page.getByTestId("event-trace")).toContainText("开始后，这里会显示规划进度");
});

test("does not pretend fixture mode supports an arbitrary city", async ({ page }) => {
  await page.goto("/");

  await page.getByLabel("目的城市").fill("泉州");
  await page.getByTestId("submit-planning-task").click();

  await expect(page.getByTestId("planning-error")).toContainText(
    "示例体验仅支持北京、上海和成都",
  );
  await expect(page.getByTestId("planning-results")).not.toBeVisible();
});

test("explains real-time planning in user-facing language", async ({ page }) => {
  await page.goto("/");

  await page.getByLabel("规划方式").selectOption("live");

  await expect(page.getByText("将查询实时地点和路线信息", { exact: false })).toBeVisible();
  await expect(page.getByText("开放时间、票价和房态请在出发前再次确认", { exact: false })).toBeVisible();
});

test("renders the live itinerary on a proxied AMap basemap", async ({ page }) => {
  await page.route(/\/api\/maps\/static-plan/, async (route) => {
    await route.fulfill({
      body: '<svg xmlns="http://www.w3.org/2000/svg" width="800" height="420"><rect width="800" height="420" fill="#dbeafe"/></svg>',
      contentType: "image/svg+xml",
      status: 200,
    });
  });
  await page.route(/\/api\/planning-tasks\/planning-task-[^/]+$/, async (route) => {
    const response = await route.fetch();
    const payload = (await response.json()) as { data_mode: "fixture" | "live" };
    payload.data_mode = "live";
    await route.fulfill({ response, json: payload });
  });

  await page.goto("/");
  await understandAndStart(page);

  const map = page.getByTestId("amap-static-map");
  await expect(map).toBeVisible({ timeout: 20_000 });
  await expect(map).toHaveAttribute("src", /\/api\/maps\/static-plan\?poi=/);
});

test("explains verification gaps without calling every error a hard conflict", async ({ page }) => {
  await page.route(/\/api\/planning-tasks\/planning-task-[^/]+$/, async (route) => {
    const response = await route.fetch();
    const payload = (await response.json()) as {
      result?: {
        state?: {
          validation?: {
            status: string;
            can_finalize: boolean;
            issues: unknown[];
          };
          review_request?: {
            kind: string;
            prompt: string;
            allowed_actions: string[];
            validation_status: string;
            can_finalize: boolean;
            issue_rule_codes: string[];
          };
        };
      };
    };
    const state = payload.result?.state;
    if (state?.validation && state.review_request) {
      state.validation.status = "conflicted";
      state.validation.can_finalize = false;
      state.validation.issues = [
        {
          issue_id: "issue-route-missing-e2e",
          rule_code: "route.missing_for_grounded_item",
          severity: "error",
          message: "带来源的行程活动缺少到达路线。",
          evidence: [
            {
              field_path: "plan.days.items.route_from_previous",
              description: "缺少路线的 item_id",
              observed_value: "plan-item-day-2-palace-museum",
            },
          ],
          responsible_node: "route",
          repairable: true,
          repair_action: "rerun_route",
          requires_user_confirmation: false,
        },
      ];
      state.review_request.kind = "conflict_resolution";
      state.review_request.prompt = "旧的通用硬冲突文案";
      state.review_request.allowed_actions = [
        "acknowledge_conflict",
        "request_revision",
        "cancel",
      ];
      state.review_request.validation_status = "conflicted";
      state.review_request.can_finalize = false;
      state.review_request.issue_rule_codes = ["route.missing_for_grounded_item"];
    }
    await route.fulfill({ response, json: payload });
  });

  await page.goto("/");
  await understandAndStart(page);

  const results = page.getByTestId("planning-results");
  await expect(results).toContainText("出发前需要确认");
  await expect(results.getByTestId("validation-summary")).toContainText("一段到达路线未取得");
  await expect(page.getByRole("button", { name: "已了解，保留行程" })).toBeVisible();
  await expect(results).not.toContainText("存在硬冲突");
});

test("applies a structured day-scoped revision and renders plan version v2", async ({ page }) => {
  await page.goto("/");
  await understandAndStart(page);
  const results = page.getByTestId("planning-results");
  await expect(results).toBeVisible({ timeout: 20_000 });

  await page.getByRole("button", { name: "局部修改" }).click();
  await page.getByLabel("活动延后时间").selectOption("120");
  await page.getByLabel("修改说明").fill("第二天想晚一点出发。");
  await page.getByRole("button", { name: "生成修改版" }).click();

  await expect(page.getByTestId("event-trace")).toContainText("更新所选行程");
  await expect(results).toContainText("修改版已生成");
  await expect(results).toContainText("修改版 · 等待再次确认");
  await expect(results).toContainText("v1 → v2");
  await expect(results).toContainText("计划已修改 · 1 个受影响日期");
  await expect(results).toContainText("12:00 — 14:00");

  await page.screenshot({
    path: "test-results/eztrip-structured-revision-v2.png",
    fullPage: true,
  });
});

test("replaces activities on two different days without ending review after the first", async ({
  page,
}) => {
  await page.goto("/");
  await understandAndStart(page);
  const results = page.getByTestId("planning-results");
  await expect(results).toBeVisible({ timeout: 20_000 });
  const protectedDayBefore = await results
    .getByTestId("itinerary-item")
    .filter({ hasText: "中国国家博物馆" })
    .textContent();

  await page.getByRole("button", { name: "局部修改" }).click();
  await page.getByLabel("修改方式").selectOption("replace_activity");
  await page.getByLabel("被替换活动").selectOption({ label: "景山公园" });
  await page.getByLabel("可选地点").selectOption({ label: "北海公园 · 西城区" });
  await page.getByLabel("修改说明").fill("把第二天的景山公园换成同一观察池里的北海公园。");
  await page.getByRole("button", { name: "生成修改版" }).click();

  await expect(page.getByTestId("event-trace")).toContainText("更新所选行程");
  await expect(results).toContainText("修改版 · 等待再次确认");
  await expect(results).toContainText("北海公园");
  await expect(
    results.getByTestId("itinerary-item").filter({ hasText: "景山公园" }),
  ).toHaveCount(0);
  await expect(results).toContainText("计划已修改 · 1 个受影响日期");
  await expect(
    results.getByTestId("itinerary-item").filter({ hasText: "中国国家博物馆" }),
  ).toHaveText(protectedDayBefore ?? "");

  await expect(page.getByLabel("修改方式")).toBeVisible();
  await page.getByLabel("修改目标日期").selectOption({ index: 0 });
  await page.getByLabel("被替换活动").selectOption({ label: "中国国家博物馆" });
  await page.getByLabel("可选地点").selectOption({ label: "南锣鼓巷 · 东城区" });
  await page.getByLabel("修改说明").fill("继续把第一天的中国国家博物馆换成南锣鼓巷。");
  await page.getByRole("button", { name: "生成修改版" }).click();

  await expect(results).toContainText("v2 → v3", { timeout: 20_000 });
  await expect(results).toContainText("南锣鼓巷");
  await expect(results).toContainText("北海公园");
  await expect(
    results.getByTestId("itinerary-item").filter({ hasText: "中国国家博物馆" }),
  ).toHaveCount(0);
  await expect(results).toContainText("修改版等待确认");
});

test("shows when extra indoor places were found for the weather day", async ({ page }) => {
  await page.route(/\/api\/planning-tasks\/planning-task-[^/]+$/, async (route) => {
    const response = await route.fetch();
    const payload = (await response.json()) as {
      result?: {
        state?: {
          plan?: { days?: Array<{ items?: Array<{ candidate_id?: string | null }> }> };
          specialists?: {
            branches?: Array<{
              specialist?: string;
              explore_result?: {
                observations?: Array<{
                  candidate?: {
                    candidate_id?: string;
                    environment?: string;
                    categories?: string[];
                  };
                }>;
              } | null;
            }>;
          };
          weather_indoor_recovery?: {
            status?: string;
            observations?: Array<{ candidate?: unknown; query_id?: string }>;
          } | null;
        };
      };
    };
    const state = payload.result?.state;
    const scheduledIds = new Set(
      state?.plan?.days?.flatMap((day) =>
        day.items?.flatMap((item) => (item.candidate_id ? [item.candidate_id] : [])) ?? [],
      ) ?? [],
    );
    const explore = state?.specialists?.branches?.find(
      (branch) => branch.specialist === "explore",
    )?.explore_result;
    const recoveredCandidates = (explore?.observations ?? [])
      .filter(
        (item) =>
          item.candidate?.environment === "indoor" &&
          !item.candidate.categories?.includes("餐饮服务") &&
          !scheduledIds.has(item.candidate.candidate_id ?? ""),
      )
      .slice(0, 2);
    if (state && recoveredCandidates.length) {
      state.weather_indoor_recovery = {
        status: "recovered",
        observations: recoveredCandidates.map((item) => ({
          candidate: item.candidate,
          query_id: "weather-indoor-query-browser-test",
        })),
      };
    }
    await route.fulfill({ response, json: payload });
  });

  await page.goto("/");
  await understandAndStart(page);
  const results = page.getByTestId("planning-results");
  await expect(results).toBeVisible({ timeout: 20_000 });
  await expect(results.getByTestId("weather-recovery-summary").first()).toContainText(
    "已根据当天活动区域补充找到",
  );
  await expect(results.getByTestId("weather-replacement-proposal").first()).toContainText(
    "当天 2 个受天气影响的活动将一起调整",
  );
});

test("shows a useful empty state when there is no replacement", async ({
  page,
}) => {
  await page.route(/\/api\/planning-tasks\/planning-task-[^/]+$/, async (route) => {
    const response = await route.fetch();
    const payload = (await response.json()) as {
      result?: {
        state?: {
          plan?: { days?: Array<{ items?: Array<{ candidate_id?: string | null }> }> };
          specialists?: {
            branches?: Array<{
              specialist?: string;
              explore_result?: {
                observations?: Array<{ candidate?: { candidate_id?: string } }>;
              } | null;
            }>;
          };
          weather_indoor_recovery?: {
            status?: string;
            observations?: unknown[];
            provider_call_count?: number;
          } | null;
        };
      };
    };
    const state = payload.result?.state;
    const scheduledIds = new Set(
      state?.plan?.days?.flatMap((day) =>
        day.items?.flatMap((item) => (item.candidate_id ? [item.candidate_id] : [])) ?? [],
      ) ?? [],
    );
    const explore = state?.specialists?.branches?.find(
      (branch) => branch.specialist === "explore",
    )?.explore_result;
    if (explore?.observations) {
      explore.observations = explore.observations.filter((item) =>
        scheduledIds.has(item.candidate?.candidate_id ?? ""),
      );
    }
    if (state) {
      state.weather_indoor_recovery = {
        status: "insufficient",
        observations: [],
        provider_call_count: 2,
      };
    }
    await route.fulfill({ response, json: payload });
  });

  await page.goto("/");
  await understandAndStart(page);
  const results = page.getByTestId("planning-results");
  await expect(results).toBeVisible({ timeout: 20_000 });
  const weatherEmptyState = results.getByTestId("weather-replacement-insufficient").first();
  await expect(weatherEmptyState).toContainText(
    "目前还没有找到足够合适且不重复的室内地点，暂时无法重新安排全天",
  );
  await expect(weatherEmptyState).toContainText(
    "建议先保留当前行程，临近出发时再根据天气调整",
  );
  await expect(weatherEmptyState).not.toContainText("自动补充查找");
  await expect(weatherEmptyState).not.toContainText("未使用的室内候选");
  await expect(
    results.getByRole("button", { name: "采用全天室内方案" }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "局部修改" }).click();
  await page.getByLabel("修改方式").selectOption("replace_activity");

  await expect(
    page.getByText("暂时没有合适的替换地点，可以保留当前安排或重新生成行程。"),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "生成修改版" })).toBeDisabled();
});

test("requires confirmation and sends the confirmed raw intent instead of old defaults", async ({ page }) => {
  let planningPostCount = 0;
  type PlanningPayload = {
    intake_confirmation_id?: string;
    request?: {
      party?: { adults?: number; children?: number };
      travel_styles?: string[];
      constraints?: { items?: Array<{ kind?: string; value?: string }> };
    };
  };
  const captured: { payload?: PlanningPayload } = {};
  await page.route(/\/api\/planning-tasks$/, async (route) => {
    if (route.request().method() === "POST") {
      planningPostCount += 1;
      captured.payload = route.request().postDataJSON() as PlanningPayload;
    }
    await route.continue();
  });
  await page.goto("/");
  await page.getByLabel("旅行需求").fill("带一个孩子去北京看科技馆，不要寺庙，节奏轻松一些。");

  await page.getByTestId("submit-planning-task").click();

  const confirmation = page.getByTestId("request-intake-confirmation");
  await expect(confirmation).toBeVisible();
  await expect(confirmation.getByTestId("request-conflict-selection")).toBeVisible();
  await expect(confirmation).toContainText("采用需求中的差异值");
  await expect(confirmation).toContainText("保留表单中的差异值");
  await expect(confirmation).toContainText("成人：需求中为 “1”");
  await expect(confirmation).toContainText("儿童：需求中为 “1”");
  await expect(confirmation).toContainText("不想去 · 寺庙");
  await expect(confirmation).toContainText("感兴趣 · 科技馆");
  expect(planningPostCount).toBe(0);

  await page.getByTestId("confirm-request-intake").click();
  await expect(page.getByTestId("event-trace")).toContainText("已收到旅行需求");
  expect(planningPostCount).toBe(1);
  expect(captured.payload?.intake_confirmation_id).toMatch(/^request-confirmation-/);
  expect(captured.payload?.request?.party).toMatchObject({ adults: 1, children: 1 });
  expect(captured.payload?.request?.travel_styles).toEqual(["科技"]);
  expect(captured.payload?.request?.constraints?.items).toEqual(
    expect.arrayContaining([
      expect.objectContaining({ kind: "avoid", value: "寺庙" }),
      expect.objectContaining({ kind: "interest", value: "科技馆" }),
    ]),
  );
});

test("keeps text preferences when form values win a structured conflict", async ({ page }) => {
  type PlanningPayload = {
    request?: {
      pace?: string;
      travel_styles?: string[];
      constraints?: { items?: Array<{ kind?: string; value?: string }> };
    };
  };
  const captured: { payload?: PlanningPayload } = {};
  await page.route(/\/api\/planning-tasks$/, async (route) => {
    if (route.request().method() === "POST") {
      captured.payload = route.request().postDataJSON() as PlanningPayload;
    }
    await route.continue();
  });
  await page.goto("/");
  await page.getByLabel("行程节奏").selectOption("standard");
  await page.getByTestId("submit-planning-task").click();

  const confirmation = page.getByTestId("request-intake-confirmation");
  await expect(confirmation.getByTestId("request-conflict-selection")).toContainText(
    "行程节奏",
  );
  await confirmation.getByText("保留表单中的差异值", { exact: true }).click();
  await page.getByTestId("confirm-request-intake").click();
  await expect.poll(() => captured.payload).toBeDefined();

  expect(captured.payload?.request?.pace).toBe("standard");
  expect(captured.payload?.request?.travel_styles).toEqual(["历史文化"]);
  expect(captured.payload?.request?.constraints?.items).toEqual(
    expect.arrayContaining([
      expect.objectContaining({ kind: "interest", value: "历史文化" }),
    ]),
  );
});

test("keeps the planning flow usable on a mobile viewport", async ({ page }) => {
  const pageErrors = collectPageErrors(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");

  await expect(page.getByTestId("submit-planning-task")).toBeVisible();
  await understandAndStart(page);
  const results = page.getByTestId("planning-results");
  await expect(results).toBeVisible({ timeout: 20_000 });
  await expect(results).toBeInViewport();

  await page.screenshot({
    path: "test-results/eztrip-planning-workspace-mobile.png",
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth > document.documentElement.clientWidth,
    ),
  ).toBe(false);
  expect(pageErrors).toEqual([]);
});
