<script lang="ts">
  import { goto } from '$app/navigation';
  import { page } from '$app/stores';
  import { onMount, tick } from 'svelte';
  import { advisorRecommend, advisorAlternatives, advisorEvaluateSelection, advisorGetRecommendation, advisorFillGap, advisorResolveOverlap } from '$lib/api';
  import type { AdvisorRecommendation, AdvisorAdapter, SelectionEval, OverlapResolution } from '$lib/types';
  import AdapterCard from '$lib/components/AdapterCard.svelte';
  import Topbar from '$lib/components/Topbar.svelte';
  import SovereigntyBadge from '$lib/components/SovereigntyBadge.svelte';
  import { setWizardHandoff } from '$lib/stores/wizard.svelte';
  import SectionTabs from '$lib/components/SectionTabs.svelte';

  const donneesTabs = [
    { href: '/advisor',       label: 'Cadrage' },
    { href: '/data/ingest',   label: 'Ingestion' },
    { href: '/data/pipeline', label: 'Pipeline' },
    { href: '/data/review',   label: 'Révision' },
    { href: '/data/search',   label: 'Audit' },
  ];

  // ── State ──────────────────────────────────────────────────────────────────
  const projectId = $derived($page.url.searchParams.get('project'));
  const readOnly  = $derived(false);

  let need  = $state('');
  let model = $state('us.anthropic.claude-sonnet-4-6');
  let loading = $state(false);
  let error   = $state('');
  let result: AdvisorRecommendation | null = $state(null);

  let adapters = $state<AdvisorAdapter[]>([]);
  let orchestrator = $state<AdvisorAdapter | null>(null);
  let altTarget   = $state<AdvisorAdapter | null>(null);
  let altLoading  = $state(false);
  let altError    = $state('');
  let alternatives = $state<AdvisorAdapter[]>([]);
  let selEval    = $state<SelectionEval | null>(null);
  let evalLoading = $state(false);
  let evalError   = $state('');

  // Refine bar
  let refineInput   = $state('');
  let refineLoading = $state(false);
  let refineError   = $state('');

  // Gap actions
  let gapTarget  = $state<string | null>(null);
  let gapLoading = $state(false);
  let gapError   = $state('');
  let gapResult  = $state<AdvisorAdapter | null>(null);
  let fillAllLoading = $state(false);
  let fillAllResults = $state<AdvisorAdapter[]>([]);
  let filledGapsList = $state<string[]>([]);   // gaps being addressed in current fillAll
  let dismissedGaps  = $state<Set<string>>(new Set()); // gaps dismissed by user (survive re-eval)

  // Selection dirty flag — prevents auto-re-eval after manual apply actions
  let selectionDirty = $state(false);

  // Software components accordion
  let swOpen     = $state(false);
  let swExpanded = $state<string | null>(null);

  // Overlap actions
  let overlapTarget      = $state<[string, string] | null>(null);
  let overlapLoading     = $state(false);
  let overlapError       = $state('');
  let overlapResolutions = $state<OverlapResolution[]>([]);
  let resolveAllLoading  = $state(false);
  interface ResolveAllEntry { pair: [string, string]; resolution: OverlapResolution; }
  let resolveAllResults  = $state<ResolveAllEntry[]>([]);

  const models = [
    { id: 'us.anthropic.claude-sonnet-4-6',              label: 'Claude Sonnet 4.6 — via AWS Bedrock (recommandé)' },
    { id: 'us.anthropic.claude-opus-4-7',                label: 'Claude Opus 4.7 — via AWS Bedrock (plus précis)' },
    { id: 'us.anthropic.claude-haiku-4-5-20251001-v1:0', label: 'Claude Haiku 4.5 — via AWS Bedrock (rapide)' },
  ];

  onMount(async () => {
    if (!projectId) return;
    loading = true;
    try {
      const r = await advisorGetRecommendation(projectId) as AdvisorRecommendation & { advisor_need?: string };
      result       = r;
      adapters     = [...(r.recommendation.adapters ?? [])];
      orchestrator = r.recommendation.orchestrator && Object.keys(r.recommendation.orchestrator).length > 0
        ? { ...r.recommendation.orchestrator }
        : null;
      if ((r as any).advisor_need) need = (r as any).advisor_need;
      else if (r.recommendation.notes) need = r.recommendation.notes;
      // Build list directly — don't read $derived inside async context
      const currentNeed = (r as any).advisor_need || r.recommendation.notes || '';
      const currentList: AdvisorAdapter[] = [
        ...(r.recommendation.adapters ?? []),
        ...(r.recommendation.orchestrator && Object.keys(r.recommendation.orchestrator).length > 0
          ? [r.recommendation.orchestrator] : []),
      ];
      if (currentNeed && currentList.length > 0) {
        await runEvalWith(currentNeed, currentList);
      }
    } catch (e) { error = (e as Error).message; }
    finally { loading = false; }
  });

  // ── Derived ────────────────────────────────────────────────────────────────
  const selection = $derived([...adapters, ...(orchestrator ? [orchestrator] : [])]);
  const totalPairs = $derived(selection.reduce((s, a) => s + a.estimated_pairs, 0));

  // ── Actions ────────────────────────────────────────────────────────────────
  async function analyse() {
    if (!need.trim()) return;
    loading = true; error = ''; result = null;
    altTarget = null; selEval = null;
    dismissedGaps = new Set(); filledGapsList = [];
    try {
      result = await advisorRecommend(need.trim(), model);
      adapters     = [...result.recommendation.adapters];
      orchestrator = { ...result.recommendation.orchestrator };
      triggerEval();
    } catch (e) { error = (e as Error).message; }
    finally { loading = false; }
  }

  async function refineSelection() {
    const q = refineInput.trim();
    if (!q || refineLoading) return;
    refineLoading = true; refineError = '';
    dismissedGaps = new Set(); filledGapsList = [];
    const augmentedNeed = `${need}\n\n[RAFFINEMENT DEMANDÉ]: ${q}\nSélection actuelle : ${selection.map(a => a.name).join(', ')}. Modifie uniquement ce qui est demandé, conserve le reste.`;
    try {
      const r = await advisorRecommend(augmentedNeed, model);
      adapters     = [...r.recommendation.adapters];
      orchestrator = r.recommendation.orchestrator && Object.keys(r.recommendation.orchestrator).length > 0
        ? { ...r.recommendation.orchestrator } : null;
      result = r;
      refineInput = '';
      triggerEval();
    } catch (e) { refineError = (e as Error).message; }
    finally { refineLoading = false; }
  }

  function removeAdapter(name: string) {
    if (altTarget?.name === name) { altTarget = null; alternatives = []; }
    adapters = adapters.filter(a => a.name !== name);
    markDirty();
  }

  function removeOrchestrator() {
    if (altTarget?.name === orchestrator?.name) { altTarget = null; alternatives = []; }
    orchestrator = null;
    markDirty();
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
    const replaced: AdvisorAdapter = {
      ...alt,
      role: altTarget.role,
      source_configs: altTarget.source_configs,
    };
    if (altTarget.role === 'orchestrator') orchestrator = replaced;
    else adapters = adapters.map(a => a.name === altTarget!.name ? replaced : a);
    altTarget = null; alternatives = [];
    markDirty();
  }

  function markDirty() {
    selectionDirty = true;
    // Do NOT clear selEval here — keep existing scores visible until user re-evaluates
  }

  // triggerEval is kept for post-analyse / post-refine only (automatic, not after manual apply)
  let evalTimer: ReturnType<typeof setTimeout> | null = null;
  function triggerEval() {
    selEval = null; selectionDirty = false;
    if (evalTimer) clearTimeout(evalTimer);
    if (selection.length === 0) return;
    evalTimer = setTimeout(runEval, 800);
  }

  async function runEvalWith(evalNeed: string, evalSelection: AdvisorAdapter[]) {
    if (evalSelection.length === 0) return;
    evalLoading = true;
    evalError = '';
    try {
      selEval = await advisorEvaluateSelection(evalNeed, evalSelection, model);
    } catch (e) {
      evalError = (e as Error).message;
      console.error('evaluate-selection failed:', e);
    } finally { evalLoading = false; }
  }

  async function runEval() {
    await runEvalWith(need, selection);
  }

  // ── Gap actions ────────────────────────────────────────────────────────────
  async function openFillGap(gap: string) {
    if (gapTarget === gap) { gapTarget = null; gapResult = null; return; }
    altTarget = null; overlapTarget = null;
    gapTarget = gap; gapLoading = true; gapError = ''; gapResult = null;
    try {
      gapResult = await advisorFillGap(need, gap, selection, model);
    } catch (e) { gapError = (e as Error).message; }
    finally { gapLoading = false; }
  }

  function applyGapAdapter() {
    if (!gapResult || !gapTarget) return;
    adapters = [...adapters, { ...gapResult, role: 'specialist' as const }];
    dismissedGaps = new Set([...dismissedGaps, gapTarget]);
    gapTarget = null; gapResult = null;
    markDirty();
  }

  function dismissGap(gap: string) {
    dismissedGaps = new Set([...dismissedGaps, gap]);
    if (gapTarget === gap) { gapTarget = null; gapResult = null; }
  }

  async function fillAllGaps() {
    const visibleGaps = (selEval?.gaps ?? []).filter(g => !dismissedGaps.has(g));
    if (!visibleGaps.length) return;
    altTarget = null; overlapTarget = null;
    gapTarget = null; gapResult = null;
    fillAllLoading = true; fillAllResults = [];
    filledGapsList = [...visibleGaps];
    const snap = [...selection];
    try {
      const results = await Promise.all(
        visibleGaps.map(g => advisorFillGap(need, g, snap, model).catch(() => null))
      );
      fillAllResults = results.filter((r): r is AdvisorAdapter => r !== null);
    } finally { fillAllLoading = false; }
  }

  function applyAllGapAdapters() {
    adapters = [...adapters, ...fillAllResults.map(r => ({ ...r, role: 'specialist' as const }))];
    dismissedGaps = new Set([...dismissedGaps, ...filledGapsList]);
    fillAllResults = [];
    filledGapsList = [];
    markDirty();
  }

  // ── Overlap actions ────────────────────────────────────────────────────────
  async function openResolveOverlap(pair: [string, string]) {
    if (overlapTarget?.[0] === pair[0] && overlapTarget?.[1] === pair[1]) {
      overlapTarget = null; overlapResolutions = []; return;
    }
    altTarget = null; gapTarget = null;
    overlapTarget = pair; overlapLoading = true; overlapError = ''; overlapResolutions = [];
    const a = selection.find(x => x.name === pair[0]);
    const b = selection.find(x => x.name === pair[1]);
    if (!a || !b) { overlapLoading = false; return; }
    try {
      overlapResolutions = await advisorResolveOverlap(need, a, b, selection, model);
    } catch (e) { overlapError = (e as Error).message; }
    finally { overlapLoading = false; }
  }

  function applyOverlapResolution(res: OverlapResolution) {
    if (!overlapTarget) return;
    const [nameA, nameB] = overlapTarget;
    if (res.type === 'remove' || res.type === 'merge') {
      adapters = adapters.filter(a => a.name !== nameA && a.name !== nameB);
      if (orchestrator?.name === nameA || orchestrator?.name === nameB) orchestrator = null;
      if (res.adapters[0]) adapters = [...adapters, res.adapters[0]];
    } else {
      adapters = adapters.map(a => { const u = res.adapters.find(r => r.name === a.name); return u ?? a; });
      if (orchestrator) { const u = res.adapters.find(r => r.name === orchestrator!.name); if (u) orchestrator = u; }
    }
    overlapTarget = null; overlapResolutions = [];
    markDirty();
  }

  async function resolveAllOverlaps() {
    if (!selEval?.overlaps?.length) return;
    altTarget = null; gapTarget = null; overlapTarget = null;
    resolveAllLoading = true; resolveAllResults = [];
    const snap = [...selection];
    try {
      const pairs = selEval.overlaps as [string, string][];
      const results = await Promise.all(
        pairs.map(async pair => {
          const a = snap.find(x => x.name === pair[0]);
          const b = snap.find(x => x.name === pair[1]);
          if (!a || !b) return null;
          const resolutions = await advisorResolveOverlap(need, a, b, snap, model).catch(() => null);
          if (!resolutions?.length) return null;
          const best = resolutions.find(r => r.type === 'clarify') ?? resolutions.find(r => r.type === 'merge') ?? resolutions[0];
          return { pair, resolution: best } as ResolveAllEntry;
        })
      );
      resolveAllResults = results.filter((r): r is ResolveAllEntry => r !== null);
    } finally { resolveAllLoading = false; }
  }

  function applyAllOverlapResolutions() {
    for (const { pair, resolution } of resolveAllResults) {
      const [nameA, nameB] = pair;
      if (resolution.type === 'remove' || resolution.type === 'merge') {
        adapters = adapters.filter(a => a.name !== nameA && a.name !== nameB);
        if (orchestrator?.name === nameA || orchestrator?.name === nameB) orchestrator = null;
        if (resolution.adapters[0]) adapters = [...adapters, resolution.adapters[0]];
      } else {
        adapters = adapters.map(a => { const u = resolution.adapters.find(r => r.name === a.name); return u ?? a; });
        if (orchestrator) { const u = resolution.adapters.find(r => r.name === orchestrator!.name); if (u) orchestrator = u; }
      }
    }
    resolveAllResults = [];
    markDirty();
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


</script>

<SectionTabs tabs={donneesTabs} />
<Topbar crumbs={readOnly
  ? [{ label: 'Projets', href: '/projects' }, { label: projectId ?? '', href: `/projects/${projectId}` }, { label: 'Analyse du besoin' }]
  : [{ label: '1 · Scoping' }, { label: 'Analyse du besoin' }]
}>
  <SovereigntyBadge mode="cloud" label="🟠 Bedrock (scoping)" size="sm" />
  {#if result}
    <button class="btn btn-ghost btn-sm" onclick={copyYaml}>Copier YAML</button>
    {#if readOnly}
      <button class="btn btn-ghost btn-sm" onclick={() => goto('/advisor')}>+ Nouvelle analyse</button>
    {:else}
      <button class="btn btn-primary btn-sm" onclick={() => { result = null; need = ''; adapters = []; orchestrator = null; altTarget = null; selEval = null; dismissedGaps = new Set(); }}>
        ↺ Nouveau besoin
      </button>
    {/if}
  {/if}
</Topbar>

<div class="content">

  <!-- ── Refine bar — disponible dès qu'une sélection existe ── -->
  {#if result && selection.length > 0}
    <div class="refine-bar mb-16">
      <span class="refine-label">Modifier la sélection :</span>
      <input
        class="refine-input"
        bind:value={refineInput}
        placeholder="Ex: Ajoute un adapter pour la gestion des exceptions…"
        disabled={refineLoading}
        onkeydown={e => e.key === 'Enter' && refineSelection()}
      />
      <button class="btn btn-ghost btn-sm" onclick={refineSelection} disabled={refineLoading || !refineInput.trim()}>
        {#if refineLoading}<span class="spinner spinner-sm"></span>{:else}↺ Affiner{/if}
      </button>
    </div>
    {#if refineError}<div class="api-error mb-8">{refineError}</div>{/if}
  {/if}

  <div class="phase0-layout" class:has-panel={altTarget || gapTarget || overlapTarget}>

    <!-- ── Col 1 : input + résumé ── -->
    <div class="col-input">
      {#if !readOnly}
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
      {:else if loading}
        <div class="card mb-16"><span class="spinner"></span> Chargement de l'analyse…</div>
      {:else if error}
        <div class="api-error mb-16">{error}</div>
      {/if}

      {#if result}
        {#if result.recommendation.software_components?.length}
        <div class="card mb-16">
          <div class="sw-accordion">
              <button class="sw-accordion__header" onclick={() => swOpen = !swOpen}>
                <span class="sw-accordion__title">Composants logiciels (déterministes — pas d'IA)</span>
                <span class="sw-accordion__count">{result.recommendation.software_components.length}</span>
                <span class="sw-accordion__chevron" class:open={swOpen}>▾</span>
              </button>
              {#if swOpen}
                <ul class="sw-accordion__list">
                  {#each result.recommendation.software_components as comp}
                    {@const sep = comp.indexOf(' — ')}
                    {@const name = sep >= 0 ? comp.slice(0, sep) : comp}
                    {@const desc = sep >= 0 ? comp.slice(sep + 3) : ''}
                    <li class="sw-accordion__item">
                      <button class="sw-accordion__item-btn" onclick={() => swExpanded = swExpanded === comp ? null : comp}>
                        <span class="sw-accordion__item-name">{name}</span>
                        <span class="sw-accordion__item-chevron" class:open={swExpanded === comp}>▾</span>
                      </button>
                      {#if swExpanded === comp && desc}
                        <div class="sw-accordion__item-desc">{desc}</div>
                      {/if}
                    </li>
                  {/each}
                </ul>
              {/if}
          </div>
        </div>
        {/if}

        <!-- Score équipe -->
        <div class="card mb-16">
          <div class="card-header mb-12">
            <div class="card-title">Score équipe</div>
            {#if evalLoading}
              <span class="spinner spinner-sm"></span>
            {:else if selectionDirty}
              <button class="btn btn-ghost btn-sm" onclick={() => { selectionDirty = false; runEval(); }}>
                ↺ Réévaluer
              </button>
            {/if}
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
            {@const visibleGaps = selEval.gaps.filter(g => !dismissedGaps.has(g))}
            {@const isSufficient = selEval.overall >= 14}
            {#if isSufficient && visibleGaps.length === 0}
              <div class="sufficiency-banner mt-12">
                <span class="sufficiency-icon">✓</span>
                <span>Sélection suffisante pour commencer l'entraînement</span>
              </div>
            {:else if !isSufficient && visibleGaps.length > 0}
              {@const shownGaps = visibleGaps.slice(0, 3)}
              {@const hiddenCount = visibleGaps.length - shownGaps.length}
              <div class="eval-section mt-12">
                <div class="eval-section-header">
                  <span class="meta-label">Lacunes ({visibleGaps.length})</span>
                  <button class="tag-fill-all" disabled={fillAllLoading} onclick={fillAllGaps} title="Générer un adapter pour chaque lacune">
                    {#if fillAllLoading}<span class="spinner spinner-xs"></span>{/if}
                    Tout couvrir
                  </button>
                </div>
                <div class="action-tags mt-6">
                  {#each shownGaps as g}
                    <div class="gap-row">
                      <button class="tag tag-warn tag-action" class:tag-active={gapTarget === g}
                        disabled={fillAllLoading} onclick={() => openFillGap(g)}>
                        {#if gapLoading && gapTarget === g}<span class="spinner spinner-xs"></span>{:else}+{/if}
                        {g}
                      </button>
                      <button class="gap-dismiss" title="Ignorer cette lacune" onclick={() => dismissGap(g)}>✕</button>
                    </div>
                  {/each}
                  {#if hiddenCount > 0}
                    <span class="gaps-hidden">+{hiddenCount} autre{hiddenCount > 1 ? 's' : ''} (non critiques)</span>
                  {/if}
                </div>
                {#if fillAllResults.length > 0}
                  <div class="fill-all-results mt-10">
                    <div class="fill-all-header">
                      <span class="meta-label" style="color:var(--green)">{fillAllResults.length} adapter{fillAllResults.length > 1 ? 's' : ''} générés</span>
                      <button class="btn-use" onclick={applyAllGapAdapters}>+ Tous ajouter</button>
                    </div>
                    <div class="fill-all-list mt-6">
                      {#each fillAllResults as r}
                        <div class="fill-all-item">
                          <span class="mono" style="font-size:11px;color:var(--accent)">{r.name}</span>
                          <span style="font-size:11px;color:var(--text-muted)">{r.description}</span>
                        </div>
                      {/each}
                    </div>
                  </div>
                {/if}
              </div>
            {/if}
            {#if selEval.overlaps?.length}
              <div class="eval-section mt-10">
                <div class="eval-section-header">
                  <span class="meta-label">Chevauchements ({selEval.overlaps.length})</span>
                  <button class="tag-fill-all tag-fill-all-danger" disabled={resolveAllLoading} onclick={resolveAllOverlaps}>
                    {#if resolveAllLoading}<span class="spinner spinner-xs"></span>{/if}
                    Tout résoudre
                  </button>
                </div>
                <div class="action-tags mt-6">
                  {#each selEval.overlaps as pair}
                    {@const pairKey = pair as [string, string]}
                    <button class="tag tag-danger tag-action"
                      class:tag-active={overlapTarget?.[0] === pairKey[0] && overlapTarget?.[1] === pairKey[1]}
                      disabled={resolveAllLoading} onclick={() => openResolveOverlap(pairKey)}>
                      {#if overlapLoading && overlapTarget?.[0] === pairKey[0]}<span class="spinner spinner-xs"></span>{:else}⚡{/if}
                      {pairKey.join(' ↔ ')}
                    </button>
                  {/each}
                </div>
                {#if resolveAllResults.length > 0}
                  <div class="fill-all-results mt-10" style="border-color:rgba(247,84,84,.2);background:rgba(247,84,84,.04)">
                    <div class="fill-all-header">
                      <span class="meta-label" style="color:var(--red)">{resolveAllResults.length} résolution{resolveAllResults.length > 1 ? 's' : ''} prête{resolveAllResults.length > 1 ? 's' : ''}</span>
                      <button class="btn-use" onclick={applyAllOverlapResolutions}>Tout appliquer</button>
                    </div>
                    <div class="fill-all-list mt-6">
                      {#each resolveAllResults as { pair, resolution }}
                        <div class="fill-all-item">
                          <div style="display:flex;align-items:center;gap:6px">
                            <span class="res-type res-type-{resolution.type}">{resolution.label}</span>
                            <span class="mono" style="font-size:10px;color:var(--text-muted)">{pair.join(' ↔ ')}</span>
                          </div>
                          <span style="font-size:11px;color:var(--text-muted)">{resolution.explanation}</span>
                        </div>
                      {/each}
                    </div>
                  </div>
                {/if}
              </div>
            {/if}
            {#if selEval.recommendation}
              <div class="eval-reco mt-12">{selEval.recommendation}</div>
            {/if}
          {:else if evalError}
            <div class="api-error" style="font-size:11px">{evalError}</div>
          {:else if !evalLoading}
            <p class="hint">Sera calculé après l'analyse.</p>
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
                active={altTarget?.name === adapter.name}
                onAlternatives={openAlternatives}
                onRemove={removeAdapter}
              />
            {/each}

            {#if orchestrator}
              <AdapterCard
                adapter={orchestrator}
                active={altTarget?.name === orchestrator.name}
                onAlternatives={openAlternatives}
                onRemove={() => removeOrchestrator()}
              />
            {/if}

            {#if selection.length === 0}
              <div class="empty-state">Tous les adapters supprimés. ↺ Recommencez.</div>
            {/if}
          </div>

          {#if selection.length > 0 && !readOnly}
            <div class="validation-bar mt-16">
              <button
                class="btn btn-primary"
                onclick={() => { setWizardHandoff(need, adapters, orchestrator); goto('/start'); }}
              >✓ Valider et créer le projet</button>
            </div>
          {:else if selection.length > 0 && readOnly}
            <div class="validation-bar mt-16">
              <a href="/projects/{projectId}" class="btn btn-ghost">← Retour au projet</a>
            </div>
          {/if}
        </div>
      </div>

      <!-- ── Col 3 : panel contextuel ── -->
      {#if altTarget || gapTarget || overlapTarget}
        <div class="col-panel">

          {#if gapTarget}
            <div class="card mb-12">
              <div class="card-header mb-12">
                <div class="card-title">Couvrir la lacune</div>
                <button class="icon-btn" onclick={() => { gapTarget = null; gapResult = null; gapError = ''; }}>✕</button>
              </div>
              <div class="gap-label mb-12">"{gapTarget}"</div>
              {#if gapLoading}
                <div class="panel-loading"><span class="spinner"></span> Génération de l'adapter…</div>
              {:else if gapError}
                <div class="api-error" style="font-size:11px">{gapError}</div>
              {:else if gapResult}
                <div class="adapters-list mb-8">
                  <AdapterCard adapter={gapResult} />
                </div>
                <button class="btn-use w-full" onclick={applyGapAdapter}>+ Ajouter à la sélection</button>
              {:else}
                <p class="hint">Cliquez sur une lacune pour générer un adapter.</p>
              {/if}
            </div>
          {/if}

          {#if overlapTarget}
            <div class="card mb-12">
              <div class="card-header mb-12">
                <div class="card-title">Résoudre le chevauchement</div>
                <button class="icon-btn" onclick={() => { overlapTarget = null; overlapResolutions = []; overlapError = ''; }}>✕</button>
              </div>
              <div class="overlap-label mb-12">
                <span class="mono">{overlapTarget[0]}</span>
                <span style="color:var(--text-muted)"> ↔ </span>
                <span class="mono">{overlapTarget[1]}</span>
              </div>
              {#if overlapLoading}
                <div class="panel-loading"><span class="spinner"></span> Analyse du chevauchement…</div>
              {:else if overlapError}
                <div class="api-error" style="font-size:11px">{overlapError}</div>
              {:else if overlapResolutions.length}
                <div class="resolution-list">
                  {#each overlapResolutions as res}
                    <div class="resolution-card">
                      <div style="display:flex;align-items:center;justify-content:space-between;gap:8px;margin-bottom:6px">
                        <span class="res-type res-type-{res.type}">{res.label}</span>
                        <button class="btn-use" onclick={() => applyOverlapResolution(res)}>Appliquer</button>
                      </div>
                      <p style="font-size:12px;color:var(--text-muted);margin:0;line-height:1.5">{res.explanation}</p>
                      {#if res.adapters?.length}
                        <div class="adapters-list mt-8">
                          {#each res.adapters as a}<AdapterCard adapter={a} />{/each}
                        </div>
                      {/if}
                    </div>
                  {/each}
                </div>
              {:else}
                <p class="hint">Cliquez sur un chevauchement pour voir les résolutions proposées.</p>
              {/if}
            </div>
          {/if}

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

        </div>
      {/if}
    {/if}

  </div>
</div>

<style>
  /* Refine bar */
  .refine-bar {
    display: flex; align-items: center; gap: 8px;
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 8px 14px;
  }
  .refine-label { font-size: 12px; color: var(--text-muted); white-space: nowrap; flex-shrink: 0; }
  .refine-input {
    flex: 1; background: var(--surface-2); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 5px 10px; font-size: 13px; color: var(--text);
    font-family: var(--font-sans); outline: none;
    transition: border-color 150ms;
  }
  .refine-input:focus { border-color: var(--accent); }
  .refine-input:disabled { opacity: 0.5; }

  .phase0-layout {
    display: grid;
    grid-template-columns: 340px 1fr;
    gap: 20px;
    align-items: start;
  }
  .phase0-layout.has-panel { grid-template-columns: 340px 1fr 360px; }

  .need-input { resize: vertical; min-height: 140px; }
  .hint { font-size: 12px; color: var(--text-muted); line-height: 1.5; margin: 0; }
  .mt-4  { margin-top: 4px; }  .mt-6  { margin-top: 6px; }  .mt-8  { margin-top: 8px; }  .mt-10 { margin-top: 10px; }
  .mt-12 { margin-top: 12px; } .mt-16 { margin-top: 16px; }
  .mb-8  { margin-bottom: 8px; } .mb-12 { margin-bottom: 12px; } .mb-16 { margin-bottom: 16px; }
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
  .sw-accordion { border:1px solid rgba(251,146,60,.25); border-radius:var(--radius); overflow:hidden; }
  .sw-accordion__header { width:100%; display:flex; align-items:center; gap:8px; background:rgba(251,146,60,.08); border:none; padding:8px 12px; cursor:pointer; text-align:left; }
  .sw-accordion__header:hover { background:rgba(251,146,60,.14); }
  .sw-accordion__title { font-size:11px; font-weight:600; color:#fb923c; text-transform:uppercase; letter-spacing:.04em; flex:1; }
  .sw-accordion__count { font-size:11px; color:rgba(251,146,60,.7); background:rgba(251,146,60,.15); border-radius:10px; padding:1px 7px; }
  .sw-accordion__chevron { font-size:12px; color:#fb923c; transition:transform .15s; }
  .sw-accordion__chevron.open { transform:rotate(180deg); }
  .sw-accordion__list { margin:0; padding:4px 0; list-style:none; }
  .sw-accordion__item { border-top:1px solid rgba(255,255,255,.05); }
  .sw-accordion__item-btn { width:100%; display:flex; align-items:center; gap:8px; background:none; border:none; padding:7px 14px; cursor:pointer; text-align:left; }
  .sw-accordion__item-btn:hover { background:rgba(255,255,255,.04); }
  .sw-accordion__item-name { font-size:12px; color:var(--text); flex:1; }
  .sw-accordion__item-chevron { font-size:11px; color:var(--text-muted); transition:transform .15s; }
  .sw-accordion__item-chevron.open { transform:rotate(180deg); }
  .sw-accordion__item-desc { padding:4px 14px 10px 14px; font-size:12px; color:var(--text-muted); line-height:1.6; }

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

  /* Eval section header */
  .eval-section-header { display:flex; align-items:center; justify-content:space-between; gap:8px; }

  /* Action tags */
  .action-tags { display:flex; flex-wrap:wrap; gap:4px; }
  .gap-row { display:flex; align-items:center; gap:3px; }
  .gap-dismiss { background:none; border:none; padding:0 3px; font-size:11px; color:var(--text-muted); cursor:pointer; opacity:0.5; line-height:1; }
  .gap-dismiss:hover { opacity:1; color:var(--text-dim); }
  .gaps-hidden { font-size:10px; color:var(--text-muted); opacity:0.6; padding:2px 6px; align-self:center; }

  /* Sufficiency banner */
  .sufficiency-banner {
    display:flex; align-items:center; gap:8px;
    padding:8px 12px; border-radius:var(--radius);
    background:rgba(74,222,128,.08); border:1px solid rgba(74,222,128,.3);
    font-size:12px; color:var(--green); font-weight:500;
  }
  .sufficiency-icon { font-size:14px; flex-shrink:0; }
  .tag-action {
    cursor:pointer; transition:background 150ms, border-color 150ms, transform 100ms;
    display:inline-flex; align-items:center; gap:4px;
  }
  .tag-action:hover { transform:scale(1.03); }
  .tag-action:disabled { opacity:0.5; cursor:not-allowed; transform:none; }
  .tag-warn.tag-action:hover   { background:rgba(245,158,11,.15); border-color:#f59e0b; }
  .tag-danger.tag-action:hover { background:rgba(255,80,80,.12); border-color:var(--red); }
  .tag-active { font-weight:600; }
  .tag-warn.tag-active   { background:rgba(245,158,11,.18); border-color:#f59e0b; }
  .tag-danger.tag-active { background:rgba(255,80,80,.15); border-color:var(--red); }

  /* Fill-all / Resolve-all buttons */
  .tag-fill-all {
    background:none; border:1px solid rgba(245,158,11,.4);
    border-radius:999px; padding:2px 10px; font-size:11px; cursor:pointer;
    color:#f59e0b; display:inline-flex; align-items:center; gap:4px;
    transition:background 150ms, border-color 150ms;
  }
  .tag-fill-all:hover { background:rgba(245,158,11,.12); border-color:#f59e0b; }
  .tag-fill-all:disabled { opacity:0.5; cursor:not-allowed; }
  .tag-fill-all-danger { color:var(--red); border-color:rgba(255,80,80,.4); }
  .tag-fill-all-danger:hover { background:rgba(255,80,80,.1); border-color:var(--red); }

  /* Fill-all results */
  .fill-all-results {
    border:1px solid rgba(74,222,128,.2); background:rgba(74,222,128,.04);
    border-radius:var(--radius); padding:10px 12px;
  }
  .fill-all-header { display:flex; align-items:center; justify-content:space-between; gap:8px; }
  .fill-all-list { display:flex; flex-direction:column; gap:6px; }
  .fill-all-item {
    display:flex; flex-direction:column; gap:2px;
    padding:6px 8px; background:var(--surface-2); border-radius:calc(var(--radius) - 2px);
  }

  /* Gap / overlap labels in panel */
  .gap-label {
    font-size:13px; font-style:italic; color:var(--text-muted);
    background:rgba(245,158,11,.08); border:1px solid rgba(245,158,11,.25);
    border-radius:var(--radius); padding:6px 12px;
  }
  .overlap-label {
    font-size:13px; background:rgba(255,80,80,.06); border:1px solid rgba(255,80,80,.2);
    border-radius:var(--radius); padding:6px 12px;
  }

  /* Resolution list */
  .resolution-list { display:flex; flex-direction:column; gap:10px; }
  .resolution-card {
    border:1px solid var(--border); border-radius:var(--radius);
    padding:10px 12px; background:var(--surface-2);
  }
  .res-type {
    font-size:10px; font-weight:700; text-transform:uppercase; letter-spacing:.06em;
    padding:2px 8px; border-radius:999px;
  }
  .res-type-clarify { background:rgba(99,179,237,.15); color:#63b3ed; }
  .res-type-merge   { background:rgba(74,222,128,.15); color:var(--green); }
  .res-type-remove  { background:rgba(255,80,80,.12); color:var(--red); }

  /* Spinner xs */
  .spinner-xs { width:10px; height:10px; border-width:1.5px; }

  /* w-full utility */
  .w-full { width:100%; justify-content:center; }

  /* Source configs list in panel */
  .source-config-list { display:flex; flex-direction:column; gap:5px; }
  .source-config-row {
    display:flex; align-items:flex-start; justify-content:space-between; gap:6px;
    padding:6px 8px; background:var(--surface-2); border-radius:calc(var(--radius) - 2px);
    border:1px solid var(--border);
  }
  .source-config-info { display:flex; flex-direction:column; gap:2px; min-width:0; flex:1; }
  .source-config-label { font-size:12px; color:var(--text); font-weight:500; line-height:1.3; }

  /* Collector type badges */
  .collector-badge {
    font-size:9px; font-weight:700; text-transform:uppercase; letter-spacing:.04em;
    padding:1px 6px; border-radius:999px; flex-shrink:0;
  }
  .collector-multilang_wiki { background:rgba(99,179,237,.15); color:#63b3ed; }
  .collector-doc_crawl    { background:rgba(74,222,128,.12); color:var(--green); }
  .collector-web_crawl    { background:rgba(251,146,60,.12); color:#fb923c; }
  .collector-github_repo  { background:rgba(167,139,250,.12); color:#a78bfa; }
  .collector-manual       { background:rgba(255,80,80,.1); color:var(--red); }

  /* Add source form */
  .source-add-form {
    border-top:1px solid var(--border); padding-top:12px; margin-top:4px;
  }
  .source-add-title { font-size:11px; font-weight:600; color:var(--text-muted); text-transform:uppercase; letter-spacing:.04em; }
  .form-row { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
  .form-input {
    width:100%; background:var(--surface-2); border:1px solid var(--border);
    border-radius:var(--radius); padding:5px 9px; font-size:12px; color:var(--text);
    font-family:var(--font-sans); outline:none; transition:border-color 150ms; box-sizing:border-box;
  }
  .form-input:focus { border-color:var(--accent); }
</style>
