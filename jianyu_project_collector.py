#!/usr/bin/env python3
"""Collect Jianyu360 project data with login cookies and normalize core fields.

This script focuses on the discovery layer:
- query Jianyu360 searchList with login cookies
- normalize field names across record variants
- classify notice types
- group records by normalized project title
- report which projects appear usable for downstream analysis

It intentionally avoids agent-based interpretation.
"""

from __future__ import annotations

import argparse
import base64
import dataclasses
import hashlib
import html
import json
import os
import random
import re
import sys
import time
import datetime as dt
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from html.parser import HTMLParser
from PIL import Image, ImageDraw, ImageFont
import numpy as np
import cv2
from sklearn.cluster import KMeans
from playwright.sync_api import sync_playwright


SEARCH_URL = "https://www.jianyu360.cn/jyapi/jybx/core/fType/searchList"
PREAGENT_URL = "https://www.jianyu360.cn/publicapply/detail/preAgent"
DETAIL_BASEINFO_URL = "https://www.jianyu360.cn/publicapply/detail/baseInfo"
LOGIN_URL = "https://www.jianyu360.cn/jylab/supsearch/index.html?keywords=&selectType=title&searchGroup=1"
DETAIL_URL_PREFIXES = (
    "https://www.jianyu360.cn",
    "https://xizang.jianyu360.cn",
)
PLAYWRIGHT_EXECUTABLE_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]
DEFAULT_PUBLISH_RANGE_DAYS = 30
TIBET_CITY_SLUGS: dict[str, str] = {
    "拉萨": "lasa",
    "拉萨市": "lasa",
    "昌都": "changdou",
    "昌都市": "changdou",
    "日喀则": "rikaze",
    "日喀则市": "rikaze",
    "林芝": "linzhi",
    "林芝市": "linzhi",
    "山南": "shannan",
    "山南市": "shannan",
    "那曲": "naqu",
    "那曲市": "naqu",
    "阿里": "ali",
    "阿里地区": "ali",
}
DEFAULT_TIBET_CITY_PAGE_SLUGS = ["lasa", "changdou", "rikaze", "linzhi", "shannan"]
GENERIC_PROJECT_TOKENS = {
    "项目",
    "工程",
    "建设",
    "改造",
    "采购",
    "招标",
    "公告",
    "公示",
    "结果",
    "中标",
    "候选人",
    "记录",
    "业务",
    "技术",
    "用房",
    "边防",
    "检查站",
    "支队",
    "总站",
    "二次",
    "标段",
}


NOTICE_KEYWORDS = {
    "招标公告": ("招标公告", "招标文件", "采购公告", "招标邀请书", "询比公告", "竞争性磋商公告", "竞争性谈判公告"),
    "开标记录": ("开标记录", "开标一览表", "开标情况", "开标记录表"),
    "中标结果": ("中标结果", "结果公告", "成交结果", "中标公告", "结果公示", "成交公告", "结果"),
    "中标候选人": ("中标候选人公示", "候选人公示", "成交候选人公示"),
}


NUMBER_RE = re.compile(r"(?<!\w)(\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)(?!\w)")
MASK_RE = re.compile(r"\*+")
UNIT_RE = re.compile(r"(万元|元)")


FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "project_name": ("title", "projectName", "name", "项目名称"),
    "notice_type": ("subtype", "type", "公告类型", "文件类型"),
    "buyer": ("buyer", "采购单位", "招标人", "采购人", "建设单位", "业主单位", "招标单位", "发包人", "实施单位", "名称"),
    "winner": ("winner", "中标单位", "成交供应商", "中标人", "成交人", "供应商名称", "中标主体", "中选单位", "第一中标候选人", "第一成交候选人"),
    "budget": ("budget", "控制价", "招标控制价", "最高投标限价", "预算金额", "最高限价", "拦标价", "招标上限价"),
    "bid_amount": ("bidAmount", "中标金额", "中标（成交）金额", "成交金额", "中标价格", "成交价", "中标价", "中选金额"),
    "bid_open_time": ("bidOpenTime", "开标时间"),
    "publish_time": ("publishTime", "发布时间", "发布日期"),
    "city": ("city", "地市", "地区"),
    "district": ("district", "区县", "县区"),
    "industry": ("industry", "行业"),
    "site": ("site", "来源", "平台"),
    "spider_code": ("spiderCode", "爬虫编码"),
}


VALUE_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "budget": ("budget", "控制价", "招标控制价", "最高投标限价", "预算金额", "最高限价", "控制价（最高限价）", "拦标价", "招标上限价"),
    "bid_amount": ("bidAmount", "中标金额", "中标（成交）金额", "成交金额", "中标价格", "成交价", "中标价", "中选金额", "投标报价", "投标总报价"),
    "winner": ("winner", "中标单位", "中标人", "成交供应商", "成交人", "供应商名称", "中标主体", "中选单位", "第一中标候选人", "第一成交候选人"),
}

QUOTE_TABLE_ALIASES = ("投标报价", "投标总报价", "报价(元)", "报价（元）", "报价", "最终报价", "投标总价", "评标报价")
BUDGET_TABLE_ALIASES = ("控制价", "最高限价", "招标控制价", "控制总价", "预算金额", "拦标价")
COMPANY_TABLE_ALIASES = ("投标人名称", "供应商名称", "投标单位", "投标人", "单位名称", "供应商")

ROLE_NOISE_PREFIXES = (
    "称响应情况",
    "响应情况",
    "推荐第一中标候选人",
    "第一中标候选人",
    "中标候选人",
    "成交候选人",
    "中标单位",
    "中标人",
    "成交供应商",
    "成交人",
)

COMPANY_ENDINGS = ("有限公司", "有限责任公司", "集团有限公司", "股份有限公司", "工程公司", "建筑工程有限公司", "路桥有限公司")
ORG_ENDINGS = (
    "有限公司",
    "有限责任公司",
    "集团有限公司",
    "股份有限公司",
    "工程公司",
    "建筑工程有限公司",
    "路桥有限公司",
    "总站",
    "支队",
    "总队",
    "大队",
    "学校",
    "医院",
    "委员会",
    "管理局",
    "人民政府",
    "公安局",
    "财政局",
    "住房和城乡建设局",
    "自然资源局",
    "边防检查总站",
)
NAME_NOISE_PATTERNS = (
    r"^\d{3,4}-\d{7,8}$",
    r"^加央白玛$",
    r"^奚莎$",
    r"^普国杰$",
    r"^采购联系人$",
    r"^采购电话$",
    r"^公告详情$",
    r"^招标详情$",
    r"^项目详情$",
)


class HTMLTextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        if data and data.strip():
            self.parts.append(data)

    def get_text(self) -> str:
        return "\n".join(self.parts)


def strip_html_fragment(source: str) -> str:
    text = html_to_text(source or "")
    text = normalize_loose_text(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def extract_open_record_table_html(html_source: str) -> str | None:
    marker_match = re.search(r"开标记录内容</th>\s*<td>", html_source, flags=re.IGNORECASE)
    if not marker_match:
        return None
    remainder = html_source[marker_match.end():]
    table_start = remainder.find("<table")
    if table_start < 0:
        return None
    i = table_start
    depth = 0
    pattern = re.compile(r"</?table\b[^>]*>", flags=re.IGNORECASE)
    for match in pattern.finditer(remainder, table_start):
        token = match.group(0).lower()
        if token.startswith("<table"):
            depth += 1
        else:
            depth -= 1
            if depth == 0:
                return remainder[table_start:match.end()]
    return None


def extract_tab_pairs(html_source: str) -> dict[str, str]:
    labels = re.findall(r'<td class="tab-label">\s*(.*?)\s*</td>', html_source, re.IGNORECASE | re.DOTALL)
    values = re.findall(r'<td class="tab-value[^"]*">\s*(.*?)\s*</td>', html_source, re.IGNORECASE | re.DOTALL)
    cleaned_labels = [normalize_text(re.sub(r"<[^>]+>", "", html.unescape(x))) for x in labels]
    cleaned_values = [normalize_loose_text(re.sub(r"<[^>]+>", "", html.unescape(x))).strip() for x in values]
    result: dict[str, str] = {}
    value_idx = 0
    for label in cleaned_labels:
        if not label:
            continue
        while value_idx < len(cleaned_values) and cleaned_values[value_idx] == "":
            value_idx += 1
        if value_idx >= len(cleaned_values):
            break
        result[label] = cleaned_values[value_idx]
        value_idx += 1
    return result


def extract_detail_section(html_source: str) -> str:
    if 'class="detail-title"' not in html_source and "class='detail-title'" not in html_source:
        return ""
    blocks: list[str] = []
    for class_name in ("base-info", "detail-html"):
        for match in re.finditer(rf'<div class="{class_name}">(.*?)</div>', html_source, re.IGNORECASE | re.DOTALL):
            blocks.append(match.group(1))
    if blocks:
        return "\n".join(blocks)
    detail_content_match = re.search(r'<main class="detail-content">(.*?)</main>', html_source, re.IGNORECASE | re.DOTALL)
    if detail_content_match:
        return detail_content_match.group(1)
    if 'class="detail-title"' in html_source or "class='detail-title'" in html_source:
        section_match = re.search(r'<section class="page-main-content">(.*?)</section>', html_source, re.IGNORECASE | re.DOTALL)
        if section_match:
            return section_match.group(1)
    match = re.search(r'<section[^>]*>(.*?)</section>', html_source, re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1)
    return ""


def extract_meta_content(html_source: str, meta_name: str) -> str | None:
    pattern = rf'<meta[^>]+name=["\']{re.escape(meta_name)}["\'][^>]+content=["\']([^"\']*)["\']'
    match = re.search(pattern, html_source, re.IGNORECASE)
    if match:
        return html.unescape(match.group(1))
    return None


def extract_title_from_html(html_source: str) -> str | None:
    match = re.search(r"<title>(.*?)</title>", html_source, re.IGNORECASE | re.DOTALL)
    if match:
        return html.unescape(re.sub(r"\s+", "", match.group(1)))
    return None


def extract_publish_date_from_html(html_source: str) -> str | None:
    patterns = [
        r"发布日期[:：]?\s*([0-9]{4}-[0-9]{2}-[0-9]{2})",
        r"发布时间[:：]?\s*([0-9]{4}-[0-9]{2}-[0-9]{2})",
        r'<span>\s*发布日期[:：]?\s*([0-9]{4}-[0-9]{2}-[0-9]{2})\s*</span>',
        r'<span>\s*发布时间[:：]?\s*([0-9]{4}-[0-9]{2}-[0-9]{2})\s*</span>',
    ]
    for pattern in patterns:
        match = re.search(pattern, html_source, re.IGNORECASE)
        if match:
            return match.group(1)
    return None


def extract_detail_url_from_html(html_source: str) -> str | None:
    for pattern in (
        r"link-href=['\"]https://www\.jianyu360\.cn/notin/page\?backTo=([^'\"&]+)",
        r"link-href=['\"]([^'\"]+/jybx/[^'\"]+)",
        r"href=['\"]([^'\"]+/jybx/[^'\"]+\.html)['\"]",
    ):
        match = re.search(pattern, html_source, re.IGNORECASE)
        if not match:
            continue
        value = html.unescape(match.group(1))
        if value.startswith("http"):
            return value
        decoded = urllib.parse.unquote(value)
        if decoded.startswith("http"):
            return decoded
        if "/jybx/" in decoded:
            return urllib.parse.urljoin("https://xizang.jianyu360.cn", decoded)
    return None


def build_nologin_content_url(url: str, keyword_hint: str = "") -> str:
    raw = str(url or "").strip()
    if not raw:
        return raw
    match = re.search(r"/(?:jybx|nologin/content)/([A-Za-z0-9_]+\.html)", raw)
    if not match:
        return raw
    content_id = match.group(1)
    keyword = urllib.parse.quote(keyword_hint or "", safe="")
    base = f"https://www.jianyu360.cn/nologin/content/{content_id}"
    return f"{base}?kds={keyword}" if keyword else base


def build_nologin_content_url_from_id(content_id: Any, keyword_hint: str = "") -> str | None:
    text = str(content_id or "").strip()
    if not text:
        return None
    if not text.endswith(".html"):
        text = f"{text}.html"
    base = f"https://www.jianyu360.cn/nologin/content/{text}"
    keyword = urllib.parse.quote(keyword_hint or "", safe="")
    return f"{base}?kds={keyword}" if keyword else base


def extract_sid_from_html(html_source: str) -> str | None:
    if not html_source:
        return None
    match = re.search(r"\b(ABC[A-Za-z0-9%+/=]+)\b", html_source)
    if not match:
        return None
    return html.unescape(match.group(1))


def extract_original_url(html_source: str) -> str | None:
    text = html_to_text(html_source or "")
    text = normalize_loose_text(text)
    current_detail_url = extract_detail_url_from_html(html_source)

    def unwrap_original_candidate(value: str) -> str | None:
        raw = html.unescape(value).strip()
        decoded = urllib.parse.unquote(raw)
        parsed = urllib.parse.urlparse(decoded)
        query = urllib.parse.parse_qs(parsed.query)
        if "backTo" in query and query["backTo"]:
            decoded = urllib.parse.unquote(query["backTo"][0]).strip()
        if not decoded.startswith("http"):
            return None
        if current_detail_url and normalize_loose_text(decoded) == normalize_loose_text(current_detail_url):
            return None
        return decoded

    direct_match = re.search(r"原文链接地址[:：]?\s*(https?://\S+)", text, flags=re.I)
    if direct_match:
        value = direct_match.group(1).strip("；;，,。)>】] ")
        return unwrap_original_candidate(value)
    patterns = (
        r"link-href=['\"]https://www\.jianyu360\.cn/notin/page\?backTo=([^'\"&]+)",
        r"link-href=['\"]([^'\"]+)['\"]",
    )
    for pattern in patterns:
        match = re.search(pattern, html_source, re.IGNORECASE)
        if not match:
            continue
        candidate = unwrap_original_candidate(match.group(1))
        if candidate:
            return candidate
    return None


def extract_attachment_links(html_source: str) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for href, inner in re.findall(r"<a[^>]+href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>", html_source or "", flags=re.I | re.S):
        text = strip_html_fragment(inner)
        if not text:
            continue
        href_value = html.unescape(href).strip()
        if not href_value or href_value.lower().startswith("javascript:"):
            continue
        if not (
            "附件" in text
            or "下载" in text
            or re.search(r"\.(?:pdf|doc|docx|xls|xlsx|zip|rar)$", href_value, flags=re.I)
            or re.search(r"\.(?:pdf|doc|docx|xls|xlsx|zip|rar)$", text, flags=re.I)
        ):
            continue
        normalized_url = href_value if href_value.startswith("http") else urllib.parse.urljoin("https://xizang.jianyu360.cn", href_value)
        key = (text, normalized_url)
        if key in seen:
            continue
        seen.add(key)
        results.append({"name": text, "url": normalized_url})
    return results


def extract_related_links(html_source: str) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for href, title, inner in re.findall(
        r'<a[^>]+class=["\'][^"\']*bid-list-link[^"\']*["\'][^>]+href=["\']([^"\']+)["\'][^>]*(?:title=["\']([^"\']+)["\'])?[^>]*>(.*?)</a>',
        html_source or "",
        flags=re.I | re.S,
    ):
        href_value = html.unescape(href).strip()
        if not href_value:
            continue
        normalized_url = href_value if href_value.startswith("http") else urllib.parse.urljoin("https://xizang.jianyu360.cn", href_value)
        if normalized_url in seen:
            continue
        seen.add(normalized_url)
        text = html.unescape(title or strip_html_fragment(inner) or "").strip()
        if not text:
            continue
        results.append({"title": text, "url": normalized_url})
    return results


def parse_detail_html(html_source: str) -> dict[str, Any]:
    title = extract_title_from_html(html_source) or ""
    meta_desc = extract_meta_content(html_source, "Description") or ""
    main_html = extract_detail_section(html_source)
    text = html_to_text(main_html)
    combined = f"{title}\n{meta_desc}\n{text}"
    label_map = extract_label_value_map(combined)
    table_map = extract_tab_pairs(main_html)
    merged_map = {**label_map, **table_map}
    attachments = extract_attachment_names(main_html)
    attachment_links = extract_attachment_links(main_html)
    original_url = extract_original_url(html_source)
    related_links = extract_related_links(html_source)
    notice_type = classify_notice_from_text(title, "", combined)
    publish_time = extract_publish_date_from_html(html_source)
    open_record_budget = None
    if notice_type == "开标记录":
        control_match = re.search(r"控制价\s*[（(]?(?:万元|元)?[)）]?\s*</th>\s*<td>\s*([0-9,]+(?:\.\d+)?)", main_html, re.IGNORECASE)
        if control_match:
            open_record_budget = parse_number(control_match.group(1))
    project_name = (
        extract_by_alias_map(table_map, FIELD_ALIASES["project_name"])
        or extract_by_alias_map(label_map, FIELD_ALIASES["project_name"])
        or extract_following_line_value(combined, FIELD_ALIASES["project_name"])
        or extract_inline_value(combined, FIELD_ALIASES["project_name"])
    )
    budget_value = extract_by_alias_map(table_map, VALUE_FIELD_ALIASES["budget"])
    bid_amount_value = extract_by_alias_map(table_map, VALUE_FIELD_ALIASES["bid_amount"])
    budget = None
    budget_source = "missing"
    if budget_value:
        budget = normalize_money_value(budget_value, budget_value or "")
        if budget is not None:
            budget_source = "table_field"
    if budget is None:
        alias_budget = extract_number_after_alias(combined, VALUE_FIELD_ALIASES["budget"])
        if alias_budget is not None:
            budget = alias_budget
            budget_source = "label_value_field"
    if budget is None:
        near_budget = extract_number_near_alias(combined, VALUE_FIELD_ALIASES["budget"])
        if near_budget is not None:
            budget = near_budget
            budget_source = "body_regex"
    bid_amount = None
    bid_amount_source = "missing"
    result_amount = extract_result_amount(combined)
    if result_amount is not None:
        bid_amount = result_amount
        bid_amount_source = "label_value_field"
    elif bid_amount_value:
        bid_amount = normalize_money_value(bid_amount_value, bid_amount_value or "")
        if bid_amount is not None:
            bid_amount_source = "table_field"
    if bid_amount is None:
        alias_bid_amount = extract_number_after_alias(combined, VALUE_FIELD_ALIASES["bid_amount"])
        if alias_bid_amount is not None:
            bid_amount = alias_bid_amount
            bid_amount_source = "label_value_field"
    if bid_amount is None:
        near_bid_amount = extract_number_near_alias(combined, VALUE_FIELD_ALIASES["bid_amount"])
        if near_bid_amount is not None:
            bid_amount = near_bid_amount
            bid_amount_source = "body_regex"
    winner = pick_best_winner_candidate(
        extract_by_alias_map(table_map, VALUE_FIELD_ALIASES["winner"])
        ,
        extract_by_alias_map(label_map, VALUE_FIELD_ALIASES["winner"]),
        extract_inline_value(combined, VALUE_FIELD_ALIASES["winner"]),
        extract_inline_value(combined, ("第一中标候选人", "推荐第一中标候选人")),
    )
    winner_source = "missing"
    if clean_entity_name(extract_by_alias_map(table_map, VALUE_FIELD_ALIASES["winner"])):
        winner_source = "table_field"
    elif clean_entity_name(extract_by_alias_map(label_map, VALUE_FIELD_ALIASES["winner"])):
        winner_source = "label_value_field"
    elif clean_entity_name(extract_inline_value(combined, VALUE_FIELD_ALIASES["winner"])) or clean_entity_name(extract_inline_value(combined, ("第一中标候选人", "推荐第一中标候选人"))):
        winner_source = "body_regex"
    buyer = clean_org_name(
        extract_by_alias_map(table_map, FIELD_ALIASES["buyer"])
        or extract_by_alias_map(label_map, FIELD_ALIASES["buyer"])
        or extract_following_line_value(combined, FIELD_ALIASES["buyer"])
        or extract_inline_value(combined, FIELD_ALIASES["buyer"])
    )
    city = normalize_city_name(
        extract_by_alias_map(table_map, FIELD_ALIASES["city"])
        or extract_by_alias_map(label_map, FIELD_ALIASES["city"])
        or extract_inline_value(combined, ("所属地区", "地区", "地市"))
    )
    district = normalize_region_text(
        extract_by_alias_map(table_map, FIELD_ALIASES["district"])
        or extract_by_alias_map(label_map, FIELD_ALIASES["district"])
        or extract_inline_value(combined, FIELD_ALIASES["district"])
    )
    record = {
        "title": title,
        "detail": combined,
        "label_map": merged_map,
        "project_name": clean_project_name(project_name) if looks_like_project_name(project_name) else None,
        "bid_number": extract_bid_number(combined),
        "buyer": buyer,
        "city": city,
        "district": district,
        "publish_time": publish_time,
        "budget": budget,
        "budget_source": budget_source,
        "bid_amount": bid_amount,
        "bid_amount_source": bid_amount_source,
        "winner": winner,
        "winner_source": winner_source,
        "bid_quotes": [],
        "bid_participants": [],
        "notice_type": notice_type,
        "attachments": attachments,
        "attachment_links": attachment_links,
        "original_url": original_url,
        "related_links": related_links,
        "procurement_scope_key": extract_procurement_scope_key(title or project_name or ""),
        "bid_section_key": extract_bid_section_key(title or project_name or ""),
    }
    if notice_type == "开标记录":
        html_participants, html_budget = extract_open_record_participants_from_html(main_html)
        text_participants = extract_open_record_participants_from_text(combined)
        budget = (
            budget
            or html_budget
            or open_record_budget
            or extract_open_record_budget_from_text(combined)
            or extract_number_after_alias(combined, VALUE_FIELD_ALIASES["budget"])
            or extract_number_near_alias(combined, VALUE_FIELD_ALIASES["budget"], window=160)
        )
        bid_amount = None
        record["budget"] = budget
        record["budget_source"] = "open_record_field" if budget is not None else record.get("budget_source", "missing")
        record["bid_amount"] = None
        record["bid_amount_source"] = "missing"
        quote_source = {"notice_type": "开标记录", "raw": {"detail": combined}, "budget": parse_number(budget) if budget else None}
        if html_participants:
            record["bid_participants"] = html_participants
            record["bid_quotes"] = [float(item["quote"]) for item in html_participants if item.get("quote") is not None]
        elif text_participants:
            record["bid_participants"] = text_participants
            record["bid_quotes"] = [float(item["quote"]) for item in text_participants if item.get("quote") is not None]
        else:
            record["bid_quotes"] = extract_bid_quotes(quote_source)
            record["bid_participants"] = extract_bid_participants(
                {"notice_type": "开标记录", "raw": {"detail": combined}, "bid_quotes": record["bid_quotes"]}
            )
    return record


def extract_attachment_names(html_source: str) -> list[str]:
    text = html_to_text(html_source or "")
    match = re.search(r"附件信息[:：]?\s*(.+)", text, flags=re.S)
    if not match:
        return []
    tail = match.group(1)
    names = re.findall(r"([^\s<>]+?\.(?:pdf|doc|docx|xls|xlsx|zip|rar))", tail, flags=re.I)
    deduped: list[str] = []
    seen: set[str] = set()
    for name in names:
        cleaned = name.strip("；;，,。")
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        deduped.append(cleaned)
    return deduped


def extract_open_record_participants_from_html(html_source: str) -> tuple[list[dict[str, Any]], float | None]:
    section = extract_open_record_table_html(html_source)
    if not section:
        return [], None
    header_match = re.search(r"<thead>(.*?)</thead>", section, flags=re.IGNORECASE | re.DOTALL)
    body_match = re.search(r"<tbody>(.*?)</tbody>", section, flags=re.IGNORECASE | re.DOTALL)
    if not header_match or not body_match:
        return [], None
    header_cells = re.findall(r"<th[^>]*>(.*?)</th>", header_match.group(1), flags=re.IGNORECASE | re.DOTALL)
    headers = [strip_html_fragment(cell) for cell in header_cells]
    if not headers:
        return [], None
    company_idx = next((i for i, value in enumerate(headers) if any(alias in value for alias in COMPANY_TABLE_ALIASES)), -1)
    quote_idx = next((i for i, value in enumerate(headers) if any(alias in value for alias in QUOTE_TABLE_ALIASES)), -1)
    budget_idx = next((i for i, value in enumerate(headers) if any(alias in value for alias in BUDGET_TABLE_ALIASES)), -1)
    if company_idx < 0 or quote_idx < 0:
        return [], None
    participants: list[dict[str, Any]] = []
    budget_candidates: list[float] = []
    row_html_list = re.findall(r"<tr[^>]*>(.*?)</tr>", body_match.group(1), flags=re.IGNORECASE | re.DOTALL)
    for row_html in row_html_list:
        cell_html = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, flags=re.IGNORECASE | re.DOTALL)
        if not cell_html:
            continue
        cells = [strip_html_fragment(cell) for cell in cell_html]
        if max(company_idx, quote_idx, budget_idx) >= len(cells):
            continue
        name = clean_entity_name(cells[company_idx]) or clean_entity_name(re.sub(r"<[^>]+>", "", cell_html[company_idx]))
        quote = normalize_money_value(cells[quote_idx], headers[quote_idx])
        if not name or not looks_like_valid_money(quote):
            continue
        participants.append(
            {
                "rank": len(participants) + 1,
                "name": name,
                "quote": float(quote),
            }
        )
        if budget_idx >= 0 and budget_idx < len(cells):
            raw_budget = parse_number(cells[budget_idx])
            budget_value = raw_budget
            if raw_budget is not None:
                # The site sometimes labels this column as "控制价(万元)" while the cell stores yuan.
                if "万元" in headers[budget_idx] and raw_budget < 100000.0:
                    budget_value = raw_budget * 10000.0
            if looks_like_valid_money(budget_value):
                budget_candidates.append(float(budget_value))
    budget = None
    if budget_candidates:
        rounded_counts = defaultdict(int)
        for value in budget_candidates:
            rounded_counts[round(value, 2)] += 1
        budget = max(rounded_counts.items(), key=lambda item: (item[1], item[0]))[0]
    return participants, budget


def looks_like_detail_html(html_source: str) -> bool:
    return any(
        marker in html_source
        for marker in (
            'class="detail-title"',
            'class="detail-summary"',
            'class="base-info"',
            'class="detail-html"',
        )
    )


def is_detail_like_title(title: str) -> bool:
    raw = str(title or "").strip()
    if not raw:
        return False
    normalized = normalize_loose_text(raw)
    negative_markers = (
        "验证码",
        "404",
        "招标信息_",
        "招标公告-第1页",
        "中标信息_",
        "中标公告-第1页",
        "首页",
    )
    if any(marker in normalized for marker in negative_markers):
        return False
    if normalized.startswith("西藏") and "第1页" in normalized:
        return False
    if normalized.startswith("林芝市招标信息") or normalized.startswith("西藏建筑工程招标信息"):
        return False
    return True


def is_detail_record_candidate(parsed: dict[str, Any], html_source: str) -> bool:
    title = str(parsed.get("title") or "").strip()
    if not is_detail_like_title(title):
        return False
    detail_text = str(parsed.get("detail") or "").strip()
    project_name = str(parsed.get("project_name") or "").strip()
    bid_number = str(parsed.get("bid_number") or "").strip()
    notice_type = str(parsed.get("notice_type") or "").strip()
    if project_name or bid_number:
        return True
    if notice_type in {"开标记录", "中标结果", "中标候选人"}:
        return True
    if detail_text and any(token in detail_text for token in ("项目名称", "项目编号", "招标编号", "中标", "投标人名称", "控制价")):
        return True
    if 'class="detail-content"' in html_source or "class='detail-content'" in html_source:
        return True
    return False


