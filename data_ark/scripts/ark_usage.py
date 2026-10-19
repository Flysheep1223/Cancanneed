#!/usr/bin/env python3
"""Read-only Ark GetInferenceUsage PoC. Run with the project .venv."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from volcengine.Credentials import Credentials
from volcengine.auth.SignerV4 import SignerV4
from volcengine.base.Request import Request

ARK_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ARK_ROOT.parent
HOST = "ark.cn-beijing.volcengineapi.com"
QUERY = {"Action": "GetInferenceUsage", "Version": "2024-01-01"}
SHANGHAI = ZoneInfo("Asia/Shanghai")
DIMENSIONS = (
    "ModelEndpoint", "ApikeyID", "ModelName", "ModelVersion", "EndpointName",
    "BillingStatus", "ModelUnitID", "BatchJobID", "BatchType",
)
INTEGER_TYPES = {"BIGINT", "INTEGER", "INT", "INT32", "INT64", "LONG"}


class UsageError(Exception):
    """A concise, user-facing error without credentials or HTTP headers."""


def parse_date(value: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise argparse.ArgumentTypeError("日期必须为 YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("日期无效") from None


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--start", required=True, type=parse_date, help="北京时间起始日期")
    result.add_argument("--end", required=True, type=parse_date, help="北京时间结束日期")
    result.add_argument("--interval", choices=("Hour", "Day"), default="Hour")
    result.add_argument("--project", default=os.getenv("ARK_PROJECT_NAME") or "default")
    result.add_argument("--endpoint", action="append", default=[], help="接入点 ID，可重复")
    result.add_argument("--api-key-id", action="append", default=[], help="资源 ID，非密钥，可重复")
    result.add_argument("--dimension", action="append", choices=DIMENSIONS, default=[],
                        help="返回此维度的所有值；使用 Values=[]，可重复")
    result.add_argument("--show-window-detail", action="store_true")
    result.add_argument("--dry-run", action="store_true", help="仅打印请求体；不签名、不联网")
    result.add_argument("--output-dir", type=Path, default=ARK_ROOT / "data")
    return result


def build_payload(args: argparse.Namespace) -> dict:
    if args.end < args.start:
        raise UsageError("--end 必须大于等于 --start")
    if not args.project.strip():
        raise UsageError("--project 不能为空")
    filters = {}
    for key, values in (("ModelEndpoint", args.endpoint), ("ApikeyID", args.api_key_id)):
        if any(not value.strip() for value in values):
            raise UsageError(f"{key} 不能包含空值")
        if values:
            filters[key] = list(dict.fromkeys(values))
    for key in args.dimension:
        filters.setdefault(key, [])
    payload = {
        "StartTime": args.start.isoformat(),
        "EndTime": args.end.isoformat(),
        "QueryInterval": args.interval,
        "ProjectName": args.project,
        "ShowWindowDetail": args.show_window_detail,
    }
    if filters:
        payload["Filters"] = [{"Key": key, "Values": values} for key, values in filters.items()]
    return payload


def sign_request(
    payload: dict, credentials: Credentials, action: str = "GetInferenceUsage"
) -> Request:
    request = Request()
    request.set_schema("https")
    request.set_method("POST")
    request.set_host(HOST)
    request.set_path("/")
    request.set_query({**QUERY, "Action": action})
    request.set_headers({"Content-Type": "application/json", "Host": HOST})
    request.set_body(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    SignerV4.sign(request, credentials)
    return request


def retry_delay(attempt: int, retry_after: str | None) -> float:
    """Respect Retry-After without blocking for more than 60 seconds."""
    delay = 2 ** attempt + random.uniform(0, 0.25)
    if retry_after:
        try:
            seconds = float(retry_after)
        except ValueError:
            try:
                seconds = (
                    parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)
                ).total_seconds()
            except (ValueError, TypeError, OverflowError):
                seconds = 0
        delay = max(delay, seconds)
    if delay > 60:
        raise UsageError("服务端要求等待超过 60 秒；本次停止，请稍后重试")
    return delay


def query_usage(payload: dict, credentials: Credentials) -> tuple[bytes, dict]:
    # Direct HTTPS; avoid implicit .netrc credentials replacing the signed header.
    with requests.Session() as session:
        session.trust_env = False
        for attempt in range(3):
            request = sign_request(payload, credentials)
            try:
                response = session.post(
                    request.build(), data=request.body.encode("utf-8"),
                    headers=dict(request.headers), timeout=(5, 30), allow_redirects=False,
                )
            except (requests.Timeout, requests.ConnectionError) as exc:
                if attempt == 2:
                    raise UsageError(f"网络请求失败：{type(exc).__name__}（已尝试 3 次）") from None
                delay = retry_delay(attempt, None)
                print(f"网络暂不可用，{delay:.1f}s 后重试", file=sys.stderr)
                time.sleep(delay)
                continue
            except requests.RequestException as exc:
                raise UsageError(f"网络请求失败：{type(exc).__name__}") from None

            if (response.status_code == 429 or 500 <= response.status_code < 600) and attempt < 2:
                delay = retry_delay(attempt, response.headers.get("Retry-After"))
                print(f"HTTP {response.status_code}，{delay:.1f}s 后重试", file=sys.stderr)
                time.sleep(delay)
                continue
            try:
                document = response.json()
            except ValueError:
                raise UsageError(f"HTTP {response.status_code}，响应不是 JSON") from None
            if not isinstance(document, dict):
                raise UsageError(f"HTTP {response.status_code}，响应顶层不是对象")
            metadata = document.get("ResponseMetadata") or {}
            if not isinstance(metadata, dict):
                raise UsageError("ResponseMetadata 格式异常")
            error = metadata.get("Error")
            if not 200 <= response.status_code < 300 or error:
                code = error.get("Code", "Unknown") if isinstance(error, dict) else "Unknown"
                request_id = metadata.get("RequestId", "unknown")
                raise UsageError(
                    f"API 查询失败：HTTP {response.status_code}，Code={code}，RequestId={request_id}"
                )
            return response.content, document
    raise UsageError("请求未完成")


def normalize_response(document: dict, interval: str) -> list[dict]:
    """Map Fields/Data dynamically; preserve unknown fields and integer precision."""
    if interval not in ("Hour", "Day"):
        raise UsageError("不支持的查询粒度")
    result = document.get("Result")
    if not isinstance(result, dict):
        raise UsageError("响应缺少 Result 对象")
    fields, data = result.get("Fields"), result.get("Data")
    if not isinstance(fields, list) or not isinstance(data, list):
        raise UsageError("Result.Fields / Result.Data 必须是数组")
    names = []
    for field in fields:
        if not isinstance(field, dict) or not isinstance(field.get("Name"), str):
            raise UsageError("Fields 包含无效列定义")
        names.append(field["Name"])
    if len(names) != len(set(names)):
        raise UsageError("Fields 中出现重复列名")
    count = result.get("DataCount")
    if type(count) is not int or count != len(data):
        raise UsageError("DataCount 与实际行数不一致")

    rows = []
    for index, values in enumerate(data):
        if not isinstance(values, list) or len(values) != len(fields):
            raise UsageError(f"第 {index + 1} 行与 Fields 列数不一致")
        row = {}
        for field, value in zip(fields, values):
            name = field["Name"]
            if value is not None and name.lower().endswith("id"):
                value = str(value)
            elif value is not None and str(field.get("Type", "")).upper() in INTEGER_TYPES:
                if type(value) not in (str, int):
                    raise UsageError(f"第 {index + 1} 行 {name} 不是整数")
                try:
                    value = int(value)
                except ValueError:
                    raise UsageError(f"第 {index + 1} 行 {name} 不是整数") from None
            row[name] = value
        try:
            day = parse_date(row["Day"])
            hour = 0
            if interval == "Hour":
                hour_value = str(row["Hour"])
                if not re.fullmatch(r"\d{1,2}", hour_value):
                    raise ValueError("Invalid hour")
                hour = int(hour_value)
            bucket = datetime(day.year, day.month, day.day, hour, tzinfo=SHANGHAI)
        except (KeyError, ValueError, TypeError, argparse.ArgumentTypeError):
            raise UsageError(f"第 {index + 1} 行缺少有效的 Day / Hour 时间字段") from None
        rows.append({"bucket_start": bucket.isoformat(), "values": row})
    return sorted(rows, key=lambda item: item["bucket_start"])


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    load_dotenv(PROJECT_ROOT / ".env", override=True)
    args = parser().parse_args(argv)
    try:
        payload = build_payload(args)
        request_info = {
            "method": "POST", "url": f"https://{HOST}/",
            "query": QUERY, "signing_service": "ark", "signing_region": "cn-beijing",
            "body": payload,
        }
        if args.dry_run:
            print(json.dumps(request_info, ensure_ascii=False, indent=2))
            return 0
        ak = os.getenv("VOLCENGINE_ACCESS_KEY")
        sk = os.getenv("VOLCENGINE_SECRET_KEY")
        if not ak or not sk:
            raise UsageError(
                "请在项目 .env 配置 VOLCENGINE_ACCESS_KEY 和 VOLCENGINE_SECRET_KEY；"
                "ARK_API_KEY 不能用于此接口。可先运行 --dry-run"
            )
        credentials = Credentials(
            ak, sk, "ark", "cn-beijing", os.getenv("VOLCENGINE_SESSION_TOKEN") or ""
        )
        raw, document = query_usage(payload, credentials)
        now = datetime.now(timezone.utc)
        run_dir = args.output_dir / f"{now:%Y%m%dT%H%M%S%fZ}-{uuid4().hex[:8]}"
        run_dir.mkdir(parents=True, exist_ok=False)
        # Keep exact successful response bytes even if a new schema breaks normalization.
        (run_dir / "raw.json").write_bytes(raw)
        write_json(run_dir / "request.json", {**request_info, "fetched_at": now.isoformat()})
        try:
            rows = normalize_response(document, args.interval)
        except UsageError as exc:
            raise UsageError(f"{exc}；原始响应已保存至 {run_dir / 'raw.json'}") from None
        write_json(run_dir / "normalized.json", {
            "source": "ark:GetInferenceUsage",
            "fetched_at": now.isoformat(), "timezone": "Asia/Shanghai",
            "request": payload, "response_metadata": document.get("ResponseMetadata"),
            "fields": document["Result"]["Fields"], "data_count": len(rows), "rows": rows,
        })
        print(f"已保存 {len(rows)} 行，项目={args.project}，粒度={args.interval}：{run_dir}")
        if not rows:
            print("空结果不代表已确认零消耗；需核对项目、时间、权限和数据延迟。")
        return 0
    except UsageError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except OSError:
        print("错误：本地配置或结果文件读写失败，请检查路径和权限。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
