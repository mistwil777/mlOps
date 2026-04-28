<script lang="ts">
  interface Props {
    mode?: 'local' | 'cloud' | 'hybrid';
    label?: string;
    size?: 'sm' | 'md';
  }

  let { mode = 'local', label, size = 'md' }: Props = $props();

  const config = $derived({
    local:  { dot: '#3ecf8e', text: label ?? '🟢 Local',   title: '100% local inference — no cloud calls' },
    cloud:  { dot: '#f7aa50', text: label ?? '🟠 Cloud',   title: 'Cloud inference — data leaves the machine' },
    hybrid: { dot: '#5b98f8', text: label ?? '◑ Hybrid',   title: 'Scoping via cloud / inference local' },
  }[mode]);
</script>

<span
  class="sov-badge sov-{mode} size-{size}"
  title={config.title}
  aria-label={config.title}
>
  <span class="sov-dot" style="background:{config.dot}"></span>
  <span class="sov-text">{config.text}</span>
</span>

<style>
  .sov-badge {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 2px 8px;
    border-radius: 999px;
    font-size: var(--text-xs);
    font-weight: 600;
    border: 1px solid transparent;
    white-space: nowrap;
  }

  .sov-local  { background: var(--green-dim);  border-color: rgba(62,207,142,.25); color: var(--green); }
  .sov-cloud  { background: var(--yellow-dim); border-color: rgba(247,170,80,.25); color: var(--yellow); }
  .sov-hybrid { background: var(--accent-dim); border-color: rgba(91,152,248,.25); color: var(--accent); }

  .size-sm { font-size: 10px; padding: 1px 6px; }

  .sov-dot {
    width: 6px; height: 6px;
    border-radius: 50%;
    flex-shrink: 0;
  }

  .sov-local .sov-dot  { box-shadow: 0 0 4px var(--green); }
  .sov-cloud .sov-dot  { box-shadow: 0 0 4px var(--yellow); }
  .sov-hybrid .sov-dot { box-shadow: 0 0 4px var(--accent); }
</style>
