<script lang="ts">
  import { onMount } from 'svelte';
  import { getProjects, getDataHealth, getLineage, getAdapterCoverage, getGenerateEstimate, startGeneratePairs } from '$lib/api';
  import type { Project, DataHealth } from '$lib/types';

  let projects: Project[] = $state([]);
  let selectedProject = $state('');
  let health: DataHealth | null = $state(null);
  let lineage: Record<string, unknown>[] = $state([]);
  let coverage: any = $state(null);
  let loading = $state(false);
  let error = $state('');

  let generateEstimate: { total_needed: number; cost_usd: number; cost_eur: number; per_adapter: any[] } | null = $state(null);
  let generateRunning = $state(false);
  let generateJobId   = $state('');
  let generateStatus  = $state<'idle' | 'running' | 'completed' | 'failed'>('idle');
  let generateLogs: string[] = $state([]);
  let showGenerate    = $state(false);

  onMount(async () => {
    projects = await getProjects().catch(() => []);
    if (projects.length > 0) {
      selectedProject = projects[0].id;
      await loadAll();
    }
  });

  async function loadAll() {
    if (!selectedProject) return;
    loading = true; error = '';
    try {
      [health, lineage, coverage] = await Promise.all([
        getDataHealth(selectedProject),
        getLineage(selectedProject).catch(() => []),
        getAdapterCoverage(selectedProject).catch(() => null),
      ]);
    } catch (e) { error = (e as Error).message; }
    finally { loading = false; }
  }

  function funnelPct(val: number, max: number) {
    return max > 0 ? Math.round((val / max) * 100) : 0;
  }

  async function loadGenerateEstimate() {
    if (selectedProject)
      generateEstimate = await getGenerateEstimate(selectedProject).catch(() => null);
  }

  function openGeneratePanel() {
    showGenerate = true;
    loadGenerateEstimate();
  }

  function openGenSSE(jobId: string, onDone: (s: string) => void) {
    const es = new EventSource(`/api/data/collect/${jobId}/stream`);
    es.addEventListener('log', (e) => {
      try { const d = JSON.parse(e.data); if (d.line) generateLogs = [...generateLogs, d.line]; } catch {}
    });
    es.addEventListener('done', (e) => {
      try { const d = JSON.parse(e.data); onDone(d.status); } catch { onDone('failed'); }
      es.close();
    });
    es.onerror = () => { onDone('failed'); es.close(); };
  }

  async function handleGenerate() {
    if (!selectedProject || generateRunning) return;
    generateRunning = true;
    generateStatus = 'running';
    generateLogs = [];
    try {
      const res = await startGeneratePairs({ project_id: selectedProject });
      generateJobId = res.job_id;
      openGenSSE(res.job_id, (s) => {
        generateRunning = false;
        generateStatus = s === 'completed' ? 'completed' : 'failed';
        if (s === 'completed') loadAll();
      });
    } catch (e) {
      generateRunning = false;
      generateStatus = 'failed';
      generateLogs = [`ERROR: ${(e as Error).message}`];
    }
  }

  // Top-level derived values accessible everywhere in the template
  const pairsCount  = $derived(health?.funnel?.pairs ?? 0);
  const hasFused    = $derived(pairsCount > 0);
  const totalTarget = $derived(coverage?.total_target ?? 4860);

  // Derive workflow step statuses from health + coverage data
  const steps = $derived.by(() => {
    const rawCount       = health?.funnel?.raw ?? 0;
    const cleanRawCount  = health?.funnel?.clean_raw ?? 0;
    const ftCount        = health?.funnel?.fine_tuning_ready ?? 0;
    const hasPairs       = coverage?.adapters?.some((a: any) => (a.collected ?? 0) > 0) ?? false;

    return [
      {
        num: 1,
        title: 'Collecte des données',
        desc: 'AWA collecte des sources bilingues (description + code) pour chaque adapter. Utilise source_discoverer pour trouver automatiquement des sources de qualité sur developpez.com, geeksforgeeks, programiz...',
        href: '/data/collect',
        label: 'Ouvrir Collecte AWA →',
        status: rawCount > 0 ? (coverage?.adapters?.every((a: any) => a.collected > 0 || a.pair_strategy !== 'fuse') ? 'done' : 'ready') : 'todo',
        stat: rawCount > 0 ? `${rawCount.toLocaleString()} records bruts — ${coverage?.adapters?.filter((a: any) => a.pair_strategy === 'fuse' && a.collected === 0).length ?? 0} adapter(s) sans données` : 'Aucun record collecté',
        tip: rawCount > 0 && (coverage?.adapters?.some((a: any) => a.pair_strategy === 'fuse' && a.collected === 0)) ? 'Certains adapters n\'ont pas de données — relancez AWA pour compléter la collecte.' : rawCount === 0 ? 'Lancez une collecte sur les adapters.' : null,
      },
      {
        num: 2,
        title: 'Nettoyage des données brutes (clean_raw)',
        desc: 'Déduplique, score la qualité, filtre les records hors-sujet, équilibre les volumes par adapter. À relancer après chaque collecte.',
        href: '/data/pipeline',
        label: 'Ouvrir Pipeline nettoyage →',
        status: cleanRawCount > 0 ? 'done' : rawCount > 0 ? 'ready' : 'blocked',
        stat: cleanRawCount > 0 ? `${cleanRawCount.toLocaleString()} records qualifiés` : 'À lancer',
        tip: rawCount > 0 && cleanRawCount === 0 ? 'Des données brutes existent — lancez clean_raw (Étape 1 dans Pipeline nettoyage).' : null,
      },
      {
        num: 3,
        title: 'Formation des paires bilingues',
        desc: 'Pour chaque adapter de traduction (pair_strategy=fuse) : le matching sémantique embed les descriptions des records Java et Python, calcule la similarité cosinus, et assemble les paires. Zéro LLM, vérification AST.',
        href: '/data/collect',
        label: 'Ouvrir Collecte AWA → bouton 🧠 Matcher',
        status: hasFused ? 'done' : cleanRawCount > 0 ? 'ready' : 'blocked',
        stat: hasFused ? `${pairsCount.toLocaleString()} paires formées` : cleanRawCount > 0 ? `${cleanRawCount.toLocaleString()} records qualifiés — prêt à matcher` : 'En attente de clean_raw',
        tip: cleanRawCount > 0 && !hasFused ? 'Lancez le matching depuis Collecte AWA — bouton 🧠 Matcher par adapter.' : null,
      },
      {
        num: 4,
        title: 'Nettoyage des paires (clean)',
        desc: 'Déduplique (MinHash), filtre par règles métier, score la qualité finale, divise en train/valid.',
        href: '/data/pipeline',
        label: 'Ouvrir Pipeline nettoyage →',
        status: ftCount > 0 ? 'done' : hasFused ? (pairsCount >= totalTarget * 0.9 ? 'ready' : 'todo') : 'blocked',
        stat: ftCount > 0
          ? `${ftCount.toLocaleString()} paires prêtes (train+valid)`
          : hasFused
            ? `${pairsCount.toLocaleString()} / ${totalTarget.toLocaleString()} paires cibles (${Math.round(pairsCount/Math.max(totalTarget,1)*100)}%)`
            : 'En attente des paires',
        tip: hasFused && ftCount === 0
          ? (pairsCount < totalTarget * 0.5
            ? `${pairsCount} paires sur ${totalTarget} cibles (${Math.round(pairsCount/totalTarget*100)}%). Vous pouvez générer les paires manquantes ou lancer clean maintenant.`
            : `${pairsCount.toLocaleString()} paires disponibles — lancez clean (Étape 2 dans Pipeline nettoyage).`)
          : null,
      },
      {
        num: 5,
        title: 'Contrôle qualité',
        desc: 'Vérifiez un échantillon des paires qualifiées. Rescapez les faux positifs, signalez les erreurs pour affiner les seuils.',
        href: '/data/review',
        label: 'Ouvrir Contrôle qualité →',
        status: ftCount > 0 ? 'ready' : 'blocked',
        stat: ftCount > 0 ? 'Données disponibles pour inspection' : 'En attente du pipeline',
        tip: null,
      },
      {
        num: 6,
        title: 'Entraînement',
        desc: 'Lance mlx_lm.lora sur les données fine-tuning-ready. Requiert le modèle de base téléchargé localement.',
        href: null,
        label: 'Commande : /train-lora',
        status: ftCount > 0 ? 'ready' : 'blocked',
        stat: ftCount > 0 ? `${ftCount.toLocaleString()} paires disponibles` : 'En attente du pipeline',
        tip: ftCount > 0 ? 'Tapez /train-lora project=migrator dans le chat Claude Code.' : null,
      },
    ];
  });
