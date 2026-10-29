<script lang="ts">
  import { onMount, onDestroy } from 'svelte';

  export interface KpiSeries {
    label: string;
    data: (number | null)[];
    color: string;
    dashed?: boolean;
    fill?: boolean;
  }

  const {
    series = [],
    labels = [],
    height = 220,
    yLabel = '',
    yMin = 0,
    yMax = 1,
    thresholdLine = null as number | null,
    formatY = (v: number) => (v * 100).toFixed(0) + '%',
  }: {
    series?: KpiSeries[];
    labels?: string[];
    height?: number;
    yLabel?: string;
    yMin?: number;
    yMax?: number;
    thresholdLine?: number | null;
    formatY?: (v: number) => string;
  } = $props();

  let canvas: HTMLCanvasElement;
  let chart: import('chart.js').Chart | null = null;

  async function initChart() {
    const {
      Chart, LineController, LineElement, PointElement,
      LinearScale, CategoryScale, Legend, Tooltip, Filler,
    } = await import('chart.js');
    Chart.register(LineController, LineElement, PointElement, LinearScale, CategoryScale, Legend, Tooltip, Filler);

    const datasets = series.map(s => ({
      label: s.label,
      data: s.data,
      borderColor: s.color,
      backgroundColor: s.fill ? s.color + '18' : 'transparent',
      fill: s.fill ?? false,
      tension: 0.35,
      pointRadius: 4,
      pointHoverRadius: 6,
      borderWidth: 2,
      borderDash: s.dashed ? [5, 3] : [],
    }));

    // Add threshold annotation as dataset if provided
    if (thresholdLine != null) {
      datasets.push({
        label: `Seuil (${formatY(thresholdLine)})`,
        data: labels.map(() => thresholdLine),
        borderColor: '#94a3b8',
        backgroundColor: 'transparent',
        fill: false,
        tension: 0,
        pointRadius: 0,
        pointHoverRadius: 0,
        borderWidth: 1,
        borderDash: [6, 4],
      } as any);
    }

    chart = new Chart(canvas, {
      type: 'line',
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: 'index', intersect: false },
        plugins: {
          legend: {
            position: 'bottom',
            labels: { font: { size: 11, family: 'Inter' }, color: '#64748b', boxWidth: 12, padding: 12 },
          },
          tooltip: {
            backgroundColor: '#0f172a',
            titleFont: { size: 12, family: 'Inter' },
            bodyFont: { size: 11, family: 'Inter' },
            padding: 10,
            callbacks: {
              label: (ctx: any) => ` ${ctx.dataset.label}: ${ctx.parsed.y != null ? formatY(ctx.parsed.y) : '—'}`,
            },
          },
        },
        scales: {
          x: {
            grid: { color: '#f1f5f9' },
            ticks: { font: { size: 11, family: 'Inter' }, color: '#94a3b8' },
          },
          y: {
            min: yMin,
            max: yMax,
            grid: { color: '#f1f5f9' },
            ticks: {
              font: { size: 11, family: 'Inter' },
              color: '#94a3b8',
              callback: (v: any) => formatY(v),
            },
            title: yLabel ? { display: true, text: yLabel, font: { size: 11 }, color: '#94a3b8' } : { display: false },
          },
        },
      },
    });
  }

  onMount(() => initChart());
  onDestroy(() => chart?.destroy());

  $effect(() => {
    if (!chart) return;
    chart.data.labels = labels;
    chart.data.datasets = series.map((s, i) => ({
      ...chart!.data.datasets[i],
      data: s.data,
      label: s.label,
    }));
    chart.update('none');
  });
</script>

<div class="kpi-chart" style="height: {height}px">
  <canvas bind:this={canvas}></canvas>
</div>

<style>
  .kpi-chart {
    width: 100%;
    position: relative;
  }
</style>
