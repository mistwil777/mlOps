<script lang="ts">
  import { page } from '$app/stores';
  import type { Snippet } from 'svelte';

  interface NavItem {
    href: string;
    icon: string;
    label: string;
  }

  interface NavGroup {
    num: number;
    label: string;
    items: NavItem[];
  }

  interface Props {
    healthPanel?: Snippet;
  }

  let { healthPanel }: Props = $props();

  const groups: NavGroup[] = [
    {
      num: 1,
      label: 'Scoping',
      items: [
        { href: '/advisor', icon: '◎', label: 'Analyse besoin' },
      ],
    },
    {
      num: 2,
      label: 'Données',
      items: [
        { href: '/data',         icon: '◧', label: 'Vue d\'ensemble' },
        { href: '/data/ingest',  icon: '⬇', label: 'Ingestion' },
        { href: '/data/pipeline',icon: '⟳', label: 'Pipeline' },
        { href: '/data/review',  icon: '◈', label: 'Révision' },
        { href: '/data/collect', icon: '◩', label: 'Collecte web' },
        { href: '/data/rag',     icon: '◈', label: 'RAG / Index' },
      ],
    },
    {
      num: 3,
      label: 'Training',
      items: [
        { href: '/base-models',  icon: '◫', label: 'Modèles base' },
        { href: '/train',        icon: '▷', label: 'Fine-tuning' },
        { href: '/runs',         icon: '◌', label: 'Runs' },
        { href: '/adapters',     icon: '◈', label: 'Adaptateurs LoRA' },
      ],
    },
    {
      num: 4,
      label: 'Évaluation',
      items: [
        { href: '/eval',         icon: '◎', label: 'Évaluations' },
        { href: '/models',       icon: '◉', label: 'Modèles fine-tunés' },
        { href: '/playground',   icon: '◐', label: 'Playground' },
      ],
    },
    {
      num: 5,
      label: 'Livraison',
      items: [
        { href: '/delivery',     icon: '⬡', label: 'Déploiement' },
        { href: '/lifecycle',    icon: '✈', label: 'Sol ↔ Embarqué' },
      ],
    },
  ];

  function isActive(href: string): boolean {
    const path = $page.url.pathname;
    if (href === '/') return path === '/';
    return path === href || path.startsWith(href + '/');
  }

  function groupActive(group: NavGroup): boolean {
    return group.items.some(i => isActive(i.href));
  }
</script>

<aside class="sidebar">
  <div class="sidebar-logo">
    <div class="dot"></div>
    <div>
      <div>MLOps Factory</div>
      <div class="logo-sub">Capgemini · AI Sovereignty</div>
    </div>
  </div>

  <nav>
    <!-- Dashboard -->
    <a href="/" class:active={isActive('/')} class="nav-top">
      <span class="nav-icon">⬡</span>
      Dashboard
    </a>
    <!-- Numbered groups -->
    {#each groups as group}
      <div class="nav-group" class:active-group={groupActive(group)}>
        <div class="nav-group-header">
          <span class="nav-group-num">{group.num}</span>
          {group.label}
        </div>
        {#each group.items as item}
          <a href={item.href} class:active={isActive(item.href)}>
            <span class="nav-icon">{item.icon}</span>
            {item.label}
          </a>
        {/each}
      </div>
    {/each}
  </nav>

  <div class="sidebar-footer">
    {#if healthPanel}{@render healthPanel()}{/if}
  </div>
</aside>

<style>
  .nav-top {
    padding: 8px 14px !important;
    font-size: var(--text-base) !important;
    font-weight: 500;
    margin: 4px 4px 2px !important;
  }

  .nav-icon {
    font-size: 14px;
    width: 16px;
    text-align: center;
    flex-shrink: 0;
    color: var(--text-dim);
  }

  a.active .nav-icon { color: var(--accent); }

  .sidebar-footer {
    padding: 12px 14px;
    border-top: 1px solid var(--border);
    flex-shrink: 0;
  }
</style>
