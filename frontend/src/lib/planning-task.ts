export type PlanningTaskStatus =
  | "queued"
  | "running"
  | "awaiting_input"
  | "succeeded"
  | "failed";

export type PlanningTaskEventKind =
  | "task_created"
  | "task_started"
  | "graph_node_completed"
  | "task_awaiting_input"
  | "task_review_submitted"
  | "task_succeeded"
  | "task_failed";

export interface SourceReference {
  provider: string;
  provider_id: string | null;
  data_mode: "live" | "fixture" | "user_input" | "estimate";
  retrieved_at: string;
  raw_response_sha256?: string | null;
}

export interface CandidatePoi {
  candidate_id: string;
  name: string;
  city: string;
  district: string | null;
  address: string | null;
  location: { latitude: number; longitude: number };
  categories: string[];
  environment: "indoor" | "outdoor" | "mixed" | "unknown";
  suggested_duration_minutes: number | null;
  tags: string[];
  source: SourceReference;
}

export interface CandidateStay {
  candidate_id: string;
  name: string;
  city: string;
  district: string | null;
  address: string | null;
  location: { latitude: number; longitude: number };
  area_name: string;
  tags: string[];
  availability_status: "unknown";
  booking_supported: false;
  source: SourceReference;
}

export interface RouteLeg {
  mode: "walking" | "transit" | "driving" | "cycling";
  distance_meters: number;
  duration_minutes: number;
  source: SourceReference;
}

export interface ItineraryItem {
  item_id: string;
  kind: "attraction" | "meal" | "stay" | "transit" | "free_time";
  title: string;
  start_at: string;
  end_at: string;
  candidate_id: string | null;
  source: SourceReference | null;
  route_from_previous: RouteLeg | null;
  cost_item_ids: string[];
  notes: string[];
}

export interface DayPlan {
  date: string;
  items: ItineraryItem[];
  weather_risk_ids: string[];
}

export interface CostItem {
  cost_item_id: string;
  category: BudgetCategory;
  description: string;
  quantity: string | number;
  unit_price: {
    minimum: string | number;
    maximum: string | number;
    currency: "CNY";
  };
  source: SourceReference;
  is_estimate: boolean;
}

export type BudgetCategory =
  | "lodging"
  | "transport"
  | "food"
  | "admission"
  | "activity"
  | "other";

export interface WeatherRisk {
  risk_id: string;
  city: string;
  starts_at: string;
  ends_at: string;
  risk_type: string;
  severity: string;
  threshold_description: string;
  advisory: string;
  source: SourceReference;
}

export interface TripPlan {
  plan_id: string;
  request_id: string;
  status: "draft" | "pending_confirmation" | "final" | "conflicted";
  destination_city: string;
  start_date: string;
  end_date: string;
  days: DayPlan[];
  cost_items: CostItem[];
  weather_risks: WeatherRisk[];
}

export interface BudgetValidationSummary {
  status:
    | "not_requested"
    | "within_limit"
    | "possible_overrun"
    | "exceeded"
    | "incomplete";
  currency: "CNY";
  total_limit: string | number | null;
  included_categories: BudgetCategory[];
  missing_categories: BudgetCategory[];
  considered_cost_item_ids: string[];
  excluded_cost_item_ids: string[];
  total_minimum: string | number;
  total_maximum: string | number;
  minimum_gap: string | number;
  maximum_gap: string | number;
}

export interface ValidationIssue {
  issue_id: string;
  rule_code: string;
  severity: "warning" | "error";
  message: string;
  responsible_node: string;
  repairable: boolean;
  repair_action: string;
  requires_user_confirmation: boolean;
}

export interface PlanValidation {
  validator_version: string;
  status: "passed" | "warning" | "conflicted";
  can_finalize: boolean;
  budget: BudgetValidationSummary;
  issues: ValidationIssue[];
  passed_rule_codes: string[];
}

export interface HumanReviewRequest {
  review_id: string;
  kind: "plan_approval" | "conflict_resolution";
  prompt: string;
  allowed_actions: string[];
  validation_status: "passed" | "warning" | "conflicted";
  can_finalize: boolean;
  issue_rule_codes: string[];
}

