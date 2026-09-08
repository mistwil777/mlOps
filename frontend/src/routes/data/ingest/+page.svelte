<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { getProjects, startIngestRaw, getIngestRawStatus } from '$lib/api';
  import type { Project } from '$lib/types';

  // ── State ──────────────────────────────────────────────────────────
  let projects: Project[]  = $state([]);
  let selectedProject      = $state('');
  let adapterName          = $state('');
  let sourceDescription    = $state('raw_upload');
  let file: File | null    = $state(null);
  let dragOver             = $state(false);
  let mode: 'raw' | 'jsonl' = $state('raw');

  // Job tracking
  let jobId      = $state('');
  let jobStatus  = $state('');
  let jobLines:  string[] = $state([]);
  let jobResult: Record<string, number> | null = $state(null);
  let minimized  = $state(false);
  let polling: ReturnType<typeof setInterval> | null = null;

  // JSONL mode state (existing behaviour)
  let uploading  = $state(false);
  let jsonlResult: { ingested: number; file: string; source: string } | null = $state(null);
  let error      = $state('');

  onMount(async () => {
    projects = await getProjects().catch(() => []);
    if (projects.length > 0) selectedProject = projects[0].id;
  });

  onDestroy(() => { if (polling) clearInterval(polling); });

  // ── File selection ────────────────────────────────────────────────
  function onFileChange(e: Event) {
    const input = e.target as HTMLInputElement;
    file = input.files?.[0] ?? null;
    error = '';
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    dragOver = false;
    const f = e.dataTransfer?.files?.[0];
    if (f) { file = f; error = ''; }
  }

  // ── Selected project adapters ─────────────────────────────────────
  let adapters: { name: string }[] = $derived(
    (projects.find(p => p.id === selectedProject) as any)?.adapters ?? []
  );

  // ── Raw ingestion (async) ─────────────────────────────────────────
  async function handleRawUpload() {
    if (!file || !selectedProject) return;
    error = '';
    jobResult = null;
    jobLines  = [];
    jobStatus = 'starting';

    try {
      const res = await startIngestRaw(selectedProject, file, sourceDescription, adapterName || undefined);
      jobId     = res.job_id;
      jobStatus = 'running';
      file      = null;
      startPolling();
    } catch (e) {
      error     = (e as Error).message;
      jobStatus = '';
    }
  }

  function startPolling() {
    if (polling) clearInterval(polling);
    polling = setInterval(async () => {
      try {
        const s = await getIngestRawStatus(jobId);
        jobLines  = (s as any).lines ?? [];
        jobStatus = (s as any).status ?? 'running';
        if ((s as any).result) jobResult = (s as any).result;
        if (jobStatus === 'completed' || jobStatus === 'failed') {
          clearInterval(polling!);
          polling = null;
        }
      } catch { /* ignore transient errors */ }
    }, 1000);
  }

  // ── JSONL ingestion (existing, synchronous) ───────────────────────
  async function handleJsonlUpload() {
    if (!file || !selectedProject) return;
    uploading  = true;
    error      = '';
    jsonlResult = null;

    const formData = new FormData();
    formData.append('file', file);

    try {
      const r = await fetch(
        `/api/data/ingest/${selectedProject}?source_description=${encodeURIComponent(sourceDescription)}`,
        { method: 'POST', body: formData },
      );
      if (!r.ok) throw new Error(`${r.status} ${await r.text().catch(() => r.statusText)}`);
      jsonlResult = await r.json();
      file = null;
    } catch (e) {
      error = (e as Error).message;
    } finally {
      uploading = false;
    }
  }
</script>

<div class="topbar">
  <span class="topbar-title">Ingestion de données</span>
</div>

