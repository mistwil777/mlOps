<script lang="ts">
  import { onMount } from 'svelte';
  import { getProjects, getQualitySummary, getRejected, getSample, flagRecord, rescueRecord } from '$lib/api';
  import type { Project, QualitySummary, RawRecord, AdapterDiagnostic } from '$lib/types';

  // ── State ──────────────────────────────────────────────────────────────────
  let projects: Project[] = $state([]);
  let selectedProject = $state('');
  let summary: QualitySummary | null = $state(null);
  let loading = $state(false);
  let error = $state('');

  // Tabs: 'rejected' | 'sample'
  let activeTab = $state<'rejected' | 'sample'>('rejected');

  // Rejected panel
  let rejectedStep = $state<string>('all');
  let rejectedPage = $state(0);
  const PAGE_SIZE = 15;
  let rejectedItems: RawRecord[] = $state([]);
  let rejectedTotal = $state(0);
  let rejectedLoading = $state(false);

  // Sample panel
  let sampleItems: RawRecord[] = $state([]);
  let sampleTotal = $state(0);
  let sampleLoading = $state(false);
  let sampleN = $state(20);

  // Detail modal
  let detailRecord: RawRecord | null = $state(null);
  let detailGlobalIdx: number | null = $state(null);
  let detailMode: 'flag' | 'rescue' = $state('flag');
  let detailNote = $state('');
  let detailCorrections: Record<string, string> = $state({});


  const STEPS = ['all', 'schema', 'syntax', 'text_quality', 'dedup', 'relevance'] as const;
  const STEP_LABELS: Record<string, string> = {
    all: 'Tous',
    schema: 'Schéma',
    syntax: 'Syntaxe',
    text_quality: 'Qualité texte',
    dedup: 'Déduplication',
    relevance: 'Pertinence',
  };

  onMount(async () => {
    projects = await getProjects().catch(() => []);
    if (projects.length > 0) { selectedProject = projects[0].id; await loadSummary(); }
  });

  async function loadSummary() {
    if (!selectedProject) return;
    loading = true; error = '';
    try {
      summary = await getQualitySummary(selectedProject);
    } catch (e) { error = (e as Error).message; }
    finally { loading = false; }
  }

  async function loadRejected() {
    if (!selectedProject) return;
    rejectedLoading = true;
    try {
      const r = await getRejected(selectedProject, rejectedStep === 'all' ? undefined : rejectedStep, rejectedPage, PAGE_SIZE);
      rejectedItems = r.items as RawRecord[];
      rejectedTotal = r.total;
    } catch (e) { error = (e as Error).message; }
    finally { rejectedLoading = false; }
  }

  async function loadSample() {
    if (!selectedProject) return;
    sampleLoading = true;
    try {
      const r = await getSample(selectedProject, sampleN);
      sampleItems = r.sample as RawRecord[];
      sampleTotal = r.total;
    } catch (e) { error = (e as Error).message; }
    finally { sampleLoading = false; }
  }

  function openDetail(item: RawRecord, idx: number, mode: 'flag' | 'rescue') {
    detailRecord = item;
    detailGlobalIdx = idx;
    detailMode = mode;
    detailNote = '';
    // Pre-fill corrections with editable text fields
    detailCorrections = Object.fromEntries(
      Object.entries(item)
        .filter(([k, v]) => typeof v === 'string' && !k.startsWith('_') && !['source_url', 'collected_at', 'adapter_name'].includes(k))
        .map(([k, v]) => [k, v as string])
    );
  }

  async function submitDetail() {
    if (detailGlobalIdx === null || !selectedProject) return;
    try {
      if (detailMode === 'flag') {
        await flagRecord(selectedProject, { index: detailGlobalIdx, action: 'flag', note: detailNote || undefined });
        sampleItems = sampleItems.filter((_, i) => i !== sampleItems.indexOf(detailRecord!));
      } else {
        await rescueRecord(selectedProject, { index: detailGlobalIdx, action: 'rescue', corrections: Object.keys(detailCorrections).length ? detailCorrections : undefined, note: detailNote || undefined });
        rejectedItems = rejectedItems.filter((_, i) => i !== rejectedItems.indexOf(detailRecord!));
        rejectedTotal--;
      }
      await loadSummary();
      detailRecord = null;
    } catch (e) { error = (e as Error).message; }
  }

  // Field display helpers
  function displayCols(r: RawRecord): [string, string, string, string] {
    if (r.task_name && r.code)
      return ['Tâche', String(r.task_name ?? ''), 'Code', String(r.code ?? '')];
    if (r.description && r.code)
      return ['Description', String(r.description ?? ''), 'Code', String(r.code ?? '')];
    return ['Prompt', String(r.prompt ?? ''), 'Completion', String(r.completion ?? '')];
  }

  function schemaLabel(r: RawRecord): string {
    if (r.task_name && r.code && r.lang) return 'multilang_wiki';
    if (r.description && r.code)         return 'doc_crawl';
    if (r.prompt && r.completion)        return 'web_crawl';
    return 'unknown';
  }

  function langBadge(r: RawRecord): string {
    const l = String(r.lang ?? '');
    return l && !l.startsWith('http') ? l : '';
  }

  function rejectReason(r: RawRecord): string {
    for (const k of Object.keys(r)) {
      if (k.startsWith('_') && k.includes('reject') || k.includes('reason')) return String(r[k]);
    }
    return String(r._rejected_step ?? '');
  }

  $effect(() => { if (activeTab === 'rejected' && selectedProject) loadRejected(); });
  $effect(() => { if (activeTab === 'sample' && selectedProject) loadSample(); });