</script>

<div class="topbar">
  <span class="topbar-title">Santé des données</span>
  <div style="display:flex; gap:8px; align-items:center">
    <select bind:value={selectedProject} onchange={loadAll} class="project-select">
      {#each projects as p}<option value={p.id}>{p.name}</option>{/each}
    </select>
    <button class="btn-refresh" onclick={loadAll} title="Rafraîchir">↺</button>
  </div>
</div>

<div class="content">
  {#if error}
    <div class="banner-error">{error}</div>
  {:else if loading}
    <p class="text-muted">Chargement…</p>
  {:else if !health}
    <p class="text-muted">Sélectionnez un projet.</p>
  {:else}

    <!-- Alerts -->
    {#if health.alerts?.length > 0}
      <div class="alerts">
        {#each health.alerts as alert}
          <div class="alert-item">⚠ {alert}</div>
        {/each}
      </div>
    {/if}

    <!-- Workflow guide -->
    <div class="card" style="margin-bottom:16px; padding:16px">
      <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:14px">
        <span class="section-title">Guide de workflow</span>
        <div style="display:flex; align-items:center; gap:8px">
          <span style="font-size:11px; color:var(--text-dim)">Mise à jour à la fin de chaque traitement</span>
          <button class="btn-refresh" onclick={loadAll} title="Actualiser maintenant" disabled={loading}>
            {loading ? '⟳' : '↺'}
          </button>
        </div>
      </div>
      <div class="workflow">
        {#each steps as step}
          <div class="step-row step-{step.status}">
            <div class="step-left">
              <div class="step-circle step-circle-{step.status}">{step.status === 'done' ? '✓' : step.num}</div>
              {#if step.num < steps.length}
                <div class="step-line step-line-{step.status}"></div>
              {/if}
            </div>
            <div class="step-body">
              <div class="step-header-row">
                <span class="step-title">{step.title}</span>
                <span class="step-stat step-stat-{step.status}">{step.stat}</span>
              </div>
              <p class="step-desc">{step.desc}</p>
              {#if step.tip}
                <div class="step-tip">→ {step.tip}</div>
              {/if}
              {#if step.href}
                <a href={step.href} class="step-link">{step.label}</a>
              {:else}
                <span class="step-cmd">{step.label}</span>
              {/if}
            </div>
          </div>
        {/each}
      </div>
    </div>

    <!-- Generate missing pairs panel -->
    {#if hasFused && pairsCount < totalTarget * 0.9}
      <div class="card generate-panel" style="margin-bottom:16px">
        <div style="display:flex; align-items:center; justify-content:space-between; flex-wrap:wrap; gap:8px">
          <div>
            <div style="font-size:13px; font-weight:600; color:var(--text);">
              ⚡ Données insuffisantes — génération synthétique disponible
            </div>
            <div style="font-size:12px; color:var(--text-muted); margin-top:2px">
              {pairsCount}/{totalTarget} paires ({Math.round(pairsCount/totalTarget*100)}%) ·
              Bedrock génère les paires manquantes, vérifiées par AST · Facturé au client en production
            </div>
          </div>
          {#if !showGenerate}
            <button class="btn btn-primary btn-sm" onclick={openGeneratePanel}>
              Voir l'estimation →
            </button>
          {/if}
        </div>

        {#if showGenerate}
          <div style="margin-top:12px; border-top:1px solid var(--border); padding-top:12px">
            {#if generateEstimate}
              <div style="display:grid; grid-template-columns:1fr 1fr; gap:8px; margin-bottom:12px">
                <div class="gen-stat">
                  <div class="gen-stat-val">{generateEstimate.total_needed.toLocaleString()}</div>
                  <div class="gen-stat-label">paires à générer</div>
                </div>
                <div class="gen-stat">
                  <div class="gen-stat-val">~{generateEstimate.cost_eur} €</div>
                  <div class="gen-stat-label">coût estimé (Nova Pro)</div>
                </div>
              </div>
              <div style="font-size:11px; color:var(--text-dim); margin-bottom:10px">
                {#each generateEstimate.per_adapter.filter(a => a.needed > 0) as a}
                  <div style="display:flex; justify-content:space-between; padding:2px 0">
                    <span>{a.adapter.split('-').slice(0,3).join('-')}</span>
                    <span style="color:var(--text-muted)">{a.existing}/{a.target} → +{a.needed}</span>
                  </div>
                {/each}
              </div>
            {:else}
              <div class="hint">Calcul de l'estimation…</div>
            {/if}

            {#if generateStatus === 'running' || generateLogs.length > 0}
              <div class="gen-log-box">
                {#each generateLogs as line}
                  <div class="log-line {line.startsWith('ERROR') ? 'log-error' : ''}">{line}</div>
                {/each}
                {#if generateRunning}<div class="log-line blink">▋</div>{/if}
              </div>
            {/if}

            <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap">
              {#if !generateRunning}
                <button class="btn btn-primary" onclick={handleGenerate}
                        disabled={!generateEstimate}>
                  ⚡ Générer maintenant
                </button>
                <button class="btn btn-ghost btn-sm" onclick={() => showGenerate = false}>Annuler</button>
              {:else}
                <span style="font-size:12px; color:var(--accent)">⟳ Génération en cours…</span>
              {/if}
              {#if generateStatus === 'completed'}
                <span style="font-size:12px; color:var(--green); font-weight:600">✓ Génération terminée</span>
              {:else if generateStatus === 'failed'}
                <span style="font-size:12px; color:var(--red)">✗ Échec</span>
              {/if}
            </div>
          </div>
        {/if}
      </div>
    {/if}

    <!-- Funnel -->
    <div class="card section" style="margin-bottom:16px">
      <div class="section-title">Entonnoir de données</div>
      <div class="funnel">
        {#each [
          { label: 'Brutes (raw)',         key: 'raw',              color: 'var(--text-muted)' },
          { label: 'Traitées (processed)', key: 'processed',        color: 'var(--accent)' },
          { label: 'Fine-tuning ready',    key: 'fine_tuning_ready',color: 'var(--green)' },
        ] as stage}
          {@const count = health.funnel[stage.key as keyof typeof health.funnel]}
          {@const pct = funnelPct(count, health.funnel.raw)}
          <div class="funnel-row">
            <div class="funnel-label">{stage.label}</div>
            <div class="funnel-bar-wrap">
              <div class="funnel-bar" style="width:{pct}%; background:{stage.color}"></div>
            </div>
            <div class="funnel-count mono">{count.toLocaleString()}</div>
          </div>
        {/each}
      </div>
    </div>

    <!-- Stage cards -->
    <div class="stages-grid">
      {#each [
        { key: 'raw',              label: 'Raw',               icon: '▤' },
        { key: 'processed',        label: 'Processed',         icon: '▥' },
        { key: 'fine_tuning_ready',label: 'Fine-tuning ready', icon: '▦' },
      ] as s}
        {@const st = health.stages[s.key as keyof typeof health.stages]}
        <div class="card stage-card">
          <div class="stage-header">
            <span style="font-size:18px">{s.icon}</span>
            <span class="stage-label">{s.label}</span>
            <span class="badge {st.exists ? 'badge-completed' : 'badge-failed'}">{st.exists ? 'présent' : 'vide'}</span>
          </div>
          <div class="stage-stats">
            <div class="stat"><span class="stat-label">Paires</span><span class="mono">{st.count.toLocaleString()}</span></div>
            <div class="stat"><span class="stat-label">Taille</span><span class="mono">{st.size_mb} MB</span></div>
            {#if 'avg_quality' in st && st.avg_quality !== null}
              <div class="stat"><span class="stat-label">Qualité moy.</span><span class="mono">{((st.avg_quality as number) * 100).toFixed(1)}%</span></div>
            {/if}
            {#if 'train' in st}
              <div class="stat"><span class="stat-label">Train</span><span class="mono">{(st as any).train}</span></div>
              <div class="stat"><span class="stat-label">Valid</span><span class="mono">{(st as any).valid}</span></div>
            {/if}
          </div>
        </div>
      {/each}
    </div>

  {/if}
</div>

<style>
  .project-select { background:var(--surface-2); color:var(--text); border:1px solid var(--border); border-radius:var(--radius); padding:4px 10px; font-size:13px; }
  .btn-refresh { background:none; border:1px solid var(--border); border-radius:var(--radius); color:var(--text-muted); padding:3px 8px; cursor:pointer; font-size:13px; }
  .btn-refresh:hover { color:var(--text); }
  .generate-panel { padding:14px; border-left:3px solid #f0883e; }
  .gen-stat { background:var(--surface-2); border-radius:var(--radius); padding:10px; text-align:center; }
  .gen-stat-val { font-size:1.4rem; font-weight:700; color:var(--text); font-family:var(--font-mono); }
  .gen-stat-label { font-size:11px; color:var(--text-muted); margin-top:2px; }
  .gen-log-box { background:var(--surface-2); border-radius:var(--radius); padding:8px 10px; max-height:200px; overflow-y:auto; font-family:var(--font-mono); font-size:11px; margin-bottom:10px; }
  .log-line { white-space:pre-wrap; word-break:break-all; color:var(--text-muted); }
  .log-error { color:var(--red); }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0} }
  .blink { animation:blink 1s step-end infinite; }

  .alerts { display:flex; flex-direction:column; gap:8px; margin-bottom:16px; }
  .alert-item { padding:10px 14px; background:rgba(255,180,0,0.1); border:1px solid rgba(255,180,0,0.4); border-radius:var(--radius); font-size:13px; color:#ffb400; }

  /* Workflow */
  .section-title { font-size:12px; font-weight:700; color:var(--text-muted); text-transform:uppercase; letter-spacing:.06em; }
  .workflow { display:flex; flex-direction:column; }
  .step-row { display:flex; gap:14px; padding:4px 0; }
  .step-left { display:flex; flex-direction:column; align-items:center; width:28px; flex-shrink:0; }
  .step-circle { width:28px; height:28px; border-radius:50%; display:flex; align-items:center; justify-content:center; font-size:12px; font-weight:700; flex-shrink:0; }
  .step-circle-done    { background:#1a3a2a; color:#56d364; border:2px solid #238636; }
  .step-circle-ready   { background:#1f3358; color:#79c0ff; border:2px solid #1f6feb; }
  .step-circle-todo    { background:#1f3358; color:#79c0ff; border:2px solid #1f6feb; }
  .step-circle-blocked { background:#21262d; color:#8b949e; border:2px solid #30363d; }
  .step-line { flex:1; width:2px; min-height:12px; margin:3px 0; border-radius:1px; }
  .step-line-done    { background:#238636; }
  .step-line-ready   { background:#1f6feb; }
  .step-line-todo    { background:#1f6feb; }
  .step-line-blocked { background:#30363d; }
  .step-body { flex:1; padding-bottom:16px; min-width:0; }
  .step-header-row { display:flex; align-items:baseline; justify-content:space-between; gap:8px; flex-wrap:wrap; margin-bottom:3px; }
  .step-title { font-size:13px; font-weight:600; color:var(--text); }
  .step-stat { font-size:11px; font-weight:600; white-space:nowrap; }
  .step-stat-done    { color:#56d364; }
  .step-stat-ready   { color:#58a6ff; }
  .step-stat-todo    { color:#58a6ff; }
  .step-stat-blocked { color:#8b949e; }
  .step-desc { font-size:12px; color:var(--text-muted); line-height:1.5; margin-bottom:5px; }
  .step-tip { font-size:12px; color:#f0883e; margin-bottom:5px; }
  .step-link { font-size:12px; color:#58a6ff; text-decoration:none; font-weight:600; }
  .step-link:hover { text-decoration:underline; }
  .step-cmd { font-size:12px; color:#8b949e; font-family:var(--font-mono); }

  /* Funnel */
  .section { padding:16px; }
  .funnel { display:flex; flex-direction:column; gap:10px; }
  .funnel-row { display:flex; align-items:center; gap:12px; }
  .funnel-label { width:180px; font-size:13px; color:var(--text-muted); }
  .funnel-bar-wrap { flex:1; background:var(--surface-2); border-radius:4px; height:18px; overflow:hidden; }
  .funnel-bar { height:100%; border-radius:4px; transition:width .4s; min-width:2px; }
  .funnel-count { width:80px; text-align:right; font-size:13px; color:var(--text-muted); }

  /* Stage cards */
  .stages-grid { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; }
  @media(max-width:640px){ .stages-grid{ grid-template-columns:1fr; } }
  .stage-card { padding:14px; display:flex; flex-direction:column; gap:10px; }
  .stage-header { display:flex; align-items:center; gap:8px; }
  .stage-label { font-weight:600; flex:1; font-size:13px; }
  .stage-stats { display:flex; flex-direction:column; gap:5px; }
  .stat { display:flex; justify-content:space-between; font-size:12px; }
  .stat-label { color:var(--text-muted); }
  .badge-failed { background:rgba(255,80,80,.15); color:var(--red); border:1px solid var(--red); padding:2px 8px; border-radius:4px; font-size:11px; }
</style>
