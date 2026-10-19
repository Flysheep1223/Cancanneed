#!/usr/bin/env python3
"""Export Ark usage records and aggregate by record timestamp minute and model."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import requests
from dotenv import load_dotenv
from volcengine.Credentials import Credentials

from ark_usage import ARK_ROOT, PROJECT_ROOT, SHANGHAI, UsageError, sign_request, write_json

# Export API model allowlist; support may change independently of this client.
# The doc still calls doubao-seed-2-0-code "code-preview"; the service rejects that name.
EXPORT_MODELS = (
    "doubao-seed-2-0-pro", "doubao-seed-2-0-lite", "doubao-seed-2-0-mini",
    "doubao-seed-2-0-code", "doubao-seedance-2-0", "doubao-seedance-2-0-fast",
    "doubao-seed-evolving", "doubao-seed-2-1-pro", "doubao-seed-2-1-turbo",
    "doubao-seed-2-1-lite", "deepseek-v4-pro-ga", "deepseek-v4-flash-ga",
    "deepseek-v4-1-flash", "doubao-seedream-5-0-pro", "doubao-seedream-5-0-lite",
    "doubao-seedance-2-5",
)
TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_hit_tokens")
CSV_FIELDS = (
    "minute_start", "account_id", "model_name", "input_tokens", "output_tokens",
    "total_tokens", "cache_hit_tokens", "record_count", "request_count",
)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def call_api(session: requests.Session, credentials: Credentials, action: str, body: dict) -> dict:
    # In particular, never retry task creation: a timeout may hide a successful submission.
    signed = sign_request(body, credentials, action=action)
    try:
        response = session.post(
            signed.build(), headers=signed.headers, data=signed.body.encode("utf-8"),
            timeout=(5, 30), allow_redirects=False,
        )
    except requests.RequestException as exc:
        raise UsageError(
            f"{action} 网络失败：{type(exc).__name__}；未自动重试。"
            "如果创建结果未知，请先核对任务，避免重复提交。"
        ) from None
    try:
        document = response.json()
    except ValueError:
        raise UsageError(f"{action} HTTP {response.status_code}，响应不是 JSON") from None
    metadata = document.get("ResponseMetadata", {})
    error = metadata.get("Error")
    if response.status_code != 200 or error:
        code = error.get("Code", "Unknown") if isinstance(error, dict) else "Unknown"
        raise UsageError(
            f"{action} HTTP {response.status_code}，Code={code}，"
            f"RequestId={metadata.get('RequestId', 'unknown')}"
        )
    return document


def wait_and_download(
    session: requests.Session, credentials: Credentials, run_dir: Path, task_id: str
) -> None:
    deadline = time.monotonic() + 300
    last_status = None
    last_report = 0.0
    while time.monotonic() < deadline:
        time.sleep(5)
        document = call_api(session, credentials, "GetRecordExportTask", {"Id": task_id})
        task = document["Result"]["Task"]
        download_url = task.pop("DownloadUrl", None)
        # Do not persist or print the signed download URL: it grants temporary access.
        write_json(run_dir / "task.json", document)
        status = task.get("Status")
        if status != last_status or time.monotonic() - last_report >= 30:
            print(f"导出任务 {task_id}：{status}", flush=True)
            last_status, last_report = status, time.monotonic()
        if status == "Failed":
            raise UsageError(f"导出失败，详情见 {run_dir / 'task.json'}")
        if status != "Succeeded":
            continue
        if task.get("FileExpired") or not download_url:
            raise UsageError("导出文件已过期或未返回下载地址")
        target = urlsplit(download_url)
        if (
            target.scheme != "https" or not target.hostname
            or not target.hostname.endswith(".volces.com") or target.username
        ):
            raise UsageError("下载地址不是预期的火山引擎 HTTPS 地址")
        temporary = run_dir / "records.csv.part"
        try:
            # Only the URL's own signature is used. Never forward IAM request headers.
            with session.get(
                download_url, timeout=(5, 30), allow_redirects=False, stream=True
            ) as response:
                if response.status_code != 200:
                    raise UsageError(f"下载失败：HTTP {response.status_code}")
                with temporary.open("wb") as output:
                    for chunk in response.iter_content(256 * 1024):
                        output.write(chunk)
            temporary.replace(run_dir / "records.csv")
        except requests.RequestException as exc:
            raise UsageError(f"下载失败：{type(exc).__name__}；可用 --resume 重试") from None
        return
    raise UsageError(f"轮询已达 5 分钟；可稍后使用 --resume {run_dir}")


def integer_cell(value: str, field: str, line: int) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"\d+", value):
        raise UsageError(f"CSV 第 {line} 行 {field} 缺失或不是非负整数")
    return int(value)


def aggregate_records(csv_path: Path, request: dict, task: dict) -> dict:
    start, end = request["StartTime"], request["EndTime"]
    export_count, total_count = task.get("ExportCount"), task.get("TotalCount")
    if task.get("Status") != "Succeeded":
        raise UsageError("仅聚合成功任务")
    if type(export_count) is not int or type(total_count) is not int:
        raise UsageError("任务缺少有效的 ExportCount / TotalCount")
    if export_count != total_count:
        raise UsageError("导出发生截断，不能生成完整统计；请缩短查询窗口后导出")
    requested_models = {
        name for item in request.get("Filters", []) if item["Key"] == "model_name"
        for name in item["Values"]
    }
    buckets, models, seen = {}, {}, {}
    duplicate_count = outside_count = row_count = 0
    required = {"uuid", "req_id", "account_id", "model_name", "timestamp", *TOKEN_FIELDS}
    with csv_path.open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not required.issubset(reader.fieldnames or []):
            raise UsageError("CSV 缺少请求、模型、时间或 Token 必需列")
        for line, row in enumerate(reader, 2):
            row_count += 1
            if None in row or any(not row.get(key) for key in required):
                raise UsageError(f"CSV 第 {line} 行缺少必需值或列数异常")
            identity = (row["account_id"], row["uuid"])
            if identity in seen:
                if seen[identity] != row:
                    raise UsageError("相同记录 UUID 存在冲突数据")
                duplicate_count += 1
                continue
            seen[identity] = row
            timestamp = integer_cell(row["timestamp"], "timestamp", line)
            if not start <= timestamp < end:
                outside_count += 1
                continue
            if row["model_name"] not in requested_models:
                raise UsageError("响应包含请求筛选范围外的模型")
            tokens = {key: integer_cell(row[key], key, line) for key in TOKEN_FIELDS}
            if tokens["cache_hit_tokens"] > tokens["input_tokens"]:
                raise UsageError("缓存 Token 超过输入 Token，需重新核对字段口径")
            minute = datetime.fromtimestamp(timestamp - timestamp % 60, SHANGHAI).isoformat()
            key = (minute, row["account_id"], row["model_name"])
            model_key = (row["account_id"], row["model_name"])
            for group, group_key in ((buckets, key), (models, model_key)):
                target = group.setdefault(group_key, {
                    **dict.fromkeys(TOKEN_FIELDS, 0), "record_count": 0, "requests": set(),
                })
                for field in TOKEN_FIELDS:
                    target[field] += tokens[field]
                target["record_count"] += 1
                target["requests"].add(row["req_id"])
    if row_count != export_count:
        raise UsageError(f"CSV 实际 {row_count} 行，与 ExportCount={export_count} 不符")

    def totals(value: dict) -> dict:
        return {
            **{field: value[field] for field in TOKEN_FIELDS},
            "total_tokens": value["input_tokens"] + value["output_tokens"],
            "record_count": value["record_count"], "request_count": len(value["requests"]),
        }

    rows = [
        {"minute_start": key[0], "account_id": key[1], "model_name": key[2], **totals(value)}
        for key, value in sorted(buckets.items())
    ]
    model_totals = [
        {"account_id": key[0], "model_name": key[1], **totals(value)}
        for key, value in sorted(models.items())
    ]
    return {
        "source": "Ark CreateRecordExportTask / GetRecordExportTask",
        "task_id": task["Id"],
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {
            "start_inclusive": datetime.fromtimestamp(start, SHANGHAI).isoformat(),
            "end_exclusive": datetime.fromtimestamp(end, SHANGHAI).isoformat(),
            "interval_seconds": 60, "timestamp_field": "timestamp",
        },
        "scope": {
            "account_ids": sorted({row["account_id"] for row in model_totals}),
            "requested_models": sorted(requested_models),
            "returned_models": sorted({row["model_name"] for row in model_totals}),
            "models_without_exported_records": sorted(
                requested_models - {row["model_name"] for row in model_totals}
            ),
            "full_account_coverage_confirmed": False,
            "note": "仅覆盖此导出接口支持且本次返回的模型记录；无记录不等于最终零用量。",
        },
        "validation": {
            "export_count": export_count, "total_count": total_count, "csv_rows": row_count,
            "duplicate_records_skipped": duplicate_count, "outside_window_skipped": outside_count,
            "truncated": False,
        },
        "metric_notes": [
            "按导出记录 timestamp 分钟分桶，不代表逐 Token 实际生成时刻。",
            "total_tokens=input_tokens+output_tokens；cache_hit_tokens 是 input_tokens 的子集。",
            "不同模型版本与接入点按 model_name 合并；不将计费缩放值当成 Token。",
            "稀疏输出；没有记录的分钟不补零，近期记录可能迟到。",
        ],
        "data_count": len(rows), "rows": rows, "model_totals": model_totals,
    }


def save_aggregation(run_dir: Path, hourly_reference: Path | None = None) -> dict:
    document = aggregate_records(
        run_dir / "records.csv", read_json(run_dir / "request.json")["body"],
        read_json(run_dir / "task.json")["Result"]["Task"],
    )
    if hourly_reference:
        reference = read_json(hourly_reference)
        known = {row["values"]["ModelName"] for row in reference["rows"]}
        document["scope"]["hourly_reference"] = {
            "request": reference["request"],
            "models_not_supported_by_export": sorted(known - set(EXPORT_MODELS)),
            "note": "这是小时参考数据中出现的模型，不证明其在本次精确分钟窗口内有请求。",
        }
    write_json(run_dir / "minute-model.json", document)
    with (run_dir / "minute-model.csv").open("w", encoding="utf-8-sig", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(document["rows"])
    write_json(run_dir / "summary.json", {key: value for key, value in document.items() if key != "rows"})
    print(
        f"已生成 {document['data_count']} 行分钟统计，"
        f"{len(document['model_totals'])} 个账号/模型组合：{run_dir}", flush=True,
    )
    return document


def parse_end(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("时间必须为带时区的 ISO 格式") from None
    if result.tzinfo is None or result.second or result.microsecond:
        raise argparse.ArgumentTypeError("时间必须带时区，并对齐整分钟")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--minutes", type=int, default=60, help="最近完整分钟数，1～180")
    parser.add_argument("--end", type=parse_end, help="结束时间，ISO 8601 格式，带时区且整分钟")
    parser.add_argument("--model", action="append", choices=EXPORT_MODELS, help="可重复；默认支持列表")
    parser.add_argument("--hourly-reference", type=Path, help="现有小时 normalized.json，用于标记缺失模型")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--resume", type=Path, help="已有任务目录，继续轮询/下载，不重新创建")
    modes.add_argument("--offline", type=Path, help="已有任务目录，仅解析本地 CSV，不联网")
    modes.add_argument("--dry-run", action="store_true", help="仅显示新任务参数，不提交")
    args = parser.parse_args()
    try:
        if args.offline:
            save_aggregation(args.offline.resolve(), args.hourly_reference)
            return 0
        end = args.end or datetime.now(timezone.utc).replace(second=0, microsecond=0)
        start = end - timedelta(minutes=args.minutes)
        if not 1 <= args.minutes <= 180 or start.timestamp() < 1777593600:
            raise UsageError("窗口需为 1～180 分钟，且起点不早于 2026-05-01 UTC")
        if end > datetime.now(timezone.utc):
            raise UsageError("结束时间不能晚于当前时间")
        payload = {
            "StartTime": int(start.timestamp()), "EndTime": int(end.timestamp()),
            "Filters": [{"Key": "model_name", "Values": list(dict.fromkeys(args.model or EXPORT_MODELS))}],
        }
        if args.dry_run:
            print(json.dumps(payload, ensure_ascii=False, indent=2))
            return 0
        load_dotenv(PROJECT_ROOT / ".env", override=True)
        ak, sk = os.getenv("VOLCENGINE_ACCESS_KEY"), os.getenv("VOLCENGINE_SECRET_KEY")
        if not ak or not sk:
            raise UsageError("请在根目录 .env 配置 AK/SK，临时凭据另需 VOLCENGINE_SESSION_TOKEN")
        credentials = Credentials(ak, sk, "ark", "cn-beijing", os.getenv("VOLCENGINE_SESSION_TOKEN") or "")
        with requests.Session() as session:
            session.trust_env = False
            if args.resume:
                run_dir = args.resume.resolve()
                task_id = read_json(run_dir / "created.json")["Result"]["Task"]["Id"]
            else:
                run_dir = ARK_ROOT / "data" / f"minutes-{end:%Y%m%dT%H%M}-{uuid4().hex[:8]}"
                run_dir.mkdir(parents=True, exist_ok=False)
                write_json(run_dir / "request.json", {"source": "CreateRecordExportTask", "body": payload})
                print(f"创建一次导出任务，结果目录：{run_dir}", flush=True)
                created = call_api(session, credentials, "CreateRecordExportTask", payload)
                write_json(run_dir / "created.json", created)
                task_id = created["Result"]["Task"]["Id"]
            if not (run_dir / "records.csv").exists():
                wait_and_download(session, credentials, run_dir, task_id)
        save_aggregation(run_dir, args.hourly_reference)
        return 0
    except (UsageError, OSError, ValueError, KeyError, csv.Error) as exc:
        message = str(exc) if isinstance(exc, UsageError) else f"本地文件或数据格式异常：{type(exc).__name__}"
        print(f"错误：{message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