</script>

<div class="topbar">
  <span class="topbar-title">Contrôle qualité</span>
  <div style="display:flex; gap:10px; align-items:center">
    <select bind:value={selectedProject} onchange={loadSummary} class="input">
      {#each projects as p}<option value={p.id}>{p.name ?? p.id}</option>{/each}
    </select>
  </div>
</div>

<div class="content">
  {#if error}
    <div class="banner-error" style="margin-bottom:16px">{error}</div>
  {/if}

  <!-- ── Pipeline status ───────────────────────────────────────────────────── -->
  {#if loading}
    <p class="text-muted">Chargement…</p>
  {:else if !summary || !summary.pipeline_ran}
    <div class="card info" style="margin-bottom:20px; padding:20px">
      <div class="card-title">Pipeline qualité non encore exécuté</div>
      <p style="color:var(--text-dim); font-size:13px">
        Lance le pipeline depuis l'onglet <strong>Pipeline nettoyage</strong> pour accéder au contrôle qualité.
      </p>
    </div>
  {:else}
    <!-- ── Summary cards ─────────────────────────────────────────────────── -->
    <div class="summary-grid" style="margin-bottom:20px">
      <div class="summary-card">
        <div class="sc-value">{summary.raw_total}</div>
        <div class="sc-label">Brutes collectées</div>
      </div>
      <div class="summary-card" style="border-color:var(--red)">
        <div class="sc-value" style="color:var(--red)">{summary.auto_rejected}</div>
        <div class="sc-label">Rejetées auto</div>
        <div class="sc-pct">{summary.raw_total > 0 ? Math.round(summary.auto_rejected / summary.raw_total * 100) : 0}%</div>
      </div>
      <div class="summary-card" style="border-color:var(--green)">
        <div class="sc-value" style="color:var(--green)">{summary.qualified}</div>
        <div class="sc-label">Qualifiées</div>
        <div class="sc-pct">{summary.raw_total > 0 ? Math.round(summary.qualified / summary.raw_total * 100) : 0}%</div>
      </div>
      <div class="summary-card" style="border-color:var(--accent)">
        <div class="sc-value" style="color:var(--accent)">{summary.rescued}</div>
        <div class="sc-label">Rescapées</div>
      </div>
      <div class="summary-card" style="border-color:var(--yellow)">
        <div class="sc-value" style="color:var(--yellow)">{summary.flagged_by_human}</div>
        <div class="sc-label">Signalées</div>
      </div>
    </div>

    <!-- Rejection breakdown per step -->
    <div class="card" style="margin-bottom:20px; padding:16px">
      <div class="card-title" style="margin-bottom:12px">Détail rejets par étape</div>
      <div class="step-bars">
        {#each Object.entries(summary.rejection_by_step) as [step, count]}
          {@const pct = summary.raw_total > 0 ? count / summary.raw_total * 100 : 0}
          <div class="step-bar-row">
            <span class="step-name">{STEP_LABELS[step] ?? step}</span>
            <div class="step-bar-track">
              <div class="step-bar-fill" style="width:{Math.min(100, pct)}%"></div>
            </div>
            <span class="step-count mono">{count}</span>
          </div>
        {/each}
      </div>
    </div>

    <!-- ── Tabs ──────────────────────────────────────────────────────────── -->
    <div class="tab-bar" style="margin-bottom:16px">
      <button class="tab-btn" class:active={activeTab === 'rejected'} onclick={() => activeTab = 'rejected'}>
        ⚡ Diagnostic des rejets
        {#if summary.auto_rejected > 0}<span class="tab-badge red">{summary.auto_rejected}</span>{/if}
      </button>
      <button class="tab-btn" class:active={activeTab === 'sample'} onclick={() => activeTab = 'sample'}>
        ✓ Spot-check qualifiées
        <span class="tab-badge green">{summary.qualified}</span>
      </button>
    </div>

    <!-- ── Diagnostic tab ───────────────────────────────────────────────── -->
    {#if activeTab === 'rejected'}
      <!-- Distribution des scores LLM -->
      {#if summary.score_distribution && summary.score_distribution.length > 0}
        {@const maxCount = Math.max(...summary.score_distribution.map(b => b.count), 1)}
        <div class="card" style="margin-bottom:16px; padding:16px">
          <div class="card-title" style="margin-bottom:4px">Distribution des scores LLM (juge Claude Sonnet)</div>
          <p class="text-muted" style="font-size:12px; margin-bottom:12px">
            Seuil d'acceptation actuel : <strong style="color:var(--accent)">0.70</strong> —
            {summary.score_distribution.filter(b => parseFloat(b.range) >= 0.7).reduce((s, b) => s + b.count, 0)} paires acceptées sur {summary.qualified}
          </p>
          <div class="score-hist">
            {#each summary.score_distribution as bucket}
              {@const pct = bucket.count / maxCount * 100}
              {@const score = parseFloat(bucket.range.split('-')[0])}
              {@const color = score >= 0.7 ? 'var(--green)' : score >= 0.5 ? 'var(--yellow)' : 'var(--red)'}
              <div class="hist-col">
                <div class="hist-bar-wrap">
                  <div class="hist-bar" style="height:{Math.max(2, pct)}%; background:{color}"></div>
                  {#if score === 0.7}
                    <div class="hist-threshold" title="Seuil 0.70"></div>
                  {/if}
                </div>
                <div class="hist-label">{bucket.range.split('-')[0]}</div>
                <div class="hist-count">{bucket.count}</div>
              </div>
            {/each}
          </div>
        </div>
      {/if}

      <!-- Diagnostic par adapter -->
      <div class="card-title" style="margin-bottom:10px">Diagnostic par adapter</div>
      <div class="diag-grid">
        {#each (summary.per_adapter ?? []) as adapter}
          {@const acceptPct = adapter.pairs_total > 0 ? adapter.accepted / adapter.pairs_total * 100 : 0}
          {@const rejectPct = 100 - acceptPct}
          {@const severity = adapter.rejection_rate > 0.7 ? 'high' : adapter.rejection_rate > 0.4 ? 'medium' : 'low'}
          {@const sevColor = severity === 'high' ? 'var(--red)' : severity === 'medium' ? 'var(--yellow)' : 'var(--green)'}
          <div class="diag-card card">
            <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:8px; margin-bottom:8px">
              <div style="font-size:13px; font-weight:600; color:var(--text)">{adapter.adapter_name.replace(/-/g, '​-')}</div>
              <span class="sev-badge" style="background:{sevColor}20; color:{sevColor}">
                {Math.round(adapter.rejection_rate * 100)}% rejeté
              </span>
            </div>

            <!-- Barre bicolore accepté/rejeté -->
            <div class="bicolor-bar" title="{adapter.accepted} acceptées / {adapter.pairs_total - adapter.accepted} rejetées">
              <div class="bicolor-accept" style="width:{acceptPct}%"></div>
              <div class="bicolor-reject" style="width:{rejectPct}%"></div>
            </div>
            <div style="display:flex; justify-content:space-between; font-size:11px; color:var(--text-dim); margin-top:3px">
              <span style="color:var(--green)">✓ {adapter.accepted} acceptées</span>
              <span style="color:var(--red)">✕ {adapter.pairs_total - adapter.accepted} rejetées</span>
            </div>

            {#if adapter.avg_llm_score !== null}
              <div style="margin-top:8px; font-size:12px; color:var(--text-dim)">
                Score LLM moyen :
                <strong style="color:{adapter.avg_llm_score >= 0.75 ? 'var(--green)' : adapter.avg_llm_score >= 0.6 ? 'var(--yellow)' : 'var(--red)'}">
                  {adapter.avg_llm_score.toFixed(2)}
                </strong>
              </div>
            {/if}

            <div class="rec-box" style="border-color:{sevColor}40; margin-top:10px">
              <span class="rec-icon">→</span>
              {adapter.recommendation}
            </div>
          </div>
        {/each}
      </div>
    {/if}

    <!-- ── Sample tab ────────────────────────────────────────────────────── -->
    {#if activeTab === 'sample'}
      <div style="display:flex; gap:10px; align-items:center; margin-bottom:12px">
        <span class="text-muted" style="font-size:12px">Taille de l'échantillon :</span>
        <select bind:value={sampleN} onchange={loadSample} class="input" style="width:80px">
          <option value={10}>10</option>
          <option value={20}>20</option>
          <option value={50}>50</option>
        </select>
        <button class="btn btn-ghost" style="font-size:12px" onclick={loadSample}>↻ Nouvel échantillon</button>
        <span class="text-muted" style="font-size:12px">{sampleTotal} données qualifiées au total</span>
      </div>

      {#if sampleLoading}
        <p class="text-muted">Chargement…</p>
      {:else if sampleItems.length === 0}
        {#if !summary.pipeline_ran}
          <p class="text-muted">Pipeline qualité non exécuté.</p>
        {:else}
          <p class="text-muted">Aucune donnée qualifiée disponible.</p>
        {/if}
      {:else}
        <p class="text-muted" style="font-size:12px; margin-bottom:8px">
          Cet échantillon aléatoire permet de vérifier visuellement que les données qualifiées
          sont correctes. Signalez celles qui vous semblent erronées.
        </p>
        <div class="record-list">
          {#each sampleItems as item, i}
            {@const [lLabel, lVal, rLabel, rVal] = displayCols(item)}
            {@const lang = langBadge(item)}
            {@const schema = schemaLabel(item)}
            {@const score = (item.llm_score ?? item.quality_score ?? null) as number | null}
            <div class="record-card card">
              <div class="record-header">
                <div style="display:flex; gap:6px; align-items:center; flex-wrap:wrap">
                  <span class="mono text-muted" style="font-size:11px">#{i}</span>
                  <span class="schema-badge schema-{schema}">{schema}</span>
                  {#if lang}<span class="schema-badge lang-badge">{lang}</span>{/if}
                  {#if item.adapter_name}<span class="schema-badge adapter-badge">{String(item.adapter_name).split('-')[0]}</span>{/if}
                  {#if score != null}<span class="quality-badge" style="color:{score >= 0.8 ? 'var(--green)' : score >= 0.7 ? 'var(--yellow)' : 'var(--red)'}">
                    ⬤ {(score * 100).toFixed(0)}%
                  </span>{/if}
                </div>
              </div>
              <div class="record-cols">
                <div class="record-col">
                  <div class="col-label">{lLabel}</div>
                  <pre class="record-text">{lVal.slice(0, 400)}{lVal.length > 400 ? '…' : ''}</pre>
                </div>
                <div class="record-col">
                  <div class="col-label">{rLabel}</div>
                  <pre class="record-text">{rVal.slice(0, 400)}{rVal.length > 400 ? '…' : ''}</pre>
                </div>
              </div>
              <div class="record-actions">
                <button class="btn btn-ghost" style="color:var(--yellow); font-size:12px"
                  onclick={() => openDetail(item, i, 'flag')}>
                  ⚑ Signaler comme erroné
                </button>
              </div>
            </div>
          {/each}
        </div>
      {/if}
    {/if}
  {/if}
</div>

<!-- ── Detail modal ───────────────────────────────────────────────────────── -->
{#if detailRecord !== null}
  <div class="modal-backdrop" onclick={() => detailRecord = null} role="presentation">
    <div class="modal" onclick={(e) => e.stopPropagation()} role="dialog" aria-modal="true">
      <div class="modal-title">
        {detailMode === 'rescue' ? '↩ Rescaper l\'enregistrement' : '⚑ Signaler l\'enregistrement'}
        {#if detailGlobalIdx !== null}<span class="mono text-muted" style="font-size:13px; margin-left:8px">#{detailGlobalIdx}</span>{/if}
      </div>

      {#if detailMode === 'rescue'}
        <p style="font-size:12px; color:var(--text-dim); margin-bottom:16px">
          Cet enregistrement a été rejeté automatiquement par le pipeline. Si vous pensez que c'est un faux positif,
          vous pouvez le corriger et le réintégrer dans les données qualifiées.
        </p>
        {#each Object.entries(detailCorrections) as [field, val]}
          <label class="form-field" style="margin-top:12px">
            <span class="field-label">{field}</span>
            <textarea bind:value={detailCorrections[field]} class="textarea"
              rows={['code','completion'].includes(field) ? 8 : 3}></textarea>
          </label>
        {/each}
      {:else}
        <p style="font-size:12px; color:var(--text-dim); margin-bottom:16px">
          Cet enregistrement semble incorrect mais a passé le pipeline automatique.
          Le signaler l'exclut des données qualifiées et aide à améliorer les seuils.
        </p>
      {/if}

      <label class="form-field" style="margin-top:12px">
        <span class="field-label">Note (optionnelle)</span>
        <input bind:value={detailNote} class="input-text" placeholder="Ex: code invalide, hors sujet, truncated…" />
      </label>

      <div style="display:flex; gap:8px; margin-top:20px; justify-content:flex-end">
        <button class="btn btn-ghost" onclick={() => detailRecord = null}>Annuler</button>
        <button class="btn btn-primary" onclick={submitDetail}>
          {detailMode === 'rescue' ? '↩ Confirmer la récupération' : '⚑ Confirmer le signalement'}
        </button>
      </div>
    </div>
  </div>
{/if}

<style>
  .input { background:var(--surface-2); color:var(--text); border:1px solid var(--border); border-radius:var(--radius); padding:4px 10px; font-size:13px; }
  .input-text { background:var(--surface-2); color:var(--text); border:1px solid var(--border); border-radius:var(--radius); padding:6px 10px; font-size:13px; width:100%; }

  /* Summary grid */
  .summary-grid { display:flex; gap:12px; flex-wrap:wrap; }
  .summary-card { background:var(--surface-1); border:1px solid var(--border); border-radius:var(--radius); padding:14px 18px; min-width:110px; display:flex; flex-direction:column; gap:2px; }
  .sc-value { font-size:22px; font-weight:700; font-family:var(--font-mono); }
  .sc-label { font-size:11px; color:var(--text-muted); text-transform:uppercase; letter-spacing:.04em; }
  .sc-pct { font-size:11px; color:var(--text-dim); }

  /* Step bars */
  .step-bars { display:flex; flex-direction:column; gap:6px; }
  .step-bar-row { display:flex; align-items:center; gap:10px; }
  .step-name { font-size:12px; color:var(--text-dim); width:100px; flex-shrink:0; }
  .step-bar-track { flex:1; height:6px; background:var(--surface-2); border-radius:3px; overflow:hidden; }
  .step-bar-fill { height:100%; background:var(--red); border-radius:3px; transition:width .3s; }
  .step-count { font-size:12px; color:var(--text-muted); width:40px; text-align:right; }

  /* Tabs */
  .tab-bar { display:flex; gap:4px; border-bottom:1px solid var(--border); padding-bottom:1px; }
  .tab-btn { background:none; border:none; padding:8px 14px; font-size:13px; color:var(--text-dim); cursor:pointer; border-bottom:2px solid transparent; margin-bottom:-1px; display:flex; align-items:center; gap:6px; }
  .tab-btn.active { color:var(--text); border-bottom-color:var(--accent); }
  .tab-badge { font-size:10px; font-weight:700; padding:1px 5px; border-radius:999px; }
  .tab-badge.red   { background:rgba(248,81,73,.15); color:var(--red); }
  .tab-badge.green { background:rgba(63,185,80,.15); color:var(--green); }

  /* Step filter */
  .step-filter-btn { background:var(--surface-2); border:1px solid var(--border); border-radius:var(--radius); padding:3px 10px; font-size:12px; color:var(--text-dim); cursor:pointer; display:flex; align-items:center; gap:5px; }
  .step-filter-btn.active { background:rgba(31,111,235,.15); color:var(--accent); border-color:var(--accent); }
  .mini-count { font-size:10px; background:var(--surface-1); padding:0 4px; border-radius:3px; }

  /* Records */
  .record-list { display:flex; flex-direction:column; gap:10px; }
  .record-card { padding:12px 14px; display:flex; flex-direction:column; gap:8px; }
  .record-header { display:flex; align-items:center; justify-content:space-between; gap:8px; flex-wrap:wrap; }
  .schema-badge { font-size:10px; font-weight:700; padding:2px 7px; border-radius:999px; background:var(--surface-2); color:var(--text-dim); text-transform:uppercase; letter-spacing:.04em; }
  .schema-doc_crawl      { background:rgba(74,222,128,.12); color:var(--green); }
  .schema-multilang_wiki { background:rgba(99,179,237,.12); color:#63b3ed; }
  .schema-web_crawl      { background:rgba(251,146,60,.12); color:#fb923c; }
  .lang-badge    { background:rgba(99,179,237,.12); color:#63b3ed; }
  .adapter-badge { background:rgba(168,85,247,.12); color:#a855f7; }
  .reject-reason { font-size:11px; color:var(--red); font-family:var(--font-mono); }
  .quality-badge { font-size:11px; font-weight:600; }
  .source-link { font-size:11px; color:var(--text-dim); text-decoration:none; flex-shrink:0; }
  .source-link:hover { color:var(--accent); }
  .record-cols { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
  .record-col { display:flex; flex-direction:column; gap:4px; }
  .col-label { font-size:11px; color:var(--text-muted); text-transform:uppercase; letter-spacing:.05em; }
  .record-text { font-family:var(--font-mono); font-size:11px; background:var(--surface-2); padding:6px 8px; border-radius:4px; white-space:pre-wrap; word-break:break-all; margin:0; max-height:160px; overflow-y:auto; }
  .record-actions { display:flex; gap:8px; }
  .pagination { display:flex; align-items:center; gap:12px; margin-top:14px; justify-content:center; }

  /* Modal */
  .modal-backdrop { position:fixed; inset:0; background:rgba(0,0,0,0.6); display:flex; align-items:center; justify-content:center; z-index:100; }
  .modal { background:var(--surface-1); border:1px solid var(--border); border-radius:var(--radius); padding:24px; width:680px; max-width:95vw; max-height:85vh; overflow-y:auto; }
  .modal-title { font-size:16px; font-weight:700; margin-bottom:4px; }
  .form-field { display:flex; flex-direction:column; gap:5px; }
  .field-label { font-size:11px; color:var(--text-muted); font-weight:600; text-transform:uppercase; letter-spacing:.04em; }
  .textarea { background:var(--surface-2); color:var(--text); border:1px solid var(--border); border-radius:var(--radius); padding:7px; font-family:var(--font-mono); font-size:12px; resize:vertical; width:100%; }
  .banner-error { background:var(--red-dim); border:1px solid rgba(247,84,84,.3); border-radius:var(--radius); padding:10px 14px; font-size:13px; color:var(--red); }

  /* Score histogram */
  .score-hist { display:flex; align-items:flex-end; gap:4px; height:80px; }
  .hist-col { display:flex; flex-direction:column; align-items:center; flex:1; height:100%; position:relative; }
  .hist-bar-wrap { flex:1; width:100%; display:flex; align-items:flex-end; position:relative; }
  .hist-bar { width:100%; border-radius:2px 2px 0 0; transition:height .3s; min-height:2px; }
  .hist-threshold { position:absolute; top:0; bottom:0; left:50%; width:2px; background:var(--accent); opacity:0.6; border-radius:1px; }
  .hist-label { font-size:9px; color:var(--text-dim); margin-top:2px; }
  .hist-count { font-size:9px; color:var(--text-muted); }

  /* Diagnostic grid */
  .diag-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(280px, 1fr)); gap:12px; margin-bottom:20px; }
  .diag-card { padding:14px 16px; display:flex; flex-direction:column; gap:4px; }
  .sev-badge { font-size:10px; font-weight:700; padding:2px 8px; border-radius:999px; white-space:nowrap; flex-shrink:0; }

  /* Bicolor bar */
  .bicolor-bar { display:flex; height:6px; border-radius:3px; overflow:hidden; background:var(--surface-2); margin-top:8px; }
  .bicolor-accept { background:var(--green); transition:width .3s; }
  .bicolor-reject { background:var(--red); opacity:.6; transition:width .3s; }

  /* Recommendation box */
  .rec-box { background:var(--surface-2); border:1px solid var(--border); border-radius:var(--radius); padding:7px 10px; font-size:11px; color:var(--text-dim); display:flex; gap:6px; align-items:flex-start; }
  .rec-icon { color:var(--accent); font-weight:700; flex-shrink:0; }

  /* Card types from global CSS */
  .card.info { border-left:3px solid var(--accent); }
</style>
