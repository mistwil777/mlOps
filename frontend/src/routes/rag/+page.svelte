<script lang="ts">
  import { onMount, onDestroy } from 'svelte';

  interface RagIndex {
    project_id: string;
    exists: boolean;
    count: number;
    size_mb: number;
    db_path?: string;
  }

  let indexes: RagIndex[] = $state([]);
  let loading = $state(true);
  let error = $state('');

  // Build job state
  let buildProject = $state('');
  let rebuildFlag = $state(false);
  let buildJobId = $state('');
  let buildLogs: string[] = $state([]);
  let buildStatus = $state<'idle' | 'running' | 'completed' | 'failed'>('idle');
  let es: EventSource | null = null;

  // Search state
  let searchProject = $state('');
  let searchQuery = $state('');
  let searchK = $state(5);
  let searchResults: Array<{ content: string; domain: string; category: string; quality_label: string }> = $state([]);
  let searching = $state(false);
  let searchError = $state('');

  onMount(async () => {
    await loadIndexes();
  });

  onDestroy(() => es?.close());

  async function loadIndexes() {
    loading = true;
    try {
      const r = await fetch('/api/rag');
      if (!r.ok) throw new Error(`${r.status}`);
      indexes = await r.json();
    } catch (e) {
      error = (e as Error).message;
    } finally {
      loading = false;
    }
  }

  async function handleBuild() {
    if (!buildProject) return;
    buildLogs = [];
    buildStatus = 'running';
    error = '';

    try {
      const r = await fetch(`/api/rag/${buildProject}/build?rebuild=${rebuildFlag}`, { method: 'POST' });
      if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
      const d = await r.json();
      buildJobId = d.job_id;
      openBuildSSE();
    } catch (e) {
      error = (e as Error).message;
      buildStatus = 'failed';
    }
  }

  function openBuildSSE() {
    es?.close();
    es = new EventSource(`/api/rag/build/${buildJobId}/stream`);
    es.addEventListener('log', (e) => {
      buildLogs = [...buildLogs, JSON.parse(e.data).line];
      scrollBuild();
    });
    es.addEventListener('done', (e) => {
      buildStatus = JSON.parse(e.data).status;
      es?.close();
      if (buildStatus === 'completed') loadIndexes();
    });
    es.onerror = () => { buildStatus = 'failed'; es?.close(); };
  }

  function scrollBuild() {
    setTimeout(() => {
      const el = document.getElementById('rag-build-log');
      if (el) el.scrollTop = el.scrollHeight;
    }, 0);
  }

  async function handleDelete(projectId: string) {
    if (!confirm(`Supprimer l'index RAG du projet « ${projectId} » ?`)) return;
    try {
      const r = await fetch(`/api/rag/${projectId}`, { method: 'DELETE' });
      if (!r.ok) throw new Error(`${r.status}`);
      await loadIndexes();
    } catch (e) {
      error = (e as Error).message;
    }
  }

  async function handleSearch() {
    if (!searchProject || !searchQuery.trim()) return;
    searching = true;
    searchError = '';
    searchResults = [];
    try {
      const r = await fetch(`/api/rag/${searchProject}/search`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: searchQuery, k: searchK }),
      });
      if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
      const d = await r.json();
      searchResults = d.results;
    } catch (e) {
      searchError = (e as Error).message;
    } finally {
      searching = false;
    }
  }

  $effect(() => {
    if (indexes.length > 0 && !buildProject) buildProject = indexes[0].project_id;
    if (indexes.length > 0 && !searchProject) searchProject = indexes[0].project_id;
  });
</script>

<div class="topbar">
  <span class="topbar-title">Gestion RAG</span>
  <button class="btn btn-ghost" onclick={loadIndexes} style="font-size:12px">↻ Actualiser</button>
</div>

