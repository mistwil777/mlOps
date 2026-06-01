import type { AdapterCoverageResult, AdvisorAdapter, AdvisorRecommendation, BaseModel, CollectJob, DataHealth, Dataset, DeployedModel, DownloadStatus, Evaluation, Health, JobStatus, Metric, OverlapResolution, PackageInfo, Project, QualitySummary, RejectedPage, ReviewStats, Run, SamplePage, SelectionEval } from './types';

const BASE = '/api';

async function req<T>(path: string, opts: RequestInit = {}): Promise<T> {
  const r = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json', ...opts.headers },
    ...opts,
  });
  if (!r.ok) {
    const err = await r.text().catch(() => r.statusText);
    throw new Error(`${r.status} ${err}`);
  }
  if (r.status === 204 || r.headers.get('content-length') === '0') return undefined as T;
  return r.json() as Promise<T>;
}

// Health
export const getHealth = () => req<Health>('/health');

// Projects
export const getProjects   = ()           => req<Project[]>('/projects');
export const getProject    = (id: string) => req<Project>(`/projects/${id}`);
export const createProject = (body: unknown) => req<Project>('/projects', { method: 'POST', body: JSON.stringify(body) });
export const patchProject  = (id: string, body: unknown) => req<Project>(`/projects/${id}`, { method: 'PATCH', body: JSON.stringify(body) });
export const deleteProject = (id: string) => req<void>(`/projects/${id}`, { method: 'DELETE' });

// Base models
export const getBaseModels       = ()              => req<BaseModel[]>('/base-models');
export const getTaskTypes        = ()              => req<string[]>('/base-models/task-types');
export const getDomains          = ()              => req<string[]>('/base-models/domains');
export const validateModel       = (id: string)   => req<{ valid: boolean; error: string | null }>(`/base-models/${id}/validate`, { method: 'POST' });
export const startDownload       = (id: string)   => req<DownloadStatus>(`/base-models/${id}/download`, { method: 'POST' });
export const getDownloadStatus   = (id: string)   => req<DownloadStatus>(`/base-models/${id}/download/status`);

// Datasets
export const getDatasets = (projectId: string) => req<Dataset[]>(`/projects/${projectId}/datasets`);

// Runs
export const getRuns        = (params?: { project_id?: string; status?: string; limit?: number }) => {
  const p = new URLSearchParams();
  if (params?.project_id) p.set('project_id', params.project_id);
  if (params?.status)     p.set('status', params.status);
  if (params?.limit)      p.set('limit', String(params.limit));
  return req<Run[]>('/runs?' + p.toString());
};
export const getRun         = (id: string) => req<Run>(`/runs/${id}`);
export const createRun      = (body: unknown) => req<{ run_id: string }>('/runs', { method: 'POST', body: JSON.stringify(body) });
export const getRunMetrics  = (id: string) => req<Metric[]>(`/runs/${id}/metrics`);
export const getRunEvals      = (id: string)            => req<Evaluation[]>(`/runs/${id}/evaluations`);
export const listEvaluations  = (projectId?: string)    => req<(Evaluation & { project_id: string; experiment_name: string; model_name: string })[]>(`/runs/evaluations${projectId ? `?project_id=${projectId}` : ''}`);
export const launchEvaluation = (runId: string)         => req<{ eval_id: string; status: string }>(`/runs/${runId}/evaluate`, { method: 'POST' });
export const cancelRun        = (runId: string)         => req<{ run_id: string; status: string }>(`/runs/${runId}/cancel`, { method: 'POST' });
export const deleteRun        = (runId: string)         => req<void>(`/runs/${runId}`, { method: 'DELETE' });
export const launchEval     = (id: string) => req<unknown>(`/runs/${id}/evaluate`, { method: 'POST' });

// Models
export const getModels      = (projectId?: string) =>
  req<DeployedModel[]>('/models' + (projectId ? `?project_id=${projectId}` : ''));
export const deployModel    = (body: unknown) => req<{ model_id: string }>('/models', { method: 'POST', body: JSON.stringify(body) });
export const serveModel     = (id: string) => req<unknown>(`/models/${id}/serve`, { method: 'POST' });
export const stopModel      = (id: string) => req<unknown>(`/models/${id}/stop`, { method: 'POST' });
export const deleteModel    = (id: string) => req<unknown>(`/models/${id}`, { method: 'DELETE' });

