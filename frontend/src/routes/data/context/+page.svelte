<script lang="ts">
  import { page } from '$app/stores';
  import { onMount } from 'svelte';
  import { req } from '$lib/api';

  interface ContextStats {
    project_id: string;
    rag_chunks: number;
    kg_entities: number;
    kg_relations: number;
    ready: boolean;
  }

  let projectId   = $state('');
  let stats       = $state<ContextStats | null>(null);
  let loading     = $state(false);
  let error       = $state('');

  // Upload state
  let normFile: File | null    = $state(null);
  let logsFile: File | null    = $state(null);
  let normSource               = $state('norm');
  let logsSource               = $state('client_logs');
  let normDrag                 = $state(false);
  let logsDrag                 = $state(false);
  let uploadingNorm            = $state(false);
  let uploadingLogs            = $state(false);
  let normResult               = $state('');
  let logsResult               = $state('');

  onMount(() => {
    const urlPid = new URLSearchParams(window.location.search).get('project');
    if (urlPid) {
      projectId = urlPid;
    } else {
      try {
        const raw = sessionStorage.getItem('mlops_current_project');
        if (raw) projectId = JSON.parse(raw)?.id ?? '';
      } catch {}
    }
    if (projectId) loadStats();
  });

  async function loadStats() {
    if (!projectId) return;
    loading = true;
    error = '';
    try {
      stats = await req<ContextStats>(`/data/ingestion-context/${projectId}/stats`);
    } catch (e: any) {
      error = e.message ?? 'Erreur';
    } finally {
      loading = false;
    }
  }

  async function uploadNorm() {
    if (!normFile || !projectId) return;
    uploadingNorm = true;
    normResult = '';
    const fd = new FormData();
    fd.append('file', normFile);
    fd.append('source_description', normSource);
    try {
      const res = await fetch(`/api/data/ingestion-context/${projectId}/norm`, { method: 'POST', body: fd });
      if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText);
      const data = await res.json();
      normResult = `✓ ${data.text_length.toLocaleString('fr-FR')} caractères indexés — ${data.rag_chunks_total} chunks RAG total`;
      normFile = null;
      await loadStats();
    } catch (e: any) {
      normResult = `Erreur : ${e.message}`;
    } finally {
      uploadingNorm = false;
    }
  }

  async function uploadLogs() {
    if (!logsFile || !projectId) return;
    uploadingLogs = true;
    logsResult = '';
    const fd = new FormData();
    fd.append('file', logsFile);
    fd.append('source_description', logsSource);
    try {
      const res = await fetch(`/api/data/ingestion-context/${projectId}/logs`, { method: 'POST', body: fd });
      if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText);
      const data = await res.json();
      logsResult = `✓ ${data.text_length.toLocaleString('fr-FR')} caractères indexés — ${data.rag_chunks_total} chunks RAG total`;
      logsFile = null;
      await loadStats();
    } catch (e: any) {
      logsResult = `Erreur : ${e.message}`;
    } finally {
      uploadingLogs = false;
    }
  }

  async function clearContext() {
    if (!projectId) return;
    if (!confirm('Supprimer tout le contexte sémantique de ce projet ?')) return;
    await req(`/data/ingestion-context/${projectId}`, { method: 'DELETE' });
    await loadStats();
  }

  function onNormDrop(e: DragEvent) {
    e.preventDefault(); normDrag = false;
    const f = e.dataTransfer?.files?.[0];
    if (f) normFile = f;
  }

  function onLogsDrop(e: DragEvent) {
    e.preventDefault(); logsDrag = false;
    const f = e.dataTransfer?.files?.[0];
    if (f) logsFile = f;
  }
</script>

