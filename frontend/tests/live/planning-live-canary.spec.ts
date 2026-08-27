import { expect, test } from "@playwright/test";

test("runs one explicit non-fixture browser planning loop to a restorable terminal outcome", async ({
  page,
}) => {
  test.skip(
    process.env.EZTRIP_RUN_LIVE_BROWSER_CANARY !== "1",
    "Set EZTRIP_RUN_LIVE_BROWSER_CANARY=1 to authorize real Provider/model calls.",
  );
  const startedAt = Date.now();
  await page.goto("/");

  const startDateInput = page.getByLabel("出发日期");
  const startDate = await startDateInput.inputValue();
  const [year, month, day] = startDate.split("-").map(Number);
  await page.getByLabel("旅行需求").fill(
    `我1名成人带2位老人从上海出发，${year}年${month}月${day}日去泉州玩2天，` +
      "总预算3000元，喜欢古建筑和闽南美食，节奏轻松，尽量少走路。",
  );
  await page.getByLabel("目的城市").fill("泉州");
  await page.getByLabel("出发城市").fill("上海");
  await page.getByLabel("行程天数").selectOption("2");
  await page.getByLabel("成人数量").selectOption("1");
  await page.getByLabel("老人数量").selectOption("2");
  await page.getByLabel("整趟预算目标").fill("3000");
  await page.getByLabel("行程节奏").selectOption("relaxed");
  await page.getByLabel("规划方式").selectOption("live");
  await expect(page.getByText("当前选择会直接启用实时调用")).toBeVisible();

  await page.getByTestId("submit-planning-task").click();
  const confirmation = page.getByTestId("request-intake-confirmation");
  await expect(confirmation).toBeVisible({ timeout: 60_000 });
  await expect(confirmation).toContainText("已合并旅行需求与当前填写，未发现冲突");
  await expect(confirmation).toContainText("泉州");
  await expect(page.getByTestId("destination-resolution")).toContainText("adcode");
  const confirm = page.getByTestId("confirm-request-intake");
  await expect(confirm).toBeEnabled();
  const planningStartedAt = Date.now();
  await confirm.click();

  await expect(page).toHaveURL(/\?task_id=planning-task-[a-f0-9]{32}$/);
  const taskUrl = page.url();
  const taskId = new URL(taskUrl).searchParams.get("task_id");
  expect(taskId).toBeTruthy();
  const results = page.getByTestId("planning-results");
  const failure = page.getByTestId("planning-error");
  await expect(results.or(failure)).toBeVisible({ timeout: 125_000 });
  const planningElapsedSeconds = Math.round((Date.now() - planningStartedAt) / 1000);
  expect(planningElapsedSeconds).toBeLessThanOrEqual(120);

  let terminalOutcome: "saved_plan_version" | "honest_degradation";
  if (await results.isVisible()) {
    terminalOutcome = "saved_plan_version";
    await expect(results).toContainText("泉州");
  await expect(results).toContainText("高德地图");
  await expect(page.getByTestId("event-trace")).toContainText("行程等待确认");

  const acknowledge = page.getByRole("button", { name: /保留当前方案|已了解，保留行程/ });
  const approve = page.getByRole("button", { name: "确认行程" });
    if (await acknowledge.isVisible()) {
      await acknowledge.click();
    } else {
      await expect(approve).toBeVisible();
      await approve.click();
    }
  await expect(results).toContainText("本次选择已保存");
  } else {
    terminalOutcome = "honest_degradation";
    await expect(failure).toContainText("任务没有完成");
    await expect(page.getByTestId("event-trace")).toContainText("任务失败");
  }

  await page.reload();
  await expect(page).toHaveURL(taskUrl);
  if (terminalOutcome === "saved_plan_version") {
  await expect(results).toContainText("本次选择已保存", { timeout: 30_000 });
  } else {
    await expect(failure).toContainText("任务没有完成", { timeout: 30_000 });
  }

  const snapshotResponse = await page.request.get(
    `http://localhost:8000/api/planning-tasks/${taskId}`,
  );
  expect(snapshotResponse.ok()).toBe(true);
  const snapshot = (await snapshotResponse.json()) as {
    status: string;
    data_mode: string;
    event_count: number;
    plan_versions: Array<{ version_number: number; plan: { days: unknown[] } }>;
    review_outcome?: { action?: string } | null;
    failure?: {
      error_code?: string;
      category?: string;
      retryable?: boolean;
      user_message?: string;
    } | null;
    result?: {
      state?: {
        request?: { destination_city?: string; destination_adcode?: string | null };
        validation?: {
          status?: string;
          can_finalize?: boolean;
          issues?: Array<{ rule_code?: string }>;
        };
        specialists?: {
          branches?: Array<{ specialist?: string; status?: string }>;
        };
        materials?: {
          route_matrix?: {
            status?: string;
            expected_edge_count?: number;
            succeeded_edge_count?: number;
            failed_edge_count?: number;
            provider_call_count?: number;
          };
        };
      };
    };
  };
  const state = snapshot.result?.state;
  const routeMatrix = state?.materials?.route_matrix;
  expect(snapshot.data_mode).toBe("live");
  if (terminalOutcome === "saved_plan_version") {
    expect(snapshot.status).toBe("succeeded");
    expect(snapshot.plan_versions.length).toBeGreaterThan(0);
  } else {
    expect(snapshot.status).toBe("failed");
    expect(snapshot.plan_versions).toHaveLength(0);
    expect(snapshot.failure?.error_code).toBeTruthy();
    expect(snapshot.failure?.category).toBeTruthy();
    expect(snapshot.failure?.user_message).toBeTruthy();
    await expect(failure).toContainText(snapshot.failure?.user_message ?? "");
  }
  const summary = {
    observed_at: new Date().toISOString(),
    elapsed_seconds: Math.round((Date.now() - startedAt) / 1000),
    planning_elapsed_seconds: planningElapsedSeconds,
    terminal_outcome: terminalOutcome,
    task_id: taskId,
    task_status: snapshot.status,
    data_mode: snapshot.data_mode,
    destination_city: state?.request?.destination_city,
    destination_adcode: state?.request?.destination_adcode,
    event_count: snapshot.event_count,
    plan_version_count: snapshot.plan_versions.length,
    day_count: snapshot.plan_versions.at(-1)?.plan.days.length,
    validation_status: state?.validation?.status,
    can_finalize: state?.validation?.can_finalize,
    issue_rule_codes: state?.validation?.issues?.map((item) => item.rule_code),
    specialist_statuses: state?.specialists?.branches?.map((item) => ({
      specialist: item.specialist,
      status: item.status,
    })),
    route_edges: routeMatrix
      ? {
          status: routeMatrix.status,
          expected: routeMatrix.expected_edge_count,
          succeeded: routeMatrix.succeeded_edge_count,
          failed: routeMatrix.failed_edge_count,
          provider_calls: routeMatrix.provider_call_count,
        }
      : null,
    review_action: snapshot.review_outcome?.action,
    failure: snapshot.failure
      ? {
          error_code: snapshot.failure.error_code,
          category: snapshot.failure.category,
          retryable: snapshot.failure.retryable,
          user_message: snapshot.failure.user_message,
        }
      : null,
    restored_after_reload: true,
  };
  process.stdout.write(`\nEZTRIP_LIVE_CANARY_SUMMARY=${JSON.stringify(summary)}\n`);
  await page.screenshot({
    path: test.info().outputPath("eztrip-live-browser-canary.png"),
    fullPage: true,
  });
});
