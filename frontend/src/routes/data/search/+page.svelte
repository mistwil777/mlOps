<script lang="ts">
  import { onMount } from 'svelte';
  import { getProjects, searchDatalakePairs, indexProjectPairs } from '$lib/api';
  import type { DatalakePair } from '$lib/api';
  import type { Project } from '$lib/types';

  const PAGE_SIZE = 20;

  let projects: Project[] = $state([]);
  let query          = $state('');
  let filterProject  = $state('');
  let filterStage    = $state('');
  let page           = $state(0);
  let items: DatalakePair[] = $state([]);
  let total          = $state(0);
  let loading        = $state(false);
  let error          = $state('');
  let indexing       = $state(false);
  let indexMsg       = $state('');
  let debounceTimer: ReturnType<typeof setTimeout> | null = null;

  // Modal
  let modalPair: DatalakePair | null = $state(null);

  onMount(async () => {
    projects = await getProjects().catch(() => []);
  });

  function triggerSearch() {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => { page = 0; doSearch(); }, 400);
  }

  async function doSearch() {
    if (!query.trim()) { items = []; total = 0; return; }
    loading = true; error = '';
    try {
      const res = await searchDatalakePairs({
        query: query.trim(),
        project_id: filterProject || undefined,
        stage: filterStage || undefined,
        k: PAGE_SIZE,
        page,
      });
      items = res.items;
      total = res.total;
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  }

  async function reindex() {
    if (!filterProject) { indexMsg = 'Sélectionnez un projet pour ré-indexer.'; return; }
    indexing = true; indexMsg = '';
    try {
      await indexProjectPairs(filterProject);
      indexMsg = `Indexation de ${filterProject} lancée en arrière-plan.`;
    } catch (e) {
      indexMsg = (e as Error).message;
    } finally {
      indexing = false;
    }
  }

  const totalPages = $derived(Math.max(1, Math.ceil(total / PAGE_SIZE)));

  function prevPage() { if (page > 0) { page--; doSearch(); } }
  function nextPage() { if (page < totalPages - 1) { page++; doSearch(); } }

  function formatDate(iso: string | undefined): string {
    if (!iso) return '';
    try {
      const d = new Date(iso);
      const now = Date.now();
      const months = Math.round((now - d.getTime()) / (1000 * 60 * 60 * 24 * 30));
      if (months < 1) return 'récent';
      if (months < 12) return `${months} mois`;
      return `${Math.round(months / 12)} an${months >= 24 ? 's' : ''}`;
    } catch { return ''; }
  }

  function freshnessColor(iso: string | undefined): string {
    if (!iso) return 'var(--text-dim)';
    const months = (Date.now() - new Date(iso).getTime()) / (1000 * 60 * 60 * 24 * 30);
    if (months > 18) return 'var(--red)';
    if (months > 6)  return 'var(--yellow)';
    return 'var(--green)';
  }

  function scoreColor(s: number | undefined): string {
    if (s == null) return 'var(--text-dim)';
    if (s >= 0.7) return 'var(--green)';
    if (s >= 0.4) return 'var(--yellow)';
    return 'var(--red)';
  }
</script>

<div class="topbar">
  <span class="topbar-title">Recherche datalake</span>
</div>

<div class="content">
  <div class="search-layout">

    <!-- Col gauche : filtres + stats -->
    <div class="search-left">
      <div class="card mb-16">
        <div class="card-title mb-10">Filtres</div>

        <div class="form-field mb-10">
          <label class="field-label" for="sq">Requête sémantique</label>
          <input
            id="sq"
            class="input"
            bind:value={query}
            oninput={triggerSearch}
            placeholder="ex: réglementation légale employeur"
          />
        </div>

        <div class="form-field mb-10">
          <label class="field-label" for="fp">Projet</label>
          <select id="fp" class="input" bind:value={filterProject} onchange={() => { page = 0; doSearch(); }}>
            <option value="">Tous les projets</option>
            {#each projects as p}
              <option value={p.id}>{p.name ?? p.id}</option>
            {/each}
          </select>
        </div>

        <div class="form-field mb-12">
          <label class="field-label" for="fs">Stage</label>
          <select id="fs" class="input" bind:value={filterStage} onchange={() => { page = 0; doSearch(); }}>
            <option value="">Tous les stages</option>
            <option value="fine-tuning-ready">fine-tuning-ready</option>
            <option value="raw">raw</option>
          </select>
        </div>

        <button
          class="btn btn-ghost btn-sm"
          style="width:100%"
          disabled={indexing || !filterProject}
          onclick={reindex}
          title="Ré-indexer les paires du projet sélectionné dans Qdrant"
        >
          {#if indexing}<span class="spinner spinner-xs"></span> Indexation…{:else}↻ Ré-indexer{/if}
        </button>
        {#if indexMsg}
          <div class="hint mt-6" style="color:var(--text-muted)">{indexMsg}</div>
        {/if}
      </div>

      {#if total > 0}
        <div class="card">
          <div class="card-title mb-8">Résultats</div>
          <div class="stat-big">{total}</div>
          <div class="hint">paires correspondantes</div>
          {#if page > 0 || total > PAGE_SIZE}
            <div class="hint mt-4">Page {page + 1} / {totalPages}</div>
          {/if}
        </div>
      {/if}
    </div>

    <!-- Col droite : résultats -->
    <div class="search-right">
      {#if !query.trim()}
        <div class="search-placeholder">
          <div class="sp-icon">⌕</div>
          <div class="sp-title">Recherche sémantique dans le datalake</div>
          <div class="sp-sub">
            Tapez une requête en langage naturel pour trouver des paires d'entraînement pertinentes
            dans tous les projets. Utilise un index vectoriel hybride (sémantique + mots-clés).
          </div>
        </div>

      {:else if loading}
        <div class="search-loading">
          <span class="spinner"></span>
          <span class="text-muted" style="font-size:13px">Recherche en cours…</span>
        </div>

      {:else if error}
        <div class="banner-error">{error}</div>

      {:else if items.length === 0}
        <div class="search-placeholder">
          <div class="sp-icon">◎</div>
          <div class="sp-title">Aucun résultat</div>
          <div class="sp-sub">Aucune paire ne correspond à cette requête. Essayez d'autres termes ou lancez une collecte.</div>
        </div>

      {:else}
        <div class="results-list">
          {#each items as pair}
            <div class="result-card card">
              <!-- L1 : header — badges + actions -->
              <div class="rc-header">
                <div class="rc-badges">
                  <span class="badge badge-project">{pair.project_id}</span>
                  <span class="badge badge-stage">{pair.stage}</span>
                  {#if pair.quality_score != null}
                    <span class="badge" style="background:rgba(0,0,0,.1); color:{scoreColor(pair.quality_score)}">
                      ◉ {(pair.quality_score * 20).toFixed(0)}/20
                    </span>
                  {/if}
                  {#if pair.collected_at}
                    <span class="badge" style="color:{freshnessColor(pair.collected_at)}">
                      ◷ {formatDate(pair.collected_at)}
                    </span>
                  {/if}
                </div>
                <button class="btn btn-ghost btn-xs" onclick={() => modalPair = pair}>
                  Voir complet →
                </button>
              </div>

              <!-- L1 : aperçu prompt + completion -->
              <div class="rc-cols">
                <div class="rc-col">
                  <div class="col-label">Prompt</div>
                  <pre class="rc-text">{pair.prompt.slice(0, 200)}{pair.prompt.length > 200 ? '…' : ''}</pre>
                </div>
                <div class="rc-col">
                  <div class="col-label">Completion</div>
                  <pre class="rc-text">{pair.completion.slice(0, 200)}{pair.completion.length > 200 ? '…' : ''}</pre>
                </div>
              </div>

              {#if pair.source_url}
                <div class="rc-footer">
                  <a href={pair.source_url} target="_blank" rel="noopener noreferrer" class="rc-source">
                    {pair.source_url.slice(0, 80)}{pair.source_url.length > 80 ? '…' : ''} ↗
                  </a>
                </div>
              {/if}
            </div>
          {/each}
        </div>

        <!-- Pagination -->
        {#if totalPages > 1}
          <div class="pagination mt-16">
            <button class="btn btn-ghost btn-sm" disabled={page === 0} onclick={prevPage}>← Préc.</button>
            <span class="text-muted" style="font-size:12px">Page {page + 1} / {totalPages}</span>
            <button class="btn btn-ghost btn-sm" disabled={page >= totalPages - 1} onclick={nextPage}>Suiv. →</button>
          </div>
        {/if}
      {/if}
    </div>

  </div>
</div>

<!-- Modal détail L2 -->
{#if modalPair}
  <div class="modal-backdrop" role="presentation" onclick={() => modalPair = null}>
    <div class="modal" role="dialog" aria-modal="true" onclick={(e) => e.stopPropagation()}>
      <div class="modal-header">
        <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap">
          <span class="badge badge-project">{modalPair.project_id}</span>
          <span class="badge badge-stage">{modalPair.stage}</span>
          {#if modalPair.quality_score != null}
            <span class="badge" style="color:{scoreColor(modalPair.quality_score)}">◉ {(modalPair.quality_score * 20).toFixed(0)}/20</span>
          {/if}
          {#if modalPair.collected_at}
            <span class="badge" style="color:{freshnessColor(modalPair.collected_at)}">◷ {formatDate(modalPair.collected_at)}</span>
          {/if}
        </div>
        <button class="icon-btn" onclick={() => modalPair = null}>✕</button>
      </div>

      <div class="modal-cols">
        <div class="modal-col">
          <div class="col-label mb-6">Prompt</div>
          <pre class="modal-text">{modalPair.prompt}</pre>
        </div>
        <div class="modal-col">
          <div class="col-label mb-6">Completion</div>
          <pre class="modal-text">{modalPair.completion}</pre>
        </div>
      </div>

      {#if modalPair.source_url}
        <div class="modal-footer">
          <a href={modalPair.source_url} target="_blank" rel="noopener noreferrer" class="rc-source">
            Source : {modalPair.source_url} ↗
          </a>
        </div>
      {/if}
    </div>
  </div>
{/if}

<style>
  .search-layout {
    display: grid;
    grid-template-columns: 260px 1fr;
    gap: 20px;
    align-items: start;
  }
  @media (max-width: 860px) { .search-layout { grid-template-columns: 1fr; } }

  .search-left  { display: flex; flex-direction: column; }
  .search-right { display: flex; flex-direction: column; }

  .card-title { font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: .07em; color: var(--text-dim); }
  .mb-6  { margin-bottom: 6px; }
  .mb-8  { margin-bottom: 8px; }
  .mb-10 { margin-bottom: 10px; }
  .mb-12 { margin-bottom: 12px; }
  .mb-16 { margin-bottom: 16px; }
  .mt-4  { margin-top: 4px; }
  .mt-6  { margin-top: 6px; }
  .mt-16 { margin-top: 16px; }

  .form-field { display: flex; flex-direction: column; gap: 4px; }
  .field-label { font-size: 11px; color: var(--text-dim); }
  .input {
    background: var(--surface-2); color: var(--text); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 6px 10px; font-size: 13px; width: 100%;
  }
  .input:focus { outline: none; border-color: var(--accent); }

  .stat-big { font-size: 28px; font-weight: 800; color: var(--accent); line-height: 1; }
  .hint { font-size: 11px; color: var(--text-dim); line-height: 1.5; }

  /* Placeholder / empty states */
  .search-placeholder, .search-loading {
    display: flex; flex-direction: column; align-items: center;
    gap: 10px; padding: 48px 24px; text-align: center;
  }
  .search-loading { flex-direction: row; justify-content: center; }
  .sp-icon  { font-size: 32px; color: var(--text-dim); }
  .sp-title { font-size: 15px; font-weight: 600; color: var(--text-muted); }
  .sp-sub   { font-size: 12px; color: var(--text-dim); max-width: 380px; line-height: 1.6; }

  /* Results */
  .results-list { display: flex; flex-direction: column; gap: 10px; }

  .result-card { display: flex; flex-direction: column; gap: 8px; }
  .rc-header {
    display: flex; align-items: center; justify-content: space-between; gap: 8px; flex-wrap: wrap;
  }
  .rc-badges { display: flex; align-items: center; gap: 6px; flex-wrap: wrap; }
  .badge {
    font-size: 10px; font-weight: 600; padding: 2px 7px;
    border-radius: 999px; background: var(--surface-2); color: var(--text-dim);
  }
  .badge-project { background: var(--accent-dim); color: var(--accent); }
  .badge-stage   { background: var(--surface-3, var(--border)); color: var(--text-dim); }

  .rc-cols {
    display: grid; grid-template-columns: 1fr 1fr; gap: 10px;
  }
  @media (max-width: 700px) { .rc-cols { grid-template-columns: 1fr; } }

  .rc-col { display: flex; flex-direction: column; gap: 4px; }
  .col-label { font-size: 10px; font-weight: 700; text-transform: uppercase; letter-spacing: .06em; color: var(--text-dim); }
  .rc-text {
    font-family: var(--font-mono); font-size: 11px; color: var(--text-muted);
    white-space: pre-wrap; word-break: break-word; margin: 0;
    background: var(--surface-2); border-radius: var(--radius); padding: 6px 8px;
    max-height: 120px; overflow: hidden;
  }
  .rc-footer { padding-top: 4px; border-top: 1px solid var(--border); }
  .rc-source { font-size: 10px; color: var(--text-dim); text-decoration: none; }
  .rc-source:hover { color: var(--accent); }

  .btn-xs { padding: 3px 8px; font-size: 11px; flex-shrink: 0; }

  /* Pagination */
  .pagination { display: flex; align-items: center; justify-content: center; gap: 12px; }

  /* Modal */
  .modal-backdrop {
    position: fixed; inset: 0; background: rgba(0,0,0,.6);
    display: flex; align-items: center; justify-content: center;
    z-index: 1000; padding: 16px;
  }
  .modal {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius-lg, 12px);
    width: 100%; max-width: 900px; max-height: 85vh;
    display: flex; flex-direction: column; overflow: hidden;
  }
  .modal-header {
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    padding: 14px 18px; border-bottom: 1px solid var(--border); flex-shrink: 0;
  }
  .modal-cols {
    display: grid; grid-template-columns: 1fr 1fr; gap: 12px;
    padding: 16px 18px; overflow: auto; flex: 1;
  }
  @media (max-width: 600px) { .modal-cols { grid-template-columns: 1fr; } }

  .modal-col { display: flex; flex-direction: column; }
  .modal-text {
    font-family: var(--font-mono); font-size: 11px; color: var(--text-muted);
    white-space: pre-wrap; word-break: break-word; margin: 0;
    background: var(--surface-2); border-radius: var(--radius); padding: 10px 12px;
    flex: 1; overflow: auto; max-height: 50vh;
  }
  .modal-footer {
    padding: 10px 18px; border-top: 1px solid var(--border); flex-shrink: 0;
  }

  /* Spinner (réutilisé du design system) */
  .spinner {
    display: inline-block; width: 12px; height: 12px;
    border: 2px solid currentColor; border-top-color: transparent;
    border-radius: 50%; animation: spin 600ms linear infinite; vertical-align: middle;
  }
  .spinner-xs { width: 10px; height: 10px; }
  @keyframes spin { to { transform: rotate(360deg); } }

  .banner-error {
    background: var(--red-dim); border: 1px solid rgba(247,84,84,.3);
    border-radius: var(--radius); padding: 10px 14px;
    font-size: var(--text-sm); color: var(--red);
  }
</style>
