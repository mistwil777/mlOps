<script lang="ts">
  import { page } from '$app/stores';
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { getProject, getDatasets, getRuns, getModels, getNextActions } from '$lib/api';
  import type { Project, Dataset, Run, DeployedModel, NextActions } from '$lib/types';
  import StatusBadge from '$lib/components/StatusBadge.svelte';
  import TaskBadge from '$lib/components/TaskBadge.svelte';
  import PipelineStepper from '$lib/components/PipelineStepper.svelte';
  import ActionBar from '$lib/components/ActionBar.svelte';
  import Topbar from '$lib/components/Topbar.svelte';
  import { setCurrentProject } from '$lib/stores/currentProject.svelte';

  const id = $derived($page.params.id as string);
  let project: Project | null      = $state(null);
  let datasets: Dataset[]          = $state([]);
  let runs: Run[]                  = $state([]);
  let models: DeployedModel[]      = $state([]);
  let nextActions = $state<NextActions | null>(null);
  let activeTab = $state('overview');

  onMount(async () => {
    [project, datasets, runs, models, nextActions] = await Promise.all([
      getProject(id),
      getDatasets(id),
      getRuns({ project_id: id }),
      getModels(id),
      getNextActions(id).catch(() => null),
    ]);
    if (project) setCurrentProject(project);
  });

  const totalPairs  = $derived(datasets.reduce((s, d) => s + (d.n_pairs || 0), 0));
  const bestValLoss = $derived(
    runs.reduce(
      (best: number | null, r) =>
        r.val_loss !== null && (best === null || r.val_loss < best) ? r.val_loss : best,
      null
    )
  );
  const phase = $derived<0 | 1 | 2 | 3 | 4>(
    nextActions?.pipeline_phase ?? (runs.length ? (runs.some(r => r.status === 'completed') ? 3 : 2) : 1)
  );

  const classifColor: Record<string, string> = {
    public: 'var(--green)', interne: 'var(--text-muted)',
    confidentiel: 'var(--yellow)', secret: 'var(--red)',
  };

  const TABS = [
    { key: 'overview', label: 'Vue d\'ensemble' },
    { key: 'datasets', label: 'Données' },
    { key: 'runs',     label: 'Training' },
    { key: 'models',   label: 'Modèles' },
    { key: 'config',   label: 'Config' },
  ];
</script>