def load_record_from_detail_file(path: str, config: SearchConfig) -> dict[str, Any] | None:
    html_source = Path(path).read_text(encoding="utf-8", errors="ignore")
    if not looks_like_detail_html(html_source):
        return None
    parsed = parse_detail_html(html_source)
    if not is_detail_record_candidate(parsed, html_source):
        return None
    title = str(parsed.get("project_name") or parsed.get("title") or Path(path).stem)
    detail_url = extract_detail_url_from_html(html_source)
    raw = {
        "id": f"file-{hashlib.md5(str(path).encode('utf-8')).hexdigest()[:12]}",
        "url": detail_url or str(Path(path).resolve()),
        "title": parsed.get("title") or title,
        "source_url": detail_url or str(Path(path).resolve()),
        "detail": parsed.get("detail") or "",
        "source_file": str(Path(path).resolve()),
        "html_title": extract_title_from_html(html_source) or "",
    }
    record = {
        "id": raw["id"],
        "title": title,
        "project_key": normalize_project_key(title),
        "notice_type": str(parsed.get("notice_type") or "其他"),
        "subtype": "",
        "area": config.province,
        "city": None,
        "district": None,
        "buyer": clean_entity_name(parsed.get("buyer")),
        "winner": clean_entity_name(parsed.get("winner")),
        "budget": parsed.get("budget"),
        "bid_amount": parsed.get("bid_amount"),
        "publish_time": parsed.get("publish_time"),
        "bid_open_time": None,
        "industry": config.industry,
        "site": None,
        "spider_code": None,
        "masked": False,
        "money_candidates": [],
        "label_values": dict(parsed.get("label_map") or {}),
        "project_name": clean_project_name(parsed.get("project_name") or title) if (parsed.get("project_name") or title) else None,
        "bid_number": str(parsed.get("bid_number") or "").strip() or None,
        "raw": raw,
        "bid_quotes": list(parsed.get("bid_quotes") or []),
        "bid_participants": list(parsed.get("bid_participants") or []),
        "attachments": list(parsed.get("attachments") or []),
        "attachment_links": list(parsed.get("attachment_links") or []),
        "original_url": parsed.get("original_url"),
        "related_links": list(parsed.get("related_links") or []),
        "raw_detail_html": html_source,
        "raw_detail_text": str(parsed.get("detail") or ""),
        "raw_detail_url": detail_url or None,
    }
    return reparse_record_from_detail(record)


def extract_bid_number(text: str) -> str | None:
    match = re.search(r"(?:招标编号|招标项目编号|项目编号)[:：]?\s*([A-Za-z0-9\-_/]+)", html.unescape(text or ""))
    if match:
        return match.group(1).strip()
    return None


@dataclass(frozen=True)
class SearchConfig:
    keywords: str
    province: str = "西藏"
    industry: str = "建筑工程"
    publish_range: str = ""
    page_size: int = 50
    max_pages: int = 20
    cookie: str = ""
    cookie_file: str = ""
    output: str = "jianyu_projects.json"
    input_json: str = ""
    input_md: str = ""
    input_html: str = ""
    input_dir: str = ""
    input_dir_batch: bool = False
    fetch_details: bool = False
    input_urls_json: str = ""
    report_md: str = ""
    customer_json: str = ""
    discover_url: str = ""
    detail_limit: int = 0
    backfill_pages: int = 0
    discover_channels: str = ""
    backfill_discover_channels: str = ""
    captcha_clicks: str = ""
    captcha_image_out: str = ""
    probe_search_captcha: bool = False
    probe_html_captcha_url: str = ""
    captcha_auto_attempts: int = 0
    source_mode: str = "auto"
    allow_core_without_notice: bool = True
    detail_verify_backfill_limit: int = 120
    search_backfill_mode: str = "auto"
    precise_project_query: str = ""


LAST_FETCH_META: dict[str, Any] = {
    "anti_verify": False,
    "anti_verify_text": None,
    "source_mode": None,
    "backfill_direct_matches": 0,
    "backfill_coarse_candidates": 0,
    "backfill_detail_verified": 0,
    "backfill_search_skipped": 0,
    "targeted_backfill_projects": 0,
    "targeted_backfill_missing_open_projects": 0,
    "targeted_backfill_recovered_open_records": 0,
    "targeted_backfill_recovered_notice_records": 0,
    "targeted_backfill_recovered_result_records": 0,
    "followup_seed_generated": 0,
    "followup_seed_related_link_generated": 0,
    "followup_seed_original_url_generated": 0,
    "followup_verified_kept": 0,
    "followup_filtered_out": 0,
}

COOKIE_PROVIDER: Callable[[], str] | None = None
PROGRESS_CALLBACK: Callable[[dict[str, Any]], None] | None = None
STOP_REQUESTED: Callable[[], bool] | None = None
BROWSER_REQUEST_PROVIDER: Callable[[str, str, Any | None, dict[str, str] | None, str], Any] | None = None
BROWSER_RENDERED_PROVIDER: Callable[[str], dict[str, Any]] | None = None
MANUAL_CAPTCHA_HANDLER: Callable[[dict[str, Any]], bool] | None = None


def emit_progress(stage: str, **payload: Any) -> None:
    message = {"stage": stage, **payload}
    if PROGRESS_CALLBACK is not None:
        try:
            PROGRESS_CALLBACK(message)
        except Exception:
            pass
    print(f"__PROGRESS__{json.dumps(message, ensure_ascii=False)}", flush=True)


def resolve_cookie(cookie: str) -> str:
    if COOKIE_PROVIDER is not None:
        try:
            value = COOKIE_PROVIDER()
            if value and value.strip():
                return value.strip()
        except Exception:
            pass
    return cookie


def browser_request(method: str, url: str, payload: Any | None, headers: dict[str, str] | None, expect: str) -> Any | None:
    if BROWSER_REQUEST_PROVIDER is None:
        return None
    try:
        return BROWSER_REQUEST_PROVIDER(method, url, payload, headers, expect)
    except Exception:
        return None


class ManualCaptchaRequired(RuntimeError):
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        super().__init__(payload.get("message") or "manual captcha required")


def handle_manual_captcha(payload: dict[str, Any]) -> bool:
    emit_progress("captcha_manual_required", **payload)
    if MANUAL_CAPTCHA_HANDLER is None:
        raise ManualCaptchaRequired(payload)
    ok = bool(MANUAL_CAPTCHA_HANDLER(payload))
    if ok:
        emit_progress("captcha_manual_resolved", **payload)
        return True
    raise ManualCaptchaRequired(payload)


def abort_if_requested() -> None:
    if STOP_REQUESTED is not None:
        try:
            if STOP_REQUESTED():
                raise RuntimeError("collection stopped")
        except RuntimeError:
            raise
        except Exception:
            pass


DETAIL_FETCH_STATE: dict[str, Any] = {
    "last_fetch_ts": 0.0,
    "consecutive_captcha": 0,
    "cooldown_until": 0.0,
}

SEARCH_FETCH_STATE: dict[str, Any] = {
    "last_fetch_ts": 0.0,
    "consecutive_captcha": 0,
    "cooldown_until": 0.0,
}

FORM_FETCH_STATE: dict[str, Any] = {
    "last_fetch_ts": 0.0,
    "cooldown_until": 0.0,
}


CAPTCHA_FONT_CANDIDATES = [
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
]


def extract_captcha_meta_from_search_response(data: dict[str, Any]) -> dict[str, Any] | None:
    img_data = data.get("imgData")
    text_verify = data.get("textVerify")
    if not img_data and not text_verify:
        return None
    return {
        "img_data": img_data,
        "text_verify": text_verify,
    }


def extract_captcha_meta_from_html(html_text: str) -> dict[str, Any] | None:
    if not html_text:
        return None
    anti_match = re.search(r'["\']?antiVerify["\']?\s*:\s*(-?\d+)', html_text)
    img_match = re.search(r'["\']?imgData["\']?\s*:\s*["\']([^"\']+)["\']', html_text)
    text_match = re.search(r'["\']?textVerify["\']?\s*:\s*["\']([^"\']+)["\']', html_text)
    dom_text_match = re.search(r"请在下图依次点击：\s*<span>([^<]+)</span>", html_text)
    dom_img_match = re.search(r'<img[^>]+id=["\']antiimg["\'][^>]+src=["\']([^"\']+)["\']', html_text)
    if not anti_match and not img_match and not text_match:
        if not dom_text_match and not dom_img_match and 'id="antiVerify"' not in html_text and "id='antiVerify'" not in html_text:
            return None
    img_data = html.unescape(img_match.group(1)) if img_match else None
    if not img_data and dom_img_match:
        img_data = html.unescape(dom_img_match.group(1))
    text_verify = html.unescape(text_match.group(1)) if text_match else None
    if not text_verify and dom_text_match:
        text_verify = html.unescape(dom_text_match.group(1))
    return {
        "anti_verify": int(anti_match.group(1)) if anti_match else None,
        "img_data": img_data,
        "text_verify": text_verify,
        "source": "html_dom" if (dom_text_match or dom_img_match) and not (img_match or text_match) else "html_json",
    }


def write_captcha_image_if_needed(config: SearchConfig, captcha_meta: dict[str, Any]) -> None:
    if not config.captcha_image_out:
        return
    img_data = str(captcha_meta.get("img_data") or "")
    if not img_data:
        return
    raw = img_data
    if "," in raw:
        raw = raw.split(",", 1)[1]
    try:
        image_bytes = base64.b64decode(raw)
    except Exception:
        return
    Path(config.captcha_image_out).write_bytes(image_bytes)


def submit_search_captcha(cookie: str, clicks: str) -> str:
    return post_form(
        "https://www.jianyu360.cn/jylab/supsearch/index.html",
        {
            "antiVerifyCheck": clicks,
            "imgw": "296",
        },
        cookie,
        extra_headers={"app": "jyseo"},
    )


def submit_html_captcha(url: str, cookie: str, clicks: str) -> str:
    return post_form(
        url,
        {
            "antiVerifyCheck": clicks,
            "imgw": "296",
        },
        cookie,
        extra_headers={
            "app": "jyseo",
            "referer": url,
            "origin": "https://www.jianyu360.cn",
            "x-requested-with": "XMLHttpRequest",
            "accept": "application/json, text/javascript, */*; q=0.01",
        },
    )


