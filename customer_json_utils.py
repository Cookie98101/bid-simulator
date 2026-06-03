import copy
import json
from pathlib import Path
from typing import Any


def derive_split_customer_json_path(customer_json_path: str, label: str) -> str:
    path = Path(customer_json_path)
    if path.suffix:
        return str(path.with_name(f"{path.stem}_{label}{path.suffix}"))
    return str(path.with_name(f"{path.name}_{label}.json"))


def is_customer_project_complete(project: dict[str, Any]) -> bool:
    return str(project.get("是否核心数据齐全") or "").strip() == "是"


def _as_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _numeric_stats(values: list[Any]) -> dict[str, Any]:
    clean_values = [value for value in (_as_float(item) for item in values) if value is not None]
    if not clean_values:
        return {"样本数": 0, "最低": None, "最高": None, "平均": None}
    return {
        "样本数": len(clean_values),
        "最低": min(clean_values),
        "最高": max(clean_values),
        "平均": sum(clean_values) / len(clean_values),
    }


def _count_stats(values: list[Any]) -> dict[str, Any]:
    stats = _numeric_stats(values)
    for key in ("最低", "最高"):
        if stats[key] is not None:
            stats[key] = int(stats[key])
    return stats


def _build_customer_analysis(projects: list[dict[str, Any]]) -> dict[str, Any]:
    bucket_names = ["小于1000万", "1000万-2000万", "2000万-5000万", "5000万-1亿", "1亿及以上", "控制价缺失"]
    bucket_rows: dict[str, dict[str, list[Any]]] = {
        name: {
            "控制价": [],
            "中标价下浮率": [],
            "报价下浮率": [],
            "参与单位数": [],
            "项目数": [],
        }
        for name in bucket_names
    }
    all_control_prices: list[Any] = []
    all_winning_down_rates: list[Any] = []
    all_quote_down_rates: list[Any] = []
    all_participant_counts: list[Any] = []

    for project in projects:
        bucket = str(project.get("控制价档位") or "控制价缺失")
        if bucket not in bucket_rows:
            bucket = "控制价缺失"
        bucket_rows[bucket]["项目数"].append(1)

        control_price = project.get("控制价")
        if control_price is not None:
            bucket_rows[bucket]["控制价"].append(control_price)
            all_control_prices.append(control_price)

        winning_down_rate = project.get("中标下浮率")
        if winning_down_rate is not None:
            bucket_rows[bucket]["中标价下浮率"].append(winning_down_rate)
            all_winning_down_rates.append(winning_down_rate)

        quote_down_rates = [
            row.get("下浮率")
            for row in project.get("各单位报价下浮率排序") or []
            if isinstance(row, dict) and row.get("下浮率") is not None
        ]
        bucket_rows[bucket]["报价下浮率"].extend(quote_down_rates)
        all_quote_down_rates.extend(quote_down_rates)

        participant_count = project.get("报价家数")
        if participant_count is not None:
            bucket_rows[bucket]["参与单位数"].append(participant_count)
            all_participant_counts.append(participant_count)

    return {
        "控制价档位统计": {
            name: {
                "项目数": len(bucket_rows[name]["项目数"]),
                "控制价统计": _numeric_stats(bucket_rows[name]["控制价"]),
                "中标价下浮率统计": _numeric_stats(bucket_rows[name]["中标价下浮率"]),
                "报价下浮率统计": _numeric_stats(bucket_rows[name]["报价下浮率"]),
                "参与单位数统计": _count_stats(bucket_rows[name]["参与单位数"]),
            }
            for name in bucket_names
        },
        "总体统计": {
            "控制价统计": _numeric_stats(all_control_prices),
            "报价下浮率统计": _numeric_stats(all_quote_down_rates),
            "中标价下浮率统计": _numeric_stats(all_winning_down_rates),
            "参与单位数统计": _count_stats(all_participant_counts),
            "有控制价项目数": len(all_control_prices),
            "有中标下浮率项目数": len(all_winning_down_rates),
            "有报价明细项目数": sum(1 for item in projects if item.get("各单位报价下浮率排序")),
        },
    }


def build_split_customer_payload(customer_payload: dict[str, Any], complete: bool) -> dict[str, Any]:
    payload = copy.deepcopy(customer_payload)
    projects = [item for item in payload.get("项目列表") or [] if isinstance(item, dict)]
    filtered_projects = [item for item in projects if is_customer_project_complete(item) == complete]
    complete_count = sum(1 for item in filtered_projects if is_customer_project_complete(item))
    file_complete_count = sum(1 for item in filtered_projects if str(item.get("是否三类文件完整") or "").strip() == "是")

    payload["筛选"] = "已抓全项目" if complete else "未抓全项目"
    payload["项目列表"] = filtered_projects
    payload["分析"] = _build_customer_analysis(filtered_projects)

    for key in ("汇总", "统计"):
        summary = payload.get(key)
        if not isinstance(summary, dict):
            continue
        summary["筛选后项目数"] = len(filtered_projects)
        summary["核心数据齐全项目数"] = complete_count
        summary["未抓全项目数"] = len(filtered_projects) - complete_count
        if "项目总数" in summary:
            summary["项目总数"] = len(filtered_projects)
        if "归并项目数" in summary:
            summary["归并项目数"] = len(filtered_projects)
        if "三文件完整项目数" in summary:
            summary["三文件完整项目数"] = file_complete_count
        if "三类文件完整项目数" in summary:
            summary["三类文件完整项目数"] = file_complete_count

    sources = payload.get("来源")
    if isinstance(sources, dict):
        for source_name, source_summary in sources.items():
            if not isinstance(source_summary, dict):
                continue
            source_projects = [item for item in filtered_projects if str(item.get("数据来源") or "") == str(source_name)]
            source_summary["项目数"] = len(source_projects)
            if "核心字段齐全项目数" in source_summary:
                source_summary["核心字段齐全项目数"] = sum(1 for item in source_projects if is_customer_project_complete(item))

    return payload


def write_customer_json_splits(customer_json_path: str, customer_payload: dict[str, Any]) -> dict[str, str]:
    paths = {
        "已抓全": derive_split_customer_json_path(customer_json_path, "已抓全"),
        "未抓全": derive_split_customer_json_path(customer_json_path, "未抓全"),
    }
    for label, complete in (("已抓全", True), ("未抓全", False)):
        Path(paths[label]).write_text(
            json.dumps(build_split_customer_payload(customer_payload, complete), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return paths