<div class="content">
  {#if error}
    <div class="banner-error" style="margin-bottom:16px">{error}</div>
  {/if}

  <!-- Index list -->
  <div class="card section" style="margin-bottom:20px">
    <div class="section-title">Index RAG actifs</div>
    {#if loading}
      <p class="text-muted">Chargement…</p>
    {:else if indexes.length === 0}
      <p class="text-muted">Aucun index RAG trouvé. Construisez-en un ci-dessous.</p>
    {:else}
      <table class="rag-table">
        <thead>
          <tr><th>Projet</th><th>Patterns</th><th>Taille</th><th>Chemin</th><th></th></tr>
        </thead>
        <tbody>
          {#each indexes as idx}
            <tr>
              <td class="mono">{idx.project_id}</td>
              <td class="mono">{idx.count.toLocaleString()}</td>
              <td class="mono">{idx.size_mb} MB</td>
              <td class="mono text-muted" style="font-size:11px">{idx.db_path ?? '—'}</td>
              <td>
                <button class="btn btn-ghost" style="color:var(--red); font-size:12px" onclick={() => handleDelete(idx.project_id)}>✕ Supprimer</button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    {/if}
  </div>

  <div class="two-cols">
    <!-- Build panel -->
    <div class="card section">
      <div class="section-title">Construire / reconstruire un index</div>
      <div class="form-col">
        <label class="form-field">
          <span class="field-label">Projet</span>
          <input bind:value={buildProject} class="input" placeholder="ex: migrator" />
        </label>
        <label class="form-field toggle-row">
          <span class="field-label">Forcer la reconstruction</span>
          <input type="checkbox" bind:checked={rebuildFlag} />
        </label>
        <button class="btn btn-primary" onclick={handleBuild} disabled={buildStatus === 'running' || !buildProject}>
          {buildStatus === 'running' ? '⟳ Construction…' : '▶ Construire'}
        </button>
        {#if buildStatus === 'completed'}
          <span class="badge badge-completed">✓ Terminé</span>
        {:else if buildStatus === 'failed'}
          <span class="badge" style="background:rgba(255,80,80,0.15); color:var(--red); border:1px solid var(--red); padding:4px 10px; border-radius:4px; font-size:12px">✗ Échoué</span>
        {/if}
      </div>

      {#if buildLogs.length > 0}
        <div id="rag-build-log" class="log-box" style="margin-top:12px">
          {#each buildLogs as line}
            <div class="log-line">{line}</div>
          {/each}
        </div>
      {/if}
    </div>

    <!-- Search panel -->
    <div class="card section">
      <div class="section-title">Tester la recherche sémantique</div>
      <div class="form-col">
        <label class="form-field">
          <span class="field-label">Projet (index)</span>
          <select bind:value={searchProject} class="input">
            {#each indexes as idx}
              <option value={idx.project_id}>{idx.project_id} ({idx.count} patterns)</option>
            {/each}
          </select>
        </label>
        <label class="form-field">
          <span class="field-label">Requête</span>
          <textarea bind:value={searchQuery} class="input" rows="3" placeholder="Collez un extrait de code ou une question…" style="resize:vertical; font-family:var(--font-mono); font-size:12px"></textarea>
        </label>
        <label class="form-field">
          <span class="field-label">Résultats (k)</span>
          <input type="number" bind:value={searchK} min="1" max="20" class="input" style="width:80px" />
        </label>
        <button class="btn btn-primary" onclick={handleSearch} disabled={searching || !searchQuery.trim()}>
          {searching ? '⟳ Recherche…' : '🔍 Rechercher'}
        </button>
      </div>

      {#if searchError}
        <div class="banner-error" style="margin-top:10px">{searchError}</div>
      {/if}

      {#if searchResults.length > 0}
        <div class="search-results" style="margin-top:12px">
          {#each searchResults as res, i}
            <div class="result-card">
              <div class="result-header">
                <span class="badge badge-completed" style="font-size:11px">{res.domain || '—'}</span>
                <span class="text-muted" style="font-size:11px">{res.category || ''}</span>
                {#if res.quality_label}
                  <span class="text-muted" style="font-size:11px">· {res.quality_label}</span>
                {/if}
              </div>
              <pre class="result-content">{res.content.slice(0, 400)}{res.content.length > 400 ? '…' : ''}</pre>
            </div>
          {/each}
        </div>
      {/if}
    </div>
  </div>
</div>

<style>
  .section { padding: 20px; }
  .section-title { font-size: 12px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 14px; }
  .rag-table { width: 100%; border-collapse: collapse; font-size: 13px; }
  .rag-table th { text-align: left; padding: 6px 12px; color: var(--text-muted); font-weight: 600; border-bottom: 1px solid var(--border); }
  .rag-table td { padding: 8px 12px; border-bottom: 1px solid var(--surface-2); }
  .two-cols { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .form-col { display: flex; flex-direction: column; gap: 12px; }
  .form-field { display: flex; flex-direction: column; gap: 5px; }
  .toggle-row { flex-direction: row; align-items: center; gap: 10px; }
  .field-label { font-size: 12px; color: var(--text-muted); }
  .input { background: var(--surface-2); color: var(--text); border: 1px solid var(--border); border-radius: var(--radius); padding: 6px 10px; font-size: 13px; width: 100%; }
  .log-box { background: var(--surface-2); border-radius: var(--radius); padding: 10px; height: 160px; overflow-y: auto; font-family: var(--font-mono); font-size: 12px; display: flex; flex-direction: column; gap: 2px; }
  .log-line { white-space: pre-wrap; color: var(--text-muted); }
  .search-results { display: flex; flex-direction: column; gap: 8px; max-height: 400px; overflow-y: auto; }
  .result-card { padding: 10px; background: var(--surface-2); border-radius: var(--radius); display: flex; flex-direction: column; gap: 6px; }
  .result-header { display: flex; align-items: center; gap: 8px; }
  .result-content { font-family: var(--font-mono); font-size: 11px; white-space: pre-wrap; word-break: break-all; margin: 0; }
</style>
