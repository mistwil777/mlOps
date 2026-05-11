<script lang="ts">
  import { page } from '$app/stores';
  import { goto } from '$app/navigation';
  import { onMount } from 'svelte';
  import type { Snippet } from 'svelte';
  import { getProjects } from '$lib/api';
  import { projectCtx, setCurrentProject } from '$lib/stores/currentProject.svelte';
  import type { Project } from '$lib/types';

  interface Props {
    healthPanel?: Snippet;
  }

  let { healthPanel }: Props = $props();
  let recentProjects = $state<Project[]>([]);

  onMount(async () => {
    recentProjects = await getProjects().catch(() => []);
  });

  // Groupes outils — affichés uniquement dans le contexte projet
  const toolGroups = [
    {
      num: 1,
      label: 'Données',
      items: [
        { href: '/data',          icon: '◧', label: 'Vue d\'ensemble' },
        { href: '/data/ingest',   icon: '⬇', label: 'Ingestion' },
        { href: '/data/pipeline', icon: '⟳', label: 'Pipeline' },
        { href: '/data/review',   icon: '◈', label: 'Révision' },
        { href: '/data/collect',  icon: '◩', label: 'Collecte web' },
        { href: '/data/rag',      icon: '◈', label: 'RAG / Index' },
      ],
    },
    {
      num: 2,
      label: 'Training',
      items: [
        { href: '/base-models',   icon: '◫', label: 'Modèles base' },
        { href: '/train',         icon: '▷', label: 'Fine-tuning' },
        { href: '/runs',          icon: '◌', label: 'Runs' },
        { href: '/adapters',      icon: '◈', label: 'Adaptateurs LoRA' },
      ],
    },
    {
      num: 3,
      label: 'Évaluation',
      items: [
        { href: '/eval',          icon: '◎', label: 'Évaluations' },
        { href: '/models',        icon: '◉', label: 'Modèles fine-tunés' },
        { href: '/playground',    icon: '◐', label: 'Playground' },
      ],
    },
    {
      num: 4,
      label: 'Livraison',
      items: [
        { href: '/delivery',      icon: '⬡', label: 'Déploiement' },
        { href: '/lifecycle',     icon: '✈', label: 'Sol ↔ Embarqué' },
      ],
    },
  ];

  const project = $derived(projectCtx.project);

  function isActive(href: string): boolean {
    const path = $page.url.pathname;
    if (href === '/') return path === '/';
    return path === href || path.startsWith(href + '/');
  }

  function groupActive(group: { items: { href: string }[] }): boolean {
    return group.items.some(i => isActive(i.href));
  }

  function classificationColor(c: string): string {
    if (c === 'secret') return 'var(--red)';
    if (c === 'confidentiel') return 'var(--orange)';
    if (c === 'interne') return 'var(--accent)';
    return 'var(--text-dim)';
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
    {#if project}
      <!-- ── ÉTAT 2 : contexte projet ── -->
      <button class="nav-back" onclick={() => { setCurrentProject(null); goto('/'); }}>
        <span class="nav-icon">←</span>
        Tous les projets
      </button>

      <div class="project-badge">
        <div class="pb-name">{project.name}</div>
        <div class="pb-meta">
          <span class="pb-domain">{project.domain}</span>
          <span class="pb-clf" style="color:{classificationColor(project.classification)}">{project.classification}</span>
        </div>
      </div>

      {#each toolGroups as group}
        <div class="nav-group" class:active-group={groupActive(group)}>
          <div class="nav-group-header">
            <span class="nav-group-num">{group.num}</span>
            {group.label}
          </div>
          {#each group.items as item}
            <a href="{item.href}?project={project.id}" class:active={isActive(item.href)}>
              <span class="nav-icon">{item.icon}</span>
              {item.label}
            </a>
          {/each}
        </div>
      {/each}

    {:else}
      <!-- ── ÉTAT 1 : accueil ── -->
      <a href="/" class:active={isActive('/')} class="nav-top">
        <span class="nav-icon">⬡</span>
        Dashboard
      </a>
      <a href="/start" class:active={isActive('/start')} class="nav-top nav-start">
        <span class="nav-icon">▶</span>
        Nouveau projet
      </a>

      {#if recentProjects.length > 0}
        <div class="nav-section-label">Projets récents</div>
        {#each recentProjects.slice(0, 8) as p}
          <button
            class="nav-project-item"
            class:active={false}
            onclick={() => { setCurrentProject(p); goto(`/projects/${p.id}`); }}
          >
            <span class="nav-icon">◉</span>
            <span class="npi-name">{p.name}</span>
            <span class="npi-clf" style="color:{classificationColor(p.classification)}">{p.classification}</span>
          </button>
        {/each}
        {#if recentProjects.length > 8}
          <a href="/projects" class="nav-all-projects">Voir tous ({recentProjects.length}) →</a>
        {/if}
      {:else}
        <div class="nav-empty">Aucun projet.<br>Commencez par créer le premier.</div>
      {/if}
    {/if}
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

  .nav-start {
    background: var(--accent-dim) !important;
    border: 1px solid rgba(91,152,248,.2) !important;
    color: var(--accent) !important;
    margin-bottom: 8px !important;
  }
  .nav-start:hover { background: rgba(91,152,248,.2) !important; }

  .nav-icon {
    font-size: 14px;
    width: 16px;
    text-align: center;
    flex-shrink: 0;
    color: var(--text-dim);
  }
  a.active .nav-icon { color: var(--accent); }

  /* Back button */
  .nav-back {
    display: flex; align-items: center; gap: 8px;
    padding: 8px 14px; width: 100%;
    background: none; border: none; cursor: pointer;
    font-size: var(--text-sm); color: var(--text-muted);
    border-radius: var(--radius); margin: 4px 4px 0;
    transition: color 150ms, background 150ms;
    text-align: left;
  }
  .nav-back:hover { color: var(--text); background: var(--surface-2); }

  /* Project badge */
  .project-badge {
    margin: 8px 8px 4px;
    padding: 10px 12px;
    background: var(--surface-2);
    border: 1px solid var(--border);
    border-radius: var(--radius-md);
  }
  .pb-name { font-size: var(--text-sm); font-weight: 700; color: var(--text); font-family: var(--font-mono); }
  .pb-meta { display: flex; align-items: center; gap: 8px; margin-top: 3px; }
  .pb-domain { font-size: 11px; color: var(--text-dim); }
  .pb-clf    { font-size: 11px; font-weight: 600; }

  /* Recent projects */
  .nav-section-label {
    font-size: 10px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .1em; color: var(--text-dim);
    padding: 10px 18px 4px;
  }

  .nav-project-item {
    display: flex; align-items: center; gap: 8px;
    padding: 7px 14px; width: 100%;
    background: none; border: none; cursor: pointer;
    font-size: var(--text-sm); color: var(--text-muted);
    border-radius: var(--radius); margin: 1px 4px;
    transition: color 150ms, background 150ms;
    text-align: left; min-width: 0;
  }
  .nav-project-item:hover { color: var(--text); background: var(--surface-2); }

  .npi-name {
    flex: 1; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; font-family: var(--font-mono); font-size: 12px;
  }
  .npi-clf { font-size: 10px; font-weight: 600; flex-shrink: 0; }

  .nav-all-projects {
    display: block; padding: 6px 18px;
    font-size: 11px; color: var(--accent);
    text-decoration: none;
    transition: color 150ms;
  }
  .nav-all-projects:hover { color: var(--text); }

  .nav-empty {
    padding: 16px 18px;
    font-size: 12px; color: var(--text-dim);
    line-height: 1.6;
  }

  .sidebar-footer {
    padding: 12px 14px;
    border-top: 1px solid var(--border);
    flex-shrink: 0;
  }
</style>
