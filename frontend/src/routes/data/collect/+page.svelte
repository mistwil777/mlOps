<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { getProjects, startCollect, getCollectStatus, getAdapterCoverage } from '$lib/api';
  import type { Project, ProjectAdapter, SourceSuggestion, AdapterCoverageResult } from '$lib/types';

  let projects: Project[] = $state([]);
  let selectedProject = $state<Project | null>(null);
  let maxPairs = $state(50);
  let error = $state('');
  let es: EventSource | null = null;

  let coverage = $state<AdapterCoverageResult | null>(null);
  let coverageLoading = $state(false);

  // ── single-source mode ────────────────────────────────────────────────────
  let selectedSource = $state<{ adapter: ProjectAdapter; source: SourceSuggestion } | null>(null);
  let singleRunning = $state(false);
  let singleStatus = $state<'idle' | 'running' | 'completed' | 'failed'>('idle');

  // ── batch mode ────────────────────────────────────────────────────────────
  type QueueItem = { adapter: ProjectAdapter; source: SourceSuggestion };
  type QueueStatus = 'pending' | 'running' | 'completed' | 'failed' | 'skipped';

  let queue: QueueItem[] = $state([]);
  let queueStatuses: QueueStatus[] = $state([]);
  let queueIdx = $state(-1);   // -1 = not running
  let batchRunning = $state(false);

  // ── shared logs ──────────────────────────────────────────────────────────
  let logs: string[] = $state([]);

  // Flat list of all sources across adapters
  const allSources = $derived(
    (selectedProject?.adapters ?? []).flatMap(adapter =>
      (adapter.source_configs ?? []).map(sc => ({ adapter, source: sc }))
    )
  );

  const hasAdapters = $derived((selectedProject?.adapters ?? []).length > 0);
  const running = $derived(singleRunning || batchRunning);

  onMount(async () => {
    projects = await getProjects().catch(() => []);
    if (projects.length > 0) {
      selectedProject = projects[0];
      await loadCoverage();
    }
  });

  onDestroy(() => es?.close());

  async function selectProject(id: string) {
    selectedProject = projects.find(p => p.id === id) ?? null;
    selectedSource = null;
    singleStatus = 'idle';
    logs = [];
    queue = [];
    queueStatuses = [];
    queueIdx = -1;
    coverage = null;
    await loadCoverage();
  }

  async function loadCoverage() {
    if (!selectedProject) return;
    coverageLoading = true;
    try {
      coverage = await getAdapterCoverage(selectedProject.id);
    } catch {
      coverage = null;
    } finally {
      coverageLoading = false;
    }
  }

  function adapterCov(name: string) {
    return coverage?.adapters.find(a => a.adapter_name === name) ?? null;
  }

  // ── single source ─────────────────────────────────────────────────────────
  async function handleStart() {
    if (!selectedProject || !selectedSource) return;
    error = '';
    logs = [];
    singleRunning = true;
    singleStatus = 'running';

    try {
      const res = await startCollect({
        project_id: selectedProject.id,
        topic: selectedSource.source.value,
        max_pairs: maxPairs,
        domain: selectedProject.domain,
        task_type: selectedProject.task_type,
        collector_type: selectedSource.source.collector_type,
        source_value: selectedSource.source.value,
        adapter_name: selectedSource.adapter.name,
      });
      openSSE(res.job_id, () => {
        singleRunning = false;
        loadCoverage();
      }, (s) => { singleStatus = s as typeof singleStatus; });
    } catch (e) {
      error = (e as Error).message;
      singleRunning = false;
      singleStatus = 'failed';
    }
  }

  // ── batch (tout lancer) ────────────────────────────────────────────────────
  function startBatch() {
    if (!selectedProject || allSources.length === 0 || batchRunning) return;
    queue = [...allSources];
    queueStatuses = allSources.map(() => 'pending' as QueueStatus);
    queueIdx = 0;
    batchRunning = true;
    logs = [];
    error = '';
    selectedSource = null;
    runNextInQueue();
  }

  async function runNextInQueue() {
    if (!selectedProject || queueIdx < 0 || queueIdx >= queue.length) {
      batchRunning = false;
      queueIdx = -1;
      await loadCoverage();
      return;
    }

    const item = queue[queueIdx];
    queueStatuses = queueStatuses.map((s, i) => i === queueIdx ? 'running' : s);
    logs = [...logs, `\n── [${queueIdx + 1}/${queue.length}] ${item.adapter.name} / ${item.source.label} ──`];

    try {
      const res = await startCollect({
        project_id: selectedProject.id,
        topic: item.source.value,
        max_pairs: maxPairs,
        domain: selectedProject.domain,
        task_type: selectedProject.task_type,
        collector_type: item.source.collector_type,
        source_value: item.source.value,
        adapter_name: item.adapter.name,
      });

      openSSE(res.job_id, (finalStatus) => {
        queueStatuses = queueStatuses.map((s, i) => i === queueIdx ? (finalStatus as QueueStatus) : s);
        queueIdx++;
        runNextInQueue();
      }, () => {});
    } catch (e) {
      logs = [...logs, `ERROR: ${(e as Error).message}`];
      queueStatuses = queueStatuses.map((s, i) => i === queueIdx ? 'failed' : s);
      queueIdx++;
      runNextInQueue();
    }
  }

  function stopBatch() {
    es?.close();
    batchRunning = false;
    queueIdx = -1;
    queueStatuses = queueStatuses.map(s => s === 'pending' ? 'skipped' : s);
    loadCoverage();
  }

  // ── SSE helper ────────────────────────────────────────────────────────────
  function openSSE(
    jobId: string,
    onDone: (status: string) => void,
    onStatus: (status: string) => void,
  ) {
    es?.close();
    es = new EventSource(`/api/data/collect/${jobId}/stream`);
    es.addEventListener('log', (e) => {
      const d = JSON.parse(e.data);
      logs = [...logs, d.line];
      scrollLog();
    });
    es.addEventListener('done', async (e) => {
      const d = JSON.parse(e.data);
      onStatus(d.status);
      es?.close();
      onDone(d.status);
    });
    es.onerror = () => {
      onStatus('failed');
      es?.close();
      onDone('failed');
    };
  }

  function scrollLog() {
    setTimeout(() => {
      const el = document.getElementById('log-box');
      if (el) el.scrollTop = el.scrollHeight;
    }, 0);
  }

  const COLLECTOR_LABELS: Record<string, string> = {
    rosetta_code: 'Rosetta',
    crawl4ai:     'Web',
    manual:       'Manuel',
    github_repo:  'GitHub',
  };

  const QUEUE_STATUS_ICONS: Record<QueueStatus, string> = {
    pending:   '○',
    running:   '⟳',
    completed: '✓',
    failed:    '✗',
    skipped:   '–',
  };
