<script lang="ts">
  import { goto } from '$app/navigation';
  import { advisorRecommend, advisorAlternatives, advisorEvaluateSelection } from '$lib/api';
  import type { AdvisorRecommendation, AdvisorAdapter, SelectionEval } from '$lib/types';
  import AdapterCard from '$lib/components/AdapterCard.svelte';
  import Topbar from '$lib/components/Topbar.svelte';
  import SovereigntyBadge from '$lib/components/SovereigntyBadge.svelte';
  import { setWizardHandoff } from '$lib/stores/wizard.svelte';

  // ── State ──────────────────────────────────────────────────────────────────
  let need  = $state('');
  let model = $state('us.anthropic.claude-sonnet-4-6');
  let loading = $state(false);
  let error   = $state('');
  let result: AdvisorRecommendation | null = $state(null);

  let adapters = $state<AdvisorAdapter[]>([]);
  let orchestrator = $state<AdvisorAdapter | null>(null);

  let showAllGlobalSources = $state(false);
  let altTarget   = $state<AdvisorAdapter | null>(null);
  let altLoading  = $state(false);
  let altError    = $state('');
  let alternatives = $state<AdvisorAdapter[]>([]);

  let sourcesAdapter = $state<AdvisorAdapter | null>(null);

  let selEval    = $state<SelectionEval | null>(null);
  let evalLoading = $state(false);

  const models = [
    { id: 'us.anthropic.claude-sonnet-4-6',              label: 'Claude Sonnet 4.6 — via AWS Bedrock (recommandé)' },
    { id: 'us.anthropic.claude-opus-4-7',                label: 'Claude Opus 4.7 — via AWS Bedrock (plus précis)' },
    { id: 'us.anthropic.claude-haiku-4-5-20251001-v1:0', label: 'Claude Haiku 4.5 — via AWS Bedrock (rapide)' },
  ];

  // ── Derived ────────────────────────────────────────────────────────────────
  const selection = $derived([...adapters, ...(orchestrator ? [orchestrator] : [])]);
  const totalPairs = $derived(selection.reduce((s, a) => s + a.estimated_pairs, 0));
  const allSources = $derived(
    selection
      .flatMap(a => (a.data_sources ?? []).map(s => ({ source: s, adapter: a.name })))
      .filter((v, i, arr) => arr.findIndex(x => x.source === v.source) === i)
  );

  // ── Actions ────────────────────────────────────────────────────────────────
  async function analyse() {
    if (!need.trim()) return;
    loading = true; error = ''; result = null;
    altTarget = null; sourcesAdapter = null; selEval = null;
    try {
      result = await advisorRecommend(need.trim(), model);
      adapters     = [...result.recommendation.adapters];
      orchestrator = { ...result.recommendation.orchestrator };
      triggerEval();
    } catch (e) { error = (e as Error).message; }
    finally { loading = false; }
  }

  function removeAdapter(name: string) {
    if (altTarget?.name === name) { altTarget = null; alternatives = []; }
    if (sourcesAdapter?.name === name) sourcesAdapter = null;
    adapters = adapters.filter(a => a.name !== name);
    triggerEval();
  }

  function removeOrchestrator() {
    if (altTarget?.name === orchestrator?.name) { altTarget = null; alternatives = []; }
    if (sourcesAdapter?.name === orchestrator?.name) sourcesAdapter = null;
    orchestrator = null;
    triggerEval();
  }

  async function openAlternatives(adapter: AdvisorAdapter) {
    altTarget    = adapter;
    altLoading   = true;
    altError     = '';
    alternatives = [];
    try {
      alternatives = await advisorAlternatives(need, adapter.name, adapter.description, selection, model);
    } catch (e) { altError = (e as Error).message; }
    finally { altLoading = false; }
  }

  function replaceWithAlternative(alt: AdvisorAdapter) {
    if (!altTarget) return;
    const clean: AdvisorAdapter = {
      name: alt.name, role: altTarget.role, description: alt.description,
      data_types: alt.data_types ?? [], data_sources: alt.data_sources ?? [],
      rag_recommended: alt.rag_recommended, estimated_pairs: alt.estimated_pairs,
    };
    if (altTarget.role === 'orchestrator') {
      orchestrator = clean;
    } else {
      adapters = adapters.map(a => a.name === altTarget!.name ? clean : a);
    }
    altTarget = null; alternatives = [];
    triggerEval();
  }

  let evalTimer: ReturnType<typeof setTimeout> | null = null;
  function triggerEval() {
    selEval = null;
    if (evalTimer) clearTimeout(evalTimer);
    if (selection.length === 0) return;
    evalTimer = setTimeout(runEval, 800);
  }

  async function runEval() {
    if (selection.length === 0) return;
    evalLoading = true;
    try {
      selEval = await advisorEvaluateSelection(need, selection, model);
    } catch { /* non-blocking */ }
    finally { evalLoading = false; }
  }

  function openSources(adapter: AdvisorAdapter) {
    sourcesAdapter = adapter;
  }

  function scoreColor(s: number): string {
    if (s >= 15) return 'var(--green)';
    if (s >= 10) return 'var(--accent)';
    return 'var(--red)';
  }

  function scoreBar(s: number): string {
    return `width:${(s / 20) * 100}%`;
  }

  function copyYaml() {
    const lines = [
      `multi_adapter: true`, `adapters:`,
      ...adapters.map(a =>
        `  - name: ${a.name}\n    role: ${a.role}\n    status: pending\n    rag: ${a.rag_recommended ?? false}\n    estimated_pairs: ${a.estimated_pairs}`
      ),
      ...(orchestrator ? [
        `  - name: ${orchestrator.name}\n    role: orchestrator\n    status: pending\n    rag: false\n    estimated_pairs: ${orchestrator.estimated_pairs}`
      ] : []),
    ];
    navigator.clipboard.writeText(lines.join('\n'));
  }

  function sourceUrl(src: string): string {
    return `https://www.google.com/search?q=${encodeURIComponent(src)}`;
  }
