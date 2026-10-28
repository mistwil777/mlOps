<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { listEvaluations, launchEvaluation, getRuns, getProjects } from '$lib/api';
  import type { Evaluation, Project, Run } from '$lib/types';
  import Topbar from '$lib/components/Topbar.svelte';
  import StatusBadge from '$lib/components/StatusBadge.svelte';
  import SectionTabs from '$lib/components/SectionTabs.svelte';
  const tabs = [{ href: '/eval', label: 'Tests' }, { href: '/playground', label: 'Playground' }];

  type EvalRow = Evaluation & { project_id: string; experiment_name: string; model_name: string };

  let evals: EvalRow[]  = $state([]);
  let projects: Project[] = $state([]);
  let runs: Run[]         = $state([]);
  let selectedProject: string = $state('');
  let launching: string | null = $state(null);
  let loading = $state(true);

  onMount(async () => {
    [projects, runs, evals] = await Promise.all([
      getProjects().catch(() => []),
      getRuns({ status: 'completed', limit: 50 }).catch(() => []),
      listEvaluations().catch(() => []),
    ]);
    loading = false;
  });

  const filteredEvals = $derived(
    selectedProject ? evals.filter(e => e.project_id === selectedProject) : evals
  );

  const unevaluatedRuns = $derived(
    runs.filter(r =>
      (!selectedProject || r.project_id === selectedProject) &&
      !evals.some(e => e.run_id === r.id)
    )
  );

  async function launchEval(runId: string) {
    launching = runId;
    try {
      await launchEvaluation(runId);
      evals = await listEvaluations(selectedProject || undefined);
    } finally {
      launching = null;
    }
  }

  function scoreColor(val: number | null, max = 1): string {
    if (val === null) return 'var(--text-dim)';
    const pct = val / max;
    if (pct >= 0.7) return 'var(--green)';
    if (pct >= 0.4) return 'var(--yellow)';
    return 'var(--red)';
  }

  function fmtLatency(ms: number | null): string {
    if (ms === null) return '—';
    if (ms < 1000) return `${ms}ms`;
    return `${(ms / 1000).toFixed(1)}s`;
  }
</script>

<SectionTabs {tabs} />
<Topbar crumbs={[{ label: 'Évaluation' }]}>
  <select
    class="form-select"
    style="width:180px"
    bind:value={selectedProject}
  >
    <option value="">Tous les projets</option>
    {#each projects as p}
      <option value={p.id}>{p.name ?? p.id}</option>
    {/each}
  </select>
</Topbar>

<div class="content">
  <!-- Runs awaiting evaluation -->
  {#if unevaluatedRuns.length > 0}
    <div class="alert-banner mb-24">
      <span class="alert-icon">⚠</span>
      <div>
        <span class="alert-title">{unevaluatedRuns.length} run{unevaluatedRuns.length > 1 ? 's' : ''} non évalué{unevaluatedRuns.length > 1 ? 's' : ''}</span>
        <span class="alert-sub"> — Évaluation requise avant déploiement (règle plateforme)</span>
      </div>
      <div class="uneval-list">
        {#each unevaluatedRuns.slice(0, 5) as run}
          <div class="uneval-row">
            <span class="mono text-accent">{run.id}</span>
            <span class="text-muted">{run.project_id} · {run.experiment_name}</span>
            <button
              class="btn btn-ghost btn-sm"
              disabled={launching === run.id}
              onclick={() => launchEval(run.id)}
            >
              {launching === run.id ? '…' : '▷ Évaluer'}
            </button>
          </div>
        {/each}
      </div>
    </div>
  {/if}

  <!-- Evaluations table -->
  <div class="section-header mb-16">
    <h2 class="section-title">Évaluations</h2>
    <span class="text-muted" style="font-size:var(--text-sm)">{filteredEvals.length} résultat{filteredEvals.length !== 1 ? 's' : ''}</span>
  </div>

  {#if loading}
    <div class="empty-state">Chargement…</div>
  {:else if filteredEvals.length === 0}
    <div class="empty-state">
      Aucune évaluation. Lancer un run terminé, puis cliquer "Évaluer".
    </div>
  {:else}
    <div class="card">
      <table class="table">
        <thead>
          <tr>
            <th>Run</th>
            <th>Projet</th>
            <th>Expérience</th>
            <th>Type</th>
            <th>BLEU</th>
            <th>ROUGE-L</th>
            <th>Pass rate</th>
            <th>Latence</th>
            <th>Date</th>
          </tr>
        </thead>
        <tbody>
          {#each filteredEvals as ev}
            <tr onclick={() => goto(`/runs/${ev.run_id}`)} style="cursor:pointer">
              <td class="mono text-accent">{ev.run_id}</td>
              <td class="text-muted">{ev.project_id}</td>
              <td class="truncate" style="max-width:160px">{ev.experiment_name}</td>
              <td>
                <span class="type-badge type-{ev.eval_type}">
                  {ev.eval_type === 'bedrock_baseline' ? 'Bedrock' : ev.eval_type}
                </span>
              </td>
              <td class="mono" style="color:{scoreColor(ev.bleu)}">{ev.bleu?.toFixed(3) ?? '—'}</td>
              <td class="mono" style="color:{scoreColor(ev.rouge_l)}">{ev.rouge_l?.toFixed(3) ?? '—'}</td>
              <td class="mono" style="color:{scoreColor(ev.pass_rate)}">{ev.pass_rate !== null ? `${(ev.pass_rate * 100).toFixed(1)}%` : '—'}</td>
              <td class="mono text-muted">{fmtLatency(ev.latency_ms)}</td>
              <td class="text-muted">{ev.created_at?.slice(0, 10)}</td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

<style>
  .alert-banner {
    display: flex;
    flex-direction: column;
    gap: 12px;
    padding: 14px 16px;
    background: var(--yellow-dim);
    border: 1px solid rgba(247,170,80,.3);
    border-radius: var(--radius-md);
  }

  .alert-icon { font-size: 16px; color: var(--yellow); flex-shrink: 0; }

  .alert-title { font-weight: 600; color: var(--yellow); font-size: var(--text-sm); }
  .alert-sub   { font-size: var(--text-sm); color: var(--text-muted); }

  .uneval-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .uneval-row {
    display: flex;
    align-items: center;
    gap: 12px;
    font-size: var(--text-sm);
  }

  .btn-sm { padding: 4px 10px; font-size: var(--text-sm); font-weight: 500; }

  .section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }

  .section-title { font-size: var(--text-md); font-weight: 600; }

  .type-badge {
    display: inline-block;
    padding: 1px 7px;
    border-radius: 999px;
    font-size: var(--text-xs);
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: .05em;
  }

  .type-local            { background: var(--green-dim);  color: var(--green); }
  .type-bedrock_baseline { background: var(--yellow-dim); color: var(--yellow); }
  .type-human            { background: var(--accent-dim); color: var(--accent); }

  .empty-state {
    text-align: center;
    color: var(--text-muted);
    padding: 48px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    font-size: var(--text-sm);
  }
</style>