// Delivery
export const getPackageInfo = (id: string) => req<PackageInfo>(`/models/${id}/package`);

// Advisor
export const advisorRecommend = (need: string, model?: string, docContexts?: string[]) =>
  req<AdvisorRecommendation>('/advisor/recommend', {
    method: 'POST',
    body: JSON.stringify({ need, model: model ?? 'us.anthropic.claude-sonnet-4-6', doc_contexts: docContexts ?? [] }),
  });

export const advisorExtractDoc = async (file: File): Promise<{ filename: string; char_count: number; was_summarized: boolean; text: string }> => {
  const fd = new FormData();
  fd.append('file', file);
  const r = await fetch(`${BASE}/advisor/extract-doc`, { method: 'POST', body: fd });
  if (!r.ok) {
    const err = await r.text().catch(() => r.statusText);
    throw new Error(`${r.status} ${err}`);
  }
  return r.json();
};

export const advisorAlternatives = (need: string, adapter_name: string, adapter_desc: string, current_selection: AdvisorAdapter[], model?: string) =>
  req<AdvisorAdapter[]>('/advisor/alternatives', {
    method: 'POST',
    body: JSON.stringify({ need, adapter_name, adapter_desc, current_selection, model: model ?? 'us.anthropic.claude-sonnet-4-6' }),
  });

export const advisorEvaluateSelection = (need: string, adapters: AdvisorAdapter[], model?: string) =>
  req<SelectionEval>('/advisor/evaluate-selection', {
    method: 'POST',
    body: JSON.stringify({ need, adapters, model: model ?? 'us.anthropic.claude-sonnet-4-6' }),
  });

export const advisorFillGap = (need: string, gap: string, current_selection: AdvisorAdapter[], model?: string) =>
  req<AdvisorAdapter>('/advisor/fill-gap', {
    method: 'POST',
    body: JSON.stringify({ need, gap, current_selection, model: model ?? 'us.anthropic.claude-sonnet-4-6' }),
  });

export const advisorResolveOverlap = (need: string, adapter_a: AdvisorAdapter, adapter_b: AdvisorAdapter, current_selection: AdvisorAdapter[], model?: string) =>
  req<OverlapResolution[]>('/advisor/resolve-overlap', {
    method: 'POST',
    body: JSON.stringify({ need, adapter_a, adapter_b, current_selection, model: model ?? 'us.anthropic.claude-sonnet-4-6' }),
  });

export interface PairAnalysisResult {
  adapter_name: string;
  quality_report: {
    total_analyzed: number;
    n_keep: number;
    n_fix: number;
    n_discard: number;
    avg_score: number;
    common_issues: string[];
    scored_pairs: Array<{ prompt: string; completion: string; score: number; issues: string[]; verdict: 'keep' | 'fix' | 'discard' }>;
  };
  generated_pairs: Array<{ prompt: string; completion: string }>;
  manual_brief: string;
}

export const advisorAnalyzePairs = (body: {
  adapter_name: string;
  adapter_desc: string;
  need: string;
  pairs_sample: Array<{ prompt: string; completion: string }>;
  n_generate?: number;
  target_pairs?: number;
}) => req<PairAnalysisResult>('/advisor/analyze-pairs', { method: 'POST', body: JSON.stringify(body) });

// Data
export const startCollect      = (body: { project_id: string; topic?: string; max_pairs?: number; domain?: string; task_type?: string; collector_type?: string; source_value?: string; adapter_name?: string; lang?: string; langs?: string[]; max_depth?: number }) =>
  req<CollectJob>('/data/collect', { method: 'POST', body: JSON.stringify(body) });
export const startFuse         = (body: { project_id: string; lang_pair: string; adapter_name?: string; max_fused?: number; llm_model?: string }) =>
  req<CollectJob>('/data/fuse', { method: 'POST', body: JSON.stringify(body) });
export const getAdapterCoverage = (projectId: string) => req<AdapterCoverageResult>(`/data/adapter-coverage/${projectId}`);
export const getCollectStatus  = (jobId: string) => req<JobStatus>(`/data/collect/${jobId}/status`);
// Review — pipeline-aware
export const getQualitySummary   = (projectId: string) => req<QualitySummary>(`/data/review/${projectId}/quality-summary`);
export const getRejected         = (projectId: string, step?: string, page = 0, pageSize = 20) =>
  req<RejectedPage>(`/data/review/${projectId}/rejected?page=${page}&page_size=${pageSize}${step ? `&step=${step}` : ''}`);
