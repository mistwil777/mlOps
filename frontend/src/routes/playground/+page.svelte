<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { getProjects } from '$lib/api';
  import type { Project } from '$lib/types';
  import SectionTabs from '$lib/components/SectionTabs.svelte';
  const tabs = [{ href: '/eval', label: 'Tests' }, { href: '/playground', label: 'Playground' }];

  let projects: Project[] = $state([]);

  let mode = $state<'single' | 'compare'>('single');

  // Single inference
  let prompt = $state('');
  let maxTokens = $state(512);
  let temperature = $state(0.1);
  let useRag = $state(false);
  let ragProject = $state('');
  let streamOutput = $state('');
  let ragContext = $state('');
  let latency = $state<number | null>(null);
  let running = $state(false);
  let es: EventSource | null = null;

  // Compare mode
  let comparePromptList = $state<string[]>(['']);
  let compareResults: Array<{
    prompt_preview: string;
    local: { completion: string | null; latency_ms: number | null; rouge_l?: number; valid?: boolean };
    nova_pro: { completion: string | null; latency_ms: number | null; rouge_l?: number; valid?: boolean };
  }> = $state([]);
  let compareStats: {
    n: number;
    local: { pass_rate: number; ci_low: number; ci_high: number; ci_margin: number };
    nova_pro: { pass_rate: number; ci_low: number; ci_high: number; ci_margin: number };
    mcnemar: { p_value: number; significant: boolean; winner: string; interpretation: string };
    sample_size: { sufficient: boolean; current_margin: number; recommended_n: number; message: string };
  } | null = $state(null);
  let comparing = $state(false);
  let compareError = $state('');

  let error = $state('');

  onMount(async () => {
    projects = await getProjects().catch(() => []);
    if (projects.length > 0) ragProject = projects[0].id;
  });

  onDestroy(() => es?.close());

  async function handleInfer() {
    if (!prompt.trim()) return;
    streamOutput = '';
    ragContext = '';
    latency = null;
    error = '';
    running = true;
    const t0 = performance.now();

    es?.close();
    es = new EventSource('/api/playground/infer/stream?' + new URLSearchParams({
      prompt: prompt,
      max_tokens: String(maxTokens),
      temperature: String(temperature),
      model: 'local',
      ...(useRag && ragProject ? { rag_project: ragProject } : {}),
    }).toString());

    // We need to POST, not GET — use fetch + SSE workaround via POST then stream
    es.close();

    try {
      const body: Record<string, unknown> = {
        prompt,
        max_tokens: maxTokens,
        temperature,
        model: 'local',
      };
      if (useRag && ragProject) body.rag_project = ragProject;

      const resp = await fetch('/api/playground/infer/stream', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });

      if (!resp.ok) {
        throw new Error(`${resp.status} ${await resp.text()}`);
      }

      const reader = resp.body!.getReader();
      const decoder = new TextDecoder();
      let buf = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        const lines = buf.split('\n');
        buf = lines.pop() ?? '';

        for (const line of lines) {
          if (line.startsWith('event: rag_context')) continue;
          if (line.startsWith('event: token')) continue;
          if (line.startsWith('event: done')) { latency = performance.now() - t0; break; }
          if (line.startsWith('data: ')) {
            try {
              const d = JSON.parse(line.slice(6));
              if ('token' in d) streamOutput += d.token;
              if ('context' in d) ragContext = d.context;
            } catch {}
          }
        }
      }
    } catch (e) {
      error = (e as Error).message;
    } finally {
      running = false;
    }
  }

  async function handleCompare() {
    const lines = comparePromptList.map(s => s.trim()).filter(Boolean);
    if (!lines.length) { compareError = 'Entrez au moins un prompt.'; return; }
    comparing = true;
    compareError = '';
    compareResults = [];
    compareStats = null;

    try {
      const r = await fetch('/api/playground/compare', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ prompts: lines, max_tokens: maxTokens, temperature }),
      });
      if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
      const d = await r.json();
      compareResults = d.results;
      compareStats = d.stats ?? null;
    } catch (e) {
      compareError = (e as Error).message;
    } finally {
      comparing = false;
    }
  }

  function pct(v: number) { return `${(v * 100).toFixed(1)}%`; }
  function pp(v: number)  { return `±${(v * 100).toFixed(1)} pp`; }