export interface VerticalSliceResult {
  workflow_version: string;
  data_mode: "live" | "fixture";
  outcome: "ready" | "conflicted";
  upstream: {
    status: string;
    candidates: CandidatePoi[];
    provider_failures: unknown[];
  };
  plan: TripPlan;
  validation: PlanValidation;
}

export interface ProductSpecialistBranch {
  specialist: "explore" | "stay" | "weather";
  status: "succeeded" | "skipped" | "failed";
  explore_result: {
    recommendations: { candidate: CandidatePoi }[];
    query_model: string;
    selection_model: string;
  } | null;
  stay_result: {
    recommendations: { candidate: CandidateStay }[];
    query_model: string;
    selection_model: string;
  } | null;
  weather_risks: WeatherRisk[];
}

export interface ProductPlanningMaterials {
  status: "ready" | "partial" | "blocked";
  issues: string[];
  shortlist: {
    poi_candidates: CandidatePoi[];
    primary_stay: CandidateStay | null;
  };
  route_matrix: {
    status: string;
    expected_edge_count: number;
    succeeded_edge_count: number;
  };
  budget_allocation: {
    status: string;
    total_limit: string | number | null;
    hard_limit: boolean | null;
  };
}

export type ProductRepairAction =
  | "rerun_constraint"
  | "rerun_explore"
  | "rerun_stay"
  | "rerun_route"
  | "replan_day"
  | "recalculate_budget"
  | "ask_user"
  | "none";

export type ProductResponsibleNode =
  | "constraint"
  | "explore"
  | "stay"
  | "weather"
  | "route"
  | "plan"
  | "budget"
  | "validator";

export interface ProductRepairAttempt {
  attempt_index: number;
  action_attempt: number;
  repair_action: ProductRepairAction;
  responsible_node: ProductResponsibleNode;
  trigger_issue_codes: string[];
  execution_status: "succeeded" | "failed";
  executed_nodes: ProductResponsibleNode[];
  reused_nodes: ProductResponsibleNode[];
  before_error_codes: string[];
  after_error_codes: string[];
  resolved_issue_codes: string[];
  introduced_issue_codes: string[];
  plan_diff: {
    changed_dates: string[];
    added_candidate_ids: string[];
    removed_candidate_ids: string[];
    total_cost_minimum_before: string | number;
    total_cost_minimum_after: string | number;
    total_cost_maximum_before: string | number;
    total_cost_maximum_after: string | number;
  };
  model_call_count: number;
  provider_call_count: number;
  error_code: string | null;
}

export interface ProductRepairResult {
  schema_version: "1.0";
  router_version: "repair-router-v1";
  outcome: "already_finalizable" | "repaired" | "waiting_for_user" | "unresolved";
  stop_reason:
    | "finalizable"
    | "user_confirmation_required"
    | "unrepairable_issue"
    | "retry_limit_reached";
  attempts: ProductRepairAttempt[];
  retry_counts: { repair_action: ProductRepairAction; attempt_count: number }[];
  pending_error_codes: string[];
  requires_user_confirmation: boolean;
  total_model_call_count: number;
  total_provider_call_count: number;
}

export interface PlanRevisionRequest {
  schema_version: "1.0";
  revision_id: string;
  base_version_id: string;
  base_plan_id: string;
  target_date: string;
  operation: "shift_day_later";
  shift_minutes: number;
  target_item_ids: string[];
  protected_item_ids: string[];
  confirmed: true;
}

export interface PlanRevisionResult {
  executor_version: "deterministic-local-revision-v1";
  request: PlanRevisionRequest;
  revised_plan: TripPlan;
  validation: PlanValidation;
  diff: {
    from_plan_id: string;
    to_plan_id: string;
    changed_dates: string[];
    rescheduled_item_ids: string[];
    added_item_ids: string[];
    removed_item_ids: string[];
  };
  reused_provider_results: true;
  reused_planner_result: true;
  model_call_count: 0;
  provider_call_count: 0;
}

