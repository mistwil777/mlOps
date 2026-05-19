<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { goto } from '$app/navigation';
  import { createProject, advisorRecommend, advisorAlternatives, advisorEvaluateSelection, advisorFillGap, advisorResolveOverlap, advisorExtractDoc, advisorAnalyzePairs, advisorCrossProjectMatches, getDataHealth, launchEvaluation, startCollect, startPipeline } from '$lib/api';
  import type { PairAnalysisResult, CrossProjectMatch } from '$lib/api';
  import type { AdvisorRecommendation, AdvisorAdapter, SelectionEval, ReviewItem, OverlapResolution } from '$lib/types';
  import Topbar from '$lib/components/Topbar.svelte';
  import SovereigntyBadge from '$lib/components/SovereigntyBadge.svelte';
  import AdapterCard from '$lib/components/AdapterCard.svelte';
  import { wizardHandoff, clearWizardHandoff } from '$lib/stores/wizard.svelte';

  // ── Étapes du wizard ──────────────────────────────────────────────────────
  // 0 Accueil  1 Besoin  2 Architecture  2.5 Sources+Collecte  3 Projet  4 Données  5 Training  6 Évaluation  7 Livraison
  let step = $state(0);
  let step2Tab = $state<'architecture' | 'sources' | 'data-analysis'>('architecture');
  let analysisSavedAt = $state<string | null>(null);

  // Étape 1 — besoin
  let need       = $state('');
  let aiModel    = $state('us.anthropic.claude-sonnet-4-6');
  let analyzing  = $state(false);
  let analyzeErr = $state('');
  let recommendation: AdvisorRecommendation | null = $state(null);

  // Documents techniques (CdC fonctionnel / technique)
  interface UploadedDoc { filename: string; char_count: number; was_summarized: boolean; text: string; }
  let uploadedDocs  = $state<UploadedDoc[]>([]);
  let uploadingDocs = $state<string[]>([]);
  let uploadErr     = $state('');
  let dropActive    = $state(false);

  // Étape 2 — adapters éditables + éval équipe + panel contextuel
  let adapters     = $state<AdvisorAdapter[]>([]);
  let orchestrator = $state<AdvisorAdapter | null>(null);
  let selEval      = $state<SelectionEval | null>(null);
  let evalLoading  = $state(false);

  // Cross-project reuse detection
  let crossMatches = $state<Record<string, CrossProjectMatch>>({});

  async function loadCrossMatches() {
    if (wizardSelection.length === 0) return;
    const entries = await Promise.allSettled(
      wizardSelection.map(a =>
        advisorCrossProjectMatches(a.description, 10)
          .then(r => [a.name, r] as [string, CrossProjectMatch])
          .catch(() => null)
      )
    );
    const result: Record<string, CrossProjectMatch> = {};
    for (const e of entries) {
      if (e.status === 'fulfilled' && e.value) {
        result[e.value[0]] = e.value[1];
      }
    }
    crossMatches = result;
  }

  let altTarget    = $state<AdvisorAdapter | null>(null);
  let altLoading   = $state(false);
  let altError     = $state('');
  let alternatives = $state<AdvisorAdapter[]>([]);
  let sourcesAdapter = $state<AdvisorAdapter | null>(null);

  // Panneau dynamique : combler une lacune
  let gapTarget    = $state<string | null>(null);
  let gapLoading   = $state(false);
  let gapError     = $state('');
  let gapResult    = $state<AdvisorAdapter | null>(null);

  // Panneau dynamique : résoudre un chevauchement
  let overlapTarget  = $state<[string, string] | null>(null);
  let overlapLoading = $state(false);
  let overlapError   = $state('');
  let overlapResolutions = $state<OverlapResolution[]>([]);

  const wizardSelection = $derived([...adapters, ...(orchestrator ? [orchestrator] : [])]);
  const wizardTotalPairs = $derived(wizardSelection.reduce((s, a) => s + a.estimated_pairs, 0));
  const allSources = $derived(
    wizardSelection
      .flatMap(a => (a.data_sources ?? []).map(s => ({ source: s, adapter: a.name })))
      .filter((v, i, arr) => arr.findIndex(x => x.source === v.source) === i)
  );

  // Étape 2.5 — sources + collecte + pipeline
  interface SourceEntry { source: string; adapter: string; selected: boolean; collector_type: string; source_value: string; }
  let sourceEntries = $state<SourceEntry[]>([]);
  let collectMaxPairs = $state(50);
  let collectTopic    = $state('');
  let collectJobId    = $state('');
  let collectRunning  = $state(false);
  let collectStatus   = $state<'idle'|'running'|'completed'|'failed'>('idle');
  let collectLogs     = $state<string[]>([]);
  let collectErr      = $state('');
  let pipelineJobId   = $state('');
  let pipelineRunning = $state(false);
  let pipelineStatus  = $state<'idle'|'running'|'completed'|'failed'>('idle');
  let pipelineLogs    = $state<string[]>([]);
  let collectedPairs  = $state<ReviewItem[]>([]);
  let collectedTotal  = $state(0);
  let showPairsPreview = $state(false);
  let collectEs: EventSource | null = null;
  let pipelineEs: EventSource | null = null;

  function initSourceEntries() {
    if (sourceEntries.length > 0) return; // ne pas écraser si déjà initialisé
    const seen = new Set<string>();
    const entries: SourceEntry[] = [];
    for (const a of wizardSelection) {
      if (a.source_configs && a.source_configs.length > 0) {
        // Structured sources from Advisor — preferred
        for (const sc of a.source_configs) {
          const key = `${sc.collector_type}:${sc.value}`;
          if (!seen.has(key)) {
            seen.add(key);
            entries.push({ source: sc.label, adapter: a.name, selected: true, collector_type: sc.collector_type, source_value: sc.value });
          }
        }
      } else {
        // Fallback: legacy string data_sources → crawl4ai
        for (const s of (a.data_sources ?? [])) {
          if (!seen.has(s)) {
            seen.add(s);
            entries.push({ source: s, adapter: a.name, selected: true, collector_type: 'crawl4ai', source_value: s });
          }
        }
      }
    }
    sourceEntries = entries;
    if (!collectTopic) collectTopic = need.trim().slice(0, 80);
  }

  function switchToSources() {
    initSourceEntries();
    step2Tab = 'sources';
  }

  function switchToDataAnalysis() {
    initDataAnalysisAdapter();
    step2Tab = 'data-analysis';
  }

  async function launchCollect(tempProjectId: string) {
    const selectedSources = sourceEntries.filter(e => e.selected);
    if (selectedSources.length === 0) { collectErr = 'Sélectionnez au moins une source.'; return; }
    collectErr = ''; collectLogs = []; collectRunning = true; collectStatus = 'running';
    collectedPairs = []; collectedTotal = 0;
    // Use the first selected source's collector_type; mixed types run sequentially per source
    const firstSrc = selectedSources[0];
    try {
      const res = await startCollect({
        project_id: tempProjectId,
        topic: collectTopic || need.trim(),
        max_pairs: collectMaxPairs,
        domain: projDomain,
        task_type: 'generation',
        collector_type: firstSrc.collector_type,
        source_value: firstSrc.source_value,
      });
      collectJobId = res.job_id;
      collectEs = new EventSource(`/api/data/collect/${collectJobId}/stream`);
      collectEs.addEventListener('log', (e) => {
        const d = JSON.parse(e.data);
        collectLogs = [...collectLogs, d.line];
        scrollBox('collect-log');
      });
      collectEs.addEventListener('done', async (e) => {
        const d = JSON.parse(e.data);
        collectStatus = d.status;
        collectRunning = false;
        collectEs?.close();
        if (d.status === 'completed') await loadCollectedPairs(tempProjectId);
      });
      collectEs.onerror = () => { collectStatus = 'failed'; collectRunning = false; collectEs?.close(); };
    } catch (e) { collectErr = (e as Error).message; collectRunning = false; collectStatus = 'failed'; }
  }

  async function loadCollectedPairs(pid: string) {
    try {
      const res = await fetch(`/api/data/review/${pid}?stage=raw&page=0&page_size=20`);
      if (!res.ok) return;
      const page = await res.json();
      collectedPairs = page.items ?? [];
      collectedTotal = page.total ?? 0;
    } catch { /* non-blocking */ }
  }

  async function launchPipeline(tempProjectId: string) {
    pipelineLogs = []; pipelineRunning = true; pipelineStatus = 'running';
    try {
      const res = await startPipeline({ project_id: tempProjectId });
      pipelineJobId = res.job_id;
      pipelineEs = new EventSource(`/api/data/pipeline/${pipelineJobId}/stream`);
      pipelineEs.addEventListener('log', (e) => {
        const d = JSON.parse(e.data);
        pipelineLogs = [...pipelineLogs, d.line];
        scrollBox('pipeline-log');
      });
      pipelineEs.addEventListener('done', (e) => {
        const d = JSON.parse(e.data);
        pipelineStatus = d.status;
        pipelineRunning = false;
        pipelineEs?.close();
      });
      pipelineEs.onerror = () => { pipelineStatus = 'failed'; pipelineRunning = false; pipelineEs?.close(); };
    } catch (e) { pipelineRunning = false; pipelineStatus = 'failed'; }
  }

  function scrollBox(id: string) {
    setTimeout(() => { const el = document.getElementById(id); if (el) el.scrollTop = el.scrollHeight; }, 0);
  }

  const collectSelectedCount = $derived(sourceEntries.filter(e => e.selected).length);

  // Étape 2 — Analyse données (onglet data-analysis)
  interface AdapterAnalysis { result: PairAnalysisResult | null; loading: boolean; error: string; nGenerate: number; }
  let dataAnalyses = $state<Record<string, AdapterAnalysis>>({});
  let dataAnalysisAdapter = $state<string>(''); // adapter sélectionné pour l'analyse

  function initDataAnalysisAdapter() {
    if (!dataAnalysisAdapter && wizardSelection.length > 0) {
      dataAnalysisAdapter = wizardSelection[0].name;
    }
  }

  async function runDataAnalysis(adapter: AdvisorAdapter) {
    const current = dataAnalyses[adapter.name] ?? { result: null, loading: false, error: '', nGenerate: 10 };
    const nGen = current.nGenerate ?? 10;
    dataAnalyses = { ...dataAnalyses, [adapter.name]: { ...current, loading: true, error: '' } };
    try {
      const result = await advisorAnalyzePairs({
        adapter_name: adapter.name,
        adapter_desc: adapter.description,
        need,
        pairs_sample: collectedPairs.map(p => ({ prompt: p.prompt, completion: p.completion })),
        n_generate: nGen,
        target_pairs: adapter.estimated_pairs,
      });
      dataAnalyses = { ...dataAnalyses, [adapter.name]: { result, loading: false, error: '', nGenerate: nGen } };
    } catch (e) {
      dataAnalyses = { ...dataAnalyses, [adapter.name]: { result: null, loading: false, error: (e as Error).message, nGenerate: nGen } };
    }
  }

  function addGeneratedPairs(adapterName: string) {
    const analysis = dataAnalyses[adapterName];
    if (!analysis?.result?.generated_pairs?.length) return;
    // On stocke les paires dans une variable accessible pour l'export ultérieur
    // (elles seront réutilisées au moment de la création du projet)
    generatedPairsStore = {
      ...generatedPairsStore,
      [adapterName]: [...(generatedPairsStore[adapterName] ?? []), ...analysis.result.generated_pairs],
    };
    dataAnalyses = { ...dataAnalyses, [adapterName]: { ...analysis, result: { ...analysis.result, generated_pairs: [] } } };
  }

  let generatedPairsStore = $state<Record<string, Array<{ prompt: string; completion: string }>>>({});

  const totalGeneratedPairs = $derived(Object.values(generatedPairsStore).reduce((s, arr) => s + arr.length, 0));

  // Étape 3 — création projet
  let projName   = $state('');
  let projDomain = $state('code');
  let projClass  = $state('interne');
  let baseModel  = $state('qwen2.5-coder-7b-4bit');
  let creating   = $state(false);
  let projectId  = $state('');
  let createErr  = $state('');

  // Étape 4 — données
  let dataReady  = $state(false);
  let pairCount  = $state(0);

  // Étape 5 — training
  let runId      = $state('');

  // Étape 6 — éval
  let evalLaunched = $state(false);

  const DOMAINS = ['code','legal','hr','finance','defense','healthcare','sales','compliance','other'];
  const BASE_MODELS = [
    { id: 'qwen2.5-coder-7b-4bit', label: 'Qwen 2.5 Coder 7B (recommandé)' },
    { id: 'mistral-7b-instruct-4bit', label: 'Mistral 7B Instruct' },
    { id: 'qwen2.5-14b-instruct-4bit', label: 'Qwen 2.5 14B (plus lent)' },
  ];

  const STEPS = [
    { num: 1, label: 'Besoin client' },
    { num: 2, label: 'Architecture' },
    { num: 3, label: 'Sources & données' },
    { num: 4, label: 'Créer le projet' },
    { num: 5, label: 'Training' },
    { num: 6, label: 'Évaluation' },
    { num: 7, label: 'Livraison' },
  ];

  // ── Actions ───────────────────────────────────────────────────────────────
  async function analyzeNeed() {
    if (!need.trim()) return;
    analyzing = true; analyzeErr = ''; recommendation = null; selEval = null;
    try {
      recommendation = await advisorRecommend(need.trim(), aiModel, uploadedDocs.map(d => d.text));
      adapters     = [...recommendation.recommendation.adapters];
      orchestrator = { ...recommendation.recommendation.orchestrator };
      const firstAdapter = adapters[0];
      if (firstAdapter && !projName) {
        projName = firstAdapter.name.split('-').slice(0, 2).join('-');
      }
      step = 2;
      runWizardEval();
      loadCrossMatches();
    } catch (e) { analyzeErr = (e as Error).message; }
    finally { analyzing = false; }
  }

  async function runWizardEval() {
    if (wizardSelection.length === 0) return;
    evalLoading = true;
    try {
      selEval = await advisorEvaluateSelection(need, wizardSelection, aiModel);
    } catch { /* non-blocking */ }
    finally { evalLoading = false; }
  }

  function scoreColor(s: number): string {
    if (s >= 15) return 'var(--green)';
    if (s >= 10) return 'var(--accent)';
    return 'var(--red)';
  }

  function scoreBar(s: number): string {
    return `width:${(s / 20) * 100}%`;
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
      alternatives = await advisorAlternatives(need, adapter.name, adapter.description, wizardSelection, aiModel);
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

  function openSources(adapter: AdvisorAdapter) {
    sourcesAdapter = adapter;
  }

  async function openFillGap(gap: string) {
    if (gapTarget === gap) { gapTarget = null; gapResult = null; return; }
    altTarget = null; overlapTarget = null; sourcesAdapter = null;
    gapTarget = gap; gapLoading = true; gapError = ''; gapResult = null;
    try {
      gapResult = await advisorFillGap(need, gap, wizardSelection, aiModel);
    } catch (e) { gapError = (e as Error).message; }
    finally { gapLoading = false; }
  }

  function applyGapAdapter() {
    if (!gapResult) return;
    adapters = [...adapters, { ...gapResult, role: 'specialist' }];
    gapTarget = null; gapResult = null;
    // No auto-eval: same rationale as applyAllGapAdapters.
  }

  let fillAllLoading = $state(false);
  let fillAllResults = $state<AdvisorAdapter[]>([]);

  // "Tout résoudre" — prend la première résolution (clarify en priorité) pour chaque paire
  let resolveAllLoading = $state(false);
  interface ResolveAllEntry { pair: [string, string]; resolution: OverlapResolution; }
  let resolveAllResults = $state<ResolveAllEntry[]>([]);

  async function fillAllGaps() {
    if (!selEval?.gaps.length) return;
    altTarget = null; overlapTarget = null; sourcesAdapter = null;
    gapTarget = null; gapResult = null;
    fillAllLoading = true; fillAllResults = [];
    const snapshot = [...wizardSelection]; // sélection figée au moment du lancement
    try {
      const results = await Promise.all(
        selEval.gaps.map(g => advisorFillGap(need, g, snapshot, aiModel).catch(() => null))
      );
      fillAllResults = results.filter((r): r is AdvisorAdapter => r !== null);
    } finally { fillAllLoading = false; }
  }

  function applyAllGapAdapters() {
    adapters = [...adapters, ...fillAllResults.map(r => ({ ...r, role: 'specialist' as const }))];
    fillAllResults = [];
    // No auto-eval: user decides when selection is final. Re-evaluating immediately
    // produces new gaps on the enlarged scope and creates an infinite loop.
  }

  async function openResolveOverlap(pair: [string, string]) {
    if (overlapTarget?.[0] === pair[0] && overlapTarget?.[1] === pair[1]) {
      overlapTarget = null; overlapResolutions = []; return;
    }
    altTarget = null; gapTarget = null; sourcesAdapter = null;
    overlapTarget = pair; overlapLoading = true; overlapError = ''; overlapResolutions = [];
    const a = wizardSelection.find(x => x.name === pair[0]);
    const b = wizardSelection.find(x => x.name === pair[1]);
    if (!a || !b) { overlapLoading = false; return; }
    try {
      overlapResolutions = await advisorResolveOverlap(need, a, b, wizardSelection, aiModel);
    } catch (e) { overlapError = (e as Error).message; }
    finally { overlapLoading = false; }
  }

  function applyOverlapResolution(res: OverlapResolution) {
    if (!overlapTarget) return;
    const [nameA, nameB] = overlapTarget;
    if (res.type === 'remove') {
      // l'adapter survivant est dans res.adapters[0]
      adapters = adapters.filter(a => a.name !== nameA && a.name !== nameB);
      if (orchestrator?.name === nameA || orchestrator?.name === nameB) orchestrator = null;
      if (res.adapters[0]) adapters = [...adapters, res.adapters[0]];
    } else if (res.type === 'merge') {
      adapters = adapters.filter(a => a.name !== nameA && a.name !== nameB);
      if (orchestrator?.name === nameA || orchestrator?.name === nameB) orchestrator = null;
      if (res.adapters[0]) adapters = [...adapters, res.adapters[0]];
    } else {
      // clarify: replace both adapters — fallback by position since Claude may rename
      adapters = adapters.map(a => {
        if (a.name === nameA) return res.adapters[0] ?? a;
        if (a.name === nameB) return res.adapters[1] ?? res.adapters[0] ?? a;
        return a;
      });
      if (orchestrator) {
        const updated = res.adapters.find(r => r.name === orchestrator!.name);
        if (updated) orchestrator = updated;
      }
    }
    overlapTarget = null; overlapResolutions = [];
    triggerEval();
  }

  async function resolveAllOverlaps() {
    if (!selEval?.overlaps?.length) return;
    altTarget = null; gapTarget = null; overlapTarget = null; sourcesAdapter = null;
    resolveAllLoading = true; resolveAllResults = [];
    const snapshot = [...wizardSelection];
    try {
      const pairs = selEval.overlaps as [string, string][];
      const results = await Promise.all(
        pairs.map(async pair => {
          const a = snapshot.find(x => x.name === pair[0]);
          const b = snapshot.find(x => x.name === pair[1]);
          if (!a || !b) return null;
          const resolutions = await advisorResolveOverlap(need, a, b, snapshot, aiModel).catch(() => null);
          if (!resolutions?.length) return null;
          // priorité : clarify > merge > remove
          const best = resolutions.find(r => r.type === 'clarify')
            ?? resolutions.find(r => r.type === 'merge')
            ?? resolutions[0];
          return { pair, resolution: best } as ResolveAllEntry;
        })
      );
      resolveAllResults = results.filter((r): r is ResolveAllEntry => r !== null);
    } finally { resolveAllLoading = false; }
  }

  function applyAllOverlapResolutions() {
    // applique séquentiellement — chaque résolution travaille sur l'état courant
    for (const { pair, resolution } of resolveAllResults) {
      const [nameA, nameB] = pair;
      if (resolution.type === 'remove' || resolution.type === 'merge') {
        adapters = adapters.filter(a => a.name !== nameA && a.name !== nameB);
        if (orchestrator?.name === nameA || orchestrator?.name === nameB) orchestrator = null;
        if (resolution.adapters[0]) adapters = [...adapters, resolution.adapters[0]];
      } else {
        // clarify: fallback by position
        adapters = adapters.map(a => {
          if (a.name === nameA) return resolution.adapters[0] ?? a;
          if (a.name === nameB) return resolution.adapters[1] ?? resolution.adapters[0] ?? a;
          return a;
        });
        if (orchestrator) {
          const updated = resolution.adapters.find(r => r.name === orchestrator!.name);
          if (updated) orchestrator = updated;
        }
      }
    }
    resolveAllResults = [];
    triggerEval();
  }

  let evalTimer: ReturnType<typeof setTimeout> | null = null;
  function triggerEval() {
    selEval = null;
    if (evalTimer) clearTimeout(evalTimer);
    if (wizardSelection.length === 0) return;
    evalTimer = setTimeout(runWizardEval, 800);
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

  async function createProj() {
    if (!projName.trim()) return;
    creating = true; createErr = '';
    try {
      const proj = await createProject({
        name: projName.trim(),
        domain: projDomain,
        classification: projClass,
        model: baseModel,
        model_format: baseModel.includes('qwen') ? 'qwen' : 'mistral',
        description: need.trim().slice(0, 200),
      });
      projectId = (proj as { id: string }).id;
      step = 4;
    } catch (e) { createErr = (e as Error).message; }
    finally { creating = false; }
  }

  async function checkDataReady() {
    if (!projectId) return;
    try {
      const health = await getDataHealth(projectId);
      pairCount = health.stages.fine_tuning_ready.count;
      dataReady = pairCount >= 200;
    } catch { dataReady = false; }
  }

  async function launchEval() {
    if (!runId) return;
    try {
      await launchEvaluation(runId);
      evalLaunched = true;
    } catch { /* non-blocking */ }
  }

  // ── Persistance multi-analyses ────────────────────────────────────────────
  // Une clé par analyse : wizard_analysis_v2__{projName}
  // Un index global   : wizard_analyses_index  (liste des analyses sauvegardées)

  const STORAGE_PREFIX = 'wizard_analysis_v2__';
  const INDEX_KEY      = 'wizard_analyses_index';

  interface AnalysisEntry {
    key: string;          // clé localStorage complète
    projName: string;
    needExcerpt: string;  // 80 premiers caractères du besoin
    adaptersCount: number;
    savedAt: string;
  }

  let savedAnalyses = $state<AnalysisEntry[]>([]);

  function _loadIndex(): AnalysisEntry[] {
    try {
      return JSON.parse(localStorage.getItem(INDEX_KEY) ?? '[]');
    } catch { return []; }
  }

  function _saveIndex(entries: AnalysisEntry[]) {
    try { localStorage.setItem(INDEX_KEY, JSON.stringify(entries)); } catch { /* indisponible */ }
  }

  function saveAnalysis() {
    if (!projName.trim() && adapters.length === 0) return;
    const key = STORAGE_PREFIX + (projName.trim() || 'sans-nom');
    const savedAt = new Date().toISOString();
    try {
      localStorage.setItem(key, JSON.stringify({
        need, aiModel, adapters, orchestrator,
        selEval, recommendation,
        projName, projDomain, projClass, baseModel,
        sourceEntries, collectTopic, collectMaxPairs,
        step2Tab,
        generatedPairsStore,
        savedAt,
      }));
      analysisSavedAt = savedAt;

      // Mettre à jour l'index
      const entry: AnalysisEntry = {
        key,
        projName: projName.trim() || 'sans-nom',
        needExcerpt: need.trim().slice(0, 80),
        adaptersCount: adapters.length + (orchestrator ? 1 : 0),
        savedAt,
      };
      const idx = _loadIndex().filter(e => e.key !== key);
      _saveIndex([entry, ...idx]); // plus récent en premier
      savedAnalyses = _loadIndex();
    } catch { /* localStorage indisponible */ }
  }

  function clearSession() {
    const key = STORAGE_PREFIX + (projName.trim() || 'sans-nom');
    try {
      localStorage.removeItem(key);
      _saveIndex(_loadIndex().filter(e => e.key !== key));
      savedAnalyses = _loadIndex();
    } catch { /* indisponible */ }
  }

  function deleteAnalysis(key: string) {
    try {
      localStorage.removeItem(key);
      _saveIndex(_loadIndex().filter(e => e.key !== key));
      savedAnalyses = _loadIndex();
    } catch { /* indisponible */ }
  }

  function loadAnalysis(key: string) {
    try {
      const raw = localStorage.getItem(key);
      if (!raw) return;
      const s = JSON.parse(raw);
      need         = s.need         ?? '';
      aiModel      = s.aiModel      ?? aiModel;
      adapters     = s.adapters     ?? [];
      orchestrator = s.orchestrator ?? null;
      selEval      = s.selEval      ?? null;
      recommendation = s.recommendation ?? null;
      projName     = s.projName     ?? '';
      projDomain   = s.projDomain   ?? projDomain;
      projClass    = s.projClass    ?? projClass;
      baseModel    = s.baseModel    ?? baseModel;
      sourceEntries        = s.sourceEntries        ?? [];
      collectTopic         = s.collectTopic         ?? '';
      collectMaxPairs      = s.collectMaxPairs      ?? 50;
      step2Tab             = s.step2Tab             ?? 'architecture';
      generatedPairsStore  = s.generatedPairsStore  ?? {};
      analysisSavedAt = s.savedAt ?? null;
      if (adapters.length > 0) {
        step = 2;
        if (!selEval) runWizardEval();
        loadCrossMatches();
      } else {
        step = 1;
      }
    } catch { /* indisponible */ }
  }

  function resetAnalysis() {
    adapters = []; orchestrator = null; selEval = null;
    recommendation = null; altTarget = null; alternatives = [];
    sourcesAdapter = null; sourceEntries = [];
    clearSession();
    step = 1;
  }

  async function handleDocFiles(files: FileList | File[]) {
    uploadErr = '';
    const list = Array.from(files).filter(f => {
      const ext = f.name.split('.').pop()?.toLowerCase() ?? '';
      return ['pdf','docx','doc','txt','md','rst','csv'].includes(ext);
    });
    if (list.length === 0) { uploadErr = 'Formats acceptés : PDF, DOCX, TXT, MD'; return; }
    for (const file of list) {
      if (uploadedDocs.find(d => d.filename === file.name)) continue; // déjà présent
      uploadingDocs = [...uploadingDocs, file.name];
      try {
        const doc = await advisorExtractDoc(file);
        uploadedDocs = [...uploadedDocs, doc];
      } catch (e) {
        uploadErr = `${file.name} : ${(e as Error).message}`;
      } finally {
        uploadingDocs = uploadingDocs.filter(n => n !== file.name);
      }
    }
  }

  function removeDoc(filename: string) {
    uploadedDocs = uploadedDocs.filter(d => d.filename !== filename);
  }

  function onDocDrop(e: DragEvent) {
    e.preventDefault(); dropActive = false;
    if (e.dataTransfer?.files) void handleDocFiles(e.dataTransfer.files);
  }

  onDestroy(() => {
    collectEs?.close();
    pipelineEs?.close();
  });

  // ── Handoff depuis l'Advisor + restauration session ───────────────────────
  onMount(() => {
    // Migration v1 → v2 : si une ancienne clé unique existe, on l'importe
    try {
      const legacy = localStorage.getItem('wizard_analysis_v1');
      if (legacy) {
        const s = JSON.parse(legacy);
        const legacyName = s.projName?.trim() || 'sans-nom';
        const legacyKey = STORAGE_PREFIX + legacyName;
        if (!localStorage.getItem(legacyKey)) {
          localStorage.setItem(legacyKey, legacy);
          const entry: AnalysisEntry = {
            key: legacyKey,
            projName: legacyName,
            needExcerpt: (s.need ?? '').slice(0, 80),
            adaptersCount: (s.adapters?.length ?? 0) + (s.orchestrator ? 1 : 0),
            savedAt: s.savedAt ?? new Date().toISOString(),
          };
          _saveIndex([entry, ..._loadIndex().filter(e => e.key !== legacyKey)]);
        }
        localStorage.removeItem('wizard_analysis_v1');
      }
    } catch { /* indisponible */ }

    // Charger l'index des analyses sauvegardées
    savedAnalyses = _loadIndex();

    if (wizardHandoff.adapters.length > 0) {
      need         = wizardHandoff.need;
      adapters     = [...wizardHandoff.adapters];
      orchestrator = wizardHandoff.orchestrator ? { ...wizardHandoff.orchestrator } : null;
      if (adapters[0] && !projName) projName = adapters[0].name.split('-').slice(0, 2).join('-');
      clearWizardHandoff();
      step = 2;
      runWizardEval();
      return;
    }
  });

  // Sauvegarde automatique à chaque modification des adapters, sources ou score
  $effect(() => {
    void adapters; void orchestrator; void selEval;
    void sourceEntries; void collectTopic; void collectMaxPairs; void step2Tab;
    void generatedPairsStore;
    if (adapters.length > 0 || orchestrator) saveAnalysis();
  });
</script>

<Topbar crumbs={[{ label: 'Démarrer un projet' }]}>
  <SovereigntyBadge mode="local" />
</Topbar>

<div class="content" class:wizard-content={step !== 2}>

  <!-- Étape 0 : accueil -->
  {#if step === 0}
    <div class="welcome">
      <div class="welcome-icon">⬡</div>
      <h1 class="welcome-title">Bienvenue dans MLOps Factory</h1>
      <p class="welcome-sub">
        Ce wizard vous guide de <strong>l'analyse du besoin client</strong> jusqu'à la
        <strong>livraison du modèle</strong>, étape par étape.<br>
        Aucune navigation dans le menu n'est nécessaire.
      </p>
      <div class="welcome-steps">
        {#each STEPS as s}
          <div class="ws-item">
            <div class="ws-num">{s.num}</div>
            <span class="ws-label">{s.label}</span>
          </div>
          {#if s.num < STEPS.length}<div class="ws-arrow">→</div>{/if}
        {/each}
      </div>

      {#if savedAnalyses.length > 0}
        <div class="saved-analyses">
          <div class="sa-header">
            <span class="sa-title">Analyses sauvegardées</span>
            <span class="hint">{savedAnalyses.length} analyse{savedAnalyses.length > 1 ? 's' : ''}</span>
          </div>
          <div class="sa-list">
            {#each savedAnalyses as entry}
              <div class="sa-item">
                <div class="sa-meta">
                  <span class="sa-name mono">{entry.projName}</span>
                  <span class="sa-count">{entry.adaptersCount} adapter{entry.adaptersCount > 1 ? 's' : ''}</span>
                  <span class="sa-date">{new Date(entry.savedAt).toLocaleString('fr-FR', { day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit' })}</span>
                </div>
                {#if entry.needExcerpt}
                  <p class="sa-excerpt">{entry.needExcerpt}{entry.needExcerpt.length >= 80 ? '…' : ''}</p>
                {/if}
                <div class="sa-actions">
                  <button class="btn btn-primary btn-sm" onclick={() => loadAnalysis(entry.key)}>
                    Reprendre →
                  </button>
                  <button class="btn btn-ghost btn-sm sa-delete" onclick={() => deleteAnalysis(entry.key)} title="Supprimer cette analyse">
                    Supprimer
                  </button>
                </div>
              </div>
            {/each}
          </div>
        </div>
      {/if}

      <button class="btn btn-primary btn-lg" onclick={() => step = 1}>
        Nouvelle analyse →
      </button>
    </div>

  <!-- Étape 1 : besoin client -->
  {:else if step === 1}
    <div class="wizard-card">
      <div class="wc-header">
        <div class="wc-step-badge">1 / {STEPS.length}</div>
        <h2 class="wc-title">Analysez le besoin client</h2>
        <p class="wc-sub">Décrivez le projet en langage naturel. L'IA recommande l'architecture d'adapters optimale.</p>
        <SovereigntyBadge mode="cloud" label="🟠 Bedrock utilisé pour cette étape uniquement" />
      </div>

      <div class="wc-body">
        <label class="form-label">Besoin client</label>
        <textarea
          class="form-textarea"
          rows={6}
          bind:value={need}
          placeholder="Ex: Système d'aide à la rédaction de contrats pour un cabinet d'avocats en droit des affaires…"
          disabled={analyzing}
        ></textarea>

        <!-- Zone documents techniques (CdCF / CdCT) -->
        <div class="form-group mt-16">
          <label class="form-label">Documents techniques (optionnel)</label>
          <p class="form-hint mb-8">Glissez un CdCF, CdCT, spec ou readme pour enrichir l'analyse. Les documents sont envoyés via AWS Bedrock.</p>

          <!-- Drop zone -->
          <div
            class="doc-drop-zone"
            class:drag-over={dropActive}
            ondragover={(e) => { e.preventDefault(); dropActive = true; }}
            ondragleave={() => dropActive = false}
            ondrop={onDocDrop}
            role="button"
            tabindex="0"
            aria-label="Zone de dépôt de documents"
          >
            <span class="ddz-icon">📄</span>
            <span class="ddz-text">Déposez vos fichiers ici</span>
            <span class="ddz-hint">PDF · DOCX · TXT · MD</span>
            <button
              class="btn btn-ghost btn-sm ddz-browse"
              onclick={(e) => { e.stopPropagation(); (e.currentTarget.nextElementSibling as HTMLInputElement)?.click(); }}
            >Parcourir</button>
            <input
              type="file"
              multiple
              accept=".pdf,.docx,.doc,.txt,.md,.rst,.csv"
              style="display:none"
              onchange={(e) => { const t = e.currentTarget as HTMLInputElement; if (t.files) void handleDocFiles(t.files); }}
            />
          </div>

          <!-- Uploads en cours -->
          {#if uploadingDocs.length > 0}
            <div class="doc-uploading mt-8">
              {#each uploadingDocs as fname}
                <div class="doc-uploading-item">
                  <span class="spinner spinner-xs"></span>
                  <span class="mono" style="font-size:11px">{fname}</span>
                </div>
              {/each}
            </div>
          {/if}

          <!-- Documents uploadés -->
          {#if uploadedDocs.length > 0}
            <div class="doc-list mt-8">
              {#each uploadedDocs as doc}
                <div class="doc-item">
                  <span class="doc-icon">📄</span>
                  <div class="doc-meta">
                    <span class="doc-name">{doc.filename}</span>
                    <span class="doc-info">{(doc.char_count / 1000).toFixed(1)} k car.{doc.was_summarized ? ' · résumé auto' : ''}</span>
                  </div>
                  <button class="icon-btn doc-remove" onclick={() => removeDoc(doc.filename)} title="Retirer">✕</button>
                </div>
              {/each}
            </div>
          {/if}

          {#if uploadErr}<div class="api-error mt-6">{uploadErr}</div>{/if}
        </div>

        <div class="form-group mt-12">
          <label class="form-label">Modèle d'analyse</label>
          <select class="form-select" bind:value={aiModel} disabled={analyzing}>
            <option value="us.anthropic.claude-sonnet-4-6">Claude Sonnet 4.6 — via AWS Bedrock (recommandé)</option>
            <option value="us.anthropic.claude-opus-4-7">Claude Opus 4.7 — via AWS Bedrock (plus précis)</option>
            <option value="us.anthropic.claude-haiku-4-5-20251001-v1:0">Claude Haiku 4.5 — via AWS Bedrock (rapide)</option>
          </select>
        </div>

        {#if analyzeErr}<div class="api-error mt-8">{analyzeErr}</div>{/if}

        {#if adapters.length > 0}
          <div class="session-banner mt-12">
            <span class="sb-icon">◉</span>
            <div class="sb-text">
              Analyse sauvegardée — <strong>{adapters.length} adapter{adapters.length > 1 ? 's' : ''}</strong> recommandés.
              {#if analysisSavedAt}
                <span class="sb-date">{new Date(analysisSavedAt).toLocaleString('fr-FR', { day:'2-digit', month:'2-digit', hour:'2-digit', minute:'2-digit' })}</span>
              {/if}
            </div>
            <button class="btn btn-ghost btn-sm" onclick={() => step = 2}>Revoir →</button>
          </div>
        {/if}
      </div>

      <div class="wc-footer">
        <button class="btn btn-ghost" onclick={() => step = 0}>← Retour</button>
        {#if adapters.length > 0}
          <button class="btn btn-ghost btn-sm reanalyze-btn" onclick={resetAnalysis} title="Efface l'analyse et relance un appel Bedrock">
            ↺ Nouvelle analyse
          </button>
        {/if}
        <button
          class="btn btn-primary"
          disabled={analyzing || !need.trim()}
          onclick={analyzeNeed}
        >
          {#if analyzing}<span class="spinner"></span> Analyse en cours…{:else if adapters.length > 0}↺ Relancer l'analyse{:else}Analyser →{/if}
        </button>
        <button class="btn btn-ghost skip-btn" onclick={() => step = 3}>
          Passer (projet sans analyse)
        </button>
      </div>
    </div>

  <!-- Étape 2 : architecture + sources & collecte (vue à onglets) -->
  {:else if step === 2 && adapters.length > 0}

    <!-- Barre d'onglets fixe -->
    <div class="step2-header mb-16">
      <div class="step2-tabs">
        <button class="step2-tab" class:active={step2Tab === 'architecture'} onclick={() => step2Tab = 'architecture'}>
          Architecture
          <span class="tab-badge">{wizardSelection.length}</span>
        </button>
        <button class="step2-tab" class:active={step2Tab === 'sources'} onclick={switchToSources}>
          Sources & Collecte
          <span class="tab-badge">{allSources.length}</span>
        </button>
        <button class="step2-tab" class:active={step2Tab === 'data-analysis'} onclick={switchToDataAnalysis}>
          Analyse données
          {#if totalGeneratedPairs > 0}<span class="tab-badge tab-badge-green">{totalGeneratedPairs}</span>{/if}
        </button>
      </div>
      <div class="step2-actions">
        <button class="btn btn-ghost btn-sm" onclick={() => { step = 1; altTarget = null; sourcesAdapter = null; }}>← Besoin</button>
        <button class="btn btn-ghost btn-sm" onclick={copyYaml}>Copier YAML</button>
        <button class="btn btn-primary btn-sm" onclick={() => step = 3}>✓ Créer le projet</button>
      </div>
    </div>

    <!-- Onglet Architecture -->
    {#if step2Tab === 'architecture'}
    <div class="phase0-layout" class:has-panel={altTarget || sourcesAdapter || gapTarget || overlapTarget}>

      <!-- Col 1 : résumé + score -->
      <div class="col-input">
        <div class="card mb-16">
          <div class="card-title mb-8">Sélection actuelle</div>
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
              <span class="summary-value">~{wizardTotalPairs.toLocaleString()}</span>
              <span class="summary-label">Paires estimées</span>
            </div>
          </div>
          {#if recommendation?.recommendation.notes}
            <div class="notes mt-12">{recommendation.recommendation.notes}</div>
          {/if}
        </div>

        <div class="card mb-16">
          <div class="card-header mb-12">
            <div class="card-title">Score équipe</div>
            {#if evalLoading}
              <span class="spinner spinner-sm"></span>
            {:else}
              <button class="btn-reeval" onclick={triggerEval} title="Réévaluer la sélection actuelle">↺ Réévaluer</button>
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
            {#if selEval.gaps.length}
              <div class="eval-section mt-12">
                <div class="eval-section-header">
                  <span class="meta-label">Lacunes ({selEval.gaps.length})</span>
                  <button
                    class="tag-fill-all"
                    disabled={fillAllLoading}
                    onclick={fillAllGaps}
                    title="Générer un adapter pour chaque lacune en parallèle"
                  >
                    {#if fillAllLoading}<span class="spinner spinner-xs"></span>{/if}
                    Tout couvrir
                  </button>
                </div>
                <div class="action-tags mt-6">
                  {#each selEval.gaps as g}
                    <button
                      class="tag tag-warn tag-action"
                      class:tag-active={gapTarget === g}
                      disabled={fillAllLoading}
                      onclick={() => openFillGap(g)}
                      title="Générer un adapter pour cette lacune"
                    >
                      {#if gapLoading && gapTarget === g}<span class="spinner spinner-xs"></span>{:else}+{/if}
                      {g}
                    </button>
                  {/each}
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
                          <span style="font-size:11px;color:var(--text-dim)">{r.description}</span>
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
                  <button
                    class="tag-fill-all tag-fill-all-danger"
                    disabled={resolveAllLoading}
                    onclick={resolveAllOverlaps}
                    title="Résoudre tous les chevauchements en parallèle (meilleure résolution automatique)"
                  >
                    {#if resolveAllLoading}<span class="spinner spinner-xs"></span>{/if}
                    Tout résoudre
                  </button>
                </div>
                <div class="action-tags mt-6">
                  {#each selEval.overlaps as pair}
                    {@const pairKey = pair as [string, string]}
                    <button
                      class="tag tag-danger tag-action"
                      class:tag-active={overlapTarget?.[0] === pairKey[0] && overlapTarget?.[1] === pairKey[1]}
                      disabled={resolveAllLoading}
                      onclick={() => openResolveOverlap(pairKey)}
                      title="Choisir manuellement la résolution"
                    >
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
                            <span class="mono" style="font-size:10px;color:var(--text-dim)">{pair.join(' ↔ ')}</span>
                          </div>
                          <span style="font-size:11px;color:var(--text-dim)">{resolution.explanation}</span>
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
          {:else if !evalLoading}
            <p class="hint">Sera calculé après l'analyse.</p>
          {/if}
        </div>

      </div>

      <!-- Col 2 : adapter cards -->
      <div class="col-adapters">
        <div class="card">
          <div class="card-header mb-12">
            <div class="card-title">Architecture recommandée</div>
            <div style="display:flex;align-items:center;gap:8px">
              <button class="btn btn-ghost btn-sm" onclick={copyYaml}>Copier YAML</button>
              <span class="hint">{wizardSelection.length} adapter{wizardSelection.length > 1 ? 's' : ''}</span>
            </div>
          </div>

          <div class="adapters-list">
            {#each adapters as adapter (adapter.name)}
              <div class="adapter-with-match">
                <AdapterCard
                  {adapter}
                  active={altTarget?.name === adapter.name || sourcesAdapter?.name === adapter.name}
                  onSources={openSources}
                  onAlternatives={openAlternatives}
                  onRemove={removeAdapter}
                />
                {#if crossMatches[adapter.name]?.total_matching > 0}
                  {@const cm = crossMatches[adapter.name]}
                  {@const oldestMonths = cm.oldest_collected_at ? Math.round((Date.now() - new Date(cm.oldest_collected_at).getTime()) / (1000 * 60 * 60 * 24 * 30)) : null}
                  <div class="cross-match-badge" style="border-color:{oldestMonths != null && oldestMonths > 18 ? 'rgba(247,84,84,.3)' : oldestMonths != null && oldestMonths > 6 ? 'rgba(247,170,80,.3)' : 'rgba(62,207,142,.3)'}">
                    <span style="color:{oldestMonths != null && oldestMonths > 18 ? 'var(--red)' : oldestMonths != null && oldestMonths > 6 ? 'var(--yellow)' : 'var(--green)'}">◈</span>
                    <span class="cmb-text">~{cm.total_matching} paires existantes</span>
                    {#each Object.entries(cm.by_project) as [proj, count]}
                      <span class="cmb-proj">{proj} ({count})</span>
                    {/each}
                    {#if oldestMonths != null}
                      <span class="cmb-age" style="color:{oldestMonths > 18 ? 'var(--red)' : oldestMonths > 6 ? 'var(--yellow)' : 'var(--text-dim)'}">
                        · {oldestMonths < 12 ? `${oldestMonths} mois` : `${Math.round(oldestMonths/12)} an${oldestMonths >= 24 ? 's' : ''}`}
                      </span>
                    {/if}
                  </div>
                {/if}
              </div>
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
            {#if wizardSelection.length === 0}
              <div class="empty-state">Tous les adapters supprimés. ↺ Recommencez.</div>
            {/if}
          </div>

        </div>
      </div>

      <!-- Col 3 : panel contextuel (alternatives / sources / gap / overlap) -->
      {#if altTarget || sourcesAdapter || gapTarget || overlapTarget}
        <div class="col-panel">
          {#if gapTarget}
            <div class="card">
              <div class="card-header mb-12">
                <div class="card-title">Couvrir la lacune</div>
                <button class="icon-btn" onclick={() => { gapTarget = null; gapResult = null; }}>✕</button>
              </div>
              <div class="gap-label mb-12">"{gapTarget}"</div>
              {#if gapLoading}
                <div class="panel-loading"><span class="spinner"></span> Génération en cours…</div>
              {:else if gapError}
                <div class="api-error">{gapError}</div>
              {:else if gapResult}
                <AdapterCard adapter={gapResult} />
                <button class="btn-use mt-8" onclick={applyGapAdapter}>+ Ajouter à la sélection</button>
              {/if}
            </div>
          {/if}
          {#if overlapTarget}
            <div class="card">
              <div class="card-header mb-12">
                <div class="card-title">Résoudre le chevauchement</div>
                <button class="icon-btn" onclick={() => { overlapTarget = null; overlapResolutions = []; }}>✕</button>
              </div>
              <div class="overlap-label mb-12">
                <span class="mono tag-danger-text">{overlapTarget[0]}</span>
                <span style="color:var(--text-dim)"> ↔ </span>
                <span class="mono tag-danger-text">{overlapTarget[1]}</span>
              </div>
              {#if overlapLoading}
                <div class="panel-loading"><span class="spinner"></span> Analyse des solutions…</div>
              {:else if overlapError}
                <div class="api-error">{overlapError}</div>
              {:else if overlapResolutions.length}
                <div class="resolution-list">
                  {#each overlapResolutions as res}
                    <div class="resolution-card">
                      <div class="res-header">
                        <span class="res-type res-type-{res.type}">{res.label}</span>
                        <button class="btn-use" onclick={() => applyOverlapResolution(res)}>Appliquer</button>
                      </div>
                      <p class="res-explanation">{res.explanation}</p>
                      {#if res.adapters.length}
                        <div class="res-adapters">
                          {#each res.adapters as a}
                            <div class="res-adapter-name mono">{a.name}</div>
                            <div class="res-adapter-desc">{a.description}</div>
                          {/each}
                        </div>
                      {/if}
                    </div>
                  {/each}
                </div>
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

    </div>

    <!-- Onglet Sources & Collecte -->
    {:else if step2Tab === 'sources'}
      {@const tempProjectId = projName.trim() || 'wizard-temp'}
      <div class="collect-layout">

        <!-- Col gauche : sélection + paramètres -->
        <div class="collect-left">
          <div class="card mb-16">
            <div class="card-header mb-12">
              <div class="card-title">Sources proposées</div>
              <span class="hint">{collectSelectedCount} / {sourceEntries.length} sélectionnées</span>
            </div>
            {#if sourceEntries.length === 0}
              <div class="empty-state">Aucune source détectée dans les adapters.</div>
            {:else}
              {@const byAdapter = sourceEntries.reduce<Record<string, typeof sourceEntries>>((acc, e) => {
                (acc[e.adapter] ??= []).push(e); return acc;
              }, {})}
              {#each Object.entries(byAdapter) as [adapterName, entries]}
                <div class="source-group mb-12">
                  <div class="source-group-label">{adapterName}</div>
                  {#each entries as entry}
                    <label class="source-row">
                      <input type="checkbox" bind:checked={entry.selected} class="source-checkbox" />
                      <span class="collector-badge collector-badge--{entry.collector_type}">
                        {entry.collector_type === 'rosetta_code' ? 'Rosetta' : entry.collector_type === 'crawl4ai' ? 'Web' : entry.collector_type === 'manual' ? 'Manuel' : 'GitHub'}
                      </span>
                      <span class="source-name">{entry.source}</span>
                      {#if entry.collector_type === 'crawl4ai' && entry.source_value?.startsWith('http')}
                        <a href={entry.source_value} target="_blank" rel="noopener noreferrer" class="source-ext" title="Ouvrir">↗</a>
                      {:else if entry.collector_type === 'rosetta_code'}
                        <a href="https://rosettacode.org/wiki/Category:Programming_Tasks" target="_blank" rel="noopener noreferrer" class="source-ext" title="Rosetta Code">↗</a>
                      {/if}
                    </label>
                  {/each}
                </div>
              {/each}
            {/if}
          </div>

          <div class="card">
            <div class="card-title mb-12">Paramètres de collecte</div>
            <div class="form-group">
              <label class="form-label" for="collect-topic">Sujet / mots-clés</label>
              <input id="collect-topic" class="form-input" bind:value={collectTopic} placeholder="ex: contrats droit des affaires" />
            </div>
            <div class="form-group mt-8">
              <label class="form-label" for="collect-max">Paires max par source</label>
              <input id="collect-max" type="number" min="10" max="500" class="form-input" bind:value={collectMaxPairs} />
            </div>
            {#if collectErr}<div class="api-error mt-8">{collectErr}</div>{/if}
            <button
              class="btn btn-primary mt-12"
              style="width:100%"
              disabled={collectRunning || collectSelectedCount === 0 || collectStatus === 'completed'}
              onclick={() => launchCollect(tempProjectId)}
            >
              {#if collectRunning}<span class="spinner"></span> Collecte en cours…
              {:else if collectStatus === 'completed'}✓ Collecte terminée
              {:else}⬇ Lancer le scraping{/if}
            </button>
          </div>
        </div>

        <!-- Col droite : logs + résultats -->
        <div class="collect-right">
          {#if collectLogs.length === 0 && collectStatus === 'idle'}
            <div class="collect-placeholder">
              <div class="cp-icon">⬇</div>
              <div class="cp-title">Collecte web automatique</div>
              <div class="cp-sub">Sélectionnez les sources à gauche, ajustez les paramètres, puis lancez le scraping pour collecter des paires d'entraînement.</div>
            </div>
          {/if}

          {#if collectLogs.length > 0 || collectRunning}
            <div class="card mb-16">
              <div class="card-header mb-8">
                <div class="card-title">Journal de collecte</div>
                <div style="display:flex;align-items:center;gap:8px">
                  {#if collectRunning}<span class="spinner spinner-sm"></span>{/if}
                  {#if collectStatus === 'completed'}<span class="status-ok">✓ Terminé</span>{/if}
                  {#if collectStatus === 'failed'}<span class="status-err">✕ Erreur</span>{/if}
                </div>
              </div>
              <pre id="collect-log" class="log-box">{collectLogs.join('\n')}</pre>
            </div>
          {/if}

          {#if collectStatus === 'completed'}
            <div class="card">
              <div class="card-header mb-12">
                <div>
                  <div class="card-title">Données collectées</div>
                  <div class="hint mt-4">{collectedTotal} paires brutes disponibles</div>
                </div>
                <button
                  class="btn btn-primary btn-sm"
                  disabled={pipelineRunning || pipelineStatus === 'completed'}
                  onclick={() => launchPipeline(tempProjectId)}
                >
                  {#if pipelineRunning}<span class="spinner"></span> Pipeline…
                  {:else if pipelineStatus === 'completed'}✓ Traité
                  {:else}⟳ Lancer le pipeline{/if}
                </button>
              </div>

              {#if pipelineLogs.length > 0 || pipelineRunning}
                <pre id="pipeline-log" class="log-box mb-12">{pipelineLogs.join('\n')}</pre>
              {/if}

              {#if collectedPairs.length > 0}
                <div class="pairs-header mb-8">
                  <span class="meta-label">Aperçu — 20 premières paires</span>
                  <button class="sources-toggle" onclick={() => showPairsPreview = !showPairsPreview}>
                    {showPairsPreview ? '▾ Masquer' : "▸ Afficher l'aperçu"}
                  </button>
                </div>
                {#if showPairsPreview}
                  <div class="pairs-list">
                    {#each collectedPairs as pair, i}
                      <div class="pair-item">
                        <div class="pair-idx">#{i + 1}</div>
                        <div class="pair-body">
                          <div class="pair-prompt">{pair.prompt}</div>
                          <div class="pair-completion">{pair.completion}</div>
                        </div>
                        {#if pair.quality_score != null}
                          <div class="pair-score" style="color:{pair.quality_score >= 0.7 ? 'var(--green)' : pair.quality_score >= 0.4 ? 'var(--yellow)' : 'var(--red)'}">
                            {(pair.quality_score * 20).toFixed(0)}/20
                          </div>
                        {/if}
                      </div>
                    {/each}
                  </div>
                {/if}
              {/if}
            </div>
          {/if}
        </div>
      </div>
    <!-- Onglet Analyse données -->
    {:else if step2Tab === 'data-analysis'}
      <div class="da-layout">

        <!-- Col gauche : sélecteur d'adapter -->
        <div class="da-left">
          <div class="card mb-16">
            <div class="card-title mb-12">Adapter à analyser</div>
            <div class="da-adapter-list">
              {#each wizardSelection as adapter}
                {@const an = dataAnalyses[adapter.name]}
                <button
                  class="da-adapter-btn"
                  class:active={dataAnalysisAdapter === adapter.name}
                  onclick={() => { dataAnalysisAdapter = adapter.name; }}
                >
                  <span class="da-adapter-name mono">{adapter.name}</span>
                  {#if an?.result}
                    <span class="da-score-pill" style="background:{an.result.quality_report.avg_score >= 7 ? 'rgba(80,200,120,.15)' : an.result.quality_report.avg_score >= 4 ? 'rgba(255,180,0,.15)' : 'rgba(247,84,84,.15)'}; color:{an.result.quality_report.avg_score >= 7 ? 'var(--green)' : an.result.quality_report.avg_score >= 4 ? 'var(--yellow)' : 'var(--red)'}">
                      {an.result.quality_report.avg_score.toFixed(1)}/10
                    </span>
                  {:else if an?.loading}
                    <span class="spinner spinner-xs"></span>
                  {/if}
                </button>
              {/each}
            </div>
          </div>

          {#if totalGeneratedPairs > 0}
            <div class="card da-total-card">
              <div class="da-total-count">{totalGeneratedPairs}</div>
              <div class="da-total-label">paires synthétiques générées</div>
              <div class="hint mt-4">Disponibles pour l'ingestion après création du projet.</div>
            </div>
          {/if}
        </div>

        <!-- Col droite : panneau d'analyse pour l'adapter sélectionné -->
        <div class="da-right">
          {#if !dataAnalysisAdapter}
            <div class="collect-placeholder">
              <div class="cp-icon">◎</div>
              <div class="cp-title">Sélectionnez un adapter</div>
            </div>
          {:else}
            {@const selectedAdapter = wizardSelection.find(a => a.name === dataAnalysisAdapter)}
            {@const an = dataAnalyses[dataAnalysisAdapter]}
            {#if selectedAdapter}
              <div class="card mb-16">
                <div class="card-header mb-12">
                  <div>
                    <div class="card-title">Analyse — <span class="mono">{selectedAdapter.name}</span></div>
                    <div class="hint mt-2">{selectedAdapter.description}</div>
                  </div>
                  <div style="display:flex;align-items:center;gap:8px">
                    <label class="da-n-label" for="da-ngen">Paires à générer</label>
                    <input
                      id="da-ngen"
                      type="number" min="1" max="50" class="da-n-input"
                      value={an?.nGenerate ?? 10}
                      onchange={(e) => {
                        const v = parseInt((e.currentTarget as HTMLInputElement).value) || 10;
                        dataAnalyses = { ...dataAnalyses, [dataAnalysisAdapter]: { ...(dataAnalyses[dataAnalysisAdapter] ?? { result: null, loading: false, error: '', nGenerate: 10 }), nGenerate: Math.min(50, Math.max(1, v)) } };
                      }}
                    />
                    <button
                      class="btn btn-primary btn-sm"
                      disabled={an?.loading || (collectedPairs.length === 0 && collectStatus !== 'completed')}
                      onclick={() => runDataAnalysis(selectedAdapter)}
                    >
                      {#if an?.loading}<span class="spinner"></span> Analyse Haiku…
                      {:else if an?.result}↺ Relancer
                      {:else}◎ Analyser{/if}
                    </button>
                  </div>
                </div>

                {#if collectedPairs.length === 0 && collectStatus !== 'completed'}
                  <div class="da-hint-collect">
                    <span class="da-hint-icon">⚠</span>
                    Lancez d'abord une collecte dans l'onglet <button class="da-link" onclick={switchToSources}>Sources & Collecte</button> pour avoir des paires à analyser.
                    L'analyse de qualité et la génération synthétique fonctionnent indépendamment.
                  </div>
                {/if}

                {#if an?.error}
                  <div class="api-error">{an.error}</div>
                {/if}

                {#if an?.result}
                  {@const qr = an.result.quality_report}

                  <!-- Rapport qualité -->
                  <div class="da-section mb-16">
                    <div class="da-section-title">Rapport qualité ({qr.total_analyzed} paires analysées)</div>
                    <div class="da-verdict-grid">
                      <div class="da-verdict-item da-verdict-keep">
                        <span class="da-verdict-count">{qr.n_keep}</span>
                        <span class="da-verdict-label">✓ À garder</span>
                      </div>
                      <div class="da-verdict-item da-verdict-fix">
                        <span class="da-verdict-count">{qr.n_fix}</span>
                        <span class="da-verdict-label">⚑ À corriger</span>
                      </div>
                      <div class="da-verdict-item da-verdict-discard">
                        <span class="da-verdict-count">{qr.n_discard}</span>
                        <span class="da-verdict-label">✕ À supprimer</span>
                      </div>
                    </div>
                    <div class="da-avg-score mt-10">
                      Score moyen : <span style="color:{qr.avg_score >= 7 ? 'var(--green)' : qr.avg_score >= 4 ? 'var(--yellow)' : 'var(--red)'}; font-weight:600">{qr.avg_score.toFixed(1)}/10</span>
                    </div>
                    {#if qr.common_issues.length > 0}
                      <div class="da-issues mt-8">
                        <span class="meta-label">Problèmes récurrents</span>
                        <div class="tags mt-4">
                          {#each qr.common_issues as issue}
                            <span class="tag tag-warn">{issue}</span>
                          {/each}
                        </div>
                      </div>
                    {/if}
                  </div>

                  <!-- Paires générées -->
                  <div class="da-section mb-16">
                    <div class="da-section-header">
                      <div class="da-section-title">Paires synthétiques générées ({an.result.generated_pairs.length})</div>
                      {#if an.result.generated_pairs.length > 0}
                        <button class="btn-use" onclick={() => addGeneratedPairs(selectedAdapter.name)}>
                          + Ajouter toutes ({an.result.generated_pairs.length})
                        </button>
                      {/if}
                    </div>
                    {#if an.result.generated_pairs.length === 0}
                      <div class="hint mt-4">Toutes les paires ont été ajoutées.</div>
                    {:else}
                      <div class="da-pairs-list mt-8">
                        {#each an.result.generated_pairs.slice(0, 5) as pair, i}
                          <div class="da-pair-item">
                            <div class="da-pair-idx">#{i + 1}</div>
                            <div class="da-pair-body">
                              <div class="da-pair-prompt">{pair.prompt.slice(0, 120)}{pair.prompt.length > 120 ? '…' : ''}</div>
                              <div class="da-pair-completion">{pair.completion.slice(0, 120)}{pair.completion.length > 120 ? '…' : ''}</div>
                            </div>
                          </div>
                        {/each}
                        {#if an.result.generated_pairs.length > 5}
                          <div class="hint">… et {an.result.generated_pairs.length - 5} paires supplémentaires</div>
                        {/if}
                      </div>
                    {/if}
                    {#if (generatedPairsStore[selectedAdapter.name]?.length ?? 0) > 0}
                      <div class="da-added-count mt-8">
                        <span style="color:var(--green)">✓</span>
                        {generatedPairsStore[selectedAdapter.name].length} paires ajoutées pour cet adapter
                      </div>
                    {/if}
                  </div>

                  <!-- Brief données manuelles -->
                  <div class="da-section">
                    <div class="da-section-header">
                      <div class="da-section-title">Brief données manuelles</div>
                      <button class="btn btn-ghost btn-sm" onclick={() => navigator.clipboard.writeText(an.result!.manual_brief)}>
                        Copier
                      </button>
                    </div>
                    <div class="da-brief mt-8">{an.result.manual_brief}</div>
                  </div>
                {/if}
              </div>
            {/if}
          {/if}
        </div>
      </div>
    {/if}

  <!-- Étape 3 : créer le projet -->
  {:else if step === 3}
    <div class="wizard-card">
      <div class="wc-header">
        <div class="wc-step-badge">3 / {STEPS.length}</div>
        <h2 class="wc-title">Créer le projet</h2>
        <p class="wc-sub">Configurez les paramètres de base. Vous pourrez tout modifier ensuite.</p>
      </div>

      <div class="wc-body">
        <div class="form-group">
          <label class="form-label" for="proj-name">Nom du projet</label>
          <input id="proj-name" class="form-input" bind:value={projName} placeholder="ex: legal-contracts" />
        </div>
        <div class="grid-2 mt-12">
          <div class="form-group">
            <label class="form-label" for="proj-domain">Domaine</label>
            <select id="proj-domain" class="form-select" bind:value={projDomain}>
              {#each DOMAINS as d}<option value={d}>{d}</option>{/each}
            </select>
          </div>
          <div class="form-group">
            <label class="form-label" for="proj-class">Classification</label>
            <select id="proj-class" class="form-select" bind:value={projClass}>
              <option value="public">public</option>
              <option value="interne">interne</option>
              <option value="confidentiel">confidentiel</option>
              <option value="secret">secret</option>
            </select>
          </div>
        </div>
        <div class="form-group mt-12">
          <label class="form-label" for="base-model">Modèle de base</label>
          <select id="base-model" class="form-select" bind:value={baseModel}>
            {#each BASE_MODELS as m}<option value={m.id}>{m.label}</option>{/each}
          </select>
        </div>
        {#if createErr}<div class="api-error mt-8">{createErr}</div>{/if}
      </div>

      <div class="wc-footer">
        <button class="btn btn-ghost" onclick={() => step = recommendation ? 2 : 1}>← Retour</button>
        <button
          class="btn btn-primary"
          disabled={creating || !projName.trim()}
          onclick={createProj}
        >
          {creating ? 'Création…' : 'Créer et continuer →'}
        </button>
      </div>
    </div>

  <!-- Étape 4 : données -->
  {:else if step === 4}
    <div class="wizard-card">
      <div class="wc-header">
        <div class="wc-step-badge">4 / {STEPS.length}</div>
        <h2 class="wc-title">Préparer les données</h2>
        <p class="wc-sub">
          Le projet <span class="mono text-accent">{projectId}</span> est créé.
          Ingérez et nettoyez vos paires d'entraînement avant de lancer le fine-tuning.
        </p>
      </div>

      <div class="wc-body">
        <div class="data-checklist">
          <div class="dc-item">
            <span class="dc-icon">◧</span>
            <div class="dc-text">
              <div class="dc-label">Ingestion des sources</div>
              <div class="dc-sub">Copiez vos fichiers dans <span class="mono">datalake/projects/{projectId}/raw/</span></div>
            </div>
            <a href="/data/ingest?project={projectId}" class="btn btn-ghost btn-sm">Ouvrir →</a>
          </div>
          <div class="dc-item">
            <span class="dc-icon">⟳</span>
            <div class="dc-text">
              <div class="dc-label">Pipeline de nettoyage</div>
              <div class="dc-sub">Score qualité, déduplication, filtrage des tokens</div>
            </div>
            <a href="/data/pipeline?project={projectId}" class="btn btn-ghost btn-sm">Ouvrir →</a>
          </div>
          <div class="dc-item">
            <span class="dc-icon">◩</span>
            <div class="dc-text">
              <div class="dc-label">Collecte web (optionnel)</div>
              <div class="dc-sub">Enrichissement automatique via recherche ciblée</div>
            </div>
            <a href="/data/collect?project={projectId}" class="btn btn-ghost btn-sm">Ouvrir →</a>
          </div>
        </div>

        {#if !dataReady}
          <div class="data-check-row mt-16">
            <button class="btn btn-ghost" onclick={checkDataReady}>↻ Vérifier l'état des données</button>
            {#if pairCount > 0}
              <span class="text-muted" style="font-size:var(--text-sm)">
                {pairCount} paires prêtes {pairCount < 200 ? '(minimum 200 requis)' : ''}
              </span>
            {/if}
          </div>
        {:else}
          <div class="data-ok mt-16">
            <span class="text-green">✓</span> {pairCount} paires fine-tuning-ready — prêt pour le training.
          </div>
        {/if}
      </div>

      <div class="wc-footer">
        <button class="btn btn-ghost" onclick={() => step = 3}>← Retour</button>
        <button class="btn btn-primary" onclick={() => step = 5}>
          Continuer vers le training →
        </button>
      </div>
    </div>

  <!-- Étape 5 : training -->
  {:else if step === 5}
    <div class="wizard-card">
      <div class="wc-header">
        <div class="wc-step-badge">5 / {STEPS.length}</div>
        <h2 class="wc-title">Lancer le fine-tuning</h2>
        <p class="wc-sub">Configurez et lancez un run QLoRA sur votre dataset.</p>
      </div>

      <div class="wc-body">
        <div class="action-card">
          <div class="ac-icon">▷</div>
          <div class="ac-text">
            <div class="ac-title">Ouvrir le configurateur de training</div>
            <div class="ac-sub">Paramétrez les hyperparamètres (rank, LR, batch…) et lancez le run MLX.</div>
          </div>
          <a href="/train?project={projectId}" class="btn btn-primary">Ouvrir →</a>
        </div>

        <div class="form-group mt-16">
          <label class="form-label" for="run-id">ID du run lancé (à coller après lancement)</label>
          <input
            id="run-id"
            class="form-input mono"
            bind:value={runId}
            placeholder="ex: a1b2c3d4"
          />
          <div class="form-hint">Visible dans /train → historique des runs</div>
        </div>
      </div>

      <div class="wc-footer">
        <button class="btn btn-ghost" onclick={() => step = 4}>← Retour</button>
        <button
          class="btn btn-primary"
          disabled={!runId.trim()}
          onclick={() => step = 6}
        >
          Run lancé, continuer →
        </button>
      </div>
    </div>

  <!-- Étape 6 : évaluation -->
  {:else if step === 6}
    <div class="wizard-card">
      <div class="wc-header">
        <div class="wc-step-badge">6 / {STEPS.length}</div>
        <h2 class="wc-title">Évaluer le modèle</h2>
        <p class="wc-sub">Évaluation obligatoire avant déploiement — BLEU, ROUGE-L, pass rate, latence.</p>
      </div>

      <div class="wc-body">
        {#if !evalLaunched}
          <div class="action-card">
            <div class="ac-icon">◉</div>
            <div class="ac-text">
              <div class="ac-title">Lancer l'évaluation du run <span class="mono text-accent">{runId}</span></div>
              <div class="ac-sub">Compare votre modèle au baseline Bedrock Nova Pro.</div>
            </div>
            <button class="btn btn-primary" onclick={launchEval}>▷ Évaluer</button>
          </div>
          <div class="action-card mt-12">
            <div class="ac-icon">◐</div>
            <div class="ac-text">
              <div class="ac-title">Tester dans le Playground</div>
              <div class="ac-sub">Envoyez des prompts manuels pour valider la qualité.</div>
            </div>
            <a href="/playground?project={projectId}" class="btn btn-ghost">Ouvrir →</a>
          </div>
        {:else}
          <div class="data-ok">
            <span class="text-green">✓</span> Évaluation lancée pour le run <span class="mono">{runId}</span>.
          </div>
          <a href="/eval" class="btn btn-ghost mt-12">Voir les résultats →</a>
        {/if}
      </div>

      <div class="wc-footer">
        <button class="btn btn-ghost" onclick={() => step = 5}>← Retour</button>
        <button class="btn btn-primary" onclick={() => step = 7}>Continuer vers la livraison →</button>
      </div>
    </div>

  <!-- Étape 7 : livraison -->
  {:else if step === 7}
    <div class="wizard-card">
      <div class="wc-header">
        <div class="wc-step-badge">7 / {STEPS.length}</div>
        <h2 class="wc-title">Livraison du modèle</h2>
        <p class="wc-sub">Fusionnez l'adapter, passez la gate qualité, promouvez en production.</p>
      </div>

      <div class="wc-body">
        <div class="action-card">
          <div class="ac-icon">⬡</div>
          <div class="ac-text">
            <div class="ac-title">Déployer le modèle</div>
            <div class="ac-sub">Fuse → staging → gate → production → serving port.</div>
          </div>
          <a href="/delivery?project={projectId}" class="btn btn-primary">Ouvrir →</a>
        </div>

        <div class="done-banner mt-24">
          <div class="done-icon">✓</div>
          <div>
            <div class="done-title">Vous avez complété toutes les étapes</div>
            <div class="done-sub">Le projet <span class="mono text-accent">{projectId}</span> est prêt pour la mise en production.</div>
          </div>
        </div>
      </div>

      <div class="wc-footer">
        <button class="btn btn-ghost" onclick={() => step = 6}>← Retour</button>
        <button class="btn btn-ghost" onclick={() => goto(`/projects/${projectId}`)}>Voir le projet</button>
        <button class="btn btn-primary" onclick={() => { step = 0; need = ''; projName = ''; projectId = ''; runId = ''; recommendation = null; evalLaunched = false; }}>
          ↺ Nouveau projet
        </button>
      </div>
    </div>
  {/if}

  <!-- Barre de progression -->
  {#if step > 0}
    {@const progressStep = (step === 2 && step2Tab === 'sources') ? 2.5 : step}
    <div class="progress-bar-wrap">
      <div class="progress-bar" style="width:{(progressStep / STEPS.length) * 100}%"></div>
    </div>
  {/if}

</div>

<style>
  .wizard-content {
    max-width: 860px;
    margin: 0 auto;
    width: 100%;
  }

  /* Step 2 — layout identique à /advisor */
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

  .summary-grid { display: grid; grid-template-columns: repeat(3,1fr); gap:10px; }
  .summary-item {
    display:flex; flex-direction:column; align-items:center;
    padding:10px 8px; background:var(--surface-2); border-radius:var(--radius); gap:3px;
  }
  .summary-value { font-size:20px; font-weight:700; color:var(--accent); }
  .summary-label { font-size:11px; color:var(--text-muted); text-align:center; }
  .notes { font-size:12px; color:var(--text-muted); background:var(--surface-2); border-radius:var(--radius); padding:10px 14px; font-style:italic; line-height:1.5; }

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
  .sources-grid { display:flex; flex-wrap:wrap; gap:6px; }
  .source-chip {
    display:inline-flex; align-items:center; gap:4px;
    padding:4px 10px; border-radius:999px;
    background:var(--surface-2); border:1px solid var(--border);
    text-decoration:none; font-size:11px; color:var(--accent);
    transition:border-color 150ms, background 150ms; max-width:180px;
  }
  .source-chip:hover { border-color:var(--accent); background:var(--accent-dim); }
  .source-chip-name { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .source-chip-icon { font-size:10px; color:var(--text-muted); flex-shrink:0; }

  .adapters-list { display:flex; flex-direction:column; gap:10px; }
  .alt-wrapper { display:flex; flex-direction:column; gap:6px; }
  .btn-use {
    align-self:flex-end; background:none; border:1px solid var(--border);
    border-radius:var(--radius); padding:4px 12px; font-size:12px; cursor:pointer;
    color:var(--accent); transition:border-color 150ms, background 150ms;
  }
  .btn-use:hover { border-color:var(--accent); background:var(--accent-dim); }

  .tags { display:flex; flex-wrap:wrap; gap:4px; }
  .tag { font-size:11px; padding:2px 8px; border-radius:999px; background:var(--surface-2); color:var(--text-muted); border:1px solid var(--border); }
  .tag-warn   { color:#f59e0b; border-color:rgba(245,158,11,.3); }
  .tag-danger { color:var(--red); border-color:rgba(255,80,80,.3); }

  /* Tags interactifs gap/overlap */
  .action-tags { display:flex; flex-wrap:wrap; gap:5px; }
  .tag-action {
    cursor: pointer; transition: filter 150ms;
    background: none;
  }
  .tag-action:hover { filter: brightness(1.3); }
  .tag-action.tag-active { box-shadow: 0 0 0 2px currentColor; }
  .tag-action:disabled { opacity:.5; cursor:default; }
  .eval-section-header { display:flex; align-items:center; justify-content:space-between; gap:6px; }
  .meta-hint { font-size:10px; color:var(--text-dim); font-style:italic; }
  .spinner-xs { width:9px; height:9px; border-width:1.5px; }
  .mt-6 { margin-top:6px; }
  .mt-10 { margin-top:10px; }

  .btn-reeval {
    font-size:10px; font-weight:600; padding:2px 8px;
    background: rgba(91,152,248,.1); border:1px solid rgba(91,152,248,.25);
    border-radius:999px; color:var(--accent); cursor:pointer;
    display:inline-flex; align-items:center; gap:4px;
    transition: background 150ms;
  }
  .btn-reeval:hover { background: rgba(91,152,248,.2); }
  .tag-fill-all {
    font-size:10px; font-weight:600; padding:2px 8px;
    background: rgba(245,158,11,.12); border:1px solid rgba(245,158,11,.3);
    border-radius:999px; color:#f59e0b; cursor:pointer;
    display:inline-flex; align-items:center; gap:4px;
    transition: background 150ms;
  }
  .tag-fill-all:hover:not(:disabled) { background: rgba(245,158,11,.22); }
  .tag-fill-all:disabled { opacity:.5; cursor:default; }
  .tag-fill-all-danger {
    background: rgba(247,84,84,.12); border-color: rgba(247,84,84,.3); color: var(--red);
  }
  .tag-fill-all-danger:hover:not(:disabled) { background: rgba(247,84,84,.22); }

  .fill-all-results {
    background: rgba(62,207,142,.06); border:1px solid rgba(62,207,142,.2);
    border-radius:var(--radius); padding:8px 10px;
  }
  .fill-all-header { display:flex; align-items:center; justify-content:space-between; }
  .fill-all-list { display:flex; flex-direction:column; gap:5px; }
  .fill-all-item { display:flex; flex-direction:column; gap:1px; padding:3px 0; border-bottom:1px solid var(--border); }
  .fill-all-item:last-child { border-bottom:none; }

  /* Panneau lacune */
  .gap-label {
    font-size:12px; color:var(--yellow, #f59e0b);
    background:rgba(245,158,11,.08); border:1px solid rgba(245,158,11,.2);
    border-radius:var(--radius); padding:8px 10px; font-style:italic; line-height:1.5;
  }

  /* Panneau chevauchement */
  .overlap-label { display:flex; align-items:center; gap:4px; font-size:12px; }
  .tag-danger-text { color:var(--red); }

  .resolution-list { display:flex; flex-direction:column; gap:10px; }
  .resolution-card {
    background:var(--surface-2); border:1px solid var(--border);
    border-radius:var(--radius-md); padding:10px 12px;
  }
  .res-header { display:flex; align-items:center; justify-content:space-between; gap:8px; margin-bottom:6px; }
  .res-type {
    font-size:10px; font-weight:700; text-transform:uppercase;
    letter-spacing:.06em; padding:2px 8px; border-radius:999px;
  }
  .res-type-clarify { background:rgba(91,152,248,.15); color:var(--accent); }
  .res-type-merge   { background:rgba(62,207,142,.15); color:var(--green); }
  .res-type-remove  { background:rgba(247,84,84,.15);  color:var(--red); }
  .res-explanation { font-size:11px; color:var(--text-muted); line-height:1.5; margin:0 0 6px; }
  .res-adapters { display:flex; flex-direction:column; gap:3px; }
  .res-adapter-name { font-size:11px; font-weight:600; color:var(--accent); }
  .res-adapter-desc { font-size:11px; color:var(--text-dim); line-height:1.4; }

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

  .empty-state { text-align:center; padding:24px; color:var(--text-muted); font-size:13px; }
  .validation-bar { border-top:1px solid var(--border); padding-top:16px; display:flex; align-items:center; gap:10px; }
  .panel-loading { display:flex; align-items:center; gap:10px; color:var(--text-muted); font-size:13px; padding:20px 0; }
  .mono { font-family:var(--font-mono); font-size:0.9em; }
  .btn-sm { padding:4px 12px; font-size:12px; }

  @media (max-width:1300px) { .phase0-layout, .phase0-layout.has-panel { grid-template-columns:1fr; } }

  /* Welcome */
  .welcome {
    display: flex; flex-direction: column; align-items: center;
    text-align: center; padding: var(--sp-10) var(--sp-6);
    gap: 20px;
  }
  .welcome-icon { font-size: 40px; }
  .welcome-title { font-size: var(--text-2xl); font-weight: 700; }
  .welcome-sub { font-size: var(--text-base); color: var(--text-muted); line-height: 1.7; max-width: 560px; }
  .welcome-steps {
    display: flex; align-items: center; flex-wrap: wrap; justify-content: center;
    gap: 6px; margin: 8px 0;
  }
  .ws-item { display: flex; flex-direction: column; align-items: center; gap: 3px; }
  .ws-num { width: 24px; height: 24px; border-radius: 50%; background: var(--surface-2); border: 2px solid var(--border); display: flex; align-items: center; justify-content: center; font-size: var(--text-xs); font-weight: 700; color: var(--text-muted); }
  .ws-label { font-size: 10px; color: var(--text-dim); white-space: nowrap; }
  .ws-arrow { font-size: 12px; color: var(--text-dim); padding: 0 2px; }

  .btn-lg { padding: 12px 28px; font-size: var(--text-base); }

  /* Wizard card */
  .wizard-card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius-lg);
    overflow: hidden;
  }

  .wc-header {
    padding: 20px 24px 16px;
    border-bottom: 1px solid var(--border);
    display: flex; flex-direction: column; gap: 6px;
  }
  .wc-step-badge {
    font-size: var(--text-xs); font-weight: 700;
    text-transform: uppercase; letter-spacing: .08em;
    color: var(--accent);
  }
  .wc-title { font-size: var(--text-xl); font-weight: 700; }
  .wc-sub   { font-size: var(--text-sm); color: var(--text-muted); line-height: 1.5; margin: 0; }

  .wc-body   { padding: 20px 24px; }
  .wc-footer {
    padding: 14px 24px;
    border-top: 1px solid var(--border);
    display: flex; align-items: center; gap: 10px;
    background: var(--surface-2);
  }

  .skip-btn { margin-left: auto; font-size: var(--text-xs); opacity: .7; }

  /* Architecture */
  .arch-summary { display: grid; grid-template-columns: repeat(3,1fr); gap: 10px; }
  .arch-stat { display: flex; flex-direction: column; align-items: center; padding: 12px; background: var(--surface-2); border-radius: var(--radius); gap: 3px; }
  .arch-num { font-size: var(--text-xl); font-weight: 700; color: var(--accent); }
  .arch-lbl { font-size: var(--text-xs); color: var(--text-muted); }
  .arch-notes { font-size: var(--text-sm); color: var(--text-muted); background: var(--surface-2); border-radius: var(--radius); padding: 10px 14px; font-style: italic; line-height: 1.5; }

  /* Team eval */
  .team-eval { background: var(--surface-2); border: 1px solid var(--border); border-radius: var(--radius-md); padding: 14px 16px; }
  .team-eval-header { display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }
  .te-label { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--text-muted); font-weight: 600; }
  .te-body { display: flex; flex-direction: column; gap: 0; }
  .te-hero { display: flex; align-items: baseline; gap: 3px; margin-bottom: 10px; }
  .te-value { font-size: 32px; font-weight: 800; line-height: 1; }
  .te-unit  { font-size: 16px; color: var(--text-muted); }
  .score-bars { display: flex; flex-direction: column; gap: 5px; }
  .score-bar-row { display: flex; align-items: center; gap: 8px; }
  .score-bar-label { font-size: 11px; color: var(--text-muted); width: 90px; flex-shrink: 0; }
  .score-bar-track { flex: 1; height: 5px; background: var(--surface-3, var(--border)); border-radius: 3px; overflow: hidden; }
  .score-bar-fill  { height: 100%; border-radius: 3px; transition: width 400ms ease; }
  .score-bar-val   { font-size: 11px; font-weight: 600; width: 24px; text-align: right; }
  .te-section { display: flex; flex-direction: column; }
  .te-meta-label { font-size: 11px; text-transform: uppercase; letter-spacing: .05em; color: var(--text-muted); }
  .te-reco { font-size: 12px; color: var(--text-muted); background: var(--surface); border-radius: var(--radius); padding: 8px 10px; font-style: italic; line-height: 1.5; margin: 0; }
  .te-hint { font-size: 12px; color: var(--text-dim); margin: 0; }
  .tags { display: flex; flex-wrap: wrap; gap: 4px; }
  .tag { font-size: 11px; padding: 2px 8px; border-radius: 999px; background: var(--surface); color: var(--text-muted); border: 1px solid var(--border); }
  .tag-warn { color: #f59e0b; border-color: rgba(245,158,11,.3); }

  /* Adapter cards dans wizard */
  .adapter-cards { display: flex; flex-direction: column; gap: 8px; }

  .spinner-sm { width: 12px; height: 12px; border-width: 2px; }

  /* Data checklist */
  .data-checklist { display: flex; flex-direction: column; gap: 8px; }
  .dc-item {
    display: flex; align-items: center; gap: 12px;
    padding: 12px 14px; border-radius: var(--radius-md);
    background: var(--surface-2); border: 1px solid var(--border);
  }
  .dc-icon { font-size: 16px; color: var(--accent); flex-shrink: 0; }
  .dc-text { flex: 1; }
  .dc-label { font-weight: 500; font-size: var(--text-sm); }
  .dc-sub   { font-size: var(--text-xs); color: var(--text-muted); margin-top: 2px; }

  .data-check-row { display: flex; align-items: center; gap: 12px; }
  .data-ok { display: flex; align-items: center; gap: 8px; font-size: var(--text-sm); color: var(--text-muted); }

  /* Action cards */
  .action-card {
    display: flex; align-items: center; gap: 14px;
    padding: 14px 16px; border-radius: var(--radius-md);
    background: var(--surface-2); border: 1px solid var(--border);
  }
  .ac-icon  { font-size: 20px; color: var(--accent); flex-shrink: 0; }
  .ac-text  { flex: 1; }
  .ac-title { font-weight: 500; font-size: var(--text-sm); }
  .ac-sub   { font-size: var(--text-xs); color: var(--text-muted); margin-top: 2px; }

  /* Done */
  .done-banner {
    display: flex; align-items: center; gap: 16px;
    padding: 20px; border-radius: var(--radius-md);
    background: var(--green-dim); border: 1px solid rgba(62,207,142,.3);
  }
  .done-icon  { font-size: 28px; color: var(--green); flex-shrink: 0; }
  .done-title { font-weight: 600; font-size: var(--text-base); color: var(--green); }
  .done-sub   { font-size: var(--text-sm); color: var(--text-muted); margin-top: 2px; }

  /* Progress bar */
  .progress-bar-wrap {
    height: 3px; background: var(--surface-3); border-radius: 2px;
    margin-top: 16px; overflow: hidden;
  }
  .progress-bar { height: 100%; background: var(--accent); border-radius: 2px; transition: width 400ms ease; }

  /* Form hint */
  .form-hint { font-size: var(--text-xs); color: var(--text-dim); margin-top: 4px; }

  .mt-12 { margin-top: 12px; }
  .mt-16 { margin-top: 16px; }
  .mt-24 { margin-top: 24px; }

  .api-error {
    background: var(--red-dim); border: 1px solid rgba(247,84,84,.3);
    border-radius: var(--radius); padding: 8px 12px;
    font-size: var(--text-sm); color: var(--red);
  }

  /* Bannière session sauvegardée */
  .session-banner {
    display: flex; align-items: center; gap: 8px;
    padding: 8px 12px; border-radius: var(--radius);
    background: var(--accent-dim); border: 1px solid rgba(91,152,248,.25);
    font-size: var(--text-sm);
  }
  .sb-icon { color: var(--accent); font-size: 14px; flex-shrink: 0; }
  .sb-text { flex: 1; color: var(--text-muted); line-height: 1.5; }
  .sb-text strong { color: var(--accent); }
  .sb-date { font-size: 11px; color: var(--text-dim); margin-left: 6px; }
  .reanalyze-btn { color: var(--text-dim); font-size: 11px; }
  .reanalyze-btn:hover { color: var(--red); border-color: var(--red); }

  /* Step 2 — barre d'onglets */
  .step2-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
  }
  .step2-tabs {
    display: flex;
    gap: 4px;
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
    padding: 3px;
  }
  .step2-tab {
    display: flex; align-items: center; gap: 6px;
    padding: 6px 14px;
    background: none; border: none; border-radius: calc(var(--radius-md) - 2px);
    font-size: var(--text-sm); font-weight: 500; color: var(--text-muted);
    cursor: pointer; transition: background 150ms, color 150ms;
  }
  .step2-tab.active { background: var(--surface); color: var(--text); box-shadow: 0 1px 3px rgba(0,0,0,.25); }
  .step2-tab:not(.active):hover { color: var(--text); background: var(--surface); }
  .tab-badge {
    font-size: 10px; font-weight: 700; padding: 1px 6px;
    border-radius: 999px; background: var(--surface-3, var(--border));
    color: var(--text-dim);
  }
  .step2-tab.active .tab-badge { background: var(--accent-dim); color: var(--accent); }
  .step2-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }

  /* Step 2 — onglet Sources & Collecte */
  .collect-layout {
    display: grid;
    grid-template-columns: 360px 1fr;
    gap: 20px;
    align-items: start;
  }
  @media (max-width: 1100px) { .collect-layout { grid-template-columns: 1fr; } }

  .source-group { display: flex; flex-direction: column; gap: 4px; }
  .source-group-label {
    font-size: 11px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .07em; color: var(--text-dim);
    padding: 4px 0 2px; font-family: var(--font-mono);
  }
  .source-row {
    display: flex; align-items: center; gap: 8px;
    padding: 6px 8px; border-radius: var(--radius);
    cursor: pointer; transition: background 150ms;
  }
  .source-row:hover { background: var(--surface-2); }
  .source-checkbox { flex-shrink: 0; accent-color: var(--accent); cursor: pointer; }
  .source-name { flex: 1; font-size: 12px; color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .source-ext {
    font-size: 11px; color: var(--text-dim); text-decoration: none;
    flex-shrink: 0; transition: color 150ms;
  }
  .source-ext:hover { color: var(--accent); }

  .collector-badge {
    display: inline-flex; align-items: center;
    padding: 1px 6px; border-radius: 4px;
    font-size: 10px; font-weight: 700; flex-shrink: 0;
    letter-spacing: .04em; text-transform: uppercase;
  }
  .collector-badge--rosetta_code { background: rgba(34,197,94,.15); color: #22c55e; }
  .collector-badge--crawl4ai     { background: rgba(91,152,248,.15); color: var(--accent); }
  .collector-badge--manual       { background: rgba(251,146,60,.15); color: #fb923c; }
  .collector-badge--github_repo  { background: rgba(168,85,247,.15); color: #a855f7; }

  .log-box {
    background: var(--bg, #0d1117); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 10px 12px;
    font-family: var(--font-mono); font-size: 11px; color: #a8b0c8;
    max-height: 200px; overflow-y: auto; white-space: pre-wrap;
    word-break: break-all; line-height: 1.6; margin: 0;
  }

  .status-ok  { font-size: 11px; font-weight: 600; color: var(--green); }
  .status-err { font-size: 11px; font-weight: 600; color: var(--red); }

  .pairs-header { display: flex; align-items: center; justify-content: space-between; }
  .pairs-list { display: flex; flex-direction: column; gap: 8px; max-height: 400px; overflow-y: auto; }
  .pair-item {
    display: flex; gap: 10px; align-items: flex-start;
    padding: 8px 10px; border-radius: var(--radius);
    background: var(--surface-2); border: 1px solid var(--border);
  }
  .pair-idx { font-size: 10px; font-weight: 700; color: var(--text-dim); width: 24px; flex-shrink: 0; padding-top: 2px; font-family: var(--font-mono); }
  .pair-body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 4px; }
  .pair-prompt     { font-size: 11px; color: var(--accent); white-space: pre-wrap; word-break: break-word; }
  .pair-completion { font-size: 11px; color: var(--text-muted); white-space: pre-wrap; word-break: break-word; border-top: 1px solid var(--border); padding-top: 4px; margin-top: 2px; }
  .pair-score { font-size: 11px; font-weight: 700; flex-shrink: 0; }

  .collect-nav {
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    padding: 12px 0;
  }

  .collect-right { display: flex; flex-direction: column; }

  .btn-sm { padding: 4px 10px; font-size: var(--text-sm); font-weight: 500; }

  .spinner {
    display: inline-block; width: 12px; height: 12px;
    border: 2px solid currentColor; border-top-color: transparent;
    border-radius: 50%; animation: spin 600ms linear infinite;
    vertical-align: middle;
  }
  @keyframes spin { to { transform: rotate(360deg); } }

  /* Étape 0 — liste des analyses sauvegardées */
  .saved-analyses {
    width: 100%; max-width: 640px;
    margin: 24px auto 0;
  }
  .sa-header {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 10px;
  }
  .sa-title {
    font-size: var(--text-sm); font-weight: 600;
    text-transform: uppercase; letter-spacing: .07em; color: var(--text-dim);
  }
  .sa-list { display: flex; flex-direction: column; gap: 8px; }
  .sa-item {
    background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); padding: 12px 14px;
    display: flex; flex-direction: column; gap: 6px;
    text-align: left;
  }
  .sa-meta {
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  }
  .sa-name { font-size: 13px; font-weight: 600; color: var(--accent); }
  .sa-count { font-size: 11px; color: var(--text-dim); background: var(--surface-2); padding: 1px 6px; border-radius: 999px; }
  .sa-date  { font-size: 11px; color: var(--text-dim); margin-left: auto; }
  .sa-excerpt {
    font-size: 12px; color: var(--text-muted);
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    margin: 0;
  }
  .sa-actions { display: flex; align-items: center; gap: 8px; }
  .sa-delete { color: var(--red); }
  .sa-delete:hover { background: rgba(247,84,84,.1); }

  /* Documents techniques — step 1 */
  .doc-drop-zone {
    display: flex; flex-direction: column; align-items: center; justify-content: center;
    gap: 4px; padding: 20px 16px;
    border: 2px dashed var(--border); border-radius: var(--radius);
    background: var(--surface); cursor: pointer;
    transition: border-color 150ms, background 150ms;
    text-align: center;
  }
  .doc-drop-zone:hover, .doc-drop-zone.drag-over {
    border-color: var(--accent); background: var(--accent-dim);
  }
  .ddz-icon { font-size: 20px; line-height: 1; }
  .ddz-text { font-size: 13px; color: var(--text); font-weight: 500; }
  .ddz-hint { font-size: 11px; color: var(--text-dim); }
  .ddz-browse { cursor: pointer; }

  .doc-uploading { display: flex; flex-direction: column; gap: 4px; }
  .doc-uploading-item {
    display: flex; align-items: center; gap: 6px;
    font-size: 11px; color: var(--text-dim); padding: 4px 8px;
    background: var(--surface-2); border-radius: var(--radius);
  }

  .doc-list { display: flex; flex-direction: column; gap: 4px; }
  .doc-item {
    display: flex; align-items: center; gap: 8px;
    padding: 6px 10px; border-radius: var(--radius);
    background: var(--surface-2); border: 1px solid var(--border);
  }
  .doc-icon { font-size: 14px; flex-shrink: 0; }
  .doc-meta { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 1px; }
  .doc-name { font-size: 12px; font-weight: 500; color: var(--text); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .doc-info { font-size: 10px; color: var(--text-dim); }
  .doc-remove { opacity: 0.5; }
  .doc-remove:hover { opacity: 1; color: var(--red); }

  /* Cross-project match badge */
  .adapter-with-match { display: flex; flex-direction: column; }
  .cross-match-badge {
    display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
    padding: 5px 10px; margin-top: -1px;
    border: 1px solid rgba(62,207,142,.3); border-top: none;
    border-radius: 0 0 var(--radius) var(--radius);
    background: rgba(62,207,142,.04);
    font-size: 11px;
  }
  .cmb-text { color: var(--text-muted); font-weight: 500; }
  .cmb-proj { font-family: var(--font-mono); font-size: 10px; color: var(--accent); background: var(--accent-dim); padding: 1px 5px; border-radius: 999px; }
  .cmb-age  { font-size: 10px; }

  /* Tab badge green */
  .tab-badge-green { background: rgba(62,207,142,.15) !important; color: var(--green) !important; }

  /* Onglet Analyse données */
  .da-layout {
    display: grid;
    grid-template-columns: 260px 1fr;
    gap: 20px;
    align-items: start;
  }
  @media (max-width: 900px) { .da-layout { grid-template-columns: 1fr; } }

  .da-adapter-list { display: flex; flex-direction: column; gap: 4px; }
  .da-adapter-btn {
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    padding: 8px 12px; border-radius: var(--radius);
    background: var(--surface-2); border: 1px solid transparent;
    cursor: pointer; text-align: left; transition: border-color 150ms, background 150ms;
  }
  .da-adapter-btn.active { background: var(--accent-dim); border-color: var(--accent); }
  .da-adapter-btn:not(.active):hover { border-color: var(--border); }
  .da-adapter-name { font-size: 12px; color: var(--text); }
  .da-score-pill { font-size: 10px; font-weight: 700; padding: 2px 7px; border-radius: 999px; flex-shrink: 0; }

  .da-total-card { text-align: center; padding: 16px; }
  .da-total-count { font-size: 28px; font-weight: 800; color: var(--green); line-height: 1; }
  .da-total-label { font-size: 12px; color: var(--text-muted); margin-top: 2px; }

  .da-n-label { font-size: 11px; color: var(--text-dim); flex-shrink: 0; }
  .da-n-input {
    width: 56px; background: var(--surface-2); color: var(--text);
    border: 1px solid var(--border); border-radius: var(--radius);
    padding: 4px 8px; font-size: 12px; text-align: center;
  }

  .da-hint-collect {
    display: flex; align-items: flex-start; gap: 8px;
    padding: 10px 12px; border-radius: var(--radius);
    background: rgba(255,180,0,.08); border: 1px solid rgba(255,180,0,.2);
    font-size: 12px; color: var(--text-muted); line-height: 1.5; margin-bottom: 12px;
  }
  .da-hint-icon { font-size: 14px; flex-shrink: 0; margin-top: 1px; color: var(--yellow, #ffb400); }
  .da-link { background: none; border: none; padding: 0; color: var(--accent); cursor: pointer; text-decoration: underline; font-size: 12px; }

  .da-section { display: flex; flex-direction: column; }
  .da-section-title { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: .07em; color: var(--text-dim); }
  .da-section-header { display: flex; align-items: center; justify-content: space-between; gap: 8px; }

  .da-verdict-grid { display: grid; grid-template-columns: repeat(3,1fr); gap: 8px; margin-top: 10px; }
  .da-verdict-item {
    display: flex; flex-direction: column; align-items: center; gap: 3px;
    padding: 10px 6px; border-radius: var(--radius); border: 1px solid transparent;
  }
  .da-verdict-keep    { background: rgba(62,207,142,.08);  border-color: rgba(62,207,142,.2); }
  .da-verdict-fix     { background: rgba(255,180,0,.08);   border-color: rgba(255,180,0,.2); }
  .da-verdict-discard { background: rgba(247,84,84,.08);   border-color: rgba(247,84,84,.2); }
  .da-verdict-count { font-size: 20px; font-weight: 800; }
  .da-verdict-keep    .da-verdict-count { color: var(--green); }
  .da-verdict-fix     .da-verdict-count { color: var(--yellow, #ffb400); }
  .da-verdict-discard .da-verdict-count { color: var(--red); }
  .da-verdict-label { font-size: 10px; color: var(--text-dim); text-align: center; }
  .da-avg-score { font-size: 12px; color: var(--text-muted); }

  .da-issues { display: flex; flex-direction: column; }

  .da-pairs-list { display: flex; flex-direction: column; gap: 6px; max-height: 240px; overflow-y: auto; }
  .da-pair-item {
    display: flex; gap: 8px; align-items: flex-start;
    padding: 7px 10px; border-radius: var(--radius);
    background: var(--surface-2); border: 1px solid var(--border);
  }
  .da-pair-idx { font-size: 10px; font-weight: 700; color: var(--text-dim); width: 20px; flex-shrink: 0; padding-top: 1px; font-family: var(--font-mono); }
  .da-pair-body { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
  .da-pair-prompt     { font-size: 11px; color: var(--accent); white-space: pre-wrap; word-break: break-word; }
  .da-pair-completion { font-size: 11px; color: var(--text-muted); white-space: pre-wrap; word-break: break-word; border-top: 1px solid var(--border); padding-top: 3px; margin-top: 2px; }
  .da-added-count { font-size: 12px; color: var(--text-muted); }

  .da-brief {
    font-size: 12px; color: var(--text-muted); line-height: 1.7;
    background: var(--surface-2); border-radius: var(--radius); padding: 12px 14px;
    white-space: pre-wrap; word-break: break-word;
    max-height: 300px; overflow-y: auto;
  }
</style>
