import { expect, test } from "@playwright/test";

test("submits a real fixture planning task and renders its evidence", async ({ page }) => {
  await page.goto("/");

  await expect(page.getByRole("heading", { name: "一份旅行计划，一条完整证据链。" })).toBeVisible();
  await page.getByTestId("submit-planning-task").click();

  const trace = page.getByTestId("event-trace");
  await expect(trace).toContainText("任务已入队");
  await expect(trace).toContainText("执行有界局部修复");
  await expect(trace).toContainText("等待人工审核");

  const results = page.getByTestId("planning-results");
  await expect(results).toBeVisible({ timeout: 20_000 });
  await expect(results).toContainText("北京 · 2 日规划草案");
  await expect(results).toContainText("故宫博物院");
  await expect(results).toContainText("天坛公园");
  await expect(results).toContainText("中国国家博物馆");
  await expect(results).toContainText("景山公园");
  await expect(results.getByTestId("itinerary-item")).toHaveCount(4);
  await expect(results).toContainText("从住宿锚点出发前往首站");
  await expect(results).toContainText("附近用餐建议");
  await expect(results).toContainText("推荐 · 不占活动名额");
  await expect(results).toContainText("Fixture 数据");
  await expect(results).toContainText("Product Graph V2");
  await expect(results).toContainText("explore");
  await expect(results).toContainText("stay");
  await expect(results).toContainText("weather");
  await expect(results).toContainText("首日优先室内或混合型活动");
  await expect(results.getByTestId("stay-recommendation")).toContainText("住宿推荐");
  await expect(results.getByTestId("stay-recommendation")).toContainText("Stay Agent 推荐理由");
  await expect(results.getByTestId("stay-recommendation")).toContainText("房价与房态待验证");
  await expect(results.getByTestId("activity-description")).toHaveCount(4);
  await expect(results.getByTestId("activity-reason")).toHaveCount(4);
  await expect(results).toContainText("Fixture 模式不调用真实地图服务");
  await expect(results.getByTestId("budget-estimate")).toContainText("预算估算");
  const repair = page.getByTestId("product-repair-summary");
  await expect(repair).toContainText("有界自动修复");
  await expect(repair).toContainText("1 次修复 · repaired");
  await expect(repair).toContainText("replan_day");
  await expect(repair).toContainText("实际执行：Plan");
  await expect(repair).toContainText("营业时间冲突已修复");
  await expect(repair).toContainText("0 次模型调用 · 0 次工具调用");

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

test("renders three main activities per day in standard pace without counting meals", async ({ page }) => {
  await page.goto("/");

  await page.getByLabel("行程节奏").selectOption("standard");
  await page.getByTestId("submit-planning-task").click();

  const results = page.getByTestId("planning-results");
  await expect(results).toBeVisible({ timeout: 20_000 });
  await expect(results.getByTestId("itinerary-item")).toHaveCount(6);
  await expect(results.getByTestId("meal-recommendations")).toHaveCount(2);
  await expect(results).toContainText("推荐 · 不占活动名额");
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
  await page.getByTestId("submit-planning-task").click();

  const results = page.getByTestId("planning-results");
  await expect(results).toBeVisible({ timeout: 20_000 });
  await expect(
    results.getByText("当前没有 3 公里内且来源可追溯的餐饮候选，不随机填充全城餐厅。"),
  ).toHaveCount(2);
});

test("does not present missing cost facts as a zero-cost trip", async ({ page }) => {
  await page.goto("/");

  await page.getByLabel("整趟预算目标").fill("2000");
  await page.getByTestId("submit-planning-task").click();

  const results = page.getByTestId("planning-results");
  await expect(results).toBeVisible({ timeout: 20_000 });
  const estimate = results.getByTestId("budget-estimate");
  await expect(estimate).toContainText("¥2,000");
  await expect(estimate).toContainText("交通");
  await expect(estimate).toContainText("餐饮");
  await expect(estimate).toContainText("规划估算，不代表实时票价");
  await expect(results).toContainText("方案可用 · 有估算提醒");
  const approve = page.getByRole("button", { name: "批准草案" });
  await expect(approve).toBeEnabled();
  await approve.click();
  await expect(results).toContainText("审核已完成");
  await expect(results).toContainText("已批准草案");
});

test("resolves Shanghai and plans a three-day fixture trip", async ({ page }) => {
  await page.goto("/");

  await page.getByLabel("目的城市").fill("上海");
  await page.getByLabel("行程天数").selectOption("3");
  await page.getByTestId("submit-planning-task").click();

  await expect(page.getByTestId("destination-resolution")).toContainText("adcode 310000");
  const results = page.getByTestId("planning-results");
  await expect(results).toBeVisible({ timeout: 20_000 });
  await expect(results).toContainText("上海 · 3 日规划草案");
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
  await expect(page.getByTestId("event-trace")).toContainText("提交后，这里会显示真实 SSE 事件");
});

test("does not pretend fixture mode supports an arbitrary city", async ({ page }) => {
  await page.goto("/");

  await page.getByLabel("目的城市").fill("泉州");
  await page.getByTestId("submit-planning-task").click();

  await expect(page.getByTestId("planning-error")).toContainText(
    "Fixture 模式仅覆盖北京、上海和成都",
  );
  await expect(page.getByTestId("planning-results")).not.toBeVisible();
});

test("treats live Provider selection as the explicit per-request opt-in", async ({ page }) => {
  await page.goto("/");

  await page.getByLabel("旅行数据模式").selectOption("live");

  await expect(page.getByText("当前选择会直接启用实时调用")).toBeVisible();
  await expect(page.getByText("Key 仍只保存在服务端", { exact: false })).toBeVisible();
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
  await page.getByTestId("submit-planning-task").click();

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
  await page.getByTestId("submit-planning-task").click();

  const results = page.getByTestId("planning-results");
  await expect(results).toContainText("关键事实待确认");
  await expect(results.getByTestId("review-issue-summary")).toContainText("一段到达路线未取得");
  await expect(page.getByRole("button", { name: "保留待验证草案" })).toBeVisible();
  await expect(results).not.toContainText("存在硬冲突");
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
  await expect(results).toContainText("12:00 — 14:00");

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
