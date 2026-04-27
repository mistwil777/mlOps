<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { getProjects, startCollect, getCollectStatus } from '$lib/api';
  import type { Project } from '$lib/types';

  let projects: Project[] = $state([]);
  let selectedProject = $state('');
  let topic = $state('');
  let maxPairs = $state(50);
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
    if (!selectedProject || !topic.trim()) return;
    error = '';
    logs = [];
    running = true;
    jobStatus = 'running';

    try {
      const res = await startCollect({ project_id: selectedProject, topic: topic.trim(), max_pairs: maxPairs });
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
    es = new EventSource(`/api/data/collect/${jobId}/stream`);

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
      if (jobStatus === 'running') {
        jobStatus = 'failed';
        running = false;
      }
      es?.close();
    };
  }

  function scrollLog() {
    setTimeout(() => {
      const el = document.getElementById('log-box');
      if (el) el.scrollTop = el.scrollHeight;
    }, 0);
  }
</script>

<div class="topbar">
  <span class="topbar-title">Collecte AWA</span>
</div>

<div class="content">
  <div class="card" style="margin-bottom:20px">
    <div class="form-grid">
      <label class="form-field">
        <span class="field-label">Projet</span>
        <select bind:value={selectedProject} class="input">
          {#each projects as p}
            <option value={p.id}>{p.name} ({p.domain}/{p.task_type})</option>
          {/each}
        </select>
      </label>

      <label class="form-field">
        <span class="field-label">Sujet / topic</span>
        <input bind:value={topic} class="input" placeholder="ex: python async functions, legal contracts..." />
      </label>

      <label class="form-field">
        <span class="field-label">Paires max</span>
        <input bind:value={maxPairs} type="number" min="5" max="500" class="input" style="width:100px" />
      </label>
    </div>

    <div style="margin-top:16px; display:flex; gap:10px; align-items:center">
      <button class="btn btn-primary" onclick={handleStart} disabled={running || !topic.trim()}>
        {running ? '⟳ Collecte en cours…' : '▶ Lancer la collecte'}
      </button>
      {#if jobStatus === 'completed'}
        <span class="badge badge-completed">✓ Terminé</span>
      {:else if jobStatus === 'failed'}
        <span class="badge badge-failed">✗ Échoué</span>
      {/if}
    </div>

    {#if error}
      <div class="banner-error" style="margin-top:12px">{error}</div>
    {/if}
  </div>

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
  .form-grid { display: flex; flex-wrap: wrap; gap: 16px; align-items: flex-end; }
  .form-field { display: flex; flex-direction: column; gap: 6px; }
  .field-label { font-size: 12px; color: var(--text-muted); }
  .input {
    background: var(--surface-2); color: var(--text); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 6px 10px; font-size: 13px; min-width: 200px;
  }
  .section-title { font-size: 12px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 10px; }
  .log-box {
    background: var(--surface-2); border-radius: var(--radius); padding: 12px;
    height: 360px; overflow-y: auto; font-family: var(--font-mono); font-size: 12px;
    display: flex; flex-direction: column; gap: 2px;
  }
  .log-line { white-space: pre-wrap; word-break: break-all; color: var(--text-muted); }
  .badge-failed { background: rgba(255,80,80,0.15); color: var(--red); border: 1px solid var(--red); padding: 4px 10px; border-radius: 4px; font-size: 12px; }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0} }
  .blink { animation: blink 1s step-end infinite; }
</style>
