#!/usr/bin/env python3
"""Export a bounded recent period, then update the combined wtpm / RPM dashboard."""

import argparse
import csv
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import requests
from dotenv import load_dotenv
from volcengine.Credentials import Credentials

from ark_minute_usage import (
    CSV_FIELDS, EXPORT_MODELS, aggregate_records, call_api, parse_end, read_json,
    save_aggregation, wait_and_download,
)
from ark_usage import ARK_ROOT, PROJECT_ROOT, SHANGHAI, UsageError, write_json


def windows(start: int, end: int) -> list[tuple[int, int]]:
    if start % 60 or end % 60 or end <= start:
        raise UsageError("时间范围必须按分钟对齐且结束晚于开始")
    return [(point, min(point + 10800, end)) for point in range(start, end, 10800)]


def save_manifest(root: Path, manifest: dict) -> None:
    temporary = root / "manifest.json.tmp"
    write_json(temporary, manifest)
    temporary.replace(root / "manifest.json")


def make_summary(root: Path, manifest: dict) -> dict:
    # Aggregate the original records using the global range, not by summing shard
    # aggregates: the service can return a timestamp just outside its query shard.
    raw_count = source_outside_count = 0
    merged = root / "merged-records.csv"
    with merged.open("w", encoding="utf-8-sig", newline="") as output:
        writer = None
        for item in manifest["windows"]:
            directory = root / item["directory"]
            task = read_json(directory / "task.json")["Result"]["Task"]
            if item["status"] != "imported" or task["ExportCount"] != task["TotalCount"]:
                raise UsageError("分片尚未导入或存在截断")
            document = read_json(directory / "minute-model.json")
            source_outside_count += document["validation"]["outside_window_skipped"]
            raw_count += task["ExportCount"]
            with (directory / "records.csv").open(encoding="utf-8-sig", newline="") as source:
                reader = csv.DictReader(source)
                if writer is None:
                    writer = csv.DictWriter(output, fieldnames=reader.fieldnames)
                    writer.writeheader()
                if reader.fieldnames != writer.fieldnames:
                    raise UsageError("分片 CSV 列定义不一致，无法直接合并")
                writer.writerows(reader)
    # This descriptor validates the local union, and is not a cloud task response.
    result = aggregate_records(
        merged,
        {"StartTime": manifest["start"], "EndTime": manifest["end"],
         "Filters": [{"Key": "model_name", "Values": manifest["models"]}]},
        {"Id": root.name, "Status": "Succeeded", "ExportCount": raw_count, "TotalCount": raw_count},
    )
    result.pop("task_id")
    result["source"] = "Ark official usage exports in contiguous windows"
    result["batch_id"] = root.name
    result["scope"]["hourly_reference"] = read_json(
        root / manifest["windows"][0]["directory"] / "minute-model.json"
    )["scope"].get("hourly_reference", {})
    result["validation"].update({
        "window_count": len(manifest["windows"]),
        "included_record_count": sum(row["record_count"] for row in result["rows"]),
        "records_outside_source_shard": source_outside_count,
        "continuous_query_windows": True,
        "note": "按全局范围重新聚合原始 CSV；分片越界但仍在批次范围内的记录按实际时间纳入。",
    })
    result["model_totals"].sort(key=lambda row: row["total_tokens"], reverse=True)
    write_json(root / "minute-model.json", result)
    write_json(root / "summary.json", {key: value for key, value in result.items() if key != "rows"})
    with (root / "minute-model.csv").open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(result["rows"])
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=24, help="最近 1～24 小时")
    parser.add_argument("--end", type=parse_end, help="固定结束时间，带时区且整分钟")
    parser.add_argument("--hourly-reference", type=Path, help="按模型查询的小时 normalized.json，用于确认账号与覆盖")
    parser.add_argument("--resume", type=Path, help="恢复已有批次；不重新提交已创建的任务")
    parser.add_argument("--dry-run", action="store_true", help="仅打印分片，不查询或导入")
    args = parser.parse_args()
    try:
        if args.resume:
            root = args.resume.resolve()
            manifest = read_json(root / "manifest.json")
            reference_path = Path(manifest["hourly_reference"])
        else:
            end = args.end or datetime.now(timezone.utc).replace(second=0, microsecond=0)
            start = end - timedelta(hours=args.hours)
            if not 1 <= args.hours <= 24 or start.timestamp() < 1777593600:
                raise UsageError("范围需为 1～24 小时，起点不早于 2026-05-01 UTC")
            if end > datetime.now(timezone.utc):
                raise UsageError("结束时间不能晚于当前时间")
            intervals = windows(int(start.timestamp()), int(end.timestamp()))
            if args.dry_run:
                for number, (lo, hi) in enumerate(intervals, 1):
                    print(f"{number}: {datetime.fromtimestamp(lo, SHANGHAI).isoformat()} → {datetime.fromtimestamp(hi, SHANGHAI).isoformat()}")
                return 0
            if not args.hourly_reference:
                raise UsageError("请用 --hourly-reference 指定已查询的小时数据，确认账号")
            reference_path = args.hourly_reference.resolve()
            reference = read_json(reference_path)
            accounts = {row["values"]["AccountID"] for row in reference["rows"]}
            if len(accounts) != 1:
                raise UsageError("小时参考数据需包含单个可确认账号")
            root = ARK_ROOT / "data" / f"sync-{args.hours}h-{end:%Y%m%dT%H%M}-{uuid4().hex[:8]}"
            root.mkdir(parents=True, exist_ok=False)
            manifest = {
                "status": "running", "account_id": accounts.pop(),
                "start": int(start.timestamp()), "end": int(end.timestamp()),
                "models": list(EXPORT_MODELS), "hourly_reference": str(reference_path),
                "windows": [
                    {"start": lo, "end": hi, "directory": f"window-{number:02d}", "status": "pending"}
                    for number, (lo, hi) in enumerate(intervals, 1)
                ],
            }
            save_manifest(root, manifest)
        print(f"批次目录：{root}", flush=True)
        load_dotenv(PROJECT_ROOT / ".env", override=True)
        if not os.getenv("VOLCENGINE_ACCESS_KEY") or not os.getenv("VOLCENGINE_SECRET_KEY"):
            raise UsageError("缺少临时凭据 AK/SK")
        credentials = Credentials(
            os.environ["VOLCENGINE_ACCESS_KEY"], os.environ["VOLCENGINE_SECRET_KEY"],
            "ark", "cn-beijing", os.getenv("VOLCENGINE_SESSION_TOKEN") or "",
        )
        with requests.Session() as session:
            session.trust_env = False
            for index, item in enumerate(manifest["windows"], 1):
                directory = root / item["directory"]
                directory.mkdir(exist_ok=True)
                if item["status"] == "imported" and item.get("record_inclusion") == "batch":
                    print(f"[{index}/{len(manifest['windows'])}] 已导入，跳过", flush=True)
                    continue
                body = {
                    "StartTime": item["start"], "EndTime": item["end"],
                    "Filters": [{"Key": "model_name", "Values": manifest["models"]}],
                }
                write_json(directory / "request.json", {"source": "CreateRecordExportTask", "body": body})
                if (directory / "created.json").is_file():
                    task_id = read_json(directory / "created.json")["Result"]["Task"]["Id"]
                else:
                    if item["status"] == "submitting":
                        raise UsageError(f"{item['directory']} 的创建结果未知，不能自动重复提交")
                    item["status"] = "submitting"
                    save_manifest(root, manifest)
                    created = call_api(session, credentials, "CreateRecordExportTask", body)
                    write_json(directory / "created.json", created)
                    task_id = created["Result"]["Task"]["Id"]
                item.update(status="created", task_id=task_id)
                save_manifest(root, manifest)
                if not (directory / "records.csv").is_file():
                    wait_and_download(session, credentials, directory, task_id)
                task = read_json(directory / "task.json")["Result"]["Task"]
                if task.get("ExportCount") != task.get("TotalCount"):
                    item["status"] = "truncated"
                    save_manifest(root, manifest)
                    raise UsageError(f"{item['directory']} 已截断，需缩小该窗口后继续；尚未导入此窗口")
                document = save_aggregation(directory, reference_path)
                accounts = document["scope"]["account_ids"]
                if accounts and accounts != [manifest["account_id"]]:
                    raise UsageError("分片返回了非预期账号，停止导入")
                completed = subprocess.run(
                    [sys.executable, str(ARK_ROOT / "scripts" / "import_tpm.py"), str(directory),
                     "--account-id", manifest["account_id"],
                     "--include-start", datetime.fromtimestamp(manifest["start"], timezone.utc).isoformat(),
                     "--include-end", datetime.fromtimestamp(manifest["end"], timezone.utc).isoformat()],
                    cwd=PROJECT_ROOT, capture_output=True, text=True,
                )
                if completed.returncode:
                    raise UsageError(completed.stderr.strip() or "分片导入失败")
                item.update(status="imported", record_inclusion="batch",
                            csv_rows=document["validation"]["csv_rows"], minute_rows=document["data_count"])
                save_manifest(root, manifest)
                print(
                    f"[{index}/{len(manifest['windows'])}] 已导入 {item['csv_rows']} 条明细、"
                    f"{item['minute_rows']} 行分钟统计", flush=True,
                )
        result = make_summary(root, manifest)
        built = subprocess.run(
            ["node", str(ARK_ROOT / "scripts" / "build-tpm-dashboard.mjs"), str(root / "minute-model.json")],
            cwd=PROJECT_ROOT, capture_output=True, text=True,
        )
        if built.returncode:
            raise UsageError("数据已导入，但 wtpm / RPM 合并看板配置生成失败")
        manifest.update(status="complete", completed_at=datetime.now(timezone.utc).isoformat())
        save_manifest(root, manifest)
        print(
            f"完成：{result['validation']['included_record_count']} 条有效明细，{result['data_count']} 行分钟数据，"
            f"{len(result['scope']['returned_models'])} 个模型；Grafana 默认范围已同步。", flush=True,
        )
        return 0
    except (UsageError, OSError, ValueError, KeyError, csv.Error) as exc:
        message = str(exc) if isinstance(exc, UsageError) else f"本地数据或执行异常：{type(exc).__name__}"
        print(f"错误：{message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
