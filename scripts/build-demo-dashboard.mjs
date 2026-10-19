import { mkdirSync, writeFileSync } from 'node:fs';

const datasource = { type: 'grafana-postgresql-datasource', uid: 'local-postgres' };
const where = `FROM public.metric_samples
WHERE labels->>'source' = 'grafana_demo_v1'
  AND $__timeFilter(event_time)
  AND labels->>'model' IN (\${model:sqlstring})`;
const colors = {
  'demo-fast': '#73BF69',
  'demo-balanced': '#5794F2',
  'demo-reasoning': '#FF9830',
};
let nextId = 1;

function target(rawSql, format = 'time_series') {
  return { refId: 'A', datasource, rawQuery: true, editorMode: 'code', rawSql, format };
}

function stat(title, description, expression, metric, unit, x, decimals = 0) {
  return {
    id: nextId++, title, description, type: 'stat', datasource,
    gridPos: { x, y: 3, w: 6, h: 4 },
    targets: [target(`SELECT ${expression} AS value\n${where}\n  AND metric_name = '${metric}';`, 'table')],
    fieldConfig: {
      defaults: { unit, decimals, color: { mode: 'fixed', fixedColor: 'blue' } },
      overrides: [],
    },
    options: {
      reduceOptions: { values: false, calcs: ['lastNotNull'], fields: '' },
      orientation: 'auto', textMode: 'value', colorMode: 'value',
      graphMode: 'none', justifyMode: 'center',
    },
  };
}

function series(title, description, metric, unit, x, y, interval = '$__interval', expression = 'avg(value)') {
  return {
    id: nextId++, title, description, type: 'timeseries', datasource,
    gridPos: { x, y, w: 12, h: 8 }, interval: interval === '$__interval' ? '5s' : interval,
    maxDataPoints: 360,
    targets: [target(`SELECT
  $__timeGroupAlias(event_time, '${interval}'),
  labels->>'model' AS metric,
  ${expression} AS value
${where}
  AND metric_name = '${metric}'
GROUP BY 1, 2
ORDER BY 1 ASC;`)],
    fieldConfig: {
      defaults: {
        unit, decimals: unit === 'ms' ? 0 : 2, min: 0,
        ...(unit === 'percentunit' ? { max: 1 } : {}),
        color: { mode: 'palette-classic' },
        custom: {
          drawStyle: 'line', lineInterpolation: 'linear', lineWidth: 2,
          fillOpacity: 8, showPoints: 'never', spanNulls: false,
          axisPlacement: 'auto', axisBorderShow: false,
          scaleDistribution: { type: 'linear' },
          stacking: { mode: 'none', group: 'A' },
          hideFrom: { legend: false, tooltip: false, viz: false },
        },
      },
      overrides: Object.entries(colors).map(([name, color]) => ({
        matcher: { id: 'byName', options: name },
        properties: [{ id: 'color', value: { mode: 'fixed', fixedColor: color } }],
      })),
    },
    options: {
      tooltip: { mode: 'multi', sort: 'desc' },
      legend: { showLegend: true, displayMode: 'table', placement: 'bottom', calcs: ['mean', 'lastNotNull'] },
    },
  };
}

function table(title, description, sql, x, w, overrides) {
  return {
    id: nextId++, title, description, type: 'table', datasource,
    gridPos: { x, y: 31, w, h: 9 },
    targets: [target(sql, 'table')],
    fieldConfig: { defaults: { decimals: 0, custom: { align: 'auto', cellOptions: { type: 'auto' } } }, overrides },
    options: { showHeader: true, cellHeight: 'sm', footer: { show: false } },
  };
}