export const getSample           = (projectId: string, n = 20, adapterName?: string) =>
  req<SamplePage>(`/data/review/${projectId}/sample?n=${n}${adapterName ? `&adapter_name=${encodeURIComponent(adapterName)}` : ''}`);
export const flagRecord          = (projectId: string, body: { index: number; action: 'flag'; note?: string }) =>
  req<{ ok: boolean; flagged_total: number }>(`/data/review/${projectId}/flag`, { method: 'POST', body: JSON.stringify(body) });
export const rescueRecord        = (projectId: string, body: { index: number; action: 'rescue'; corrections?: Record<string, string>; note?: string }) =>
  req<{ ok: boolean; rescued_from: string; record: unknown }>(`/data/review/${projectId}/rescue`, { method: 'POST', body: JSON.stringify(body) });
export const getReviewStats      = (projectId: string) => req<ReviewStats>(`/data/review/${projectId}/stats`);
export const startPipeline     = (body: { project_id: string; threshold?: number }) =>
  req<CollectJob>('/data/pipeline', { method: 'POST', body: JSON.stringify(body) });
export const getPipelineStatus = (jobId: string) => req<JobStatus>(`/data/pipeline/${jobId}/status`);
export const getDataHealth     = (projectId: string) => req<DataHealth>(`/data/health/${projectId}`);
export const getLineage        = (projectId: string, limit = 100) => req<Record<string, unknown>[]>(`/data/lineage/${projectId}?limit=${limit}`);
export const browsePairs       = (projectId: string, stage = 'raw', adapterName?: string, page = 0, pageSize = 20) =>
  req<{ total: number; page: number; page_size: number; items: DatalakePair[] }>(`/data/browse/${projectId}?stage=${stage}&page=${page}&page_size=${pageSize}${adapterName ? `&adapter_name=${encodeURIComponent(adapterName)}` : ''}`);

export interface DatalakePair {
  project_id: string;
  stage: string;
  prompt: string;
  completion: string;
  quality_score?: number;
  collected_at?: string;
  source_url?: string;
  adapter_name?: string;
  pair_hash: string;
}

export interface CrossProjectMatch {
  total_matching: number;
  by_project: Record<string, number>;
  samples: DatalakePair[];
  oldest_collected_at: string | null;
}

export const searchDatalakePairs = (body: {
  query: string; project_id?: string; stage?: string; k?: number; page?: number;
}) => req<{ total: number; page: number; page_size: number; items: DatalakePair[] }>(
  '/data/search', { method: 'POST', body: JSON.stringify(body) }
);

export const indexProjectPairs = (projectId: string) =>
  req<{ ok: boolean; project_id: string }>(`/data/index-project/${projectId}`, { method: 'POST' });

export const advisorCrossProjectMatches = (adapterDesc: string, k = 10) =>
  req<CrossProjectMatch>(`/advisor/cross-project-matches?adapter_desc=${encodeURIComponent(adapterDesc)}&k=${k}`);

export const advisorGetRecommendation = (projectId: string) =>
  req<AdvisorRecommendation>(`/advisor/recommendation/${projectId}`);

export const advisorPatchAdapterSources = (projectId: string, adapterName: string, sourceConfigs: import('./types').SourceSuggestion[]) =>
  req<{ ok: boolean; adapter_name: string; n_sources: number }>(`/advisor/adapter-sources/${projectId}`, {
    method: 'PATCH',
    body: JSON.stringify({ adapter_name: adapterName, source_configs: sourceConfigs }),
  });

export const advisorRecommendSources = (body: {
  adapter_name: string;
  adapter_description: string;
  domain?: string;
  task_type?: string;
  existing_source_values?: string[];
  existing_source_labels?: string[];
}) => req<Array<{
  label: string;
  collector_type: string;
  value: string;
  estimated_pairs: number;
  lang_hint?: string;
  reason?: string;
}>>('/advisor/recommend-sources', { method: 'POST', body: JSON.stringify(body) });

// Project next-actions
export const getNextActions    = (projectId: string) => req<import('./types').NextActions>(`/projects/${projectId}/next-actions`);