</script>

<div class="topbar">
  <span class="topbar-title">Collecte</span>
</div>

<div class="content">

  <!-- Projet -->
  <div class="card mb-16">
    <div class="card-header mb-12">
      <div class="card-title">Projet</div>
    </div>
    <div class="form-row">
      <select class="input" onchange={e => selectProject((e.target as HTMLSelectElement).value)}>
        {#each projects as p}
          <option value={p.id} selected={p.id === selectedProject?.id}>{p.name}</option>
        {/each}
      </select>
      <label class="form-field-inline">
        <span class="field-label">Paires max par source</span>
        <input bind:value={maxPairs} type="number" min="5" max="500" class="input input-sm" />
      </label>
    </div>
  </div>

  {#if selectedProject && !hasAdapters}
    <div class="banner-warn mb-16">
      Ce projet n'a pas encore d'adapters définis. Retournez dans le wizard pour compléter l'analyse du besoin.
    </div>
  {/if}

  {#if selectedProject && hasAdapters}

    <!-- Couverture par adapter -->
    <div class="card mb-16">
      <div class="card-header mb-12">
        <div class="card-title">Couverture des données</div>
        {#if !coverageLoading}
          <button class="btn-icon" onclick={loadCoverage} title="Rafraîchir">↺</button>
        {/if}
      </div>

      {#if coverageLoading}
        <p class="hint">Chargement…</p>
      {:else if coverage}
        <div class="global-bar mb-12">
          <div class="global-bar-label">
            <span class="bar-count">{coverage.total_collected}</span>
            <span class="hint">/ {coverage.total_target} paires cibles</span>
            {#if coverage.untagged > 0}
              <span class="hint ml-8">({coverage.untagged} non taguées)</span>
            {/if}
          </div>
          <div class="progress-track">
            <div class="progress-fill" style="width:{Math.min(100, Math.round(coverage.total_collected / Math.max(coverage.total_target,1) * 100))}%"></div>
          </div>
        </div>

        <div class="adapter-coverage-list">
          {#each selectedProject.adapters ?? [] as adapter}
            {@const cov = adapterCov(adapter.name)}
            <div class="cov-row">
              <div class="cov-left">
                <span class="role-badge role-badge--{adapter.role}">{adapter.role === 'orchestrator' ? 'ORCH' : 'SPEC'}</span>
                <span class="cov-name">{adapter.name}</span>
              </div>
              <div class="cov-bar-wrap">
                <div class="cov-progress-track">
                  <div class="cov-progress-fill" class:fill-ok={cov && cov.pct >= 80} style="width:{cov ? Math.min(100, cov.pct) : 0}%"></div>
                </div>
                <span class="cov-numbers">
                  {cov?.collected ?? 0} / {cov?.target ?? adapter.estimated_pairs}
                  {#if cov && cov.missing > 0}
                    <span class="missing-badge">−{cov.missing}</span>
                  {:else if cov && cov.pct >= 100}
                    <span class="ok-badge">✓</span>
                  {/if}
                </span>
              </div>
            </div>
          {/each}
        </div>
      {:else}
        <p class="hint">Aucune donnée collectée pour ce projet.</p>
      {/if}
    </div>

    <!-- Sources par adapter -->
    <div class="card mb-16">
      <div class="card-header mb-12">
        <div class="card-title">Sources disponibles</div>
        <div style="display:flex; gap:8px; align-items:center">
          <span class="hint">{allSources.length} source{allSources.length > 1 ? 's' : ''}</span>
          {#if batchRunning}
            <button class="btn btn-danger btn-sm" onclick={stopBatch}>⏹ Arrêter</button>
          {:else}
            <button class="btn btn-primary btn-sm" onclick={startBatch} disabled={running}>
              ▶▶ Tout lancer ({allSources.length})
            </button>
          {/if}
        </div>
      </div>

      <!-- Batch queue progress -->
      {#if queue.length > 0}
        <div class="queue-list mb-12">
          {#each queue as item, i}
            {@const st = queueStatuses[i]}
            <div class="queue-item" class:queue-item--running={st === 'running'} class:queue-item--done={st === 'completed'} class:queue-item--failed={st === 'failed'}>
              <span class="queue-icon queue-icon--{st}">{QUEUE_STATUS_ICONS[st]}</span>
              <span class="collector-badge collector-badge--{item.source.collector_type}">
                {COLLECTOR_LABELS[item.source.collector_type] ?? item.source.collector_type}
              </span>
              <span class="queue-label">{item.adapter.name} / {item.source.label}</span>
            </div>
          {/each}
        </div>
      {/if}

      {#each selectedProject.adapters ?? [] as adapter}
        {#if (adapter.source_configs ?? []).length > 0}
          {@const cov = adapterCov(adapter.name)}
          <div class="adapter-section mb-12">
            <div class="adapter-name mb-6">
              <span class="role-badge role-badge--{adapter.role}">{adapter.role === 'orchestrator' ? 'ORCH' : 'SPEC'}</span>
              {adapter.name}
              <span class="hint">~{adapter.estimated_pairs} paires cibles</span>
              {#if cov && cov.missing > 0}
                <span class="missing-badge">−{cov.missing} manquantes</span>
              {:else if cov && cov.pct >= 100}
                <span class="ok-badge">✓ complet</span>
              {/if}
            </div>
            <div class="sources-list">
              {#each adapter.source_configs as sc}
                {@const isSelected = selectedSource?.source === sc}
                <button
                  class="source-row"
                  class:source-row--selected={isSelected}
                  onclick={() => selectedSource = isSelected ? null : { adapter, source: sc }}
                >
                  <span class="collector-badge collector-badge--{sc.collector_type}">
                    {COLLECTOR_LABELS[sc.collector_type] ?? sc.collector_type}
                  </span>
                  <span class="source-label">{sc.label}</span>
                  {#if sc.estimated_pairs}
                    <span class="source-pairs">~{sc.estimated_pairs} paires</span>
                  {/if}
                  <span class="source-value mono">{sc.value}</span>
                </button>
              {/each}
            </div>
          </div>
        {/if}
      {/each}
    </div>

    <!-- Lancement source unique -->
    {#if selectedSource && !batchRunning}
      <div class="card mb-16">
        <div class="card-header mb-8">
          <div class="card-title">Lancer la collecte</div>
        </div>
        <div class="launch-summary mb-12">
          <span class="collector-badge collector-badge--{selectedSource.source.collector_type}">
            {COLLECTOR_LABELS[selectedSource.source.collector_type] ?? selectedSource.source.collector_type}
          </span>
          <span class="mono">{selectedSource.source.label}</span>
          <span class="hint">→ {selectedSource.adapter.name}</span>
        </div>
        <div style="display:flex; gap:10px; align-items:center">
          <button class="btn btn-primary" onclick={handleStart} disabled={running}>
            {singleRunning ? '⟳ Collecte en cours…' : '▶ Lancer'}
          </button>
          {#if singleStatus === 'completed'}
            <span class="badge badge-completed">✓ Terminé</span>
          {:else if singleStatus === 'failed'}
            <span class="badge badge-failed">✗ Échoué</span>
          {/if}
        </div>
        {#if error}
          <div class="banner-error mt-8">{error}</div>
        {/if}
      </div>
    {/if}
  {/if}

  <!-- Logs -->
  {#if logs.length > 0 || running}
    <div class="card">
      <div class="section-title">Logs</div>
      <div id="log-box" class="log-box">
        {#each logs as line}
          <div class="log-line">{line}</div>
        {/each}
        {#if running}
          <div class="log-line blink">▋</div>
        {/if}
      </div>
    </div>
  {/if}

</div>

<style>
  .card-header { display:flex; align-items:center; justify-content:space-between; }
  .card-title  { font-size:13px; font-weight:600; color:var(--text); }
  .form-row    { display:flex; gap:12px; align-items:center; }
  .form-field-inline { display:flex; align-items:center; gap:6px; }
  .field-label { font-size:12px; color:var(--text-muted); white-space:nowrap; }
  .input {
    background:var(--surface-2); color:var(--text); border:1px solid var(--border);
    border-radius:var(--radius); padding:6px 10px; font-size:13px; min-width:220px;
  }
  .input-sm { min-width:70px; }
  .hint { font-size:11px; color:var(--text-dim); }
  .ml-8 { margin-left:8px; }

  /* Coverage */
  .global-bar-label { display:flex; align-items:center; gap:6px; margin-bottom:6px; }
  .bar-count { font-size:13px; font-weight:600; color:var(--text); }
  .progress-track {
    height:6px; background:var(--surface-3); border-radius:3px; overflow:hidden;
  }
  .progress-fill {
    height:100%; background:var(--accent); border-radius:3px; transition:width 400ms ease;
  }
  .adapter-coverage-list { display:flex; flex-direction:column; gap:6px; margin-top:4px; }
  .cov-row { display:flex; align-items:center; gap:10px; }
  .cov-left { display:flex; align-items:center; gap:6px; min-width:200px; }
  .cov-name { font-size:12px; color:var(--text); font-weight:500; }
  .cov-bar-wrap { flex:1; display:flex; align-items:center; gap:8px; }
  .cov-progress-track {
    flex:1; height:5px; background:var(--surface-3); border-radius:3px; overflow:hidden;
  }
  .cov-progress-fill {
    height:100%; background:var(--accent); border-radius:3px; transition:width 400ms ease;
  }
  .cov-progress-fill.fill-ok { background:var(--green); }
  .cov-numbers { font-size:11px; color:var(--text-dim); white-space:nowrap; display:flex; align-items:center; gap:4px; }
  .missing-badge { background:rgba(255,80,80,.15); color:var(--red); border-radius:3px; padding:1px 5px; font-size:10px; font-weight:700; }
  .ok-badge { background:rgba(34,197,94,.15); color:#22c55e; border-radius:3px; padding:1px 5px; font-size:10px; font-weight:700; }

  .btn-icon {
    background:none; border:none; color:var(--text-muted); cursor:pointer;
    font-size:14px; padding:2px 6px; border-radius:4px; transition:color 120ms;
  }
  .btn-icon:hover { color:var(--text); }

  /* Queue */
  .queue-list { display:flex; flex-direction:column; gap:3px; background:var(--surface-2); border-radius:var(--radius); padding:8px 10px; }
  .queue-item {
    display:flex; align-items:center; gap:6px; padding:2px 0;
    font-size:11px; color:var(--text-dim);
    opacity:.6; transition:opacity 150ms;
  }
  .queue-item--running { opacity:1; color:var(--text); }
  .queue-item--done    { opacity:.5; }
  .queue-item--failed  { opacity:.8; color:var(--red); }
  .queue-label { flex:1; }
  .queue-icon { width:14px; text-align:center; font-size:10px; }
  .queue-icon--running { color:var(--accent); animation:spin 1s linear infinite; display:inline-block; }
  .queue-icon--completed { color:#22c55e; }
  .queue-icon--failed    { color:var(--red); }
  @keyframes spin { to { transform:rotate(360deg); } }

  .adapter-section { border-left:2px solid var(--border); padding-left:12px; }
  .adapter-name {
    display:flex; align-items:center; gap:8px;
    font-size:12px; font-weight:600; color:var(--text);
  }
  .role-badge {
    font-size:9px; font-weight:700; padding:1px 5px; border-radius:3px;
    text-transform:uppercase; letter-spacing:.05em;
  }
  .role-badge--specialist  { background:rgba(91,152,248,.15); color:var(--accent); }
  .role-badge--orchestrator{ background:rgba(168,85,247,.15); color:#a855f7; }

  .sources-list { display:flex; flex-direction:column; gap:4px; }
  .source-row {
    display:flex; align-items:center; gap:8px; padding:7px 10px;
    background:var(--surface-2); border:1px solid var(--border);
    border-radius:var(--radius); cursor:pointer; text-align:left;
    transition:border-color 120ms, background 120ms;
  }
  .source-row:hover     { border-color:var(--accent); background:var(--surface-3); }
  .source-row--selected { border-color:var(--accent); background:rgba(91,152,248,.08); }
  .source-label  { flex:1; font-size:12px; color:var(--text); }
  .source-pairs  { font-size:11px; color:var(--text-dim); white-space:nowrap; }
  .source-value  { font-size:10px; color:var(--text-dim); max-width:180px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

  .collector-badge {
    font-size:9px; font-weight:700; padding:2px 6px; border-radius:999px;
    text-transform:uppercase; letter-spacing:.05em; white-space:nowrap; flex-shrink:0;
  }
  .collector-badge--rosetta_code { background:rgba(34,197,94,.15);  color:#22c55e; }
  .collector-badge--crawl4ai     { background:rgba(91,152,248,.15);  color:var(--accent); }
  .collector-badge--manual       { background:rgba(251,146,60,.15);  color:#fb923c; }
  .collector-badge--github_repo  { background:rgba(168,85,247,.15);  color:#a855f7; }

  .launch-summary { display:flex; align-items:center; gap:10px; }
  .btn-sm { padding:4px 10px; font-size:11px; }
  .btn-danger { background:rgba(255,80,80,.15); color:var(--red); border:1px solid rgba(255,80,80,.3); border-radius:var(--radius); cursor:pointer; }
  .btn-danger:hover { background:rgba(255,80,80,.25); }

  .section-title { font-size:12px; font-weight:600; color:var(--text-muted); text-transform:uppercase; letter-spacing:.05em; margin-bottom:10px; }
  .log-box {
    background:var(--surface-2); border-radius:var(--radius); padding:12px;
    height:320px; overflow-y:auto; font-family:var(--font-mono); font-size:12px;
    display:flex; flex-direction:column; gap:2px;
  }
  .log-line { white-space:pre-wrap; word-break:break-all; color:var(--text-muted); }
  .badge-failed { background:rgba(255,80,80,0.15); color:var(--red); border:1px solid var(--red); padding:4px 10px; border-radius:4px; font-size:12px; }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0} }
  .blink { animation:blink 1s step-end infinite; }
  .banner-warn { background:rgba(251,146,60,.1); border:1px solid rgba(251,146,60,.3); color:#fb923c; border-radius:var(--radius); padding:10px 14px; font-size:12px; }
</style>