const panels = [{
  id: nextId++, title: '模拟数据 · PostgreSQL → Grafana', type: 'text',
  gridPos: { x: 0, y: 0, w: 24, h: 3 },
  options: {
    mode: 'markdown',
    content: '**模拟数据，不代表真实模型性能。** 3 个虚拟模型 · 每 5 秒采样、刷新 · 已补 2 小时历史 · 保留 6 小时。上方可筛选模型和时间；每小时第 35–41 分钟模拟负载升高。',
  },
},
stat('模拟请求数', '当前时间范围、所选模型的模拟请求数。每个请求包含 8 条指标记录。', 'count(*)', 'ttft_ms', 'locale', 0),
stat('平均 TTFT', '当前时间范围内的平均首 Token 延迟。', 'avg(value)', 'ttft_ms', 'ms', 6),
stat('P95 端到端耗时', '对当前时间范围内所有所选请求直接计算 P95，包含模拟推理耗时。', 'percentile_cont(0.95) WITHIN GROUP (ORDER BY value)', 'e2e_ms', 'ms', 12),
stat('请求缓存命中率', '命中缓存的请求数 / 总请求数；与命中 Token 比例分别统计。', 'avg(value)', 'cache_hit', 'percentunit', 18, 1),
series('TTFT · 首 Token 延迟', '按时间窗口对每个模型取平均。单位 ms。', 'ttft_ms', 'ms', 0, 7),
series('E2E · 端到端耗时', '模拟 TTFT +（输出 Token + 推理 Token）/ 生成速度。', 'e2e_ms', 'ms', 12, 7),
series('生成速度 · 含推理 Token', '模拟输出和推理的总生成速度；不是单独的可见输出速度。', 'tokens_per_second', 'suffix:tok/s', 0, 15),
series('请求缓存命中率 · 1 分钟', '1 分钟内至少命中部分输入缓存的请求占比。', 'cache_hit', 'percentunit', 12, 15, '1m'),
series('输出 Token · 平均每请求', '每个模型每个时间窗口的可见输出 Token 平均值；不包含推理 Token。', 'output_tokens', 'short', 0, 23),
series('推理 Token · 平均每请求', '推理 Token 独立统计；demo-fast 设为无推理 Token。', 'reasoning_tokens', 'short', 12, 23),
table('Token 汇总 · 当前时间范围', '缓存 Token 比例 = 命中缓存的输入 Token 总量 / 输入 Token 总量。', `SELECT
  labels->>'model' AS "模型",
  sum(value) FILTER (WHERE metric_name = 'input_tokens') AS "输入 Token",
  sum(value) FILTER (WHERE metric_name = 'output_tokens') AS "输出 Token",
  sum(value) FILTER (WHERE metric_name = 'reasoning_tokens') AS "推理 Token",
  sum(value) FILTER (WHERE metric_name = 'cached_input_tokens')
    / nullif(sum(value) FILTER (WHERE metric_name = 'input_tokens'), 0) AS "缓存 Token 比例"
${where}
GROUP BY 1 ORDER BY 1;`, 0, 12, [{
  matcher: { id: 'byName', options: '缓存 Token 比例' },
  properties: [{ id: 'unit', value: 'percentunit' }, { id: 'decimals', value: 1 }],
}]),
table('最新模拟请求 · 原始采样明细', '查看最新 15 个模拟请求；时间随每 5 秒刷新前进。每个请求在数据库中由 8 条指标组成。', `SELECT
  event_time AS "采样时间",
  labels->>'model' AS "模型",
  max(value) FILTER (WHERE metric_name = 'ttft_ms') AS "TTFT",
  max(value) FILTER (WHERE metric_name = 'e2e_ms') AS "E2E",
  max(value) FILTER (WHERE metric_name = 'output_tokens') AS "输出 Token",
  max(value) FILTER (WHERE metric_name = 'reasoning_tokens') AS "推理 Token"
${where}
GROUP BY 1, 2 ORDER BY 1 DESC, 2
LIMIT 15;`, 12, 12, [
  { matcher: { id: 'byName', options: '采样时间' }, properties: [
    { id: 'unit', value: 'dateTimeAsIso' }, { id: 'custom.width', value: 185 },
  ] },
  ...['TTFT', 'E2E'].map(name => ({
    matcher: { id: 'byName', options: name },
    properties: [{ id: 'unit', value: 'ms' }],
  })),
]),
];

const modelQuery = `SELECT DISTINCT labels->>'model' AS __text, labels->>'model' AS __value
FROM public.metric_samples
WHERE labels->>'source' = 'grafana_demo_v1'
ORDER BY 1;`;

const dashboard = {
  uid: 'postgres-live-demo',
  title: '模型性能 · PostgreSQL 实时演示（模拟数据）',
  description: '演示 SQL 查询、时间窗口聚合、模型筛选及每 5 秒刷新。所有数据均为人工模拟。',
  tags: ['demo', 'postgresql', '模拟数据'],
  timezone: 'browser', schemaVersion: 39, version: 1,
  editable: false, graphTooltip: 1, refresh: '5s',
  time: { from: 'now-2h', to: 'now' },
  timepicker: { refresh_intervals: ['5s', '10s', '30s', '1m', '5m'] },
  annotations: { list: [] },
  templating: {
    list: [{
      name: 'model', label: '模型', type: 'query', datasource,
      definition: modelQuery, query: modelQuery,
      refresh: 1, sort: 1, multi: true, includeAll: true,
      current: { selected: true, text: 'All', value: '$__all' },
      options: [],
    }],
  },
  panels,
};

const output = new URL('../config/dashboards/postgres-live-demo.json', import.meta.url);
mkdirSync(new URL('../config/dashboards/', import.meta.url), { recursive: true });
writeFileSync(output, `${JSON.stringify(dashboard, null, 2)}\n`);
console.log(`已生成看板：${output.pathname}`);