export interface PlanningTaskSnapshot {
  workflow_version: string;
  task_id: string;
  request_id: string;
  data_mode: "live" | "fixture";
  status: PlanningTaskStatus;
  created_at: string;
  updated_at: string;
  event_count: number;
  result: {
    checkpoint_id: string;
    next_nodes: string[];
    state: {
      workflow_version: "stateful-planning-checkpoint-v1" | "product-planning-graph-v2";
      status: string;
      vertical_slice?: VerticalSliceResult | null;
      specialists?: {
        status: string;
        total_model_call_count: number;
        total_provider_call_count: number;
        branches: ProductSpecialistBranch[];
      } | null;
      materials?: ProductPlanningMaterials | null;
      plan_agent?: {
        status: "planned" | "skipped";
        agent_version: string;
        prompt_version: string;
        model: string | null;
        model_call_count: number;
      } | null;
      plan?: TripPlan | null;
      validation?: PlanValidation | null;
      repair?: ProductRepairResult | null;
      review_request: HumanReviewRequest | null;
      revision_result: PlanRevisionResult | null;
    };
  } | null;
  failure: {
    error_code: string;
    category: string;
    retryable: boolean;
    user_message: string;
  } | null;
  plan_versions: PlanVersion[];
  review_outcome: PlanningTaskReviewOutcome | null;
}

export interface PlanningTaskAccepted {
  task_id: string;
  request_id: string;
  status: "queued";
  task_url: string;
  events_url: string;
}

export interface PlanningTaskEvent {
  event_id: string;
  sequence: number;
  task_id: string;
  kind: PlanningTaskEventKind;
  task_status: PlanningTaskStatus;
  occurred_at: string;
  message: string;
  node: string | null;
  state_status: string | null;
  review_id: string | null;
  review_action: HumanReviewAction | null;
  error_code: string | null;
}

export type HumanReviewAction =
  | "approve_draft"
  | "acknowledge_conflict"
  | "request_revision"
  | "cancel";

export interface PlanVersion {
  version_id: string;
  plan: TripPlan;
  version_number: number;
  based_on_version_id: string | null;
  created_at: string;
  input_constraint_sha256: string;
  tool_snapshot_ids: string[];
  model_versions: Record<string, string>;
  prompt_versions: Record<string, string>;
  change_summary: string[];
  changed_dates: string[];
}

export interface PlanningTaskPlanDiff {
  from_version_id: string;
  to_version_id: string;
  plan_changed: boolean;
  changed_dates: string[];
  added_item_ids: string[];
  removed_item_ids: string[];
  rescheduled_item_ids: string[];
  summary: string[];
}

export interface PlanningTaskReviewOutcome {
  decision_id: string;
  review_id: string;
  action: HumanReviewAction;
  reviewer_id: string;
  comment: string | null;
  decided_at: string;
  resulting_state_status: string;
  plan_diff: PlanningTaskPlanDiff;
}

export interface PlanningTaskReviewDecisionAccepted {
  decision_id: string;
  task_id: string;
  review_id: string;
  action: HumanReviewAction;
  status: "running";
  idempotent_replay: boolean;
  task_url: string;
  events_url: string;
}

export interface PlanRevisionSelection {
  targetDate: string;
  shiftMinutes: number;
}

export interface PlannerFormValues {
  rawText: string;
  originCity: string;
  startDate: string;
  adults: number;
  budgetLimit: string;
}

const fallbackApiBaseUrl = "http://localhost:8000";

export const apiBaseUrl = (
  process.env.NEXT_PUBLIC_API_BASE_URL ?? fallbackApiBaseUrl
).replace(/\/$/, "");

function addCalendarDays(date: string, days: number): string {
  const [year, month, day] = date.split("-").map(Number);
  const result = new Date(Date.UTC(year, month - 1, day + days));
  return result.toISOString().slice(0, 10);
}

