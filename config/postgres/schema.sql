\set ON_ERROR_STOP on
\getenv writer_user POSTGRES_USER
\getenv reader_user GRAFANA_POSTGRES_USER

BEGIN;
SET ROLE :"writer_user";
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- 通用指标表。一个时间点可有多个指标或多组标签，原始明细会保留。
CREATE TABLE IF NOT EXISTS public.metric_samples (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_time timestamptz NOT NULL DEFAULT now(),
    metric_name text NOT NULL,
    value double precision NOT NULL,
    labels jsonb NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(labels) = 'object'),
    ingested_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS metric_samples_name_time_idx
    ON public.metric_samples (metric_name, event_time DESC);
CREATE INDEX IF NOT EXISTS metric_samples_time_idx
    ON public.metric_samples (event_time DESC);

COMMENT ON TABLE public.metric_samples IS '业务程序写入的指标；Grafana 只读查询';
COMMENT ON COLUMN public.metric_samples.event_time IS '指标发生时间，使用带时区的时间戳';
COMMENT ON COLUMN public.metric_samples.ingested_at IS '数据库接收这条记录的时间';
COMMENT ON COLUMN public.metric_samples.labels IS '模型、服务、环境等分类标签，JSON 对象';

GRANT USAGE ON SCHEMA public TO :"reader_user";
GRANT SELECT ON ALL TABLES IN SCHEMA public TO :"reader_user";
ALTER DEFAULT PRIVILEGES FOR ROLE :"writer_user" IN SCHEMA public
    GRANT SELECT ON TABLES TO :"reader_user";
COMMIT;
