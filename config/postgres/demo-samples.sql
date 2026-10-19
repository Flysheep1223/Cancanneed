\set ON_ERROR_STOP on
-- lookback_minutes 由 scripts/demo.sh 传入：120 补历史，0 写当前一批。
BEGIN;
SET LOCAL statement_timeout = '15s';
SET LOCAL lock_timeout = '5s';
SELECT pg_advisory_xact_lock(hashtext('grafana_demo_v1'));

WITH samples AS MATERIALIZED (
    SELECT
        t AS event_time, m.*,
        extract(epoch FROM t)::double precision AS seconds,
        random() AS cache_draw,
        random() AS latency_noise,
        random() AS token_noise,
        random() AS speed_noise,
        random() AS input_noise
    FROM generate_series(
        date_bin('5 seconds', now(), '2026-01-01'::timestamptz)
            - make_interval(mins => :lookback_minutes),
        date_bin('5 seconds', now(), '2026-01-01'::timestamptz),
        interval '5 seconds'
    ) AS t
    CROSS JOIN (VALUES
        ('demo-fast',      240.0, 115.0, 0.84, 0.0,    0.0),
        ('demo-balanced',  520.0,  72.0, 0.68, 160.0,  1.1),
        ('demo-reasoning', 980.0,  44.0, 0.52, 1050.0, 2.2)
    ) AS m(model, base_ttft, base_tps, cache_probability, base_reasoning, phase)
), workload AS (
    SELECT *,
        CASE WHEN cache_draw < cache_probability THEN 1.0 ELSE 0.0 END AS cache_hit,
        1.0 + 0.18 * sin(seconds / 150.0 + phase) AS wave,
        -- 每小时第 35～41 分钟模拟一段负载升高，便于观察历史曲线。
        CASE WHEN mod(seconds::bigint, 3600) BETWEEN 2100 AND 2460
             THEN 1.65 ELSE 1.0 END AS load_factor,
        round(180.0 + token_noise * 520.0) AS output_tokens,
        round(base_reasoning * (0.6 + token_noise * 0.9)) AS reasoning_tokens,
        round(1800.0 + input_noise * 4200.0) AS input_tokens
    FROM samples
), timings AS (
    SELECT *,
        base_ttft * wave * load_factor * (0.75 + latency_noise * 0.5)
            * CASE WHEN cache_hit = 1 THEN 0.72 ELSE 1.18 END AS ttft_ms,
        base_tps * (0.90 + speed_noise * 0.2) / (wave * load_factor) AS tps,
        CASE WHEN cache_hit = 1
             THEN round(input_tokens * (0.60 + input_noise * 0.32))
             ELSE 0 END AS cached_input_tokens
    FROM workload
)
INSERT INTO public.metric_samples (event_time, metric_name, value, labels)
SELECT
    event_time, metric.metric_name, metric.value,
    jsonb_build_object(
        'source', 'grafana_demo_v1', 'model', model,
        'environment', 'simulation', 'simulated', true
    )
FROM timings
CROSS JOIN LATERAL (VALUES
    ('ttft_ms', ttft_ms),
    ('e2e_ms', ttft_ms + 1000.0 * (output_tokens + reasoning_tokens) / tps),
    ('output_tokens', output_tokens),
    ('reasoning_tokens', reasoning_tokens),
    ('tokens_per_second', tps),
    ('cache_hit', cache_hit),
    ('input_tokens', input_tokens),
    ('cached_input_tokens', cached_input_tokens)
) AS metric(metric_name, value)
ON CONFLICT (event_time, metric_name, (labels->>'model'))
    WHERE labels->>'source' = 'grafana_demo_v1'
DO NOTHING;

-- 定期裁剪演示数据，业务数据不受影响。
DELETE FROM public.metric_samples
WHERE labels->>'source' = 'grafana_demo_v1'
  AND event_time < now() - interval '6 hours';
COMMIT;
