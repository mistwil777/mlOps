<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { getHealth, getProjects, getRuns, getModels } from '$lib/api';
  import type { Run, Project, DeployedModel, Health } from '$lib/types';
  import MetricCard from '$lib/components/MetricCard.svelte';
  import StatusBadge from '$lib/components/StatusBadge.svelte';
  import LossChart from '$lib/components/LossChart.svelte';
  import { getRunMetrics } from '$lib/api';
  import type { Metric } from '$lib/types';

  let health: Health | null = $state(null);
  let projects: Project[]       = $state([]);
  let activeRuns: Run[]         = $state([]);
  let recentRuns: Run[]         = $state([]);
  let models: DeployedModel[]   = $state([]);
  let runMetrics: Record<string, Metric[]> = $state({});

  onMount(async () => {
    [health, projects, recentRuns, models] = await Promise.all([
      getHealth().catch(() => null),
      getProjects().catch(() => []),
      getRuns({ limit: 10 } as Parameters<typeof getRuns>[0]).catch(() => []),
      getModels().catch(() => []),
    ]);
    activeRuns = recentRuns.filter(r => r.status === 'running');

    for (const r of activeRuns) {
      getRunMetrics(r.id).then(m => { runMetrics[r.id] = m; });
    }
  });

  function fmtDuration(s: number | null) {
    if (!s) return '—';
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.round(s/60)}min`;
    return `${(s/3600).toFixed(1)}h`;
  }

  const completedRuns = $derived(recentRuns.filter(r => r.status === 'completed'));
  const totalPairs    = $derived(projects.reduce((a) => a, 0));
</script>

<div class="topbar">
  <span class="topbar-title">Dashboard</span>
  <a href="/projects/new" class="btn btn-primary">+ Nouveau projet</a>
  <a href="/train" class="btn btn-ghost">▷ Lancer training</a>
</div>

<div class="content">
  <!-- Métriques globales -->
  <div class="grid-4 mb-24">
    <MetricCard label="Projets actifs" value={projects.length} />
    <MetricCard label="Runs actifs" value={activeRuns.length} color={activeRuns.length > 0 ? 'var(--green)' : undefined} />
    <MetricCard label="Modèles déployés" value={models.filter(m => m.status === 'serving').length} />
    <MetricCard label="Disque libre" value={health ? `${health.disk_free_gb} GB` : '…'} />
  </div>

  <!-- Runs actifs -->
  {#if activeRuns.length > 0}
    <h2 class="section-title">Runs en cours</h2>
    <div class="grid-2 mb-24">
      {#each activeRuns as run}
        <div class="card" onclick={() => goto(`/runs/${run.id}`)} style="cursor:pointer">
          <div class="card-header">
            <div>
              <div class="card-title">{run.experiment_name}</div>
              <div class="text-muted" style="font-size:12px;margin-top:2px">{run.project_id} · {run.model_name}</div>
            </div>
            <StatusBadge status={run.status} />
          </div>
          <LossChart metrics={runMetrics[run.id] ?? []} height={140} />
          <div class="flex items-center gap-16 mt-8" style="font-size:12px;color:var(--text-muted)">
            <span>Iters: {run.iters}</span>
            <span>LR: {run.learning_rate}</span>
            <span>Rank: {run.lora_rank}</span>
          </div>
        </div>
      {/each}
    </div>
  {/if}

  <!-- Runs récents -->
  <div class="flex justify-between items-center mb-16">
    <h2 class="section-title" style="margin-bottom:0">Runs récents</h2>
    <a href="/train" class="text-accent" style="font-size:13px">Nouveau run →</a>
  </div>
  <div class="card mb-24">
    <table class="table">
      <thead>
        <tr>
          <th>Run</th><th>Projet</th><th>Modèle</th><th>Statut</th>
          <th>Train loss</th><th>Val loss</th><th>Durée</th><th>Date</th>
        </tr>
      </thead>
      <tbody>
        {#each recentRuns as run}
          <tr onclick={() => goto(`/runs/${run.id}`)}>
            <td class="mono text-accent">{run.id}</td>
            <td>{run.project_id}</td>
            <td class="truncate" style="max-width:140px">{run.model_name}</td>
            <td><StatusBadge status={run.status} /></td>
            <td class="mono">{run.train_loss?.toFixed(4) ?? '—'}</td>
            <td class="mono">{run.val_loss?.toFixed(4) ?? '—'}</td>
            <td>{fmtDuration(run.duration_s)}</td>
            <td class="text-muted">{run.started_at?.slice(0, 10)}</td>
          </tr>
        {:else}
          <tr><td colspan="8" style="text-align:center;color:var(--text-muted);padding:24px">Aucun run. <a href="/train">Lancer un premier training</a></td></tr>
        {/each}
      </tbody>
    </table>
  </div>

  <!-- Modèles déployés -->
  <div class="flex justify-between items-center mb-16">
    <h2 class="section-title" style="margin-bottom:0">Modèles</h2>
    <a href="/models" class="text-accent" style="font-size:13px">Voir tous →</a>
  </div>
  <div class="grid-3">
    {#each models.slice(0, 3) as m}
      <div class="card" onclick={() => goto(`/delivery/${m.id}`)} style="cursor:pointer">
        <div class="card-header">
          <div>
            <div class="card-title">{m.project_name ?? m.project_id}</div>
            <div class="text-muted" style="font-size:12px">{m.model_name}</div>
          </div>
          <StatusBadge status={m.status} />
        </div>
        <div class="flex gap-16 mt-8" style="font-size:12px;color:var(--text-muted)">
          <span>Port {m.serving_port}</span>
          {#if m.val_loss}<span>Val loss: {m.val_loss.toFixed(4)}</span>{/if}
          {#if m.size_mb}<span>{m.size_mb} MB</span>{/if}
        </div>
      </div>
    {:else}
      <div class="card" style="grid-column:span 3;text-align:center;color:var(--text-muted);padding:32px">
        Aucun modèle déployé. <a href="/train">Lancer un training</a> pour commencer.
      </div>
    {/each}
  </div>
</div>

<style>
  .section-title { font-size: 15px; font-weight: 600; margin-bottom: 16px; }
</style>
