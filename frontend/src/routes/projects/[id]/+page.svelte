<script lang="ts">
  import { page } from '$app/stores';
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { getProject, getDatasets, getRuns, getModels } from '$lib/api';
  import type { Project, Dataset, Run, DeployedModel } from '$lib/types';
  import StatusBadge from '$lib/components/StatusBadge.svelte';
  import TaskBadge from '$lib/components/TaskBadge.svelte';

  const id = $derived($page.params.id as string);
  let project: Project | null   = $state(null);
  let datasets: Dataset[]       = $state([]);
  let runs: Run[]               = $state([]);
  let models: DeployedModel[]   = $state([]);
  let activeTab = $state('datasets');

  onMount(async () => {
    [project, datasets, runs, models] = await Promise.all([
      getProject(id),
      getDatasets(id),
      getRuns({ project_id: id }),
      getModels(id),
    ]);
  });

  const totalPairs = $derived(datasets.reduce((s, d) => s + (d.n_pairs || 0), 0));
</script>

{#if project}
  <div class="topbar">
    <span class="topbar-title">{project.name}</span>
    <TaskBadge domain={project.domain} taskType={project.task_type} />
    <span class="text-muted mono" style="font-size:12px">{project.model}</span>
    <a href="/train?project={project.id}" class="btn btn-primary">▷ Lancer training</a>
  </div>

  <div class="content">
    <!-- Stats header -->
    <div class="grid-4 mb-24">
      <div class="metric">
        <div class="metric-label">Pairs total</div>
        <div class="metric-value">{totalPairs.toLocaleString()}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Runs</div>
        <div class="metric-value">{runs.length}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Meilleur val loss</div>
        <div class="metric-value">
          {runs.reduce((best, r) => r.val_loss !== null && (best === null || r.val_loss < best) ? r.val_loss : best, null as number | null)?.toFixed(4) ?? '—'}
        </div>
      </div>
      <div class="metric">
        <div class="metric-label">Modèles déployés</div>
        <div class="metric-value">{models.length}</div>
      </div>
    </div>

    <!-- Tabs -->
    <div class="tabs">
      {#each ['datasets', 'runs', 'modèles', 'config'] as tab}
        <button class="tab {activeTab === tab ? 'active' : ''}" onclick={() => activeTab = tab}>{tab}</button>
      {/each}
    </div>

    <!-- Datasets -->
    {#if activeTab === 'datasets'}
      <div class="pipeline-funnel">
        {#each datasets as d}
          <div class="funnel-stage card">
            <div class="funnel-header">
              <span class="funnel-label">{d.stage}</span>
              <span class="funnel-count">{(d.n_pairs || 0).toLocaleString()} pairs</span>
            </div>
            <div class="funnel-bar-bg">
              <div class="funnel-bar" style="width:{datasets[0]?.n_pairs ? Math.min(100, (d.n_pairs / datasets[0].n_pairs) * 100) : 0}%"></div>
            </div>
            <div class="flex gap-16 mt-8" style="font-size:12px;color:var(--text-muted)">
              {#if d.n_train}<span>Train: {d.n_train}</span>{/if}
              {#if d.n_valid}<span>Valid: {d.n_valid}</span>{/if}
              {#if d.avg_quality}<span>Qualité moy: {d.avg_quality.toFixed(2)}</span>{/if}
              <span>{d.size_mb} MB</span>
            </div>
          </div>
        {/each}
      </div>
    {/if}

    <!-- Runs -->
    {#if activeTab === 'runs'}
      <div class="card">
        <table class="table">
          <thead>
            <tr><th>ID</th><th>Expérience</th><th>Statut</th><th>Train loss</th><th>Val loss</th><th>Iters</th><th>Durée</th><th>Date</th></tr>
          </thead>
          <tbody>
            {#each runs as r}
              <tr onclick={() => goto(`/runs/${r.id}`)}>
                <td class="mono text-accent">{r.id}</td>
                <td>{r.experiment_name}</td>
                <td><StatusBadge status={r.status} /></td>
                <td class="mono">{r.train_loss?.toFixed(4) ?? '—'}</td>
                <td class="mono">{r.val_loss?.toFixed(4) ?? '—'}</td>
                <td>{r.iters}</td>
                <td>{r.duration_s ? `${Math.round(r.duration_s/60)}min` : '—'}</td>
                <td class="text-muted">{r.started_at?.slice(0,10)}</td>
              </tr>
            {:else}
              <tr><td colspan="8" style="text-align:center;padding:24px;color:var(--text-muted)">Aucun run</td></tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}

    <!-- Modèles -->
    {#if activeTab === 'modèles'}
      <div class="grid-3">
        {#each models as m}
          <div class="card" onclick={() => goto(`/delivery/${m.id}`)} style="cursor:pointer">
            <div class="card-header">
              <div class="card-title">{m.model_name}</div>
              <StatusBadge status={m.status} />
            </div>
            <div class="text-muted" style="font-size:12px">Run: {m.run_id}</div>
            <div class="text-muted" style="font-size:12px">Port: {m.serving_port}</div>
            {#if m.size_mb}<div class="text-muted" style="font-size:12px">{m.size_mb} MB</div>{/if}
          </div>
        {:else}
          <div class="card" style="grid-column:span 3;text-align:center;color:var(--text-muted);padding:32px">Aucun modèle déployé</div>
        {/each}
      </div>
    {/if}

    <!-- Config -->
    {#if activeTab === 'config'}
      <div class="card">
        <pre class="yaml-preview">{JSON.stringify({
          domain: project.domain, task_type: project.task_type,
          model: project.model, model_format: project.model_format,
          quality_thresholds: project.quality_thresholds,
          min_tokens: project.min_tokens, max_tokens: project.max_tokens,
          blacklist_patterns: project.blacklist_patterns,
        }, null, 2)}</pre>
      </div>
    {/if}
  </div>
{:else}
  <div class="content"><p class="text-muted">Chargement…</p></div>
{/if}

<style>
  .pipeline-funnel { display: flex; flex-direction: column; gap: 12px; }
  .funnel-stage { }
  .funnel-header { display: flex; justify-content: space-between; margin-bottom: 8px; }
  .funnel-label { font-weight: 600; text-transform: uppercase; font-size: 12px; letter-spacing: 1px; color: var(--text-muted); }
  .funnel-count { font-weight: 700; font-size: 16px; }
  .funnel-bar-bg { height: 6px; background: var(--surface-3); border-radius: 3px; overflow: hidden; }
  .funnel-bar { height: 100%; background: var(--accent); border-radius: 3px; transition: width 400ms; }
  .yaml-preview {
    background: #0a0c12; padding: 16px; border-radius: var(--radius);
    font-family: var(--font-mono); font-size: 12px; line-height: 1.8;
    color: #a8b0c8; white-space: pre-wrap;
  }
</style>
