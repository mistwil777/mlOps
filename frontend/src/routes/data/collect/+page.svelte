<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { getProjects, startCollect, startFuse, startSemanticMatch, startDiscoverSources, getCollectStatus, getAdapterCoverage, advisorRecommendSources, getCollectHistory } from '$lib/api';
  import type { Project, ProjectAdapter, SourceSuggestion, AdapterCoverageResult } from '$lib/types';

  let projects: Project[] = $state([]);
  let selectedProject = $state<Project | null>(null);
  let maxPairs = $state(50);
  let error = $state('');
  let es: EventSource | null = null;

  let coverage = $state<AdapterCoverageResult | null>(null);
  let coverageLoading = $state(false);

  // ── single-source mode ────────────────────────────────────────────────────
  let selectedSource = $state<{ adapter: ProjectAdapter; source: SourceSuggestion } | null>(null);
  let singleRunning = $state(false);
  let singleStatus = $state<'idle' | 'running' | 'completed' | 'failed'>('idle');

  // ── batch mode ────────────────────────────────────────────────────────────
  type QueueItem = { adapter: ProjectAdapter; source: SourceSuggestion };
  type QueueStatus = 'pending' | 'running' | 'completed' | 'failed' | 'skipped';

  let queue: QueueItem[] = $state([]);
  let queueStatuses: QueueStatus[] = $state([]);
  let queueIdx = $state(-1);   // -1 = not running
  let batchRunning = $state(false);

  // ── source history (server-side via data_lineage) ────────────────────────
  type SourceHistoryEntry = { status: 'completed' | 'failed'; ts: string; pairs?: number };
  let sourceHistory = $state<Record<string, SourceHistoryEntry>>({});

  function sourceKey(projectId: string, adapterName: string, sourceValue: string) {
    return `${projectId}__${adapterName}__${sourceValue}`;
  }

  async function loadHistory(projectId: string) {
    try {
      sourceHistory = await getCollectHistory(projectId) as Record<string, SourceHistoryEntry>;
    } catch { sourceHistory = {}; }
  }

  function markSourceDone(projectId: string, adapterName: string, sourceValue: string, status: 'completed' | 'failed') {
    const k = sourceKey(projectId, adapterName, sourceValue);
    sourceHistory = { ...sourceHistory, [k]: { status, ts: new Date().toISOString() } };
    // No local save needed — data_lineage is updated server-side on collect completion
  }

  function getSourceStatus(adapterName: string, sourceValue: string): SourceHistoryEntry | null {
    if (!selectedProject) return null;
    return sourceHistory[sourceKey(selectedProject.id, adapterName, sourceValue)] ?? null;
  }

  // ── ajout manuel de source ────────────────────────────────────────────────
  let showAddSource = $state(false);
  let newSrcAdapter  = $state('');
  let newSrcType     = $state<'doc_crawl' | 'web_crawl' | 'multilang_wiki'>('doc_crawl');
  let newSrcUrl      = $state('');
  let newSrcLabel    = $state('');
  let newSrcLang     = $state('');
  let newSrcPairs    = $state(100);
  let addSrcErr      = $state('');

  // ── recommandation automatique de sources ─────────────────────────────────
  type RecSuggestion = { label: string; collector_type: string; value: string; estimated_pairs: number; lang_hint?: string; reason?: string };
  let recLoading     = $state(false);
  let recSuggestions = $state<RecSuggestion[]>([]);
  let recSelected    = $state<Set<number>>(new Set());
  let recErr         = $state('');
  let recAddingAll   = $state(false);

  async function loadSourceRecommendations() {
    if (!selectedProject || !newSrcAdapter) return;
    const adapter = (selectedProject.adapters ?? []).find(a => a.name === newSrcAdapter);
    if (!adapter) return;
    recLoading = true;
    recErr = '';
    recSuggestions = [];
    recSelected = new Set();
    try {
      const existingSources = (adapter.source_configs ?? []).filter(s => typeof s.value === 'string' && s.value.length > 0);
      recSuggestions = await advisorRecommendSources({
        adapter_name: adapter.name,
        adapter_description: adapter.description ?? '',
        domain: selectedProject.domain ?? '',
        task_type: selectedProject.task_type ?? '',
        existing_source_values: existingSources.map(s => s.value as string),
        existing_source_labels: existingSources.map(s => (s.label ?? '') as string),
      });
      // All selected by default
      recSelected = new Set(recSuggestions.map((_, i) => i));
    } catch (e) {
      recErr = (e as Error).message;
    } finally {
      recLoading = false;
    }
  }

  function toggleRecSelection(i: number) {
    const s = new Set(recSelected);
    s.has(i) ? s.delete(i) : s.add(i);
    recSelected = s;
  }

  function toggleAllRec() {
    recSelected = recSelected.size === recSuggestions.length
      ? new Set()
      : new Set(recSuggestions.map((_, i) => i));
  }

  function applyRecSuggestion(s: RecSuggestion) {
    newSrcUrl   = s.value;
    newSrcLabel = s.label;
    newSrcType  = (s.collector_type as typeof newSrcType) ?? 'doc_crawl';
    newSrcLang  = s.lang_hint ?? '';
    newSrcPairs = s.estimated_pairs ?? 100;
    recSuggestions = [];
    recSelected = new Set();
  }

  async function addSelectedSources() {
    if (!selectedProject || recSelected.size === 0) return;
    recAddingAll = true;
    addSrcErr = '';
    const toAdd = [...recSelected].map(i => recSuggestions[i]);
    const newConfigs: import('$lib/types').SourceSuggestion[] = toAdd.map(s => ({
      collector_type: s.collector_type as import('$lib/types').SourceSuggestion['collector_type'],
      value: s.value,
      label: s.label,
      estimated_pairs: s.estimated_pairs,
      ...(s.lang_hint ? { lang_hint: s.lang_hint } : {}),
    }));
    try {
      const adapterName = newSrcAdapter;
      const res = await fetch(`/api/projects/${selectedProject.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          adapters: (selectedProject.adapters ?? []).map(a =>
            a.name === adapterName
              ? { ...a, source_configs: [...(a.source_configs ?? []), ...newConfigs] }
              : a
          ),
        }),
      });
      if (!res.ok) throw new Error(await res.text());
      selectedProject = await res.json();
      recSuggestions = [];
      recSelected = new Set();
      showAddSource = false;
    } catch (e) {
      addSrcErr = (e as Error).message;
    } finally {
      recAddingAll = false;
    }
  }

  async function addSource() {
    if (!selectedProject || !newSrcUrl.trim() || !newSrcAdapter) { addSrcErr = 'Adapter et URL requis.'; return; }
    addSrcErr = '';
    const sc: SourceSuggestion = {
      collector_type: newSrcType,
      value: newSrcUrl.trim(),
      label: newSrcLabel.trim() || newSrcUrl.trim(),
      estimated_pairs: newSrcPairs,
      ...(newSrcLang ? { lang_hint: newSrcLang.trim() } : {}),
    };
    // Patch project via API to persist the new source_config
    try {
      const res = await fetch(`/api/projects/${selectedProject.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          adapters: (selectedProject.adapters ?? []).map(a =>
            a.name === newSrcAdapter
              ? { ...a, source_configs: [...(a.source_configs ?? []), sc] }
              : a
          ),
        }),
      });
      if (!res.ok) throw new Error(await res.text());
      const updated = await res.json();
      selectedProject = updated;
      showAddSource = false;
      newSrcUrl = ''; newSrcLabel = ''; newSrcLang = ''; newSrcPairs = 100;
    } catch (e) { addSrcErr = (e as Error).message; }
  }

  // ── fusion panel ─────────────────────────────────────────────────────────
  let fuseRunning = $state(false);
  let fuseStatus = $state<'idle' | 'running' | 'completed' | 'failed'>('idle');
  let fuseJobId = $state<string | null>(null);

  // ── shared logs ──────────────────────────────────────────────────────────
  let logs: string[] = $state([]);
  let logsCopied = $state(false);

  function copyLogs() {
    navigator.clipboard.writeText(logs.join('\n')).then(() => {
      logsCopied = true;
      setTimeout(() => logsCopied = false, 2000);
    });
  }

  // Accordion state — which adapter names are expanded (L2 visible)
  let expandedAdapters: Set<string> = $state(new Set());
  // Session-only exclusions — sources excluded from next batch (not persisted)
  let excludedSources: Set<string> = $state(new Set());
  // Session-only discovered sources per adapter — populated by source_discoverer, not persisted to YAML
  let discoveredSources: Record<string, Array<{url: string; label: string; lang: string; concept: string}>> = $state({});
  let discoveringAdapter: string | null = $state(null);

  function toggleAdapter(name: string) {
    const next = new Set(expandedAdapters);
    next.has(name) ? next.delete(name) : next.add(name);
    expandedAdapters = next;
  }

  function toggleExclude(adapterName: string, sourceValue: string) {
    if (!selectedProject) return;
    const k = sourceKey(selectedProject.id, adapterName, sourceValue);
    const next = new Set(excludedSources);
    next.has(k) ? next.delete(k) : next.add(k);
    excludedSources = next;
  }

  async function discoverSources(adapterName: string, langPair: string) {
    if (!selectedProject || discoveringAdapter) return;
    discoveringAdapter = adapterName;
    // Auto-expand so user sees results appear when done
    const next = new Set(expandedAdapters);
    next.add(adapterName);
    expandedAdapters = next;
    logs = [...logs, `\n── AWA : recherche de sources pour ${adapterName} (${langPair}) ──`];
    try {
      const res = await startDiscoverSources({
        project_id: selectedProject.id,
        lang_pair: langPair,
        adapter_name: adapterName,
        max_sources: 5,
      });
      // Stream logs then fetch results from dedicated endpoint
      openSSE(res.job_id, async (status) => {
        discoveringAdapter = null;
        if (status === 'completed') {
          try {
            const resultsRes = await fetch(`/api/data/discover-sources/${res.job_id}/results`);
            const data = await resultsRes.json();
            const sources: Array<{url: string; label: string; lang_hint: string; concept: string}> = data.sources ?? [];
            if (sources.length > 0) {
              const found = sources.map(s => ({
                url: s.url,
                label: s.label || `${s.concept} (${s.lang_hint})`,
                lang: s.lang_hint,
                concept: s.concept,
              }));
              discoveredSources = { ...discoveredSources, [adapterName]: found };
              logs = [...logs, `✓ ${found.length} source(s) découverte(s) — visible dans la liste ci-dessous`];
            } else {
              logs = [...logs, `ℹ Aucune nouvelle source trouvée pour ${adapterName}`];
            }
          } catch (e) {
            logs = [...logs, `⚠ Impossible de lire les résultats: ${(e as Error).message}`];
          }
        }
      }, () => { discoveringAdapter = null; });
    } catch (e) {
      logs = [...logs, `ERROR découverte: ${(e as Error).message}`];
      discoveringAdapter = null;
    }
  }

  // Flat list of all sources across adapters — includes session-discovered, respects exclusions
  const allSources = $derived(
    (selectedProject?.adapters ?? []).flatMap(adapter => {
      // Existing declared sources
      const declared = (adapter.source_configs ?? [])
        .filter(sc => !excludedSources.has(sourceKey(selectedProject!.id, adapter.name, sc.value)))
        .map(sc => ({ adapter, source: sc }));
      // Session-discovered sources (not yet in YAML)
      const discovered = (discoveredSources[adapter.name] ?? [])
        .filter(d => !excludedSources.has(sourceKey(selectedProject!.id, adapter.name, d.url)))
        .map(d => ({
          adapter,
          source: {
            collector_type: 'doc_crawl' as const,
            value: d.url,
            label: `[AWA] ${d.label}`,
            lang_hint: d.lang,
            estimated_pairs: 50,
            _discovered: true,
          }
        }));
      return [...declared, ...discovered];
    })
  );

  const hasAdapters = $derived((selectedProject?.adapters ?? []).length > 0);
  const running = $derived(singleRunning || batchRunning || fuseRunning);
  const fuseAdaptersWithPair = $derived(
    (selectedProject?.adapters ?? []).filter((a: any) => a.pair_strategy === 'fuse' && a.lang_pair)
  );

  // Collect unique lang_pair keys from adapters with pair_strategy=fuse.
  // Primary source: adapter.lang_pair (e.g. "java-python").
  // Fallback: source_configs[].lang_pair_key (legacy).
  const fusiblePairKeys = $derived((() => {
    if (!selectedProject) return [] as string[];
    const keys = new Set<string>();
    for (const adapter of selectedProject.adapters ?? []) {
      // Primary: adapter-level lang_pair (set by project config)
      if (adapter.pair_strategy === 'fuse' && adapter.lang_pair) {
        keys.add(adapter.lang_pair);
        continue;
      }
      // Fallback: source_config fusible flag
      for (const sc of adapter.source_configs ?? []) {
        if (sc.fusible && sc.lang_pair_key) keys.add(sc.lang_pair_key);
      }
    }
    return [...keys];
  })());

  // Fallback: infer from doc_crawl lang_hints only when no explicit key found
  const docLangPairs = $derived((() => {
    if (!selectedProject) return [] as string[];
    if (fusiblePairKeys.length > 0) return [] as string[];
    const langs = new Set<string>();
    for (const adapter of selectedProject.adapters ?? []) {
      for (const sc of adapter.source_configs ?? []) {
        if (sc.collector_type === 'doc_crawl' && sc.lang_hint) langs.add(sc.lang_hint);
      }
    }
    return [...langs];
  })());

  onMount(async () => {
    projects = await getProjects().catch(() => []);
    if (projects.length > 0) {
      selectedProject = projects[0];
      await loadHistory(projects[0].id);
      await loadCoverage();
    }
  });

  onDestroy(() => es?.close());

  async function selectProject(id: string) {
    selectedProject = projects.find(p => p.id === id) ?? null;
    selectedSource = null;
    singleStatus = 'idle';
    logs = [];
    queue = [];
    queueStatuses = [];
    queueIdx = -1;
    coverage = null;
    if (selectedProject) await loadHistory(selectedProject.id);
    await loadCoverage();
  }

  async function loadCoverage() {
    if (!selectedProject) return;
    coverageLoading = true;
    try {
      coverage = await getAdapterCoverage(selectedProject.id);
    } catch {
      coverage = null;
    } finally {
      coverageLoading = false;
    }
  }

  function adapterCov(name: string) {
    return coverage?.adapters.find(a => a.adapter_name === name) ?? null;
  }

  // Derive langs for multilang_wiki from fusible doc_crawl sources in the same adapter.
  function getLangsForSource(adapter: { source_configs?: Array<{ collector_type: string; fusible?: boolean; lang_hint?: string }> }): string[] | undefined {
    const langs = (adapter.source_configs ?? [])
      .filter(s => s.fusible && s.lang_hint)
      .map(s => s.lang_hint as string);
    const unique = [...new Set(langs)];
    return unique.length >= 2 ? unique : undefined;
  }

  // ── single source ─────────────────────────────────────────────────────────
  async function handleStart() {
    if (!selectedProject || !selectedSource) return;
    error = '';
    logs = [];
    singleRunning = true;
    singleStatus = 'running';

    try {
      const wikiLangs = selectedSource.source.collector_type === 'multilang_wiki'
        ? getLangsForSource(selectedSource.adapter) : undefined;
      const srcEst = selectedSource.source.estimated_pairs ?? 100;
      const srcMaxPages = selectedSource.source.collector_type === 'doc_crawl' ? srcEst * 4 : srcEst;
      const res = await startCollect({
        project_id: selectedProject.id,
        topic: selectedSource.source.collector_type === 'manual' ? undefined : selectedSource.source.value,
        max_pairs: srcMaxPages,
        domain: selectedProject.domain ?? '',
        task_type: selectedProject.task_type ?? '',
        collector_type: selectedSource.source.collector_type,
        source_value: selectedSource.source.value,
        adapter_name: selectedSource.adapter.name,
        lang: selectedSource.source.lang_hint,
        max_depth: selectedSource.source.collector_type === 'doc_crawl' ? 5 : undefined,
        ...(wikiLangs ? { langs: wikiLangs } : {}),
      });
      openSSE(res.job_id, (finalStatus) => {
        singleRunning = false;
        singleStatus = finalStatus as typeof singleStatus;
        markSourceDone(selectedProject!.id, selectedSource!.adapter.name, selectedSource!.source.value, finalStatus === 'completed' ? 'completed' : 'failed');
        loadCoverage();
      }, (s) => { singleStatus = s as typeof singleStatus; });
    } catch (e) {
      error = (e as Error).message;
      singleRunning = false;
      singleStatus = 'failed';
      markSourceDone(selectedProject.id, selectedSource.adapter.name, selectedSource.source.value, 'failed');
    }
  }

  // ── batch (tout lancer) ────────────────────────────────────────────────────
  function startBatch(forceAll = false) {
    if (!selectedProject || allSources.length === 0 || batchRunning) return;
    // By default skip sources already marked completed — only queue pending/failed ones
    const toRun = forceAll
      ? allSources
      : allSources.filter(s => getSourceStatus(s.adapter.name, s.source.value)?.status !== 'completed');
    if (toRun.length === 0) return;
    queue = toRun;
    queueStatuses = toRun.map(() => 'pending' as QueueStatus);
    queueIdx = 0;
    batchRunning = true;
    logs = [];
    error = '';
    selectedSource = null;
    runNextInQueue();
  }

  async function runNextInQueue() {
    if (!selectedProject || queueIdx < 0 || queueIdx >= queue.length) {
      batchRunning = false;
      queueIdx = -1;
      await loadCoverage();
      return;
    }

    const item = queue[queueIdx];
    queueStatuses = queueStatuses.map((s, i) => i === queueIdx ? 'running' : s);
    logs = [...logs, `\n── [${queueIdx + 1}/${queue.length}] ${item.adapter.name} / ${item.source.label} ──`];

    try {
      const wikiLangs = item.source.collector_type === 'multilang_wiki'
        ? getLangsForSource(item.adapter) : undefined;
      const itemEst = item.source.estimated_pairs ?? 100;
      const itemMaxPages = item.source.collector_type === 'doc_crawl' ? itemEst * 4 : itemEst;
      const res = await startCollect({
        project_id: selectedProject.id,
        topic: item.source.collector_type === 'manual' ? undefined : item.source.value,
        max_pairs: itemMaxPages,
        domain: selectedProject.domain ?? '',
        task_type: selectedProject.task_type ?? '',
        collector_type: item.source.collector_type,
        source_value: item.source.value,
        adapter_name: item.adapter.name,
        lang: item.source.lang_hint,
        max_depth: item.source.collector_type === 'doc_crawl' ? 5 : undefined,
        ...(wikiLangs ? { langs: wikiLangs } : {}),
      });

      openSSE(res.job_id, (finalStatus) => {
        const st = finalStatus as QueueStatus;
        queueStatuses = queueStatuses.map((s, i) => i === queueIdx ? st : s);
        markSourceDone(selectedProject!.id, item.adapter.name, item.source.value, st === 'completed' ? 'completed' : 'failed');
        queueIdx++;
        runNextInQueue();
      }, () => {});
    } catch (e) {
      logs = [...logs, `ERROR: ${(e as Error).message}`];
      queueStatuses = queueStatuses.map((s, i) => i === queueIdx ? 'failed' : s);
      markSourceDone(selectedProject.id, item.adapter.name, item.source.value, 'failed');
      queueIdx++;
      runNextInQueue();
    }
  }

  function stopBatch() {
    es?.close();
    batchRunning = false;
    queueIdx = -1;
    queueStatuses = queueStatuses.map(s => s === 'pending' ? 'skipped' : s);
    loadCoverage();
  }

  // ── SSE helper ────────────────────────────────────────────────────────────
  // Reconnects automatically when the browser drops the SSE (tab switch, sleep,
  // network hiccup). The backend job keeps running — we just re-attach to the stream.
  function openSSE(
    jobId: string,
    onDone: (status: string) => void,
    onStatus: (status: string) => void,
  ) {
    let settled = false;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

    function connect() {
      es?.close();
      es = new EventSource(`/api/data/collect/${jobId}/stream`);

      es.addEventListener('log', (e) => {
        const d = JSON.parse(e.data);
        logs = [...logs, d.line];
        scrollLog();
      });

      es.addEventListener('done', (e) => {
        if (settled) return;
        settled = true;
        if (reconnectTimer) clearTimeout(reconnectTimer);
        const d = JSON.parse(e.data);
        onStatus(d.status);
        es?.close();
        onDone(d.status);
      });

      es.onerror = () => {
        es?.close();
        if (settled) return;
        // Poll server status — if finished, resolve; if still running, reconnect.
        reconnectTimer = setTimeout(async () => {
          try {
            const s = await getCollectStatus(jobId);
            if (s.status === 'completed' || s.status === 'failed') {
              if (!settled) { settled = true; onStatus(s.status); onDone(s.status); }
            } else {
              connect(); // still running — reattach
            }
          } catch {
            connect(); // network error — try again
          }
        }, 3000);
      };
    }

    connect();
  }

  function scrollLog() {
    setTimeout(() => {
      const el = document.getElementById('log-box');
      if (el) el.scrollTop = el.scrollHeight;
    }, 0);
  }

  const COLLECTOR_LABELS: Record<string, string> = {
    multilang_wiki: 'Wiki',
    web_crawl:    'Web',
    manual:       'Manuel',
    github_repo:  'GitHub',
    doc_crawl:    'Docs',
  };

  function getFusionPairs(langs: string[]): { key: string; label: string }[] {
    // Build unidirectional pairs only (first lang is source, second is target).
    // Does not generate the reverse — the fuser is directional and languages.yaml
    // defines exactly one direction per pair.
    const pairs: { key: string; label: string }[] = [];
    for (let i = 0; i < langs.length - 1; i++) {
      for (let j = i + 1; j < langs.length; j++) {
        pairs.push({ key: `${langs[i]}-${langs[j]}`, label: `${langs[i]} → ${langs[j]}` });
      }
    }
    return pairs;
  }

  async function handleFuse(langPair: string) {
    if (!selectedProject || fuseRunning) return;
    fuseRunning = true;
    fuseStatus = 'running';
    logs = [...logs, `\n── Fusion ${langPair} ──`];
    try {
      const res = await startFuse({
        project_id: selectedProject.id,
        lang_pair: langPair,
      });
      fuseJobId = res.job_id;
      openSSE(res.job_id, (s) => {
        fuseRunning = false;
        fuseStatus = s === 'completed' ? 'completed' : 'failed';
        fuseJobId = null;
        loadCoverage();
      }, () => {});
    } catch (e) {
      logs = [...logs, `ERROR fusion: ${(e as Error).message}`];
      fuseRunning = false;
      fuseStatus = 'failed';
    }
  }

  let semanticRunning = $state(false);
  let semanticStatus  = $state<'idle' | 'running' | 'completed' | 'failed'>('idle');

  async function handleSemanticMatchAdapter(adapterName: string, langPair: string) {
    if (!selectedProject || semanticRunning) return;
    semanticRunning = true;
    semanticStatus = 'running';
    logs = [...logs, `\n── Matching sémantique ${adapterName} (${langPair}) ──`];
    try {
      const res = await startSemanticMatch({
        project_id: selectedProject.id,
        lang_pair: langPair,
        adapter_name: adapterName,
      });
      openSSE(res.job_id, (s) => {
        semanticRunning = false;
        semanticStatus = s === 'completed' ? 'completed' : 'failed';
        loadCoverage();
      }, () => {});
    } catch (e) {
      logs = [...logs, `ERROR matching sémantique: ${(e as Error).message}`];
      semanticRunning = false;
      semanticStatus = 'failed';
    }
  }

  async function handleSemanticMatch(langPair: string) {
    if (!selectedProject || semanticRunning) return;
    semanticRunning = true;
    semanticStatus = 'running';
    logs = [...logs, `\n── Matching sémantique ${langPair} ──`];
    try {
      const res = await startSemanticMatch({
        project_id: selectedProject.id,
        lang_pair: langPair,
      });
      openSSE(res.job_id, (s) => {
        semanticRunning = false;
        semanticStatus = s === 'completed' ? 'completed' : 'failed';
        loadCoverage();
      }, () => {});
    } catch (e) {
      logs = [...logs, `ERROR matching sémantique: ${(e as Error).message}`];
      semanticRunning = false;
      semanticStatus = 'failed';
    }
  }

  const QUEUE_STATUS_ICONS: Record<QueueStatus, string> = {
    pending:   '○',
    running:   '⟳',
    completed: '✓',
    failed:    '✗',
    skipped:   '–',
  };