export function buildPlanningTaskRequest(values: PlannerFormValues) {
  const requestId = `web-request-${crypto.randomUUID().replaceAll("-", "")}`;
  const budget = {
    total_limit: values.budgetLimit.trim(),
    currency: "CNY" as const,
    scope: "party_total" as const,
    period: "whole_trip" as const,
    included_categories: ["transport", "food", "admission", "activity"],
    hard_limit: false,
  };

  return {
    schema_version: "1.0",
    request: {
      schema_version: "1.0",
      request_id: requestId,
      locale: "zh-CN",
      raw_text: values.rawText.trim(),
      origin_city: values.originCity.trim() || null,
      destination_city: "北京市",
      start_date: values.startDate,
      end_date: addCalendarDays(values.startDate, 1),
      party: {
        adults: values.adults,
        children: 0,
        seniors: 0,
        rooms: 1,
      },
      budget,
      travel_styles: ["历史文化", "轻步行"],
      constraints: {
        items: [
          {
            constraint_id: "web-must-visit-palace-museum",
            kind: "must_visit",
            value: "故宫博物院",
            strength: "hard",
            priority: 5,
            source: "user_confirmed",
            applies_to_dates: [],
            confirmed: true,
          },
          {
            constraint_id: "web-must-visit-temple-of-heaven",
            kind: "must_visit",
            value: "天坛公园",
            strength: "hard",
            priority: 5,
            source: "user_confirmed",
            applies_to_dates: [],
            confirmed: true,
          },
        ],
      },
    },
    cost_items: [],
    data_mode: "fixture" as const,
  };
}

async function parseJsonResponse<T>(response: Response): Promise<T> {
  if (response.ok) {
    return (await response.json()) as T;
  }

  let detail = `请求失败（HTTP ${response.status}）`;
  try {
    const body = (await response.json()) as { detail?: string | unknown[] };
    if (typeof body.detail === "string") {
      detail = body.detail;
    } else if (body.detail) {
      detail = JSON.stringify(body.detail);
    }
  } catch {
    // Keep the HTTP fallback when a proxy returns non-JSON content.
  }
  throw new Error(detail);
}

export async function createPlanningTask(
  values: PlannerFormValues,
): Promise<PlanningTaskAccepted> {
  const response = await fetch(`${apiBaseUrl}/api/planning-tasks`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(buildPlanningTaskRequest(values)),
  });
  return parseJsonResponse<PlanningTaskAccepted>(response);
}

export async function getPlanningTask(
  taskId: string,
): Promise<PlanningTaskSnapshot> {
  const response = await fetch(`${apiBaseUrl}/api/planning-tasks/${taskId}`, {
    cache: "no-store",
  });
  return parseJsonResponse<PlanningTaskSnapshot>(response);
}

export async function submitPlanningTaskReview(
  taskId: string,
  input: {
    decisionId: string;
    reviewId: string;
    action: HumanReviewAction;
    reviewerId: string;
    comment?: string;
    revisionRequest?: PlanRevisionRequest;
  },
): Promise<PlanningTaskReviewDecisionAccepted> {
  const response = await fetch(
    `${apiBaseUrl}/api/planning-tasks/${taskId}/review-decisions`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        schema_version: "1.0",
        decision_id: input.decisionId,
        review_id: input.reviewId,
        action: input.action,
        reviewer_id: input.reviewerId,
        comment: input.comment?.trim() || null,
        revision_request: input.revisionRequest ?? null,
      }),
    },
  );
  return parseJsonResponse<PlanningTaskReviewDecisionAccepted>(response);
}

export function buildPlanRevisionRequest(
  snapshot: PlanningTaskSnapshot,
  selection: PlanRevisionSelection,
): PlanRevisionRequest {
  const version = snapshot.plan_versions.at(-1);
  if (!version) {
    throw new Error("当前任务没有可修改的计划版本。");
  }
  const targetDay = version.plan.days.find((day) => day.date === selection.targetDate);
  if (!targetDay) {
    throw new Error("修改日期不属于当前计划。");
  }
  return {
    schema_version: "1.0",
    revision_id: `revision-${crypto.randomUUID().replaceAll("-", "")}`,
    base_version_id: version.version_id,
    base_plan_id: version.plan.plan_id,
    target_date: targetDay.date,
    operation: "shift_day_later",
    shift_minutes: selection.shiftMinutes,
    target_item_ids: targetDay.items.map((item) => item.item_id),
    protected_item_ids: version.plan.days
      .filter((day) => day.date !== targetDay.date)
      .flatMap((day) => day.items.map((item) => item.item_id)),
    confirmed: true,
  };
}

export function planningEventsUrl(taskId: string, afterSequence = 0): string {
  const base = `${apiBaseUrl}/api/planning-tasks/${taskId}/events`;
  return afterSequence > 0 ? `${base}?after=${afterSequence}` : base;
}
