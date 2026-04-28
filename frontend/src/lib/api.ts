import type { AdvisorAdapter, AdvisorRecommendation, BaseModel, CollectJob, DataHealth, Dataset, DeployedModel, DownloadStatus, Evaluation, Health, JobStatus, Metric, PackageInfo, Project, ReviewItem, ReviewPage, ReviewStats, Run, SelectionEval } from './types';

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
export const advisorRecommend = (need: string, model?: string) =>
  req<AdvisorRecommendation>('/advisor/recommend', {
    method: 'POST',
    body: JSON.stringify({ need, model: model ?? 'us.anthropic.claude-sonnet-4-6' }),
  });

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

// Data
export const startCollect      = (body: { project_id: string; topic: string; max_pairs?: number }) =>
  req<CollectJob>('/data/collect', { method: 'POST', body: JSON.stringify(body) });
export const getCollectStatus  = (jobId: string) => req<JobStatus>(`/data/collect/${jobId}/status`);
export const getReview         = (projectId: string, page = 0, pageSize = 20, status?: string) =>
  req<ReviewPage>(`/data/review/${projectId}?page=${page}&page_size=${pageSize}${status ? `&status=${status}` : ''}`);
export const postReview        = (projectId: string, body: { index: number; action: string; corrected_prompt?: string; corrected_completion?: string }) =>
  req<{ ok: boolean; index: number; status: string }>(`/data/review/${projectId}`, { method: 'POST', body: JSON.stringify(body) });
export const getReviewStats    = (projectId: string) => req<ReviewStats>(`/data/review/${projectId}/stats`);
export const startPipeline     = (body: { project_id: string; use_llm_score?: boolean; threshold?: number }) =>
  req<CollectJob>('/data/pipeline', { method: 'POST', body: JSON.stringify(body) });
export const getPipelineStatus = (jobId: string) => req<JobStatus>(`/data/pipeline/${jobId}/status`);
export const getDataHealth     = (projectId: string) => req<DataHealth>(`/data/health/${projectId}`);
export const getLineage        = (projectId: string, limit = 100) => req<Record<string, unknown>[]>(`/data/lineage/${projectId}?limit=${limit}`);

// Project next-actions
export const getNextActions    = (projectId: string) => req<import('./types').NextActions>(`/projects/${projectId}/next-actions`);