</script>

<SectionTabs {tabs} />
<div class="topbar">
  <span class="topbar-title">Playground</span>
  <div class="mode-tabs">
    {#each [['single', 'Test simple'], ['compare', 'Comparaison']] as [m, label]}
      <button class="mode-tab {mode === m ? 'active' : ''}" onclick={() => mode = m as typeof mode}>{label}</button>
    {/each}
  </div>
</div>

<div class="content">
  {#if error}
    <div class="banner-error" style="margin-bottom:16px">{error}</div>
  {/if}

  <!-- ── PARAMS BAR ── -->
  <div class="params-bar card" style="margin-bottom:16px">
    <label class="param-field">
      <span class="param-label">Max tokens</span>
      <input type="number" bind:value={maxTokens} min="64" max="4096" step="64" class="param-input" />
    </label>
    <label class="param-field">
      <span class="param-label">Température</span>
      <div style="display:flex; align-items:center; gap:8px">
        <input type="range" min="0" max="1" step="0.05" bind:value={temperature} style="width:100px" />
        <span class="mono" style="font-size:12px; width:32px">{temperature.toFixed(2)}</span>
      </div>
    </label>
    <label class="param-field toggle-row">
      <span class="param-label">RAG</span>
      <input type="checkbox" bind:checked={useRag} />
      {#if useRag}
        <select bind:value={ragProject} class="param-input" style="min-width:120px">
          {#each projects as p}
            <option value={p.id}>{p.id}</option>
          {/each}
        </select>
      {/if}
    </label>
  </div>

  <!-- ── SINGLE MODE ── -->
  {#if mode === 'single'}
    <div class="two-pane">
      <div class="pane card">
        <div class="pane-title">Prompt</div>
        <textarea bind:value={prompt} class="prompt-area" placeholder="Entrez votre prompt…" rows="12"></textarea>
        <button class="btn btn-primary" style="margin-top:10px" onclick={handleInfer} disabled={running || !prompt.trim()}>
          {running ? '⟳ Génération…' : '▶ Générer'}
        </button>
      </div>

      <div class="pane card">
        <div class="pane-title">
          Completion
          {#if latency !== null}
            <span class="text-muted" style="font-size:11px; margin-left:8px">{latency.toFixed(0)} ms</span>
          {/if}
        </div>
        <pre class="output-area">{streamOutput || (running ? '▋' : '(en attente…)')}</pre>

        {#if ragContext}
          <details style="margin-top:12px">
            <summary class="text-muted" style="font-size:12px; cursor:pointer">Contexte RAG injecté</summary>
            <pre class="rag-ctx">{ragContext}</pre>
          </details>
        {/if}
      </div>
    </div>
  {/if}

  <!-- ── COMPARE MODE ── -->
  {#if mode === 'compare'}
    <div class="card" style="margin-bottom:16px; padding:16px">
      <div class="section-title">Prompts à comparer</div>
      <div class="prompt-list">
        {#each comparePromptList as _, i}
          <div class="prompt-list-item">
            <span class="prompt-num">{i + 1}</span>
            <textarea
              bind:value={comparePromptList[i]}
              class="prompt-area"
              rows="4"
              placeholder="Entrez un prompt…"
            ></textarea>
            {#if comparePromptList.length > 1}
              <button class="btn-remove" onclick={() => { comparePromptList = comparePromptList.filter((_, j) => j !== i); }} title="Supprimer">✕</button>
            {/if}
          </div>
        {/each}
      </div>
      <div style="display:flex; gap:8px; margin-top:10px; align-items:center">
        <button class="btn btn-ghost" onclick={() => { comparePromptList = [...comparePromptList, '']; }}>+ Ajouter un prompt</button>
        <button class="btn btn-primary" onclick={handleCompare} disabled={comparing || comparePromptList.every(s => !s.trim())}>
          {comparing ? '⟳ Comparaison…' : '▶ Comparer local vs Nova Pro'}
        </button>
      </div>
      {#if compareError}
        <div class="banner-error" style="margin-top:8px">{compareError}</div>
      {/if}
    </div>

    {#if compareStats}
      <!-- ── Sample size warning ── -->
      {#if !compareStats.sample_size.sufficient}
        <div class="stat-warning mb-12">
          <span class="stat-warning-icon">⚠</span>
          <span>{compareStats.sample_size.message}</span>
        </div>
      {/if}

      <!-- ── Stats summary ── -->
      <div class="card stat-summary mb-16">
        <div class="stat-row">
          <div class="stat-col">
            <div class="stat-label">Local (mlx_lm)</div>
            <div class="stat-rate">{pct(compareStats.local.pass_rate)}</div>
            <div class="stat-ci">IC 95 % : [{pct(compareStats.local.ci_low)} – {pct(compareStats.local.ci_high)}] {pp(compareStats.local.ci_margin)}</div>
          </div>
          <div class="stat-vs">vs</div>
          <div class="stat-col">
            <div class="stat-label">Nova Pro</div>
            <div class="stat-rate">{pct(compareStats.nova_pro.pass_rate)}</div>
            <div class="stat-ci">IC 95 % : [{pct(compareStats.nova_pro.ci_low)} – {pct(compareStats.nova_pro.ci_high)}] {pp(compareStats.nova_pro.ci_margin)}</div>
          </div>
        </div>
        <div class="mcnemar-row">
          <span class="mcnemar-label">Test McNemar</span>
          <span class="mcnemar-verdict {compareStats.mcnemar.significant ? 'significant' : 'tie'}">
            {compareStats.mcnemar.interpretation}
          </span>
        </div>
      </div>
    {/if}

    {#if compareResults.length > 0}
      {#each compareResults as row, i}
        <div class="card" style="margin-bottom:12px; padding:16px">
          <div class="text-muted" style="font-size:12px; margin-bottom:10px">Prompt {i+1}: <span class="mono">{row.prompt_preview}…</span></div>
          <div class="compare-row">
            <div class="compare-col">
              <div class="compare-header">
                <span class="badge badge-completed">Local (mlx_lm)</span>
                {#if row.local.latency_ms !== null}
                  <span class="text-muted" style="font-size:11px">{row.local.latency_ms?.toFixed(0)} ms</span>
                {/if}
                {#if row.local.rouge_l !== undefined}
                  <span class="badge" style="background:rgba(130,110,255,0.1); color:var(--accent); border:1px solid var(--accent); padding:2px 8px; border-radius:4px; font-size:11px">ROUGE-L {(row.local.rouge_l * 100).toFixed(1)}%</span>
                {/if}
              </div>
              <pre class="compare-text">{row.local.completion ?? (row.local as any).error ?? '—'}</pre>
            </div>
            <div class="compare-col">
              <div class="compare-header">
                <span class="badge" style="background:rgba(255,140,0,0.1); color:#ff8c00; border:1px solid #ff8c00; padding:4px 8px; border-radius:4px; font-size:12px">Nova Pro</span>
                {#if row.nova_pro.latency_ms !== null}
                  <span class="text-muted" style="font-size:11px">{row.nova_pro.latency_ms?.toFixed(0)} ms</span>
                {/if}
              </div>
              <pre class="compare-text">{row.nova_pro.completion ?? (row.nova_pro as any).error ?? '—'}</pre>
            </div>
          </div>
        </div>
      {/each}
    {/if}
  {/if}

</div>

<style>
  .mode-tabs { display: flex; gap: 4px; }
  .mode-tab { padding: 6px 14px; font-size: 12px; border: 1px solid var(--border); border-radius: var(--radius); background: var(--surface-2); color: var(--text-muted); cursor: pointer; }
  .mode-tab.active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .params-bar { display: flex; flex-wrap: wrap; gap: 20px; align-items: center; padding: 12px 16px; }
  .param-field { display: flex; align-items: center; gap: 8px; }
  .toggle-row { gap: 8px; }
  .param-label { font-size: 12px; color: var(--text-muted); white-space: nowrap; }
  .param-input { background: var(--surface-2); color: var(--text); border: 1px solid var(--border); border-radius: var(--radius); padding: 4px 8px; font-size: 12px; }
  .two-pane { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .pane { padding: 16px; display: flex; flex-direction: column; gap: 0; }
  .pane-title { font-size: 12px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; }
  .prompt-area { background: var(--surface-2); color: var(--text); border: 1px solid var(--border); border-radius: var(--radius); padding: 10px; font-family: var(--font-mono); font-size: 12px; resize: vertical; width: 100%; flex: 1; min-height: 200px; }
  .output-area { font-family: var(--font-mono); font-size: 12px; background: var(--surface-2); padding: 10px; border-radius: var(--radius); white-space: pre-wrap; word-break: break-all; flex: 1; min-height: 200px; overflow-y: auto; margin: 0; }
  .rag-ctx { font-family: var(--font-mono); font-size: 11px; background: var(--surface-2); padding: 8px; border-radius: 4px; white-space: pre-wrap; margin: 6px 0 0 0; color: var(--text-muted); }
  .section-title { font-size: 12px; font-weight: 600; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 10px; }
  .compare-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .compare-col { display: flex; flex-direction: column; gap: 6px; }
  .compare-header { display: flex; align-items: center; gap: 8px; }
  .compare-text { font-family: var(--font-mono); font-size: 11px; background: var(--surface-2); padding: 8px; border-radius: 4px; white-space: pre-wrap; word-break: break-all; margin: 0; max-height: 240px; overflow-y: auto; }
  .prompt-list { display: flex; flex-direction: column; gap: 10px; }
  .prompt-list-item { display: flex; gap: 8px; align-items: flex-start; }
  .prompt-num { font-size: 11px; font-weight: 700; color: var(--text-muted); min-width: 18px; margin-top: 10px; text-align: right; }
  .prompt-list-item .prompt-area { flex: 1; min-height: 80px; }
  .btn-remove { background: none; border: 1px solid var(--border); color: var(--text-muted); border-radius: var(--radius); padding: 4px 8px; cursor: pointer; font-size: 11px; margin-top: 6px; flex-shrink: 0; }
  .btn-remove:hover { color: var(--red, #e66767); border-color: var(--red, #e66767); }

  /* Stats block */
  .stat-warning { display:flex; gap:8px; align-items:flex-start; padding:10px 14px; background:#fff8ed; border:1px solid #f59e0b; border-radius:var(--radius); font-size:12px; color:#92400e; }
  .stat-warning-icon { font-size:14px; flex-shrink:0; }
  .stat-summary { padding:16px; }
  .stat-row { display:flex; align-items:center; gap:24px; margin-bottom:14px; }
  .stat-col { flex:1; display:flex; flex-direction:column; gap:2px; }
  .stat-label { font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.05em; color:var(--text-muted); }
  .stat-rate { font-size:28px; font-weight:700; color:var(--text); font-variant-numeric:tabular-nums; }
  .stat-ci { font-size:11px; color:var(--text-muted); font-family:var(--font-mono); }
  .stat-vs { font-size:13px; color:var(--text-muted); font-weight:600; flex-shrink:0; }
  .mcnemar-row { border-top:1px solid var(--border); padding-top:12px; display:flex; align-items:baseline; gap:10px; }
  .mcnemar-label { font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.05em; color:var(--text-muted); flex-shrink:0; }
  .mcnemar-verdict { font-size:12px; color:var(--text); }
  .mcnemar-verdict.significant { color:#0284c7; font-weight:600; }
  .mcnemar-verdict.tie { color:var(--text-muted); }
  .mb-12 { margin-bottom:12px; }
  .mb-16 { margin-bottom:16px; }
</style>
