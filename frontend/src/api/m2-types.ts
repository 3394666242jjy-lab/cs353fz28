/**
 * M2 前端契约类型（按 dev/04-API接口文档 定义）。
 * 后端 B/D 的 M2 接口尚未合并，这里先作为前端开发契约；
 * 联调时如字段变化，只需改本文件与 client.ts。
 */

export interface DocumentTextUpdate {
  extracted_text: string;
  expected_version: number;
}

export interface DocumentUpdateAccepted {
  document_id: string;
  version: number;
  execution_id: string;
}

export interface DocumentDeleteAccepted {
  status: 'deleted';
  purge_status: 'pending' | 'running' | 'done' | 'failed';
}

export interface PlanTask {
  id: string;
  title: string;
  requirement_ids: string[];
  scheduled_date: string; // YYYY-MM-DD
  estimated_minutes: number;
  source_ids: string[];
  status?: 'pending' | 'done' | 'skipped';
}

export interface PlanResult {
  id: string;
  analysis_id: string;
  status: 'draft' | 'confirmed';
  deadline: string;
  timezone: string;
  daily_minutes: number;
  tasks: PlanTask[];
  draft_tasks?: PlanTask[];
  warnings?: string[];
  created_at?: string;
  confirmed_at?: string | null;
}

export interface PlanCreateRequest {
  analysis_id: string;
  deadline: string;
  timezone: string;
  daily_minutes: number;
}

export interface PlanConfirmResult {
  plan: PlanResult;
  tasks: PlanTask[];
  execution_id: string;
}

export interface ExecutionEventData {
  execution_id: string;
  status: 'queued' | 'running' | 'waiting_for_input' | 'succeeded' | 'failed';
  stage?: string;
  result_type?: 'document' | 'analysis' | 'plan';
  result_id?: string;
  error?: { code: string; message: string };
  pending_input?: {
    kind: 'clarification' | 'plan_confirmation';
    interrupt_id: string;
    checkpoint_id: string;
    question?: string;
    plan_id?: string;
  };
}

export interface ExecutionEvent {
  id: string;
  type: 'stage' | 'waiting_for_input' | 'completed' | 'failed';
  data: ExecutionEventData;
}
