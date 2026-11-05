<script lang="ts">
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';
  import { getPipelineProgress, type PipelineStep } from '$lib/api';
  import { projectCtx } from '$lib/stores/currentProject.svelte';

  let steps = $state<PipelineStep[]>([]);
  let lastLoaded = $state<string | null>(null);

  function getProjectId(): string | null {
    return $page.url.searchParams.get('project') ?? projectCtx.project?.id ?? null;
  }

  async function load(pid: string) {
    if (pid === lastLoaded) return;
    lastLoaded = pid;
    try {
      const res = await getPipelineProgress(pid);
      steps = res.steps;
    } catch {
      steps = [];
    }
  }

  onMount(() => {
    // Direct sessionStorage read — bypasses SSR null-init of projectCtx
    const urlPid = new URLSearchParams(window.location.search).get('project');
    let pid = urlPid;
    if (!pid) {
      try {
        const raw = sessionStorage.getItem('mlops_current_project');
        if (raw) pid = JSON.parse(raw)?.id ?? null;
      } catch {}
    }
    if (pid) load(pid);
  });

  // React to subsequent project changes (navigation, selector)
  $effect(() => {
    const pid = $page.url.searchParams.get('project') ?? projectCtx.project?.id ?? null;
    if (pid) load(pid);
  });

  function handleClick(step: PipelineStep) {
    if (step.status === 'locked') return;
    const pid = getProjectId();
    const href = pid ? `${step.href}?project=${pid}` : step.href;
    goto(href);
  }

  function isCurrentPath(href: string) {
    return $page.url.pathname === href || $page.url.pathname.startsWith(href + '/');
  }
</script>

<div class="progress-bar">
  {#if steps.length === 0}
    <span class="loading">—</span>
  {:else}
    {#each steps as step, i}
      <button
        class="step"
        class:done={step.status === 'done'}
        class:available={step.status === 'available'}
        class:locked={step.status === 'locked'}
        class:current={isCurrentPath(step.href)}
        disabled={step.status === 'locked'}
        onclick={() => handleClick(step)}
        title={step.hint ?? step.label}
      >
        <span class="dot">
          {#if step.status === 'done'}✓{:else}{i + 1}{/if}
        </span>
        <span class="step-label">{step.label}</span>
        {#if step.count !== null && step.status === 'done'}
          <span class="step-count">{step.count.toLocaleString('fr-FR')}</span>
        {/if}
      </button>
      {#if i < steps.length - 1}
        <div class="connector" class:filled={step.status === 'done'}></div>
      {/if}
    {/each}
  {/if}
</div>

<style>
  .progress-bar {
    display: flex;
    align-items: center;
    padding: 0 24px;
    height: 36px;
    background: var(--surface-1);
    border-bottom: 1px solid var(--border);
    gap: 0;
    flex-shrink: 0;
  }

  .loading {
    font-size: 11px;
    color: var(--text-dim);
  }

  .step {
    display: flex;
    align-items: center;
    gap: 5px;
    background: none;
    border: none;
    padding: 4px 8px;
    border-radius: 6px;
    cursor: pointer;
    white-space: nowrap;
    transition: background 0.1s;
    flex-shrink: 0;
  }

  .step:hover:not(:disabled) { background: var(--surface-2); }
  .step:disabled { cursor: default; }

  .dot {
    width: 18px;
    height: 18px;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 9px;
    font-weight: 700;
    flex-shrink: 0;
  }

  .done .dot     { background: #22c55e; color: #fff; }
  .available .dot { background: var(--accent); color: #fff; }
  .locked .dot   { background: var(--border); color: var(--text-dim); }

  .step-label { font-size: 12px; font-weight: 500; }
  .done .step-label     { color: var(--text-muted); }
  .available .step-label { color: var(--accent); font-weight: 600; }
  .locked .step-label   { color: var(--text-dim); }
  .current .step-label  { text-decoration: underline; text-underline-offset: 3px; }

  .step-count {
    font-size: 10px;
    color: var(--text-dim);
    background: var(--surface-2);
    padding: 1px 5px;
    border-radius: 999px;
  }

  .connector {
    flex: 1;
    height: 1px;
    background: var(--border);
    min-width: 12px;
    max-width: 40px;
  }
  .connector.filled { background: #22c55e; }
</style>
