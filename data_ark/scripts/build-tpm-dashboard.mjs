import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';

const summaryPath = process.argv[2];
// No input builds the public template. Private batch windows stay in .runtime/.
const summary = summaryPath ? JSON.parse(readFileSync(summaryPath, 'utf8')) : null;
const time = summary ? {
  from: new Date(summary.window.start_inclusive).toISOString(),
  to: new Date(summary.window.end_exclusive).toISOString(),
} : { from: 'now-24h', to: 'now' };
const datasource = { type: 'grafana-postgresql-datasource', uid: 'local-postgres' };
const WTPM_UNIT = 'suffix: wtpm';
const account = 'account_id IN (${account:sqlstring})';
const selected = 'model_name IN (${model:sqlstring})';
const period = "minute_start >= $__timeFrom()::timestamptz AND minute_start < $__timeTo()::timestamptz";

function target(rawSql, format = 'table') {
  return { refId: 'A', datasource, rawSql, format, rawQuery: true, editorMode: 'code' };
}

// Display every minute for the selected models, filling absent records with zero.
function seriesSql(source, field, divisor) {
  return `WITH bounds AS (
  SELECT greatest(min(window_start), date_trunc('minute', $__timeFrom()::timestamptz)) AS lo,
         least(max(window_end), $__timeTo()::timestamptz) AS hi
  FROM public.ark_usage_exports WHERE ${account}
), minutes AS (
  SELECT generate_series(lo, hi - interval '1 microsecond', interval '1 minute') AS time FROM bounds
), models AS (
  SELECT DISTINCT model_name FROM public.ark_usage_records WHERE ${account} AND ${selected}
), samples AS (
  SELECT minute_start, model_name, sum(${field}) AS value
  FROM public.${source} WHERE ${account} AND ${selected} AND ${period} GROUP BY 1, 2
)
SELECT minutes.time, models.model_name AS metric, coalesce(samples.value, 0) / ${divisor} AS value
FROM minutes CROSS JOIN models
LEFT JOIN samples ON samples.minute_start = minutes.time AND samples.model_name = models.model_name
ORDER BY 1, 2;`;
}

function series({ id, title, source, field, divisor, unit, axisLabel, description, y }) {
  return {
    id, type: 'timeseries', title, description, datasource,
    gridPos: { x: 0, y, w: 24, h: 10 }, interval: '1m', maxDataPoints: 2000,
    targets: [target(seriesSql(source, field, divisor), 'time_series')],
    fieldConfig: {
      defaults: {
        unit, decimals: 0, min: 0,
        color: { mode: 'fixed', fixedColor: '#B877D9' },
        custom: {
          drawStyle: 'line', lineInterpolation: 'linear', lineWidth: 1,
          fillOpacity: 0, showPoints: 'never', spanNulls: true,
          axisPlacement: 'auto', axisBorderShow: false,
          axisLabel, scaleDistribution: { type: 'linear' },
          stacking: { mode: 'none', group: 'A' },
          hideFrom: { legend: false, tooltip: false, viz: false },
        },
      },
      overrides: [],
    },
    options: {
      tooltip: { mode: 'multi', sort: 'desc', hideZeros: false },
      legend: {
        showLegend: true, displayMode: 'list', placement: 'bottom',
        calcs: [],
      },
    },
  };
}

const panels = [
  series({
    id: 6, title: '各模型总 wtpm · 随时间变化', source: 'ark_model_tpm',
    field: 'total_tokens', divisor: '10000.0', unit: WTPM_UNIT,
    axisLabel: 'wtpm（万 Token / 分钟）',
    description: '1 wtpm = 10,000 Token/分钟。输入与输出相加，缓存已包含在输入中。固定一分钟桶，缺少记录补 0；仅覆盖已导入的部分模型。',
    y: 0,
  }),
  series({
    id: 7, title: '各模型 RPM · 随时间变化', source: 'ark_model_rpm',
    field: 'request_count', divisor: '1', unit: 'suffix: rpm',
    axisLabel: '请求 / 分钟',
    description: '每分钟请求数，按账号、模型和请求 ID 去重，以最早用量记录时间分桶；缺失分钟补 0。仅覆盖已导出请求。',
    y: 10,
  }),
];

const modelQuery = `SELECT DISTINCT model_name AS __text, model_name AS __value
FROM public.ark_usage_records WHERE ${account} ORDER BY 1;`;
const accountQuery = 'SELECT DISTINCT account_id AS __text, account_id AS __value FROM public.ark_usage_records ORDER BY 1;';
const dashboard = {
  uid: 'ark-model-tpm', title: '方舟用量 · wtpm / RPM',
  description: '同一看板展示各模型 Token 与请求量的分钟趋势，共用账号、模型和时间筛选。1 wtpm = 10,000 Token/分钟，RPM 为请求/分钟；仅覆盖已导入数据。',
  tags: ['ark', 'tpm', 'rpm', '真实用量'], timezone: 'Asia/Shanghai',
  schemaVersion: 39, version: 1, editable: false, graphTooltip: 1,
  refresh: '30s', time,
  timepicker: { refresh_intervals: ['30s', '1m', '5m'] },
  annotations: { list: [] },
  links: [],
  templating: { list: [
    {
      name: 'account', label: '账号', type: 'query', datasource,
      definition: accountQuery, query: accountQuery,
      refresh: 1, sort: 1, multi: false, includeAll: false,
      // Let Grafana select from the local database without publishing an account ID.
      options: [],
    },
    {
      name: 'model', label: '模型', type: 'query', datasource,
      definition: modelQuery, query: modelQuery,
      refresh: 2, sort: 1, multi: true, includeAll: true,
      current: { selected: true, text: 'All', value: '$__all' }, options: [],
    },
  ] },
  panels,
};

const directory = new URL(
  summary ? '../../.runtime/ark-dashboards/' : '../grafana/dashboards/',
  import.meta.url,
);
mkdirSync(directory, { recursive: true });
const output = new URL('ark-model-tpm.json', directory);
writeFileSync(output, `${JSON.stringify(dashboard, null, 2)}\n`);
console.log(`已生成看板：${output.pathname}`);