<div class="content">
  <!-- Mode toggle -->
  <div class="mode-toggle" style="margin-bottom:16px">
    <button class="toggle-btn {mode === 'raw' ? 'active' : ''}" onclick={() => mode = 'raw'}>
      Import brut (tout format)
    </button>
    <button class="toggle-btn {mode === 'jsonl' ? 'active' : ''}" onclick={() => mode = 'jsonl'}>
      Import JSONL
    </button>
  </div>

  {#if mode === 'raw'}
  <!-- ── RAW MODE ───────────────────────────────────────────────── -->
  <div class="card" style="margin-bottom:20px">
    <div class="info-block text-muted" style="font-size:13px; margin-bottom:16px">
      Déposez n'importe quel fichier : <span class="mono">.java</span>, <span class="mono">.py</span>,
      <span class="mono">.pdf</span>, <span class="mono">.docx</span>, <span class="mono">.txt</span>,
      <span class="mono">.zip</span>… L'agent extrait le contenu, génère des paires d'entraînement
      et les valide automatiquement. Vous pouvez naviguer librement pendant le traitement.
    </div>

    <div class="form-grid">
      <label class="form-field">
        <span class="field-label">Projet cible</span>
        <select bind:value={selectedProject} class="input">
          {#each projects as p}
            <option value={p.id}>{p.name} ({p.domain}/{p.task_type})</option>
          {/each}
        </select>
      </label>

      <label class="form-field">
        <span class="field-label">Adapter (optionnel)</span>
        <select bind:value={adapterName} class="input">
          <option value="">Détection automatique</option>
          {#each adapters as a}
            <option value={a.name}>{a.name}</option>
          {/each}
        </select>
      </label>

      <label class="form-field">
        <span class="field-label">Source (description)</span>
        <input bind:value={sourceDescription} class="input" placeholder="ex: client_batch_1, doc_architecture…" />
      </label>
    </div>

    <!-- Drop zone -->
    <div
      class="drop-zone {dragOver ? 'drag-over' : ''} {file ? 'has-file' : ''}"
      style="margin-top:16px"
      ondragover={(e) => { e.preventDefault(); dragOver = true; }}
      ondragleave={() => dragOver = false}
      ondrop={onDrop}
      role="region"
      aria-label="Zone de dépôt"
    >
      {#if file}
        <div class="file-info">
          <span class="mono">{file.name}</span>
          <span class="text-muted" style="font-size:12px">({(file.size / 1024).toFixed(1)} KB)</span>
          <button class="btn btn-ghost" style="font-size:12px; padding:2px 8px" onclick={() => file = null}>✕</button>
        </div>
      {:else}
        <div class="drop-hint">
          <span style="font-size:28px; opacity:0.4">⬆</span>
          <span>Glissez un fichier ici ou</span>
          <label class="btn btn-ghost" style="cursor:pointer">
            Parcourir
            <input type="file" onchange={onFileChange} style="display:none" />
          </label>
        </div>
      {/if}
    </div>

    <div style="margin-top:16px">
      <button class="btn btn-primary" onclick={handleRawUpload} disabled={!file || !selectedProject || jobStatus === 'running'}>
        {jobStatus === 'running' ? '⟳ Traitement en cours…' : '↑ Lancer l\'ingestion'}
      </button>
    </div>

    {#if error}
      <div class="banner-error" style="margin-top:12px">{error}</div>
    {/if}
  </div>

  <!-- Job panel -->
  {#if jobId}
  <div class="card job-panel">
    <div class="job-header">
      <span class="job-status status-{jobStatus}">
        {#if jobStatus === 'running'}⟳ Traitement en cours{:else if jobStatus === 'completed'}✓ Terminé{:else if jobStatus === 'failed'}✗ Échec{:else}{jobStatus}{/if}
      </span>
      <span class="text-muted mono" style="font-size:11px">{jobId}</span>
      <button class="btn btn-ghost" style="font-size:11px; padding:2px 8px; margin-left:auto" onclick={() => minimized = !minimized}>
        {minimized ? 'Afficher' : 'Réduire'}
      </button>
    </div>

    {#if !minimized}
      <div class="log-container">
        {#each jobLines as line}
          <div class="log-line {line.includes('[ERROR]') ? 'log-error' : line.includes('[WARNING]') ? 'log-warn' : ''}">{line}</div>
        {/each}
        {#if jobStatus === 'running'}
          <div class="log-line log-muted">…</div>
        {/if}
      </div>

      {#if jobResult}
        <div class="result-summary">
          <div class="result-stat"><span class="stat-val">{jobResult.generated ?? 0}</span><span class="stat-label">générées</span></div>
          <div class="result-stat"><span class="stat-val green">{jobResult.validated ?? 0}</span><span class="stat-label">validées</span></div>
          <div class="result-stat"><span class="stat-val red">{jobResult.rejected ?? 0}</span><span class="stat-label">rejetées</span></div>
          <div class="result-stat"><span class="stat-val accent">{jobResult.patterns_learned ?? 0}</span><span class="stat-label">patterns appris</span></div>
        </div>
      {/if}
    {/if}
  </div>
  {/if}

  {:else}
  <!-- ── JSONL MODE (existing behaviour) ───────────────────────── -->
  <div class="card" style="margin-bottom:20px">
    <div class="info-block text-muted" style="font-size:13px; margin-bottom:16px">
      Importez un fichier <span class="mono">.jsonl</span> (chaque ligne = <span class="mono">{`{"prompt":"…","completion":"…"}`}</span>)
      ou une archive <span class="mono">.zip</span> contenant plusieurs fichiers JSONL.
      Les données sont ajoutées directement au stage <span class="mono">raw/</span> sans traitement LLM.
    </div>

    <div class="form-grid">
      <label class="form-field">
        <span class="field-label">Projet cible</span>
        <select bind:value={selectedProject} class="input">
          {#each projects as p}
            <option value={p.id}>{p.name} ({p.domain}/{p.task_type})</option>
          {/each}
        </select>
      </label>
      <label class="form-field">
        <span class="field-label">Source (description)</span>
        <input bind:value={sourceDescription} class="input" placeholder="ex: manual_upload" />
      </label>
    </div>

    <div
      class="drop-zone {dragOver ? 'drag-over' : ''} {file ? 'has-file' : ''}"
      style="margin-top:16px"
      ondragover={(e) => { e.preventDefault(); dragOver = true; }}
      ondragleave={() => dragOver = false}
      ondrop={onDrop}
      role="region"
      aria-label="Zone de dépôt"
    >
      {#if file}
        <div class="file-info">
          <span class="mono">{file.name}</span>
          <span class="text-muted" style="font-size:12px">({(file.size / 1024).toFixed(1)} KB)</span>
          <button class="btn btn-ghost" style="font-size:12px; padding:2px 8px" onclick={() => file = null}>✕</button>
        </div>
      {:else}
        <div class="drop-hint">
          <span style="font-size:28px; opacity:0.4">⬆</span>
          <span>Glissez un fichier ici ou</span>
          <label class="btn btn-ghost" style="cursor:pointer">
            Parcourir
            <input type="file" accept=".jsonl,.zip" onchange={onFileChange} style="display:none" />
          </label>
        </div>
      {/if}
    </div>

    <div style="margin-top:16px">
      <button class="btn btn-primary" onclick={handleJsonlUpload} disabled={uploading || !file || !selectedProject}>
        {uploading ? '⟳ Envoi en cours…' : '↑ Ingérer'}
      </button>
    </div>

    {#if error}
      <div class="banner-error" style="margin-top:12px">{error}</div>
    {/if}
    {#if jsonlResult}
      <div class="success-box" style="margin-top:12px">
        <strong>✓ Ingestion réussie</strong> — {jsonlResult.ingested} paires importées dans <span class="mono">{jsonlResult.file}</span>
      </div>
    {/if}
  </div>

  <div class="card">
    <div class="section-title">Format attendu</div>
    <pre class="code-example">{`// Minimum requis :
{"prompt": "texte d'entrée…", "completion": "texte de sortie…"}

// Champs optionnels préservés :
{"prompt": "…", "completion": "…", "meta": {"source": "…", "domain": "code"}}`}</pre>
  </div>
  {/if}
</div>

<style>
  .mode-toggle { display: flex; gap: 2px; background: var(--surface-2); border-radius: var(--radius); padding: 3px; width: fit-content; }
  .toggle-btn { padding: 5px 14px; font-size: 12px; border-radius: calc(var(--radius) - 2px); border: none; background: transparent; color: var(--text-muted); cursor: pointer; transition: background 0.15s, color 0.15s; }
  .toggle-btn.active { background: var(--surface-3, var(--accent)); color: var(--text); }
  .info-block { line-height: 1.6; }
  .form-grid { display: flex; flex-wrap: wrap; gap: 16px; }
  .form-field { display: flex; flex-direction: column; gap: 6px; }
  .field-label { font-size: 12px; color: var(--text-muted); }
  .input { background: var(--surface-2); color: var(--text); border: 1px solid var(--border); border-radius: var(--radius); padding: 6px 10px; font-size: 13px; min-width: 200px; }
  .drop-zone { border: 2px dashed var(--border); border-radius: var(--radius); padding: 32px; display: flex; align-items: center; justify-content: center; cursor: default; transition: border-color 0.2s, background 0.2s; }
  .drop-zone.drag-over { border-color: var(--accent); background: rgba(var(--accent-rgb, 130,110,255), 0.05); }
  .drop-zone.has-file { border-color: var(--green); }
  .drop-hint { display: flex; flex-direction: column; align-items: center; gap: 8px; font-size: 13px; color: var(--text-muted); }
  .file-info { display: flex; align-items: center; gap: 10px; font-size: 13px; }
  .success-box { padding: 12px 16px; background: rgba(80,200,120,0.1); border: 1px solid var(--green); border-radius: var(--radius); font-size: 13px; color: var(--green); }
  .section-title { font-size: 12px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 10px; }
  .code-example { font-family: var(--font-mono); font-size: 12px; background: var(--surface-2); padding: 12px; border-radius: var(--radius); white-space: pre-wrap; color: var(--text-muted); }

  /* Job panel */
  .job-panel { padding: 14px 16px; }
  .job-header { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; font-size: 13px; }
  .job-status { font-weight: 600; }
  .status-running { color: var(--accent); }
  .status-completed { color: var(--green); }
  .status-failed { color: var(--red, #e66767); }
  .log-container { background: var(--surface-2); border-radius: var(--radius); padding: 10px 12px; max-height: 220px; overflow-y: auto; font-family: var(--font-mono); font-size: 11px; display: flex; flex-direction: column; gap: 2px; }
  .log-line { color: var(--text-muted); }
  .log-error { color: var(--red, #e66767); }
  .log-warn { color: var(--yellow, #e8a600); }
  .log-muted { opacity: 0.4; }
  .result-summary { display: flex; gap: 24px; margin-top: 14px; padding-top: 12px; border-top: 1px solid var(--border); }
  .result-stat { display: flex; flex-direction: column; align-items: center; gap: 2px; }
  .stat-val { font-size: 20px; font-weight: 700; }
  .stat-label { font-size: 11px; color: var(--text-muted); }
  .green { color: var(--green); }
  .red { color: var(--red, #e66767); }
  .accent { color: var(--accent); }
</style>