{#if project}
  <Topbar crumbs={[{ label: 'Projets', href: '/projects' }, { label: project.name ?? project.id }]}>
    <TaskBadge domain={project.domain} taskType={project.task_type} />
    {#if project.classification}
      <span style="font-size:var(--text-xs);font-weight:700;text-transform:uppercase;color:{classifColor[project.classification]??'var(--text-muted)'}">
        {project.classification}
      </span>
    {/if}
    <a href="/train?project={project.id}" class="btn btn-primary">▷ Training</a>
  </Topbar>

  <div class="content">
    <!-- Pipeline stepper -->
    <div class="stepper-card mb-24">
      <PipelineStepper {phase} />
    </div>

    <!-- Actions recommandées -->
    {#if nextActions?.actions?.length}
      <div class="mb-24">
        <ActionBar actions={nextActions.actions} />
      </div>
    {/if}

    <!-- KPIs -->
    <div class="grid-4 mb-24">
      <div class="metric">
        <div class="metric-label">Paires totales</div>
        <div class="metric-value">{totalPairs.toLocaleString()}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Runs</div>
        <div class="metric-value">{runs.length}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Meilleur val loss</div>
        <div class="metric-value">{bestValLoss?.toFixed(4) ?? '—'}</div>
      </div>
      <div class="metric">
        <div class="metric-label">Modèles déployés</div>
        <div class="metric-value">{models.length}</div>
      </div>
    </div>

    <!-- Tabs -->
    <div class="tabs mb-0">
      {#each TABS as tab}
        <button
          class="tab"
          class:active={activeTab === tab.key}
          onclick={() => activeTab = tab.key}
        >{tab.label}</button>
      {/each}
    </div>

    <!-- Tab: Overview -->
    {#if activeTab === 'overview'}
      <div class="tab-content">
        {#if project.description}
          <p class="proj-desc">{project.description}</p>
        {/if}

        <!-- Quick links -->
        <div class="quick-links">
          <a href="/data?project={project.id}" class="quick-link">
            <span class="ql-icon">◧</span>
            <span>Gérer les données</span>
          </a>
          <a href="/train?project={project.id}" class="quick-link">
            <span class="ql-icon">▷</span>
            <span>Lancer un run</span>
          </a>
          <a href="/playground?project={project.id}" class="quick-link">
            <span class="ql-icon">◐</span>
            <span>Playground</span>
          </a>
          <a href="/delivery?project={project.id}" class="quick-link">
            <span class="ql-icon">⬡</span>
            <span>Déploiement</span>
          </a>
        </div>
      </div>
    {/if}

    <!-- Tab: Données -->
    {#if activeTab === 'datasets'}
      <div class="tab-content">
        <div class="section-action">
          <a href="/data?project={project.id}" class="btn btn-ghost">Gérer → </a>
        </div>
        {#if datasets.length === 0}
          <div class="empty-state">Aucune donnée ingérée. <a href="/data/ingest">Lancer l'ingestion</a></div>
        {:else}
          <div class="pipeline-funnel">
            {#each datasets as d}
              <div class="funnel-stage card">
                <div class="funnel-header">
                  <span class="funnel-label">{d.stage}</span>
                  <span class="funnel-count">{(d.n_pairs || 0).toLocaleString()} paires</span>
                </div>
                <div class="funnel-bar-bg">
                  <div class="funnel-bar" style="width:{datasets[0]?.n_pairs ? Math.min(100, ((d.n_pairs ?? 0) / datasets[0].n_pairs) * 100) : 0}%"></div>
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
      </div>
    {/if}

    <!-- Tab: Training -->
    {#if activeTab === 'runs'}
      <div class="tab-content">
        <div class="section-action">
          <a href="/train?project={project.id}" class="btn btn-primary">▷ Nouveau run</a>
        </div>
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
                  <td>{r.duration_s ? `${Math.round(r.duration_s / 60)}min` : '—'}</td>
                  <td class="text-muted">{r.started_at?.slice(0, 10)}</td>
                </tr>
              {:else}
                <tr><td colspan="8" class="empty-cell">Aucun run</td></tr>
              {/each}
            </tbody>
          </table>
        </div>
      </div>
    {/if}

    <!-- Tab: Modèles -->
    {#if activeTab === 'models'}
      <div class="tab-content">
        {#if models.length === 0}
          <div class="empty-state">Aucun modèle déployé pour ce projet.</div>
        {:else}
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
            {/each}
          </div>
        {/if}
      </div>
    {/if}

    <!-- Tab: Config -->
    {#if activeTab === 'config'}
      <div class="tab-content">
        <div class="card">
          <pre class="yaml-preview">{JSON.stringify({
            domain: project.domain, task_type: project.task_type,
            model: project.model, model_format: project.model_format,
            quality_thresholds: project.quality_thresholds,
            min_tokens: project.min_tokens, max_tokens: project.max_tokens,
            blacklist_patterns: project.blacklist_patterns,
          }, null, 2)}</pre>
        </div>
      </div>
    {/if}
  </div>
{:else}
  <div class="content"><p class="text-muted">Chargement…</p></div>
{/if}

<style>
  .stepper-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    padding: 16px 24px;
  }

  .tab-content {
    padding: 20px 0 0;
  }

  .section-action {
    display: flex;
    justify-content: flex-end;
    margin-bottom: 12px;
  }

  .pipeline-funnel { display: flex; flex-direction: column; gap: 12px; }
  .funnel-header { display: flex; justify-content: space-between; margin-bottom: 8px; }
  .funnel-label { font-weight: 600; text-transform: uppercase; font-size: var(--text-xs); letter-spacing: .08em; color: var(--text-muted); }
  .funnel-count { font-weight: 700; font-size: var(--text-lg); }
  .funnel-bar-bg { height: 6px; background: var(--surface-3); border-radius: 3px; overflow: hidden; }
  .funnel-bar { height: 100%; background: var(--accent); border-radius: 3px; transition: width 400ms; }

  .yaml-preview {
    background: #0a0c12; padding: 16px; border-radius: var(--radius);
    font-family: var(--font-mono); font-size: var(--text-sm); line-height: 1.8;
    color: #a8b0c8; white-space: pre-wrap;
  }

  .proj-desc {
    font-size: var(--text-sm);
    color: var(--text-muted);
    line-height: 1.6;
    margin-bottom: 20px;
  }

  .quick-links {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 10px;
  }

  .quick-link {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
    padding: 16px 12px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    text-decoration: none;
    color: var(--text-muted);
    font-size: var(--text-sm);
    transition: border-color 150ms, background 150ms, color 150ms;
    text-align: center;
  }

  .quick-link:hover {
    border-color: var(--accent);
    background: var(--accent-dim);
    color: var(--accent);
    text-decoration: none;
  }

  .ql-icon { font-size: 20px; }

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
</style>
