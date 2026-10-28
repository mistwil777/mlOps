<script lang="ts">
  interface Props {
    phase: 0 | 1 | 2 | 3 | 4;
    compact?: boolean;
    projectId?: string;
  }

  let { phase, compact = false, projectId }: Props = $props();

  const STEPS = [
    { num: 0, label: 'Scoping',    icon: '◎', href: projectId ? `/advisor?project=${projectId}` : '/advisor' },
    { num: 1, label: 'Données',    icon: '◧', href: '/data' },
    { num: 2, label: 'Training',   icon: '▷', href: '/train' },
    { num: 3, label: 'Évaluation', icon: '◉', href: '/models' },
    { num: 4, label: 'Livraison',  icon: '⬡', href: '/delivery' },
  ];
</script>

<div class="stepper" class:compact>
  {#each STEPS as step, i}
    {@const done    = step.num < phase}
    {@const current = step.num === phase}
    {#if i > 0}
      <div class="connector" class:done={step.num <= phase}></div>
    {/if}
    <a
      href={step.href}
      class="step"
      class:done
      class:current
      title={step.label}
      aria-current={current ? 'step' : undefined}
    >
      <div class="step-dot">
        {#if done}
          <span class="step-check">✓</span>
        {:else}
          <span class="step-icon">{step.icon}</span>
        {/if}
      </div>
      {#if !compact}
        <span class="step-label">{step.label}</span>
      {/if}
    </a>
  {/each}
</div>

<style>
  .stepper {
    display: flex;
    align-items: center;
    gap: 0;
  }

  .connector {
    flex: 1;
    height: 2px;
    background: var(--border);
    min-width: 16px;
    transition: background 300ms;
  }

  .connector.done { background: var(--accent); }

  .step {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 5px;
    text-decoration: none;
    flex-shrink: 0;
  }

  .step-dot {
    width: 28px; height: 28px;
    border-radius: 50%;
    border: 2px solid var(--border);
    background: var(--surface-2);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 12px;
    color: var(--text-dim);
    transition: border-color 200ms, background 200ms, color 200ms;
  }

  .step.done .step-dot {
    border-color: var(--accent);
    background: var(--accent);
    color: #fff;
  }

  .step.current .step-dot {
    border-color: var(--accent);
    background: var(--accent-dim);
    color: var(--accent);
    box-shadow: 0 0 0 3px var(--accent-dim);
  }

  .step:hover .step-dot {
    border-color: var(--accent-hover);
    color: var(--accent-hover);
    text-decoration: none;
  }

  .step-label {
    font-size: var(--text-xs);
    color: var(--text-dim);
    white-space: nowrap;
    transition: color 200ms;
  }

  .step.done .step-label    { color: var(--text-muted); }
  .step.current .step-label { color: var(--accent); font-weight: 600; }

  .step-check { font-size: 11px; }
  .step-icon  { font-size: 11px; }

  /* Compact: dots only, no labels */
  .compact .step-dot { width: 22px; height: 22px; font-size: 10px; }
  .compact .step-label { display: none; }
</style>
