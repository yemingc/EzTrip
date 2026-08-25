import { expect, test, type Page } from "@playwright/test";

async function understandAndStart(
  page: Page,
  selection: "proposal" | "form" = "proposal",
) {
  await page.getByTestId("submit-planning-task").click();
  const confirmation = page.getByTestId("request-intake-confirmation");
  await expect(confirmation).toBeVisible();
  if (selection === "form") {
    await confirmation.getByText("使用当前表单", { exact: true }).click();
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

test("generates a complete sample itinerary with user-facing copy", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "把想去的地方，变成一份好用的行程。" })).toBeVisible();
  await understandAndStart(page);

  const trace = page.getByTestId("event-trace");
  await expect(trace).toContainText("已收到旅行需求");
  await expect(trace).toContainText("调整不合理安排");
  await expect(trace).toContainText("行程等待确认");

  const results = page.getByTestId("planning-results");
  await expect(results).toBeVisible({ timeout: 20_000 });
  await expect(results).toContainText("北京 · 2 天行程");
  await expect(results).toContainText("故宫博物院");
  await expect(results).toContainText("天坛公园");
  await expect(results).toContainText("中国国家博物馆");
  await expect(results).toContainText("景山公园");
  await expect(results.getByTestId("itinerary-item")).toHaveCount(4);
  await expect(results).toContainText("从住宿地点出发前往首站");
  await expect(results).toContainText("附近用餐建议");
  await expect(results).toContainText("首日优先室内或混合型活动");
  await expect(results.getByTestId("stay-recommendation")).toContainText("住宿推荐");
  await expect(results.getByTestId("stay-recommendation")).toContainText("推荐理由");
  await expect(results.getByTestId("stay-recommendation")).toContainText("价格和空房情况以预订平台为准");
  await expect(results.getByTestId("activity-description")).toHaveCount(4);
  await expect(results.getByTestId("activity-reason")).toHaveCount(4);
  await expect(results).toContainText("示例体验暂不显示地图");
  await expect(results.getByTestId("budget-estimate")).toContainText("预算估算");
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
  await page.goto("/");

  await page.getByLabel("整趟预算目标").fill("2000");
  await understandAndStart(page);

  const results = page.getByTestId("planning-results");
  await expect(results).toBeVisible({ timeout: 20_000 });
  const estimate = results.getByTestId("budget-estimate");
  await expect(estimate).toContainText("¥2,000");
  await expect(estimate).toContainText("交通");
  await expect(estimate).toContainText("餐饮");
  await expect(estimate).toContainText("实际费用以出行时为准");
  await expect(results).toContainText("行程可用 · 请留意提示");
  const approve = page.getByRole("button", { name: "确认行程" });
  await expect(approve).toBeEnabled();
  await approve.click();
  await expect(results).toContainText("本次选择已保存");
  await expect(results).toContainText("行程已确认");
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
  await expect(results.getByTestId("review-issue-summary")).toContainText("一段到达路线未取得");
  await expect(page.getByRole("button", { name: "保留当前方案" })).toBeVisible();
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

test("replaces one activity from available places and preserves the other day", async ({
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
  await expect(results).not.toContainText("景山公园");
  await expect(results).toContainText("计划已修改 · 1 个受影响日期");
  await expect(
    results.getByTestId("itinerary-item").filter({ hasText: "中国国家博物馆" }),
  ).toHaveText(protectedDayBefore ?? "");
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
    await route.fulfill({ response, json: payload });
  });

  await page.goto("/");
  await understandAndStart(page);
  await expect(page.getByTestId("planning-results")).toBeVisible({ timeout: 20_000 });
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
  await expect(confirmation).toContainText("带一个孩子");
  await expect(confirmation).toContainText("不要寺庙");
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

test("keeps the planning flow usable on a mobile viewport", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");

  await expect(page.getByTestId("submit-planning-task")).toBeVisible();
  await understandAndStart(page);
  await expect(page.getByTestId("planning-results")).toBeVisible({ timeout: 20_000 });

  await page.screenshot({
    path: "test-results/eztrip-planning-workspace-mobile.png",
    fullPage: true,
  });
});
