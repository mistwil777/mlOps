<script lang="ts">
  import { page } from '$app/stores';
  import { onMount, onDestroy } from 'svelte';
  import { getRun, getRunMetrics, cancelRun, launchEval, getRunEvals } from '$lib/api';
  import type { Run, Metric, Evaluation } from '$lib/types';
  import StatusBadge from '$lib/components/StatusBadge.svelte';
  import LossChart from '$lib/components/LossChart.svelte';
  import { sseMulti } from '$lib/sse';

  const id = $derived($page.params.id as string);
  let run: Run | null = $state(null);
  let metrics: Metric[] = $state([]);
  let logs: string[] = $state([]);
  let evals: Evaluation[] = $state([]);
  let logEl: HTMLDivElement = $state()!;
  let cleanup: (() => void) | null = null;

  onMount(async () => {
    run = await getRun(id).catch(() => null);
    [metrics, evals] = await Promise.all([getRunMetrics(id), getRunEvals(id)]);

    cleanup = sseMulti(`/api/runs/${id}/stream`, {
      metric: (d: unknown) => {
        const m = d as { step: number; train_loss: number; val_loss?: number; status: string };
        if (!metrics.find(x => x.step === m.step)) {
          metrics = [...metrics, { step: m.step, train_loss: m.train_loss, val_loss: m.val_loss ?? null }];
        }
      },
      status: (d: unknown) => {
        const s = d as { status: string };
        if (run) run = { ...run, status: s.status as Run['status'] };
        if (s.status !== 'running') cleanup?.();
      },
    });

    sseMulti(`/api/runs/${id}/logs/stream`, {
      log: (d: unknown) => {
        const l = d as { line: string };
        logs = [...logs.slice(-200), l.line];
        setTimeout(() => logEl?.scrollTo(0, logEl.scrollHeight), 0);
      },
    });
  });

  onDestroy(() => cleanup?.());

  async function cancel() {
    if (!confirm('Annuler ce run ?')) return;
    await cancelRun(id);
    run = await getRun(id);
  }

  async function evaluate() {
    await launchEval(id);
    alert('Évaluation lancée — les résultats apparaîtront sous peu.');
  }
</script>

<div class="topbar">
  <span class="topbar-title">Run <span class="mono text-accent">{id}</span></span>
  {#if run}<StatusBadge status={run.status} />{/if}
  <span style="flex:1"></span>
  {#if run?.status === 'running'}
    <button class="btn btn-danger" onclick={cancel}>■ Annuler</button>
  {/if}
  {#if run?.status === 'completed'}
    <button class="btn btn-primary" onclick={evaluate}>▷ Évaluer</button>
  {/if}
</div>

<div class="content">
  {#if run}
    <!-- Hyperparams -->
    <div class="grid-4 mb-24">
      <div class="metric"><div class="metric-label">Projet</div><div class="metric-value" style="font-size:18px">{run.project_id}</div></div>
      <div class="metric"><div class="metric-label">Modèle</div><div class="metric-value" style="font-size:14px">{run.model_name}</div></div>
      <div class="metric"><div class="metric-label">Train loss</div><div class="metric-value" style="color:var(--accent)">{run.train_loss?.toFixed(4) ?? '—'}</div></div>
      <div class="metric"><div class="metric-label">Val loss</div><div class="metric-value" style="color:var(--green)">{run.val_loss?.toFixed(4) ?? '—'}</div></div>
    </div>

    <!-- Loss chart -->
    <div class="card mb-16">
      <div class="card-header">
        <div class="card-title">Courbes de loss</div>
        <span class="text-muted" style="font-size:12px">{metrics.length} points</span>
      </div>
      <LossChart {metrics} height={260} />
    </div>

    <!-- Params + logs -->
    <div class="run-bottom">
      <div class="card">
        <div class="card-title mb-16">Hyperparamètres</div>
        <table class="table">
          <tbody>
            {#each [
              ['Iters', run.iters],
              ['Batch size', run.batch_size],
              ['Learning rate', run.learning_rate],
              ['LoRA rank', run.lora_rank],
              ['Expérience', run.experiment_name],
              ['Adapter path', run.adapter_path],
              ['MLflow run', run.mlflow_run_id],
              ['Démarré', run.started_at?.slice(0,19).replace('T',' ')],
              ['Terminé', run.finished_at?.slice(0,19).replace('T',' ') ?? '—'],
            ] as [label, value]}
              <tr>
                <td class="text-muted" style="width:140px">{label}</td>
                <td class="mono">{value ?? '—'}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>

      <div class="card">
        <div class="card-title mb-12">Logs live</div>
        <div class="log-viewer" bind:this={logEl}>
          {#each logs as line}
            <div class="log-line">{line}</div>
          {:else}
            <div class="text-muted">En attente de logs…</div>
          {/each}
        </div>
      </div>
    </div>

    <!-- Evaluations -->
    {#if evals.length > 0}
      <div class="card mt-16">
        <div class="card-title mb-16">Évaluations</div>
        <table class="table">
          <thead>
            <tr><th>Type</th><th>BLEU</th><th>Rouge-L</th><th>Pass rate</th><th>Latence (ms)</th><th>Date</th></tr>
          </thead>
          <tbody>
            {#each evals as e}
              <tr>
                <td class="badge badge-completed">{e.eval_type}</td>
                <td class="mono">{e.bleu?.toFixed(3) ?? '—'}</td>
                <td class="mono">{e.rouge_l?.toFixed(3) ?? '—'}</td>
                <td class="mono">{e.pass_rate !== null ? `${(e.pass_rate * 100).toFixed(1)}%` : '—'}</td>
                <td class="mono">{e.latency_ms ?? '—'}</td>
                <td class="text-muted">{e.created_at?.slice(0,10)}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  {:else}
    <p class="text-muted">Chargement…</p>
  {/if}
</div>

<style>
  .run-bottom { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
</style>
