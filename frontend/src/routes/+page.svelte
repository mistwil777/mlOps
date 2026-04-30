<script lang="ts">
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { getHealth, getProjects, getRuns, getModels } from '$lib/api';
  import type { Run, Project, DeployedModel, Health } from '$lib/types';
  import MetricCard from '$lib/components/MetricCard.svelte';
  import StatusBadge from '$lib/components/StatusBadge.svelte';
  import LossChart from '$lib/components/LossChart.svelte';
  import ProjectCard from '$lib/components/ProjectCard.svelte';
  import SovereigntyBadge from '$lib/components/SovereigntyBadge.svelte';
  import Topbar from '$lib/components/Topbar.svelte';
  import { getRunMetrics, deleteRun } from '$lib/api';
  import type { Metric } from '$lib/types';

  let health: Health | null           = $state(null);
  let projects: Project[]             = $state([]);
  let activeRuns: Run[]               = $state([]);
  let recentRuns: Run[]               = $state([]);
  let models: DeployedModel[]         = $state([]);
  let runMetrics: Record<string, Metric[]> = $state({});
  let loading = $state(true);

  onMount(async () => {
    [health, projects, recentRuns, models] = await Promise.all([
      getHealth().catch(() => null),
      getProjects().catch(() => []),
      getRuns({ limit: 10 } as Parameters<typeof getRuns>[0]).catch(() => []),
      getModels().catch(() => []),
    ]);
    activeRuns = recentRuns.filter(r => r.status === 'running');
    loading = false;
    for (const r of activeRuns) {
      getRunMetrics(r.id).then(m => { runMetrics[r.id] = m; });
    }
  });

  function fmtDuration(s: number | null) {
    if (!s) return '—';
    if (s < 60) return `${s}s`;
    if (s < 3600) return `${Math.round(s / 60)}min`;
    return `${(s / 3600).toFixed(1)}h`;
  }

  const completedRuns  = $derived(recentRuns.filter(r => r.status === 'completed'));
  const servingModels  = $derived(models.filter(m => m.status === 'serving'));
</script>

<Topbar title="Dashboard">
  <SovereigntyBadge mode="local" />
</Topbar>

<div class="content">
  <!-- KPI row -->
  <div class="grid-4 mb-24">
    <MetricCard label="Projets" value={projects.length} />
    <MetricCard
      label="Runs actifs"
      value={activeRuns.length}
      color={activeRuns.length > 0 ? 'var(--green)' : undefined}
    />
    <MetricCard
      label="Modèles servis"
      value={servingModels.length}
      color={servingModels.length > 0 ? 'var(--accent)' : undefined}
    />
    <MetricCard
      label="Disque libre"
      value={health ? `${health.disk_free_gb} GB` : '…'}
    />
  </div>

  <!-- Active runs -->
  {#if activeRuns.length > 0}
    <div class="section-header mb-16">
      <h2 class="section-title">Runs en cours</h2>
      <span class="badge badge-running">{activeRuns.length} actif{activeRuns.length > 1 ? 's' : ''}</span>
    </div>
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
          <LossChart metrics={runMetrics[run.id] ?? []} height={120} />
          <div class="flex items-center gap-16 mt-8" style="font-size:12px;color:var(--text-muted)">
            <span>Iters: {run.iters}</span>
            <span>LR: {run.learning_rate}</span>
            <span>Rank: {run.lora_rank}</span>
          </div>
        </div>
      {/each}
    </div>
  {/if}

  <!-- Projects with pipeline stepper -->
  <div class="section-header mb-16">
    <h2 class="section-title">Projets</h2>
    <a href="/projects" class="text-accent" style="font-size:13px">Voir tous →</a>
  </div>

  {#if loading}
    <div class="empty-state mb-24">Chargement…</div>
  {:else if projects.length === 0}
    <div class="empty-state mb-24">
      Aucun projet. <a href="/start">Créer le premier →</a>
    </div>
  {:else}
    <div class="project-list mb-24">
      {#each projects.slice(0, 6) as project}
        <ProjectCard {project} onDelete={(id) => { projects = projects.filter(p => p.id !== id); }} />
      {/each}
    </div>
  {/if}

  <!-- Recent runs table -->
  <div class="section-header mb-16">
    <h2 class="section-title">Runs récents</h2>
    <a href="/train" class="text-accent" style="font-size:13px">Nouveau run →</a>
  </div>
  <div class="card mb-24">
    <table class="table">
      <thead>
        <tr>
          <th>Run</th><th>Projet</th><th>Modèle</th><th>Statut</th>
          <th>Train loss</th><th>Val loss</th><th>Durée</th><th>Date</th><th></th>
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
            <td onclick={e => e.stopPropagation()} role="cell">
              <button
                class="run-trash"
                title="Archiver ce run"
                onclick={async () => {
                  await deleteRun(run.id);
                  recentRuns = recentRuns.filter(r => r.id !== run.id);
                }}
              >
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
                  <polyline points="3 6 5 6 21 6"/>
                  <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6"/>
                  <path d="M10 11v6M14 11v6"/>
                  <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2"/>
                </svg>
              </button>
            </td>
          </tr>
        {:else}
          <tr>
            <td colspan="9" class="empty-cell">
              Aucun run. <a href="/train">Lancer un premier training</a>
            </td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>

  <!-- Deployed models -->
  {#if servingModels.length > 0 || models.length > 0}
    <div class="section-header mb-16">
      <h2 class="section-title">Modèles</h2>
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
      {/each}
    </div>
  {/if}
</div>

<style>
  .section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
  }

  .section-title {
    font-size: var(--text-md);
    font-weight: 600;
  }

  .project-list {
    display: flex;
    flex-direction: column;
    gap: 6px;
  }

  .empty-state {
    text-align: center;
    color: var(--text-muted);
    padding: 32px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    font-size: var(--text-sm);
  }

  .empty-cell {
    text-align: center;
    color: var(--text-muted);
    padding: 24px;
  }

  .run-trash {
    background: none;
    border: 1px solid transparent;
    border-radius: 4px;
    padding: 3px 6px;
    cursor: pointer;
    color: var(--text-dim);
    display: flex;
    align-items: center;
    transition: border-color 150ms, color 150ms;
  }
  .run-trash:hover { border-color: var(--red); color: var(--red); }
</style>
