export interface ProjectAdapter {
  name: string;
  role: 'specialist' | 'orchestrator';
  description: string;
  estimated_pairs: number;
  rag_recommended: boolean;
  source_configs: SourceSuggestion[];
  data_sources: string[];
}

export interface Project {
  id: string;
  name: string;
  description: string;
  classification: string;
  domain: string;
  task_type: string;
  model: string;
  model_format: string;
  rag_index: string;
  quality_thresholds: { min_pass_rate: number; min_rouge_l: number; max_latency_ratio: number };
  min_tokens: number;
  max_tokens: number;
  blacklist_patterns: string[];
  n_runs: number;
  last_run: string | null;
  last_status: RunStatus | null;
  db: Record<string, unknown> | null;
  adapters?: ProjectAdapter[];
}

export interface BaseModel {
  id: string;
  name: string;
  hf_path: string;
  format: string;
  vram_gb: number;
  training: Record<string, number>;
  inference: Record<string, number>;
  downloaded: boolean;
  local_path: string | null;
  download_status: 'idle' | 'downloading' | 'completed' | 'failed';
}

export interface DownloadStatus {
  status: 'idle' | 'downloading' | 'completed' | 'failed' | 'already_downloaded';
  progress: number;
  error: string | null;
  local_path?: string;
}

export interface Dataset {
  stage: 'raw' | 'processed' | 'fine-tuning-ready';
  path: string;
  n_pairs: number;
  n_train: number | null;
  n_valid: number | null;
  avg_quality: number | null;
  size_mb: number;
  files: string[];
}

export type RunStatus = 'running' | 'completed' | 'failed' | 'cancelled';

export interface Run {
  id: string;
  project_id: string;
  experiment_name: string;
  model_name: string;
  status: RunStatus;
  iters: number;
  batch_size: number;
  learning_rate: number;
  lora_rank: number;
  train_loss: number | null;
  val_loss: number | null;
  duration_s: number | null;
  adapter_path: string | null;
  mlflow_run_id: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface Metric {
  step: number;
  train_loss: number;
  val_loss: number | null;
}

export interface Evaluation {
  id: string;
  run_id: string;
  project_id: string;
  eval_type: 'local' | 'bedrock_baseline' | 'human';
  bleu: number | null;
  rouge_l: number | null;
  pass_rate: number | null;
  latency_ms: number | null;
  notes: string | null;
  created_at: string;
}

export type ModelStatus = 'serving' | 'stopped' | 'error';

export interface DeployedModel {
  id: string;
  project_id: string;
  run_id: string;
  fused_path: string | null;
  serving_port: number;
  status: ModelStatus;
  deployed_at: string;
  stopped_at: string | null;
  model_name: string | null;
  val_loss: number | null;
  train_loss: number | null;
  project_name: string | null;
  size_mb: number | null;
}

export interface Health {
  mlx_server: boolean;
  mlflow: boolean;
  serving: boolean;
  disk_free_gb: number;
}

export interface CollectJob {
  job_id: string;
}

export interface JobStatus {
  status: 'running' | 'completed' | 'failed';
  lines: number;
}

export interface ReviewItem {
  prompt: string;
  completion: string;
  rag_context?: string;
  quality_score?: number;
  review_status?: 'validate' | 'reject' | 'correct';
  meta?: Record<string, unknown>;
}

export interface ReviewPage {
  total: number;
  page: number;
  page_size: number;
  items: ReviewItem[];
}

export interface ReviewStats {
  total: number;
  validated: number;
  rejected: number;
  corrected: number;
  pending: number;
}

export interface DataHealth {
  project_id: string;
  funnel: { raw: number; processed: number; fine_tuning_ready: number };
  stages: {
    raw: { exists: boolean; count: number; size_mb: number; avg_quality: number | null; scored_count: number; reviewed_count: number };
    processed: { exists: boolean; count: number; size_mb: number; avg_quality: number | null; scored_count: number; reviewed_count: number };
    fine_tuning_ready: { exists: boolean; count: number; size_mb: number; train: number; valid: number };
  };
  alerts: string[];
  lineage_summary?: Array<{ source_type: string; n: number; avg_q: number | null }>;
}

export interface SourceSuggestion {
  label: string;
  collector_type: 'rosetta_code' | 'crawl4ai' | 'manual' | 'github_repo';
  value: string;
  estimated_pairs?: number;
}

export interface AdvisorAdapter {
  name: string;
  role: 'specialist' | 'orchestrator';
  description: string;
  data_types: string[];
  data_sources: string[];
  source_configs?: SourceSuggestion[];
  rag_recommended?: boolean;
  estimated_pairs: number;
  // populated only for alternatives
  score?: number;
  team_score?: number;
  pros?: string[];
  cons?: string[];
  team_note?: string;
}

export interface SelectionEval {
  coverage: number;
  cohesion: number;
  balance: number;
  orchestrability: number;
  overall: number;
  gaps: string[];
  overlaps: string[][];
  recommendation: string;
}

export interface OverlapResolution {
  type: 'clarify' | 'merge' | 'remove';
  label: string;
  explanation: string;
  adapters: AdvisorAdapter[];
}

export interface AdvisorRecommendation {
  project: string;
  n_specialists: number;
  total_estimated_pairs: number;
  config_updated: string | null;
  recommendation: {
    adapters: AdvisorAdapter[];
    orchestrator: AdvisorAdapter;
    rag_global: boolean;
    notes: string;
    software_components?: string[];
  };
}

export interface NextAction {
  id: string;
  priority: 'high' | 'medium' | 'low';
  label: string;
  detail: string;
  href: string;
  icon: string;
}

export interface NextActions {
  project_id: string;
  pipeline_phase: 0 | 1 | 2 | 3 | 4;
  actions: NextAction[];
}

export interface AdapterCoverageItem {
  adapter_name: string;
  role: 'specialist' | 'orchestrator';
  target: number;
  collected: number;
  missing: number;
  pct: number;
  source_configs: SourceSuggestion[];
}

export interface AdapterCoverageResult {
  project_id: string;
  total_target: number;
  total_collected: number;
  total_raw: number;
  untagged: number;
  adapters: AdapterCoverageItem[];
}

export interface PackageInfo {
  model_id: string;
  project_name: string;
  model_name: string;
  deployed_at: string;
  fused: { path: string; exists: boolean; size_mb: number };
  adapter: { path: string; exists: boolean; size_mb: number };
  evaluations: Evaluation[];
  changelog: Partial<Run>[];
  run: { val_loss: number | null; iters: number; lora_rank: number; learning_rate: number };
}
