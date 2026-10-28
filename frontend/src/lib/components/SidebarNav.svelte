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

  const groupDots: Record<number, string> = { 1: '#2DD4BF', 2: '#38BDF8', 3: '#A855F7', 4: '#EC4899' };

  const toolGroups = [
    {
      num: 1,
      label: 'Données',
      items: [
        { href: '/advisor',       icon: '◑', label: 'Cadrage' },
        { href: '/data/ingest',   icon: '⬇', label: 'Ingestion' },
        { href: '/data/pipeline', icon: '⟳', label: 'Pipeline' },
        { href: '/data/review',   icon: '◈', label: 'Révision' },
        { href: '/data/search',   icon: '⌕', label: 'Audit' },
      ],
    },
    {
      num: 2,
      label: 'Entraînement',
      items: [
        { href: '/train',         icon: '▷', label: 'Fine-tuning' },
        { href: '/runs',          icon: '◌', label: 'Runs' },
      ],
    },
    {
      num: 3,
      label: 'Évaluation',
      items: [
        { href: '/eval',          icon: '◎', label: 'Tests' },
        { href: '/playground',    icon: '◐', label: 'Playground' },
      ],
    },
    {
      num: 4,
      label: 'Livraison',
      items: [
        { href: '/delivery',      icon: '⬡', label: 'Bundle client' },
        { href: '/lifecycle',     icon: '✈', label: 'Cycle de vie' },
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
    <div class="logo-icon"></div>
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
        <a href="{group.items[0].href}?project={project.id}" class="nav-group-link" class:active-group={groupActive(group)}>
          <span class="group-dot" style="background:{groupDots[group.num]}"></span>
          <span class="group-num">{group.num}</span>
          <span class="group-label">{group.label}</span>
        </a>
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

</aside>

<style>
  .group-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
  .group-num { font-size: 11px; font-weight: 700; color: #CBD5E1; width: 14px; }
  .group-label { font-size: 14px; font-weight: 700; color: #0F172A; }

  .nav-group-link {
    display: flex; align-items: center; gap: 8px;
    padding: 9px 14px; text-decoration: none;
    border-radius: var(--radius); margin: 2px 0;
    transition: background 150ms;
  }
  .nav-group-link:hover { background: #F1F5F9; }
  .nav-group-link.active-group { background: #EFF6FF; }
  .nav-group-link.active-group .group-label { color: #0284C7; }
  .nav-group-link.active-group .group-num { color: #93C5FD; }

  .nav-top {
    padding: 7px 14px !important;
    font-size: var(--text-base) !important;
    font-weight: 500;
    margin: 4px 0 2px !important;
    padding-left: 14px !important;
  }

  .nav-start {
    background: rgba(2,132,199,.08) !important;
    border: 1px solid rgba(2,132,199,.2) !important;
    color: #0284C7 !important;
    margin-bottom: 8px !important;
    border-radius: var(--radius) !important;
  }
  .nav-start:hover { background: rgba(2,132,199,.14) !important; }

  .nav-icon {
    font-size: 13px;
    width: 14px;
    text-align: center;
    flex-shrink: 0;
    color: #94A3B8;
  }
  a.active .nav-icon { color: #0284C7; }

  /* Back button */
  .nav-back {
    display: flex; align-items: center; gap: 8px;
    padding: 7px 14px; width: 100%;
    background: none; border: none; cursor: pointer;
    font-size: var(--text-sm); color: #64748B;
    margin: 4px 0 0;
    transition: color 150ms;
    text-align: left;
  }
  .nav-back:hover { color: #334155; }

  /* Project badge */
  .project-badge {
    margin: 8px 0 4px;
    padding: 10px 14px;
    background: #F8FAFC;
    border: 1px solid #E2E8F0;
    border-radius: var(--radius-md);
  }
  .pb-name { font-size: var(--text-sm); font-weight: 700; color: #0F172A; font-family: var(--font-mono); }
  .pb-meta { display: flex; align-items: center; gap: 8px; margin-top: 3px; }
  .pb-domain { font-size: 11px; color: #94A3B8; }
  .pb-clf    { font-size: 11px; font-weight: 600; }

  /* Recent projects */
  .nav-section-label {
    font-size: 10px; font-weight: 700; text-transform: uppercase;
    letter-spacing: .1em; color: #94A3B8;
    padding: 10px 18px 4px;
  }

  .nav-project-item {
    display: flex; align-items: center; gap: 8px;
    padding: 7px 14px; width: 100%;
    background: none; border: none; cursor: pointer;
    font-size: var(--text-sm); color: #64748B;
    margin: 1px 0;
    transition: color 150ms, background 150ms;
    text-align: left; min-width: 0;
  }
  .nav-project-item:hover { color: #334155; background: #F8FAFC; }

  .npi-name {
    flex: 1; overflow: hidden; text-overflow: ellipsis;
    white-space: nowrap; font-family: var(--font-mono); font-size: 12px;
  }
  .npi-clf { font-size: 10px; font-weight: 600; flex-shrink: 0; }

  .nav-all-projects {
    display: block; padding: 6px 18px;
    font-size: 11px; color: #0284C7;
    text-decoration: none;
    transition: color 150ms;
  }
  .nav-all-projects:hover { color: #0F172A; }

  .nav-empty {
    padding: 16px 18px;
    font-size: 12px; color: #94A3B8;
    line-height: 1.6;
  }

  .sidebar-footer {
    padding: 12px 14px;
    border-top: 1px solid #E2E8F0;
    flex-shrink: 0;
  }
</style>