</script>

<Topbar crumbs={[{ label: '1 · Scoping' }, { label: 'Analyse du besoin' }]}>
  <SovereigntyBadge mode="cloud" label="🟠 Bedrock (scoping)" size="sm" />
  {#if result}
    <button class="btn btn-ghost btn-sm" onclick={copyYaml}>Copier YAML</button>
    <button class="btn btn-primary btn-sm" onclick={() => { result = null; need = ''; adapters = []; orchestrator = null; altTarget = null; sourcesAdapter = null; selEval = null; }}>
      ↺ Nouveau besoin
    </button>
  {/if}
</Topbar>

<div class="content">
  <div class="phase0-layout" class:has-panel={altTarget || sourcesAdapter}>

    <!-- ── Col 1 : input + résumé ── -->
    <div class="col-input">
      <div class="card mb-16">
        <div class="card-title mb-8">Décrivez le besoin client</div>
        <p class="hint mb-12">Langage naturel — domaine, contraintes, déploiement.</p>
        <textarea
          class="form-textarea need-input"
          bind:value={need}
          placeholder="Ex: Système d'aide à la rédaction de contrats pour un cabinet d'avocats…"
          rows={6}
          disabled={loading}
        ></textarea>
        <div class="form-group mt-12">
          <label class="form-label">Modèle</label>
          <select class="form-select" bind:value={model} disabled={loading}>
            {#each models as m}<option value={m.id}>{m.label}</option>{/each}
          </select>
        </div>
        <button class="btn btn-primary w-full mt-12" onclick={analyse} disabled={loading || !need.trim()}>
          {#if loading}<span class="spinner"></span> Analyse en cours…{:else}◈ Analyser le besoin{/if}
        </button>
        {#if error}<div class="api-error mt-12">{error}</div>{/if}
      </div>

      {#if result}
        <!-- Résumé sélection -->
        <div class="card mb-16">
          <div class="card-title mb-12">Sélection actuelle</div>
          <div class="summary-grid">
            <div class="summary-item">
              <span class="summary-value">{adapters.length}</span>
              <span class="summary-label">Spécialistes</span>
            </div>
            <div class="summary-item">
              <span class="summary-value">{orchestrator ? 1 : 0}</span>
              <span class="summary-label">Orchestrateur</span>
            </div>
            <div class="summary-item">
              <span class="summary-value">~{totalPairs.toLocaleString()}</span>
              <span class="summary-label">Paires estimées</span>
            </div>
          </div>
          {#if result.recommendation.notes}
            <div class="notes mt-12">{result.recommendation.notes}</div>
          {/if}
          {#if result.recommendation.software_components?.length}
            <div class="software-components mt-12">
              <div class="software-components__title">Composants logiciels (déterministes — pas d'IA)</div>
              <ul class="software-components__list">
                {#each result.recommendation.software_components as comp}
                  <li>{comp}</li>
                {/each}
              </ul>
            </div>
          {/if}
        </div>

        <!-- Score équipe -->
        <div class="card mb-16">
          <div class="card-header mb-12">
            <div class="card-title">Score équipe</div>
            {#if evalLoading}<span class="spinner spinner-sm"></span>{/if}
          </div>
          {#if selEval}
            <div class="team-score-hero">
              <span class="team-score-value" style="color:{scoreColor(selEval.overall)}">{selEval.overall.toFixed(1)}</span>
              <span class="team-score-label">/20</span>
            </div>
            <div class="score-bars mt-12">
              {#each [['Couverture', selEval.coverage], ['Cohésion', selEval.cohesion], ['Équilibre', selEval.balance], ['Orchestrabilité', selEval.orchestrability]] as [label, val]}
                <div class="score-bar-row">
                  <span class="score-bar-label">{label}</span>
                  <div class="score-bar-track">
                    <div class="score-bar-fill" style="{scoreBar(val as number)};background:{scoreColor(val as number)}"></div>
                  </div>
                  <span class="score-bar-val" style="color:{scoreColor(val as number)}">{val}</span>
                </div>
              {/each}
            </div>
            {#if selEval.gaps.length}
              <div class="eval-section mt-12">
                <span class="meta-label">Lacunes détectées</span>
                <div class="tags mt-4">
                  {#each selEval.gaps as g}<span class="tag tag-warn">{g}</span>{/each}
                </div>
              </div>
            {/if}
            {#if selEval.overlaps.length}
              <div class="eval-section mt-8">
                <span class="meta-label">Risques de chevauchement</span>
                <div class="tags mt-4">
                  {#each selEval.overlaps as pair}<span class="tag tag-danger">{pair.join(' ↔ ')}</span>{/each}
                </div>
              </div>
            {/if}
            {#if selEval.recommendation}
              <div class="eval-reco mt-12">{selEval.recommendation}</div>
            {/if}
          {:else if !evalLoading}
            <p class="hint">Sera calculé après l'analyse.</p>
          {/if}
        </div>

        <!-- Sources globales -->
        <div class="card">
          <div class="card-title mb-12">Sources ({allSources.length})</div>
          <div class="sources-grid">
            {#each allSources.slice(0, showAllGlobalSources ? undefined : 5) as { source, adapter }}
              <a href={sourceUrl(source)} target="_blank" rel="noopener noreferrer" class="source-chip" title="{adapter}">
                <span class="source-chip-name">{source}</span>
                <span class="source-chip-icon">↗</span>
              </a>
            {/each}
          </div>
          {#if allSources.length > 5}
            <button class="sources-toggle mt-8" onclick={() => showAllGlobalSources = !showAllGlobalSources}>
              {showAllGlobalSources ? '▾ Réduire' : `▸ Voir les ${allSources.length - 5} autres sources`}
            </button>
          {/if}
        </div>
      {/if}
    </div>

    <!-- ── Col 2 : adapters ── -->
    {#if result}
      <div class="col-adapters">
        <div class="card">
          <div class="card-header mb-12">
            <div class="card-title">Architecture recommandée</div>
            <span class="hint">{selection.length} adapter{selection.length > 1 ? 's' : ''}</span>
          </div>

          <div class="adapters-list">
            {#each adapters as adapter (adapter.name)}
              <AdapterCard
                {adapter}
                active={altTarget?.name === adapter.name || sourcesAdapter?.name === adapter.name}
                onSources={openSources}
                onAlternatives={openAlternatives}
                onRemove={removeAdapter}
              />
            {/each}

            {#if orchestrator}
              <AdapterCard
                adapter={orchestrator}
                active={altTarget?.name === orchestrator.name || sourcesAdapter?.name === orchestrator.name}
                onSources={openSources}
                onAlternatives={openAlternatives}
                onRemove={() => removeOrchestrator()}
              />
            {/if}

            {#if selection.length === 0}
              <div class="empty-state">Tous les adapters supprimés. ↺ Recommencez.</div>
            {/if}
          </div>

          {#if selection.length > 0}
            <div class="validation-bar mt-16">
              <button
                class="btn btn-primary"
                onclick={() => { setWizardHandoff(need, adapters, orchestrator); goto('/start'); }}
              >✓ Valider et créer le projet</button>
            </div>
          {/if}
        </div>
      </div>

      <!-- ── Col 3 : panel contextuel ── -->
      {#if altTarget || sourcesAdapter}
        <div class="col-panel">

          {#if altTarget}
            <div class="card">
              <div class="card-header mb-12">
                <div class="card-title">Alternatives — <span class="mono">{altTarget.name}</span></div>
                <button class="icon-btn" onclick={() => { altTarget = null; alternatives = []; }}>✕</button>
              </div>

              {#if altLoading}
                <div class="panel-loading"><span class="spinner"></span> Génération + scoring…</div>
              {:else if altError}
                <div class="api-error">{altError}</div>
              {:else if alternatives.length}
                <div class="adapters-list">
                  {#each alternatives as alt}
                    <div class="alt-wrapper">
                      <AdapterCard adapter={alt} />
                      <button class="btn-use" onclick={() => replaceWithAlternative(alt)}>↩ Utiliser cet adapter</button>
                    </div>
                  {/each}
                </div>
              {/if}
            </div>
          {/if}

          {#if sourcesAdapter}
            <div class="card">
              <div class="card-header mb-12">
                <div class="card-title">Sources — <span class="mono">{sourcesAdapter.name}</span></div>
                <button class="icon-btn" onclick={() => sourcesAdapter = null}>✕</button>
              </div>
              <div class="eval-section mb-16">
                <span class="meta-label">Types de données</span>
                <div class="tags mt-4">
                  {#each (sourcesAdapter.data_types ?? []) as dt}<span class="tag">{dt}</span>{/each}
                </div>
              </div>
              <div class="eval-section">
                <span class="meta-label">Sources à explorer</span>
                <div class="sources-grid mt-8">
                  {#each (sourcesAdapter.data_sources ?? []) as src}
                    <a href={sourceUrl(src)} target="_blank" rel="noopener noreferrer" class="source-chip">
                      <span class="source-chip-name">{src}</span>
                      <span class="source-chip-icon">↗</span>
                    </a>
                  {/each}
                </div>
              </div>
            </div>
          {/if}

        </div>
      {/if}
    {/if}

  </div>
</div>

<style>
  .phase0-layout {
    display: grid;
    grid-template-columns: 340px 1fr;
    gap: 20px;
    align-items: start;
  }
  .phase0-layout.has-panel { grid-template-columns: 340px 1fr 360px; }

  .need-input { resize: vertical; min-height: 140px; }
  .hint { font-size: 12px; color: var(--text-muted); line-height: 1.5; margin: 0; }
  .mt-4  { margin-top: 4px; }  .mt-8  { margin-top: 8px; }
  .mt-12 { margin-top: 12px; } .mt-16 { margin-top: 16px; }
  .mb-12 { margin-bottom: 12px; } .mb-16 { margin-bottom: 16px; }
  .ml-4  { margin-left: 4px; }  .ml-6  { margin-left: 6px; }

  /* Summary */
  .summary-grid { display: grid; grid-template-columns: repeat(3,1fr); gap:10px; }
  .summary-item {
    display:flex; flex-direction:column; align-items:center;
    padding:10px 8px; background:var(--surface-2); border-radius:var(--radius); gap:3px;
  }
  .summary-value { font-size:20px; font-weight:700; color:var(--accent); }
  .summary-label { font-size:11px; color:var(--text-muted); text-align:center; }
  .notes { font-size:12px; color:var(--text-muted); background:var(--surface-2); border-radius:var(--radius); padding:10px 14px; font-style:italic; line-height:1.5; }
  .software-components { background:rgba(251,146,60,.08); border:1px solid rgba(251,146,60,.25); border-radius:var(--radius); padding:10px 14px; }
  .software-components__title { font-size:11px; font-weight:600; color:#fb923c; text-transform:uppercase; letter-spacing:.04em; margin-bottom:6px; }
  .software-components__list { margin:0; padding-left:16px; font-size:12px; color:var(--text-muted); line-height:1.7; }

  /* Team score */
  .team-score-hero { display:flex; align-items:baseline; gap:4px; }
  .team-score-value { font-size:38px; font-weight:800; line-height:1; }
  .team-score-label { font-size:18px; color:var(--text-muted); }
  .score-bars { display:flex; flex-direction:column; gap:6px; }
  .score-bar-row { display:flex; align-items:center; gap:8px; }
  .score-bar-label { font-size:11px; color:var(--text-muted); width:90px; flex-shrink:0; }
  .score-bar-track { flex:1; height:6px; background:var(--surface-2); border-radius:3px; overflow:hidden; }
  .score-bar-fill  { height:100%; border-radius:3px; transition:width 400ms ease; }
  .score-bar-val   { font-size:11px; font-weight:600; width:24px; text-align:right; }
  .eval-section { display:flex; flex-direction:column; }
  .eval-reco { font-size:12px; color:var(--text-muted); background:var(--surface-2); border-radius:var(--radius); padding:8px 12px; font-style:italic; line-height:1.5; }

  .sources-toggle {
    background: none; border: none; padding: 0;
    font-size: 11px; color: var(--text-muted); cursor: pointer;
    text-decoration: underline; text-underline-offset: 2px;
    transition: color 150ms; display: block;
  }
  .sources-toggle:hover { color: var(--accent); }

  /* Sources grille compacte */
  .sources-grid { display:flex; flex-wrap:wrap; gap:6px; }
  .source-chip {
    display:inline-flex; align-items:center; gap:4px;
    padding:4px 10px; border-radius:999px;
    background:var(--surface-2); border:1px solid var(--border);
    text-decoration:none; font-size:11px; color:var(--accent);
    transition:border-color 150ms, background 150ms;
    max-width:180px;
  }
  .source-chip:hover { border-color:var(--accent); background:var(--accent-dim); }
  .source-chip-name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .source-chip-icon { font-size:10px; color:var(--text-muted); flex-shrink:0; }

  /* Adapters */
  .adapters-list { display:flex; flex-direction:column; gap:10px; }

  .alt-wrapper { display:flex; flex-direction:column; gap:6px; }
  .btn-use {
    align-self:flex-end; background:none; border:1px solid var(--border);
    border-radius:var(--radius); padding:4px 12px; font-size:12px; cursor:pointer;
    color:var(--accent); transition:border-color 150ms, background 150ms;
  }
  .btn-use:hover { border-color:var(--accent); background:var(--accent-dim); }

  /* Tags */
  .tags { display:flex; flex-wrap:wrap; gap:4px; }
  .tag { font-size:11px; padding:2px 8px; border-radius:999px; background:var(--surface-2); color:var(--text-muted); border:1px solid var(--border); }
  .tag-warn   { color:#f59e0b; border-color:rgba(245,158,11,.3); }
  .tag-danger { color:var(--red); border-color:rgba(255,80,80,.3); }

  /* Meta labels */
  .meta-label { font-size:11px; text-transform:uppercase; letter-spacing:0.05em; color:var(--text-muted); }
  .meta-label.green { color:var(--green); }
  .meta-label.red   { color:var(--red); }

  .icon-btn {
    background:none; border:1px solid var(--border); border-radius:4px;
    padding:2px 7px; font-size:12px; cursor:pointer; color:var(--text-muted);
    transition:border-color 150ms,color 150ms,background 150ms; white-space:nowrap;
  }
  .icon-btn:hover        { border-color:var(--accent); color:var(--accent); }
  .icon-btn.danger:hover { border-color:var(--red); color:var(--red); }
  .icon-btn.accent       { color:var(--accent); border-color:color-mix(in srgb,var(--accent) 40%,transparent); }
  .icon-btn.accent:hover { background:var(--accent-dim); }

  .empty-state { text-align:center; padding:24px; color:var(--text-muted); font-size:13px; }
  .validation-bar { border-top:1px solid var(--border); padding-top:16px; }
  .panel-loading { display:flex; align-items:center; gap:10px; color:var(--text-muted); font-size:13px; padding:20px 0; }
  .mono { font-family:var(--font-mono); font-size:0.9em; }
  .btn-sm { padding:4px 12px; font-size:12px; }

  .spinner {
    display:inline-block; width:14px; height:14px;
    border:2px solid rgba(255,255,255,0.3); border-top-color:white;
    border-radius:50%; animation:spin 0.7s linear infinite; vertical-align:middle;
  }
  .spinner-sm { width:12px; height:12px; border-width:2px; }
  @keyframes spin { to { transform:rotate(360deg); } }

  .api-error {
    background:rgba(255,80,80,0.1); border:1px solid var(--red);
    border-radius:var(--radius); padding:10px 14px; font-size:13px; color:var(--red); font-family:var(--font-mono);
  }

  @media (max-width:1300px) { .phase0-layout, .phase0-layout.has-panel { grid-template-columns:1fr; } }
</style>
