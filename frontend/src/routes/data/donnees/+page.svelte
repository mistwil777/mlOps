<script lang="ts">
  import { onMount } from 'svelte';
  import { page } from '$app/stores';
  import { req } from '$lib/api';
  import { buildChecklist, type DocumentChecklist } from '$lib/utils/cdcDocuments';

  // ── State ─────────────────────────────────────────────────────────
  let projectId = $state('');
  let projectCfg: Record<string, unknown> | null = $state(null);
  let checklist: DocumentChecklist | null = $state(null);

  interface CtxStats { rag_chunks: number; kg_entities: number; kg_relations: number; ready: boolean; }
  interface RawFileInfo { name: string; size: number; }
  interface JobStatus { status: string; pairs?: number; }

  let ctxStats    = $state<CtxStats | null>(null);
  let rawFiles    = $state<RawFileInfo[]>([]);
  let cleanStatus = $state<JobStatus | null>(null);
  let pairsStatus = $state<JobStatus | null>(null);
  let cleanPairsStatus = $state<JobStatus | null>(null);

  // A — normes upload
  let normFiles: FileList | null = $state(null);
  let normDrag  = $state(false);
  let normBusy  = $state(false);
  let normLog   = $state('');

  // B — données terrain upload
  let terrainFiles: FileList | null = $state(null);
  let terrainDrag  = $state(false);
  let terrainBusy  = $state(false);
  let terrainLog   = $state('');

  // C — clean_raw
  let cleanBusy  = $state(false);
  let cleanLines: string[] = $state([]);
  let cleanDone  = $state(false);
  let cleanCount = $state(0);

  // D — generate pairs
  let pairsBusy  = $state(false);
  let pairsLines: string[] = $state([]);
  let pairsDone  = $state(false);
  let pairsCount = $state(0);

  // E — clean pairs
  let cleanPairsBusy  = $state(false);
  let cleanPairsLines: string[] = $state([]);
  let cleanPairsDone  = $state(false);
  let cleanPairsCount = $state(0);
  let qualityMode = $state<'strict'|'balanced'|'permissive'>('balanced');
  let rawFilesExpanded = $state(false);
  let rawFilesPage     = $state(0); // 0-indexed, 10 per page

  // ── Derived ───────────────────────────────────────────────────────
  const normesRequired = $derived(checklist !== null && checklist.fromCdc && checklist.normes.length > 0);
  const stepADone      = $derived(!normesRequired || (ctxStats !== null && ctxStats.ready));
  const stepBDone      = $derived(rawFiles.length > 0);
  const stepCDone      = $derived(cleanDone || cleanCount > 0);
  const stepDDone      = $derived(pairsDone || pairsCount > 0);
  const stepEDone      = $derived(cleanPairsDone || cleanPairsCount > 0);

  // ── Init ──────────────────────────────────────────────────────────
  onMount(() => {
    const urlPid = new URLSearchParams(window.location.search).get('project');
    let pid = urlPid;
    if (!pid) {
      try { const raw = sessionStorage.getItem('mlops_current_project'); if (raw) pid = JSON.parse(raw)?.id ?? ''; } catch {}
    }
    if (pid) { projectId = pid; loadAll(); }
  });

  $effect(() => {
    const qp = $page.url.searchParams.get('project');
    if (qp && qp !== projectId) { projectId = qp; loadAll(); }
  });

  async function loadAll() {
    if (!projectId) return;
    const [cfg, stats, rawInfo, pipelineProgress] = await Promise.all([
      req<Record<string, unknown>>(`/projects/${projectId}`).catch(() => null),
      req<CtxStats>(`/data/ingestion-context/${projectId}/stats`).catch(() => null),
      req<{ files: RawFileInfo[]; total: number }>(`/data/raw-files/${projectId}`).catch(() => null),
      req<any>(`/data/progress/${projectId}`).catch(() => null),
    ]);
    if (cfg) { projectCfg = cfg; checklist = buildChecklist(cfg); }
    ctxStats  = stats;
    rawFiles  = rawInfo?.files ?? [];
    // Restore pipeline step states from server
    if (pipelineProgress) {
      cleanCount      = pipelineProgress.clean_raw_count ?? pipelineProgress.steps?.find((s: any) => s.key === 'pipeline')?.count ?? 0;
      pairsCount      = pipelineProgress.pairs_count ?? 0;
      cleanPairsCount = pipelineProgress.fine_tuning_ready ?? pipelineProgress.steps?.find((s: any) => s.key === 'revision')?.count ?? 0;
    }
  }

  // ── A: Upload normes (batch) ──────────────────────────────────────
  function onNormDrop(e: DragEvent) {
    e.preventDefault(); normDrag = false;
    if (e.dataTransfer?.files?.length) normFiles = e.dataTransfer.files;
  }

  async function uploadNorms() {
    if (!normFiles?.length || !projectId) return;
    normBusy = true; normLog = '';
    const fd = new FormData();
    for (const f of normFiles) fd.append('files', f);
    try {
      const res = await fetch(`/api/data/ingestion-context/${projectId}/norm/batch`, { method: 'POST', body: fd });
      if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText);
      const data = await res.json();
      const ok = data.results.filter((r: any) => !r.error).length;
      const err = data.results.filter((r: any) => r.error).length;
      normLog = `✓ ${ok} fichier(s) indexé(s) — ${data.rag_chunks_total} chunks RAG${err ? ` · ${err} erreur(s)` : ''}`;
      normFiles = null;
      await loadAll();
    } catch (e: any) {
      normLog = `Erreur : ${e.message}`;
    } finally { normBusy = false; }
  }

  // ── B: Upload données terrain (batch, store only) ─────────────────
  function onTerrainDrop(e: DragEvent) {
    e.preventDefault(); terrainDrag = false;
    if (e.dataTransfer?.files?.length) terrainFiles = e.dataTransfer.files;
  }

  async function uploadTerrain() {
    if (!terrainFiles?.length || !projectId) return;
    terrainBusy = true; terrainLog = '';
    const fd = new FormData();
    for (const f of terrainFiles) fd.append('files', f);
    try {
      const res = await fetch(`/api/data/raw-files/${projectId}`, { method: 'POST', body: fd });
      if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText);
      const data = await res.json();
      terrainLog = `✓ ${data.saved.length} fichier(s) stocké(s) dans datalake/raw/`;
      terrainFiles = null;
      await loadAll();
    } catch (e: any) {
      terrainLog = `Erreur : ${e.message}`;
    } finally { terrainBusy = false; }
  }

  // ── C: Nettoyage données brutes ───────────────────────────────────
  async function runCleanRaw() {
    if (!projectId) return;
    cleanBusy = true; cleanLines = []; cleanDone = false;
    try {
      const res = await fetch('/api/data/clean-raw', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_id: projectId }),
      });
      if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText);
      const { job_id } = await res.json();
      await streamJob(`/api/data/clean-raw/${job_id}/stream`, cleanLines, (lines) => { cleanLines = lines; });
      await loadAll();
      cleanDone = true;
    } catch (e: any) {
      cleanLines = [...cleanLines, `[ERROR] ${e.message}`];
    } finally { cleanBusy = false; }
  }

  // ── D: Génération de paires ───────────────────────────────────────
  async function runGeneratePairs() {
    if (!projectId) return;
    pairsBusy = true; pairsLines = []; pairsDone = false;
    try {
      const res = await fetch('/api/data/generate-pairs', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_id: projectId }),
      });
      if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText);
      const { job_id } = await res.json();
      await streamJob(`/api/data/pipeline/${job_id}/stream`, pairsLines, (lines) => { pairsLines = lines; });
      await loadAll();
      pairsDone = true;
    } catch (e: any) {
      pairsLines = [...pairsLines, `[ERROR] ${e.message}`];
    } finally { pairsBusy = false; }
  }

  // ── E: Nettoyage des paires ───────────────────────────────────────
  async function runCleanPairs() {
    if (!projectId) return;
    cleanPairsBusy = true; cleanPairsLines = []; cleanPairsDone = false;
    try {
      const res = await fetch('/api/data/pipeline', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project_id: projectId, quality_mode: qualityMode }),
      });
      if (!res.ok) throw new Error((await res.json()).detail ?? res.statusText);
      const { job_id } = await res.json();
      await streamJob(`/api/data/pipeline/${job_id}/stream`, cleanPairsLines, (lines) => { cleanPairsLines = lines; });
      await loadAll();
      cleanPairsDone = true;
    } catch (e: any) {
      cleanPairsLines = [...cleanPairsLines, `[ERROR] ${e.message}`];
    } finally { cleanPairsBusy = false; }
  }

  // ── SSE helper ────────────────────────────────────────────────────
  function streamJob(url: string, lines: string[], setLines: (l: string[]) => void): Promise<void> {
    return new Promise((resolve) => {
      const es = new EventSource(url);
      es.onmessage = (e) => {
        const line = e.data as string;
        if (line.startsWith('[DONE]') || line.startsWith('[SUMMARY]')) { es.close(); resolve(); }
        else { setLines([...lines, line]); lines = [...lines, line]; }
      };
      es.onerror = () => { es.close(); resolve(); };
    });
  }

  function fmtSize(bytes: number) {
    if (bytes < 1024) return `${bytes} o`;
    if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} Ko`;
    return `${(bytes / 1024 / 1024).toFixed(1)} Mo`;
  }

  async function clearContext() {
    if (!confirm('Supprimer tout le contexte sémantique (RAG + KG) ?')) return;
    await req(`/data/ingestion-context/${projectId}`, { method: 'DELETE' });
    await loadAll();
  }
</script>

<div class="topbar">
  <span class="topbar-title">Préparation des données</span>
  <div class="topbar-steps">
    <span class="step-dot" class:done={stepADone}>A</span>
    <span class="step-sep">→</span>
    <span class="step-dot" class:done={stepBDone} class:locked={!stepADone}>B</span>
    <span class="step-sep">→</span>
    <span class="step-dot" class:done={stepCDone} class:locked={!stepBDone}>C</span>
    <span class="step-sep">→</span>
    <span class="step-dot" class:done={stepDDone} class:locked={!stepCDone}>D</span>
    <span class="step-sep">→</span>
    <span class="step-dot" class:done={stepEDone} class:locked={!stepDDone}>E</span>
  </div>
</div>

<div class="content">
  {#if !projectId}
    <div class="empty">Sélectionnez un projet.</div>
  {:else}

  <!-- ── A : Contexte sémantique ─────────────────────────────────── -->
  <div class="step-block" class:step-done={stepADone}>
    <div class="step-label">
      <span class="step-badge" class:badge-done={stepADone}>A</span>
      <div>
        <div class="step-title">Contexte sémantique {normesRequired ? '' : '(optionnel)'}</div>
        <div class="step-desc">Normes, référentiels, templates — alimente RAG + Knowledge Graph.</div>
      </div>
      {#if ctxStats?.ready}
        <div class="step-summary">
          {ctxStats.rag_chunks} chunks RAG · {ctxStats.kg_entities} entités KG
          <button class="btn-link-danger" onclick={clearContext}>Réinitialiser</button>
        </div>
      {/if}
    </div>

    <div
      class="dropzone"
      class:drag={normDrag}
      ondragover={(e) => { e.preventDefault(); normDrag = true; }}
      ondragleave={() => normDrag = false}
      ondrop={onNormDrop}
      role="button" tabindex="0"
    >
      {#if normFiles?.length}
        <span class="file-sel">{normFiles.length} fichier(s) sélectionné(s)</span>
        <button class="btn-remove" onclick={() => normFiles = null}>✕</button>
      {:else}
        <span class="dz-hint">Glissez un dossier ou des fichiers (PDF, DOCX, TXT) ou</span>
        <label class="btn-browse">
          Parcourir
          <input type="file" multiple accept=".pdf,.docx,.xlsx,.txt"
            onchange={(e) => { const fl = (e.target as HTMLInputElement).files; if (fl?.length) normFiles = fl; }} />
        </label>
      {/if}
    </div>
    <div class="step-footer">
      <button class="btn-action" disabled={!normFiles?.length || normBusy} onclick={uploadNorms}>
        {normBusy ? '↑ Indexation…' : '↑ Indexer'}
      </button>
    </div>
    {#if normLog}
      <div class="result-line" class:err={normLog.startsWith('Erreur')}>{normLog}</div>
    {/if}
  </div>

  <!-- ── B : Données terrain ─────────────────────────────────────── -->
  <div class="step-block" class:step-done={stepBDone} class:step-locked={!stepADone}>
    <div class="step-label">
      <span class="step-badge" class:badge-done={stepBDone} class:badge-locked={!stepADone}>B</span>
      <div>
        <div class="step-title">Données terrain</div>
        <div class="step-desc">Logs, feedback, cas réels — stockés dans datalake/raw/ sans traitement.</div>
      </div>
      {#if rawFiles.length > 0}
        <div class="step-summary">{rawFiles.length} fichier(s) · {rawFiles.reduce((s, f) => s + f.size, 0) > 0 ? fmtSize(rawFiles.reduce((s, f) => s + f.size, 0)) : ''}</div>
      {/if}
    </div>

    {#if !stepADone}
      <div class="locked-hint">Complétez l'étape A d'abord.</div>
    {:else}
      {#if rawFiles.length > 0}
        <button class="file-toggle" onclick={() => { rawFilesExpanded = !rawFilesExpanded; rawFilesPage = 0; }}>
          {rawFilesExpanded ? '▲ Masquer les fichiers' : `▶ Voir les ${rawFiles.length} fichiers stockés`}
        </button>
        {#if rawFilesExpanded}
          <div class="file-list">
            {#each rawFiles.slice(rawFilesPage * 10, rawFilesPage * 10 + 10) as f}
              <div class="file-row"><span class="file-name">{f.name}</span><span class="file-size">{fmtSize(f.size)}</span></div>
            {/each}
            <div class="file-pagination">
              <span class="file-page-info">{rawFilesPage * 10 + 1}–{Math.min(rawFilesPage * 10 + 10, rawFiles.length)} sur {rawFiles.length}</span>
              {#if rawFilesPage > 0}
                <button class="file-page-btn" onclick={() => rawFilesPage--}>← Précédent</button>
              {/if}
              {#if (rawFilesPage + 1) * 10 < rawFiles.length}
                <button class="file-page-btn" onclick={() => rawFilesPage++}>Suivant →</button>
              {/if}
            </div>
          </div>
        {/if}
      {/if}
      <div
        class="dropzone"
        class:drag={terrainDrag}
        ondragover={(e) => { e.preventDefault(); terrainDrag = true; }}
        ondragleave={() => terrainDrag = false}
        ondrop={onTerrainDrop}
        role="button" tabindex="0"
      >
        {#if terrainFiles?.length}
          <span class="file-sel">{terrainFiles.length} fichier(s) sélectionné(s)</span>
          <button class="btn-remove" onclick={() => terrainFiles = null}>✕</button>
        {:else}
          <span class="dz-hint">Glissez un dossier ou des fichiers ou</span>
          <label class="btn-browse">
            Parcourir
            <input type="file" multiple
              onchange={(e) => { const fl = (e.target as HTMLInputElement).files; if (fl?.length) terrainFiles = fl; }} />
          </label>
        {/if}
      </div>
      <div class="step-footer">
        <button class="btn-action" disabled={!terrainFiles?.length || terrainBusy} onclick={uploadTerrain}>
          {terrainBusy ? '↑ Stockage…' : '↑ Stocker'}
        </button>
      </div>
      {#if terrainLog}
        <div class="result-line" class:err={terrainLog.startsWith('Erreur')}>{terrainLog}</div>
      {/if}
    {/if}
  </div>

  <!-- ── C : Nettoyage données brutes ───────────────────────────── -->
  <div class="step-block" class:step-done={stepCDone} class:step-locked={!stepBDone}>
    <div class="step-label">
      <span class="step-badge" class:badge-done={stepCDone} class:badge-locked={!stepBDone}>C</span>
      <div>
        <div class="step-title">Nettoyage des données brutes</div>
        <div class="step-desc">Déduplique, score et filtre les fichiers terrain sur leurs embeddings. Le contexte sémantique (RAG + KG) est utilisé à l'étape D.</div>
      </div>
      {#if cleanCount > 0}
        <div class="step-summary">{cleanCount.toLocaleString('fr-FR')} records qualifiés</div>
      {/if}
    </div>

    {#if !stepBDone}
      <div class="locked-hint">Complétez l'étape B d'abord.</div>
    {:else}
      <div class="step-footer">
        <button class="btn-action" disabled={cleanBusy} onclick={runCleanRaw}>
          {cleanBusy ? '⟳ Nettoyage…' : cleanCount > 0 ? '⟳ Relancer' : '▶ Lancer le nettoyage'}
        </button>
      </div>
      {#if cleanLines.length > 0}
        <div class="log-box">
          {#each cleanLines as line}
            <div class="log-line" class:log-err={line.includes('[ERROR]')} class:log-warn={line.includes('[WARNING]')} class:log-ok={line.includes('[INFO]')}>{line}</div>
          {/each}
          {#if cleanBusy}<div class="log-line blink">▋</div>{/if}
        </div>
      {/if}
    {/if}
  </div>

  <!-- ── D : Génération de paires ───────────────────────────────── -->
  <div class="step-block" class:step-done={stepDDone} class:step-locked={!stepCDone}>
    <div class="step-label">
      <span class="step-badge" class:badge-done={stepDDone} class:badge-locked={!stepCDone}>D</span>
      <div>
        <div class="step-title">Génération de paires</div>
        <div class="step-desc">Matching sémantique source↔cible. C'est ici que le RAG + KG de l'étape A ancre la génération dans le vocabulaire du domaine.</div>
      </div>
      {#if pairsCount > 0}
        <div class="step-summary">{pairsCount.toLocaleString('fr-FR')} paires formées</div>
      {/if}
    </div>

    {#if !stepCDone}
      <div class="locked-hint">Complétez l'étape C d'abord.</div>
    {:else}
      <div class="step-footer">
        <button class="btn-action" disabled={pairsBusy} onclick={runGeneratePairs}>
          {pairsBusy ? '⟳ Génération…' : pairsCount > 0 ? '⟳ Relancer' : '▶ Générer les paires'}
        </button>
      </div>
      {#if pairsLines.length > 0}
        <div class="log-box">
          {#each pairsLines as line}
            <div class="log-line" class:log-err={line.includes('[ERROR]')} class:log-ok={line.includes('[INFO]')}>{line}</div>
          {/each}
          {#if pairsBusy}<div class="log-line blink">▋</div>{/if}
        </div>
      {/if}
    {/if}
  </div>

  <!-- ── E : Nettoyage des paires ───────────────────────────────── -->
  <div class="step-block" class:step-done={stepEDone} class:step-locked={!stepDDone}>
    <div class="step-label">
      <span class="step-badge" class:badge-done={stepEDone} class:badge-locked={!stepDDone}>E</span>
      <div>
        <div class="step-title">Nettoyage des paires</div>
        <div class="step-desc">Déduplication, règles métier, score qualité. Produit fine-tuning-ready/.</div>
      </div>
      {#if cleanPairsCount > 0}
        <div class="step-summary">{cleanPairsCount.toLocaleString('fr-FR')} paires gold</div>
      {/if}
    </div>

    {#if !stepDDone}
      <div class="locked-hint">Complétez l'étape D d'abord.</div>
    {:else}
      <div class="step-footer">
        <div class="quality-mode-selector">
          <span class="threshold-label">Rigueur sémantique</span>
          <div class="mode-pills">
            <button class="mode-pill" class:active={qualityMode === 'permissive'} onclick={() => qualityMode = 'permissive'}>
              Permissif <span class="mode-hint">top 50 %</span>
            </button>
            <button class="mode-pill" class:active={qualityMode === 'balanced'} onclick={() => qualityMode = 'balanced'}>
              Équilibré <span class="mode-hint">top 35 %</span>
            </button>
            <button class="mode-pill" class:active={qualityMode === 'strict'} onclick={() => qualityMode = 'strict'}>
              Strict <span class="mode-hint">top 20 %</span>
            </button>
          </div>
        </div>
        <button class="btn-action" disabled={cleanPairsBusy} onclick={runCleanPairs}>
          {cleanPairsBusy ? '⟳ Nettoyage…' : cleanPairsCount > 0 ? '⟳ Relancer' : '▶ Nettoyer les paires'}
        </button>
      </div>
      {#if cleanPairsLines.length > 0}
        <div class="log-box">
          {#each cleanPairsLines as line}
            <div class="log-line" class:log-err={line.includes('[ERROR]')} class:log-ok={line.includes('[INFO]')}>{line}</div>
          {/each}
          {#if cleanPairsBusy}<div class="log-line blink">▋</div>{/if}
        </div>
      {/if}
    {/if}
  </div>


  {/if}
</div>

<style>
  .topbar { display:flex; align-items:center; justify-content:space-between; padding:0 24px; height:48px; border-bottom:1px solid var(--border); flex-shrink:0; }
  .topbar-title { font-size:15px; font-weight:700; color:var(--ink); }
  .topbar-steps { display:flex; align-items:center; gap:4px; }
  .step-sep { color:var(--text-dim); font-size:11px; }
  .step-dot { width:22px; height:22px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:10px; font-weight:700; background:#E2E8F0; color:#64748B; }
  .step-dot.done { background:#D1FAE5; color:#065F46; }
  .step-dot.locked { background:#F1F5F9; color:#CBD5E1; }

  .content { padding:24px 28px; max-width:860px; display:flex; flex-direction:column; gap:16px; }
  .empty { padding:40px; text-align:center; color:var(--text-muted); font-size:14px; }

  /* Step blocks */
  .step-block { border:1px solid #E2E8F0; border-radius:12px; padding:20px; background:#fff; }
  .step-block.step-done { border-color:#A7F3D0; background:#F0FDF4; }
  .step-block.step-locked { opacity:0.6; pointer-events:none; }

  .step-label { display:flex; align-items:flex-start; gap:14px; margin-bottom:16px; }
  .step-badge { width:28px; height:28px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:12px; font-weight:700; background:#E2E8F0; color:#64748B; flex-shrink:0; margin-top:2px; }
  .step-badge.badge-done { background:#059669; color:#fff; }
  .step-badge.badge-locked { background:#F1F5F9; color:#CBD5E1; }
  .step-title { font-size:14px; font-weight:700; color:var(--ink); margin-bottom:3px; }
  .step-desc { font-size:12px; color:var(--text-muted); line-height:1.5; }
  .step-summary { margin-left:auto; font-size:12px; color:#059669; font-weight:600; white-space:nowrap; padding-top:4px; display:flex; flex-direction:column; align-items:flex-end; gap:4px; }
  .btn-link-danger { background:none; border:none; color:#EF4444; font-size:11px; cursor:pointer; padding:0; text-decoration:underline; }

  /* Dropzone */
  .dropzone { border:2px dashed #CBD5E1; border-radius:8px; padding:20px; display:flex; align-items:center; justify-content:center; gap:10px; font-size:12px; color:var(--text-muted); cursor:pointer; transition:border-color 0.15s, background 0.15s; }
  .dropzone.drag { border-color:var(--accent); background:#EFF6FF; }
  .dz-hint { font-size:12px; }
  .file-sel { color:var(--ink); font-weight:500; flex:1; }
  .btn-remove { background:none; border:none; cursor:pointer; color:#94A3B8; font-size:14px; padding:0 4px; }
  .btn-browse { display:inline-block; padding:4px 10px; background:#F1F5F9; border:1px solid #CBD5E1; border-radius:6px; font-size:12px; cursor:pointer; color:var(--ink); }
  .btn-browse input { display:none; }

  /* File list */
  .file-list { margin-bottom:12px; display:flex; flex-direction:column; gap:4px; }
  .file-row { display:flex; justify-content:space-between; font-size:12px; padding:4px 8px; background:#F8FAFC; border-radius:4px; }
  .file-name { color:var(--ink); font-family:var(--font-mono); }
  .file-size { color:var(--text-muted); }
  .file-toggle { font-size:12px; color:var(--accent); padding:4px 0; background:none; border:none; cursor:pointer; font-weight:600; margin-bottom:8px; }
  .file-toggle:hover { text-decoration:underline; }
  .file-pagination { display:flex; align-items:center; gap:10px; margin-top:6px; padding-top:6px; border-top:1px solid #E2E8F0; }
  .file-page-info { font-size:11px; color:var(--text-muted); flex:1; }
  .file-page-btn { font-size:11px; color:var(--accent); background:none; border:none; cursor:pointer; font-weight:600; padding:2px 6px; }
  .file-page-btn:hover { text-decoration:underline; }

  /* Footer */
  .step-footer { display:flex; align-items:center; gap:12px; margin-top:12px; }
  .btn-action { padding:7px 18px; background:var(--accent); color:#fff; border:none; border-radius:7px; font-size:13px; font-weight:600; cursor:pointer; }
  .btn-action:disabled { opacity:0.5; cursor:default; }
  .threshold-label { display:flex; align-items:center; gap:8px; font-size:12px; color:var(--text-muted); }
  .quality-mode-selector { display:flex; align-items:center; gap:10px; margin-left:auto; }
  .mode-pills { display:flex; gap:4px; }
  .mode-pill { padding:4px 10px; border-radius:20px; border:1px solid #E2E8F0; background:#F8FAFC; font-size:11px; cursor:pointer; display:flex; align-items:center; gap:4px; }
  .mode-pill.active { background:#0284C7; color:#fff; border-color:#0284C7; }
  .mode-hint { opacity:0.7; font-size:10px; }

  /* Locked */
  .locked-hint { font-size:12px; color:#94A3B8; font-style:italic; }

  /* Results */
  .result-line { margin-top:8px; font-size:12px; color:#065F46; background:#D1FAE5; padding:6px 10px; border-radius:6px; }
  .result-line.err { color:#991B1B; background:#FEE2E2; }

  /* Log box */
  .log-box { background:#0F172A; border-radius:6px; padding:10px 12px; max-height:200px; overflow-y:auto; font-family:var(--font-mono); font-size:11px; margin-top:12px; display:flex; flex-direction:column; gap:1px; }
  .log-line { white-space:pre-wrap; word-break:break-all; color:#94A3B8; }
  .log-err  { color:#F87171; }
  .log-warn { color:#FBBF24; }
  .log-ok   { color:#6EE7B7; }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0} }
  .blink { animation:blink 1s step-end infinite; }

</style>