def fetch_html_with_headers(url: str, cookie: str, extra_headers: dict[str, str] | None = None) -> str:
    apply_detail_rate_limit()
    headers = {
        "cookie": cookie,
        "user-agent": "Mozilla/5.0",
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def load_captcha_font(size: int = 34) -> ImageFont.FreeTypeFont | None:
    for path in CAPTCHA_FONT_CANDIDATES:
        try:
            return ImageFont.truetype(path, size)
        except Exception:
            continue
    return None


def extract_captcha_candidate_boxes(image_path: str) -> list[dict[str, int]]:
    img = np.array(Image.open(image_path).convert("RGB"))
    pixels = img.reshape(-1, 3).astype("float32")
    kmeans = KMeans(n_clusters=8, random_state=0, n_init=10).fit(pixels)
    labels = kmeans.labels_.reshape(img.shape[:2])
    centers = kmeans.cluster_centers_
    order = np.argsort(centers.mean(axis=1))
    raw_boxes: list[list[int]] = []
    for cluster_idx in order[:5]:
        mask = (labels == cluster_idx).astype("uint8") * 255
        mask = cv2.medianBlur(mask, 3)
        num, _, stats, _ = cv2.connectedComponentsWithStats(mask, 8)
        for idx in range(1, num):
            x, y, w, h, area = [int(v) for v in stats[idx]]
            if area < 20 or w < 4 or h < 6:
                continue
            if w > 100 or h > 90:
                continue
            raw_boxes.append([x, y, w, h, area])
    raw_boxes.sort(key=lambda item: item[0])
    merged: list[list[int]] = []
    for box in raw_boxes:
        x, y, w, h, area = box
        if not merged:
            merged.append(box)
            continue
        px, py, pw, ph, parea = merged[-1]
        if x <= px + pw and y <= py + ph and py <= y + h:
            nx = min(px, x)
            ny = min(py, y)
            nr = max(px + pw, x + w)
            nb = max(py + ph, y + h)
            merged[-1] = [nx, ny, nr - nx, nb - ny, parea + area]
        else:
            merged.append(box)

    def split_wide_box(box: list[int]) -> list[list[int]]:
        x, y, w, h, area = box
        if w < 40:
            return [box]
        gray = cv2.cvtColor(img[y:y+h, x:x+w], cv2.COLOR_RGB2GRAY)
        edge = cv2.Canny(gray, 60, 180)
        col = edge.sum(axis=0)
        valleys = []
        for i in range(2, len(col) - 2):
            if col[i] <= col[i - 1] and col[i] <= col[i + 1] and col[i] < np.percentile(col, 30):
                valleys.append(i)
        split_points = [p for p in valleys if 8 < p < w - 8]
        if not split_points:
            return [box]
        cut_points = [0]
        for p in split_points:
            if p - cut_points[-1] >= 12:
                cut_points.append(p)
        cut_points.append(w)
        result: list[list[int]] = []
        for left, right in zip(cut_points, cut_points[1:]):
            if right - left < 8:
                continue
            sub = edge[:, left:right]
            ys, xs = np.where(sub > 0)
            if len(xs) < 20:
                continue
            sx = x + left + int(xs.min())
            sy = y + int(ys.min())
            sw = int(xs.max() - xs.min() + 1)
            sh = int(ys.max() - ys.min() + 1)
            result.append([sx, sy, sw, sh, int(len(xs))])
        return result or [box]

    final_boxes: list[dict[str, int]] = []
    for box in merged:
        for sub in split_wide_box(box):
            x, y, w, h, area = sub
            if area < 20 or w < 4 or h < 6:
                continue
            final_boxes.append(
                {
                    "x": x,
                    "y": y,
                    "w": w,
                    "h": h,
                    "area": area,
                    "cx": int(x + w / 2),
                    "cy": int(y + h / 2),
                }
            )
    final_boxes.sort(key=lambda item: (item["cx"], item["cy"]))
    return final_boxes


def render_char_mask(char: str, width: int, height: int, font: ImageFont.FreeTypeFont) -> np.ndarray:
    canvas = Image.new("L", (width, height), 0)
    draw = ImageDraw.Draw(canvas)
    bbox = draw.textbbox((0, 0), char, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = max(0, (width - tw) // 2 - bbox[0])
    y = max(0, (height - th) // 2 - bbox[1])
    draw.text((x, y), char, fill=255, font=font)
    return (np.array(canvas) > 32).astype("uint8")


def crop_box_mask(image_path: str, box: dict[str, int]) -> np.ndarray:
    img = np.array(Image.open(image_path).convert("RGB"))
    crop = img[box["y"]: box["y"] + box["h"], box["x"]: box["x"] + box["w"]]
    hsv = cv2.cvtColor(crop, cv2.COLOR_RGB2HSV)
    mask = ((hsv[:, :, 1] > 70) & (hsv[:, :, 2] < 235)).astype("uint8")
    return mask


def score_char_to_box(image_path: str, box: dict[str, int], char: str, font: ImageFont.FreeTypeFont) -> float:
    box_mask = crop_box_mask(image_path, box)
    char_mask = render_char_mask(char, box["w"], box["h"], font)
    if box_mask.shape != char_mask.shape:
        return -1e9
    overlap = np.logical_and(box_mask, char_mask).sum()
    union = np.logical_or(box_mask, char_mask).sum()
    iou = overlap / union if union else 0.0
    density_gap = abs(float(box_mask.mean()) - float(char_mask.mean()))
    return iou - density_gap * 0.3


def infer_captcha_clicks(image_path: str, text_verify: str) -> str | None:
    if not text_verify:
        return None
    font = load_captcha_font()
    if font is None:
        return None
    boxes = extract_captcha_candidate_boxes(image_path)
    if not boxes:
        return None
    chosen: list[dict[str, int]] = []
    used: set[int] = set()
    for char in text_verify:
        scored = [
            (score_char_to_box(image_path, box, char, font), idx, box)
            for idx, box in enumerate(boxes)
            if idx not in used
        ]
        if not scored:
            return None
        scored.sort(key=lambda item: item[0], reverse=True)
        _, idx, box = scored[0]
        used.add(idx)
        chosen.append(box)
    chosen.sort(key=lambda item: item["cx"])
    return ";".join(f"{item['cx']},{item['cy']}" for item in chosen)


def infer_captcha_click_candidates(image_path: str, text_verify: str, limit: int = 5) -> list[str]:
    if not text_verify:
        return []
    boxes = extract_captcha_candidate_boxes(image_path)
    if not boxes:
        return []
    # fallback heuristic: pick left-to-right boxes with reasonable size combinations
    boxes = [b for b in boxes if b["w"] >= 5 and b["h"] >= 8]
    boxes.sort(key=lambda item: item["cx"])
    if len(boxes) < len(text_verify):
        return []
    candidates: list[tuple[float, str]] = []
    if len(boxes) == len(text_verify):
        coords = ";".join(f"{b['cx']},{b['cy']}" for b in boxes)
        return [coords]
    from itertools import combinations
    for combo in combinations(boxes, len(text_verify)):
        xs = [b["cx"] for b in combo]
        widths = [b["w"] for b in combo]
        # prefer left-to-right spread and reasonable average width
        spread = xs[-1] - xs[0]
        width_score = sum(min(w, 40) for w in widths) / len(widths)
        score = spread + width_score * 0.5
        coords = ";".join(f"{b['cx']},{b['cy']}" for b in combo)
        candidates.append((score, coords))
    candidates.sort(key=lambda item: item[0], reverse=True)
    ordered: list[str] = []
    seen: set[str] = set()
    for _, coords in candidates:
        if coords in seen:
            continue
        seen.add(coords)
        ordered.append(coords)
        if len(ordered) >= limit:
            break
    return ordered


def infer_captcha_click_candidates_template(image_path: str, text_verify: str, limit: int = 5) -> list[str]:
    if not text_verify:
        return []
    img = np.array(Image.open(image_path).convert("RGB"))
    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    edge = cv2.Canny(gray, 60, 180)
    fonts = [path for path in CAPTCHA_FONT_CANDIDATES if Path(path).exists()]
    if not fonts:
        return []
    per_char_points: list[list[tuple[int, int]]] = []
    for ch in text_verify:
        hits: list[tuple[float, int, int]] = []
        for font_path in fonts:
            for size in range(24, 41, 2):
                try:
                    font = ImageFont.truetype(font_path, size)
                except Exception:
                    continue
                dummy = Image.new("L", (80, 80), 0)
                draw = ImageDraw.Draw(dummy)
                bbox = draw.textbbox((0, 0), ch, font=font)
                tw = bbox[2] - bbox[0] + 8
                th = bbox[3] - bbox[1] + 8
                if tw >= img.shape[1] or th >= img.shape[0]:
                    continue
                canvas = Image.new("L", (tw, th), 0)
                draw = ImageDraw.Draw(canvas)
                draw.text((4 - bbox[0], 4 - bbox[1]), ch, fill=255, font=font)
                templ = (np.array(canvas) > 20).astype("uint8") * 255
                templ_edge = cv2.Canny(templ, 50, 150)
                if templ_edge.sum() == 0:
                    continue
                res = cv2.matchTemplate(edge, templ_edge, cv2.TM_CCOEFF_NORMED)
                _, maxv, _, maxloc = cv2.minMaxLoc(res)
                cx = int(maxloc[0] + tw / 2)
                cy = int(maxloc[1] + th / 2)
                hits.append((float(maxv), cx, cy))
        hits.sort(key=lambda item: item[0], reverse=True)
        coords: list[tuple[int, int]] = []
        seen: set[tuple[int, int]] = set()
        for _, cx, cy in hits:
            key = (cx, cy)
            if any(abs(cx - sx) <= 12 and abs(cy - sy) <= 12 for sx, sy in seen):
                continue
            seen.add(key)
            coords.append(key)
            if len(coords) >= 4:
                break
        if not coords:
            return []
        per_char_points.append(coords)
    from itertools import product
    candidates: list[tuple[float, str]] = []
    for combo in product(*per_char_points):
        xs = [p[0] for p in combo]
        ys = [p[1] for p in combo]
        spread = max(xs) - min(xs)
        if spread < 40:
            continue
        penalty = sum(abs(a - b) < 8 for i, a in enumerate(xs) for b in xs[i + 1:])
        score = spread - penalty * 20 - np.std(ys) * 0.2
        coords = ";".join(f"{x},{y}" for x, y in combo)
        candidates.append((float(score), coords))
    candidates.sort(key=lambda item: item[0], reverse=True)
    ordered: list[str] = []
    seen_coords: set[str] = set()
    for _, coords in candidates:
        if coords in seen_coords:
            continue
        seen_coords.add(coords)
        ordered.append(coords)
        if len(ordered) >= limit:
            break
    return ordered


def auto_attempt_search_captcha(cookie: str, config: SearchConfig) -> dict[str, Any]:
    data = fetch_page_with_fallback(cookie, dataclasses.replace(config, industry=""), 1)
    for _ in range(max(0, config.captcha_auto_attempts)):
        captcha_meta = extract_captcha_meta_from_search_response(data) or {}
        write_captcha_image_if_needed(config, captcha_meta)
        if not captcha_meta or not config.captcha_image_out or not Path(config.captcha_image_out).exists():
            break
        candidates = infer_captcha_click_candidates(config.captcha_image_out, str(captcha_meta.get("text_verify") or ""), limit=5)
        unlocked = False
        for clicks in candidates:
            submit_search_captcha(cookie, clicks)
            data = fetch_page_with_fallback(cookie, dataclasses.replace(config, industry=""), 1)
            if (data.get("data") or {}).get("list"):
                unlocked = True
                break
        if unlocked:
            return data
    return data


def probe_html_captcha(url: str, cookie: str, config: SearchConfig) -> dict[str, Any]:
    html_text = ""
    captcha_meta: dict[str, Any] = {}
    for _ in range(3):
        html_text = fetch_html_with_headers(url, cookie)
        captcha_meta = extract_captcha_meta_from_html(html_text) or {}
        if captcha_meta.get("img_data") or captcha_meta.get("text_verify") or captcha_meta.get("anti_verify") is not None:
            break
        time.sleep(0.3)
    write_captcha_image_if_needed(config, captcha_meta)
    suggested_clicks = None
    suggested_click_candidates: list[str] = []
    suggested_click_candidates_template: list[str] = []
    if config.captcha_image_out and Path(config.captcha_image_out).exists() and captcha_meta.get("text_verify"):
        text_verify = str(captcha_meta.get("text_verify") or "")
        suggested_clicks = infer_captcha_clicks(config.captcha_image_out, text_verify)
        suggested_click_candidates = infer_captcha_click_candidates(config.captcha_image_out, text_verify)
        suggested_click_candidates_template = infer_captcha_click_candidates_template(config.captcha_image_out, text_verify)
    return {
        "url": url,
        "captcha": {
            "has_captcha": bool(captcha_meta),
            "anti_verify": captcha_meta.get("anti_verify"),
            "text_verify": captcha_meta.get("text_verify"),
            "has_img_data": bool(captcha_meta.get("img_data")),
            "source": captcha_meta.get("source"),
            "html_contains_anti_verify": "antiVerify" in html_text,
            "html_contains_img_data": "imgData" in html_text,
            "html_contains_text_verify": "textVerify" in html_text,
            "image_written": bool(config.captcha_image_out and Path(config.captcha_image_out).exists()),
            "image_path": config.captcha_image_out or None,
            "suggested_clicks": suggested_clicks,
            "suggested_click_candidates": suggested_click_candidates,
            "suggested_click_candidates_template": suggested_click_candidates_template,
        },
    }


def auto_attempt_html_captcha(url: str, cookie: str, config: SearchConfig) -> dict[str, Any]:
    html_text = fetch_html_with_headers(url, cookie)
    attempt_error = None
    for _ in range(max(0, config.captcha_auto_attempts)):
        captcha_meta = extract_captcha_meta_from_html(html_text) or {}
        write_captcha_image_if_needed(config, captcha_meta)
        if not captcha_meta or not config.captcha_image_out or not Path(config.captcha_image_out).exists():
            break
        text_verify = str(captcha_meta.get("text_verify") or "")
        if not text_verify:
            break
        candidates = infer_captcha_click_candidates(config.captcha_image_out, text_verify, limit=5)
        unlocked = False
        for clicks in candidates:
            try:
                submit_html_captcha(url, cookie, clicks)
                html_text = fetch_html_with_headers(url, cookie)
            except Exception as exc:
                attempt_error = str(exc)
                continue
            latest = extract_captcha_meta_from_html(html_text) or {}
            if not latest:
                unlocked = True
                break
        if unlocked:
            break
    final_meta = extract_captcha_meta_from_html(html_text) or {}
    write_captcha_image_if_needed(config, final_meta)
    return {
        "url": url,
        "captcha": {
            "has_captcha": bool(final_meta),
            "anti_verify": final_meta.get("anti_verify"),
            "text_verify": final_meta.get("text_verify"),
            "has_img_data": bool(final_meta.get("img_data")),
            "source": final_meta.get("source"),
            "html_contains_anti_verify": "antiVerify" in html_text,
            "html_contains_img_data": "imgData" in html_text,
            "html_contains_text_verify": "textVerify" in html_text,
            "image_written": bool(config.captcha_image_out and Path(config.captcha_image_out).exists()),
            "image_path": config.captcha_image_out or None,
            "search_unlocked": not bool(final_meta),
            "attempt_error": attempt_error,
        },
    }


def parse_args() -> SearchConfig:
    parser = argparse.ArgumentParser(description="Collect Jianyu360 search results.")
    parser.add_argument("--keywords", default="房建")
    parser.add_argument("--province", default="西藏")
    parser.add_argument("--industry", default="建筑工程")
    parser.add_argument("--publish-range", default="")
    parser.add_argument("--recent-days", type=int, default=DEFAULT_PUBLISH_RANGE_DAYS)
    parser.add_argument("--page-size", type=int, default=50)
    parser.add_argument("--max-pages", type=int, default=20)
    parser.add_argument("--cookie", default=os.environ.get("JY_COOKIE", ""))
    parser.add_argument("--cookie-file", default="")
    parser.add_argument("--output", default="jianyu_projects.json")
    parser.add_argument("--input-json", default="")
    parser.add_argument("--input-md", default="")
    parser.add_argument("--input-html", default="")
    parser.add_argument("--input-dir", default="")
    parser.add_argument("--input-dir-batch", action="store_true")
    parser.add_argument("--fetch-details", action="store_true")
    parser.add_argument("--input-urls-json", default="")
    parser.add_argument("--report-md", default="")
    parser.add_argument("--discover-url", default="")
    parser.add_argument("--detail-limit", type=int, default=0)
    parser.add_argument("--backfill-pages", type=int, default=80)
    parser.add_argument("--discover-channels", default="jzgc,fwcg,xzbg,rdaf,ylws,jxsb,nyhg,slsd")
    parser.add_argument("--backfill-discover-channels", default="")
    parser.add_argument("--captcha-clicks", default="")
    parser.add_argument("--captcha-image-out", default="")
    parser.add_argument("--probe-search-captcha", action="store_true")
    parser.add_argument("--probe-html-captcha-url", default="")
    parser.add_argument("--captcha-auto-attempts", type=int, default=0)
    parser.add_argument("--source-mode", choices=("auto", "search", "area_listing"), default="auto")
    parser.add_argument("--strict-three-files", action="store_true")
    parser.add_argument("--detail-verify-backfill-limit", type=int, default=120)
    parser.add_argument("--search-backfill-mode", choices=("auto", "on", "off"), default="auto")
    parser.add_argument("--precise-project-query", default="")
    args = parser.parse_args()
    return SearchConfig(
        keywords=args.keywords,
        province=args.province,
        industry=args.industry,
        publish_range=args.publish_range or build_publish_range(args.recent_days),
        page_size=args.page_size,
        max_pages=args.max_pages,
        cookie=args.cookie,
        cookie_file=args.cookie_file,
        output=args.output,
        input_json=args.input_json,
        input_md=args.input_md,
        input_html=args.input_html,
        input_dir=args.input_dir,
        input_dir_batch=args.input_dir_batch,
        fetch_details=args.fetch_details,
        input_urls_json=args.input_urls_json,
        report_md=args.report_md,
        customer_json="",
        discover_url=args.discover_url,
        detail_limit=args.detail_limit,
        backfill_pages=args.backfill_pages,
        discover_channels=args.discover_channels,
        backfill_discover_channels=args.backfill_discover_channels,
        captcha_clicks=args.captcha_clicks,
        captcha_image_out=args.captcha_image_out,
        probe_search_captcha=args.probe_search_captcha,
        probe_html_captcha_url=args.probe_html_captcha_url,
        captcha_auto_attempts=args.captcha_auto_attempts,
        source_mode=args.source_mode,
        allow_core_without_notice=not args.strict_three_files,
        detail_verify_backfill_limit=args.detail_verify_backfill_limit,
        search_backfill_mode=args.search_backfill_mode,
        precise_project_query=args.precise_project_query,
    )


def load_cookie(config: SearchConfig, required: bool = True) -> str:
    provider_cookie = resolve_cookie("")
    if provider_cookie:
        return provider_cookie
    if config.cookie.strip():
        return config.cookie.strip()
    if config.cookie_file:
        return Path(config.cookie_file).read_text(encoding="utf-8").strip()
    default_path = Path("/tmp/jianyu_cookie.txt")
    if default_path.exists():
        return default_path.read_text(encoding="utf-8").strip()
    if required:
        raise SystemExit("Missing cookie. Pass --cookie, --cookie-file, or set JY_COOKIE.")
    return ""


def post_json(url: str, payload: dict[str, Any], cookie: str) -> dict[str, Any]:
    abort_if_requested()
    apply_search_rate_limit()
    browser_result = browser_request("POST", url, payload, {"content-type": "application/json"}, "json")
    if isinstance(browser_result, dict):
        return browser_result
    cookie = resolve_cookie(cookie)
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "content-type": "application/json",
            "cookie": cookie,
            "user-agent": "Mozilla/5.0",
            "accept": "application/json, text/plain, */*",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def post_form(url: str, payload: dict[str, Any], cookie: str, extra_headers: dict[str, str] | None = None) -> str:
    abort_if_requested()
    apply_form_rate_limit()
    browser_result = browser_request(
        "POST",
        url,
        payload,
        {"content-type": "application/x-www-form-urlencoded; charset=UTF-8", **(extra_headers or {})},
        "text",
    )
    if isinstance(browser_result, str):
        return browser_result
    cookie = resolve_cookie(cookie)
    data = urllib.parse.urlencode(payload).encode("utf-8")
    headers = {
        "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        "cookie": cookie,
        "user-agent": "Mozilla/5.0",
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if extra_headers:
        headers.update(extra_headers)
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def post_form_json(url: str, payload: dict[str, Any], cookie: str, extra_headers: dict[str, str] | None = None) -> dict[str, Any]:
    text = post_form(url, payload, cookie, extra_headers=extra_headers)
    return json.loads(text or "{}")


def fetch_html(url: str, cookie: str) -> str:
    abort_if_requested()
    apply_detail_rate_limit()
    browser_result = browser_request("GET", url, None, None, "text")
    if isinstance(browser_result, str):
        return browser_result
    cookie = resolve_cookie(cookie)
    headers = {
        "user-agent": "Mozilla/5.0",
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    if cookie:
        headers["cookie"] = cookie
    request = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read().decode("utf-8", errors="replace")


def fetch_detail_preagent(url: str, cookie: str) -> dict[str, Any]:
    abort_if_requested()
    browser_result = browser_request(
        "GET",
        PREAGENT_URL,
        None,
        {
            "referer": url,
            "origin": "https://www.jianyu360.cn",
            "x-requested-with": "XMLHttpRequest",
            "accept": "application/json, text/plain, */*",
        },
        "json",
    )
    if isinstance(browser_result, dict):
        return browser_result
    cookie = resolve_cookie(cookie)
    headers = {
        "referer": url,
        "origin": "https://www.jianyu360.cn",
        "x-requested-with": "XMLHttpRequest",
        "accept": "application/json, text/plain, */*",
    }
    request = urllib.request.Request(PREAGENT_URL, headers={**headers, "cookie": cookie, "user-agent": "Mozilla/5.0"}, method="GET")
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8", errors="replace"))


def fetch_detail_baseinfo(url: str, sid: str, token: str, cookie: str) -> dict[str, Any]:
    return post_form_json(
        DETAIL_BASEINFO_URL,
        {"token": token},
        cookie,
        extra_headers={
            "referer": url,
            "origin": "https://www.jianyu360.cn",
            "x-requested-with": "XMLHttpRequest",
            "accept": "application/json, text/plain, */*",
        },
    )


def apply_rate_limit(state: dict[str, Any], min_gap_range: tuple[float, float]) -> None:
    now = time.time()
    cooldown_until = float(state.get("cooldown_until") or 0.0)
    if cooldown_until > now:
        time.sleep(cooldown_until - now)
        now = time.time()
    last_fetch_ts = float(state.get("last_fetch_ts") or 0.0)
    min_gap = random.uniform(*min_gap_range)
    wait_needed = min_gap - (now - last_fetch_ts)
    if wait_needed > 0:
        time.sleep(wait_needed)
    state["last_fetch_ts"] = time.time()


def register_captcha_hit(state: dict[str, Any], base_cooldown: float = 120.0, max_cooldown: float = 900.0) -> None:
    count = int(state.get("consecutive_captcha") or 0) + 1
    state["consecutive_captcha"] = count
    cooldown_seconds = min(max_cooldown, base_cooldown * count)
    state["cooldown_until"] = time.time() + cooldown_seconds


def clear_captcha_hits(state: dict[str, Any]) -> None:
    state["consecutive_captcha"] = 0
    state["cooldown_until"] = 0.0


def apply_search_rate_limit() -> None:
    apply_rate_limit(SEARCH_FETCH_STATE, (5.0, 12.0))


def apply_form_rate_limit() -> None:
    apply_rate_limit(FORM_FETCH_STATE, (6.0, 10.0))


def apply_detail_rate_limit() -> None:
    apply_rate_limit(DETAIL_FETCH_STATE, (8.0, 20.0))


def is_captcha_html(html_text: str) -> bool:
    if not html_text:
        return False
    return bool(extract_captcha_meta_from_html(html_text) or "id=\"antiVerify\"" in html_text or "id='antiVerify'" in html_text)


def register_detail_captcha_hit() -> None:
    register_captcha_hit(DETAIL_FETCH_STATE, base_cooldown=180.0, max_cooldown=1800.0)
    register_captcha_hit(SEARCH_FETCH_STATE, base_cooldown=120.0, max_cooldown=1200.0)
    FORM_FETCH_STATE["cooldown_until"] = max(
        float(FORM_FETCH_STATE.get("cooldown_until") or 0.0),
        float(DETAIL_FETCH_STATE.get("cooldown_until") or 0.0),
    )


def clear_detail_captcha_hits() -> None:
    clear_captcha_hits(DETAIL_FETCH_STATE)


def fetch_rendered_open_record_payload(url: str, cookie: str) -> dict[str, Any]:
    abort_if_requested()
    if BROWSER_RENDERED_PROVIDER is not None:
        try:
            payload = BROWSER_RENDERED_PROVIDER(url)
            if isinstance(payload, dict) and payload:
                return payload
        except Exception:
            pass
    cookie = resolve_cookie(cookie)
    browser = None
    with sync_playwright() as p:
        browser = None
        launch_candidates: list[dict[str, Any]] = []
        if PLAYWRIGHT_EXECUTABLE_CANDIDATES and Path(PLAYWRIGHT_EXECUTABLE_CANDIDATES[0]).exists():
            launch_candidates.append({"headless": True, "executable_path": PLAYWRIGHT_EXECUTABLE_CANDIDATES[0]})
        if sys.platform.startswith("win"):
            launch_candidates.extend(
                [
                    {"headless": True, "channel": "msedge"},
                    {"headless": True, "channel": "chrome"},
                    {"headless": True},
                ]
            )
        else:
            launch_candidates.extend(
                [
                    {"headless": True, "channel": "chrome"},
                    {"headless": True, "channel": "msedge"},
                    {"headless": True},
                ]
            )
        last_error: Exception | None = None
        for launch_kwargs in launch_candidates:
            try:
                browser = p.chromium.launch(**launch_kwargs)
                break
            except Exception as exc:
                last_error = exc
        if browser is None:
            raise RuntimeError(f"无法启动 Playwright 浏览器：{last_error}")
        context = browser.new_context()
        cookies = []
        for pair in [part.strip() for part in cookie.split(";") if "=" in part]:
            name, value = pair.split("=", 1)
            cookies.append(
                {
                    "name": name.strip(),
                    "value": value.strip(),
                    "domain": ".jianyu360.cn",
                    "path": "/",
                }
            )
        if cookies:
            context.add_cookies(cookies)
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.get_by_text("投标人名称").first.wait_for(state="visible", timeout=15000)
        except Exception:
            page.wait_for_timeout(4000)
        table_payload = page.evaluate(
            """() => {
                const tables = Array.from(document.querySelectorAll('table'));
                return tables.map((table) =>
                    Array.from(table.querySelectorAll('tr'))
                        .map((tr) =>
                            Array.from(tr.querySelectorAll('th,td'))
                                .map((td) => (td.innerText || '').trim())
                                .filter(Boolean)
                        )
                        .filter((row) => row.length > 0)
                );
            }"""
        )
        chunks = []
        for table in table_payload or []:
            if not isinstance(table, list):
                continue
            for row in table:
                if not isinstance(row, list):
                    continue
                line = "\t".join(str(cell).strip() for cell in row if str(cell).strip())
                if line:
                    chunks.append(line)
        text = "\n".join(chunks).strip()
        if not text:
            text = page.locator("body").inner_text()
        browser.close()
        return {
            "text": text,
            "tables": table_payload or [],
        }


def fetch_rendered_open_record_text(url: str, cookie: str) -> str:
    payload = fetch_rendered_open_record_payload(url, cookie)
    return str(payload.get("text") or "")


def extract_open_record_participants_from_rendered_tables(
    tables: list[Any],
) -> tuple[list[dict[str, Any]], float | None]:
    participants: list[dict[str, Any]] = []
    budget_candidates: list[float] = []
    seen_names: set[str] = set()
    for table in tables or []:
        if not isinstance(table, list) or not table:
            continue
        rows = [row for row in table if isinstance(row, list) and row]
        if len(rows) < 2:
            continue
        headers = [normalize_loose_text(str(cell or "")) for cell in rows[0]]
        if not headers:
            continue
        company_idx = next((i for i, value in enumerate(headers) if any(alias in value for alias in COMPANY_TABLE_ALIASES)), -1)
        quote_idx = next((i for i, value in enumerate(headers) if any(alias in value for alias in QUOTE_TABLE_ALIASES)), -1)
        budget_idx = next((i for i, value in enumerate(headers) if any(alias in value for alias in BUDGET_TABLE_ALIASES)), -1)
        data_rows = rows[1:]
        if company_idx < 0 or quote_idx < 0:
            width = max(len(row) for row in data_rows)
            col_values: list[list[str]] = [[] for _ in range(width)]
            for row in data_rows[:40]:
                for idx, cell in enumerate(row):
                    col_values[idx].append(str(cell or "").strip())

            def money_ratio(values: list[str]) -> float:
                if not values:
                    return 0.0
                hits = 0
                for value in values:
                    parsed = parse_number(value)
                    if looks_like_valid_money(parsed):
                        hits += 1
                return hits / len(values)

            def text_ratio(values: list[str]) -> float:
                if not values:
                    return 0.0
                hits = 0
                for value in values:
                    if re.search(r"[\u4e00-\u9fa5A-Za-z]{2,}", value) and not parse_number(value):
                        hits += 1
                return hits / len(values)

            money_scores = [money_ratio(values) for values in col_values]
            text_scores = [text_ratio(values) for values in col_values]
            if company_idx < 0:
                company_idx = max(range(width), key=lambda idx: text_scores[idx], default=-1)
                if company_idx >= 0 and text_scores[company_idx] < 0.5:
                    company_idx = -1
            money_ranked = sorted(
                [idx for idx in range(width) if idx != company_idx],
                key=lambda idx: (money_scores[idx], idx),
                reverse=True,
            )
            if quote_idx < 0 and money_ranked:
                quote_idx = money_ranked[0] if money_scores[money_ranked[0]] >= 0.5 else -1
            if budget_idx < 0 and len(money_ranked) > 1:
                for idx in money_ranked[1:]:
                    if money_scores[idx] >= 0.5:
                        budget_idx = idx
                        break
        if company_idx < 0 or quote_idx < 0:
            continue
        for row in data_rows:
            if not isinstance(row, list):
                continue
            cells = [str(cell or "").strip() for cell in row]
            if max(company_idx, quote_idx, budget_idx) >= len(cells):
                continue
            name = clean_entity_name(cells[company_idx])
            if not name:
                raw_name = cells[company_idx]
                if re.search(r"[\u4e00-\u9fa5A-Za-z]{4,}", raw_name):
                    name = raw_name
            quote_header = headers[quote_idx] if quote_idx < len(headers) else ""
            quote = normalize_money_value(cells[quote_idx], quote_header)
            normalized_name = normalize_loose_text(name or "")
            if any(token in normalized_name for token in ("开标", "记录", "内容", "投标", "报价", "控制价", "递交", "日期", "工期")):
                continue
            if not name or not looks_like_valid_money(quote) or name in seen_names:
                continue
            seen_names.add(name)
            participants.append(
                {
                    "rank": len(participants) + 1,
                    "name": name,
                    "quote": float(quote),
                }
            )
            if budget_idx >= 0 and budget_idx < len(cells):
                raw_budget = parse_number(cells[budget_idx])
                budget_value = raw_budget
                budget_header = headers[budget_idx] if budget_idx < len(headers) else ""
                if raw_budget is not None and "万元" in budget_header and raw_budget < 100000.0:
                    budget_value = raw_budget * 10000.0
                if looks_like_valid_money(budget_value):
                    budget_candidates.append(float(budget_value))
    budget = None
    if budget_candidates:
        rounded_counts = defaultdict(int)
        for value in budget_candidates:
            rounded_counts[round(value, 2)] += 1
        budget = max(rounded_counts.items(), key=lambda item: (item[1], item[0]))[0]
    if budget is not None and participants:
        participants = [
            item for item in participants
            if item.get("quote") is not None and abs(float(item["quote"]) - float(budget)) > 0.01
        ]
        for idx, item in enumerate(participants, start=1):
            item["rank"] = idx
    return participants, budget


def html_to_text(source: str) -> str:
    parser = HTMLTextExtractor()
    try:
        parser.feed(source or "")
    except Exception:
        return source or ""
    return parser.get_text()


def normalize_text(value: str) -> str:
    value = html.unescape(value or "")
    value = re.sub(r"\s+", "", value)
    value = value.replace("（", "(").replace("）", ")")
    value = value.replace("【", "").replace("】", "")
    value = value.replace("—", "-").replace("～", "-")
    return value.strip()


def normalize_loose_text(value: str) -> str:
    value = html.unescape(value or "")
    value = value.replace("（", "(").replace("）", ")")
    value = value.replace("【", "").replace("】", "")
    value = value.replace("—", "-").replace("～", "-")
    return value


def normalize_project_key(title: str) -> str:
    text = normalize_text(title)
    text = re.sub(r"^(name|title|projectName|project_name)[:：]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^招标结果", "", text)
    text = re.sub(r"^招标公告", "", text)
    text = re.sub(r"^中标结果", "", text)
    text = re.sub(r"^-?西藏招标网$", "", text)
    text = re.sub(r"-西藏招标网$", "", text)
    text = re.sub(r"^【[^】]+】", "", text)
    text = re.sub(r"\(二次\)|（二次）", "", text)
    text = re.sub(r"\(1标段\)|（1标段）|\(2标段\)|（2标段）", "", text)
    text = re.sub(r"(招标公告|招标文件|开标记录|中标结果公告|中标结果|中标候选人公示|中标公告|结果公告|结果公示|采购公告|结果|公示)$", "", text)
    text = re.sub(r"项目项目$", "项目", text)
    text = re.sub(r"(项目)$", "", text)
    text = re.sub(r"[;；。,.，:：]+$", "", text)
    return text


def extract_section_key(title: Any) -> str:
    text = normalize_text(str(title or ""))
    if not text:
        return ""
    text = re.sub(r"^【[^】]+】", "", text)
    text = re.sub(r"招标编号[:：]?[A-Za-z0-9\-_/()（）]+", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[(（]第?\d+标段[)）]", "", text)
    text = re.sub(r"\d+标段", "", text)
    for token in (
        "中标结果公告",
        "中标候选人公示",
        "中标公告",
        "结果公告",
        "结果公示",
        "开标记录",
        "开标会议",
        "招标文件",
        "招标公告",
        "公开招标公告",
        "竞争性磋商公告",
        "竞争性谈判公告",
        "询比公告",
        "采购公告",
        "补充文件",
        "更正公告",
    ):
        text = text.replace(token, "")
    text = text.replace("项目项目", "项目")
    text = re.sub(r"[;；。,.，:：]+$", "", text)
    return text.strip("-_ ")


def extract_procurement_scope_key(title: Any) -> str:
    text = extract_section_key(title)
    if not text:
        return ""
    markers = (
        "全过程造价咨询服务采购",
        "全过程造价咨询",
        "造价咨询服务采购",
        "造价咨询服务",
        "监理服务采购",
        "监理服务",
        "监理",
        "检测服务采购",
        "检测服务",
        "质量检测服务采购",
        "质量检测服务",
        "质量检测",
        "服务采购",
        "EPC总承包",
        "EPC项目",
        "EPC",
        "施工",
    )
    for marker in markers:
        if marker in text:
            return marker
    return "default"


def extract_bid_section_key(title: Any) -> str:
    text = normalize_text(str(title or ""))
    if not text:
        return ""
    match = re.search(r"(?:[(（])(\d+)标段(?:[)）])", text)
    if match:
        return f"标段{match.group(1)}"
    match = re.search(r"(\d+)标段", text)
    if match:
        return f"标段{match.group(1)}"
    return "default"


def candidate_source_rank(source: Any) -> int:
    order = {
        "table_field": 0,
        "label_value_field": 1,
        "open_record_field": 2,
        "body_regex": 3,
        "shell_page": 4,
        "unknown": 5,
        "missing": 6,
    }
    return order.get(str(source or "unknown"), 5)


def normalize_bid_number(value: Any) -> str:
    text = normalize_text(str(value or ""))
    if not text:
        return ""
    return re.sub(r"[^A-Za-z0-9]", "", text).upper()


def bid_number_match_score(left: Any, right: Any) -> int:
    a = normalize_bid_number(left)
    b = normalize_bid_number(right)
    if not a or not b:
        return 0
    if a == b:
        return 3
    if len(a) >= 8 and len(b) >= 8 and (a.startswith(b) or b.startswith(a)):
        return 2
    if len(a) >= 10 and len(b) >= 10:
        tail = min(12, len(a), len(b))
        if a[-tail:] == b[-tail:]:
            return 1
    return 0


def project_overlap_score(left: str, right: str) -> int:
    a = normalize_project_key(left)
    b = normalize_project_key(right)
    if not a or not b:
        return 0
    if a == b:
        return 4
    if len(a) >= 8 and len(b) >= 8 and (a in b or b in a):
        return 3
    a_tokens = [token for token in re.split(r"[()（）\-_/、，,;；]+", a) if token]
    b_tokens = [token for token in re.split(r"[()（）\-_/、，,;；]+", b) if token]
    overlap = sum(1 for token in a_tokens if len(token) >= 4 and token in b)
    if overlap >= 3:
        return 2
    if a_tokens and b_tokens and a_tokens[0] == b_tokens[0] and overlap >= 1:
        return 1
    return 0


def build_record_group_key(record: dict[str, Any]) -> str:
    project_name = str(record.get("project_name") or "").strip()
    title = str(record.get("title") or "").strip()
    detail_html = str(record.get("raw_detail_html") or "")
    if detail_html:
        parsed = parse_detail_html(detail_html)
        parsed_project_name = str(parsed.get("project_name") or "").strip()
        if parsed_project_name:
            project_name = parsed_project_name
    bid_number = normalize_bid_number(record.get("bid_number"))
    if bid_number:
        return f"bid:{bid_number}"
    if not project_name and title:
        project_name = clean_project_name(title) or ""
    if project_name:
        return f"name:{normalize_project_key(project_name)}"
    if title:
        return f"title:{normalize_project_key(title)}"
    return "unknown"


def split_keywords(raw: str) -> list[str]:
    return [part for part in re.split(r"[\s,，;；/]+", raw.strip()) if part]


def matches_keywords(item: dict[str, Any], keywords: str) -> bool:
    if not keywords.strip():
        return True
    haystack = "".join(
        str(item.get(key, "") or "")
        for key in ("title", "detail", "buyer", "industry", "site", "city", "district")
    )
    parts = split_keywords(keywords)
    if not parts:
        return True
    return all(part in haystack for part in parts)


def first_present(item: dict[str, Any], aliases: tuple[str, ...]) -> Any:
    for alias in aliases:
        if alias in item and item[alias] not in (None, "", []):
            return item[alias]
    return None


def classify_notice(title: str, subtype: str = "") -> str:
    haystack = f"{title}{subtype}"
    if "开标记录" in haystack:
        return "开标记录"
    if "中标候选人公示" in haystack or "成交候选人公示" in haystack:
        return "中标候选人"
    if any(marker in haystack for marker in ("中标结果公告", "中标公告", "结果公告", "成交结果", "结果公示", "成交公告")):
        return "中标结果"
    for notice_type, keywords in NOTICE_KEYWORDS.items():
        if any(keyword in haystack for keyword in keywords):
            return notice_type
    return "其他"


def classify_notice_from_text(title: str, subtype: str = "", detail: str = "") -> str:
    primary = classify_notice(title, subtype)
    if primary != "其他":
        return primary
    haystack = f"{title}{subtype}{detail}"
    return classify_notice(haystack, "")


def extract_money_candidates(text: str) -> list[float]:
    cleaned = normalize_loose_text(text)
    candidates: list[float] = []
    for match in NUMBER_RE.finditer(cleaned):
        raw = match.group(1).replace(",", "")
        try:
            candidates.append(float(raw))
        except ValueError:
            continue
    return candidates


def parse_number(raw: Any) -> float | None:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = normalize_loose_text(str(raw))
    candidates = extract_money_candidates(text)
    if not candidates:
        return None
    return candidates[0]


def normalize_money_value(raw: Any, text_hint: str = "") -> float | None:
    value = parse_number(raw)
    if value is None:
        return None
    hint = normalize_text(text_hint)
    unit_match = UNIT_RE.search(hint)
    if unit_match and unit_match.group(1) == "万元":
        return value * 10000.0
    return value


def extract_result_amount(detail: str) -> float | None:
    text = normalize_loose_text(detail)
    patterns = [
        r"中标价(?:格)?(?:（元）|\(元\)|[:：])\s*([0-9][0-9,]*(?:\.\d+)?)",
        r"中标(?:（成交）|\(成交\))?金额(?:（元）|\(元\)|[:：])\s*([0-9][0-9,]*(?:\.\d+)?)",
        r"成交金额(?:（元）|\(元\)|[:：])\s*([0-9][0-9,]*(?:\.\d+)?)",
        r"中标价（元）\s*([0-9][0-9,]*(?:\.\d+)?)",
        r"中标金额\s*([0-9][0-9,]*(?:\.\d+)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        value = parse_number(match.group(1))
        if looks_like_valid_money(value):
            return value
    return None


def clean_entity_name(raw: Any) -> str | None:
    if raw is None:
        return None
    text = html.unescape(str(raw)).strip()
    if not text:
        return None
    text = re.sub(r"\s+", "", text)
    for prefix in ROLE_NOISE_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix):]
    text = text.strip("：:;,，；。")
    for ending in COMPANY_ENDINGS:
        idx = text.find(ending)
        if idx != -1:
            text = text[: idx + len(ending)]
            break
    text = re.split(r"(投标报价|报价|中标金额|成交金额|金额[:：])", text, maxsplit=1)[0]
    company_match = re.search(
        r"([\u4e00-\u9fa5A-Za-z0-9()（）\-]+?(?:有限责任公司|集团有限公司|股份有限公司|建筑工程有限公司|工程有限公司|路桥有限公司|有限公司))",
        text,
    )
    if company_match:
        text = company_match.group(1)
    if not any(ending in text for ending in COMPANY_ENDINGS):
        return None
    return text or None


def clean_org_name(raw: Any) -> str | None:
    if raw is None:
        return None
    text = html.unescape(str(raw)).strip()
    if not text:
        return None
    text = re.sub(r"\s+", "", text)
    text = text.strip("：:;,，；。")
    text = re.split(r"(投标报价|报价|中标金额|成交金额|金额[:：]|联系方式|联系电话)", text, maxsplit=1)[0]
    text = re.sub(r"^(采购单位|采购人|招标人|建设单位|业主单位|名称)[：:]", "", text)
    if len(text) < 4:
        return None
    for ending in ORG_ENDINGS:
        idx = text.find(ending)
        if idx != -1:
            text = text[: idx + len(ending)]
            break
    if re.fullmatch(r"(?:0\d{2,3}-?)?\d{7,11}", text):
        return None
    if re.fullmatch(r"[A-Za-z0-9_\-.]+@[A-Za-z0-9_\-.]+", text):
        return None
    if not re.search(r"[\u4e00-\u9fa5]", text):
        return None
    return text or None


def pick_best_winner_candidate(*values: Any) -> str | None:
    for value in values:
        cleaned = clean_entity_name(value)
        if cleaned:
            return cleaned
    return None


def clean_project_name(raw: Any) -> str | None:
    if raw is None:
        return None
    text = html.unescape(str(raw)).strip()
    if not text:
        return None
    text = re.sub(r"\s+", "", text)
    text = text.replace("（", "(").replace("）", ")")
    text = text.replace("【", "").replace("】", "")
    text = re.sub(r"^(项目名称|工程名称)[：:]", "", text)
    text = re.sub(r"^(name|title|projectName|project_name|项目名称|工程名称)[：:]", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^name:", "", text, flags=re.IGNORECASE)
    text = re.sub(r"^关于", "", text)
    text = re.sub(r"^(招标结果|招标公告|中标结果|中标公告|结果公告|结果公示|采购公告)", "", text)
    text = re.sub(r"(招标公告|招标文件|开标记录|中标结果公告|中标结果|中标候选人公示|中标公告|结果公告|结果公示|采购公告|结果|公示)$", "", text)
    text = re.sub(r"(中标\(成交\)|中标|成交|流标公告|流标|废标公告|废标|终止公告|终止|询价|招标失败公告|失败公告|推荐的中标候选人公示及否决原因|推荐的中标候选人公示|推荐的)$", "", text)
    text = re.sub(r"[-_]+西藏招标网$", "", text)
    text = text.strip("：:;,，；。()（）-_")
    for pattern in NAME_NOISE_PATTERNS:
        if re.fullmatch(pattern, text):
            return None
    if len(text) <= 4 and not re.search(r"项目|工程|学校|医院|公园|办公|改造|维修|建设|采购|房|楼|站|园|路|厂|村|镇", text):
        return None
    return text or None


def looks_like_project_name(value: Any) -> bool:
    text = clean_project_name(value)
    if not text:
        return False
    if "本条项目信息由剑鱼标讯" in text:
        return False
    if "客服热线" in text or "工作时间" in text or "投稿邮箱" in text:
        return False
    if re.fullmatch(r"(?:0\d{2,3}-?)?\d{7,11}", text):
        return False
    if re.fullmatch(r"[A-Za-z0-9_\-.]+@[A-Za-z0-9_\-.]+", text):
        return False
    if re.fullmatch(r"bid:[A-Za-z0-9]+", text, flags=re.IGNORECASE):
        return False
    if re.fullmatch(r"[A-Za-z0-9\-_/]{10,}", text) and not re.search(r"项目|工程|采购|建设|改造|维修|学校|医院|道路|标段", text):
        return False
    if len(text) <= 20 and not re.search(r"项目|工程|采购|建设|改造|维修|学校|医院|道路|楼|站|园|标段|公示|公告", text):
        return False
    return True


def extract_label_value_map(text: str) -> dict[str, str]:
    cleaned = html.unescape(text or "")
    result: dict[str, str] = {}
    for line in cleaned.splitlines():
        line = line.strip()
        if not line:
            continue
        if "：" in line:
            key, value = line.split("：", 1)
        elif ":" in line:
            key, value = line.split(":", 1)
        else:
            continue
        key = normalize_text(key)
        value = value.strip()
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}\s*\d{1,2}", key):
            continue
        if re.fullmatch(r"\d{1,2}", key) and re.fullmatch(r"\d{2}:\d{2}(?::\d{2})?", value):
            continue
        if key and value and key not in result:
            result[key] = value
    return result


def extract_following_line_value(text: str, aliases: tuple[str, ...], max_lookahead: int = 3) -> str | None:
    lines = [line.strip() for line in html.unescape(text or "").splitlines()]
    normalized_aliases = {normalize_text(alias) for alias in aliases}
    for idx, line in enumerate(lines):
        if normalize_text(line) not in normalized_aliases:
            continue
        for offset in range(1, max_lookahead + 1):
            next_idx = idx + offset
            if next_idx >= len(lines):
                break
            candidate = lines[next_idx].strip()
            if not candidate:
                continue
            normalized_candidate = normalize_text(candidate)
            if normalized_candidate in normalized_aliases:
                continue
            return candidate
    return None


def extract_inline_value(text: str, aliases: tuple[str, ...]) -> str | None:
    raw = html.unescape(text or "")
    for alias in aliases:
        patterns = [
            rf"{re.escape(alias)}[：:]\s*([^\n\r]+?)(?:[。；;]|$)",
            rf"{re.escape(alias)}\s*([^\n\r]*?[0-9,]+(?:\.\d+)?\s*(?:万元|元)?)(?:[。；;]|$)",
        ]
        for pattern in patterns:
            match = re.search(pattern, raw)
            if match:
                return match.group(1).strip()
    return None


def extract_number_after_alias(text: str, aliases: tuple[str, ...]) -> float | None:
    raw = html.unescape(text or "")
    for alias in aliases:
        pattern = rf"{re.escape(alias)}\s*[:：]?\s*([0-9,]+(?:\.\d+)?)\s*(?:[（(]?\s*(万元|元)\s*[)）]?)?"
        match = re.search(pattern, raw)
        if not match:
            continue
        number = match.group(1)
        unit = match.group(2) or ""
        return normalize_money_value(number, unit)
    return None


def extract_number_near_alias(text: str, aliases: tuple[str, ...], window: int = 48) -> float | None:
    raw = html.unescape(text or "")
    for alias in aliases:
        for match in re.finditer(re.escape(alias), raw):
            segment = raw[match.end(): match.end() + window]
            values = [value for value in extract_money_candidates(segment) if 100000.0 <= value <= 1000000000.0]
            if values:
                return max(values)
    return None


def extract_by_alias_map(label_map: dict[str, str], aliases: tuple[str, ...]) -> str | None:
    normalized_aliases = {normalize_text(alias) for alias in aliases}
    for key, value in label_map.items():
        if key in normalized_aliases:
            return value
    for key, value in label_map.items():
        if any(alias in key for alias in normalized_aliases):
            return value
    return None


def detect_masked(text: str) -> bool:
    return bool(MASK_RE.search(text or ""))


def choose_best_money(values: list[float]) -> float | None:
    if not values:
        return None
    return max(values)


def looks_like_valid_money(value: float | None) -> bool:
    if value is None:
        return False
    return 100000.0 <= value <= 1000000000.0


def should_fallback_bid_amount(record: dict[str, Any]) -> bool:
    if record.get("notice_type") not in {"中标结果", "中标候选人"}:
        return False
    detail = str(record.get("raw", {}).get("detail", "") or "")
    return any(alias in detail for alias in VALUE_FIELD_ALIASES["bid_amount"])


def should_fallback_budget(record: dict[str, Any]) -> bool:
    if record.get("notice_type") not in {"招标公告", "开标记录"}:
        return False
    detail = str(record.get("raw", {}).get("detail", "") or "")
    return any(alias in detail for alias in VALUE_FIELD_ALIASES["budget"])


def choose_preferred_record(group: list[dict[str, Any]], notice_type: str) -> dict[str, Any] | None:
    if notice_type == "开标记录":
        candidates = [
            item for item in group
            if item["notice_type"] == notice_type
            or looks_like_open_record_text(item)
        ]
    else:
        candidates = [item for item in group if item["notice_type"] == notice_type]
    if not candidates:
        return None
    def publish_sort_value(item: dict[str, Any]) -> int:
        value = item.get("publish_time")
        if isinstance(value, (int, float)):
            return int(value)
        if isinstance(value, str):
            parsed = parse_iso_date(value)
            if parsed:
                return int(parsed.strftime("%Y%m%d"))
        return 0
    def detail_len(item: dict[str, Any]) -> int:
        return len(str(item.get("raw_detail_text") or item.get("raw", {}).get("detail") or ""))
    def bid_quote_count(item: dict[str, Any]) -> int:
        if item.get("bid_quotes"):
            return len(item.get("bid_quotes") or [])
        if item.get("bid_participants"):
            return len(item.get("bid_participants") or [])
        return 0
    candidates.sort(
        key=lambda item: (
            candidate_source_rank(item.get("budget_source") if notice_type in {"招标公告", "开标记录"} else item.get("bid_amount_source")),
            0 if item.get("raw_detail_html") else 1,
            -bid_quote_count(item) if notice_type == "开标记录" else 0,
            0 if item.get("winner") else 1,
            0 if item.get("bid_amount") is not None else 1,
            0 if item.get("budget") is not None else 1,
            -detail_len(item),
            item.get("masked", False),
            -publish_sort_value(item),
        )
    )
    return candidates[0]


def choose_consistent_price(records: list[dict[str, Any]], field: str, control_price: float | None) -> dict[str, Any] | None:
    candidates = [item for item in records if item.get(field) is not None]
    if not candidates:
        return None
    candidates.sort(
        key=lambda item: (
            candidate_source_rank(item.get(f"{field}_source")),
            0 if item.get("procurement_scope_key") not in {"default", ""} else 1,
            0 if item.get("bid_section_key") not in {"default", ""} else 1,
            0 if item.get("raw_detail_html") else 1,
            0 if item.get(field) is not None else 1,
        )
    )
    if control_price is None:
        return candidates[0]
    consistent = [item for item in candidates if item.get(field) is not None and 0.2 <= float(item[field]) / float(control_price) <= 5.0]
    return consistent[0] if consistent else candidates[0]


def looks_like_open_record_text(record: dict[str, Any]) -> bool:
    title = str(record.get("title") or "")
    detail = str(record.get("raw_detail_text") or record.get("raw", {}).get("detail", "") or "")
    if "开标记录" in title:
        return True
    if "投标人名称" in detail and ("投标报价" in detail or "投标总报价" in detail):
        return True
    if "报价单位" in detail and "报价" in detail:
        return True
    return False


def extract_bid_quotes(record: dict[str, Any]) -> list[float]:
    if record.get("notice_type") != "开标记录" and not looks_like_open_record_text(record):
        return []
    if record.get("bid_participants"):
        return [float(item["quote"]) for item in record["bid_participants"] if item.get("quote") is not None]
    detail = str(record.get("raw_detail_text") or record.get("raw", {}).get("detail", "") or "")
    values = [value for value in extract_money_candidates(detail) if 100000.0 <= value <= 1000000000.0]
    if not values:
        return []
    budget = record.get("budget")
    quotes = []
    for value in values:
        if budget is not None and abs(float(value) - float(budget)) < 0.01:
            continue
        quotes.append(float(value))
    deduped: list[float] = []
    seen: set[float] = set()
    for value in quotes:
        rounded = round(value, 2)
        if rounded in seen:
            continue
        seen.add(rounded)
        deduped.append(value)
    return deduped


def extract_bid_participants(record: dict[str, Any]) -> list[dict[str, Any]]:
    if record.get("notice_type") != "开标记录" and not looks_like_open_record_text(record):
        return []
    detail = str(record.get("raw_detail_text") or record.get("raw", {}).get("detail", "") or "")
    if "投标人名称" not in detail and "投标总报价" not in detail and "投标报价" not in detail:
        return []
    if "投标人名称" in detail:
        detail = detail.split("投标人名称", 1)[1]
    lines = [line.strip() for line in detail.splitlines() if line.strip()]
    participants: list[dict[str, Any]] = []
    company_pattern = re.compile(rf"([\u4e00-\u9fa5A-Za-z0-9()（）\-]+?(?:{'|'.join(map(re.escape, COMPANY_ENDINGS))}))")
    i = 0
    while i < len(lines):
        company_match = company_pattern.search(lines[i])
        if not company_match:
            i += 1
            continue
        name = company_match.group(1)
        quote = None
        for j in range(i + 1, min(i + 8, len(lines))):
            numbers = extract_money_candidates(lines[j])
            if not numbers:
                continue
            candidate = numbers[0]
            if 100000 < candidate < 1000000000:
                quote = candidate
                break
        participants.append({"rank": len(participants) + 1, "name": name, "quote": quote})
        i = j + 1 if quote is not None else i + 1
    return [item for item in participants if item.get("quote") is not None]


def extract_open_record_participants_from_text(detail_text: str) -> list[dict[str, Any]]:
    text = normalize_loose_text(detail_text or "")
    if "投标人名称" not in text:
        return []
    participants: list[dict[str, Any]] = []
    company_pattern = re.compile(rf"([\u4e00-\u9fa5A-Za-z0-9()（）\-]+?(?:{'|'.join(map(re.escape, COMPANY_ENDINGS))}))")
    for match in company_pattern.finditer(text):
        raw_name = match.group(1)
        last_name = None
        for ending in COMPANY_ENDINGS:
            idx = raw_name.rfind(ending)
            if idx != -1:
                candidate = raw_name[: idx + len(ending)]
                if any(token in candidate for token in ("项目", "开标记录内容", "投标人名称", "工期", "递交时间")):
                    candidate = candidate.split("递交时间")[-1].split("工期")[-1].split("投标人名称")[-1].split("开标记录内容")[-1]
                last_name = candidate
        name = clean_entity_name(last_name or raw_name)
        if not name:
            continue
        tail = text[match.end():]
        money_candidates = []
        for money_match in re.finditer(r"\d{6,10}\.\d{2}", tail):
            value = parse_number(money_match.group(0))
            if looks_like_valid_money(value):
                money_candidates.append(float(value))
            if len(money_candidates) >= 2:
                break
        if not money_candidates:
            continue
        quote = money_candidates[0]
        participants.append(
            {
                "rank": len(participants) + 1,
                "name": name,
                "quote": float(quote),
            }
        )
    return participants


def infer_open_record_budget(detail_text: str, participants: list[dict[str, Any]]) -> float | None:
    if not detail_text:
        return None
    values = [value for value in extract_money_candidates(detail_text) if 100000.0 <= value <= 1000000000.0]
    if not values:
        return None
    participant_quotes = {float(item["quote"]) for item in participants if item.get("quote") is not None}
    repeated = [value for value in values if values.count(value) >= 2]
    repeated_unique = sorted({value for value in repeated if value not in participant_quotes}, reverse=True)
    if repeated_unique:
        return repeated_unique[0]
    for value in sorted(set(values), reverse=True):
        if value not in participant_quotes:
            return value
    return None


def extract_open_record_budget_from_text(detail_text: str) -> float | None:
    text = normalize_loose_text(detail_text or "")
    company_pattern = re.compile(rf"([\u4e00-\u9fa5A-Za-z0-9()（）\-]+?(?:{'|'.join(map(re.escape, COMPANY_ENDINGS))}))")
    company_match = company_pattern.search(text)
    if company_match:
        tail = text[company_match.end():]
        money_candidates = []
        for money_match in re.finditer(r"\d{6,10}\.\d{2}", tail):
            value = parse_number(money_match.group(0))
            if looks_like_valid_money(value):
                money_candidates.append(float(value))
            if len(money_candidates) >= 2:
                break
        if len(money_candidates) >= 2:
            return money_candidates[1]
    patterns = [
        r"控制价\(万元\).*?(\d+(?:\.\d+)?)",
        r"控制价（万元）.*?(\d+(?:\.\d+)?)",
        r"控制价(?:\s*[:：]|\s*)\s*(\d+(?:\.\d+)?)\s*(?:万元|元)?",
        r"最高限价(?:\s*[:：]|\s*)\s*(\d+(?:\.\d+)?)\s*(?:万元|元)?",
        r"招标控制价(?:\s*[:：]|\s*)\s*(\d+(?:\.\d+)?)\s*(?:万元|元)?",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
        if not match:
            continue
        value = parse_number(match.group(1))
        if value is None:
            continue
        if value < 100000.0:
            value *= 10000.0
        if looks_like_valid_money(value):
            return value
    return None


def reparse_record_from_detail(record: dict[str, Any]) -> dict[str, Any]:
    html_text = str(record.get("raw_detail_html") or "")
    if not html_text:
        return record
    if "<html" in html_text.lower() or "<body" in html_text.lower():
        parsed = parse_detail_html(html_text)
        detail_text = str(parsed.get("detail") or "")
    else:
        detail_text = html_to_text(html_text)
        parsed = {
            "detail": detail_text,
            "title": record.get("title") or "",
            "project_name": record.get("project_name") or "",
            "notice_type": record.get("notice_type") or "",
            "publish_time": record.get("publish_time"),
            "budget": None,
            "budget_source": "missing",
            "bid_amount": None,
            "bid_amount_source": "missing",
            "winner": record.get("winner"),
            "winner_source": record.get("winner_source", "missing"),
            "buyer": record.get("buyer"),
            "city": record.get("city"),
            "district": record.get("district"),
            "bid_quotes": [],
            "bid_participants": [],
            "attachments": [],
            "attachment_links": [],
            "original_url": None,
            "related_links": [],
            "procurement_scope_key": "default",
            "bid_section_key": "default",
        }
    if not detail_text:
        return record
    merged = dict(record)
    raw = dict(merged.get("raw", {}) or {})
    raw["detail"] = detail_text
    raw["title"] = parsed.get("title") or raw.get("title")
    raw["html_title"] = parsed.get("title") or raw.get("html_title")
    merged["raw"] = raw
    refined = canonical_record(raw)
    refined["raw_detail_html"] = html_text
    refined["raw_detail_url"] = record.get("raw_detail_url")
    refined["raw_detail_text"] = detail_text
    original_title = str(record.get("title") or "").strip()
    original_project_name = str(record.get("project_name") or "").strip()
    original_notice_type = str(record.get("notice_type") or "").strip()
    if parsed.get("project_name") and looks_like_project_name(parsed.get("project_name")):
        refined["project_name"] = parsed["project_name"]
    elif original_project_name and looks_like_project_name(original_project_name):
        refined["project_name"] = original_project_name
    if parsed.get("title") and looks_like_project_name(parsed.get("title")):
        refined["title"] = parsed["title"]
    elif original_title and looks_like_project_name(original_title):
        refined["title"] = original_title
    if parsed.get("notice_type") and parsed.get("notice_type") != "其他":
        refined["notice_type"] = parsed["notice_type"]
    elif original_notice_type:
        refined["notice_type"] = original_notice_type
    if parsed.get("publish_time"):
        refined["publish_time"] = parsed["publish_time"]
    if looks_like_valid_money(parsed.get("budget")):
        refined["budget"] = parsed["budget"]
    if parsed.get("budget_source"):
        refined["budget_source"] = parsed.get("budget_source")
    if looks_like_valid_money(parsed.get("bid_amount")):
        refined["bid_amount"] = parsed["bid_amount"]
    if parsed.get("bid_amount_source"):
        refined["bid_amount_source"] = parsed.get("bid_amount_source")
    if parsed.get("winner"):
        refined["winner"] = parsed["winner"]
    if parsed.get("winner_source"):
        refined["winner_source"] = parsed.get("winner_source")
    if parsed.get("buyer"):
        refined["buyer"] = parsed["buyer"]
    if parsed.get("city"):
        refined["city"] = parsed["city"]
    if parsed.get("district"):
        refined["district"] = parsed["district"]
    if parsed.get("bid_quotes"):
        refined["bid_quotes"] = parsed["bid_quotes"]
    if parsed.get("bid_participants"):
        refined["bid_participants"] = parsed["bid_participants"]
    if parsed.get("attachments"):
        refined["attachments"] = list(parsed["attachments"])
    if parsed.get("attachment_links"):
        refined["attachment_links"] = list(parsed["attachment_links"])
    if parsed.get("original_url"):
        refined["original_url"] = parsed["original_url"]
    if parsed.get("related_links"):
        refined["related_links"] = list(parsed["related_links"])
    if parsed.get("procurement_scope_key"):
        refined["procurement_scope_key"] = parsed.get("procurement_scope_key")
    if parsed.get("bid_section_key"):
        refined["bid_section_key"] = parsed.get("bid_section_key")
    if refined.get("notice_type") == "开标记录":
        if parsed.get("bid_participants"):
            refined["bid_participants"] = list(parsed["bid_participants"])
            refined["bid_quotes"] = [float(item["quote"]) for item in refined["bid_participants"] if item.get("quote") is not None]
        elif extract_open_record_participants_from_text(detail_text):
            refined["bid_participants"] = extract_open_record_participants_from_text(detail_text)
            refined["bid_quotes"] = [float(item["quote"]) for item in refined["bid_participants"] if item.get("quote") is not None]
        open_budget = extract_open_record_budget_from_text(detail_text) or infer_open_record_budget(detail_text, refined.get("bid_participants") or [])
        if looks_like_valid_money(open_budget):
            refined["budget"] = open_budget
        refined["bid_amount"] = None
    if not looks_like_project_name(refined.get("project_name")) and original_project_name and looks_like_project_name(original_project_name):
        refined["project_name"] = original_project_name
    if not looks_like_project_name(refined.get("title")) and original_title and looks_like_project_name(original_title):
        refined["title"] = original_title
    return refined


def canonical_record(item: dict[str, Any]) -> dict[str, Any]:
    title = str(first_present(item, FIELD_ALIASES["project_name"]) or "")
    detail = str(item.get("detail", "") or "")
    subtype = str(first_present(item, FIELD_ALIASES["notice_type"]) or "")
    label_map = extract_label_value_map(detail)
    project_name_raw = (
        extract_by_alias_map(label_map, FIELD_ALIASES["project_name"])
        or extract_following_line_value(detail, FIELD_ALIASES["project_name"])
        or extract_inline_value(detail, FIELD_ALIASES["project_name"])
        or title
    )
    budget_raw = first_present(item, VALUE_FIELD_ALIASES["budget"])
    budget_text = extract_by_alias_map(label_map, VALUE_FIELD_ALIASES["budget"])
    budget_inline = extract_inline_value(detail, VALUE_FIELD_ALIASES["budget"])
    bid_amount_raw = first_present(item, VALUE_FIELD_ALIASES["bid_amount"])
    bid_amount_text = extract_by_alias_map(label_map, VALUE_FIELD_ALIASES["bid_amount"])
    bid_amount_inline = extract_inline_value(detail, VALUE_FIELD_ALIASES["bid_amount"])
    winner_raw = first_present(item, VALUE_FIELD_ALIASES["winner"])
    winner_text = extract_by_alias_map(label_map, VALUE_FIELD_ALIASES["winner"])
    winner_inline = extract_inline_value(detail, VALUE_FIELD_ALIASES["winner"])
    if winner_inline is None:
        winner_inline = extract_inline_value(detail, ("第一中标候选人", "推荐第一中标候选人"))
    budget = (
        normalize_money_value(budget_raw, detail)
        or normalize_money_value(budget_text, budget_text or detail)
        or normalize_money_value(budget_inline, budget_inline or detail)
        or extract_number_after_alias(detail, VALUE_FIELD_ALIASES["budget"])
        or normalize_money_value(first_present(label_map, VALUE_FIELD_ALIASES["budget"]), detail)  # type: ignore[arg-type]
    )
    budget_source = "missing"
    if budget_raw is not None:
        budget_source = "table_field"
    elif budget_text is not None or budget_inline is not None or first_present(label_map, VALUE_FIELD_ALIASES["budget"]) is not None:
        budget_source = "label_value_field"
    elif extract_number_after_alias(detail, VALUE_FIELD_ALIASES["budget"]) is not None:
        budget_source = "body_regex"
    bid_amount = (
        extract_result_amount(detail)
        or
        normalize_money_value(bid_amount_raw, detail)
        or normalize_money_value(bid_amount_text, bid_amount_text or detail)
        or normalize_money_value(bid_amount_inline, bid_amount_inline or detail)
        or extract_number_after_alias(detail, VALUE_FIELD_ALIASES["bid_amount"])
        or normalize_money_value(first_present(label_map, VALUE_FIELD_ALIASES["bid_amount"]), detail)  # type: ignore[arg-type]
    )
    bid_amount_source = "missing"
    if extract_result_amount(detail) is not None:
        bid_amount_source = "body_regex"
    elif bid_amount_raw is not None:
        bid_amount_source = "table_field"
    elif bid_amount_text is not None or bid_amount_inline is not None or first_present(label_map, VALUE_FIELD_ALIASES["bid_amount"]) is not None:
        bid_amount_source = "label_value_field"
    winner = pick_best_winner_candidate(winner_raw, winner_text, winner_inline)
    winner_source = "missing"
    if winner_raw is not None:
        winner_source = "table_field"
    elif winner_text is not None or winner_inline is not None:
        winner_source = "label_value_field"

    record = {
        "id": item.get("id"),
        "title": title,
        "project_key": normalize_project_key(title),
        "notice_type": classify_notice_from_text(title, subtype, detail),
        "subtype": subtype,
        "area": item.get("area"),
        "city": normalize_city_name(
            first_present(item, FIELD_ALIASES["city"])
            or extract_by_alias_map(label_map, FIELD_ALIASES["city"])
            or extract_inline_value(detail, FIELD_ALIASES["city"])
        ),
        "district": normalize_region_text(
            first_present(item, FIELD_ALIASES["district"])
            or extract_by_alias_map(label_map, FIELD_ALIASES["district"])
            or extract_inline_value(detail, FIELD_ALIASES["district"])
        ),
        "buyer": clean_org_name(first_present(item, FIELD_ALIASES["buyer"]) or extract_by_alias_map(label_map, FIELD_ALIASES["buyer"])),
        "winner": winner,
        "winner_source": winner_source,
        "project_name": clean_project_name(project_name_raw) if project_name_raw else None,
        "bid_number": extract_bid_number(detail),
        "budget": budget,
        "budget_source": budget_source,
        "bid_amount": bid_amount,
        "bid_amount_source": bid_amount_source,
        "publish_time": first_present(item, FIELD_ALIASES["publish_time"]),
        "bid_open_time": first_present(item, FIELD_ALIASES["bid_open_time"]),
        "industry": first_present(item, FIELD_ALIASES["industry"]),
        "site": first_present(item, FIELD_ALIASES["site"]),
        "spider_code": first_present(item, FIELD_ALIASES["spider_code"]),
        "masked": detect_masked(title) or detect_masked(detail),
        "money_candidates": extract_money_candidates(f"{title}\n{detail}"),
        "label_values": label_map,
        "attachments": list(item.get("attachments") or []),
        "attachment_links": list(item.get("attachment_links") or []),
        "original_url": item.get("original_url"),
        "related_links": list(item.get("related_links") or []),
        "procurement_scope_key": extract_procurement_scope_key(title),
        "bid_section_key": extract_bid_section_key(title),
        "raw": item,
    }
    if not looks_like_valid_money(record["budget"]):
        record["budget"] = None
    if not looks_like_valid_money(record["bid_amount"]):
        record["bid_amount"] = None
    if "html_title" in item:
        record["raw"]["html_title"] = item["html_title"]
    if record["bid_amount"] is None and record["money_candidates"]:
        if should_fallback_bid_amount(record):
            record["bid_amount"] = choose_best_money(record["money_candidates"])
        elif should_fallback_budget(record) and record["budget"] is None:
            record["budget"] = choose_best_money(record["money_candidates"])
    if not looks_like_valid_money(record["bid_amount"]):
        record["bid_amount"] = None
    if not looks_like_valid_money(record["budget"]):
        record["budget"] = None
    if record["budget"] is None:
        for alias in VALUE_FIELD_ALIASES["budget"]:
            if alias in label_map:
                record["budget"] = normalize_money_value(label_map[alias], alias)
                if record["budget"] is not None:
                    break
    if record["notice_type"] == "开标记录" and record["budget"] is None:
        record["budget"] = (
            extract_open_record_budget_from_text(detail)
            or
            extract_number_near_alias(detail, VALUE_FIELD_ALIASES["budget"])
            or extract_number_after_alias(detail, VALUE_FIELD_ALIASES["budget"])
        )
    if record["notice_type"] == "开标记录":
        text_participants = extract_open_record_participants_from_text(detail)
        if text_participants:
            record["bid_participants"] = text_participants
            record["bid_quotes"] = [float(item["quote"]) for item in text_participants if item.get("quote") is not None]
        if record["budget"] is None:
            record["budget"] = extract_open_record_budget_from_text(detail)
    if record["bid_amount"] is None:
        for alias in VALUE_FIELD_ALIASES["bid_amount"]:
            if alias in label_map:
                record["bid_amount"] = normalize_money_value(label_map[alias], alias)
                if record["bid_amount"] is not None:
                    break
    if not looks_like_valid_money(record["bid_amount"]):
        record["bid_amount"] = None
    if not looks_like_valid_money(record["budget"]):
        record["budget"] = None
    if not record.get("bid_participants"):
        record["bid_participants"] = extract_bid_participants(record)
    if not record.get("bid_quotes"):
        record["bid_quotes"] = extract_bid_quotes(record)
    if record["notice_type"] == "开标记录":
        record["bid_amount"] = None
    return record


def enrich_record_with_detail(record: dict[str, Any], cookie: str) -> dict[str, Any]:
    source = record.get("raw", {}) or {}
    original_title = str(record.get("title") or "").strip()
    original_notice_type = str(record.get("notice_type") or "").strip()
    url = build_nologin_content_url_from_id(source.get("id"), str(record.get("project_name") or record.get("title") or ""))
    if not url:
        url = source.get("url") or source.get("detailUrl") or source.get("href")
    if not url and source.get("id"):
        url = f"https://xizang.jianyu360.cn/jybx/{source['id']}.html"
    if not url:
        return record
    if url.startswith("/"):
        url = f"https://www.jianyu360.cn{url}"
    url = build_nologin_content_url(url, str(record.get("project_name") or record.get("title") or ""))
    if not url.startswith(DETAIL_URL_PREFIXES):
        return record
    try:
        html_text = fetch_html(url, cookie)
    except Exception:
        return record
    if is_captcha_html(html_text):
        register_detail_captcha_hit()
        LAST_FETCH_META["anti_verify"] = True
        if handle_manual_captcha(
            {
                "scope": "detail_html",
                "url": url,
                "title": str(record.get("title") or record.get("project_name") or ""),
                "message": "详情页触发验证码，等待人工处理后继续。",
            }
        ):
            try:
                html_text = fetch_html(url, cookie)
            except Exception:
                return record
            if is_captcha_html(html_text):
                return record
    clear_detail_captcha_hits()
    sid = extract_sid_from_html(html_text)
    if sid:
        try:
            preagent = fetch_detail_preagent(url, cookie)
        except Exception:
            preagent = {}
        token = str(((preagent.get("data") or {}).get("token")) or "")
        if token:
            try:
                baseinfo = fetch_detail_baseinfo(url, sid, token, cookie)
            except Exception:
                baseinfo = {}
            detail_html = str((((baseinfo.get("data") or {}).get("detailInfo") or {}).get("detail")) or "")
            if detail_html:
                raw = dict(record.get("raw", {}) or {})
                raw["detail"] = detail_html
                raw["url"] = url
                record["raw"] = raw
                record["raw_detail_html"] = detail_html
                record["raw_detail_url"] = url
                record["raw_detail_text"] = html_to_text(detail_html)
                reparsed = reparse_record_from_detail(record)
                if reparsed.get("notice_type") == "开标记录" and reparsed.get("bid_participants"):
                    return reparsed
    parsed = parse_detail_html(html_text)
    rendered_override = False
    if (
        str(record.get("notice_type") or "") == "开标记录"
        and (
            "投标人名称" not in str(parsed.get("detail") or "")
            or not parsed.get("bid_quotes")
        )
    ):
        try:
            rendered_payload = fetch_rendered_open_record_payload(url, cookie)
        except Exception:
            rendered_payload = {}
        rendered_text = str(rendered_payload.get("text") or "")
        rendered_tables = list(rendered_payload.get("tables") or [])
        if is_captcha_html(rendered_text):
            register_detail_captcha_hit()
            if handle_manual_captcha(
                {
                    "scope": "rendered_open_record",
                    "url": url,
                    "title": str(record.get("title") or record.get("project_name") or ""),
                    "message": "开标记录渲染页触发验证码，等待人工处理后继续。",
                }
            ):
                try:
                    rendered_payload = fetch_rendered_open_record_payload(url, cookie)
                except Exception:
                    return record
                rendered_text = str(rendered_payload.get("text") or "")
                rendered_tables = list(rendered_payload.get("tables") or [])
                if is_captcha_html(rendered_text):
                    return record
        if rendered_text and "投标人名称" in rendered_text:
            raw = dict(record.get("raw", {}) or {})
            raw["detail"] = rendered_text
            parsed = parse_detail_html(html_text)
            parsed["detail"] = rendered_text
            rendered_budget = extract_open_record_budget_from_text(rendered_text)
            rendered_participants, rendered_table_budget = extract_open_record_participants_from_rendered_tables(rendered_tables)
            if not rendered_participants:
                rendered_participants = extract_open_record_participants_from_text(rendered_text)
            parsed["bid_participants"] = rendered_participants
            parsed["bid_quotes"] = [float(item["quote"]) for item in rendered_participants if item.get("quote") is not None]
            chosen_rendered_budget = rendered_table_budget or rendered_budget
            if looks_like_valid_money(chosen_rendered_budget):
                parsed["budget"] = chosen_rendered_budget
                parsed["budget_source"] = "open_record_field"
            parsed["notice_type"] = "开标记录"
            rendered_override = True
    if (not rendered_override and not looks_like_detail_html(html_text)) or not is_detail_record_candidate(parsed, html_text):
        return record
    raw = dict(record.get("raw", {}) or {})
    raw["detail"] = parsed.get("detail") or raw.get("detail")
    raw["url"] = url
    raw["html_title"] = parsed.get("title")
    record.update(
        {
            "title": parsed.get("title") if looks_like_project_name(parsed.get("title")) else (original_title or record.get("title")),
            "notice_type": parsed.get("notice_type") if parsed.get("notice_type") and parsed.get("notice_type") != "其他" else (original_notice_type or record.get("notice_type")),
            "budget": parsed.get("budget") if parsed.get("budget") is not None else record.get("budget"),
            "bid_amount": parsed.get("bid_amount") if parsed.get("bid_amount") is not None else record.get("bid_amount"),
            "winner": parsed.get("winner") or record.get("winner"),
            "bid_quotes": parsed.get("bid_quotes") or record.get("bid_quotes") or [],
            "bid_participants": parsed.get("bid_participants") or record.get("bid_participants") or [],
            "attachments": parsed.get("attachments") or record.get("attachments") or [],
            "raw_detail_text": parsed.get("detail"),
            "raw": raw,
        }
    )
    record["raw_detail_html"] = html_text
    record["raw_detail_url"] = url
    if record.get("notice_type") == "开标记录":
        record["bid_amount"] = None
    return record


def fetch_page(cookie: str, config: SearchConfig, page_num: int) -> dict[str, Any]:
    payload = {
        "searchGroup": 1,
        "reqType": "lastNews",
        "pageNum": page_num,
        "pageSize": config.page_size,
        "keyWords": config.keywords,
        "searchMode": 0,
        "bidField": "",
        "publishTime": config.publish_range,
        "selectType": "title,content",
        "subtype": "",
        "exclusionWords": "",
        "buyer": "",
        "winner": "",
        "agency": "",
        "industry": config.industry,
        "province": config.province,
        "city": "",
        "district": "",
        "buyerClass": "",
        "fileExists": "",
        "price": "",
        "buyerTel": "",
        "winnerTel": "",
    }
    data = post_json(SEARCH_URL, payload, cookie)
    global LAST_FETCH_META
    anti_text = data.get("textVerify")
    anti_img = data.get("imgData")
    LAST_FETCH_META["anti_verify"] = bool(anti_text or anti_img)
    LAST_FETCH_META["anti_verify_text"] = anti_text
    LAST_FETCH_META["source_mode"] = "searchList"
    return data


def fetch_page_with_fallback(cookie: str, config: SearchConfig, page_num: int) -> dict[str, Any]:
    """Try the strict province-filtered query first, then fall back to area post-filtering."""
    primary = fetch_page(cookie, config, page_num)
    primary_list = (primary.get("data") or {}).get("list")
    if primary_list:
        return primary

    if config.province:
        relaxed = dataclasses.replace(config, province="", industry="")
        relaxed_result = fetch_page(cookie, relaxed, page_num)
        relaxed_list = (relaxed_result.get("data") or {}).get("list")
        if relaxed_list:
            return relaxed_result
    return primary


def load_records(cookie: str, config: SearchConfig) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    emit_progress("search_start", max_pages=config.max_pages, page_size=config.page_size)
    for page in range(1, config.max_pages + 1):
        abort_if_requested()
        emit_progress("search_page_start", page=page, max_pages=config.max_pages, collected_records=len(records))
        data = fetch_page_with_fallback(cookie, config, page)
        if (data.get("textVerify") or data.get("imgData")) and not (data.get("data") or {}).get("list"):
            register_captcha_hit(SEARCH_FETCH_STATE, base_cooldown=180.0, max_cooldown=1800.0)
            captcha_meta = extract_captcha_meta_from_search_response(data)
            if captcha_meta:
                write_captcha_image_if_needed(config, captcha_meta)
                LAST_FETCH_META["anti_verify_text"] = captcha_meta.get("text_verify")
            emit_progress(
                "captcha_detected",
                page=page,
                max_pages=config.max_pages,
                text_verify=str((captcha_meta or {}).get("text_verify") or ""),
            )
            if config.captcha_auto_attempts > 0:
                emit_progress("captcha_auto_attempt_start", page=page, max_pages=config.max_pages)
                data = auto_attempt_search_captcha(cookie, config)
                if (data.get("data") or {}).get("list"):
                    clear_captcha_hits(SEARCH_FETCH_STATE)
                    emit_progress("captcha_auto_attempt_success", page=page, max_pages=config.max_pages)
                else:
                    captcha_meta = extract_captcha_meta_from_search_response(data)
                    if captcha_meta:
                        write_captcha_image_if_needed(config, captcha_meta)
                        LAST_FETCH_META["anti_verify_text"] = captcha_meta.get("text_verify")
                    emit_progress("captcha_auto_attempt_failed", page=page, max_pages=config.max_pages)
            elif config.captcha_clicks:
                submit_search_captcha(cookie, config.captcha_clicks)
                data = fetch_page_with_fallback(cookie, config, page)
                if not ((data.get("data") or {}).get("list")) and (data.get("textVerify") or data.get("imgData")):
                    captcha_meta = extract_captcha_meta_from_search_response(data)
                    if captcha_meta:
                        write_captcha_image_if_needed(config, captcha_meta)
                        LAST_FETCH_META["anti_verify_text"] = captcha_meta.get("text_verify")
            if not ((data.get("data") or {}).get("list")) and (data.get("textVerify") or data.get("imgData")):
                resumed = handle_manual_captcha(
                    {
                        "scope": "search_list",
                        "page": page,
                        "max_pages": config.max_pages,
                        "text_verify": str((captcha_meta or {}).get("text_verify") or ""),
                        "url": LOGIN_URL,
                        "message": "列表页触发验证码，等待人工处理后继续。",
                    }
                )
                if resumed:
                    data = fetch_page_with_fallback(cookie, config, page)
            if not ((data.get("data") or {}).get("list")):
                raise SystemExit("searchList returned antiVerify captcha instead of results.")
        clear_captcha_hits(SEARCH_FETCH_STATE)
        if data.get("error_code") not in (0, "0", None):
            raise SystemExit(f"searchList failed on page {page}: {data}")
        payload = data.get("data") or {}
        page_items = payload.get("list") or []
        emit_progress(
            "search_page_loaded",
            page=page,
            max_pages=config.max_pages,
            page_items=len(page_items),
            collected_records=len(records),
        )
        if not page_items:
            break
        for item in page_items:
            abort_if_requested()
            if config.province and str(item.get("area") or "") != config.province:
                continue
            if not matches_keywords(item, config.keywords):
                continue
            record = canonical_record(item)
            if config.fetch_details:
                record = enrich_record_with_detail(record, cookie if not config.input_json else "")
                record = reparse_record_from_detail(record)
            record_id = str(record.get("id") or hashlib.md5(json.dumps(item, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest())
            if record_id in seen_ids:
                continue
            seen_ids.add(record_id)
            records.append(record)
        grouped_count = len(merge_project_groups(group_records(normalize_records_after_detail(records))))
        emit_progress(
            "search_page_done",
            page=page,
            max_pages=config.max_pages,
            collected_records=len(records),
            grouped_projects=grouped_count,
        )
    return records


def probe_search_access(cookie: str, config: SearchConfig) -> dict[str, Any]:
    data = fetch_page_with_fallback(cookie, dataclasses.replace(config, max_pages=1, fetch_details=False), 1)
    payload = data.get("data") or {}
    page_items = payload.get("list") or []
    captcha_meta = extract_captcha_meta_from_search_response(data) or {}
    return {
        "ok": bool(page_items) or not bool(captcha_meta),
        "has_list": bool(page_items),
        "list_count": len(page_items),
        "has_captcha": bool(captcha_meta),
        "text_verify": str(captcha_meta.get("text_verify") or ""),
        "anti_verify": bool(data.get("textVerify") or data.get("imgData") or data.get("antiVerify") not in (None, 0, "0")),
    }


def load_records_from_json(path: str, config: SearchConfig) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    skip_keyword_filter = False
    if isinstance(payload, list):
        items = payload
        if payload and all(isinstance(item, dict) and looks_like_normalized_record(item) for item in payload):
            skip_keyword_filter = True
    elif isinstance(payload, dict):
        items = payload.get("list") or payload.get("data") or []
        if not items and payload.get("projects"):
            items = load_records_from_project_json(path)
            skip_keyword_filter = True
    else:
        items = []
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        item_area = str(item.get("area") or "").strip()
        if config.province and item_area and item_area != config.province:
            continue
        if not skip_keyword_filter and not matches_keywords(item, config.keywords):
            continue
        if looks_like_normalized_record(item):
            record = dict(item)
            if config.fetch_details and record.get("raw"):
                record = enrich_record_with_detail(record, "")
                record = reparse_record_from_detail(record)
        else:
            record = canonical_record(item)
            if config.fetch_details:
                record = enrich_record_with_detail(record, "")
                record = reparse_record_from_detail(record)
        record_id = str(record.get("id") or hashlib.md5(json.dumps(item, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest())
        if record_id in seen_ids:
            continue
        seen_ids.add(record_id)
        records.append(record)
    return records


def load_records_from_urls_json(path: str, config: SearchConfig) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise SystemExit("--input-urls-json expects a JSON array.")
    records: list[dict[str, Any]] = []
    for idx, item in enumerate(payload, start=1):
        if isinstance(item, str):
            item = {"url": item}
        if isinstance(item, dict):
            url = str(item.get("url") or item.get("detailUrl") or item.get("href") or "").strip()
            if not url:
                continue
            title = str(item.get("title") or item.get("project_name") or url)
            raw = dict(item)
            raw.setdefault("id", f"url-{idx}")
            raw.setdefault("url", url)
            raw.setdefault("title", title)
            raw.setdefault("source_url", url)
            raw.setdefault("detail", raw.get("detail") or "")
            records.append(
                {
                    "id": raw["id"],
                    "title": title,
                    "project_key": normalize_project_key(title),
                    "notice_type": str(item.get("notice_type") or item.get("subtype") or "其他"),
                    "subtype": str(item.get("subtype") or ""),
                    "area": item.get("area") or config.province,
                    "city": item.get("city"),
                    "district": item.get("district"),
                    "buyer": clean_entity_name(item.get("buyer")),
                    "winner": clean_entity_name(item.get("winner")),
                    "budget": normalize_money_value(item.get("budget"), str(item.get("budget") or "")),
                    "bid_amount": normalize_money_value(item.get("bid_amount"), str(item.get("bid_amount") or "")),
                    "publish_time": item.get("publish_time"),
                    "bid_open_time": item.get("bid_open_time"),
                    "industry": item.get("industry") or config.industry,
                    "site": item.get("site"),
                    "spider_code": item.get("spider_code"),
                    "masked": False,
                    "money_candidates": [],
                    "label_values": {},
                    "project_name": clean_project_name(item.get("project_name") or title) if (item.get("project_name") or title) else None,
                    "bid_number": str(item.get("bid_number") or "").strip() or None,
                    "raw": raw,
                }
            )
    return records


def load_records_from_project_json(path: str) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not payload.get("projects"):
        raise SystemExit("--input-json project mode expects a collector output with projects[].records.")
    records: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for project in payload.get("projects") or []:
        if not isinstance(project, dict):
            continue
        for record in project.get("records") or []:
            if not isinstance(record, dict):
                continue
            url = str(record.get("raw_detail_url") or (record.get("raw") or {}).get("url") or record.get("id") or "")
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            records.append(record)
    return records


OUTPUT_LIKE_NAME_TOKENS = (
    "output",
    "report",
    "recheck",
    "rescan",
    "rebuilt",
    "regrouped",
    "discover",
    "probe",
    "smoke",
    "debug",
    "latest",
    "summary",
    "check",
    "fixed",
    "cleanup",
    "after_",
)


def is_collector_output_json(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    top_level_keys = set(payload.keys())
    if {"projects", "stats"} & top_level_keys:
        return True
    if "records" in top_level_keys and "record_count" in top_level_keys:
        return True
    if "package_summaries" in top_level_keys:
        return True
    return False


def is_seed_like_json_payload(payload: Any) -> bool:
    if isinstance(payload, list):
        if not payload:
            return False
        if all(isinstance(item, str) for item in payload):
            return True
        if all(isinstance(item, dict) for item in payload):
            return all(any(key in item for key in ("url", "detailUrl", "href")) for item in payload)
        return False
    if isinstance(payload, dict):
        if is_collector_output_json(payload):
            return False
        if isinstance(payload.get("list"), list) or isinstance(payload.get("data"), list):
            return True
    return False


def should_skip_input_dir_text_file(file_path: Path) -> bool:
    name = file_path.name.lower()
    stem = file_path.stem.lower()
    if any(token in stem for token in OUTPUT_LIKE_NAME_TOKENS):
        return True
    if name.endswith(".md"):
        return True
    return False


def should_include_input_dir_json(file_path: Path, payload: Any) -> bool:
    if should_skip_input_dir_text_file(file_path):
        return False
    return is_seed_like_json_payload(payload) or (
        isinstance(payload, dict) and bool(payload.get("projects"))
    )


def record_identity(record: dict[str, Any]) -> str:
    return str(record.get("raw", {}).get("url") or record.get("raw_detail_url") or record.get("id") or "")


def record_needs_backfill(record: dict[str, Any]) -> bool:
    has_control = record.get("budget") is not None
    has_winner = record.get("bid_amount") is not None or record.get("winner") is not None
    has_open = bool(record.get("bid_quotes") or record.get("bid_participants"))
    if not has_control:
        return True
    if not has_winner:
        return True
    if not has_open:
        return True
    return False


def merge_unique_quotes(values: list[float]) -> list[float]:
    merged: list[float] = []
    seen: set[float] = set()
    for value in values:
        rounded = round(float(value), 2)
        if rounded in seen:
            continue
        seen.add(rounded)
        merged.append(float(value))
    return merged


def merge_unique_participants(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[str, float | None]] = set()
    for item in values:
        name = str(item.get("name") or "").strip()
        quote_raw = item.get("quote")
        quote = round(float(quote_raw), 2) if quote_raw is not None else None
        key = (name, quote)
        if key in seen:
            continue
        seen.add(key)
        merged.append(item)
    return merged


def merge_record_pair(primary: dict[str, Any], secondary: dict[str, Any]) -> dict[str, Any]:
    merged = dict(primary)
    merged_raw = dict(primary.get("raw") or {})
    secondary_raw = dict(secondary.get("raw") or {})
    secondary_detail = str(secondary_raw.get("detail") or "")
    primary_detail = str(merged_raw.get("detail") or "")
    secondary_is_shell_detail = "采购联系人" in secondary_detail and "项目所属地区" in secondary_detail
    merged_source_files = sorted(
        {
            str(path).strip()
            for path in [
                *(list(primary.get("source_files") or [])),
                *(list(secondary.get("source_files") or [])),
                merged_raw.get("source_file"),
                secondary_raw.get("source_file"),
            ]
            if str(path or "").strip()
        }
    )
    for key, value in secondary.items():
        if key == "raw":
            continue
        if key == "source_files":
            continue
        if key == "label_values":
            label_values = dict(merged.get("label_values") or {})
            label_values.update(dict(value or {}))
            merged[key] = label_values
            continue
        if key == "money_candidates":
            existing = list(merged.get("money_candidates") or [])
            merged[key] = existing + [v for v in list(value or []) if v not in existing]
            continue
        if key == "bid_quotes":
            merged[key] = merge_unique_quotes(list(merged.get("bid_quotes") or []) + list(value or []))
            continue
        if key == "bid_participants":
            merged[key] = merge_unique_participants(list(merged.get("bid_participants") or []) + list(value or []))
            continue
        current_value = merged.get(key)
        if current_value in (None, "", [], {}):
            if value not in (None, "", [], {}):
                merged[key] = value
            continue
        if key in {"raw_detail_html", "raw_detail_text"}:
            current_len = len(str(current_value or ""))
            new_len = len(str(value or ""))
            if new_len > current_len:
                merged[key] = value
            continue
        if key == "raw_detail_url" and not current_value and value:
            merged[key] = value
            continue
        if key == "title":
            current_len = len(str(current_value or ""))
            new_len = len(str(value or ""))
            if new_len > current_len:
                merged[key] = value
            continue
    for k, v in secondary_raw.items():
        if v in (None, "", [], {}):
            continue
        if k == "detail" and secondary_is_shell_detail and primary_detail:
            continue
        if k == "title" and str(v).strip() == "-西藏招标网" and merged_raw.get("title"):
            continue
        merged_raw[k] = v
    merged["raw"] = merged_raw
    merged["source_files"] = merged_source_files
    return merged


def merge_records_by_identity(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged_map: dict[str, dict[str, Any]] = {}
    ordered: list[str] = []
    for record in records:
        identity = record_identity(record)
        if not identity:
            identity = f"anonymous:{len(ordered)}"
        if identity not in merged_map:
            merged_map[identity] = record
            ordered.append(identity)
            continue
        merged_map[identity] = merge_record_pair(merged_map[identity], record)
    return [merged_map[key] for key in ordered]


def attach_package_metadata(records: list[dict[str, Any]], package_name: str, package_path: str) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for record in records:
        item = dict(record)
        raw = dict(item.get("raw") or {})
        raw.setdefault("package_name", package_name)
        raw.setdefault("package_path", package_path)
        item["raw"] = raw
        package_names = sorted(
            {
                str(name).strip()
                for name in [*(list(item.get("package_names") or [])), package_name]
                if str(name or "").strip()
            }
        )
        package_paths = sorted(
            {
                str(path).strip()
                for path in [*(list(item.get("package_paths") or [])), package_path]
                if str(path or "").strip()
            }
        )
        item["package_names"] = package_names
        item["package_paths"] = package_paths
        enriched.append(item)
    return enriched


def load_records_from_input_dir(path: str, config: SearchConfig) -> list[dict[str, Any]]:
    root = Path(path)
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"--input-dir must be an existing directory: {path}")
    records: list[dict[str, Any]] = []
    for file_path in sorted(root.rglob("*")):
        if not file_path.is_file():
            continue
        suffix = file_path.suffix.lower()
        file_records: list[dict[str, Any]] = []
        if suffix in {".html", ".htm"}:
            detail_record = load_record_from_detail_file(str(file_path), config)
            if detail_record is not None:
                file_records = [detail_record]
            else:
                continue
        elif suffix == ".json":
            try:
                payload = json.loads(file_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not should_include_input_dir_json(file_path, payload):
                continue
            if isinstance(payload, dict) and payload.get("projects"):
                file_records = load_records_from_project_json(str(file_path))
            elif isinstance(payload, list):
                if payload and all(
                    isinstance(item, dict) and any(key in item for key in ("detail", "publishTime", "subtype", "title", "area"))
                    for item in payload
                ):
                    file_records = load_records_from_json(str(file_path), config)
                else:
                    file_records = load_records_from_urls_json(str(file_path), config)
            elif isinstance(payload, dict) and (payload.get("list") or payload.get("data")):
                file_records = load_records_from_json(str(file_path), config)
        elif suffix == ".md":
            continue
        for record in file_records:
            raw = dict(record.get("raw") or {})
            raw.setdefault("source_file", str(file_path))
            record["raw"] = raw
            record["source_files"] = sorted(
                {
                    str(path).strip()
                    for path in [record.get("source_files"), raw.get("source_file")]
                    if str(path or "").strip()
                }
            )
            records.append(record)
    return merge_records_by_identity(records)


def load_records_from_input_dir_batch(path: str, config: SearchConfig) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    root = Path(path)
    if not root.exists() or not root.is_dir():
        raise SystemExit(f"--input-dir must be an existing directory: {path}")
    package_dirs = [item for item in sorted(root.iterdir()) if item.is_dir()]
    if not package_dirs:
        raise SystemExit(f"--input-dir-batch expects immediate subdirectories under: {path}")
    all_records: list[dict[str, Any]] = []
    package_summaries: list[dict[str, Any]] = []
    for package_dir in package_dirs:
        package_records = load_records_from_input_dir(str(package_dir), config)
        package_records = attach_package_metadata(package_records, package_dir.name, str(package_dir.resolve()))
        package_projects = merge_project_groups(group_records(package_records))
        package_summaries.append(
            {
                "package_name": package_dir.name,
                "package_path": str(package_dir.resolve()),
                "record_count": len(package_records),
                "project_group_count": len(package_projects),
            }
        )
        all_records.extend(package_records)
    return merge_records_by_identity(all_records), package_summaries


def looks_like_normalized_record(item: dict[str, Any]) -> bool:
    return any(
        key in item
        for key in (
            "notice_type",
            "project_key",
            "raw_detail_url",
            "raw_detail_html",
            "bid_quotes",
            "bid_participants",
            "label_values",
        )
    )


def extract_urls_from_text(text: str) -> list[str]:
    urls: list[str] = []
    for match in re.finditer(r"https?://[^\s<>'\"]+", text or ""):
        url = match.group(0).rstrip(").,，；;】]")
        urls.append(url)
    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        if url in seen:
            continue
        seen.add(url)
        unique.append(url)
    return unique


def parse_iso_date(text: str) -> dt.date | None:
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def normalize_region_text(text: str) -> str | None:
    value = normalize_loose_text(text or "").strip()
    if not value:
        return None
    value = value.replace("\xa0", " ")
    value = re.sub(r"\s+", "", value)
    value = value.replace("西藏自治区", "").replace("西藏", "")
    return value or None


def normalize_city_name(text: str) -> str | None:
    value = normalize_region_text(text)
    if not value:
        return None
    for city_name in TIBET_CITY_SLUGS:
        if city_name in value:
            return city_name
    return value


def city_slug_from_text(text: str) -> str | None:
    city_name = normalize_city_name(text)
    if not city_name:
        return None
    return TIBET_CITY_SLUGS.get(city_name)


def build_publish_range(recent_days: int) -> str:
    days = max(1, int(recent_days))
    now = int(time.time())
    start = now - days * 24 * 60 * 60
    return f"{start}-{now}"


def publish_range_start_date(publish_range: str) -> dt.date | None:
    if not publish_range:
        return None
    try:
        start = int(str(publish_range).split("-", 1)[0])
    except Exception:
        return None
    return dt.datetime.fromtimestamp(start).date()


def parse_discover_channels(config: SearchConfig) -> list[str]:
    raw = config.discover_channels.strip() or "jzgc"
    parts = [part.strip().strip("/") for part in raw.split(",") if part.strip()]
    return parts or ["jzgc"]


def parse_backfill_discover_channels(config: SearchConfig) -> list[str]:
    raw = config.backfill_discover_channels.strip() or config.discover_channels.strip() or "jzgc"
    parts = [part.strip().strip("/") for part in raw.split(",") if part.strip()]
    return parts or ["jzgc"]


def build_channel_listing_page_url(channel: str, page: int, base_url: str = "") -> str:
    base = base_url.strip() or f"https://xizang.jianyu360.cn/{channel}"
    if page <= 1:
        return base
    return urllib.parse.urljoin(base.rstrip("/") + "/", f"../{channel}_{page}/")


def build_city_listing_page_url(city_slug: str, page: int) -> str:
    if page <= 1:
        return f"https://xizang.jianyu360.cn/{city_slug}/"
    return f"https://xizang.jianyu360.cn/{city_slug}_{page}/"


def build_listing_sources(config: SearchConfig, channels: list[str], city_slugs: list[str] | None = None) -> list[dict[str, str]]:
    sources: list[dict[str, str]] = []
    for channel in channels:
        sources.append(
            {
                "kind": "channel",
                "token": channel,
                "base_url": config.discover_url.strip() or f"https://xizang.jianyu360.cn/{channel}",
            }
        )
    for city_slug in city_slugs or []:
        sources.append(
            {
                "kind": "city",
                "token": city_slug,
                "base_url": build_city_listing_page_url(city_slug, 1),
            }
        )
    return sources


def build_listing_page_url(source: dict[str, str], page: int) -> str:
    if source.get("kind") == "city":
        return build_city_listing_page_url(source["token"], page)
    return build_channel_listing_page_url(source["token"], page, source.get("base_url", ""))


def preferred_city_slugs_from_records(records: list[dict[str, Any]]) -> list[str]:
    slugs: list[str] = []
    for item in records:
        candidates = [
            item.get("city"),
            item.get("district"),
            (item.get("label_values") or {}).get("地区"),
            (item.get("label_values") or {}).get("地市"),
            item.get("title"),
            item.get("project_name"),
            str((item.get("raw") or {}).get("detail") or ""),
        ]
        for candidate in candidates:
            slug = city_slug_from_text(str(candidate or ""))
            if slug and slug not in slugs:
                slugs.append(slug)
    return slugs


def extract_project_tokens(text: str) -> list[str]:
    normalized = normalize_project_key(text)
    if not normalized:
        return []
    tokens = [token for token in re.split(r"[()（）\\-_/、，,;；\\s]+", normalized) if token]
    filtered: list[str] = []
    for token in tokens:
        if len(token) < 2:
            continue
        if token in GENERIC_PROJECT_TOKENS:
            continue
        filtered.append(token)
    return filtered


def load_records_from_area_listing(cookie: str, config: SearchConfig) -> list[dict[str, Any]]:
    channels = parse_discover_channels(config)
    city_slugs = DEFAULT_TIBET_CITY_PAGE_SLUGS if config.province == "西藏" else []
    sources = build_listing_sources(config, channels, city_slugs=city_slugs)
    start_date = publish_range_start_date(config.publish_range)
    records: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    detail_count = 0
    for source in sources:
        for page in range(1, config.max_pages + 1):
            page_url = build_listing_page_url(source, page)
            try:
                html_text = fetch_html(page_url, cookie)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    break
                raise
            page_records = extract_records_from_area_listing_html(html_text, config, page_url, start_date)
            if not page_records:
                break
            stop_due_to_date = False
            for record in page_records:
                url = str(record.get("raw", {}).get("url") or "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                publish_time = record.get("publish_time")
                publish_date = parse_iso_date(str(publish_time)) if publish_time else None
                if start_date and publish_date and publish_date < start_date:
                    stop_due_to_date = True
                    continue
                if config.fetch_details and (config.detail_limit <= 0 or detail_count < config.detail_limit):
                    record = reparse_record_from_detail(enrich_record_with_detail(record, cookie))
                    detail_count += 1
                records.append(record)
            if stop_due_to_date:
                break
    global LAST_FETCH_META
    LAST_FETCH_META["anti_verify"] = False
    LAST_FETCH_META["anti_verify_text"] = None
    LAST_FETCH_META["source_mode"] = "area_listing"
    return records


def extract_records_from_area_listing_html(
    html_text: str, config: SearchConfig, source_url: str, start_date: dt.date | None
) -> list[dict[str, Any]]:
    pattern = re.compile(
        r'<li class="card-bid-item">.*?<a class="item-title[^"]*" title="([^"]+)" href="([^"]+)".*?</a>.*?<span class="item-time">([^<]+)</span>',
        re.IGNORECASE | re.DOTALL,
    )
    records: list[dict[str, Any]] = []
    for idx, match in enumerate(pattern.finditer(html_text), start=1):
        title = html.unescape(match.group(1)).strip()
        href = html.unescape(match.group(2)).strip()
        publish_date_text = normalize_loose_text(match.group(3)).strip()
        publish_date = parse_iso_date(publish_date_text)
        if start_date and publish_date and publish_date < start_date:
            continue
        if not looks_like_building_project(title):
            continue
        url = href if href.startswith("http") else urllib.parse.urljoin(source_url, href)
        raw = {
            "id": f"area-{idx}-{hashlib.md5(url.encode('utf-8')).hexdigest()[:10]}",
            "url": url,
            "title": title,
            "publishTime": publish_date_text,
            "area": config.province,
            "detail": title,
            "source_url": source_url,
        }
        record = canonical_record(raw)
        record["publish_time"] = publish_date_text
        records.append(record)
    return records


def iter_area_listing_records(cookie: str, config: SearchConfig, max_pages: int, start_date: dt.date | None = None) -> list[dict[str, Any]]:
    channels = parse_backfill_discover_channels(config)
    city_slugs = DEFAULT_TIBET_CITY_PAGE_SLUGS if config.province == "西藏" else []
    sources = build_listing_sources(config, channels, city_slugs=city_slugs)
    records: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for source in sources:
        for page in range(1, max_pages + 1):
            page_url = build_listing_page_url(source, page)
            try:
                html_text = fetch_html(page_url, cookie)
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    break
                raise
            for record in extract_records_from_area_listing_html(html_text, config, page_url, start_date):
                url = str(record.get("raw", {}).get("url") or "")
                if not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                records.append(record)
    return records


def build_project_backfill_target(group: list[dict[str, Any]]) -> dict[str, Any]:
    names: list[str] = []
    bid_numbers: list[str] = []
    publish_dates: list[dt.date] = []
    matched_notice_types: set[str] = set()
    for item in group:
        for key in ("project_name", "title"):
            value = clean_project_name(item.get(key))
            if value and value not in names:
                names.append(value)
        bid_number = str(item.get("bid_number") or "").strip()
        if bid_number and bid_number not in bid_numbers:
            bid_numbers.append(bid_number)
        publish_date = parse_iso_date(str(item.get("publish_time") or ""))
        if publish_date:
            publish_dates.append(publish_date)
        notice_type = str(item.get("notice_type") or "").strip()
        if notice_type:
            matched_notice_types.add(notice_type)
    primary_name = names[0] if names else ""
    missing_notice_types = {"招标公告", "开标记录", "中标结果", "中标候选人"} - matched_notice_types
    primary_gap = "generic"
    if "开标记录" in missing_notice_types:
        primary_gap = "missing_open_record"
    elif "中标结果" in missing_notice_types:
        primary_gap = "missing_result"
    elif "招标公告" in missing_notice_types:
        primary_gap = "missing_notice"
    return {
        "primary_name": primary_name,
        "names": names,
        "bid_numbers": bid_numbers,
        "matched_notice_types": sorted(matched_notice_types),
        "missing_notice_types": sorted(missing_notice_types),
        "primary_gap": primary_gap,
        "min_publish_date": min(publish_dates) if publish_dates else None,
        "max_publish_date": max(publish_dates) if publish_dates else None,
    }


def build_project_backfill_queries(target: dict[str, Any]) -> list[str]:
    queries: list[str] = []
    seen: set[str] = set()

    def add(query: Any) -> None:
        text = str(query or "").strip()
        if not text or text in seen:
            return
        seen.add(text)
        queries.append(text)

    for bid_number in target.get("bid_numbers") or []:
        add(bid_number)
    primary_name = str(target.get("primary_name") or "").strip()
    if primary_name:
        add(primary_name)
        compact_name = normalize_project_key(primary_name)
        add(compact_name)
        tokens = [token for token in re.split(r"[()（）\-_/、，,;；\s]+", compact_name) if len(token) >= 3]
        if len(tokens) >= 2:
            add("".join(tokens[:2]))
            add(" ".join(tokens[:2]))
        if len(tokens) >= 3:
            add("".join(tokens[:3]))
        for token in tokens[:6]:
            add(token)
    missing_notice_types = set(target.get("missing_notice_types") or [])
    if primary_name:
        if "开标记录" in missing_notice_types:
            add(f"{primary_name} 开标记录")
            add(f"{primary_name} 开标")
            add(f"{primary_name} 开标一览表")
        if "招标公告" in missing_notice_types:
            add(f"{primary_name} 招标公告")
            add(f"{primary_name} 招标文件")
            add(f"{primary_name} 采购公告")
        if "中标结果" in missing_notice_types:
            add(f"{primary_name} 中标结果")
            add(f"{primary_name} 中标公告")
            add(f"{primary_name} 成交结果")
        if "中标候选人" in missing_notice_types:
            add(f"{primary_name} 中标候选人")
            add(f"{primary_name} 中标候选人公示")
            add(f"{primary_name} 成交候选人公示")
    for bid_number in target.get("bid_numbers") or []:
        if "开标记录" in missing_notice_types:
            add(f"{bid_number} 开标记录")
        if "招标公告" in missing_notice_types:
            add(f"{bid_number} 招标公告")
        if "中标结果" in missing_notice_types:
            add(f"{bid_number} 中标结果")
        if "中标候选人" in missing_notice_types:
            add(f"{bid_number} 中标候选人")
    return queries


def record_matches_project_backfill(record: dict[str, Any], target: dict[str, Any]) -> bool:
    title_norm = normalize_project_key(str(record.get("title") or record.get("project_name") or ""))
    record_bid_number = record.get("bid_number")
    for bid_number in target.get("bid_numbers") or []:
        if bid_number_match_score(record_bid_number, bid_number) >= 2:
            return True
    for project_name in target.get("names") or []:
        project_norm = normalize_project_key(project_name)
        if not project_norm or not title_norm:
            continue
        if project_overlap_score(project_norm, title_norm) >= 2:
            return True
    return False


def record_coarse_matches_project(record: dict[str, Any], target: dict[str, Any]) -> bool:
    title_norm = normalize_project_key(str(record.get("title") or record.get("project_name") or ""))
    if not title_norm:
        return False
    for project_name in target.get("names") or []:
        project_norm = normalize_project_key(project_name)
        if not project_norm:
            continue
        if project_overlap_score(project_norm, title_norm) >= 1:
            return True
        project_tokens = [token for token in re.split(r"[()（）\-_/、，,;；]+", project_norm) if len(token) >= 3]
        hit_count = sum(1 for token in project_tokens[:6] if token in title_norm)
        if hit_count >= 2:
            return True
        rich_tokens = extract_project_tokens(project_norm)
        if rich_tokens:
            exact_hits = sum(1 for token in rich_tokens if token in title_norm)
            long_hits = sum(1 for token in rich_tokens if len(token) >= 4 and token in title_norm)
            if exact_hits >= 3 or long_hits >= 2:
                return True
    return False


def record_publish_date_near_target(record: dict[str, Any], target: dict[str, Any], gap: str) -> bool:
    publish_date = parse_iso_date(str(record.get("publish_time") or ""))
    if not publish_date:
        return True
    min_date = target.get("min_publish_date")
    max_date = target.get("max_publish_date")
    if not isinstance(min_date, dt.date) and not isinstance(max_date, dt.date):
        return True
    before_days = 180
    after_days = 30
    if gap == "missing_open_record":
        before_days = 20
        after_days = 5
    elif gap == "missing_result":
        before_days = 2
        after_days = 20
    elif gap == "missing_notice":
        before_days = 120
        after_days = 3
    lower_bound = (min_date or max_date) - dt.timedelta(days=before_days)
    upper_bound = (max_date or min_date) + dt.timedelta(days=after_days)
    return lower_bound <= publish_date <= upper_bound


def targeted_area_backfill_records(cookie: str, config: SearchConfig, seed_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not seed_records:
        return []
    grouped = group_records(seed_records)
    channels = parse_backfill_discover_channels(config)
    collected: list[dict[str, Any]] = []
    seen_urls: set[str] = {
        str(item.get("raw", {}).get("url") or item.get("raw_detail_url") or "")
        for item in seed_records
        if str(item.get("raw", {}).get("url") or item.get("raw_detail_url") or "")
    }
    verify_budget = max(0, int(config.detail_verify_backfill_limit))
    recovered_notice_records = 0
    recovered_open_records = 0
    recovered_result_records = 0
    targeted_project_count = 0
    targeted_missing_open_project_count = 0
    for _, group in grouped.items():
        target = build_project_backfill_target(group)
        if not target.get("primary_name"):
            continue
        targeted_project_count += 1
        target_hits: list[dict[str, Any]] = []
        target_seen: set[str] = set()
        matched_notice_types = set(target.get("matched_notice_types") or [])
        wanted_notice_types = set(target.get("missing_notice_types") or [])
        primary_gap = str(target.get("primary_gap") or "generic")
        if primary_gap == "missing_open_record":
            targeted_missing_open_project_count += 1
        preferred_channels = list(channels)
        if primary_gap == "missing_open_record":
            preferred_channels = [channel for channel in channels if channel in {"jzgc", "fwcg"}] or list(channels)
        elif primary_gap == "missing_result":
            preferred_channels = [channel for channel in channels if channel in {"jzgc", "fwcg", "xzbg"}] or list(channels)
        elif primary_gap == "missing_notice":
            preferred_channels = [channel for channel in channels if channel in {"jzgc", "xzbg"}] or list(channels)
        preferred_city_slugs = preferred_city_slugs_from_records(group)
        city_page_slugs = []
        if config.province == "西藏":
            city_page_slugs = preferred_city_slugs + [
                slug for slug in DEFAULT_TIBET_CITY_PAGE_SLUGS
                if slug not in preferred_city_slugs
            ]
        source_specs = build_listing_sources(config, preferred_channels, city_slugs=city_page_slugs)
        min_seed_date = target.get("min_publish_date")
        max_seed_date = target.get("max_publish_date")
        for source in source_specs:
            older_miss_streak = 0
            for page in range(1, max(1, config.backfill_pages) + 1):
                page_url = build_listing_page_url(source, page)
                try:
                    html_text = fetch_html(page_url, cookie)
                except Exception:
                    break
                page_records = extract_records_from_area_listing_html(html_text, config, page_url, None)
                if not page_records:
                    break
                if min_seed_date:
                    page_dates = [
                        parse_iso_date(str(item.get("publish_time") or ""))
                        for item in page_records
                        if parse_iso_date(str(item.get("publish_time") or ""))
                    ]
                    if page_dates:
                        newest_page_date = max(page_dates)
                        oldest_page_date = min(page_dates)
                        if newest_page_date < (min_seed_date - dt.timedelta(days=120)):
                            break
                        if max_seed_date and oldest_page_date > (max_seed_date + dt.timedelta(days=14)):
                            older_miss_streak += 1
                            if older_miss_streak >= 3:
                                continue
                for record in page_records:
                    url = str(record.get("raw", {}).get("url") or "")
                    if not url or url in seen_urls or url in target_seen:
                        continue
                    if not record_publish_date_near_target(record, target, primary_gap):
                        continue
                    if not (record_matches_project_backfill(record, target) or record_coarse_matches_project(record, target)):
                        continue
                    if primary_gap == "missing_open_record":
                        notice_type = str(record.get("notice_type") or "")
                        detail_blob = str((record.get("raw") or {}).get("detail") or "")
                        if notice_type != "开标记录" and "开标" not in detail_blob and "投标报价" not in detail_blob:
                            continue
                    target_hits.append(record)
                    target_seen.add(url)
                if wanted_notice_types and target_hits:
                    hit_notice_types = {
                        str(item.get("notice_type") or "")
                        for item in target_hits
                        if str(item.get("notice_type") or "")
                    }
                    if wanted_notice_types.intersection(hit_notice_types):
                        break
        for record in target_hits:
            if verify_budget <= 0:
                break
            url = str(record.get("raw", {}).get("url") or "")
            if not url or url in seen_urls:
                continue
            verify_budget -= 1
            try:
                enriched = reparse_record_from_detail(enrich_record_with_detail(record, cookie))
            except Exception:
                continue
            if not record_matches_project_backfill(enriched, target):
                continue
            seen_urls.add(url)
            notice_type = str(enriched.get("notice_type") or "")
            if notice_type == "开标记录":
                recovered_open_records += 1
            elif notice_type == "招标公告":
                recovered_notice_records += 1
            elif notice_type in {"中标结果", "中标候选人"}:
                recovered_result_records += 1
            collected.append(enriched)
    LAST_FETCH_META["targeted_backfill_projects"] = targeted_project_count
    LAST_FETCH_META["targeted_backfill_missing_open_projects"] = targeted_missing_open_project_count
    LAST_FETCH_META["targeted_backfill_recovered_open_records"] = recovered_open_records
    LAST_FETCH_META["targeted_backfill_recovered_notice_records"] = recovered_notice_records
    LAST_FETCH_META["targeted_backfill_recovered_result_records"] = recovered_result_records
    return collected


def groups_should_merge(primary: list[dict[str, Any]], secondary: list[dict[str, Any]]) -> bool:
    primary_names = [
        str(item.get("project_name") or item.get("title") or "").strip()
        for item in primary
        if item.get("project_name") or item.get("title")
    ]
    secondary_names = [
        str(item.get("project_name") or item.get("title") or "").strip()
        for item in secondary
        if item.get("project_name") or item.get("title")
    ]
    primary_bids = [item.get("bid_number") for item in primary if item.get("bid_number")]
    secondary_bids = [item.get("bid_number") for item in secondary if item.get("bid_number")]
    for a in primary_bids:
        for b in secondary_bids:
            if bid_number_match_score(a, b) >= 2:
                return True
    best_score = 0
    best_token_overlap = 0
    for a in primary_names:
        for b in secondary_names:
            best_score = max(best_score, project_overlap_score(a, b))
            left_tokens = set(extract_project_tokens(a))
            right_tokens = set(extract_project_tokens(b))
            token_overlap = len(left_tokens & right_tokens)
            best_token_overlap = max(best_token_overlap, token_overlap)
            if best_score >= 3:
                return True
    notice_types = {str(item.get("notice_type") or "") for item in [*primary, *secondary]}
    if best_token_overlap >= 2 and best_score >= 2 and len(notice_types) >= 2:
        return True
    return False


def merge_project_groups(groups: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    ordered = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))
    merged: list[tuple[str, list[dict[str, Any]]]] = []
    for key, group in ordered:
        placed = False
        for idx, (_, existing_group) in enumerate(merged):
            if groups_should_merge(existing_group, group):
                existing_urls = {
                    str(item.get("raw", {}).get("url") or item.get("raw_detail_url") or item.get("id") or "")
                    for item in existing_group
                }
                for item in group:
                    item_url = str(item.get("raw", {}).get("url") or item.get("raw_detail_url") or item.get("id") or "")
                    if item_url and item_url in existing_urls:
                        continue
                    existing_group.append(item)
                    if item_url:
                        existing_urls.add(item_url)
                merged[idx] = (merged[idx][0], existing_group)
                placed = True
                break
        if not placed:
            merged.append((key, list(group)))
    return {key: group for key, group in merged}


def backfill_project_records_via_search(cookie: str, config: SearchConfig, seed_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not seed_records:
        return []
    grouped = group_records(seed_records)
    collected: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for _, group in grouped.items():
        target = build_project_backfill_target(group)
        queries = build_project_backfill_queries(target)
        if not queries:
            continue
        for query in queries[:8]:
            search_config = dataclasses.replace(
                config,
                keywords=query,
                province="",
                industry="",
                max_pages=min(max(config.max_pages, 1), 2),
                fetch_details=False,
            )
            try:
                search_records = load_records(cookie, search_config)
            except SystemExit as exc:
                if "antiVerify captcha" in str(exc):
                    LAST_FETCH_META["backfill_search_skipped"] = LAST_FETCH_META.get("backfill_search_skipped", 0) + 1
                    continue
                raise
            except Exception:
                LAST_FETCH_META["backfill_search_skipped"] = LAST_FETCH_META.get("backfill_search_skipped", 0) + 1
                continue
            for record in search_records:
                url = str(record.get("raw", {}).get("url") or record.get("raw_detail_url") or "")
                if not url or url in seen_urls:
                    continue
                if not record_matches_project_backfill(record, target):
                    continue
                seen_urls.add(url)
                collected.append(record)
            if collected:
                break
    return collected


def backfill_project_records_via_precise_search(cookie: str, config: SearchConfig, seed_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not seed_records:
        return []
    grouped = group_records(seed_records)
    collected: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for _, group in grouped.items():
        target = build_project_backfill_target(group)
        queries = build_project_backfill_queries(target)
        if not queries:
            continue
        target_notice_types = set(target.get("matched_notice_types") or [])
        missing_notice_types = set(target.get("missing_notice_types") or [])
        group_hits: list[dict[str, Any]] = []
        group_hit_notice_types: set[str] = set()
        for query in queries[:6]:
            search_config = dataclasses.replace(
                config,
                keywords=query,
                province="",
                industry="",
                max_pages=1,
                page_size=max(20, config.page_size),
                fetch_details=False,
            )
            try:
                search_records = load_records(cookie, search_config)
            except SystemExit as exc:
                if "antiVerify captcha" in str(exc):
                    LAST_FETCH_META["backfill_search_skipped"] = LAST_FETCH_META.get("backfill_search_skipped", 0) + 1
                    continue
                raise
            except Exception:
                LAST_FETCH_META["backfill_search_skipped"] = LAST_FETCH_META.get("backfill_search_skipped", 0) + 1
                continue
            for record in search_records:
                url = str(record.get("raw", {}).get("url") or record.get("raw_detail_url") or "")
                if not url or url in seen_urls:
                    continue
                if not record_matches_project_backfill(record, target):
                    continue
                notice_type = str(record.get("notice_type") or "")
                if notice_type in target_notice_types and notice_type not in missing_notice_types:
                    continue
                seen_urls.add(url)
                group_hits.append(record)
                if notice_type:
                    group_hit_notice_types.add(notice_type)
            if missing_notice_types and missing_notice_types.issubset(group_hit_notice_types):
                break
        collected.extend(group_hits)
    return collected


def backfill_project_records(cookie: str, config: SearchConfig, seed_records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not seed_records:
        return seed_records
    all_records = list(seed_records)
    if config.backfill_pages <= 0:
        return all_records
    should_try_search_backfill = config.search_backfill_mode == "on"
    if config.search_backfill_mode == "auto":
        should_try_search_backfill = config.source_mode != "area_listing"
    if should_try_search_backfill:
        precise_search_backfilled = backfill_project_records_via_precise_search(cookie, config, seed_records)
        all_records = merge_records_by_url(all_records, precise_search_backfilled)
        search_backfilled = backfill_project_records_via_search(cookie, config, all_records)
        all_records = merge_records_by_url(all_records, search_backfilled)
    targeted_area_hits = targeted_area_backfill_records(cookie, config, all_records)
    all_records = merge_records_by_url(all_records, targeted_area_hits)
    if config.backfill_pages <= config.max_pages:
        return all_records
    by_group = group_records(seed_records)
    long_range_records = iter_area_listing_records(cookie, dataclasses.replace(config, fetch_details=False), config.backfill_pages, None)
    seen_urls = {str(item.get("raw", {}).get("url") or "") for item in all_records}
    detail_verified_candidates: list[dict[str, Any]] = []
    detail_verify_seen: set[str] = set()
    verify_budget = max(0, int(config.detail_verify_backfill_limit))
    coarse_candidates: list[tuple[str, dict[str, Any]]] = []
    direct_match_count = 0
    for _, group in by_group.items():
        target = build_project_backfill_target(group)
        if not target.get("primary_name"):
            continue
        for record in long_range_records:
            url = str(record.get("raw", {}).get("url") or "")
            if not url or url in seen_urls:
                continue
            if not record_matches_project_backfill(record, target):
                if not record_coarse_matches_project(record, target):
                    continue
                coarse_candidates.append((target, record))
                continue
            seen_urls.add(url)
            all_records.append(record)
            direct_match_count += 1
    for target, record in coarse_candidates:
        if verify_budget <= 0:
            break
        url = str(record.get("raw", {}).get("url") or "")
        if not url or url in seen_urls or url in detail_verify_seen:
            continue
        detail_verify_seen.add(url)
        verify_budget -= 1
        try:
            enriched = reparse_record_from_detail(enrich_record_with_detail(record, cookie))
        except Exception:
            continue
        if not record_matches_project_backfill(enriched, target):
            continue
        detail_verified_candidates.append(enriched)
        seen_urls.add(url)
    all_records = merge_records_by_url(all_records, detail_verified_candidates)
    LAST_FETCH_META["backfill_direct_matches"] = direct_match_count
    LAST_FETCH_META["backfill_coarse_candidates"] = len(coarse_candidates)
    LAST_FETCH_META["backfill_detail_verified"] = len(detail_verified_candidates)
    return all_records


def merge_records_by_url(primary: list[dict[str, Any]], secondary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in [*primary, *secondary]:
        url = str(item.get("raw", {}).get("url") or item.get("raw_detail_url") or item.get("id") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        merged.append(item)
    return merged


def records_from_followup_seed_urls(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    generated: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    original_seed_count = 0
    related_seed_count = 0
    for item in records:
        seeds: list[dict[str, Any]] = []
        original_url = str(item.get("original_url") or "").strip()
        if original_url:
            seeds.append({"url": original_url, "source": "original_url"})
        for link in item.get("related_links") or []:
            if not isinstance(link, dict):
                continue
            url = str(link.get("url") or "").strip()
            title = str(link.get("title") or "").strip()
            if not url:
                continue
            seeds.append({"url": url, "title": title, "source": "related_link"})
        project_name = item.get("project_name") or item.get("title") or ""
        bid_number = item.get("bid_number")
        source_notice_type = item.get("notice_type")
        for seed in seeds:
            if not isinstance(seed, dict):
                continue
            url = str(seed.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            if seed.get("source") == "related_link":
                related_seed_count += 1
            else:
                original_seed_count += 1
            generated.append(
                {
                    "id": f"followup-{hashlib.md5(url.encode('utf-8')).hexdigest()[:12]}",
                    "title": str(seed.get("title") or project_name or "").strip(),
                    "project_key": normalize_project_key(str(project_name or seed.get("title") or "")),
                    "notice_type": classify_notice_from_text(str(seed.get("title") or ""), "", str(seed.get("title") or "")),
                    "subtype": "",
                    "area": item.get("area"),
                    "city": item.get("city"),
                    "district": item.get("district"),
                    "buyer": item.get("buyer"),
                    "winner": None,
                    "project_name": clean_project_name(project_name) if project_name else None,
                    "bid_number": bid_number,
                    "budget": None,
                    "bid_amount": None,
                    "publish_time": None,
                    "bid_open_time": None,
                    "industry": item.get("industry"),
                    "site": item.get("site"),
                    "spider_code": item.get("spider_code"),
                    "masked": False,
                    "money_candidates": [],
                    "label_values": {},
                    "attachments": [],
                    "attachment_links": [],
                    "original_url": None,
                    "related_links": [],
                    "raw": {
                        "id": f"followup-{hashlib.md5(url.encode('utf-8')).hexdigest()[:12]}",
                        "url": url,
                        "title": str(seed.get("title") or project_name or "").strip(),
                        "detail": str(seed.get("title") or project_name or "").strip(),
                        "source": seed.get("source"),
                        "source_project_name": clean_project_name(project_name) if project_name else None,
                        "source_bid_number": bid_number,
                        "source_notice_type": source_notice_type,
                    },
                }
            )
    LAST_FETCH_META["followup_seed_generated"] = len(generated)
    LAST_FETCH_META["followup_seed_original_url_generated"] = original_seed_count
    LAST_FETCH_META["followup_seed_related_link_generated"] = related_seed_count
    return generated


def is_followup_shell_record(record: dict[str, Any]) -> bool:
    source = (record.get("raw") or {}).get("source")
    if source not in {"original_url", "related_link"}:
        return False
    return not record_has_detail_payload(record)


def followup_record_matches_origin(record: dict[str, Any]) -> bool:
    raw = record.get("raw") or {}
    source = raw.get("source")
    if source != "related_link":
        return True
    source_project_name = clean_project_name(raw.get("source_project_name"))
    source_bid_number = raw.get("source_bid_number")
    target_names = [source_project_name] if source_project_name else []
    target_bid_numbers = [source_bid_number] if source_bid_number else []
    if not target_names and not target_bid_numbers:
        return False
    target = {
        "names": target_names,
        "bid_numbers": target_bid_numbers,
    }
    if record_matches_project_backfill(record, target):
        return True
    return record_coarse_matches_project(record, target)


def extract_seed_title_from_line(line: str, url: str) -> str:
    text = line.strip()
    if "|" in text:
        parts = [part.strip(" -\t") for part in text.split("|")]
        url_idx = next((idx for idx, part in enumerate(parts) if url in part), -1)
        if url_idx > 0:
            candidate = parts[url_idx - 1]
            if candidate:
                return candidate
    text = text.replace(url, "").strip(" -\t|")
    return text


def load_records_from_markdown(path: str, config: SearchConfig) -> list[dict[str, Any]]:
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in text.splitlines():
        if "http" not in line:
            continue
        for url in extract_urls_from_text(line):
            title = extract_seed_title_from_line(line, url)
            if url in seen:
                continue
            seen.add(url)
            records.append(
                {
                    "id": f"md-{len(records)+1}",
                    "title": title,
                    "project_key": normalize_project_key(title),
                    "notice_type": classify_notice(title),
                    "subtype": "",
                    "area": config.province,
                    "city": None,
                    "district": None,
                    "buyer": None,
                    "winner": None,
                    "budget": None,
                    "bid_amount": None,
                    "publish_time": None,
                    "bid_open_time": None,
                    "industry": config.industry,
                    "site": None,
                    "spider_code": None,
                    "masked": False,
                    "money_candidates": [],
                    "label_values": {},
                    "project_name": clean_project_name(title) if title else None,
                    "bid_number": None,
                    "raw": {
                        "id": f"md-{len(records)+1}",
                        "url": url,
                        "title": title,
                        "source_url": url,
                        "detail": line,
                    },
                }
            )
    return records


def load_records_from_html(path: str, config: SearchConfig) -> list[dict[str, Any]]:
    html_text = Path(path).read_text(encoding="utf-8", errors="ignore")
    records: list[dict[str, Any]] = []
    seen: set[str] = set()
    pattern = re.compile(
        r'<a[^>]+href=["\']([^"\']*?/jybx/[^"\']+)["\'][^>]*(?:title=["\']([^"\']+)["\'])?[^>]*>(.*?)</a>',
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(html_text):
        href = html.unescape(match.group(1))
        title = html.unescape(re.sub(r"<[^>]+>", "", match.group(2) or match.group(3) or "")).strip()
        if not href or href in seen:
            continue
        seen.add(href)
        url = href if href.startswith("http") else urllib.parse.urljoin("https://xizang.jianyu360.cn", href)
        records.append(
            {
                "id": f"html-{len(records)+1}",
                "title": title,
                "project_key": normalize_project_key(title),
                "notice_type": classify_notice(title),
                "subtype": "",
                "area": config.province,
                "city": None,
                "district": None,
                "buyer": None,
                "winner": None,
                "budget": None,
                "bid_amount": None,
                "publish_time": None,
                "bid_open_time": None,
                "industry": config.industry,
                "site": None,
                "spider_code": None,
                "masked": False,
                "money_candidates": [],
                "label_values": {},
                "project_name": clean_project_name(title) if title else None,
                "bid_number": None,
                "raw": {
                    "id": f"html-{len(records)+1}",
                    "url": url,
                    "title": title,
                    "source_url": href,
                    "detail": title,
                },
            }
        )
    return records


def group_records(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[build_record_group_key(record)].append(record)
    return grouped


def looks_like_building_project(text: str) -> bool:
    text = str(text or "")
    return any(
        token in text
        for token in (
            "项目",
            "工程",
            "房",
            "楼",
            "改造",
            "建设",
            "维修",
            "园",
            "用房",
            "周转房",
            "幼儿园",
            "监理",
            "施工",
            "EPC",
            "总承包",
            "安置点",
            "棚户",
            "办公楼",
            "业务技术用房",
            "基础设施",
            "医院",
            "学校",
        )
    )


def select_candidate_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped = merge_project_groups(group_records(records))
    selected: list[dict[str, Any]] = []
    for _, group in grouped.items():
        if len(group) < 2:
            continue
        sample_title = next((str(item.get("project_name") or item.get("title") or "") for item in group if item.get("project_name") or item.get("title")), "")
        if not looks_like_building_project(sample_title):
            continue
        notice_blob = "\n".join(str(item.get("title") or "") for item in group)
        if not any(token in notice_blob for token in ("中标", "成交", "结果", "候选人", "开标", "招标公告", "采购公告")):
            continue
        selected.extend(group)
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in selected:
        url = str(item.get("raw", {}).get("url") or item.get("raw_detail_url") or item.get("id") or "")
        if url in seen:
            continue
        seen.add(url)
        deduped.append(item)
    return deduped


def project_status(group: list[dict[str, Any]]) -> dict[str, Any]:
    has_notice = any(item["notice_type"] == "招标公告" or "招标公告" in item["title"] or "招标文件" in item["title"] for item in group)
    has_open = any(item["notice_type"] == "开标记录" or looks_like_open_record_text(item) for item in group)
    has_result = any(
        item["notice_type"] in {"中标结果", "中标候选人"}
        or any(marker in item["title"] for marker in ("中标结果", "中标公告", "成交结果", "结果公示", "成交公告", "中标候选人公示", "成交候选人公示"))
        for item in group
    )
    has_all = has_notice and has_open and has_result
    return {
        "has_notice": has_notice,
        "has_open": has_open,
        "has_result": has_result,
        "usable": has_all,
        "count": len(group),
    }


def build_project_summary(project_key: str, group: list[dict[str, Any]], allow_core_without_notice: bool = True) -> dict[str, Any]:
    notice_record = choose_preferred_record(group, "招标公告")
    open_record = choose_preferred_record(group, "开标记录")
    result_record = choose_preferred_record(group, "中标结果") or choose_preferred_record(group, "中标候选人")
    anchor_scope_key = str((result_record or open_record or notice_record or {}).get("procurement_scope_key") or "default")
    anchor_section_key = str((result_record or open_record or notice_record or {}).get("bid_section_key") or "default")

    def scoped_group(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        scoped = [
            item for item in items
            if (item.get("procurement_scope_key") or "default") == anchor_scope_key
            and (item.get("bid_section_key") or "default") == anchor_section_key
        ]
        return scoped or items

    def pick_project_title() -> str:
        candidates = [
            notice_record,
            open_record,
            result_record,
            *group,
        ]
        for item in candidates:
            if not item:
                continue
            for key in ("project_name", "title"):
                raw_value = str(item.get(key) or "").strip()
                value = clean_project_name(raw_value) if raw_value else None
                if value and looks_like_project_name(value) and "西藏招标网" not in value and "招标结果" not in value:
                    return value
        bid_number_text = bid_number or project_key
        return clean_project_name(bid_number_text) or project_key

    working_group = scoped_group(group)
    control_price = None
    control_price_source = "missing"
    bid_number = None
    scoped_open_record = choose_preferred_record([item for item in working_group if item.get("notice_type") == "开标记录"], "开标记录") if working_group else open_record
    scoped_notice_record = choose_preferred_record([item for item in working_group if item.get("notice_type") == "招标公告"], "招标公告") if working_group else notice_record
    if scoped_open_record and scoped_open_record.get("budget") is not None:
        control_price = scoped_open_record["budget"]
        control_price_source = scoped_open_record.get("budget_source") or "missing"
    elif scoped_notice_record and scoped_notice_record.get("budget") is not None:
        control_price = scoped_notice_record["budget"]
        control_price_source = scoped_notice_record.get("budget_source") or "missing"
    else:
        chosen_budget_record = choose_consistent_price(working_group, "budget", None)
        if chosen_budget_record:
            control_price = chosen_budget_record.get("budget")
            control_price_source = chosen_budget_record.get("budget_source") or "missing"
    for item in [result_record, notice_record, open_record, *working_group]:
        if item and item.get("bid_number"):
            bid_number = item["bid_number"]
            break

    winning_price = None
    winning_company = None
    winning_price_source = "missing"
    winning_company_source = "missing"
    bid_quotes: list[float] = []
    bid_participants: list[dict[str, Any]] = []
    attachments: list[str] = []
    attachment_links: list[dict[str, str]] = []
    original_urls: list[str] = []
    related_links: list[dict[str, str]] = []
    followup_seed_urls: list[dict[str, str]] = []
    scoped_result_record = choose_preferred_record([item for item in working_group if item.get("notice_type") in {"中标结果", "中标候选人"}], "中标结果") or choose_preferred_record([item for item in working_group if item.get("notice_type") in {"中标结果", "中标候选人"}], "中标候选人")
    if scoped_result_record:
        winning_price = scoped_result_record.get("bid_amount")
        winning_company = scoped_result_record.get("winner")
        winning_price_source = scoped_result_record.get("bid_amount_source") or "missing"
        winning_company_source = scoped_result_record.get("winner_source") or "missing"
    if winning_price is None:
        chosen_result_record = choose_consistent_price(working_group, "bid_amount", control_price)
        if chosen_result_record:
            winning_price = chosen_result_record.get("bid_amount")
            winning_price_source = chosen_result_record.get("bid_amount_source") or "missing"
    if winning_company is None:
        for item in working_group:
            if item.get("winner"):
                winning_company = item["winner"]
                winning_company_source = item.get("winner_source") or "missing"
                break
    if scoped_open_record:
        bid_quotes = list(scoped_open_record.get("bid_quotes") or [])
        bid_participants = list(scoped_open_record.get("bid_participants") or [])
        if not bid_quotes and scoped_open_record.get("bid_participants"):
            bid_quotes = [float(item["quote"]) for item in scoped_open_record["bid_participants"] if item.get("quote") is not None]
    if not bid_quotes:
        for item in working_group:
            if item.get("bid_quotes"):
                bid_quotes = list(item["bid_quotes"])
                bid_participants = list(item.get("bid_participants") or [])
                break
            if item.get("bid_participants"):
                bid_participants = list(item["bid_participants"])
                bid_quotes = [float(p["quote"]) for p in item["bid_participants"] if p.get("quote") is not None]
                break
    for item in group:
        for name in item.get("attachments") or []:
            if name not in attachments:
                attachments.append(name)
        for link in item.get("attachment_links") or []:
            if not isinstance(link, dict):
                continue
            if link not in attachment_links:
                attachment_links.append(link)
        original_url = str(item.get("original_url") or "").strip()
        if original_url and original_url not in original_urls:
            original_urls.append(original_url)
        for link in item.get("related_links") or []:
            if not isinstance(link, dict):
                continue
            if link not in related_links:
                related_links.append(link)
    followup_seen: set[str] = set()
    for url in original_urls:
        if url and url not in followup_seen:
            followup_seen.add(url)
            followup_seed_urls.append({"url": url, "source": "original_url"})
    for link in related_links:
        url = str(link.get("url") or "").strip()
        title = str(link.get("title") or "").strip()
        if not url or url in followup_seen:
            continue
        followup_seen.add(url)
        followup_seed_urls.append({"url": url, "title": title, "source": "related_link"})

    issues: list[str] = []
    if notice_record is None:
        issues.append("缺招标公告/招标文件")
    if open_record is None:
        issues.append("缺开标记录")
    if result_record is None:
        issues.append("缺中标结果/候选人")
    if control_price is None:
        issues.append("缺控制价")
    if winning_price is None:
        issues.append("缺中标价")
    if winning_company is None:
        issues.append("缺中标单位")
    if not bid_quotes:
        issues.append("缺全体报价")

    has_required_prices = winning_price is not None and control_price is not None and bool(bid_quotes)
    has_required_core_fields = (
        control_price is not None
        and winning_price is not None
        and winning_company is not None
        and bool(bid_quotes)
    )
    can_analyze_core = has_required_prices and (allow_core_without_notice or notice_record is not None)
    file_complete = (
        notice_record is not None
        and open_record is not None
        and result_record is not None
        and has_required_core_fields
    )
    source_files = sorted(
        {
            str(source_file).strip()
            for item in group
            for source_file in (list(item.get("source_files") or []) or [str((item.get("raw") or {}).get("source_file") or "").strip()])
            if str(source_file or "").strip()
        }
    )
    package_names = sorted(
        {
            str(package_name).strip()
            for item in group
            for package_name in (list(item.get("package_names") or []) or [str((item.get("raw") or {}).get("package_name") or "").strip()])
            if str(package_name or "").strip()
        }
    )
    package_paths = sorted(
        {
            str(package_path).strip()
            for item in group
            for package_path in (list(item.get("package_paths") or []) or [str((item.get("raw") or {}).get("package_path") or "").strip()])
            if str(package_path or "").strip()
        }
    )
    source_urls = sorted(
        {
            str(item.get("raw_detail_url") or (item.get("raw") or {}).get("url") or "").strip()
            for item in group
            if str(item.get("raw_detail_url") or (item.get("raw") or {}).get("url") or "").strip()
        }
    )
    notice_types_present = sorted(
        {
            str(item.get("notice_type") or "").strip()
            for item in group
            if str(item.get("notice_type") or "").strip()
        }
    )
    missing_notice_types: list[str] = []
    if notice_record is None:
        missing_notice_types.append("招标公告")
    if open_record is None:
        missing_notice_types.append("开标记录")
    if result_record is None:
        missing_notice_types.append("中标结果")
    record_type_counts: dict[str, int] = {}
    for item in group:
        notice_type = str(item.get("notice_type") or "其他")
        record_type_counts[notice_type] = record_type_counts.get(notice_type, 0) + 1
    winning_down_rate = calc_down_rate(control_price, winning_price)
    avg_quote = (sum(bid_quotes) / len(bid_quotes)) if bid_quotes else None
    avg_down_rate = calc_down_rate(control_price, avg_quote) if avg_quote is not None else None
    max_quote = max(bid_quotes) if bid_quotes else None
    min_quote = min(bid_quotes) if bid_quotes else None
    max_down_rate = calc_down_rate(control_price, min_quote) if min_quote is not None else None
    min_down_rate = calc_down_rate(control_price, max_quote) if max_quote is not None else None
    if file_complete:
        readiness_stage = "strict_complete"
        primary_blocker = None
        next_action = "none"
    elif can_analyze_core:
        readiness_stage = "core_ready_missing_notice"
        primary_blocker = "missing_notice"
        next_action = "recover_notice"
    elif result_record is not None and open_record is None:
        readiness_stage = "result_only_blocked_by_open"
        primary_blocker = "missing_open_record"
        next_action = "recover_open_record"
    elif open_record is not None and result_record is None:
        readiness_stage = "open_only_blocked_by_result"
        primary_blocker = "missing_result"
        next_action = "recover_result"
    elif notice_record is not None and open_record is None and result_record is None:
        readiness_stage = "notice_only"
        primary_blocker = "missing_open_record"
        next_action = "recover_open_record"
    else:
        readiness_stage = "partial_unready"
        if open_record is None:
            primary_blocker = "missing_open_record"
            next_action = "recover_open_record"
        elif result_record is None:
            primary_blocker = "missing_result"
            next_action = "recover_result"
        elif notice_record is None:
            primary_blocker = "missing_notice"
            next_action = "recover_notice"
        elif control_price is None:
            primary_blocker = "missing_control_price"
            next_action = "recover_notice_or_open"
        elif winning_price is None:
            primary_blocker = "missing_winning_price"
            next_action = "recover_result"
        elif winning_company is None:
            primary_blocker = "missing_winning_company"
            next_action = "recover_result"
        else:
            primary_blocker = "missing_bid_quotes"
            next_action = "recover_open_record"

    return {
        "project_key": project_key,
        "project_title": pick_project_title(),
        "record_count": len(group),
        "package_names": package_names,
        "package_name_count": len(package_names),
        "package_paths": package_paths,
        "package_path_count": len(package_paths),
        "source_files": source_files,
        "source_file_count": len(source_files),
        "source_urls": source_urls,
        "source_url_count": len(source_urls),
        "notice_types_present": notice_types_present,
        "missing_notice_types": missing_notice_types,
        "record_type_counts": record_type_counts,
        "bid_number": bid_number,
        "control_price": control_price,
        "control_price_source": control_price_source,
        "winning_price": winning_price,
        "winning_price_source": winning_price_source,
        "winning_company": winning_company,
        "winning_company_source": winning_company_source,
        "bid_quotes": bid_quotes,
        "bid_participants": bid_participants,
        "attachments": attachments,
        "attachment_count": len(attachments),
        "attachment_links": attachment_links,
        "attachment_link_count": len(attachment_links),
        "original_urls": original_urls,
        "original_url_count": len(original_urls),
        "related_links": related_links,
        "related_link_count": len(related_links),
        "followup_seed_urls": followup_seed_urls,
        "followup_seed_count": len(followup_seed_urls),
        "bid_quote_count": len(bid_quotes),
        "bid_quote_min": min_quote,
        "bid_quote_max": max_quote,
        "bid_quote_avg": avg_quote,
        "winning_down_rate": winning_down_rate,
        "avg_down_rate": avg_down_rate,
        "max_down_rate": max_down_rate,
        "min_down_rate": min_down_rate,
        "notice_record_id": notice_record.get("id") if notice_record else None,
        "open_record_id": open_record.get("id") if open_record else None,
        "result_record_id": result_record.get("id") if result_record else None,
        "notice_record_url": notice_record.get("raw_detail_url") if notice_record else None,
        "open_record_url": open_record.get("raw_detail_url") if open_record else None,
        "result_record_url": result_record.get("raw_detail_url") if result_record else None,
        "has_notice": notice_record is not None,
        "has_open": open_record is not None,
        "has_result": result_record is not None,
        "has_control_price": control_price is not None,
        "has_winning_price": winning_price is not None,
        "has_winning_company": winning_company is not None,
        "has_bid_quotes": bool(bid_quotes),
        "has_required_core_fields": has_required_core_fields,
        "file_complete": file_complete,
        "can_analyze_core": can_analyze_core,
        "core_ready_reason": None if not can_analyze_core else ("full_three_files" if file_complete else "open_plus_result"),
        "readiness_stage": readiness_stage,
        "primary_blocker": primary_blocker,
        "next_action": next_action,
        "issues": issues,
    }


def calc_down_rate(control_price: float | None, amount: float | None) -> float | None:
    if not control_price or not amount or control_price <= 0:
        return None
    return (control_price - amount) / control_price * 100.0


def count_core_projects(records: list[dict[str, Any]], allow_core_without_notice: bool = True) -> int:
    grouped = merge_project_groups(group_records(normalize_records_after_detail(records)))
    return sum(
        1
        for project_key, group in grouped.items()
        if build_project_summary(project_key, group, allow_core_without_notice).get("can_analyze_core")
    )


def build_project_audit_meta(projects: list[dict[str, Any]]) -> dict[str, Any]:
    complete_projects: list[dict[str, Any]] = []
    core_ready_incomplete_projects: list[dict[str, Any]] = []
    incomplete_projects: list[dict[str, Any]] = []
    missing_notice_projects: list[dict[str, Any]] = []
    missing_open_projects: list[dict[str, Any]] = []
    missing_result_projects: list[dict[str, Any]] = []
    missing_control_price_projects: list[dict[str, Any]] = []
    missing_winning_price_projects: list[dict[str, Any]] = []
    missing_winning_company_projects: list[dict[str, Any]] = []
    missing_bid_quotes_projects: list[dict[str, Any]] = []
    missing_core_field_projects: list[dict[str, Any]] = []
    issue_counts: dict[str, int] = {}

    def pack_project(item: dict[str, Any]) -> dict[str, Any]:
        summary = item["summary"]
        return {
            "project_key": item["project_key"],
            "project_title": summary["project_title"],
            "record_count": summary["record_count"],
            "file_complete": summary["file_complete"],
            "can_analyze_core": summary["can_analyze_core"],
            "core_ready_reason": summary["core_ready_reason"],
            "readiness_stage": summary["readiness_stage"],
            "primary_blocker": summary["primary_blocker"],
            "next_action": summary["next_action"],
            "notice_types_present": summary["notice_types_present"],
            "missing_notice_types": summary["missing_notice_types"],
            "issues": summary["issues"],
            "control_price": summary["control_price"],
            "winning_price": summary["winning_price"],
            "winning_company": summary["winning_company"],
            "bid_quote_count": summary["bid_quote_count"],
            "attachment_count": summary.get("attachment_count", 0),
            "attachments": summary.get("attachments", []),
            "attachment_link_count": summary.get("attachment_link_count", 0),
            "original_url_count": summary.get("original_url_count", 0),
            "related_link_count": summary.get("related_link_count", 0),
            "followup_seed_count": summary.get("followup_seed_count", 0),
            "source_file_count": summary["source_file_count"],
            "source_url_count": summary["source_url_count"],
        }

    for item in projects:
        summary = item["summary"]
        packed = pack_project(item)
        if summary["file_complete"]:
            complete_projects.append(packed)
        else:
            incomplete_projects.append(packed)
        if summary["can_analyze_core"] and not summary["file_complete"]:
            core_ready_incomplete_projects.append(packed)
        if not summary["has_notice"]:
            missing_notice_projects.append(packed)
        if not summary["has_open"]:
            missing_open_projects.append(packed)
        if not summary["has_result"]:
            missing_result_projects.append(packed)
        if not summary["has_control_price"]:
            missing_control_price_projects.append(packed)
        if not summary["has_winning_price"]:
            missing_winning_price_projects.append(packed)
        if not summary["has_winning_company"]:
            missing_winning_company_projects.append(packed)
        if not summary["has_bid_quotes"]:
            missing_bid_quotes_projects.append(packed)
        if not summary["has_required_core_fields"]:
            missing_core_field_projects.append(packed)
        for issue in summary["issues"]:
            issue_counts[issue] = issue_counts.get(issue, 0) + 1

    return {
        "complete_projects": complete_projects,
        "core_ready_incomplete_projects": core_ready_incomplete_projects,
        "incomplete_projects": incomplete_projects,
        "missing_notice_projects": missing_notice_projects,
        "missing_open_projects": missing_open_projects,
        "missing_result_projects": missing_result_projects,
        "missing_control_price_projects": missing_control_price_projects,
        "missing_winning_price_projects": missing_winning_price_projects,
        "missing_winning_company_projects": missing_winning_company_projects,
        "missing_bid_quotes_projects": missing_bid_quotes_projects,
        "missing_core_field_projects": missing_core_field_projects,
        "issue_counts": dict(sorted(issue_counts.items(), key=lambda kv: (-kv[1], kv[0]))),
    }


def derive_customer_json_path(output_path: str) -> str:
    path = Path(output_path)
    if path.suffix.lower() == ".json":
        return str(path.with_name(f"{path.stem}_客户版.json"))
    return str(path.with_name(f"{path.name}_客户版.json"))


def yes_no(value: bool) -> str:
    return "是" if value else "否"


def readiness_stage_to_cn(value: str | None) -> str:
    mapping = {
        "strict_complete": "三类文件完整",
        "core_ready_missing_notice": "核心数据齐全但缺公告",
        "result_only_blocked_by_open": "已有结果缺开标",
        "open_only_blocked_by_result": "已有开标缺结果",
        "notice_only": "仅有公告",
        "partial_unready": "数据未齐",
    }
    return mapping.get(str(value or ""), "未知")


def blocker_to_cn(value: str | None) -> str:
    mapping = {
        "missing_notice": "缺招标公告",
        "missing_open_record": "缺开标记录",
        "missing_result": "缺中标结果",
        "missing_control_price": "缺控制价",
        "missing_winning_price": "缺中标价",
        "missing_winning_company": "缺中标单位",
        "missing_bid_quotes": "缺全体报价",
        "none": "无",
        "": "无",
        None: "无",
    }
    return mapping.get(value, str(value or "无"))


def build_customer_project(item: dict[str, Any]) -> dict[str, Any]:
    summary = item.get("summary") or {}
    participants = summary.get("bid_participants") or []
    quote_list: list[dict[str, Any]] = []
    if participants:
        for participant in participants:
            if not isinstance(participant, dict):
                continue
            quote_list.append(
                {
                    "单位名称": str(participant.get("company") or participant.get("name") or "").strip() or "未识别单位",
                    "报价": participant.get("quote"),
                }
            )
    else:
        for index, quote in enumerate(summary.get("bid_quotes") or [], start=1):
            quote_list.append({"单位名称": f"报价单位{index}", "报价": quote})

    return {
        "项目名称": summary.get("project_title") or item.get("project_key") or "",
        "项目编号": summary.get("bid_number") or "",
        "当前状态": readiness_stage_to_cn(summary.get("readiness_stage")),
        "主要问题": blocker_to_cn(summary.get("primary_blocker")),
        "是否核心数据齐全": yes_no(bool(summary.get("can_analyze_core"))),
        "是否三类文件完整": yes_no(bool(summary.get("file_complete"))),
        "已有文件类型": list(summary.get("notice_types_present") or []),
        "缺失项": list(summary.get("issues") or []),
        "控制价": summary.get("control_price"),
        "中标价": summary.get("winning_price"),
        "中标单位": summary.get("winning_company") or "",
        "报价家数": summary.get("bid_quote_count", 0),
        "最高报价": summary.get("bid_quote_max"),
        "最低报价": summary.get("bid_quote_min"),
        "平均报价": summary.get("bid_quote_avg"),
        "中标下浮率": summary.get("winning_down_rate"),
        "最高下浮率": summary.get("max_down_rate"),
        "最低下浮率": summary.get("min_down_rate"),
        "平均下浮率": summary.get("avg_down_rate"),
        "各单位报价": quote_list,
    }


def build_customer_json(output: dict[str, Any]) -> dict[str, Any]:
    meta = output.get("meta") or {}
    projects = output.get("projects") or []
    return {
        "说明": "这是给客户查看的简化结果，只保留中文字段、状态和核心数字。",
        "搜索条件": {
            "关键词": meta.get("keywords") or "",
            "地区": meta.get("province") or "",
            "时间范围": meta.get("publish_range") or "",
        },
        "统计": {
            "抓取记录数": meta.get("record_count", 0),
            "归并项目数": meta.get("project_count", 0),
            "核心数据齐全项目数": meta.get("core_analyzable_project_count", 0),
            "三类文件完整项目数": meta.get("file_complete_project_count", 0),
        },
        "项目列表": [build_customer_project(item) for item in projects],
    }


def build_markdown_report(output: dict[str, Any]) -> str:
    projects = output["projects"]
    meta = output["meta"]
    audit = meta.get("audit", {})
    lines: list[str] = []
    lines.append("# 剑鱼项目采集报告")
    lines.append("")
    lines.append(f"- 关键词: {meta['keywords']}")
    lines.append(f"- 地区: {meta['province']}")
    lines.append(f"- 记录数: {meta['record_count']}")
    lines.append(f"- 项目数: {meta['project_count']}")
    lines.append(f"- 文件完整项目数: {sum(1 for item in projects if item['summary']['file_complete'])}")
    lines.append(f"- 核心可分析项目数: {sum(1 for item in projects if item['summary']['can_analyze_core'])}")
    readiness_counts: dict[str, int] = {}
    blocker_counts: dict[str, int] = {}
    for item in projects:
        readiness = str(item["summary"].get("readiness_stage") or "")
        blocker = str(item["summary"].get("primary_blocker") or "")
        if readiness:
            readiness_counts[readiness] = readiness_counts.get(readiness, 0) + 1
        if blocker:
            blocker_counts[blocker] = blocker_counts.get(blocker, 0) + 1
    if readiness_counts:
        lines.append(f"- 就绪阶段分布: {' | '.join(f'{k}:{v}' for k, v in sorted(readiness_counts.items()))}")
    if blocker_counts:
        lines.append(f"- 主要阻塞分布: {' | '.join(f'{k}:{v}' for k, v in sorted(blocker_counts.items()))}")
    lines.append(f"- 发现源: {meta.get('source_mode')}")
    if meta.get("backfill_direct_matches") is not None:
        lines.append(f"- 回补直接命中: {meta.get('backfill_direct_matches', 0)}")
        lines.append(f"- 回补粗匹配候选: {meta.get('backfill_coarse_candidates', 0)}")
        lines.append(f"- 回补详情核验补回: {meta.get('backfill_detail_verified', 0)}")
    if meta.get("targeted_backfill_projects") is not None:
        lines.append(
            f"- 定向回补项目数: {meta.get('targeted_backfill_projects', 0)} "
            f"(缺开标记录 {meta.get('targeted_backfill_missing_open_projects', 0)})"
        )
        lines.append(
            f"- 定向回补补回: 开标记录 {meta.get('targeted_backfill_recovered_open_records', 0)} | "
            f"招标公告 {meta.get('targeted_backfill_recovered_notice_records', 0)} | "
            f"结果类 {meta.get('targeted_backfill_recovered_result_records', 0)}"
        )
    if meta.get("anti_verify"):
        lines.append(f"- 验证码阻塞: 是 ({meta.get('anti_verify_text') or '未知'})")
    else:
        lines.append("- 验证码阻塞: 否")
    if audit.get("issue_counts"):
        lines.append(f"- 缺失问题分布: {' | '.join(f'{key}:{value}' for key, value in audit['issue_counts'].items())}")
    lines.append("")
    lines.append("| 项目 | 文件完整 | 核心可分析 | 阶段 | 主要阻塞 | 核心依据 | 控制价 | 中标价 | 中标下浮率 | 报价家数 | 附件数 | 原文数 | 追踪种子数 | 缺失项 |")
    lines.append("|---|---:|---:|---|---|---|---:|---:|---:|---:|---:|---:|---:|---|")
    for item in projects:
        summary = item["summary"]
        down = summary.get("winning_down_rate")
        missing = "、".join(summary["issues"]) if summary["issues"] else ""
        core_reason = summary.get("core_ready_reason") or ""
        readiness = summary.get("readiness_stage") or ""
        blocker = summary.get("primary_blocker") or ""
        control_price_text = "" if summary["control_price"] is None else f"{summary['control_price']:.2f}"
        winning_price_text = "" if summary["winning_price"] is None else f"{summary['winning_price']:.2f}"
        down_text = "" if down is None else f"{down:.4f}%"
        lines.append(
            f"| {summary['project_title']} | "
            f"{'Y' if summary['file_complete'] else 'N'} | "
            f"{'Y' if summary['can_analyze_core'] else 'N'} | "
            f"{readiness} | "
            f"{blocker} | "
            f"{core_reason} | "
            f"{control_price_text} | "
            f"{winning_price_text} | "
            f"{down_text} | "
            f"{summary['bid_quote_count']} | "
            f"{summary.get('attachment_count', 0)} | "
            f"{summary.get('original_url_count', 0)} | "
            f"{summary.get('followup_seed_count', 0)} | "
            f"{missing} |"
        )
    lines.append("")
    lines.append("## 可分析项目")
    lines.append("")
    analyzable = [item for item in projects if item["summary"]["can_analyze_core"]]
    if not analyzable:
        lines.append("- 无")
    else:
        for item in analyzable:
            s = item["summary"]
            win_down = s.get("winning_down_rate")
            avg_quote = s["bid_quote_avg"]
            avg_down = s.get("avg_down_rate")
            max_down = s.get("max_down_rate")
            min_down = s.get("min_down_rate")
            win_down_text = "" if win_down is None else f"{win_down:.4f}%"
            avg_quote_text = "" if avg_quote is None else f"{avg_quote:.2f}"
            avg_down_text = "" if avg_down is None else f"{avg_down:.4f}%"
            bid_quote_max_text = "" if s["bid_quote_max"] is None else f"{s['bid_quote_max']:.2f}"
            bid_quote_min_text = "" if s["bid_quote_min"] is None else f"{s['bid_quote_min']:.2f}"
            max_down_text = "" if max_down is None else f"{max_down:.4f}%"
            min_down_text = "" if min_down is None else f"{min_down:.4f}%"
            lines.append(f"### {s['project_title']}")
            lines.append(f"- 控制价: {s['control_price']:.2f}")
            lines.append(f"- 中标价: {s['winning_price']:.2f}")
            lines.append(f"- 中标单位: {s['winning_company'] or ''}")
            lines.append(f"- 中标下浮率: {win_down_text}")
            lines.append(f"- 报价家数: {s['bid_quote_count']}")
            lines.append(f"- 记录数: {s['record_count']}")
            lines.append(f"- 来源项目包数: {s['package_name_count']}")
            lines.append(f"- 来源项目包: {', '.join(s['package_names']) if s['package_names'] else ''}")
            lines.append(f"- 已具备公告类型: {', '.join(s['notice_types_present']) if s['notice_types_present'] else ''}")
            lines.append(f"- 缺失公告类型: {', '.join(s['missing_notice_types']) if s['missing_notice_types'] else ''}")
            lines.append(f"- 最高报价: {bid_quote_max_text}")
            lines.append(f"- 最低下浮率: {min_down_text}")
            lines.append(f"- 最低报价: {bid_quote_min_text}")
            lines.append(f"- 最高下浮率: {max_down_text}")
            lines.append(f"- 平均报价: {avg_quote_text}")
            lines.append(f"- 平均下浮率: {avg_down_text}")
            lines.append(f"- 来源文件数: {s['source_file_count']}")
            lines.append(f"- 来源链接数: {s['source_url_count']}")
            lines.append(f"- 附件数: {s.get('attachment_count', 0)}")
            lines.append(f"- 附件链接数: {s.get('attachment_link_count', 0)}")
            lines.append(f"- 原文链接数: {s.get('original_url_count', 0)}")
            lines.append(f"- 关联链接数: {s.get('related_link_count', 0)}")
            lines.append(f"- 追踪种子数: {s.get('followup_seed_count', 0)}")
            lines.append(f"- 开标记录: {s['open_record_url'] or ''}")
            lines.append(f"- 结果公告: {s['result_record_url'] or ''}")
            lines.append(f"- 招标公告: {s['notice_record_url'] or ''}")
            if s.get("attachments"):
                lines.append(f"- 附件: {', '.join(s['attachments'])}")
            if s.get("original_urls"):
                lines.append(f"- 原文链接: {', '.join(s['original_urls'])}")
            if s.get("attachment_links"):
                attachment_link_texts = [f"{item.get('name', '')} {item.get('url', '')}".strip() for item in s["attachment_links"]]
                lines.append(f"- 附件链接: {', '.join(attachment_link_texts)}")
            if s.get("related_links"):
                related_link_texts = [f"{item.get('title', '')} {item.get('url', '')}".strip() for item in s["related_links"][:10]]
                lines.append(f"- 关联链接: {', '.join(related_link_texts)}")
            if s.get("followup_seed_urls"):
                followup_seed_texts = [f"{item.get('source', '')} {item.get('title', '')} {item.get('url', '')}".strip() for item in s["followup_seed_urls"][:20]]
                lines.append(f"- 追踪种子: {', '.join(followup_seed_texts)}")
            if s["source_files"]:
                lines.append("- 来源文件:")
                for source_file in s["source_files"]:
                    lines.append(f"  - {source_file}")
            if s["package_paths"]:
                lines.append("- 来源项目包目录:")
                for package_path in s["package_paths"]:
                    lines.append(f"  - {package_path}")
            if s["source_urls"]:
                lines.append("- 来源链接:")
                for source_url in s["source_urls"]:
                    lines.append(f"  - {source_url}")
            lines.append("")
    lines.append("## 完整性审计")
    lines.append("")
    lines.append(f"- 严格完整项目数: {len(audit.get('complete_projects', []))}")
    lines.append(f"- 核心可分析但文件不完整项目数: {len(audit.get('core_ready_incomplete_projects', []))}")
    lines.append(f"- 缺招标公告项目数: {len(audit.get('missing_notice_projects', []))}")
    lines.append(f"- 缺开标记录项目数: {len(audit.get('missing_open_projects', []))}")
    lines.append(f"- 缺中标结果项目数: {len(audit.get('missing_result_projects', []))}")
    lines.append(f"- 缺核心字段项目数: {len(audit.get('missing_core_field_projects', []))}")
    lines.append("")

    def append_project_list(title: str, key: str) -> None:
        lines.append(f"### {title}")
        items = audit.get(key, [])
        if not items:
            lines.append("- 无")
            lines.append("")
            return
        for item in items:
            issues_text = "、".join(item.get("issues") or [])
            lines.append(
                f"- {item['project_title']} | 记录数 {item['record_count']} | 报价家数 {item['bid_quote_count']} | 缺失 {issues_text}"
            )
        lines.append("")

    append_project_list("严格完整项目", "complete_projects")
    append_project_list("核心可分析但文件不完整项目", "core_ready_incomplete_projects")
    append_project_list("缺招标公告项目", "missing_notice_projects")
    append_project_list("缺开标记录项目", "missing_open_projects")
    append_project_list("缺中标结果项目", "missing_result_projects")
    append_project_list("缺核心字段项目", "missing_core_field_projects")
    return "\n".join(lines)


def write_json(path: str, payload: Any) -> None:
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def write_customer_json(output_path: str, output: dict[str, Any], customer_json_path: str = "") -> str:
    target_path = customer_json_path or derive_customer_json_path(output_path)
    write_json(target_path, build_customer_json(output))
    return target_path


def normalize_records_after_detail(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for record in records:
        raw = record.get("raw", {}) or {}
        detail_html = str(record.get("raw_detail_html") or "")
        title = record.get("title") or raw.get("html_title") or raw.get("title") or record.get("project_name") or ""
        if detail_html:
            parsed = parse_detail_html(detail_html)
            if parsed.get("project_name") and looks_like_project_name(parsed.get("project_name")):
                title = parsed["project_name"]
        detail_text = str(record.get("raw_detail_text") or raw.get("detail") or "")
        if not title:
            project_name = extract_by_alias_map(extract_label_value_map(detail_text), ("项目名称",))
            if project_name and looks_like_project_name(project_name):
                title = project_name
        record["project_key"] = normalize_project_key(str(title))
        if title:
            record["title"] = title
        normalized.append(record)
    return normalized


def record_has_detail_payload(record: dict[str, Any]) -> bool:
    return bool(
        record.get("raw_detail_html")
        or record.get("raw_detail_text")
        or record.get("detail_html")
        or record.get("detail_text")
    )


def should_fetch_detail_for_batch(record: dict[str, Any], project_summary: dict[str, Any] | None) -> bool:
    if not record_needs_backfill(record):
        return False
    if project_summary is None:
        return True
    notice_type = str(record.get("notice_type") or "")
    has_win = bool(project_summary.get("winning_price")) and bool(project_summary.get("winning_company"))
    has_control = bool(project_summary.get("control_price"))
    has_quotes = bool(project_summary.get("bid_quote_count"))
    blocker = str(project_summary.get("primary_blocker") or "")
    if notice_type in {"中标结果", "中标候选人"}:
        return not has_win
    if notice_type == "招标公告":
        return not has_control
    if notice_type == "开标记录":
        return has_win and not has_quotes
    if notice_type == "其他":
        return (not has_control) or (has_win and not has_quotes)
    return False


def detail_fetch_priority(record: dict[str, Any]) -> tuple[int, int]:
    title = str(record.get("title") or "")
    detail = str((record.get("raw") or {}).get("detail") or "")
    notice_type = str(record.get("notice_type") or "")
    if notice_type == "中标结果" or any(token in title for token in ("中标结果", "中标公告", "成交结果", "结果公示", "成交公告")):
        return (0, 0)
    if notice_type == "中标候选人" or "中标候选人" in title or "成交候选人" in title:
        return (1, 0)
    if notice_type == "开标记录" or "开标记录" in title or ("投标报价" in detail and "投标人" in detail):
        return (2, 0)
    if notice_type == "招标公告":
        return (3, 0)
    return (4, 0)


def run_precise_project_capture(config: SearchConfig) -> int:
    query = config.precise_project_query.strip()
    if not query:
        raise SystemExit("--precise-project-query is required for precise project capture mode.")
    cookie = load_cookie(config)
    search_config = dataclasses.replace(
        config,
        keywords=query,
        province="",
        industry="",
        publish_range="",
        max_pages=1,
        page_size=max(20, config.page_size),
        fetch_details=False,
    )
    records = load_records(cookie, search_config)
    records = [record for record in records if record_matches_project_backfill(record, {"names": [query], "bid_numbers": []}) or project_overlap_score(query, str(record.get("title") or record.get("project_name") or "")) >= 2]
    for record in records:
        title = str(record.get("title") or "").strip()
        project_name = str(record.get("project_name") or "").strip()
        if title and looks_like_project_name(clean_project_name(title)):
            record["title"] = title
        if project_name and not looks_like_project_name(project_name):
            cleaned_from_title = clean_project_name(title)
            if cleaned_from_title and looks_like_project_name(cleaned_from_title):
                record["project_name"] = cleaned_from_title
    records = merge_records_by_url([], records)
    base_records = [canonical_record(record.get("raw") or record) for record in records]
    for idx, record in enumerate(records):
        base = base_records[idx]
        if record.get("title"):
            base["title"] = record.get("title")
        if record.get("project_name"):
            base["project_name"] = record.get("project_name")
        if record.get("notice_type"):
            base["notice_type"] = record.get("notice_type")
        if record.get("raw"):
            base["raw"] = dict(record.get("raw") or {})
    hydrated: list[dict[str, Any]] = []
    detail_count = 0
    for idx, record in enumerate(sorted(records, key=detail_fetch_priority)):
        original_record = dict(record)
        base_record = next(
            (
                item for item in base_records
                if str((item.get("raw") or {}).get("url") or item.get("id") or "")
                == str((record.get("raw") or {}).get("url") or record.get("id") or "")
            ),
            canonical_record(record.get("raw") or record),
        )
        if config.detail_limit > 0 and detail_count >= config.detail_limit:
            hydrated.append(base_record)
            continue
        enriched = reparse_record_from_detail(enrich_record_with_detail(record, cookie))
        if original_record.get("notice_type") and enriched.get("notice_type") == "其他":
            enriched["notice_type"] = original_record.get("notice_type")
        if original_record.get("title") and str(enriched.get("title") or "").strip() in {"", "-西藏招标网"}:
            enriched["title"] = original_record.get("title")
        if original_record.get("project_name") and not looks_like_project_name(enriched.get("project_name")):
            enriched["project_name"] = original_record.get("project_name")
        raw = dict(enriched.get("raw") or {})
        original_raw = dict(original_record.get("raw") or {})
        if original_raw.get("detail") and ("采购联系人" in str(enriched.get("raw_detail_text") or "") or not enriched.get("bid_quotes")):
            raw["detail"] = original_raw.get("detail")
            enriched["raw"] = raw
            enriched = reparse_record_from_detail(enriched)
            if original_record.get("notice_type") and enriched.get("notice_type") == "其他":
                enriched["notice_type"] = original_record.get("notice_type")
            if original_record.get("title") and str(enriched.get("title") or "").strip() in {"", "-西藏招标网"}:
                enriched["title"] = original_record.get("title")
            if original_record.get("project_name") and not looks_like_project_name(enriched.get("project_name")):
                enriched["project_name"] = original_record.get("project_name")
        merged = merge_record_pair(base_record, enriched)
        if original_record.get("notice_type") and merged.get("notice_type") == "其他":
            merged["notice_type"] = original_record.get("notice_type")
        if original_record.get("title") and str(merged.get("title") or "").strip() in {"", "-西藏招标网"}:
            merged["title"] = original_record.get("title")
        if original_record.get("project_name") and not looks_like_project_name(merged.get("project_name")):
            merged["project_name"] = original_record.get("project_name")
        hydrated.append(merged)
        detail_count += 1
    hydrated = normalize_records_after_detail(hydrated)
    deduped_hydrated: list[dict[str, Any]] = []
    seen_precise_urls: set[str] = set()
    for record in hydrated:
        url = str((record.get("raw") or {}).get("url") or record.get("raw_detail_url") or record.get("id") or "")
        if url and url in seen_precise_urls:
            continue
        if url:
            seen_precise_urls.add(url)
        deduped_hydrated.append(record)
    groups = merge_project_groups(group_records(deduped_hydrated))
    project_summaries = [build_project_summary(project_key, group, config.allow_core_without_notice) for project_key, group in groups.items()]
    output = {
        "meta": {
            "keywords": config.keywords,
            "province": config.province,
            "industry": config.industry,
            "publish_range": config.publish_range,
            "page_size": config.page_size,
            "max_pages": config.max_pages,
            "record_count": len(deduped_hydrated),
            "project_count": len(project_summaries),
            "usable_project_count": sum(1 for item in project_summaries if item.get("file_complete")),
            "file_complete_project_count": sum(1 for item in project_summaries if item.get("file_complete")),
            "core_analyzable_project_count": sum(1 for item in project_summaries if item.get("can_analyze_core")),
            "source_mode": "precise_project_query",
            "precise_project_query": query,
        },
        "projects": [
            {
                "project_key": project_key,
                "status": project_status(group),
                "summary": build_project_summary(project_key, group, config.allow_core_without_notice),
                "records": group,
            }
            for project_key, group in groups.items()
        ],
    }
    write_json(config.output, output)
    customer_json_path = write_customer_json(config.output, output, config.customer_json)
    output["meta"]["customer_json_path"] = customer_json_path
    write_json(config.output, output)
    if config.report_md:
        report_lines = [f"# 精准项目抓取结果", "", f"- 查询词: {query}", ""]
        for project in output["projects"]:
            summary = project["summary"]
            report_lines.append(f"## {summary.get('project_title')}")
            report_lines.append(f"- 文件类型: {', '.join(summary.get('notice_types_present') or [])}")
            report_lines.append(f"- 缺失项: {', '.join(summary.get('issues') or []) or '无'}")
            report_lines.append(f"- 控制价: {summary.get('control_price')}")
            report_lines.append(f"- 中标价: {summary.get('winning_price')}")
            report_lines.append(f"- 中标单位: {summary.get('winning_company')}")
            report_lines.append(f"- 报价家数: {summary.get('bid_quote_count')}")
            report_lines.append("")
        Path(config.report_md).write_text("\n".join(report_lines), encoding="utf-8")
    print(json.dumps(output["meta"], ensure_ascii=False, indent=2))
    return 0


def run_collection(config: SearchConfig) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    package_summaries: list[dict[str, Any]] = []
    source_mode = None
    anti_verify_meta = {
        "source_mode": None,
        "anti_verify": False,
        "anti_verify_text": None,
    }
    try:
        if config.input_urls_json:
            source_mode = "input_urls_json"
            records = load_records_from_urls_json(config.input_urls_json, dataclasses.replace(config, industry=""))
        elif config.input_dir:
            if config.input_dir_batch:
                source_mode = "input_dir_batch"
                records, package_summaries = load_records_from_input_dir_batch(config.input_dir, dataclasses.replace(config, industry=""))
            else:
                source_mode = "input_dir"
                records = load_records_from_input_dir(config.input_dir, dataclasses.replace(config, industry=""))
        elif config.input_html:
            source_mode = "input_html"
            records = load_records_from_html(config.input_html, dataclasses.replace(config, industry=""))
        elif config.input_md:
            source_mode = "input_md"
            records = load_records_from_markdown(config.input_md, dataclasses.replace(config, industry=""))
        elif config.input_json:
            source_mode = "input_json"
            records = load_records_from_json(config.input_json, dataclasses.replace(config, industry=""))
        else:
            if config.source_mode == "area_listing":
                cookie = load_cookie(config, required=False)
                source_mode = "area_listing"
                discovery_config = dataclasses.replace(config, industry="", fetch_details=False)
                records = load_records_from_area_listing(cookie, discovery_config)
                if config.fetch_details:
                    seed_records = [
                        item for item in records
                        if looks_like_building_project(str(item.get("project_name") or item.get("title") or ""))
                    ]
                    candidate_records = backfill_project_records(cookie, dataclasses.replace(config, industry=""), seed_records)
                    candidate_records = merge_records_by_url(seed_records, candidate_records)
                    records = []
                    detail_count = 0
                    for record in sorted(candidate_records, key=detail_fetch_priority):
                        if not record_needs_backfill(record):
                            records.append(record)
                            continue
                        if config.detail_limit > 0 and detail_count >= config.detail_limit:
                            records.append(record)
                            continue
                        records.append(reparse_record_from_detail(enrich_record_with_detail(record, cookie)))
                        detail_count += 1
            else:
                cookie = load_cookie(config)
                source_mode = "searchList"
                try:
                    records = load_records(cookie, dataclasses.replace(config, industry="", fetch_details=False))
                except SystemExit as exc:
                    if "antiVerify captcha" not in str(exc) or config.source_mode == "search":
                        raise
                    source_mode = "area_listing"
                    cookie = load_cookie(config, required=False)
                    discovery_config = dataclasses.replace(config, industry="", fetch_details=False)
                    records = load_records_from_area_listing(cookie, discovery_config)
                    if config.fetch_details:
                        seed_records = [
                            item for item in records
                            if looks_like_building_project(str(item.get("project_name") or item.get("title") or ""))
                        ]
                        candidate_records = backfill_project_records(cookie, dataclasses.replace(config, industry=""), seed_records)
                        candidate_records = merge_records_by_url(seed_records, candidate_records)
                        records = []
                        detail_count = 0
                        for record in sorted(candidate_records, key=detail_fetch_priority):
                            if not record_needs_backfill(record):
                                records.append(record)
                                continue
                            if config.detail_limit > 0 and detail_count >= config.detail_limit:
                                records.append(record)
                                continue
                            records.append(reparse_record_from_detail(enrich_record_with_detail(record, cookie)))
                            detail_count += 1
    except SystemExit as exc:
        if "antiVerify captcha" not in str(exc):
            raise
        anti_verify_meta = dict(LAST_FETCH_META)
        anti_verify_meta["source_mode"] = source_mode
        output = {
            "meta": {
                "keywords": config.keywords,
                "province": config.province,
                "industry": config.industry,
                "publish_range": config.publish_range,
                "page_size": config.page_size,
                "max_pages": config.max_pages,
                "record_count": 0,
                "project_count": 0,
                "usable_project_count": 0,
                "core_analyzable_project_count": 0,
                "source_mode": anti_verify_meta.get("source_mode"),
                "anti_verify": anti_verify_meta.get("anti_verify"),
                "anti_verify_text": anti_verify_meta.get("anti_verify_text"),
                "backfill_direct_matches": LAST_FETCH_META.get("backfill_direct_matches", 0),
                "backfill_coarse_candidates": LAST_FETCH_META.get("backfill_coarse_candidates", 0),
                "backfill_detail_verified": LAST_FETCH_META.get("backfill_detail_verified", 0),
                "backfill_search_skipped": LAST_FETCH_META.get("backfill_search_skipped", 0),
                "targeted_backfill_projects": LAST_FETCH_META.get("targeted_backfill_projects", 0),
                "targeted_backfill_missing_open_projects": LAST_FETCH_META.get("targeted_backfill_missing_open_projects", 0),
                "targeted_backfill_recovered_open_records": LAST_FETCH_META.get("targeted_backfill_recovered_open_records", 0),
                "targeted_backfill_recovered_notice_records": LAST_FETCH_META.get("targeted_backfill_recovered_notice_records", 0),
                "targeted_backfill_recovered_result_records": LAST_FETCH_META.get("targeted_backfill_recovered_result_records", 0),
                "followup_seed_generated": LAST_FETCH_META.get("followup_seed_generated", 0),
                "followup_seed_original_url_generated": LAST_FETCH_META.get("followup_seed_original_url_generated", 0),
                "followup_seed_related_link_generated": LAST_FETCH_META.get("followup_seed_related_link_generated", 0),
                "followup_verified_kept": LAST_FETCH_META.get("followup_verified_kept", 0),
                "followup_filtered_out": LAST_FETCH_META.get("followup_filtered_out", 0),
                "package_count": len(package_summaries),
                "packages": package_summaries,
            },
            "projects": [],
        }
        write_json(config.output, output)
        customer_json_path = write_customer_json(config.output, output, config.customer_json)
        output["meta"]["customer_json_path"] = customer_json_path
        write_json(config.output, output)
        return output

    if config.fetch_details:
        cookie = load_cookie(config, required=False)
        should_backfill_seed_records = bool(
            config.input_urls_json
            or config.input_md
            or config.input_html
            or config.input_json
            or config.input_dir
            or config.input_dir_batch
        )
        if should_backfill_seed_records:
            seed_records = [
                item for item in records
                if looks_like_building_project(str(item.get("project_name") or item.get("title") or ""))
            ]
            candidate_records = backfill_project_records(cookie, dataclasses.replace(config, industry=""), seed_records)
            records = merge_records_by_url(records, candidate_records)
        pregrouped = merge_project_groups(group_records(normalize_records_after_detail(records)))
        pre_summaries = {
            project_key: build_project_summary(project_key, group, config.allow_core_without_notice)
            for project_key, group in pregrouped.items()
        }
        eligible_detail_records = 0
        for record in records:
            abort_if_requested()
            project_key = build_record_group_key(record)
            project_summary = pre_summaries.get(project_key)
            if not record_needs_backfill(record):
                continue
            if record_has_detail_payload(record):
                continue
            if not should_fetch_detail_for_batch(record, project_summary):
                continue
            eligible_detail_records += 1
        detail_plan_total = eligible_detail_records if config.detail_limit <= 0 else min(eligible_detail_records, config.detail_limit)
        pre_core_count = count_core_projects(records, config.allow_core_without_notice)
        emit_progress(
            "detail_plan_ready",
            total_records=len(records),
            grouped_projects=len(pregrouped),
            eligible_detail_records=eligible_detail_records,
            detail_plan_total=detail_plan_total,
            core_projects=pre_core_count,
        )
        detail_count = 0
        hydrated_records: list[dict[str, Any]] = []
        for record in records:
            project_key = build_record_group_key(record)
            project_summary = pre_summaries.get(project_key)
            if not record_needs_backfill(record):
                hydrated_records.append(record)
                continue
            if record_has_detail_payload(record):
                hydrated_records.append(reparse_record_from_detail(record))
                continue
            if not should_fetch_detail_for_batch(record, project_summary):
                hydrated_records.append(record)
                continue
            if config.detail_limit > 0 and detail_count >= config.detail_limit:
                hydrated_records.append(record)
                continue
            emit_progress(
                "detail_fetch_start",
                current=detail_count + 1,
                total=detail_plan_total,
                title=str(record.get("title") or record.get("project_name") or ""),
            )
            hydrated_records.append(reparse_record_from_detail(enrich_record_with_detail(record, cookie)))
            detail_count += 1
            current_core_count = count_core_projects(hydrated_records + records[len(hydrated_records):], config.allow_core_without_notice)
            emit_progress(
                "detail_fetch_done",
                current=detail_count,
                total=detail_plan_total,
                title=str(record.get("title") or record.get("project_name") or ""),
                core_projects=current_core_count,
            )
        records = hydrated_records
        followup_records = records_from_followup_seed_urls(records)
        if followup_records:
            records = merge_records_by_url(records, followup_records)
            hydrated_followups: list[dict[str, Any]] = []
            kept_followups = 0
            filtered_followups = 0
            for record in records:
                abort_if_requested()
                if record_has_detail_payload(record):
                    if not followup_record_matches_origin(record):
                        filtered_followups += 1
                        continue
                    if (record.get("raw") or {}).get("source") in {"original_url", "related_link"}:
                        kept_followups += 1
                    hydrated_followups.append(record)
                    continue
                if config.detail_limit > 0 and detail_count >= config.detail_limit:
                    if not is_followup_shell_record(record):
                        hydrated_followups.append(record)
                    continue
                emit_progress(
                    "detail_fetch_start",
                    current=detail_count + 1,
                    total=detail_plan_total,
                    title=str(record.get("title") or record.get("project_name") or ""),
                )
                enriched = reparse_record_from_detail(enrich_record_with_detail(record, cookie))
                if is_followup_shell_record(enriched):
                    filtered_followups += 1
                    continue
                if not followup_record_matches_origin(enriched):
                    filtered_followups += 1
                    continue
                if ((enriched.get("raw") or {}).get("source")) in {"original_url", "related_link"}:
                    kept_followups += 1
                hydrated_followups.append(enriched)
                detail_count += 1
                current_core_count = count_core_projects(hydrated_followups, config.allow_core_without_notice)
                emit_progress(
                    "detail_fetch_done",
                    current=detail_count,
                    total=detail_plan_total,
                    title=str(record.get("title") or record.get("project_name") or ""),
                    core_projects=current_core_count,
                )
            records = hydrated_followups
            LAST_FETCH_META["followup_verified_kept"] = kept_followups
            LAST_FETCH_META["followup_filtered_out"] = filtered_followups

    records = normalize_records_after_detail(records)

    grouped = merge_project_groups(group_records(records))

    projects: list[dict[str, Any]] = []
    for project_key, group in sorted(grouped.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        status = project_status(group)
        summary = build_project_summary(project_key, group, allow_core_without_notice=config.allow_core_without_notice)
        projects.append(
            {
                "project_key": project_key,
                "status": status,
                "summary": summary,
                "records": group,
            }
        )

    audit = build_project_audit_meta(projects)

    output = {
        "meta": {
            "keywords": config.keywords,
            "province": config.province,
            "industry": config.industry,
            "publish_range": config.publish_range,
            "page_size": config.page_size,
            "max_pages": config.max_pages,
            "record_count": len(records),
            "project_count": len(projects),
            "usable_project_count": sum(1 for item in projects if item["status"]["usable"]),
            "file_complete_project_count": sum(1 for item in projects if item["summary"]["file_complete"]),
            "core_analyzable_project_count": sum(1 for item in projects if item["summary"]["can_analyze_core"]),
            "source_mode": source_mode or LAST_FETCH_META.get("source_mode"),
            "anti_verify": LAST_FETCH_META.get("anti_verify"),
            "anti_verify_text": LAST_FETCH_META.get("anti_verify_text"),
            "backfill_direct_matches": LAST_FETCH_META.get("backfill_direct_matches", 0),
            "backfill_coarse_candidates": LAST_FETCH_META.get("backfill_coarse_candidates", 0),
            "backfill_detail_verified": LAST_FETCH_META.get("backfill_detail_verified", 0),
            "backfill_search_skipped": LAST_FETCH_META.get("backfill_search_skipped", 0),
            "targeted_backfill_projects": LAST_FETCH_META.get("targeted_backfill_projects", 0),
            "targeted_backfill_missing_open_projects": LAST_FETCH_META.get("targeted_backfill_missing_open_projects", 0),
            "targeted_backfill_recovered_open_records": LAST_FETCH_META.get("targeted_backfill_recovered_open_records", 0),
            "targeted_backfill_recovered_notice_records": LAST_FETCH_META.get("targeted_backfill_recovered_notice_records", 0),
            "targeted_backfill_recovered_result_records": LAST_FETCH_META.get("targeted_backfill_recovered_result_records", 0),
            "followup_seed_generated": LAST_FETCH_META.get("followup_seed_generated", 0),
            "followup_seed_original_url_generated": LAST_FETCH_META.get("followup_seed_original_url_generated", 0),
            "followup_seed_related_link_generated": LAST_FETCH_META.get("followup_seed_related_link_generated", 0),
            "followup_verified_kept": LAST_FETCH_META.get("followup_verified_kept", 0),
            "followup_filtered_out": LAST_FETCH_META.get("followup_filtered_out", 0),
            "package_count": len(package_summaries),
            "packages": package_summaries,
            "audit": audit,
        },
        "projects": projects,
    }
    write_json(config.output, output)
    customer_json_path = write_customer_json(config.output, output, config.customer_json)
    output["meta"]["customer_json_path"] = customer_json_path
    write_json(config.output, output)
    if config.report_md:
        Path(config.report_md).write_text(build_markdown_report(output), encoding="utf-8")
    return output


def main() -> int:
    config = parse_args()
    if config.precise_project_query.strip():
        return run_precise_project_capture(config)
    if config.probe_html_captcha_url:
        cookie = load_cookie(config)
        if config.captcha_auto_attempts > 0:
            output = auto_attempt_html_captcha(config.probe_html_captcha_url, cookie, config)
        else:
            output = probe_html_captcha(config.probe_html_captcha_url, cookie, config)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    if config.probe_search_captcha:
        cookie = load_cookie(config)
        data = fetch_page_with_fallback(cookie, dataclasses.replace(config, industry=""), 1)
        if config.captcha_auto_attempts > 0:
            data = auto_attempt_search_captcha(cookie, config)
        captcha_meta = extract_captcha_meta_from_search_response(data) or {}
        write_captcha_image_if_needed(config, captcha_meta)
        suggested_clicks = None
        suggested_click_candidates: list[str] = []
        suggested_click_candidates_template: list[str] = []
        if config.captcha_image_out and Path(config.captcha_image_out).exists() and captcha_meta.get("text_verify"):
            suggested_clicks = infer_captcha_clicks(config.captcha_image_out, str(captcha_meta.get("text_verify")))
            suggested_click_candidates = infer_captcha_click_candidates(config.captcha_image_out, str(captcha_meta.get("text_verify")))
            suggested_click_candidates_template = infer_captcha_click_candidates_template(config.captcha_image_out, str(captcha_meta.get("text_verify")))
        if config.captcha_clicks:
            submit_search_captcha(cookie, config.captcha_clicks)
            data = fetch_page_with_fallback(cookie, dataclasses.replace(config, industry=""), 1)
            captcha_meta = extract_captcha_meta_from_search_response(data) or {}
            write_captcha_image_if_needed(config, captcha_meta)
            if config.captcha_image_out and Path(config.captcha_image_out).exists() and captcha_meta.get("text_verify"):
                suggested_clicks = infer_captcha_clicks(config.captcha_image_out, str(captcha_meta.get("text_verify")))
                suggested_click_candidates = infer_captcha_click_candidates(config.captcha_image_out, str(captcha_meta.get("text_verify")))
                suggested_click_candidates_template = infer_captcha_click_candidates_template(config.captcha_image_out, str(captcha_meta.get("text_verify")))
        output = {
            "captcha": {
                "has_captcha": bool(captcha_meta),
                "text_verify": captcha_meta.get("text_verify"),
                "image_written": bool(config.captcha_image_out and Path(config.captcha_image_out).exists()),
                "image_path": config.captcha_image_out or None,
                "suggested_clicks": suggested_clicks,
                "suggested_click_candidates": suggested_click_candidates,
                "suggested_click_candidates_template": suggested_click_candidates_template,
                "clicks_used": config.captcha_clicks or None,
                "search_unlocked": bool((data.get("data") or {}).get("list")),
            }
        }
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    output = run_collection(config)
    print(json.dumps(output["meta"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
