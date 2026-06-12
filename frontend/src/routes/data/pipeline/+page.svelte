<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { getProjects, startCleanRaw, cancelCleanRaw, getCleanRawEstimate, startPipeline } from '$lib/api';
  import type { Project } from '$lib/types';

  let projects: Project[] = $state([]);
  let selectedProject = $state('');
  let threshold = $state(0.7);
  let error = $state('');
  let es: EventSource | null = null;
  let logs: string[] = $state([]);
  let copied = $state(false);

  // Step 1 — clean_raw
  let cleanRawRunning = $state(false);
  let cleanRawStatus  = $state<'idle' | 'running' | 'completed' | 'failed' | 'cancelled'>('idle');
  let cleanRawJobId   = $state('');
  let cleanRawCancelling = $state(false);
  let cleanRawEstimate: { raw_records: number; est_label: string } | null = $state(null);

  // Step 2 — clean (pairs pipeline)
  let pipelineRunning = $state(false);
  let pipelineStatus  = $state<'idle' | 'running' | 'completed' | 'failed'>('idle');
  let pipelineJobId   = $state('');

  onMount(async () => {
    projects = await getProjects().catch(() => []);
    if (projects.length > 0) {
      selectedProject = projects[0].id;
      cleanRawEstimate = await getCleanRawEstimate(projects[0].id).catch(() => null);
    }
  });

  async function loadEstimate() {
    if (selectedProject)
      cleanRawEstimate = await getCleanRawEstimate(selectedProject).catch(() => null);
  }

  onDestroy(() => es?.close());

  function copyLogs() {
    navigator.clipboard.writeText(logs.join('\n')).then(() => {
      copied = true;
      setTimeout(() => copied = false, 2000);
    });
  }

  function openSSE(jobId: string, onDone: (status: string) => void) {
    es?.close();
    es = new EventSource(`/api/data/collect/${jobId}/stream`);
    es.addEventListener('message', (e) => {
      try { const d = JSON.parse(e.data); if (d.line) logs = [...logs, d.line]; } catch {}
    });
    es.addEventListener('log', (e) => {
      try { const d = JSON.parse(e.data); if (d.line) logs = [...logs, d.line]; scrollLog(); } catch {}
    });
    es.addEventListener('done', (e) => {
      try { const d = JSON.parse(e.data); onDone(d.status); } catch { onDone('failed'); }
      es?.close();
    });
    es.onerror = () => { onDone('failed'); es?.close(); };
  }

  function scrollLog() {
    setTimeout(() => {
      const el = document.getElementById('log-pipeline');
      if (el) el.scrollTop = el.scrollHeight;
    }, 0);
  }

  async function handleCleanRaw() {
    if (!selectedProject || cleanRawRunning) return;
    error = '';
    logs = [...logs, '\n── Étape 1 : Nettoyage des données brutes (clean_raw) ──'];
    cleanRawRunning = true;
    cleanRawStatus = 'running';
    cleanRawCancelling = false;
    try {
      const res = await startCleanRaw({ project_id: selectedProject });
      cleanRawJobId = res.job_id;
      openSSE(res.job_id, (s) => {
        cleanRawRunning = false;
        cleanRawCancelling = false;
        cleanRawStatus = s === 'completed' ? 'completed'
          : s === 'cancelled' ? 'cancelled'
          : 'failed';
        cleanRawJobId = '';
      });
    } catch (e) {
      const msg = (e as Error).message;
      // 409 = already running — show the existing job info, don't reset
      if (msg.includes('409') || msg.includes('déjà en cours')) {
        error = msg;
        cleanRawRunning = false;
        cleanRawStatus = 'idle';
      } else {
        error = msg;
        cleanRawRunning = false;
        cleanRawStatus = 'failed';
      }
    }
  }

  async function handleCancelCleanRaw() {
    if (!cleanRawJobId || cleanRawCancelling) return;
    cleanRawCancelling = true;
    logs = [...logs, '⏹ Annulation en cours…'];
    try {
      await cancelCleanRaw(cleanRawJobId);
    } catch (e) {
      logs = [...logs, `⚠ Annulation: ${(e as Error).message}`];
      cleanRawCancelling = false;
    }
  }

  async function handlePipeline() {
    if (!selectedProject || pipelineRunning) return;
    error = '';
    logs = [...logs, '\n── Étape 2 : Nettoyage des paires (clean) ──'];
    pipelineRunning = true;
    pipelineStatus = 'running';
    try {
      const res = await startPipeline({ project_id: selectedProject, threshold });
      pipelineJobId = res.job_id;
      openSSE(res.job_id, (s) => {
        pipelineRunning = false;
        pipelineStatus = s === 'completed' ? 'completed' : 'failed';
        pipelineJobId = '';
      });
    } catch (e) {
      error = (e as Error).message;
      pipelineRunning = false;
      pipelineStatus = 'failed';
    }
  }

  const running = $derived(cleanRawRunning || pipelineRunning);
