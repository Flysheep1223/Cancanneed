#!/usr/bin/env python3
"""Import a completed Ark record export into the local PostgreSQL TPM tables."""

import argparse
import csv
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from dotenv import load_dotenv
from psycopg import sql
from psycopg.types.json import Jsonb

from ark_minute_usage import aggregate_records, parse_end, read_json
from ark_usage import ARK_ROOT, PROJECT_ROOT, UsageError


def load_export(
    directory: Path, account_id: str | None = None,
    inclusion_window: tuple[int, int] | None = None,
) -> tuple[dict, dict, list[tuple]]:
    request = read_json(directory / "request.json")["body"]
    task = read_json(directory / "task.json")["Result"]["Task"]
    original_start, original_end = request["StartTime"], request["EndTime"]
    include_start, include_end = inclusion_window or (original_start, original_end)
    if not include_start <= original_start < original_end <= include_end:
        raise UsageError("批次纳入范围必须覆盖本导出查询窗口")
    effective_request = {**request, "StartTime": include_start, "EndTime": include_end}
    summary = aggregate_records(directory / "records.csv", effective_request, task)
    # The export's coverage is its real query window; inclusion may span the batch
    # because the service can return records timestamped just outside a shard.
    summary["scope"]["record_inclusion_window"] = dict(summary["window"])
    summary["window"]["start_inclusive"] = datetime.fromtimestamp(original_start, timezone.utc).isoformat()
    summary["window"]["end_exclusive"] = datetime.fromtimestamp(original_end, timezone.utc).isoformat()
    accounts = summary["scope"]["account_ids"]
    if account_id and any(account != account_id for account in accounts):
        raise UsageError("导出记录账号与指定账号不一致")
    if not accounts and account_id:
        summary["scope"]["account_ids"] = [account_id]
        accounts = [account_id]
    if len(accounts) != 1:
        raise UsageError("导出须属于单个账号；空结果需用 --account-id 指定已确认账号")
    if (directory / "minute-model.json").is_file():
        saved_scope = read_json(directory / "minute-model.json").get("scope", {})
        summary["scope"]["hourly_reference"] = saved_scope.get("hourly_reference", {})
    records = {}
    with (directory / "records.csv").open(encoding="utf-8-sig", newline="") as source:
        for row in csv.DictReader(source):
            timestamp = int(row["timestamp"])
            if not include_start <= timestamp < include_end:
                continue
            records[(row["account_id"], row["uuid"])] = (
                row["account_id"], row["uuid"], row["req_id"],
                datetime.fromtimestamp(timestamp, timezone.utc),
                row["model_name"], row["model_version"], row["model_endpoint"],
                int(row["input_tokens"]), int(row["output_tokens"]), int(row["cache_hit_tokens"]),
                task["Id"],
            )
    return summary, task, list(records.values())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="含 records.csv、request.json 和 task.json 的导出目录")
    parser.add_argument("--account-id", help="已确认账号；用于记录空窗口并核对账号归属")
    parser.add_argument("--include-start", type=parse_end, help="批次全局起点；与 --include-end 配对使用")
    parser.add_argument("--include-end", type=parse_end, help="批次全局终点，不改写导出窗口元数据")
    parser.add_argument("--dry-run", action="store_true", help="校验文件，不连接数据库")
    args = parser.parse_args()
    try:
        if bool(args.include_start) != bool(args.include_end):
            raise UsageError("--include-start 和 --include-end 必须同时提供")
        inclusion_window = (
            (int(args.include_start.timestamp()), int(args.include_end.timestamp()))
            if args.include_start else None
        )
        summary, task, records = load_export(args.directory.resolve(), args.account_id, inclusion_window)
        account = summary["scope"]["account_ids"][0]
        if args.dry_run:
            print(f"校验通过：账号 {account}，{len(records)} 条明细，{summary['data_count']} 行分钟统计")
            return 0
        load_dotenv(PROJECT_ROOT / ".env", override=True)
        required = ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_DB", "POSTGRES_USER", "POSTGRES_PASSWORD")
        if any(not os.getenv(key) for key in required):
            raise UsageError("根目录 .env 缺少 PostgreSQL 连接配置")
        scope = summary["scope"]
        with psycopg.connect(
            host=os.environ["POSTGRES_HOST"], port=int(os.environ["POSTGRES_PORT"]),
            dbname=os.environ["POSTGRES_DB"], user=os.environ["POSTGRES_USER"],
            password=os.environ["POSTGRES_PASSWORD"], connect_timeout=5,
            application_name="ark-tpm-import",
        ) as connection:
            connection.execute((ARK_ROOT / "sql" / "schema.sql").read_text())
            connection.execute(
                """INSERT INTO public.ark_usage_exports (
                    task_id, account_id, window_start, window_end, exported_at, export_count,
                    requested_models, unsupported_models, full_account_coverage, scope
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (task_id) DO UPDATE SET
                    scope = EXCLUDED.scope, unsupported_models = EXCLUDED.unsupported_models,
                    imported_at = now()""",
                (
                    task["Id"], account,
                    datetime.fromisoformat(summary["window"]["start_inclusive"]),
                    datetime.fromisoformat(summary["window"]["end_exclusive"]),
                    datetime.fromtimestamp(task["EndTime"], timezone.utc),
                    summary["validation"]["export_count"], scope["requested_models"],
                    scope.get("hourly_reference", {}).get("models_not_supported_by_export", []),
                    scope["full_account_coverage_confirmed"], Jsonb(scope),
                ),
            )
            with connection.cursor() as cursor:
                cursor.executemany(
                    """INSERT INTO public.ark_usage_records (
                        account_id, record_uuid, request_id, event_time, model_name,
                        model_version, endpoint_id, input_tokens, output_tokens,
                        cache_hit_tokens, last_export_task_id
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (account_id, record_uuid) DO UPDATE SET
                        request_id = EXCLUDED.request_id, event_time = EXCLUDED.event_time,
                        model_name = EXCLUDED.model_name, model_version = EXCLUDED.model_version,
                        endpoint_id = EXCLUDED.endpoint_id, input_tokens = EXCLUDED.input_tokens,
                        output_tokens = EXCLUDED.output_tokens,
                        cache_hit_tokens = EXCLUDED.cache_hit_tokens,
                        last_export_task_id = EXCLUDED.last_export_task_id, imported_at = now()
                    WHERE (SELECT exported_at FROM public.ark_usage_exports
                           WHERE task_id = EXCLUDED.last_export_task_id) >=
                          (SELECT exported_at FROM public.ark_usage_exports
                           WHERE task_id = ark_usage_records.last_export_task_id)""",
                    records,
                )
            reader = os.getenv("GRAFANA_POSTGRES_USER")
            if reader:
                connection.execute(
                    sql.SQL("GRANT SELECT ON public.ark_usage_exports, public.ark_usage_records, "
                            "public.ark_model_tpm, public.ark_model_rpm TO {}").format(sql.Identifier(reader))
                )
        print(
            f"已导入账号 {account} 的 {len(records)} 条明细；"
            f"按 UUID 更新，重复导入不会增加用量。分钟视图：public.ark_model_tpm"
        )
        return 0
    except (UsageError, OSError, ValueError, KeyError, csv.Error) as exc:
        message = str(exc) if isinstance(exc, UsageError) else f"本地数据异常：{type(exc).__name__}"
        print(f"错误：{message}", file=sys.stderr)
        return 1
    except psycopg.Error as exc:
        print(
            f"错误：数据库导入失败，SQLSTATE={exc.sqlstate or 'unknown'}，"
            f"{exc.diag.message_primary or type(exc).__name__}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
