/**
 * M2 前端契约类型（按 dev/04-API接口文档 与 B/D M2 分支实现对齐）。
 * 后端接口尚未合并到 main 时，这里作为前端开发契约；
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
  document_id: string;
  status: 'deleted';
  purge_status: 'pending' | 'done' | 'failed';
}

export interface DocumentExtractionPage {
  page: number;
  text: string;
}

/** GET /documents/{id} 附带的提取结果：文本版或 PDF 分页。 */
export interface DocumentExtraction {
  kind: 'text' | 'pdf';
  text?: string | null;
  pages?: DocumentExtractionPage[] | null;
}

/** POST /executions/{id}/resume 的用户回答。 */
export interface ResumeAnswer {
  text: string;
  continue_without_evidence: boolean;
  document_ids: string[];
}

export interface ResumeRequest {
  interrupt_id: string;
  checkpoint_id: string;
  answer: ResumeAnswer;
}

export interface EvidenceRefLike {
  document_id?: string | null;
  version?: number | null;
  chunk_id?: string | null;
}

/** 计划草稿任务：确认前不存在正式任务 ID。 */
export interface PlanDraftTask {
  title: string;
  requirement_ids: string[];
  scheduled_date: string; // YYYY-MM-DD
  estimated_minutes: number;
  source_refs: EvidenceRefLike[];
}

/** 确认后的正式任务：稳定 ID，幂等写入。 */
export interface PlanTask {
  id: string;
  plan_id: string;
  title: string;
  requirement_ids: string[];
  scheduled_date: string;
  estimated_minutes: number;
  status: 'todo' | 'in_progress' | 'done';
  source_refs: EvidenceRefLike[];
}

export interface PlanResult {
  id: string;
  analysis_id: string;
  execution_id: string;
  status: 'draft' | 'confirmed';
  deadline: string;
  timezone: string;
  daily_minutes: number;
  draft_tasks: PlanDraftTask[];
  tasks: PlanTask[] | null;
  warnings?: string[];
  confirmed_at?: string | null;
  created_at?: string;
}

export interface PlanCreateRequest {
  analysis_id: string;
  deadline: string;
  timezone: string;
  daily_minutes: number;
}

export interface PlanConfirmResult {
  plan: PlanResult;
  execution_id: string;
  resumed: boolean;
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