</script>

<div class="topbar">
  <span class="topbar-title">Pipeline nettoyage</span>
</div>

<div class="content">

  <!-- Projet selector -->
  <div class="card" style="margin-bottom:16px">
    <label class="form-field">
      <span class="field-label">Projet</span>
      <select bind:value={selectedProject} class="input" style="max-width:220px"
              onchange={loadEstimate}>
        {#each projects as p}
          <option value={p.id}>{p.name}</option>
        {/each}
      </select>
    </label>
  </div>

  <!-- Step 1 — clean_raw -->
  <div class="card step-card" style="margin-bottom:12px">
    <div class="step-header">
      <div class="step-num">1</div>
      <div class="step-info">
        <div class="step-title">Nettoyage des données brutes</div>
        <div class="step-desc text-muted">
          Déduplique, score et filtre les records bruts par adapter.
          Produit <span class="mono">processed/clean_raw/&lt;adapter&gt;.jsonl</span>.
          À relancer après chaque nouvelle collecte.
        </div>
        {#if cleanRawEstimate}
          <div class="step-estimate">
            <span class="estimate-records">📦 {cleanRawEstimate.raw_records.toLocaleString()} records bruts</span>
            <span class="estimate-time">⏱ Durée estimée : <strong>{cleanRawEstimate.est_label}</strong></span>
            <span class="estimate-note">(embeddings Bedrock · M4 Pro · ~3 min/1 000 records)</span>
          </div>
        {/if}
        {#if cleanRawStatus === 'running'}
          <div class="step-warning">
            ⚠ Un seul clean_raw peut tourner à la fois — évitez les lancements multiples
            qui saturent le CPU (embeddings Bedrock intensifs).
          </div>
        {/if}
      </div>
      <div class="step-actions">
        {#if cleanRawRunning}
          <button
            class="btn btn-danger btn-sm"
            disabled={cleanRawCancelling}
            onclick={handleCancelCleanRaw}
          >
            {cleanRawCancelling ? '⟳ Annulation…' : '⏹ Annuler'}
          </button>
          <span class="badge-running">⟳ En cours…</span>
        {:else}
          <button class="btn btn-primary" onclick={handleCleanRaw}
                  disabled={running || !selectedProject}>
            ▶ Lancer clean_raw
          </button>
        {/if}
        {#if cleanRawStatus === 'completed'}
          <span class="badge-ok">✓ Terminé</span>
        {:else if cleanRawStatus === 'failed'}
          <span class="badge-err">✗ Échoué</span>
        {:else if cleanRawStatus === 'cancelled'}
          <span class="badge-warn">⏹ Annulé</span>
        {/if}
      </div>
    </div>
  </div>

  <!-- Step 2 — clean (pairs) -->
  <div class="card step-card" style="margin-bottom:20px">
    <div class="step-header">
      <div class="step-num">2</div>
      <div class="step-info">
        <div class="step-title">Nettoyage des paires</div>
        <div class="step-desc text-muted">
          Déduplique (exact + minhash), filtre par règles métier, score la qualité, split train/valid.
          Produit <span class="mono">fine-tuning-ready/train.jsonl</span>.
          À lancer après transform ou semantic-match.
        </div>
      </div>
      <div class="step-actions">
        <div style="display:flex; align-items:center; gap:8px; margin-bottom:8px">
          <span class="field-label">Seuil qualité</span>
          <input type="range" min="0.3" max="0.95" step="0.05" bind:value={threshold} style="width:120px" />
          <span class="mono" style="font-size:13px; width:36px">{threshold.toFixed(2)}</span>
        </div>
        <button class="btn btn-primary" onclick={handlePipeline}
                disabled={running || !selectedProject}>
          {pipelineRunning ? '⟳ En cours…' : '▶ Lancer clean'}
        </button>
        {#if pipelineStatus === 'completed'}
          <span class="badge-ok">✓ Terminé</span>
        {:else if pipelineStatus === 'failed'}
          <span class="badge-err">✗ Échoué</span>
        {/if}
      </div>
    </div>
  </div>

  {#if error}
    <div class="banner-error" style="margin-bottom:12px">{error}</div>
  {/if}

  <!-- Logs -->
  {#if logs.length > 0 || running}
    <div class="card">
      <div class="log-header">
        <span class="section-title">Logs</span>
        {#if logs.length > 0}
          <button class="btn-copy" onclick={copyLogs}>
            {copied ? '✓ Copié' : '⎘ Copier'}
          </button>
        {/if}
      </div>
      <div id="log-pipeline" class="log-box">
        {#each logs as line}
          <div class="log-line {line.startsWith('ERROR') || line.includes('error') ? 'log-error' : line.startsWith('\n──') ? 'log-section' : line.startsWith('⏹') ? 'log-cancel' : ''}">{line}</div>
        {/each}
        {#if running}
          <div class="log-line blink">▋</div>
        {/if}
      </div>
    </div>
  {/if}

</div>

<style>
  .form-field { display: flex; flex-direction: column; gap: 6px; }
  .field-label { font-size: 12px; color: var(--text-muted); }
  .input { background: var(--surface-2); color: var(--text); border: 1px solid var(--border); border-radius: var(--radius); padding: 6px 10px; font-size: 13px; }

  .step-card { padding: 16px; }
  .step-header { display: flex; align-items: flex-start; gap: 14px; }
  .step-num {
    width: 28px; height: 28px; border-radius: 50%; flex-shrink: 0;
    background: #1f3358; color: #79c0ff; border: 2px solid #1f6feb;
    display: flex; align-items: center; justify-content: center;
    font-weight: 700; font-size: 13px; margin-top: 2px;
  }
  .step-info { flex: 1; min-width: 0; }
  .step-title { font-size: 14px; font-weight: 600; color: var(--text); margin-bottom: 4px; }
  .step-desc { font-size: 12px; line-height: 1.5; }
  .step-warning { font-size: 11px; color: #f0883e; margin-top: 6px; background: rgba(240,136,62,.08); padding: 5px 8px; border-radius: 4px; border-left: 2px solid #f0883e; }
  .step-estimate { display:flex; gap:12px; flex-wrap:wrap; align-items:center; margin-top:8px; font-size:12px; background:var(--surface-2); padding:6px 10px; border-radius:4px; }
  .estimate-records { color:var(--text-muted); }
  .estimate-time { color:var(--text); }
  .estimate-note { color:var(--text-dim); font-size:11px; }
  .step-actions { flex-shrink: 0; display: flex; flex-direction: column; gap: 6px; align-items: flex-end; }

  .badge-ok     { font-size: 12px; color: var(--green); font-weight: 600; }
  .badge-err    { font-size: 12px; color: var(--red); font-weight: 600; }
  .badge-warn   { font-size: 12px; color: #f0883e; font-weight: 600; }
  .badge-running { font-size: 12px; color: #58a6ff; font-weight: 600; }
  .btn-danger { background: rgba(255,80,80,.15); color: var(--red); border: 1px solid var(--red); border-radius: var(--radius); padding: 5px 12px; font-size: 12px; cursor: pointer; }
  .btn-danger:hover:not(:disabled) { background: rgba(255,80,80,.25); }
  .btn-danger:disabled { opacity: 0.5; cursor: not-allowed; }

  .log-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; }
  .section-title { font-size: 12px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; }
  .btn-copy { background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius); padding: 3px 10px; font-size: 11px; color: var(--text-muted); cursor: pointer; }
  .btn-copy:hover { color: var(--text); border-color: var(--accent); }
  .log-box { background: var(--surface-2); border-radius: var(--radius); padding: 12px; height: 400px; overflow-y: auto; font-family: var(--font-mono); font-size: 12px; display: flex; flex-direction: column; gap: 2px; }
  .log-line { white-space: pre-wrap; word-break: break-all; color: var(--text-muted); }
  .log-section { color: var(--accent); font-weight: 600; }
  .log-cancel { color: #f0883e; }
  .log-error { color: var(--red); }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0} }
  .blink { animation: blink 1s step-end infinite; }
</style>
