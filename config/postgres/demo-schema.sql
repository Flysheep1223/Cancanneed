\set ON_ERROR_STOP on

-- 仅约束本演示的数据，不影响业务程序的采样方式。
CREATE UNIQUE INDEX IF NOT EXISTS metric_samples_demo_unique_idx
    ON public.metric_samples (event_time, metric_name, (labels->>'model'))
    WHERE labels->>'source' = 'grafana_demo_v1';
