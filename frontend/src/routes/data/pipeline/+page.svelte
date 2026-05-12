<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { getProjects, startPipeline } from '$lib/api';
  import type { Project } from '$lib/types';

  let projects: Project[] = $state([]);
  let selectedProject = $state('');
  let threshold = $state(0.7);
  let running = $state(false);
  let jobId = $state('');
  let logs: string[] = $state([]);
  let jobStatus = $state<'idle' | 'running' | 'completed' | 'failed'>('idle');
  let error = $state('');
  let es: EventSource | null = null;

  onMount(async () => {
    projects = await getProjects().catch(() => []);
    if (projects.length > 0) selectedProject = projects[0].id;
  });

  onDestroy(() => es?.close());

  async function handleStart() {
    if (!selectedProject) return;
    error = '';
    logs = [];
    running = true;
    jobStatus = 'running';

    try {
      const res = await startPipeline({
        project_id: selectedProject,
        threshold,
      });
      jobId = res.job_id;
      openSSE();
    } catch (e) {
      error = (e as Error).message;
      running = false;
      jobStatus = 'failed';
    }
  }

  function openSSE() {
    es?.close();
    es = new EventSource(`/api/data/pipeline/${jobId}/stream`);

    es.addEventListener('log', (e) => {
      const d = JSON.parse(e.data);
      logs = [...logs, d.line];
      scrollLog();
    });

    es.addEventListener('done', (e) => {
      const d = JSON.parse(e.data);
      jobStatus = d.status;
      running = false;
      es?.close();
    });

    es.onerror = () => {
      if (jobStatus === 'running') { jobStatus = 'failed'; running = false; }
      es?.close();
    };
  }

  function scrollLog() {
    setTimeout(() => {
      const el = document.getElementById('log-pipeline');
      if (el) el.scrollTop = el.scrollHeight;
    }, 0);
  }
</script>

<div class="topbar">
  <span class="topbar-title">Pipeline nettoyage</span>
</div>

<div class="content">
  <div class="card" style="margin-bottom:20px">
    <div class="pipeline-info text-muted" style="font-size:13px; margin-bottom:16px">
      Le pipeline applique successivement : déduplication (exact + minhash) → filtrage par règles → scoring qualité → split train/valid.
      Les données sources sont lues depuis <span class="mono">datalake/projects/&lt;projet&gt;/processed/pairs_raw.jsonl</span>.
    </div>

    <div class="form-grid">
      <label class="form-field">
        <span class="field-label">Projet</span>
        <select bind:value={selectedProject} class="input">
          {#each projects as p}
            <option value={p.id}>{p.name}</option>
          {/each}
        </select>
      </label>

      <label class="form-field">
        <span class="field-label">Seuil qualité</span>
        <div style="display:flex; align-items:center; gap:8px">
          <input type="range" min="0.3" max="0.95" step="0.05" bind:value={threshold} style="width:140px" />
          <span class="mono" style="font-size:13px; width:36px">{threshold.toFixed(2)}</span>
        </div>
      </label>

    </div>

    <div style="margin-top:16px; display:flex; gap:10px; align-items:center">
      <button class="btn btn-primary" onclick={handleStart} disabled={running || !selectedProject}>
        {running ? '⟳ Pipeline en cours…' : '▶ Lancer le pipeline'}
      </button>
      {#if jobStatus === 'completed'}
        <span class="badge badge-completed">✓ Terminé</span>
      {:else if jobStatus === 'failed'}
        <span class="badge" style="background:rgba(255,80,80,0.15); color:var(--red); border:1px solid var(--red); padding:4px 10px; border-radius:4px; font-size:12px">✗ Échoué</span>
      {/if}
    </div>

    {#if error}
      <div class="banner-error" style="margin-top:12px">{error}</div>
    {/if}
  </div>

  {#if logs.length > 0 || running}
    <div class="card">
      <div class="section-title">Logs</div>
      <div id="log-pipeline" class="log-box">
        {#each logs as line}
          <div class="log-line {line.startsWith('ERROR') || line.includes('error') ? 'log-error' : ''}">{line}</div>
        {/each}
        {#if running}
          <div class="log-line blink">▋</div>
        {/if}
      </div>
    </div>
  {/if}
</div>

<style>
  .pipeline-info { line-height: 1.6; }
  .form-grid { display: flex; flex-wrap: wrap; gap: 20px; align-items: flex-end; }
  .form-field { display: flex; flex-direction: column; gap: 6px; }
  .field-label { font-size: 12px; color: var(--text-muted); }
  .input { background: var(--surface-2); color: var(--text); border: 1px solid var(--border); border-radius: var(--radius); padding: 6px 10px; font-size: 13px; }
  .section-title { font-size: 12px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 10px; }
  .log-box { background: var(--surface-2); border-radius: var(--radius); padding: 12px; height: 360px; overflow-y: auto; font-family: var(--font-mono); font-size: 12px; display: flex; flex-direction: column; gap: 2px; }
  .log-line { white-space: pre-wrap; word-break: break-all; color: var(--text-muted); }
  .log-error { color: var(--red); }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0} }
  .blink { animation: blink 1s step-end infinite; }
</style>
