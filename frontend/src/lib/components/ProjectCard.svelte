<script lang="ts">
  import type { Project, NextActions } from '$lib/types';
  import { goto } from '$app/navigation';
  import { getNextActions, deleteProject } from '$lib/api';
  import PipelineStepper from './PipelineStepper.svelte';
  import ActionBar from './ActionBar.svelte';

  interface Props {
    project: Project;
    onDelete?: (id: string) => void;
  }

  let { project, onDelete }: Props = $props();

  let nextActions = $state<NextActions | null>(null);
  let expanded = $state(false);
  let confirmDelete = $state(false);
  let deleting = $state(false);

  async function handleDelete(e: MouseEvent) {
    e.stopPropagation();
    if (!confirmDelete) { confirmDelete = true; return; }
    deleting = true;
    try {
      await deleteProject(project.id);
      onDelete?.(project.id);
    } catch {
      deleting = false;
      confirmDelete = false;
    }
  }

  function cancelDelete(e: MouseEvent) {
    e.stopPropagation();
    confirmDelete = false;
  }

  $effect(() => {
    if (expanded && !nextActions) {
      getNextActions(project.id).then(a => { nextActions = a; }).catch(() => {});
    }
  });

  const phase = $derived<0 | 1 | 2 | 3 | 4>(
    nextActions?.pipeline_phase ?? inferPhase(project)
  );

  function inferPhase(p: Project): 0 | 1 | 2 | 3 | 4 {
    if (!p.n_runs) return p.model ? 1 : 0;
    if (p.last_status === 'completed') return 3;
    return 2;
  }

  const classifColor: Record<string, string> = {
    public:       'var(--green)',
    interne:      'var(--text-muted)',
    confidentiel: 'var(--yellow)',
    secret:       'var(--red)',
  };
</script>

<div
  class="project-card"
  class:expanded
>
  <!-- L1 -->
  <div
    class="l1"
    onclick={() => expanded = !expanded}
    role="button"
    tabindex="0"
    onkeydown={e => e.key === 'Enter' && (expanded = !expanded)}
  >
    <div class="l1-left">
      <span class="chevron">{expanded ? '▾' : '▸'}</span>
      <div class="proj-info">
        <span class="proj-name">{project.name ?? project.id}</span>
        <span class="proj-domain">{project.domain}</span>
      </div>
    </div>
    <div class="l1-right" onclick={e => e.stopPropagation()} role="presentation">
      {#if project.classification}
        <span
          class="classif-badge"
          style="color:{classifColor[project.classification] ?? 'var(--text-muted)'}"
        >{project.classification}</span>
      {/if}
      <PipelineStepper {phase} compact />
      <button
        class="btn btn-ghost btn-sm"
        onclick={() => goto(`/projects/${project.id}`)}
      >Ouvrir</button>

      {#if confirmDelete}
        <button class="btn btn-danger btn-sm" disabled={deleting} onclick={handleDelete}>
          {deleting ? '…' : 'Confirmer'}
        </button>
        <button class="btn btn-ghost btn-sm" onclick={cancelDelete}>Annuler</button>
      {:else}
        <button class="trash-btn" title="Supprimer le projet" onclick={handleDelete}>✕ Supprimer</button>
      {/if}
    </div>
  </div>

  <!-- L2 -->
  {#if expanded}
    <div class="l2">
      {#if project.description}
        <p class="proj-desc">{project.description}</p>
      {/if}
      <div class="proj-meta">
        <span><span class="meta-k">Modèle</span> {project.model}</span>
        <span><span class="meta-k">Runs</span> {project.n_runs ?? 0}</span>
        {#if project.last_run}
          <span><span class="meta-k">Dernier run</span> {project.last_run.slice(0, 10)}</span>
        {/if}
      </div>
      <div class="stepper-full mt-12">
        <PipelineStepper {phase} />
      </div>
      {#if nextActions?.actions?.length}
        <div class="mt-12">
          <ActionBar actions={nextActions.actions} />
        </div>
      {/if}
    </div>
  {/if}
</div>

<style>
  .project-card {
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    overflow: hidden;
    transition: border-color 150ms;
  }

  .project-card.expanded { border-color: var(--accent); }

  .l1 {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 10px 14px;
    cursor: pointer;
    user-select: none;
    gap: 12px;
    background: var(--surface);
  }

  .l1:hover { background: var(--surface-2); }

  .l1-left  { display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1; }
  .l1-right { display: flex; align-items: center; gap: 10px; flex-shrink: 0; }

  .chevron { font-size: 11px; color: var(--text-dim); width: 12px; flex-shrink: 0; }

  .proj-info { display: flex; flex-direction: column; min-width: 0; }
  .proj-name { font-weight: 600; font-size: var(--text-base); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .proj-domain { font-size: var(--text-xs); color: var(--text-muted); }

  .classif-badge {
    font-size: var(--text-xs);
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .06em;
  }

  .btn-sm { padding: 4px 10px; font-size: var(--text-sm); font-weight: 500; }

  .trash-btn {
    font-size: 12px;
    opacity: 1;
    background: none;
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 3px 8px;
    cursor: pointer;
    color: var(--text-muted);
    transition: border-color 150ms, color 150ms;
  }
  .trash-btn:hover { border-color: var(--red); color: var(--red); }

  .l2 {
    padding: 12px 14px 14px;
    border-top: 1px solid var(--border);
    background: var(--surface);
    display: flex;
    flex-direction: column;
  }

  .proj-desc {
    font-size: var(--text-sm);
    color: var(--text-muted);
    line-height: 1.5;
    margin-bottom: 8px;
  }

  .proj-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    font-size: var(--text-sm);
    color: var(--text-muted);
  }

  .meta-k { color: var(--text-dim); margin-right: 3px; font-size: var(--text-xs); text-transform: uppercase; letter-spacing: .06em; }

  .stepper-full { width: 100%; }

  .mt-12 { margin-top: 12px; }
</style>