<div class="ctx-page">
  <div class="ctx-header">
    <div>
      <h1>Contexte sémantique</h1>
      <p class="subtitle">
        Ancrez la génération de paires dans la réalité de votre domaine.<br>
        L'agent ne peut pas ingérer de données sans ce contexte.
      </p>
    </div>
    {#if stats}
      <div class="status-badge" class:ready={stats.ready}>
        {stats.ready ? '✓ Prêt' : '⚠ Contexte vide'}
      </div>
    {/if}
  </div>

  {#if !projectId}
    <div class="empty">Sélectionnez un projet pour accéder au contexte.</div>
  {:else if loading}
    <div class="loading">Chargement…</div>
  {:else if error}
    <div class="error">{error}</div>
  {:else}

    <!-- Stats bar -->
    {#if stats}
      <div class="stats-row">
        <div class="stat">
          <div class="stat-val">{stats.rag_chunks.toLocaleString('fr-FR')}</div>
          <div class="stat-lbl">Chunks RAG</div>
        </div>
        <div class="stat">
          <div class="stat-val">{stats.kg_entities}</div>
          <div class="stat-lbl">Entités KG</div>
        </div>
        <div class="stat">
          <div class="stat-val">{stats.kg_relations}</div>
          <div class="stat-lbl">Relations KG</div>
        </div>
        <button class="btn-clear" onclick={clearContext}>Réinitialiser</button>
      </div>
    {/if}

    <!-- Upload normes -->
    <div class="upload-card">
      <div class="card-title">
        <span class="card-icon">📋</span>
        <div>
          <div class="card-name">Normes & standards</div>
          <div class="card-desc">PDF, DOCX, XLSX — normes sectorielles, réglementations, référentiels métier</div>
        </div>
      </div>

      <div
        class="dropzone"
        class:drag={normDrag}
        ondragover={(e) => { e.preventDefault(); normDrag = true; }}
        ondragleave={() => normDrag = false}
        ondrop={onNormDrop}
        role="button"
        tabindex="0"
      >
        {#if normFile}
          <span class="file-selected">📄 {normFile.name}</span>
        {:else}
          <span>Glissez un fichier ici ou</span>
          <label class="btn-browse">
            Parcourir
            <input type="file" accept=".pdf,.docx,.xlsx,.txt" onchange={(e) => normFile = (e.target as HTMLInputElement).files?.[0] ?? null} />
          </label>
        {/if}
      </div>

      <div class="card-footer">
        <input class="source-input" bind:value={normSource} placeholder="Source (ex: ISO_9001)" />
        <button class="btn-upload" disabled={!normFile || uploadingNorm} onclick={uploadNorm}>
          {uploadingNorm ? 'Indexation…' : '↑ Indexer'}
        </button>
      </div>
      {#if normResult}
        <div class="result" class:err={normResult.startsWith('Erreur')}>{normResult}</div>
      {/if}
    </div>

    <!-- Upload logs/feedback -->
    <div class="upload-card">
      <div class="card-title">
        <span class="card-icon">📊</span>
        <div>
          <div class="card-name">Logs & retours terrain</div>
          <div class="card-desc">Historique de feedback client, logs d'exploitation, cas d'usage réels</div>
        </div>
      </div>

      <div
        class="dropzone"
        class:drag={logsDrag}
        ondragover={(e) => { e.preventDefault(); logsDrag = true; }}
        ondragleave={() => logsDrag = false}
        ondrop={onLogsDrop}
        role="button"
        tabindex="0"
      >
        {#if logsFile}
          <span class="file-selected">📄 {logsFile.name}</span>
        {:else}
          <span>Glissez un fichier ici ou</span>
          <label class="btn-browse">
            Parcourir
            <input type="file" accept=".txt,.csv,.log,.jsonl" onchange={(e) => logsFile = (e.target as HTMLInputElement).files?.[0] ?? null} />
          </label>
        {/if}
      </div>

      <div class="card-footer">
        <input class="source-input" bind:value={logsSource} placeholder="Source (ex: prod_logs_2024)" />
        <button class="btn-upload" disabled={!logsFile || uploadingLogs} onclick={uploadLogs}>
          {uploadingLogs ? 'Indexation…' : '↑ Indexer'}
        </button>
      </div>
      {#if logsResult}
        <div class="result" class:err={logsResult.startsWith('Erreur')}>{logsResult}</div>
      {/if}
    </div>

    <!-- Ontologie CDC -->
    <div class="info-card">
      <div class="info-icon">⚙</div>
      <div>
        <div class="info-title">Ontologie (CDC)</div>
        <div class="info-body">
          Les entités, relations et contraintes métier se déclarent dans la section
          <code>ontology:</code> du fichier de configuration du projet.
          Elles sont automatiquement chargées dans le Knowledge Graph au démarrage de l'agent.
        </div>
      </div>
    </div>

  {/if}
</div>

<style>
  .ctx-page { padding: 32px; max-width: 760px; }

  .ctx-header {
    display: flex; align-items: flex-start; justify-content: space-between;
    margin-bottom: 28px;
  }
  h1 { font-size: 20px; font-weight: 700; color: var(--ink); margin: 0 0 6px; }
  .subtitle { font-size: 13px; color: var(--text-muted); line-height: 1.5; margin: 0; }

  .status-badge {
    padding: 6px 14px; border-radius: 999px;
    font-size: 12px; font-weight: 600;
    background: #FEF3C7; color: #92400E;
    flex-shrink: 0;
  }
  .status-badge.ready { background: #D1FAE5; color: #065F46; }

  .stats-row {
    display: flex; align-items: center; gap: 24px;
    background: #F8FAFC; border: 1px solid #E2E8F0; border-radius: 10px;
    padding: 16px 20px; margin-bottom: 20px;
  }
  .stat { text-align: center; flex: 1; }
  .stat-val { font-size: 22px; font-weight: 700; color: var(--ink); }
  .stat-lbl { font-size: 11px; color: var(--text-muted); margin-top: 2px; }

  .btn-clear {
    margin-left: auto; padding: 6px 12px;
    font-size: 12px; color: #EF4444;
    background: none; border: 1px solid #FCA5A5;
    border-radius: 6px; cursor: pointer;
  }
  .btn-clear:hover { background: #FEF2F2; }

  .upload-card {
    border: 1px solid #E2E8F0; border-radius: 10px;
    padding: 20px; margin-bottom: 16px;
    background: #fff;
  }

  .card-title { display: flex; align-items: flex-start; gap: 12px; margin-bottom: 14px; }
  .card-icon { font-size: 20px; line-height: 1; }
  .card-name { font-size: 14px; font-weight: 600; color: var(--ink); }
  .card-desc { font-size: 12px; color: var(--text-muted); margin-top: 2px; }

  .dropzone {
    border: 2px dashed #CBD5E1; border-radius: 8px;
    padding: 24px; text-align: center;
    display: flex; align-items: center; justify-content: center; gap: 10px;
    font-size: 13px; color: var(--text-muted);
    cursor: pointer; transition: border-color 0.15s, background 0.15s;
  }
  .dropzone.drag { border-color: var(--accent); background: #EFF6FF; }
  .file-selected { color: var(--ink); font-weight: 500; }

  .btn-browse {
    display: inline-block; padding: 5px 12px;
    background: #F1F5F9; border: 1px solid #CBD5E1;
    border-radius: 6px; font-size: 12px; cursor: pointer;
    color: var(--ink);
  }
  .btn-browse input { display: none; }

  .card-footer {
    display: flex; gap: 8px; margin-top: 12px;
  }
  .source-input {
    flex: 1; padding: 6px 10px; font-size: 12px;
    border: 1px solid #E2E8F0; border-radius: 6px;
    font-family: var(--font-mono);
  }
  .btn-upload {
    padding: 6px 16px; background: var(--accent); color: #fff;
    border: none; border-radius: 6px; font-size: 13px; font-weight: 600;
    cursor: pointer;
  }
  .btn-upload:disabled { opacity: 0.5; cursor: default; }

  .result {
    margin-top: 8px; font-size: 12px; color: #065F46;
    background: #D1FAE5; padding: 6px 10px; border-radius: 6px;
  }
  .result.err { color: #991B1B; background: #FEE2E2; }

  .info-card {
    display: flex; gap: 12px; align-items: flex-start;
    background: #F8FAFC; border: 1px solid #E2E8F0;
    border-radius: 10px; padding: 16px 20px;
    margin-top: 8px;
  }
  .info-icon { font-size: 18px; line-height: 1; }
  .info-title { font-size: 13px; font-weight: 600; color: var(--ink); margin-bottom: 4px; }
  .info-body { font-size: 12px; color: var(--text-muted); line-height: 1.6; }
  .info-body code {
    background: #E2E8F0; padding: 1px 5px; border-radius: 4px;
    font-family: var(--font-mono); font-size: 11px;
  }

  .empty, .loading { padding: 40px; text-align: center; color: var(--text-muted); font-size: 14px; }
  .error { padding: 12px 16px; background: #FEE2E2; color: #991B1B; border-radius: 8px; font-size: 13px; }
</style>
