<script lang="ts">
  import { page } from '$app/stores';
  import { onMount, onDestroy } from 'svelte';
  import { getProjects, startCleanRaw, cancelCleanRaw, getCleanRawEstimate, startPipeline,
           getAnalysisReport, runAnalysis, validateAnalysis, type AnalysisCategory, type AnalysisReport } from '$lib/api';
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

  // Step 0 — analyze source
  let analysisReport   = $state<AnalysisReport | null>(null);
  let analysisRunning  = $state(false);
  let acceptedCats     = $state<AnalysisCategory[]>([]);
  let rejectedNames    = $state<string[]>([]);

  // Step 2 — clean (pairs pipeline)
  let pipelineRunning = $state(false);
  let pipelineStatus  = $state<'idle' | 'running' | 'completed' | 'failed'>('idle');
  let pipelineJobId   = $state('');

  onMount(async () => {
    projects = await getProjects().catch(() => []);
    if (projects.length > 0) {
      const _qp = $page.url.searchParams.get('project');
      selectedProject = _qp && projects.find(p => p.id === _qp) ? _qp : projects[0].id;
      cleanRawEstimate = await getCleanRawEstimate(selectedProject).catch(() => null);
      analysisReport = await getAnalysisReport(selectedProject).catch(() => null);
      if (analysisReport?.proposed_categories) acceptedCats = [...analysisReport.proposed_categories];
    }
  });

  async function loadAnalysis() {
    if (!selectedProject) return;
    analysisReport = await getAnalysisReport(selectedProject).catch(() => null);
    if (analysisReport?.proposed_categories) acceptedCats = [...analysisReport.proposed_categories];
  }

  async function handleRunAnalysis() {
    if (!selectedProject || analysisRunning) return;
    analysisRunning = true;
    try {
      await runAnalysis(selectedProject, 20);
      // poll until done
      let attempts = 0;
      const poll = setInterval(async () => {
        attempts++;
        const r = await getAnalysisReport(selectedProject).catch(() => null);
        if (r && r.validation_status !== 'not_run') {
          clearInterval(poll);
          analysisRunning = false;
          analysisReport = r;
          acceptedCats = [...(r.proposed_categories ?? [])];
        }
        if (attempts > 60) { clearInterval(poll); analysisRunning = false; }
      }, 3000);
    } catch (e) {
      error = (e as Error).message;
      analysisRunning = false;
    }
  }

  function toggleCat(cat: AnalysisCategory) {
    if (rejectedNames.includes(cat.name)) {
      rejectedNames = rejectedNames.filter(n => n !== cat.name);
      acceptedCats = [...acceptedCats, cat];
    } else {
      rejectedNames = [...rejectedNames, cat.name];
      acceptedCats = acceptedCats.filter(c => c.name !== cat.name);
    }
  }

  async function handleValidate() {
    if (!selectedProject || acceptedCats.length === 0) return;
    try {
      await validateAnalysis(selectedProject, { accepted_categories: acceptedCats, rejected_names: rejectedNames });
      analysisReport = await getAnalysisReport(selectedProject);
    } catch (e) {
      error = (e as Error).message;
    }
  }

  async function loadEstimate() {
    if (!selectedProject) return;
    cleanRawEstimate = await getCleanRawEstimate(selectedProject).catch(() => null);
    await loadAnalysis();
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

  const analysisValidated = $derived(analysisReport?.validation_status === 'validated');
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

  <!-- Step 0 — Analyse source -->
  <div class="card step-card" style="margin-bottom:12px">
    <div class="step-header">
      <div class="step-num" class:step-done={analysisValidated}>0</div>
      <div class="step-info">
        <div class="step-title">Analyse sémantique des sources</div>
        <div class="step-desc text-muted">
          Échantillonne les documents bruts et propose une taxonomie de catégories via LLM.
          À valider avant de lancer le pipeline — garantit qu'aucune catégorie n'est oubliée.
        </div>
        {#if analysisReport && analysisReport.validation_status === 'pending'}
          <div class="analysis-panel">
            <div class="analysis-subtitle">
              Catégories proposées ({analysisReport.proposed_categories.length}) — cochez pour accepter
            </div>
            <div class="cat-list">
              {#each analysisReport.proposed_categories as cat}
                {@const rejected = rejectedNames.includes(cat.name)}
                <div class="cat-row {rejected ? 'cat-rejected' : ''}">
                  <input type="checkbox" checked={!rejected} onchange={() => toggleCat(cat)} id="cat-{cat.name}" />
                  <label for="cat-{cat.name}">
                    <span class="cat-name">{cat.name}</span>
                    <span class="cat-desc">{cat.description}</span>
                    {#if cat.estimated_count_in_sample}
                      <span class="cat-count">{cat.estimated_count_in_sample} occ.</span>
                    {/if}
                  </label>
                </div>
              {/each}
            </div>
            {#if analysisReport.missing_from_config.length > 0}
              <div class="missing-banner">
                <strong>⚠ Patterns détectés non déclarés dans la config :</strong>
                {#each analysisReport.missing_from_config as m}
                  <div class="missing-row">• <strong>{m.name}</strong> — {m.reason}</div>
                {/each}
              </div>
            {/if}
            {#if analysisReport.analyst_notes}
              <div class="analyst-notes">{analysisReport.analyst_notes}</div>
            {/if}
            <button class="btn btn-primary" style="margin-top:12px" onclick={handleValidate}
                    disabled={acceptedCats.length === 0}>
              ✓ Valider les catégories ({acceptedCats.length} acceptées)
            </button>
          </div>
        {/if}
      </div>
      <div class="step-actions">
        {#if analysisRunning}
          <span class="badge-running">⟳ Analyse en cours…</span>
        {:else if analysisValidated}
          <span class="badge-ok">✓ Validé</span>
        {:else}
          <button class="btn btn-secondary" onclick={handleRunAnalysis}
                  disabled={running || !selectedProject}>
            ▶ Analyser les sources
          </button>
        {/if}
        {#if analysisReport?.validation_status === 'pending'}
          <span class="badge-warn">En attente de validation</span>
        {/if}
      </div>
    </div>
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
                  disabled={running || !selectedProject || !analysisValidated}>
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
                disabled={running || !selectedProject || !analysisValidated}>
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
    background: #DBEAFE; color: #1D4ED8; border: 2px solid #0284C7;
    display: flex; align-items: center; justify-content: center;
    font-weight: 700; font-size: 13px; margin-top: 2px;
  }
  .step-done { background: #D1FAE5; color: #065F46; border-color: #059669; }

  .analysis-panel { margin-top: 14px; border-top: 1px solid var(--border); padding-top: 12px; }
  .analysis-subtitle { font-size: 12px; font-weight: 600; color: var(--text-muted); margin-bottom: 8px; text-transform: uppercase; letter-spacing: .04em; }
  .cat-list { display: flex; flex-direction: column; gap: 6px; margin-bottom: 10px; }
  .cat-row { display: flex; align-items: flex-start; gap: 8px; padding: 8px 10px; background: var(--surface-2); border-radius: var(--radius); border: 1px solid var(--border); }
  .cat-row label { display: flex; align-items: baseline; gap: 8px; cursor: pointer; flex: 1; }
  .cat-rejected { opacity: 0.45; }
  .cat-name { font-family: var(--font-mono); font-size: 12px; font-weight: 700; color: var(--accent); }
  .cat-desc { font-size: 12px; color: var(--text-muted); flex: 1; }
  .cat-count { font-size: 11px; color: var(--text-dim); white-space: nowrap; }
  .missing-banner { background: #FEF3C7; border: 1px solid #F59E0B; border-radius: var(--radius); padding: 10px 14px; font-size: 12px; color: #92400E; margin-bottom: 8px; }
  .missing-row { margin-top: 4px; }
  .analyst-notes { font-size: 12px; color: var(--text-muted); font-style: italic; margin-bottom: 6px; }
  .step-info { flex: 1; min-width: 0; }
  .step-title { font-size: 14px; font-weight: 600; color: var(--text); margin-bottom: 4px; }
  .step-desc { font-size: 12px; line-height: 1.5; }
  .step-warning { font-size: 11px; color: #F97316; margin-top: 6px; background: rgba(240,136,62,.08); padding: 5px 8px; border-radius: 4px; border-left: 2px solid #F97316; }
  .step-estimate { display:flex; gap:12px; flex-wrap:wrap; align-items:center; margin-top:8px; font-size:12px; background:var(--surface-2); padding:6px 10px; border-radius:4px; }
  .estimate-records { color:var(--text-muted); }
  .estimate-time { color:var(--text); }
  .estimate-note { color:var(--text-dim); font-size:11px; }
  .step-actions { flex-shrink: 0; display: flex; flex-direction: column; gap: 6px; align-items: flex-end; }

  .badge-ok     { font-size: 12px; color: var(--green); font-weight: 600; }
  .badge-err    { font-size: 12px; color: var(--red); font-weight: 600; }
  .badge-warn   { font-size: 12px; color: #F97316; font-weight: 600; }
  .badge-running { font-size: 12px; color: #0284C7; font-weight: 600; }
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
  .log-cancel { color: #F97316; }
  .log-error { color: var(--red); }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0} }
  .blink { animation: blink 1s step-end infinite; }
</style>
