<script lang="ts">
  import { page } from '$app/stores';
  import { onMount } from 'svelte';
  import { goto } from '$app/navigation';
  import { getProject, getDatasets, getRuns, getModels, getNextActions } from '$lib/api';
  import type { Project, Dataset, Run, DeployedModel, NextActions } from '$lib/types';
  import TaskBadge from '$lib/components/TaskBadge.svelte';
  import ActionBar from '$lib/components/ActionBar.svelte';
  import Topbar from '$lib/components/Topbar.svelte';
  import { setCurrentProject } from '$lib/stores/currentProject.svelte';

  const id = $derived($page.params.id as string);
  let project: Project | null   = $state(null);
  let datasets: Dataset[]       = $state([]);
  let runs: Run[]               = $state([]);
  let models: DeployedModel[]   = $state([]);
  let nextActions = $state<NextActions | null>(null);

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

  const classifColor: Record<string, string> = {
    public: 'var(--green)', interne: 'var(--text-muted)',
    confidentiel: 'var(--yellow)', secret: 'var(--red)',
  };
</script>

{#if project}
  <Topbar crumbs={[{ label: 'Projets', href: '/projects' }, { label: project.name ?? project.id }]}>
    <TaskBadge domain={project.domain} taskType={project.task_type} />
    {#if project.classification}
      <span style="font-size:var(--text-xs);font-weight:700;text-transform:uppercase;color:{classifColor[project.classification]??'var(--text-muted)'}">
        {project.classification}
      </span>
    {/if}
  </Topbar>

  <div class="content">
    {#if nextActions?.actions?.length}
      <div class="mb-24">
        <ActionBar actions={nextActions.actions} />
      </div>
    {/if}

    <div class="grid-4">
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
  </div>
{:else}
  <div class="content"><p class="text-muted">Chargement…</p></div>
{/if}

