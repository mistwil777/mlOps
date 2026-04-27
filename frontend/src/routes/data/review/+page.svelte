<script lang="ts">
  import { onMount } from 'svelte';
  import { getProjects, getReview, postReview, getReviewStats } from '$lib/api';
  import type { Project, ReviewItem, ReviewStats } from '$lib/types';

  let projects: Project[] = $state([]);
  let selectedProject = $state('');
  let filterStatus = $state<string>('pending');
  let page = $state(0);
  const PAGE_SIZE = 10;

  let items: ReviewItem[] = $state([]);
  let total = $state(0);
  let stats: ReviewStats | null = $state(null);
  let loading = $state(false);
  let error = $state('');

  // Correction modal state
  let editIdx = $state<number | null>(null);
  let editPrompt = $state('');
  let editCompletion = $state('');

  onMount(async () => {
    projects = await getProjects().catch(() => []);
    if (projects.length > 0) {
      selectedProject = projects[0].id;
      await load();
    }
  });

  async function load() {
    if (!selectedProject) return;
    loading = true;
    error = '';
    try {
      const [rev, st] = await Promise.all([
        getReview(selectedProject, page, PAGE_SIZE, filterStatus || undefined),
        getReviewStats(selectedProject),
      ]);
      items = rev.items;
      total = rev.total;
      stats = st;
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  }

  async function act(globalIdx: number, action: 'validate' | 'reject') {
    try {
      await postReview(selectedProject, { index: globalIdx, action });
      await load();
    } catch (e) {
      error = (e as Error).message;
    }
  }

  function openEdit(item: ReviewItem, globalIdx: number) {
    editIdx = globalIdx;
    editPrompt = item.prompt;
    editCompletion = item.completion;
  }

  async function saveEdit() {
    if (editIdx === null) return;
    try {
      await postReview(selectedProject, {
        index: editIdx,
        action: 'correct',
        corrected_prompt: editPrompt,
        corrected_completion: editCompletion,
      });
      editIdx = null;
      await load();
    } catch (e) {
      error = (e as Error).message;
    }
  }

  function globalIndex(localIdx: number) {
    return page * PAGE_SIZE + localIdx;
  }

  function statusColor(status?: string) {
    if (status === 'validate') return 'var(--green)';
    if (status === 'reject')   return 'var(--red)';
    if (status === 'correct')  return 'var(--accent)';
    return 'var(--text-muted)';
  }

  $effect(() => { load(); });
</script>

<div class="topbar">
  <span class="topbar-title">Revue humaine</span>
  <div style="display:flex; gap:10px; align-items:center">
    <select bind:value={selectedProject} class="input">
      {#each projects as p}
        <option value={p.id}>{p.name}</option>
      {/each}
    </select>
    <select bind:value={filterStatus} class="input">
      <option value="">Tous</option>
      <option value="pending">En attente</option>
      <option value="validated">Validés</option>
      <option value="rejected">Rejetés</option>
    </select>
  </div>
</div>

<div class="content">
  {#if error}
    <div class="banner-error" style="margin-bottom:16px">{error}</div>
  {/if}

  {#if stats}
    <div class="stats-bar card" style="margin-bottom:16px; padding:12px 20px">
      <div class="stat-item"><span class="stat-label">Total</span><span class="mono">{stats.total}</span></div>
      <div class="stat-item"><span class="stat-label">En attente</span><span class="mono text-muted">{stats.pending}</span></div>
      <div class="stat-item"><span class="stat-label">Validés</span><span class="mono" style="color:var(--green)">{stats.validated}</span></div>
      <div class="stat-item"><span class="stat-label">Rejetés</span><span class="mono" style="color:var(--red)">{stats.rejected}</span></div>
      <div class="stat-item"><span class="stat-label">Corrigés</span><span class="mono" style="color:var(--accent)">{stats.corrected}</span></div>
    </div>
  {/if}

  {#if loading}
    <p class="text-muted">Chargement…</p>
  {:else if items.length === 0}
    <p class="text-muted">Aucune paire à réviser pour ce filtre.</p>
  {:else}
    <div class="pairs-list">
      {#each items as item, i}
        {@const gi = globalIndex(i)}
        <div class="pair-card card" style="border-left: 3px solid {statusColor(item.review_status)}">
          <div class="pair-header">
            <span class="mono text-muted" style="font-size:11px">#{gi}</span>
            {#if item.quality_score !== undefined}
              <span class="badge badge-completed" style="font-size:11px">score: {(item.quality_score * 100).toFixed(0)}%</span>
            {/if}
            {#if item.review_status}
              <span class="review-badge" style="color:{statusColor(item.review_status)}">{item.review_status}</span>
            {/if}
          </div>

          <div class="pair-cols">
            <div class="pair-col">
              <div class="col-label">Prompt</div>
              <pre class="pair-text">{item.prompt.slice(0, 600)}{item.prompt.length > 600 ? '…' : ''}</pre>
            </div>
            <div class="pair-col">
              <div class="col-label">Completion</div>
              <pre class="pair-text">{item.completion.slice(0, 600)}{item.completion.length > 600 ? '…' : ''}</pre>
            </div>
          </div>

          <div class="pair-actions">
            <button class="btn btn-ghost" style="color:var(--green)" onclick={() => act(gi, 'validate')}>✓ Valider</button>
            <button class="btn btn-ghost" style="color:var(--red)" onclick={() => act(gi, 'reject')}>✗ Rejeter</button>
            <button class="btn btn-ghost" onclick={() => openEdit(item, gi)}>✎ Corriger</button>
          </div>
        </div>
      {/each}
    </div>

    <div class="pagination">
      <button class="btn btn-ghost" disabled={page === 0} onclick={() => { page--; }}>← Préc.</button>
      <span class="text-muted" style="font-size:13px">Page {page + 1} / {Math.ceil(total / PAGE_SIZE)}</span>
      <button class="btn btn-ghost" disabled={(page + 1) * PAGE_SIZE >= total} onclick={() => { page++; }}>Suiv. →</button>
    </div>
  {/if}
</div>

<!-- Correction modal -->
{#if editIdx !== null}
  <div class="modal-backdrop" onclick={() => editIdx = null}>
    <div class="modal" onclick={(e) => e.stopPropagation()}>
      <div class="modal-title">Corriger la paire #{editIdx}</div>
      <label class="form-field">
        <span class="field-label">Prompt</span>
        <textarea bind:value={editPrompt} class="textarea" rows="6"></textarea>
      </label>
      <label class="form-field" style="margin-top:12px">
        <span class="field-label">Completion</span>
        <textarea bind:value={editCompletion} class="textarea" rows="6"></textarea>
      </label>
      <div style="display:flex; gap:8px; margin-top:16px; justify-content:flex-end">
        <button class="btn btn-ghost" onclick={() => editIdx = null}>Annuler</button>
        <button class="btn btn-primary" onclick={saveEdit}>Sauvegarder</button>
      </div>
    </div>
  </div>
{/if}

<style>
  .input { background: var(--surface-2); color: var(--text); border: 1px solid var(--border); border-radius: var(--radius); padding: 4px 10px; font-size: 13px; }
  .stats-bar { display: flex; gap: 24px; flex-wrap: wrap; }
  .stat-item { display: flex; flex-direction: column; gap: 2px; }
  .stat-label { font-size: 11px; color: var(--text-muted); }
  .pairs-list { display: flex; flex-direction: column; gap: 12px; }
  .pair-card { padding: 14px; display: flex; flex-direction: column; gap: 10px; }
  .pair-header { display: flex; align-items: center; gap: 8px; }
  .review-badge { font-size: 11px; font-weight: 600; text-transform: uppercase; }
  .pair-cols { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .pair-col { display: flex; flex-direction: column; gap: 6px; }
  .col-label { font-size: 11px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; }
  .pair-text { font-family: var(--font-mono); font-size: 11px; background: var(--surface-2); padding: 8px; border-radius: 4px; overflow-x: auto; white-space: pre-wrap; word-break: break-all; margin: 0; max-height: 160px; overflow-y: auto; }
  .pair-actions { display: flex; gap: 8px; }
  .pagination { display: flex; align-items: center; gap: 12px; margin-top: 16px; justify-content: center; }
  .form-field { display: flex; flex-direction: column; gap: 6px; }
  .field-label { font-size: 12px; color: var(--text-muted); }
  .textarea { background: var(--surface-2); color: var(--text); border: 1px solid var(--border); border-radius: var(--radius); padding: 8px; font-family: var(--font-mono); font-size: 12px; resize: vertical; width: 100%; }
  .modal-backdrop { position: fixed; inset: 0; background: rgba(0,0,0,0.6); display: flex; align-items: center; justify-content: center; z-index: 100; }
  .modal { background: var(--surface-1); border: 1px solid var(--border); border-radius: var(--radius); padding: 24px; width: 700px; max-width: 95vw; }
  .modal-title { font-size: 16px; font-weight: 700; margin-bottom: 16px; }
</style>
