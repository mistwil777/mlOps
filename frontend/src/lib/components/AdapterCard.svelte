<script lang="ts">
  import type { AdvisorAdapter } from '$lib/types';

  interface Props {
    adapter: AdvisorAdapter;
    onAlternatives?: (a: AdvisorAdapter) => void;
    onRemove?: (name: string) => void;
    active?: boolean;
  }

  let { adapter, onAlternatives, onRemove, active = false }: Props = $props();

  let expanded = $state(false);
  let showDepth = $state(false);

  function scoreColor(s: number): string {
    if (s >= 15) return 'var(--green)';
    if (s >= 10) return 'var(--accent)';
    return 'var(--red)';
  }

  function scoreBarWidth(s: number): string {
    return `width:${(s / 20) * 100}%`;
  }

  const isOrchestrator = $derived(adapter.role === 'orchestrator');
  const hasScores      = $derived(adapter.score !== undefined);
</script>

<div
  class="adapter-card"
  class:active
  class:orchestrator={isOrchestrator}
  class:expanded
>
  <!-- ── L1 : toujours visible ── -->
  <div class="l1" onclick={() => expanded = !expanded} role="button" tabindex="0"
    onkeydown={e => e.key === 'Enter' && (expanded = !expanded)}>
    <div class="l1-left">
      <span class="chevron">{expanded ? '▾' : '▸'}</span>
      <span class="adapter-name">{adapter.name}</span>
      {#if isOrchestrator}<span class="badge badge-running ml-6">orchestrateur</span>{/if}
    </div>
    <div class="l1-right" onclick={e => e.stopPropagation()} role="presentation">
      <span class="pairs-count">~{adapter.estimated_pairs}</span>
      {#if hasScores}
        <span class="mini-score" style="color:{scoreColor(adapter.score!)}">
          ◉ {adapter.score}/20
        </span>
        <span class="mini-score" style="color:{scoreColor(adapter.team_score!)}" title="Score équipe">
          ⬡ {adapter.team_score}/20
        </span>
      {/if}
      {#if onAlternatives}
        <button class="icon-btn" title="Alternatives" onclick={() => onAlternatives!(adapter)}>⟳</button>
      {/if}
      {#if onRemove}
        <button class="icon-btn danger" title="Supprimer" onclick={() => onRemove!(adapter.name)}>✕</button>
      {/if}
    </div>
  </div>

  <!-- ── L2 : description + scores + pros/cons ── -->
  {#if expanded}
    <div class="l2">
      <p class="adapter-desc">{adapter.description}</p>

      {#if adapter.rag_recommended}
        <p class="rag-note mt-6">◈ RAG recommandé pour cet adapter</p>
      {/if}

      {#if hasScores}
        <div class="scores-row mt-10">
          <div class="score-item">
            <span class="score-label">Solo</span>
            <div class="score-track"><div class="score-fill" style="{scoreBarWidth(adapter.score!)};background:{scoreColor(adapter.score!)}"></div></div>
            <span class="score-val" style="color:{scoreColor(adapter.score!)}">{adapter.score}/20</span>
          </div>
          <div class="score-item">
            <span class="score-label">Équipe</span>
            <div class="score-track"><div class="score-fill" style="{scoreBarWidth(adapter.team_score!)};background:{scoreColor(adapter.team_score!)}"></div></div>
            <span class="score-val" style="color:{scoreColor(adapter.team_score!)}">{adapter.team_score}/20</span>
          </div>
        </div>
        {#if adapter.team_note}
          <p class="team-note mt-6">{adapter.team_note}</p>
        {/if}
        {#if adapter.pros?.length || adapter.cons?.length}
          <div class="pros-cons mt-10">
            {#if adapter.pros?.length}
              <div class="pros-cons-col">
                <span class="meta-label green">Avantages</span>
                <ul class="pc-list">{#each adapter.pros as p}<li class="pro">+ {p}</li>{/each}</ul>
              </div>
            {/if}
            {#if adapter.cons?.length}
              <div class="pros-cons-col">
                <span class="meta-label red">Inconvénients</span>
                <ul class="pc-list">{#each adapter.cons as c}<li class="con">− {c}</li>{/each}</ul>
              </div>
            {/if}
          </div>
        {/if}
      {:else}
        <p class="no-score-hint mt-8">Score non calculé — lancez "↺ Réévaluer" dans le panneau Score équipe.</p>
      {/if}

      <!-- L3 toggle -->
      {#if adapter.data_types?.length}
        <button class="l3-toggle mt-10" onclick={() => showDepth = !showDepth}>
          {showDepth ? '▾ Masquer' : '▸ Types de données'}
        </button>

        {#if showDepth}
          <div class="l3">
            <div class="depth-section">
              <div class="tags mt-4">
                {#each adapter.data_types as dt}<span class="tag">{dt}</span>{/each}
              </div>
            </div>
          </div>
        {/if}
      {/if}
    </div>
  {/if}
</div>

<style>
  .adapter-card {
    border: 1px solid var(--border);
    border-radius: var(--radius);
    overflow: hidden;
    transition: border-color 150ms;
  }
  .adapter-card.active      { border-color: var(--accent); background: var(--accent-dim); }
  .adapter-card.orchestrator { border-color: color-mix(in srgb, var(--accent) 50%, transparent); }
  .adapter-card.expanded    { border-color: color-mix(in srgb, var(--accent) 60%, transparent); }

  /* L1 */
  .l1 {
    display: flex; justify-content: space-between; align-items: center;
    padding: 10px 14px; cursor: pointer; user-select: none;
    gap: 8px;
  }
  .l1:hover { background: var(--surface-2); }
  .l1-left  { display: flex; align-items: center; gap: 4px; flex-wrap: wrap; min-width: 0; }
  .l1-right { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }

  .chevron      { font-size: 11px; color: var(--text-muted); width: 12px; }
  .adapter-name { font-weight: 600; font-family: var(--font-mono); font-size: 13px; }
  .pairs-count  { font-size: 11px; color: var(--text-muted); font-family: var(--font-mono); }
  .mini-score   { font-size: 11px; font-weight: 600; font-family: var(--font-mono); }
  .ml-6 { margin-left: 6px; }
  .ml-4 { margin-left: 4px; }

  /* L2 */
  .l2 {
    padding: 0 14px 14px;
    border-top: 1px solid var(--border);
  }
  .adapter-desc { font-size: 12px; color: var(--text-muted); margin: 10px 0 0; line-height: 1.5; }

  .scores-row { display: flex; flex-direction: column; gap: 5px; }
  .score-item { display: flex; align-items: center; gap: 8px; }
  .score-label { font-size: 11px; color: var(--text-muted); width: 44px; flex-shrink: 0; }
  .score-track { flex: 1; height: 5px; background: var(--surface-2); border-radius: 3px; overflow: hidden; }
  .score-fill  { height: 100%; border-radius: 3px; transition: width 300ms ease; }
  .score-val   { font-size: 11px; font-weight: 600; width: 36px; text-align: right; flex-shrink: 0; }
  .team-note   { font-size: 11px; color: var(--text-muted); font-style: italic; line-height: 1.4; margin: 0; }
  .rag-note      { font-size: 11px; color: var(--accent); margin: 0; }
  .no-score-hint { font-size: 11px; color: var(--text-muted); font-style: italic; margin: 0; }

  .pros-cons     { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; }
  .pros-cons-col { display: flex; flex-direction: column; gap: 4px; }
  .pc-list  { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 2px; }
  .pro { font-size: 11px; color: var(--green); }
  .con { font-size: 11px; color: var(--red); }

  /* L3 */
  .l3-toggle {
    background: none; border: none; padding: 0;
    font-size: 11px; color: var(--text-muted); cursor: pointer;
    text-decoration: underline; text-underline-offset: 2px;
    transition: color 150ms;
  }
  .l3-toggle:hover { color: var(--accent); }
  .l3 { margin-top: 10px; display: flex; flex-direction: column; }
  .depth-section { display: flex; flex-direction: column; }

  /* Tags */
  .tags { display: flex; flex-wrap: wrap; gap: 4px; }
  .tag  { font-size: 11px; padding: 2px 8px; border-radius: 999px; background: var(--surface-2); color: var(--text-muted); border: 1px solid var(--border); }

  /* Meta labels */
  .meta-label       { font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--text-muted); }
  .meta-label.green { color: var(--green); }
  .meta-label.red   { color: var(--red); }

  /* Icon buttons */
  .icon-btn {
    background: none; border: 1px solid var(--border); border-radius: 4px;
    padding: 2px 7px; font-size: 12px; cursor: pointer; color: var(--text-muted);
    transition: border-color 150ms, color 150ms, background 150ms;
  }
  .icon-btn:hover        { border-color: var(--accent); color: var(--accent); }
  .icon-btn.danger:hover { border-color: var(--red); color: var(--red); }

  .mt-4  { margin-top: 4px; }
  .mt-6  { margin-top: 6px; }
  .mt-8  { margin-top: 8px; }
  .mt-10 { margin-top: 10px; }
</style>
