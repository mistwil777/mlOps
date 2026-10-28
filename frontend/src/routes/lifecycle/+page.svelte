<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import { getProjects } from '$lib/api';
  import type { Project } from '$lib/types';
  import SectionTabs from '$lib/components/SectionTabs.svelte';
  const tabs = [{ href: '/delivery', label: 'Bundle client' }, { href: '/lifecycle', label: 'Cycle de vie' }];

  interface RagStats {
    total_patterns: number;
    new_since_last_train: number;
    retrain_threshold: number;
    progress_pct: number;
  }

  interface TrainingInfo {
    run_id: string | null;
    finished_at: string | null;
    val_loss: number | null;
  }

  interface EmbeddedVersion {
    bundle_id: string;
    created_at: string;
    corpus_hash: string;
    val_loss: number | null;
  }

  interface LifecycleStatus {
    project_id: string;
    rag: RagStats;
    last_training: TrainingInfo;
    pending_retrain: boolean;
    last_embedded: Record<string, unknown> | null;
    embedded_versions: EmbeddedVersion[];
  }

  let projects: Project[] = $state([]);
  let selectedId = $state('');
  let status: LifecycleStatus | null = $state(null);
  let loading = $state(false);
  let error = $state('');
  let actionMsg = $state('');
  let interval: ReturnType<typeof setInterval> | null = null;

  onMount(async () => {
    projects = await getProjects().catch(() => []);
    if (projects.length) {
      selectedId = projects[0].id;
      await load();
    }
    interval = setInterval(load, 15000);
  });

  onDestroy(() => { if (interval) clearInterval(interval); });

  async function load() {
    if (!selectedId) return;
    loading = true;
    error = '';
    try {
      const r = await fetch(`/api/projects/${selectedId}/lifecycle`);
      if (!r.ok) throw new Error(await r.text());
      status = await r.json();
    } catch (e: unknown) {
      error = e instanceof Error ? e.message : String(e);
    } finally {
      loading = false;
    }
  }

  async function ingestFeedback() {
    actionMsg = '';
    const r = await fetch(`/api/projects/${selectedId}/lifecycle/ingest-feedback`, { method: 'POST' });
    const data = await r.json();
    actionMsg = `Ingéré : ${data.n_ingested} entrées · retrain=${data.retrain_triggered}`;
    await load();
  }

  function progressColor(pct: number): string {
    if (pct >= 100) return 'var(--green)';
    if (pct >= 60)  return 'var(--yellow)';
    return 'var(--accent)';
  }

  function fmtDate(iso: string | null): string {
    if (!iso) return '—';
    return new Date(iso).toLocaleDateString('fr-FR', { day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' });
  }
</script>

<SectionTabs {tabs} />
<div class="topbar">
  <span class="topbar-title">Cycle de vie sol ↔ embarqué</span>
  <select class="form-select" style="width:220px" bind:value={selectedId} onchange={load}>
    {#each projects as p}
      <option value={p.id}>{p.name}</option>
    {/each}
  </select>
  {#if loading}<span class="text-muted" style="font-size:12px">Chargement…</span>{/if}
</div>

<div class="content">
  {#if error}
    <div style="color:var(--red);padding:16px;background:rgba(248,81,73,.1);border-radius:8px;margin-bottom:16px">{error}</div>
  {/if}

  {#if status}
    <!-- ── Environnements ── -->
    <div class="grid-2 mb-24">

      <!-- SOL -->
      <div class="card" style="border-color:var(--green)">
        <div class="card-header">
          <span class="card-title" style="color:var(--green)">🏢 Environnement Sol</span>
          <span class="badge" style="background:rgba(63,185,80,.15);color:var(--green);border:1px solid rgba(63,185,80,.3)">ACTIF</span>
        </div>

        <!-- Progress vers seuil retrain -->
        <div style="margin-bottom:20px">
          <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px">
            <span style="font-size:13px;color:var(--text-muted)">Nouveaux patterns depuis dernier training</span>
            <span style="font-weight:700;font-size:15px">
              {status.rag.new_since_last_train} / {status.rag.retrain_threshold}
            </span>
          </div>
          <div style="height:10px;background:var(--surface-2);border-radius:10px;overflow:hidden">
            <div style="height:100%;border-radius:10px;transition:width .4s;
                        width:{status.rag.progress_pct}%;
                        background:{progressColor(status.rag.progress_pct)}">
            </div>
          </div>
          <div style="font-size:11px;color:var(--text-muted);margin-top:4px">{status.rag.progress_pct}% du seuil de réentraînement</div>
        </div>

        <div class="grid-2" style="gap:12px;margin-bottom:16px">
          <div class="metric" style="padding:12px 16px">
            <div class="metric-label">Patterns RAG total</div>
            <div class="metric-value">{status.rag.total_patterns.toLocaleString()}</div>
          </div>
          <div class="metric" style="padding:12px 16px">
            <div class="metric-label">Dernier training</div>
            <div style="font-size:13px;font-weight:600;margin-top:4px">{fmtDate(status.last_training.finished_at)}</div>
            {#if status.last_training.val_loss !== null}
              <div class="metric-sub">val_loss {status.last_training.val_loss.toFixed(4)}</div>
            {/if}
          </div>
        </div>

        {#if status.pending_retrain}
          <div style="background:rgba(210,153,34,.1);border:1px solid rgba(210,153,34,.3);border-radius:8px;padding:12px;margin-bottom:12px;font-size:13px;color:var(--yellow)">
            ⚠️ Seuil atteint — un réentraînement est en attente
          </div>
        {/if}

        <button class="btn btn-ghost" style="width:100%" onclick={ingestFeedback}>
          ↓ Ingérer feedback en attente
        </button>
        {#if actionMsg}
          <div style="font-size:12px;color:var(--green);margin-top:8px">{actionMsg}</div>
        {/if}
      </div>

      <!-- EMBARQUÉ -->
      <div class="card" style="border-color:var(--red)">
        <div class="card-header">
          <span class="card-title" style="color:var(--red)">✈️ Modèle embarqué</span>
          <span class="badge" style="background:rgba(248,81,73,.15);color:var(--red);border:1px solid rgba(248,81,73,.3)">AIR-GAP</span>
        </div>

        {#if status.last_embedded}
          {@const m = status.last_embedded as Record<string, unknown>}
          {@const training = m.training as Record<string, unknown> | undefined}
          {@const rag_corpus = m.rag_corpus as Record<string, unknown> | undefined}
          <div style="margin-bottom:16px">
            <div style="font-size:12px;color:var(--text-muted);margin-bottom:4px">Dernière version livrée</div>
            <div style="font-weight:700;font-size:14px;font-family:var(--mono)">{String(m.bundle_id ?? '—')}</div>
            <div style="font-size:12px;color:var(--text-muted);margin-top:2px">{fmtDate(String(m.created_at ?? ''))}</div>
          </div>
          <div class="grid-2" style="gap:12px;margin-bottom:16px">
            <div class="metric" style="padding:12px 16px">
              <div class="metric-label">Val loss embarqué</div>
              <div class="metric-value" style="font-size:20px">
                {training?.val_loss != null ? Number(training.val_loss).toFixed(4) : '—'}
              </div>
            </div>
            <div class="metric" style="padding:12px 16px">
              <div class="metric-label">Corpus hash</div>
              <div style="font-family:var(--mono);font-size:11px;color:var(--text-muted);margin-top:4px;word-break:break-all">
                {String(rag_corpus?.hash ?? '—').slice(0, 16)}…
              </div>
            </div>
          </div>
          <div style="font-size:12px;color:var(--text-muted);padding:10px;background:var(--surface-2);border-radius:6px">
            🔒 Zéro dépendance externe — LanceDB et SQLite exclus du bundle
          </div>
        {:else}
          <div style="padding:32px;text-align:center;color:var(--text-muted)">
            Aucun bundle embarqué exporté pour ce projet
          </div>
        {/if}
      </div>
    </div>

    <!-- ── Cycle de vie schématique ── -->
    <div class="card mb-24">
      <div class="card-title" style="margin-bottom:20px">Flux sol ↔ embarqué</div>
      <div style="display:flex;align-items:center;gap:0;overflow-x:auto;padding-bottom:4px">

        <div style="flex:1;min-width:110px;text-align:center">
          <div style="font-size:22px;margin-bottom:6px">📥</div>
          <div style="font-size:12px;font-weight:700;color:var(--green)">Feedback sol</div>
          <div style="font-size:11px;color:var(--text-muted);margin-top:4px">raw/feedback/</div>
        </div>

        <div style="color:var(--text-muted);font-size:18px;padding:0 8px;flex-shrink:0">→</div>

        <div style="flex:1;min-width:110px;text-align:center">
          <div style="font-size:22px;margin-bottom:6px">🧠</div>
          <div style="font-size:12px;font-weight:700;color:var(--purple)">RAG enrichi</div>
          <div style="font-size:11px;color:var(--text-muted);margin-top:4px">LanceDB sol</div>
        </div>

        <div style="color:var(--text-muted);font-size:18px;padding:0 8px;flex-shrink:0">→</div>

        <div style="flex:1;min-width:110px;text-align:center">
          <div style="font-size:22px;margin-bottom:6px">✨</div>
          <div style="font-size:12px;font-weight:700;color:var(--accent2)">Enrich JSONL</div>
          <div style="font-size:11px;color:var(--text-muted);margin-top:4px">pipeline/enrich.py</div>
        </div>

        <div style="color:var(--text-muted);font-size:18px;padding:0 8px;flex-shrink:0">→</div>

        <div style="flex:1;min-width:110px;text-align:center">
          <div style="font-size:22px;margin-bottom:6px">🏋️</div>
          <div style="font-size:12px;font-weight:700;color:var(--orange)">Fine-tuning RAT</div>
          <div style="font-size:11px;color:var(--text-muted);margin-top:4px">mlx_lm.lora</div>
        </div>

        <div style="color:var(--text-muted);font-size:18px;padding:0 8px;flex-shrink:0">→</div>

        <div style="flex:1;min-width:110px;text-align:center">
          <div style="font-size:22px;margin-bottom:6px">📦</div>
          <div style="font-size:12px;font-weight:700;color:var(--red)">Bundle embarqué</div>
          <div style="font-size:11px;color:var(--text-muted);margin-top:4px">export_embedded.py</div>
        </div>

        <div style="color:var(--text-muted);font-size:18px;padding:0 8px;flex-shrink:0">→</div>

        <div style="flex:1;min-width:110px;text-align:center">
          <div style="font-size:22px;margin-bottom:6px">✈️</div>
          <div style="font-size:12px;font-weight:700;color:var(--red)">Avion au sol</div>
          <div style="font-size:11px;color:var(--text-muted);margin-top:4px">MAJ + redécollage</div>
        </div>

      </div>
    </div>

    <!-- ── Historique versions embarquées ── -->
    {#if status.embedded_versions.length}
      <div class="card">
        <div class="card-title" style="margin-bottom:16px">Historique versions embarquées</div>
        <table class="table">
          <thead>
            <tr>
              <th>Bundle ID</th>
              <th>Date export</th>
              <th>Val loss</th>
              <th>Corpus hash</th>
            </tr>
          </thead>
          <tbody>
            {#each status.embedded_versions as v}
              <tr>
                <td class="mono" style="font-size:12px">{v.bundle_id}</td>
                <td>{fmtDate(v.created_at)}</td>
                <td>{v.val_loss != null ? v.val_loss.toFixed(4) : '—'}</td>
                <td class="mono" style="font-size:11px;color:var(--text-muted)">{v.corpus_hash?.slice(0,16) ?? '—'}…</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  {:else if !loading}
    <div style="padding:48px;text-align:center;color:var(--text-muted)">
      Sélectionne un projet pour voir son cycle de vie.
    </div>
  {/if}
</div>
