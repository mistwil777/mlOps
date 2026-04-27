<script lang="ts">
  import { onMount, onDestroy } from 'svelte';
  import type { Metric } from '$lib/types';

  const { metrics = [], height = 220 }: { metrics?: Metric[]; height?: number } = $props();

  let canvas: HTMLCanvasElement;
  let chart: import('chart.js').Chart | null = null;

  async function initChart() {
    const { Chart, LineController, LineElement, PointElement, LinearScale, CategoryScale, Legend, Tooltip, Filler } = await import('chart.js');
    Chart.register(LineController, LineElement, PointElement, LinearScale, CategoryScale, Legend, Tooltip, Filler);

    chart = new Chart(canvas, {
      type: 'line',
      data: {
        labels: metrics.map(m => m.step),
        datasets: [
          {
            label: 'Train loss',
            data: metrics.map(m => m.train_loss),
            borderColor: '#4f8ef7',
            backgroundColor: 'rgba(79,142,247,.08)',
            fill: true,
            tension: 0.3,
            pointRadius: 0,
            borderWidth: 2,
          },
          {
            label: 'Val loss',
            data: metrics.map(m => m.val_loss),
            borderColor: '#3ecf8e',
            backgroundColor: 'rgba(62,207,142,.06)',
            fill: true,
            tension: 0.3,
            pointRadius: 0,
            borderWidth: 2,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 0 },
        plugins: {
          legend: { labels: { color: '#8b92a8', boxWidth: 12, font: { size: 12 } } },
          tooltip: { mode: 'index', intersect: false },
        },
        scales: {
          x: {
            ticks: { color: '#8b92a8', maxTicksLimit: 8, font: { size: 11 } },
            grid: { color: '#2e3348' },
          },
          y: {
            ticks: { color: '#8b92a8', font: { size: 11 } },
            grid: { color: '#2e3348' },
          },
        },
      },
    });
  }

  $effect(() => {
    if (!chart || !metrics.length) return;
    chart.data.labels = metrics.map(m => m.step);
    chart.data.datasets[0].data = metrics.map(m => m.train_loss);
    chart.data.datasets[1].data = metrics.map(m => m.val_loss ?? null);
    chart.update('none');
  });

  onMount(() => { initChart(); });
  onDestroy(() => { chart?.destroy(); });
</script>

<div style="height:{height}px;position:relative;">
  <canvas bind:this={canvas}></canvas>
</div>
