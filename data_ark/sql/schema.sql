-- Imported official export records. Executed as metrics_writer in one transaction.
CREATE TABLE IF NOT EXISTS public.ark_usage_exports (
    task_id text PRIMARY KEY,
    account_id text NOT NULL,
    window_start timestamptz NOT NULL,
    window_end timestamptz NOT NULL CHECK (window_end > window_start),
    exported_at timestamptz NOT NULL,
    export_count integer NOT NULL CHECK (export_count >= 0),
    requested_models text[] NOT NULL,
    unsupported_models text[] NOT NULL DEFAULT '{}',
    full_account_coverage boolean NOT NULL DEFAULT false,
    scope jsonb NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.ark_usage_records (
    account_id text NOT NULL,
    record_uuid text NOT NULL,
    request_id text NOT NULL,
    event_time timestamptz NOT NULL,
    model_name text NOT NULL,
    model_version text NOT NULL,
    endpoint_id text NOT NULL,
    input_tokens bigint NOT NULL CHECK (input_tokens >= 0),
    output_tokens bigint NOT NULL CHECK (output_tokens >= 0),
    cache_hit_tokens bigint NOT NULL CHECK (
        cache_hit_tokens >= 0 AND cache_hit_tokens <= input_tokens
    ),
    last_export_task_id text NOT NULL REFERENCES public.ark_usage_exports (task_id),
    imported_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, record_uuid)
);

CREATE INDEX IF NOT EXISTS ark_usage_records_time_idx
    ON public.ark_usage_records (account_id, event_time, model_name);

CREATE OR REPLACE VIEW public.ark_model_tpm AS
SELECT
    date_trunc('minute', event_time) AS minute_start,
    account_id,
    model_name,
    sum(input_tokens) AS input_tokens,
    sum(output_tokens) AS output_tokens,
    sum(input_tokens::numeric + output_tokens) AS total_tokens,
    sum(cache_hit_tokens) AS cache_hit_tokens,
    count(*) AS record_count,
    count(DISTINCT request_id) AS request_count
FROM public.ark_usage_records
GROUP BY 1, 2, 3;

COMMENT ON VIEW public.ark_model_tpm IS
    '官方导出记录按记录时间汇总的每分钟 Token 消耗；缺失分钟不补零，非 TPM 配额';

-- One inference request can produce multiple billing records. Deduplicate before
-- bucketing so even records reported in different minutes count as one request.
CREATE OR REPLACE VIEW public.ark_model_rpm AS
WITH requests AS (
    SELECT
        account_id,
        model_name,
        request_id,
        min(event_time) AS request_time
    FROM public.ark_usage_records
    GROUP BY account_id, model_name, request_id
)
SELECT
    date_trunc('minute', request_time) AS minute_start,
    account_id,
    model_name,
    count(*) AS request_count
FROM requests
GROUP BY 1, 2, 3;

COMMENT ON VIEW public.ark_model_rpm IS
    '按账号、模型、请求 ID 去重，归入最早用量记录所在分钟；仅含已导出请求，非 RPM 配额';
