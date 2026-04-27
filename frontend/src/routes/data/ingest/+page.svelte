<script lang="ts">
  import { onMount } from 'svelte';
  import { getProjects } from '$lib/api';
  import type { Project } from '$lib/types';

  let projects: Project[] = $state([]);
  let selectedProject = $state('');
  let sourceDescription = $state('manual_upload');
  let file: File | null = $state(null);
  let dragOver = $state(false);
  let uploading = $state(false);
  let result: { ingested: number; total_raw: number; source: string } | null = $state(null);
  let error = $state('');

  onMount(async () => {
    projects = await getProjects().catch(() => []);
    if (projects.length > 0) selectedProject = projects[0].id;
  });

  function onFileChange(e: Event) {
    const input = e.target as HTMLInputElement;
    file = input.files?.[0] ?? null;
    error = '';
    result = null;
  }

  function onDrop(e: DragEvent) {
    e.preventDefault();
    dragOver = false;
    const f = e.dataTransfer?.files?.[0];
    if (f) { file = f; error = ''; result = null; }
  }

  async function handleUpload() {
    if (!file || !selectedProject) return;
    uploading = true;
    error = '';
    result = null;

    const formData = new FormData();
    formData.append('file', file);

    try {
      const r = await fetch(
        `/api/data/ingest/${selectedProject}?source_description=${encodeURIComponent(sourceDescription)}`,
        { method: 'POST', body: formData },
      );
      if (!r.ok) {
        const msg = await r.text().catch(() => r.statusText);
        throw new Error(`${r.status} ${msg}`);
      }
      result = await r.json();
      file = null;
    } catch (e) {
      error = (e as Error).message;
    } finally {
      uploading = false;
    }
  }
</script>

<div class="topbar">
  <span class="topbar-title">Ingestion manuelle</span>
</div>

<div class="content">
  <div class="card" style="margin-bottom:20px">
    <div class="info-block text-muted" style="font-size:13px; margin-bottom:16px">
      Importez vos propres données d'entraînement au format <span class="mono">.jsonl</span> (chaque ligne = <span class="mono">{`{"prompt":"…","completion":"…"}`}</span>) ou une archive <span class="mono">.zip</span> contenant plusieurs fichiers JSONL.
      Les données sont ajoutées au stage <span class="mono">raw/</span> du projet sélectionné et tracées dans le journal de lignage.
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
        <input bind:value={sourceDescription} class="input" placeholder="ex: client_data_batch1, team_annotations…" />
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
          <span style="font-size:20px">📄</span>
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

    <div style="margin-top:16px; display:flex; gap:10px; align-items:center">
      <button class="btn btn-primary" onclick={handleUpload} disabled={uploading || !file || !selectedProject}>
        {uploading ? '⟳ Envoi en cours…' : '↑ Ingérer'}
      </button>
    </div>

    {#if error}
      <div class="banner-error" style="margin-top:12px">{error}</div>
    {/if}

    {#if result}
      <div class="success-box" style="margin-top:12px">
        <strong>✓ Ingestion réussie</strong><br/>
        {result.ingested} paires importées · {result.total_raw} paires au total dans raw/
      </div>
    {/if}
  </div>

  <div class="card">
    <div class="section-title">Format attendu</div>
    <pre class="code-example">{`// Chaque ligne du fichier JSONL doit avoir au minimum :
{"prompt": "texte d'entrée…", "completion": "texte de sortie…"}

// Les champs optionnels sont préservés :
{"prompt": "…", "completion": "…", "meta": {"source": "…", "domain": "code"}}`}</pre>
  </div>
</div>

<style>
  .info-block { line-height: 1.6; }
  .form-grid { display: flex; flex-wrap: wrap; gap: 16px; }
  .form-field { display: flex; flex-direction: column; gap: 6px; }
  .field-label { font-size: 12px; color: var(--text-muted); }
  .input { background: var(--surface-2); color: var(--text); border: 1px solid var(--border); border-radius: var(--radius); padding: 6px 10px; font-size: 13px; min-width: 200px; }
  .drop-zone {
    border: 2px dashed var(--border); border-radius: var(--radius); padding: 32px;
    display: flex; align-items: center; justify-content: center; cursor: default; transition: border-color 0.2s, background 0.2s;
  }
  .drop-zone.drag-over { border-color: var(--accent); background: rgba(var(--accent-rgb, 130,110,255), 0.05); }
  .drop-zone.has-file { border-color: var(--green); }
  .drop-hint { display: flex; flex-direction: column; align-items: center; gap: 8px; font-size: 13px; color: var(--text-muted); }
  .file-info { display: flex; align-items: center; gap: 10px; font-size: 13px; }
  .success-box { padding: 12px 16px; background: rgba(80,200,120,0.1); border: 1px solid var(--green); border-radius: var(--radius); font-size: 13px; color: var(--green); }
  .section-title { font-size: 12px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 10px; }
  .code-example { font-family: var(--font-mono); font-size: 12px; background: var(--surface-2); padding: 12px; border-radius: var(--radius); white-space: pre-wrap; color: var(--text-muted); }
</style>