</script>

<div class="topbar">
  <span class="topbar-title">Collecte</span>
</div>

<div class="content">

  <!-- Projet -->
  <div class="card mb-16">
    <div class="card-header mb-12">
      <div class="card-title">Projet</div>
    </div>
    <div class="form-row">
      <select class="input" onchange={e => selectProject((e.target as HTMLSelectElement).value)}>
        {#each projects as p}
          <option value={p.id} selected={p.id === selectedProject?.id}>{p.name}</option>
        {/each}
      </select>
    </div>
  </div>

  {#if selectedProject && !hasAdapters}
    <div class="banner-warn mb-16">
      Ce projet n'a pas encore d'adapters définis. Retournez dans le wizard pour compléter l'analyse du besoin.
    </div>
  {/if}

  {#if selectedProject && hasAdapters}

    <!-- Couverture par adapter -->
    <div class="card mb-16">
      <div class="card-header mb-12">
        <div class="card-title">Couverture des données</div>
        {#if !coverageLoading}
          <button class="btn-icon" onclick={loadCoverage} title="Rafraîchir">↺</button>
        {/if}
      </div>

      {#if coverageLoading}
        <p class="hint">Chargement…</p>
      {:else if coverage}
        <div class="global-bar mb-12">
          <div class="global-bar-label">
            {#if coverage.phase === 'collecting'}
              <span class="bar-count">{coverage.total_raw}</span>
              <span class="hint"> records bruts collectés</span>
              <span class="hint ml-8">— fusion en attente → {coverage.total_target} paires cibles</span>
            {:else}
              <span class="bar-count">{coverage.total_collected}</span>
              <span class="hint">/ {coverage.total_target} paires cibles</span>
            {/if}
            {#if coverage.untagged > 0}
              <span class="hint ml-8">({coverage.untagged} non taguées)</span>
            {/if}
          </div>
          <div class="progress-track">
            <div class="progress-fill" style="width:{coverage.phase === 'collecting' ? Math.min(100, Math.round(coverage.total_raw / Math.max(coverage.total_target * 3, 1) * 100)) : Math.min(100, Math.round(coverage.total_collected / Math.max(coverage.total_target,1) * 100))}%"></div>
          </div>
        </div>

        <div class="adapter-coverage-list">
          {#each selectedProject.adapters ?? [] as adapter}
            {@const cov = adapterCov(adapter.name)}
            <div class="cov-row">
              <div class="cov-left">
                <span class="role-badge role-badge--{adapter.role}">{adapter.role === 'orchestrator' ? 'ORCH' : 'SPEC'}</span>
                <span class="cov-name">{adapter.name}</span>
              </div>
              <div class="cov-bar-wrap">
                <div class="cov-progress-track">
                  <div class="cov-progress-fill"
                    class:fill-ok={cov && (cov.pair_strategy === 'fuse' && cov.fused_pairs === 0 ? cov.collected > 0 : cov.pct >= 80)}
                    style="width:{cov ? (cov.pair_strategy === 'fuse' && cov.fused_pairs === 0 ? Math.min(100, Math.round(cov.collected / Math.max(cov.target, 1) * 100)) : Math.min(100, cov.pct)) : 0}%">
                  </div>
                </div>
                <span class="cov-numbers">
                  {#if cov?.pair_strategy === 'fuse' && (cov?.fused_pairs ?? 0) === 0}
                    {cov.collected} records
                  {:else}
                    {cov?.collected ?? 0} / {cov?.target ?? adapter.estimated_pairs}
                    {#if cov && cov.missing > 0}
                      <span class="missing-badge">−{cov.missing}</span>
                    {:else if cov && cov.pct >= 100}
                      <span class="ok-badge">✓</span>
                    {/if}
                  {/if}
                </span>
              </div>
            </div>
          {/each}
        </div>
      {:else}
        <p class="hint">Aucune donnée collectée pour ce projet.</p>
      {/if}
    </div>

    <!-- Sources par adapter -->
    <div class="card mb-16">
      <div class="card-header mb-12">
        <div class="card-title">Sources disponibles</div>
        <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap">
          <span class="hint">{allSources.length} source{allSources.length > 1 ? 's' : ''}</span>
          {#if batchRunning}
            <button class="btn btn-danger btn-sm" onclick={stopBatch}>⏹ Arrêter</button>
          {:else}
            {@const pendingCount = allSources.filter(s => getSourceStatus(s.adapter.name, s.source.value)?.status !== 'completed').length}
            {@const doneCount = allSources.length - pendingCount}
            {#if pendingCount > 0}
              <button class="btn btn-primary btn-sm" onclick={() => startBatch(false)} disabled={running}>
                ▶▶ {doneCount > 0 ? `Continuer (${pendingCount} restantes)` : `Tout lancer (${pendingCount})`}
              </button>
            {/if}
            {#if doneCount > 0}
              <button class="btn btn-ghost btn-sm" onclick={() => startBatch(true)} disabled={running} title="Relancer même les sources déjà collectées">
                ↺ Tout relancer
              </button>
            {/if}
          {/if}
          {#if fuseAdaptersWithPair.length > 0}
            <button
              class="btn btn-ghost btn-sm"
              disabled={!!discoveringAdapter || running}
              title="Lancer AWA sur tous les adapters de traduction séquentiellement"
              onclick={async () => {
                for (const adapter of fuseAdaptersWithPair) {
                  await new Promise<void>(resolve => {
                    discoverSources(adapter.name, adapter.lang_pair);
                    const check = setInterval(() => {
                      if (!discoveringAdapter) { clearInterval(check); resolve(); }
                    }, 500);
                  });
                }
              }}
            >
              {discoveringAdapter ? `⟳ AWA : ${discoveringAdapter.split('-').slice(0,2).join('-')}…` : `🔍 AWA tout (${fuseAdaptersWithPair.length})`}
            </button>
          {/if}
        </div>
      </div>

      <!-- Batch queue progress -->
      {#if queue.length > 0}
        <div class="queue-list mb-12">
          {#each queue as item, i}
            {@const st = queueStatuses[i]}
            <div class="queue-item" class:queue-item--running={st === 'running'} class:queue-item--done={st === 'completed'} class:queue-item--failed={st === 'failed'}>
              <span class="queue-icon queue-icon--{st}">{QUEUE_STATUS_ICONS[st]}</span>
              <span class="collector-badge collector-badge--{item.source.collector_type}">
                {COLLECTOR_LABELS[item.source.collector_type] ?? item.source.collector_type}
              </span>
              <span class="queue-label">{item.adapter.name} / {item.source.label}</span>
            </div>
          {/each}
        </div>
      {/if}

      {#each selectedProject.adapters ?? [] as adapter}
        {@const cov = adapterCov(adapter.name)}
        {@const adapterEmpty = !cov || (cov.collected ?? 0) === 0}
        {@const isExpanded = expandedAdapters.has(adapter.name)}
        {@const discoveredForAdapter = discoveredSources[adapter.name] ?? []}
        {@const totalSrc = (adapter.source_configs ?? []).length + discoveredForAdapter.length}
        {@const adapterDone = (adapter.source_configs ?? []).filter(
          sc => getSourceStatus(adapter.name, sc.value)?.status === 'completed'
        ).length}
        {@const adapterPending =
          // Declared sources: not completed, not failed, not excluded
          (adapter.source_configs ?? []).filter(sc => {
            const hist = getSourceStatus(adapter.name, sc.value);
            return hist?.status !== 'completed'
              && hist?.status !== 'failed'
              && !excludedSources.has(sourceKey(selectedProject.id, adapter.name, sc.value));
          }).length
          // AWA-discovered sources not excluded (never failed — new)
          + discoveredForAdapter.filter(
            d => !excludedSources.has(sourceKey(selectedProject.id, adapter.name, d.url))
          ).length
        }

        <div class="adapter-section mb-8" class:adapter-empty={adapterEmpty}>
          <!-- L1 — toujours visible -->
          <div class="adapter-l1">
            <button class="btn-expand" onclick={() => toggleAdapter(adapter.name)}
                    title="{isExpanded ? 'Masquer' : 'Voir'} les {totalSrc} sources">
              {isExpanded ? '▾' : '▸'}
            </button>
            <span class="role-badge role-badge--{adapter.role}">
              {adapter.role === 'orchestrator' ? 'ORCH' : 'SPEC'}
            </span>
            <span class="adapter-l1-name">{adapter.name}</span>
            <span class="hint" style="font-size:11px;white-space:nowrap">
              {adapterDone}/{totalSrc} sources
            </span>
            {#if adapterEmpty}
              <span class="missing-badge">aucune donnée</span>
            {:else if cov && cov.missing > 0}
              <span class="missing-badge" style="font-size:11px">−{cov.missing}</span>
            {:else if cov && cov.pct >= 100}
              <span class="ok-badge">✓</span>
            {/if}
            <!-- AWA discover button — always shown for fuse adapters -->
            {#if adapter.lang_pair}
              <button class="btn btn-ghost btn-sm"
                disabled={!!discoveringAdapter || running}
                title="AWA recherche des pages couvrant les mêmes concepts en {adapter.lang_pair}"
                onclick={() => discoverSources(adapter.name, adapter.lang_pair)}>
                {discoveringAdapter === adapter.name ? '⟳ Recherche…' : '🔍 AWA'}
              </button>
            {/if}
            <!-- Collect button -->
            {#if adapterPending > 0}
              <button class="btn btn-primary btn-sm" disabled={running}
                onclick={() => {
                  const toRun = allSources.filter(s => s.adapter.name === adapter.name);
                  queue = toRun; queueStatuses = toRun.map(() => 'pending' as QueueStatus);
                  queueIdx = 0; batchRunning = true; logs = []; runNextInQueue();
                }}>
                ▶ Collecter ({adapterPending})
              </button>
            {:else if totalSrc > 0}
              <button class="btn btn-ghost btn-sm" disabled={running}
                onclick={() => {
                  const toRun = (adapter.source_configs ?? []).map(sc => ({ adapter, source: sc }));
                  queue = toRun; queueStatuses = toRun.map(() => 'pending' as QueueStatus);
                  queueIdx = 0; batchRunning = true; logs = []; runNextInQueue();
                }}>
                ↺ Relancer
              </button>
            {/if}
          </div>

          <!-- L2 — sources, visible uniquement si expanded -->
          {#if isExpanded}
            <div class="sources-list" style="margin-top:6px">
              <!-- Sources découvertes par AWA en session -->
              {#each discoveredSources[adapter.name] ?? [] as d}
                {@const isExcluded = excludedSources.has(sourceKey(selectedProject.id, adapter.name, d.url))}
                <div class="source-row-wrap source-discovered" class:source-excluded={isExcluded}>
                  <div class="source-row source-row--new">
                    <span class="collector-badge collector-badge--doc_crawl">DOCS</span>
                    <span class="source-badge-new">AWA</span>
                    <span class="source-label">{d.concept} ({d.lang})</span>
                    <span class="source-pairs">~50 records</span>
                    <span class="source-value mono">{d.url}</span>
                  </div>
                  <button class="btn-exclude"
                    onclick={() => toggleExclude(adapter.name, d.url)}
                    title={isExcluded ? 'Réintégrer' : 'Exclure du batch'}>
                    {isExcluded ? '↩' : '✕'}
                  </button>
                </div>
              {/each}
              <!-- Sources déclarées dans la config -->
              {#each adapter.source_configs as sc}
                {@const isSelected = selectedSource?.source === sc}
                {@const hist = getSourceStatus(adapter.name, sc.value)}
                {@const isExcluded = excludedSources.has(sourceKey(selectedProject.id, adapter.name, sc.value))}
                <div class="source-row-wrap" class:source-excluded={isExcluded}>
                  <button
                    class="source-row"
                    class:source-row--selected={isSelected}
                    class:source-row--done={hist?.status === 'completed'}
                    class:source-row--failed={hist?.status === 'failed'}
                    onclick={() => selectedSource = isSelected ? null : { adapter, source: sc }}
                  >
                    <span class="collector-badge collector-badge--{sc.collector_type}">
                      {COLLECTOR_LABELS[sc.collector_type] ?? sc.collector_type}
                    </span>
                    <span class="source-label">{sc.label}</span>
                    {#if hist}
                      <span class="source-hist source-hist--{hist.status}"
                            title="Dernière collecte : {new Date(hist.ts).toLocaleString('fr')}">
                        {hist.status === 'completed' ? '✓' : '✗'}
                      </span>
                    {/if}
                    {#if sc.estimated_pairs}
                      <span class="source-pairs">~{sc.estimated_pairs} records</span>
                    {/if}
                    <span class="source-value mono">{sc.value}</span>
                  </button>
                  <button class="btn-exclude"
                    onclick={() => toggleExclude(adapter.name, sc.value)}
                    title={isExcluded ? 'Réintégrer dans le prochain batch' : 'Exclure du prochain batch (session)'}>
                    {isExcluded ? '↩' : '✕'}
                  </button>
                </div>
              {/each}
            </div>
          {/if}
        </div>
      {/each}

      <!-- Ajout manuel d'une source -->
      <div class="add-source-section mt-8">
        {#if !showAddSource}
          <button class="btn btn-ghost btn-sm" onclick={() => { showAddSource = true; newSrcAdapter = selectedProject?.adapters?.[0]?.name ?? ''; }}>
            + Ajouter une source
          </button>
        {:else}
          <div class="add-source-form">
            <div class="form-row mb-8">
              <label class="field-label">Adapter</label>
              <select class="input input-sm" bind:value={newSrcAdapter} onchange={() => { recSuggestions = []; recErr = ''; }}>
                {#each selectedProject.adapters ?? [] as a}
                  <option value={a.name}>{a.name}</option>
                {/each}
              </select>
              <label class="field-label">Type</label>
              <select class="input input-sm" bind:value={newSrcType}>
                <option value="doc_crawl">Docs</option>
                <option value="web_crawl">Web</option>
                <option value="multilang_wiki">Wiki</option>
              </select>
              <button
                class="btn btn-ghost btn-sm rec-btn"
                onclick={loadSourceRecommendations}
                disabled={recLoading}
                title="Obtenir des recommandations de sources pour cet adapter (Haiku — faible coût)"
              >
                {#if recLoading}⟳{:else}✦{/if} Recommander
              </button>
            </div>

            <!-- Suggestions IA -->
            {#if recSuggestions.length > 0}
              <div class="rec-list mb-8">
                <div class="rec-header">
                  <button class="rec-toggle-all" onclick={toggleAllRec}>
                    {recSelected.size === recSuggestions.length ? '☑' : recSelected.size > 0 ? '⊟' : '☐'} Tout
                  </button>
                  <span>Suggestions — cocher puis ajouter</span>
                  <button
                    class="btn btn-primary btn-sm"
                    onclick={addSelectedSources}
                    disabled={recSelected.size === 0 || recAddingAll}
                  >
                    {recAddingAll ? '⟳' : '+'} Ajouter {recSelected.size > 0 ? `(${recSelected.size})` : ''}
                  </button>
                </div>
                {#each recSuggestions as s, i}
                  {@const checked = recSelected.has(i)}
                  <div
                    class="rec-item"
                    class:rec-item--checked={checked}
                    role="button"
                    tabindex="0"
                    onclick={() => toggleRecSelection(i)}
                    onkeydown={e => e.key === 'Enter' && toggleRecSelection(i)}
                  >
                    <span class="rec-check">{checked ? '☑' : '☐'}</span>
                    <span class="collector-badge collector-badge--{s.collector_type}">
                      {COLLECTOR_LABELS[s.collector_type] ?? s.collector_type}
                    </span>
                    <span class="rec-label">{s.label}</span>
                    {#if s.lang_hint}<span class="rec-lang">{s.lang_hint}</span>{/if}
                    <span class="rec-pairs">~{s.estimated_pairs}</span>
                    {#if s.reason}<span class="rec-reason">{s.reason}</span>{/if}
                    <button
                      class="rec-prefill-btn"
                      title="Pré-remplir le formulaire avec cette source"
                      onclick={e => { e.stopPropagation(); applyRecSuggestion(s); }}
                    >↓</button>
                  </div>
                {/each}
              </div>
            {/if}
            {#if recErr}<div class="banner-error mb-8">{recErr}</div>{/if}

            <div class="form-row mb-8">
              <label class="field-label">URL / valeur</label>
              <input class="input" style="flex:1" bind:value={newSrcUrl} placeholder="https://docs.example.com/" />
            </div>
            <div class="form-row mb-8">
              <label class="field-label">Label</label>
              <input class="input" style="flex:1" bind:value={newSrcLabel} placeholder="Nom affiché (optionnel)" />
              <label class="field-label">Langue</label>
              <input class="input input-sm" bind:value={newSrcLang} placeholder="python" />
              <label class="field-label">~Paires</label>
              <input class="input input-sm" type="number" min="10" bind:value={newSrcPairs} />
            </div>
            {#if addSrcErr}<div class="banner-error mb-8">{addSrcErr}</div>{/if}
            <div style="display:flex;gap:8px">
              <button class="btn btn-primary btn-sm" onclick={addSource}>Ajouter</button>
              <button class="btn btn-ghost btn-sm" onclick={() => { showAddSource = false; addSrcErr = ''; recSuggestions = []; }}>Annuler</button>
            </div>
          </div>
        {/if}
      </div>
    </div>

    <!-- Lancement source unique -->
    {#if selectedSource && !batchRunning}
      <div class="card mb-16">
        <div class="card-header mb-8">
          <div class="card-title">Lancer la collecte</div>
        </div>
        <div class="launch-summary mb-12">
          <span class="collector-badge collector-badge--{selectedSource.source.collector_type}">
            {COLLECTOR_LABELS[selectedSource.source.collector_type] ?? selectedSource.source.collector_type}
          </span>
          <span class="mono">{selectedSource.source.label}</span>
          <span class="hint">→ {selectedSource.adapter.name}</span>
        </div>
        <div style="display:flex; gap:10px; align-items:center">
          <button
            class="btn"
            class:btn-primary={singleStatus !== 'completed'}
            class:btn-ghost={singleStatus === 'completed'}
            onclick={handleStart}
            disabled={running}
          >
            {#if singleRunning}
              ⟳ Collecte en cours…
            {:else if singleStatus === 'completed'}
              ↺ Relancer
            {:else if singleStatus === 'failed'}
              ↺ Réessayer
            {:else}
              ▶ Lancer
            {/if}
          </button>
          {#if singleStatus === 'failed' && !singleRunning}
            <span class="badge badge-failed">✗ Échoué</span>
          {/if}
        </div>
        {#if error}
          <div class="banner-error mt-8">{error}</div>
        {/if}
      </div>
    {/if}
  {/if}

  <!-- Formation des paires — adapters fuse -->
  {#if selectedProject && fuseAdaptersWithPair.length > 0}
    {@const matchableAdapters = fuseAdaptersWithPair.filter((a: any) => {
      const cov = coverage?.adapters?.find((c: any) => c.adapter_name === a.name);
      // Already matched = fused_pairs > 0
      if ((cov?.fused_pairs ?? 0) > 0) return false;
      // Matchable = has BOTH java AND python records in clean_raw
      return (cov?.clean_raw_java ?? 0) > 0 && (cov?.clean_raw_python ?? 0) > 0;
    })}
    <div class="card mb-16">
      <div class="card-header mb-8">
        <div class="card-title">Formation des paires bilingues</div>
        <span class="hint">{matchableAdapters.length}/{fuseAdaptersWithPair.length} adapters prêts à matcher</span>
      </div>

      {#if matchableAdapters.length === 0}
        <p class="hint">Aucun adapter n'a de données dans clean_raw. Lancez d'abord la collecte puis le nettoyage (Étape 1, Pipeline nettoyage).</p>
      {:else}
        <p class="hint mb-10">
          Le moteur embed les descriptions, calcule la similarité cosinus et assemble les paires Java→Python. Zéro LLM, vérification AST.
        </p>

        <!-- Semantic match -->
        <div style="margin-bottom:12px">
          <div style="font-size:12px;color:var(--text-muted);margin-bottom:8px;font-weight:600;">
            🧠 Matching sémantique — <span style="color:var(--green);font-weight:400;">recommandé</span>
            <span class="hint" style="font-weight:400"> · Cosinus sur descriptions, zéro LLM, vérification AST</span>
          </div>
          <div style="display:flex; gap:8px; align-items:center; flex-wrap:wrap;">
            <!-- Bouton "Tout matcher" si plusieurs adapters prêts -->
            {#if matchableAdapters.length > 1}
              <button
                class="btn btn-primary btn-sm"
                disabled={semanticRunning || running}
                onclick={async () => {
                  for (const adapter of matchableAdapters) {
                    await new Promise<void>(resolve => {
                      handleSemanticMatchAdapter(adapter.name, adapter.lang_pair);
                      const check = setInterval(() => {
                        if (!semanticRunning) { clearInterval(check); resolve(); }
                      }, 500);
                    });
                  }
                }}
              >
                {semanticRunning ? '⟳ En cours…' : `🧠 Tout matcher (${matchableAdapters.length})`}
              </button>
              <span class="hint" style="font-size:11px">ou par adapter :</span>
            {/if}
            <!-- Bouton par adapter matchable -->
            {#each matchableAdapters as adapter}
              {@const cov = coverage?.adapters?.find((a: any) => a.adapter_name === adapter.name)}
              <button
                class="btn btn-primary btn-sm"
                onclick={() => handleSemanticMatchAdapter(adapter.name, adapter.lang_pair)}
                disabled={semanticRunning || running}
                title={`Java: ${cov?.clean_raw_java ?? 0} · Python: ${cov?.clean_raw_python ?? 0}`}
              >
                {semanticRunning ? '⟳' : '🧠'} {adapter.name.split('-').slice(0,3).join('-')}
                <span style="font-size:10px;opacity:.7;margin-left:4px">J:{cov?.clean_raw_java ?? 0}/P:{cov?.clean_raw_python ?? 0}</span>
              </button>
            {/each}
            {#if semanticStatus === 'completed'}
              <span class="ok-badge">✓ Matching terminé</span>
            {:else if semanticStatus === 'failed'}
              <span class="missing-badge">✗ Matching échoué</span>
            {/if}
          </div>
          <!-- Adapters non prêts — informatif -->
          {#if fuseAdaptersWithPair.length > matchableAdapters.length}
            {@const notReady = fuseAdaptersWithPair.filter((a: any) => !matchableAdapters.includes(a))}
            <div style="margin-top:8px;font-size:11px;color:var(--text-dim)">
              En attente de données : {notReady.map((a: any) => a.name.split('-').slice(0,2).join('-')).join(', ')}
            </div>
          {/if}
        </div>

        <!-- LLM fuse (fallback) -->
        <div>
          <div style="font-size:12px;color:var(--text-muted);margin-bottom:6px;font-weight:600;">
            ⚡ Fusion LLM — <span style="color:var(--text-muted);font-weight:400;">fallback (modèle local requis)</span>
          </div>
          <div style="display:flex; gap:10px; align-items:center; flex-wrap:wrap;">
          {#each [...new Set(fuseAdaptersWithPair.map((a: any) => a.lang_pair))] as langPair}
            <button
              class="btn btn-ghost btn-sm"
              onclick={() => handleFuse(langPair)}
              disabled={fuseRunning || running}
            >
              {fuseRunning ? '⟳ Fusion en cours…' : `⚡ Fusionner ${langPair}`}
            </button>
          {/each}
          {#if fuseRunning}
            <button class="btn btn-ghost btn-sm" onclick={() => { es?.close(); fuseRunning = false; fuseStatus = 'failed'; }}>
              ✕ Annuler
            </button>
          {/if}
          {#if fuseStatus === 'completed'}
            <span class="ok-badge">✓ Fusion terminée</span>
          {:else if fuseStatus === 'failed'}
            <span class="missing-badge">✗ Fusion échouée</span>
          {/if}
          </div>
        </div>
      {/if}
    </div>
  {/if}

  <!-- Logs -->
  {#if logs.length > 0 || running}
    <div class="card">
      <div class="card-header">
        <span class="section-title">Logs</span>
        <button
          class="btn btn-ghost btn-sm"
          title="Copier les logs"
          onclick={copyLogs}
        >{logsCopied ? '✓ Copié' : '⧉ Copier'}</button>
      </div>
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
  .card-header { display:flex; align-items:center; justify-content:space-between; }
  .card-title  { font-size:13px; font-weight:600; color:var(--text); }
  .form-row    { display:flex; gap:12px; align-items:center; }
  .form-field-inline { display:flex; align-items:center; gap:6px; }
  .field-label { font-size:12px; color:var(--text-muted); white-space:nowrap; }
  .input {
    background:var(--surface-2); color:var(--text); border:1px solid var(--border);
    border-radius:var(--radius); padding:6px 10px; font-size:13px; min-width:220px;
  }
  .input-sm { min-width:70px; }
  .hint { font-size:11px; color:var(--text-dim); }
  .ml-8 { margin-left:8px; }

  /* Coverage */
  .global-bar-label { display:flex; align-items:center; gap:6px; margin-bottom:6px; }
  .bar-count { font-size:13px; font-weight:600; color:var(--text); }
  .progress-track {
    height:6px; background:var(--surface-3); border-radius:3px; overflow:hidden;
  }
  .progress-fill {
    height:100%; background:var(--accent); border-radius:3px; transition:width 400ms ease;
  }
  .adapter-coverage-list { display:flex; flex-direction:column; gap:6px; margin-top:4px; }
  .cov-row { display:flex; align-items:center; gap:10px; }
  .cov-left { display:flex; align-items:center; gap:6px; min-width:200px; }
  .cov-name { font-size:12px; color:var(--text); font-weight:500; }
  .cov-bar-wrap { flex:1; display:flex; align-items:center; gap:8px; }
  .cov-progress-track {
    flex:1; height:5px; background:var(--surface-3); border-radius:3px; overflow:hidden;
  }
  .cov-progress-fill {
    height:100%; background:var(--accent); border-radius:3px; transition:width 400ms ease;
  }
  .cov-progress-fill.fill-ok { background:var(--green); }
  .cov-numbers { font-size:11px; color:var(--text-dim); white-space:nowrap; display:flex; align-items:center; gap:4px; }
  .missing-badge { background:rgba(255,80,80,.15); color:var(--red); border-radius:3px; padding:1px 5px; font-size:10px; font-weight:700; }
  .ok-badge { background:rgba(34,197,94,.15); color:#22c55e; border-radius:3px; padding:1px 5px; font-size:10px; font-weight:700; }

  .btn-icon {
    background:none; border:none; color:var(--text-muted); cursor:pointer;
    font-size:14px; padding:2px 6px; border-radius:4px; transition:color 120ms;
  }
  .btn-icon:hover { color:var(--text); }

  /* Queue */
  .queue-list { display:flex; flex-direction:column; gap:3px; background:var(--surface-2); border-radius:var(--radius); padding:8px 10px; }
  .queue-item {
    display:flex; align-items:center; gap:6px; padding:2px 0;
    font-size:11px; color:var(--text-dim);
    opacity:.6; transition:opacity 150ms;
  }
  .queue-item--running { opacity:1; color:var(--text); }
  .queue-item--done    { opacity:.5; }
  .queue-item--failed  { opacity:.8; color:var(--red); }
  .queue-label { flex:1; }
  .queue-icon { width:14px; text-align:center; font-size:10px; }
  .queue-icon--running { color:var(--accent); animation:spin 1s linear infinite; display:inline-block; }
  .queue-icon--completed { color:#22c55e; }
  .queue-icon--failed    { color:var(--red); }
  @keyframes spin { to { transform:rotate(360deg); } }

  .adapter-section { border-left:2px solid var(--border); padding-left:10px; }
  .adapter-empty { border-left-color:rgba(255,80,80,.5); }
  .adapter-l1 { display:flex; align-items:center; gap:6px; flex-wrap:wrap; padding:3px 0; }
  .adapter-l1-name { font-size:12px; font-weight:600; flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .btn-expand { background:none; border:none; color:var(--text-muted); cursor:pointer; font-size:13px; padding:0 2px; line-height:1; flex-shrink:0; width:16px; }
  .btn-expand:hover { color:var(--text); }
  .source-row-wrap { display:flex; align-items:center; gap:3px; }
  .source-row-wrap .source-row { flex:1; min-width:0; }
  .btn-exclude { background:none; border:1px solid transparent; border-radius:var(--radius); color:var(--text-dim); cursor:pointer; font-size:11px; padding:2px 5px; flex-shrink:0; opacity:0; transition:opacity .15s; }
  .source-row-wrap:hover .btn-exclude { opacity:1; }
  .btn-exclude:hover { color:var(--red); border-color:rgba(255,80,80,.4); }
  .source-excluded .source-row { opacity:0.35; }
  .source-excluded .btn-exclude { opacity:1; color:var(--accent); border-color:rgba(91,152,248,.4); }
  .source-row--new { border-color:rgba(91,152,248,.3); background:rgba(91,152,248,.04); }
  .source-badge-new { font-size:9px; font-weight:700; padding:1px 5px; border-radius:999px; background:rgba(91,152,248,.2); color:#58a6ff; border:1px solid rgba(91,152,248,.4); white-space:nowrap; flex-shrink:0; }
  .adapter-name {
    display:flex; align-items:center; gap:8px;
    font-size:12px; font-weight:600; color:var(--text);
  }
  .role-badge {
    font-size:9px; font-weight:700; padding:1px 5px; border-radius:3px;
    text-transform:uppercase; letter-spacing:.05em;
  }
  .role-badge--specialist  { background:rgba(91,152,248,.15); color:var(--accent); }
  .role-badge--orchestrator{ background:rgba(168,85,247,.15); color:#a855f7; }

  .sources-list { display:flex; flex-direction:column; gap:4px; }
  .source-row {
    display:flex; align-items:center; gap:8px; padding:7px 10px;
    background:var(--surface-2); border:1px solid var(--border);
    border-radius:var(--radius); cursor:pointer; text-align:left;
    transition:border-color 120ms, background 120ms;
  }
  .source-row:hover     { border-color:var(--accent); background:var(--surface-3); }
  .source-row--selected { border-color:var(--accent); background:rgba(91,152,248,.08); }
  .source-row--done  {
    border-color:rgba(34,197,94,.3);
    background:rgba(34,197,94,.04);
  }
  .source-row--done:hover { border-color:rgba(34,197,94,.6); background:rgba(34,197,94,.08); }
  .source-row--failed {
    border-color:rgba(255,80,80,.25);
    background:rgba(255,80,80,.03);
  }
  .source-label  { flex:1; font-size:12px; color:var(--text); }
  .source-pairs  { font-size:11px; color:var(--text-dim); white-space:nowrap; }
  .source-value  { font-size:10px; color:var(--text-dim); max-width:180px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .source-hist {
    font-size:10px; font-weight:700; padding:1px 5px;
    border-radius:999px; white-space:nowrap; flex-shrink:0;
  }
  .source-hist--completed { background:rgba(34,197,94,.15); color:#22c55e; }
  .source-hist--failed    { background:rgba(255,80,80,.15);  color:var(--red); }
  .fusible-badge {
    font-size:10px; color:#f59e0b; opacity:.8; cursor:help; flex-shrink:0;
  }

  .collector-badge {
    font-size:9px; font-weight:700; padding:2px 6px; border-radius:999px;
    text-transform:uppercase; letter-spacing:.05em; white-space:nowrap; flex-shrink:0;
  }
  .collector-badge--multilang_wiki { background:rgba(34,197,94,.15);  color:#22c55e; }
  .collector-badge--web_crawl    { background:rgba(91,152,248,.15);  color:var(--accent); }
  .collector-badge--manual       { background:rgba(251,146,60,.15);  color:#fb923c; }
  .collector-badge--github_repo  { background:rgba(168,85,247,.15);  color:#a855f7; }
  .collector-badge--doc_crawl    { background:rgba(20,184,166,.15);  color:#14b8a6; }
  .mb-10 { margin-bottom:10px; }

  .launch-summary { display:flex; align-items:center; gap:10px; }
  .btn-sm { padding:4px 10px; font-size:11px; }
  .btn-danger { background:rgba(255,80,80,.15); color:var(--red); border:1px solid rgba(255,80,80,.3); border-radius:var(--radius); cursor:pointer; }
  .btn-danger:hover { background:rgba(255,80,80,.25); }
  .btn-ghost { background:transparent; color:var(--text-muted); border:1px solid var(--border); border-radius:var(--radius); cursor:pointer; }
  .btn-ghost:hover { color:var(--text); border-color:var(--text-muted); }

  .section-title { font-size:12px; font-weight:600; color:var(--text-muted); text-transform:uppercase; letter-spacing:.05em; margin-bottom:10px; }
  .log-box {
    background:var(--surface-2); border-radius:var(--radius); padding:12px;
    height:320px; overflow-y:auto; font-family:var(--font-mono); font-size:12px;
    display:flex; flex-direction:column; gap:2px;
  }
  .log-line { white-space:pre-wrap; word-break:break-all; color:var(--text-muted); }
  .badge-failed { background:rgba(255,80,80,0.15); color:var(--red); border:1px solid var(--red); padding:4px 10px; border-radius:4px; font-size:12px; }
  @keyframes blink { 0%,100%{opacity:1} 50%{opacity:0} }
  .blink { animation:blink 1s step-end infinite; }
  .banner-warn  { background:rgba(251,146,60,.1); border:1px solid rgba(251,146,60,.3); color:#fb923c; border-radius:var(--radius); padding:10px 14px; font-size:12px; }
  .banner-error { background:rgba(255,80,80,.1);  border:1px solid rgba(255,80,80,.3);  color:var(--red); border-radius:var(--radius); padding:8px 12px; font-size:12px; }

  .add-source-section { padding-top:4px; }
  .add-source-form {
    border:1px dashed var(--border); border-radius:var(--radius);
    padding:12px 14px; background:var(--surface-2); margin-top:6px;
  }

  /* Source recommendations */
  .rec-btn { color:var(--accent); border-color:rgba(91,152,248,.3); }
  .rec-btn:hover { background:rgba(91,152,248,.08); }

  .rec-list {
    border:1px solid rgba(91,152,248,.25); border-radius:var(--radius);
    background:rgba(91,152,248,.04); overflow:hidden;
  }
  .rec-header {
    display:flex; align-items:center; gap:8px;
    font-size:10px; font-weight:600; color:var(--accent);
    padding:5px 10px; background:rgba(91,152,248,.08);
    border-bottom:1px solid rgba(91,152,248,.15);
  }
  .rec-header span { flex:1; text-transform:uppercase; letter-spacing:.05em; }
  .rec-toggle-all {
    background:none; border:none; color:var(--accent); cursor:pointer;
    font-size:12px; padding:0 4px; line-height:1;
  }
  .rec-item {
    display:flex; align-items:center; gap:7px; width:100%;
    padding:6px 10px; background:transparent;
    border-bottom:1px solid rgba(91,152,248,.08); cursor:pointer;
    transition:background 120ms;
  }
  .rec-item:last-child { border-bottom:none; }
  .rec-item:hover { background:rgba(91,152,248,.08); }
  .rec-item--checked { background:rgba(91,152,248,.1); }
  .rec-check { font-size:13px; color:var(--accent); flex-shrink:0; user-select:none; width:16px; }
  .rec-label { flex:1; font-size:12px; color:var(--text); }
  .rec-lang  { font-size:10px; color:var(--text-dim); background:var(--surface-3); padding:1px 5px; border-radius:3px; flex-shrink:0; }
  .rec-pairs { font-size:10px; color:var(--text-dim); white-space:nowrap; flex-shrink:0; }
  .rec-reason { font-size:10px; color:var(--text-dim); font-style:italic; max-width:240px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .rec-prefill-btn {
    background:none; border:1px solid var(--border); border-radius:3px;
    color:var(--text-dim); cursor:pointer; font-size:11px; padding:1px 5px;
    flex-shrink:0; transition:color 120ms, border-color 120ms;
  }
  .rec-prefill-btn:hover { color:var(--text); border-color:var(--text-muted); }
</style>
