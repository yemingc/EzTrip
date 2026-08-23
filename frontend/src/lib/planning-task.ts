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
      status: string;
      vertical_slice: VerticalSliceResult;
      review_request: HumanReviewRequest | null;
    };
  } | null;
  failure: {
    error_code: string;
    category: string;
    retryable: boolean;
    user_message: string;
  } | null;
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
  error_code: string | null;
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
  const budget = values.budgetLimit.trim()
    ? {
        total_limit: values.budgetLimit.trim(),
        currency: "CNY" as const,
        scope: "party_total" as const,
        period: "whole_trip" as const,
        included_categories: ["transport", "food", "admission", "activity"],
        hard_limit: true,
      }
    : null;

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

export function planningEventsUrl(taskId: string): string {
  return `${apiBaseUrl}/api/planning-tasks/${taskId}/events`;
}
