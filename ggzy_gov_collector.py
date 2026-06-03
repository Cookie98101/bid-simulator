#!/usr/bin/env python3
"""Collector for https://www.ggzy.gov.cn/ with basic anti-bot handling.

The site exposes a public list API but also has two defensive branches:
- code 829: captcha required
- code 800: temporary cool-down requested

This collector keeps one session, adds jittered delays, retries with backoff,
and falls back to manual captcha entry when needed.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import html
import json
import random
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from customer_json_utils import write_customer_json_splits


BASE_URL = "https://www.ggzy.gov.cn"
LIST_URL = f"{BASE_URL}/information/pubTradingInfo/getTradList"
CAPTCHA_URL = f"{BASE_URL}/information/captcha"
DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Origin": BASE_URL,
    "Referer": f"{BASE_URL}/deal/dealList.html",
    "Content-Type": "application/x-www-form-urlencoded",
}

PROVINCE_CODES = {
    "北京": "110000",
    "天津": "120000",
    "河北": "130000",
    "山西": "140000",
    "内蒙古": "150000",
    "辽宁": "210000",
    "吉林": "220000",
    "黑龙江": "230000",
    "上海": "310000",
    "江苏": "320000",
    "浙江": "330000",
    "安徽": "340000",
    "福建": "350000",
    "江西": "360000",
    "山东": "370000",
    "河南": "410000",
    "湖北": "420000",
    "湖南": "430000",
    "广东": "440000",
    "广西": "450000",
    "海南": "460000",
    "重庆": "500000",
    "四川": "510000",
    "贵州": "520000",
    "云南": "530000",
    "西藏": "540000",
    "陕西": "610000",
    "甘肃": "620000",
    "青海": "630000",
    "宁夏": "640000",
    "新疆": "650000",
    "兵团": "660000",
}

PROGRESS_CALLBACK = None
STOP_REQUESTED = None
CAPTCHA_HANDLER = None

TARGET_PROJECT_CATEGORY_TOKENS = (
    "房建",
    "房屋建筑",
    "住宅",
    "公租房",
    "保障房",
    "安置房",
    "周转房",
    "业务技术用房",
    "办公楼",
    "综合楼",
    "宿舍楼",
    "教学楼",
    "中学",
    "小学",
    "校舍",
    "实验楼",
    "门诊楼",
    "住院楼",
    "幼儿园",
    "学校",
    "医院",
    "市政",
    "市政工程",
    "道路工程",
    "桥梁工程",
    "给排水",
    "供水",
    "饮水",
    "农村饮水",
    "供排水",
    "水环境",
    "供热管网",
    "排水管网",
    "水利",
    "水库",
    "灌区",
    "河道",
    "堤防",
    "泵站",
    "公路",
    "国道",
    "省道",
    "县道",
    "乡道",
    "城中村改造",
    "棚户区改造",
    "人居环境整治",
)

TARGET_PROJECT_EXCLUDE_TOKENS = (
    "EPC",
    "EP C",
    "工程总承包",
    "设计施工总承包",
    "勘察设计施工总承包",
    "监理",
    "勘察",
    "设计",
    "造价咨询",
    "咨询服务",
    "检测服务",
    "检测",
    "测绘",
    "审计",
)

TARGET_EVALUATION_TOKENS = (
    "智能化评审",
    "智能化评标",
    "智能评审",
)

OPEN_ROW_NOISE_TOKENS = (
    "开标参与人",
    "开标地点",
    "开标时间",
    "开标记录内容",
    "投标人名称",
    "中标候选人名称",
    "候选人名称",
    "中标人名称",
    "排序",
    "序号",
    "评标情况",
    "基本情况",
    "资格能力条件",
    "项目负责人情况",
    "项目业绩",
    "其他公示内容",
    "异议的渠道和方式",
    "监督部门",
    "联系方式",
)


def clean_text(value: str | None) -> str:
    text = html.unescape(value or "")
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def emit_progress(payload: dict[str, Any]) -> None:
    if PROGRESS_CALLBACK is None:
        return
    try:
        PROGRESS_CALLBACK(payload)
    except Exception:
        pass


def stop_requested() -> bool:
    if STOP_REQUESTED is None:
        return False
    try:
        return bool(STOP_REQUESTED())
    except Exception:
        return False


def strip_units(value: str) -> str:
    return re.sub(r"\s*(元|万元)\s*$", "", clean_text(value))


def parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    match = re.search(r"(\d+(?:,\d{3})*(?:\.\d+)?)", value)
    if not match:
        return None
    return float(match.group(1).replace(",", ""))


def calc_down_rate(control_price: float | None, amount: float | None) -> float | None:
    if not control_price or not amount or control_price <= 0:
        return None
    return (control_price - amount) / control_price * 100.0


def write_json(path: str, payload: Any) -> None:
    Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def derive_customer_json_path(output_path: str) -> str:
    path = Path(output_path)
    if path.suffix.lower() == ".json":
        return str(path.with_name(f"{path.stem}_客户版.json"))
    return str(path.with_name(f"{path.name}_客户版.json"))


def yes_no(value: bool) -> str:
    return "是" if value else "否"


def looks_like_target_construction(text: str) -> bool:
    normalized = clean_text(text)
    if not normalized:
        return False
    lower = normalized.lower()
    if any(token.lower() in lower for token in TARGET_PROJECT_EXCLUDE_TOKENS):
        return False
    if not any(token in normalized for token in TARGET_PROJECT_CATEGORY_TOKENS):
        return False
    return any(token in normalized for token in ("项目", "工程", "建设", "改造", "维修", "新建", "扩建"))


def has_intelligent_evaluation(text: str) -> bool:
    normalized = clean_text(text)
    return any(token in normalized for token in TARGET_EVALUATION_TOKENS)


def sleep_jitter(min_seconds: float, max_seconds: float) -> None:
    time.sleep(random.uniform(min_seconds, max_seconds))


def today_str() -> str:
    return dt.date.today().isoformat()


def date_days_ago(days: int) -> str:
    return (dt.date.today() - dt.timedelta(days=days)).isoformat()


def normalize_title(title: str) -> str:
    title = clean_text(title)
    title = re.sub(r"(中标结果公告|中标结果公示|中标候选人公示|招标公告|招标文件|开标记录|资格预审文件|采购公告|采购/资审公告|招标/资审公告|招标/资审文件澄清|更正公告|变更公告|结果公告|中标公告|成交公告)$", "", title)
    title = re.sub(r"\s+", "", title)
    return title


@dataclass
class CrawlConfig:
    province: str = "西藏"
    city: str = ""
    classify: str = "01"
    stage: str = "0104"
    source_type: str = "1"
    deal_time: str = "02"
    days: int = 30
    begin: str = ""
    end: str = ""
    keyword: str = ""
    max_projects: int | None = None
    max_pages: int | None = None
    request_min_delay: float = 0.25
    request_max_delay: float = 0.9
    retry_count: int = 3
    cooldown_seconds: float = 20.0
    captcha_dir: str = "tmp/ggzy_captcha"
    output_json: str = "ggzy_gov_output.json"


@dataclass
class PageRecord:
    stage: str
    url: str
    title: str
    publish_time: str = ""
    text: str = ""
    tables: list[dict[str, Any]] = field(default_factory=list)
    extracted: dict[str, Any] = field(default_factory=dict)


class GGZYClient:
    def __init__(self, config: CrawlConfig) -> None:
        self.config = config
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.captcha_token: str = ""
        self.captcha_image_path: str = ""
        self.last_request_at = 0.0

    def throttle(self) -> None:
        if self.last_request_at <= 0:
            self.last_request_at = time.time()
            return
        elapsed = time.time() - self.last_request_at
        target = random.uniform(self.config.request_min_delay, self.config.request_max_delay)
        if elapsed < target:
            time.sleep(target - elapsed)
        self.last_request_at = time.time()

    def get_captcha(self) -> dict[str, str]:
        self.throttle()
        resp = self.session.get(CAPTCHA_URL, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        captcha = data.get("data") or {}
        token = str(captcha.get("captchaToken") or "")
        image = str(captcha.get("captchaImage") or "")
        if not token or not image:
            raise RuntimeError("验证码接口返回缺失 token 或图片")
        out_dir = Path(self.config.captcha_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        image_path = out_dir / f"captcha_{ts}.png"
        image_path.write_bytes(__import__("base64").b64decode(image))
        self.captcha_token = token
        self.captcha_image_path = str(image_path)
        return {"captchaToken": token, "captchaImagePath": str(image_path)}

    def _post_list_once(self, payload: dict[str, str], verify_code: str | None = None) -> dict[str, Any]:
        headers = dict(self.session.headers)
        token = ""
        if verify_code:
            token = f"{self.captcha_token}#{verify_code}"
        headers["X-Pass-Token"] = token
        self.throttle()
        resp = self.session.post(LIST_URL, data=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def post_list(self, payload: dict[str, str]) -> dict[str, Any]:
        last_error: Exception | None = None
        verify_code: str | None = None
        for _ in range(self.config.retry_count):
            try:
                data = self._post_list_once(payload, verify_code=verify_code)
            except Exception as exc:
                last_error = exc
                sleep_jitter(self.config.request_min_delay, self.config.request_max_delay)
                continue

            code = data.get("code")
            if code == 200:
                return data
            if code == 800:
                emit_progress({"stage": "cooldown", "message": "触发频控，自动冷却后重试"})
                time.sleep(self.config.cooldown_seconds)
                verify_code = None
                continue
            if code == 829:
                captcha = self.get_captcha()
                emit_progress(
                    {
                        "stage": "captcha_required",
                        "message": "列表接口触发验证码",
                        "captcha_image_path": captcha["captchaImagePath"],
                    }
                )
                if CAPTCHA_HANDLER is not None:
                    try:
                        verify_code = str(
                            CAPTCHA_HANDLER(
                                {
                                    "scope": "ggzy_list",
                                    "url": f"{BASE_URL}/deal/dealList.html",
                                    "captcha_image_path": captcha["captchaImagePath"],
                                }
                            )
                            or ""
                        ).strip()
                    except Exception:
                        verify_code = ""
                else:
                    print(f"[captcha] 已生成：{captcha['captchaImagePath']}")
                    verify_code = input("请输入验证码：").strip()
                if not verify_code:
                    raise RuntimeError(f"需要验证码：{self.captcha_image_path}")
                continue
            raise RuntimeError(f"列表接口返回异常 code={code} message={data.get('message')}")

        raise RuntimeError(f"列表接口请求失败：{last_error}")

    def get_html(self, url: str) -> str:
        self.throttle()
        resp = self.session.get(url, timeout=30)
        resp.raise_for_status()
        return resp.text


def parse_list_records(data: dict[str, Any]) -> list[dict[str, Any]]:
    records = data.get("data", {}).get("records") or []
    normalized: list[dict[str, Any]] = []
    for item in records:
        title = clean_text(item.get("title"))
        industry = clean_text(item.get("industryTypeText") or "")
        business_type = clean_text(item.get("businessTypeText") or "")
        info_type = clean_text(item.get("informationTypeText") or "")
        haystack = "\n".join([title, industry, business_type, info_type])
        if not looks_like_target_construction(haystack):
            continue
        normalized.append(
            {
                "id": item.get("id"),
                "title": title,
                "publish_time": item.get("publishTime") or "",
                "province": item.get("provinceText") or "",
                "city": item.get("cityText") or "",
                "platform": item.get("transactionSourcesPlatformText") or "",
                "business_type": business_type,
                "information_type": info_type,
                "industry": industry,
                "url": item.get("url") or "",
                "raw": item,
                "project_key": item.get("tenderProjectCode") or normalize_title(title or ""),
            }
        )
    return normalized


def parse_a_detail_page(html_text: str, page_url: str) -> dict[str, Any]:
    soup = BeautifulSoup(html_text, "html.parser")
    title = clean_text(soup.select_one("h4.h4_o").get_text(" ", strip=True) if soup.select_one("h4.h4_o") else "")
    project_code = ""
    m = re.search(r"招标项目编号：\s*([^<\s]+)", html_text)
    if m:
        project_code = clean_text(m.group(1))
    platform = clean_text(soup.select_one("#platformName").get_text(" ", strip=True) if soup.select_one("#platformName") else "")
    publish_time = ""
    p = soup.select_one("p.p_o")
    if p:
        text = clean_text(p.get_text(" ", strip=True))
        m = re.search(r"发布时间：\s*([0-9:\-\s]+)", text)
        if m:
            publish_time = clean_text(m.group(1))
    first_last_url = ""
    m = re.search(r"var\s+firstLastUrl\s*=\s*'([^']+)'", html_text)
    if m:
        first_last_url = m.group(1)
    related_pages: list[dict[str, str]] = []
    for li in soup.select(".fully_list li"):
        anchor = li.find("a")
        if not anchor:
            continue
        onclick = anchor.get("onclick") or ""
        m = re.search(r"showDetail\(this,\s*'?(\d+)'?,\s*'([^']+)'\)", onclick)
        if not m:
            continue
        related_pages.append(
            {
                "stage": m.group(1),
                "title": clean_text(anchor.get("title") or anchor.get_text(" ", strip=True)),
                "url": m.group(2),
                "date": clean_text(li.find("span").get_text(" ", strip=True) if li.find("span") else ""),
            }
        )
    return {
        "page_url": page_url,
        "title": title,
        "project_code": project_code,
        "platform": platform,
        "publish_time": publish_time,
        "first_last_url": first_last_url,
        "related_pages": related_pages,
        "text": clean_text(soup.get_text(" ", strip=True)),
    }


def _extract_bid_rows_from_table(table: BeautifulSoup) -> list[dict[str, Any]]:
    thead = table.find("thead")
    headers: list[str] = []
    if thead is not None:
        headers = [clean_text(th.get_text(" ", strip=True)) for th in thead.find_all("th")]
    if not headers:
        first_row = table.find("tr")
        if first_row is not None:
            headers = [clean_text(cell.get_text(" ", strip=True)) for cell in first_row.find_all(["th", "td"], recursive=False)]
    if not headers:
        return []
    normalized_headers = [re.sub(r"\s+", "", header) for header in headers]
    rows: list[dict[str, Any]] = []
    tbody = table.find("tbody")
    body_rows = tbody.find_all("tr", recursive=False) if tbody is not None else []
    if not body_rows:
        all_rows = table.find_all("tr", recursive=False)
        body_rows = all_rows[1:] if len(all_rows) > 1 else []
    for tr in body_rows:
        cells = [clean_text(td.get_text(" ", strip=True)) for td in tr.find_all(["td", "th"], recursive=False)]
        if not cells:
            continue
        compact_cells = [re.sub(r"\s+", "", cell) for cell in cells if cell]
        first_cell = compact_cells[0] if compact_cells else ""
        if len(compact_cells) == 1 and len(compact_cells[0]) > 120:
            continue
        if first_cell in OPEN_ROW_NOISE_TOKENS:
            continue
        if headers:
            header_first = normalized_headers[0] if normalized_headers else ""
            if first_cell and first_cell == header_first and len(compact_cells) <= len(normalized_headers):
                if all(
                    idx < len(normalized_headers) and compact_cells[idx] == normalized_headers[idx]
                    for idx in range(min(len(compact_cells), len(normalized_headers)))
                ):
                    continue
        row = {headers[i]: cells[i] if i < len(cells) else "" for i in range(len(headers))}
        row["_normalized_headers"] = normalized_headers
        rows.append(row)
    return rows


def _row_value(row: dict[str, Any], *aliases: str) -> str:
    normalized: dict[str, str] = {}
    for key, value in row.items():
        if str(key).startswith("_"):
            continue
        normalized[re.sub(r"\s+", "", clean_text(str(key)))] = clean_text(str(value))
    for alias in aliases:
        alias_norm = re.sub(r"\s+", "", clean_text(alias))
        if alias_norm in normalized:
            return normalized[alias_norm]
    return ""


def parse_open_record_table(soup: BeautifulSoup, html_text: str = "") -> list[dict[str, Any]]:
    def is_bid_table(headers: list[str]) -> bool:
        normalized = [re.sub(r"\s+", "", clean_text(header)) for header in headers if clean_text(header)]
        header_text = "".join(normalized)
        has_company = any(token in header_text for token in ("投标人名称", "投标单位", "投标人", "供应商名称", "单位名称"))
        has_quote = any(token in header_text for token in ("投标总报价", "投标报价", "报价", "最终报价", "投标总价"))
        return has_company and has_quote

    for table in soup.find_all("table"):
        headers = [clean_text(th.get_text(" ", strip=True)) for th in table.select("thead th")]
        if not headers:
            headers = [clean_text(th.get_text(" ", strip=True)) for th in table.find_all("th")]
        if is_bid_table(headers):
            rows = _extract_bid_rows_from_table(table)
            if rows:
                return rows
    raw_text = html.unescape(html_text or "")
    if "<table" not in raw_text.lower():
        return []
    for table_html in re.findall(r"(<table[\s\S]*?</table>)", raw_text, flags=re.I):
        table_soup = BeautifulSoup(table_html, "html.parser")
        table = table_soup.find("table")
        if table is None:
            continue
        headers = [clean_text(th.get_text(" ", strip=True)) for th in table.find_all("th")]
        if not is_bid_table(headers):
            continue
        rows = _extract_bid_rows_from_table(table)
        if rows:
            return rows
    return []


def parse_candidate_table(soup: BeautifulSoup, html_text: str = "") -> list[dict[str, Any]]:
    def is_candidate_table(headers: list[str]) -> bool:
        normalized = [re.sub(r"\s+", "", clean_text(header)) for header in headers if clean_text(header)]
        header_text = "".join(normalized)
        has_company = any(token in header_text for token in ("中标候选人名称", "候选人名称", "中标人名称"))
        has_quote = any(token in header_text for token in ("投标报价", "投标总报价", "报价", "成交价"))
        return has_company and has_quote

    for table in soup.find_all("table"):
        thead = table.find("thead")
        headers = [clean_text(th.get_text(" ", strip=True)) for th in thead.find_all("th")] if thead is not None else []
        if not headers:
            first_row = table.find("tr")
            if first_row is not None:
                headers = [clean_text(cell.get_text(" ", strip=True)) for cell in first_row.find_all(["th", "td"], recursive=False)]
        if is_candidate_table(headers):
            rows = _extract_bid_rows_from_table(table)
            if rows:
                return rows
    raw_text = html.unescape(html_text or "")
    if "<table" not in raw_text.lower():
        return []
    for table_html in re.findall(r"(<table[\s\S]*?</table>)", raw_text, flags=re.I):
        table_soup = BeautifulSoup(table_html, "html.parser")
        table = table_soup.find("table")
        if table is None:
            continue
        thead = table.find("thead")
        headers = [clean_text(th.get_text(" ", strip=True)) for th in thead.find_all("th")] if thead is not None else []
        if not headers:
            first_row = table.find("tr")
            if first_row is not None:
                headers = [clean_text(cell.get_text(" ", strip=True)) for cell in first_row.find_all(["th", "td"], recursive=False)]
        if not is_candidate_table(headers):
            continue
        rows = _extract_bid_rows_from_table(table)
        if rows:
            return rows
    return []


def parse_b_detail_page(html_text: str, page_url: str) -> PageRecord:
    soup = BeautifulSoup(html_text, "html.parser")
    title = clean_text(soup.select_one("h4.h4_o").get_text(" ", strip=True) if soup.select_one("h4.h4_o") else "")
    p = soup.select_one("p.p_o")
    publish_time = ""
    if p:
        text = clean_text(p.get_text(" ", strip=True))
        m = re.search(r"(发布时间|开标时间)：+\s*([0-9:\-A-Za-z年月日\s]+)", text)
        if m:
            publish_time = clean_text(m.group(2))
    full_text = clean_text(soup.get_text(" ", strip=True))
    page_haystack = f"{title}\n{full_text}"
    is_open_page = "开标记录" in page_haystack or "开标结果记录" in page_haystack or "投标人名称" in full_text

    extracted: dict[str, Any] = {}
    if is_open_page:
        rows = parse_open_record_table(soup, html_text)
        bids = []
        for row in rows:
            company = _row_value(row, "投标人名称", "投标单位", "投标人", "供应商名称", "单位名称")
            bid_amount = _row_value(row, "投标报价（元）", "投标报价 （元）", "投标报价", "投标总报价（元）", "投标总报价 （元）", "投标总报价", "最终报价")
            control_price = _row_value(row, "控制价(万元)", "控制价（万元）", "控制价", "最高限价", "招标控制价")
            bids.append(
                {
                    "company": clean_text(company),
                    "bid_amount_raw": clean_text(bid_amount),
                    "bid_amount": parse_float(bid_amount),
                    "control_price_raw": clean_text(control_price),
                    "control_price": parse_float(control_price),
                    "row": row,
                }
            )
        extracted = {"bids": bids}
    elif (
        "中标结果公告" in page_haystack
        or "中标公告" in page_haystack
        or "结果公告" in page_haystack
        or "结果公示" in page_haystack
        or "中标候选人公示" in page_haystack
    ):
        lines = [clean_text(line) for line in soup.get_text("\n").splitlines()]
        lines = [line for line in lines if line]
        winner = ""
        amount = None
        winner_match = re.search(
            r"(?:中标人|中标单位|成交供应商)\s*[:：]\s*(?:<span[^>]*>)?\s*([^<\s][^<]*)",
            html_text,
            flags=re.I,
        )
        if winner_match:
            winner = clean_text(winner_match.group(1))
        amount_match = re.search(
            r"(?:投标报价中标价格|投标报价|中标价格|中标金额|中标价|成交金额|成交价)\s*[:：]?\s*(?:<span[^>]*>)?\s*([\d,]+(?:\.\d+)?)",
            html_text,
            flags=re.I,
        )
        if amount_match:
            amount = parse_float(amount_match.group(1))
        section_start = None
        for idx, line in enumerate(lines):
            if "中标人信息" in line:
                section_start = idx
                break
        if section_start is not None:
            try:
                start = section_start
                end = len(lines)
                for marker in ("二、其他公告内容", "三、监督部门", "四、联系方式"):
                    for j in range(start + 1, len(lines)):
                        if marker in lines[j]:
                            end = min(end, j)
                            break
                section = lines[start + 1 : end]
                for idx, line in enumerate(section):
                    compact = line.replace(" ", "")
                    if compact in {"中标人:", "中标人：", "中标单位:", "中标单位：", "成交供应商:", "成交供应商：", "中标人", "中标单位", "成交供应商"} and not winner:
                        if idx + 1 < len(section):
                            winner = section[idx + 1]
                        break
                for idx, line in enumerate(section):
                    compact = line.replace(" ", "")
                    if ("中标价格" in compact or "中标金额" in compact or "中标价" in compact or "成交金额" in compact or "成交价" in compact) and amount is None:
                        next_text = section[idx + 1] if idx + 1 < len(section) else ""
                        amount = parse_float(next_text) or parse_float(line)
                        break
            except ValueError:
                pass
        if not winner:
            for idx, line in enumerate(lines):
                compact = line.replace(" ", "")
                if "中标人" in compact or "中标单位" in compact or "成交供应商" in compact:
                    parts = re.split(r"[:：]", line, maxsplit=1)
                    candidate = clean_text(parts[1] if len(parts) > 1 else "")
                    if not candidate and idx + 1 < len(lines):
                        candidate = clean_text(lines[idx + 1])
                    if candidate and candidate not in {"中标人", "中标单位", "成交供应商"}:
                        winner = candidate
                        break
        if amount is None:
            amount_patterns = [
                r"(?:投标报价中标价格|投标报价|中标价格|中标金额|中标价|成交金额|成交价)[:：]?\s*([\d,]+(?:\.\d+)?)",
                r"(?:投标报价中标价格|投标报价|中标价格|中标金额|中标价|成交金额|成交价)\s*([\d,]+(?:\.\d+)?)\s*(?:元|万元)",
            ]
            for line in lines:
                compact = line.replace(" ", "")
                for pat in amount_patterns:
                    m = re.search(pat, compact)
                    if m:
                        amount = parse_float(m.group(1))
                        break
                if amount is not None:
                    break
        extracted = {"winner": winner, "winner_amount": amount}
        if "中标候选人公示" in page_haystack:
            candidate_rows = parse_candidate_table(soup, html_text)
            extracted["candidate_rows"] = candidate_rows
            if candidate_rows:
                extracted["candidate_top_company"] = clean_text(
                    str(candidate_rows[0].get("中标候选人名称") or candidate_rows[0].get("候选人名称") or candidate_rows[0].get("中标人名称") or "")
                )
                extracted["candidate_top_amount"] = parse_float(
                    _row_value(
                        candidate_rows[0],
                        "投标报价（元）",
                        "投标报价 （元）",
                        "投标报价",
                        "投标总报价（元）",
                        "投标总报价 （元）",
                        "投标总报价",
                        "成交价",
                    )
                )
    else:
        budget_patterns = [
            r"(?:招标控制价|最高投标限价|最高限价|预算金额|控制价)[:：]\s*([\d,]+(?:\.\d+)?)",
            r"(?:招标控制价|最高投标限价|最高限价|预算金额|控制价).*?([\d,]+(?:\.\d+)?)\s*(?:元|万元)",
        ]
        budget = None
        for pat in budget_patterns:
            m = re.search(pat, full_text)
            if m:
                budget = parse_float(m.group(1))
                break
        scale_patterns = [
            r"(?:项目规模|建设规模|项目概况|工程概况)[:：]\s*([^。]+)",
            r"(?:项目规模|建设规模|项目概况|工程概况)\s*([^。]+)",
        ]
        project_scale = ""
        for pat in scale_patterns:
            m = re.search(pat, full_text)
            if m:
                project_scale = clean_text(m.group(1))
                break
        extracted = {
            "budget": budget,
            "project_scale": project_scale,
        }

    title_match = re.search(r"([^。]{5,80}(?:中标候选人公示|中标结果公告|开标记录))", full_text)
    inferred_title = clean_text(title_match.group(1)) if title_match else title

    return PageRecord(
        stage="0104" if ("中标" in page_haystack or "结果" in page_haystack or "候选人" in page_haystack or "开标记录" in page_haystack) else "",
        url=page_url,
        title=title or inferred_title,
        publish_time=publish_time,
        text=full_text,
        tables=[{"rows": parse_open_record_table(soup, html_text)}] if ("开标记录" in page_haystack or "开标结果记录" in page_haystack or "投标人名称" in full_text) else [],
        extracted=extracted,
    )


def infer_notice_type(title: str) -> str:
    text = clean_text(title)
    if "开标记录" in text or "开标结果记录" in text or ("开标" in text and "记录" in text):
        return "开标记录"
    if "中标" in text or "成交" in text or "结果" in text:
        return "中标结果"
    if "投标文件" in text or "递交时间" in text or "控制价" in text:
        return "招标公告"
    if "招标" in text or "采购" in text or "资格预审" in text:
        return "招标公告"
    return "其他"


def infer_notice_type_from_page(page: dict[str, Any]) -> str:
    title = str(page.get("title") or "")
    url = str(page.get("url") or "")
    extracted = page.get("extracted") or {}
    base = infer_notice_type(title)
    if base != "其他":
        return base
    if "/0102/" in url or extracted.get("bids"):
        return "开标记录"
    if "/0104/" in url or extracted.get("winner") or extracted.get("winner_amount") is not None:
        return "中标结果"
    if "/0101/" in url or "/0105/" in url or extracted.get("budget") is not None or extracted.get("project_scale"):
        return "招标公告"
    return "其他"


def normalize_price(value: float | None, raw: str = "", header: str = "") -> float | None:
    if value is None:
        return None
    raw_text = clean_text(raw)
    header_text = clean_text(header)
    body = f"{header_text} {raw_text}"
    if "万元" in body and value < 100000:
        return value * 10000.0
    return value


def choose_first(*values: Any) -> Any:
    for value in values:
        if value not in (None, "", [], {}):
            return value
    return None


def choose_control_price(detail_entry: dict[str, Any], detail_pages: list[dict[str, Any]]) -> tuple[float | None, str | None]:
    for page in detail_pages:
        page_title = str(page.get("title") or "")
        extracted = page.get("extracted") or {}
        bids = extracted.get("bids") or []
        for bid in bids:
            normalized = normalize_price(
                bid.get("control_price"),
                str(bid.get("control_price_raw") or ""),
                "控制价",
            )
            if normalized is not None:
                return normalized, "open_record_table"
        budget = normalize_price(extracted.get("budget"), str(extracted.get("budget") or ""), page_title)
        if budget is not None:
            return budget, "detail_body_budget"
    entry_budget = normalize_price(detail_entry.get("budget"), str(detail_entry.get("budget") or ""), detail_entry.get("title") or "")
    if entry_budget is not None:
        return entry_budget, "entry_body_budget"
    return None, None


def build_bid_participants(detail_pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    participants: list[dict[str, Any]] = []
    seen: set[tuple[str, float | None]] = set()
    for page in detail_pages:
        extracted = page.get("extracted") or {}
        bids = extracted.get("bids") or []
        for bid in bids:
            company = clean_text(str(bid.get("company") or ""))
            amount = normalize_price(bid.get("bid_amount"), str(bid.get("bid_amount_raw") or ""), "投标报价")
            if not company and amount is None:
                continue
            key = (company, amount)
            if key in seen:
                continue
            seen.add(key)
            participants.append(
                {
                    "company": company or "未识别单位",
                    "quote": amount,
                    "quote_raw": clean_text(str(bid.get("bid_amount_raw") or "")),
                }
            )
    return participants


def build_project_summary(project: dict[str, Any]) -> dict[str, Any]:
    detail_entry = project.get("detail_entry") or {}
    detail_pages = project.get("detail_pages") or []
    review_pages = [page for page in detail_pages if infer_notice_type_from_page(page) == "招标公告" and has_intelligent_evaluation(str(page.get("text") or ""))]
    notice_pages = [page for page in detail_pages if infer_notice_type_from_page(page) == "招标公告"]
    open_pages = [page for page in detail_pages if infer_notice_type_from_page(page) == "开标记录"]
    result_pages = [page for page in detail_pages if infer_notice_type_from_page(page) == "中标结果"]
    notice_types_present = sorted({infer_notice_type_from_page(page) for page in detail_pages if infer_notice_type_from_page(page) != "其他"})

    winning_company = None
    winning_price = None
    winning_company_source = None
    winning_price_source = None
    candidate_pages = [page for page in detail_pages if "中标候选人公示" in str(page.get("title") or "")]
    for page in result_pages:
        if "中标候选人公示" in str(page.get("title") or ""):
            continue
        extracted = page.get("extracted") or {}
        page_winner = clean_text(str(extracted.get("winner") or ""))
        page_amount = normalize_price(extracted.get("winner_amount"), str(extracted.get("winner_amount") or ""), "中标价")
        if winning_company is None and page_winner:
            winning_company = page_winner
            winning_company_source = "result_page"
        if winning_price is None and page_amount is not None:
            winning_price = page_amount
            winning_price_source = "result_page"
    if winning_company is None or winning_price is None:
        for page in candidate_pages:
            extracted = page.get("extracted") or {}
            candidate_rows = extracted.get("candidate_rows") or []
            if not candidate_rows:
                continue
            top_row = candidate_rows[0]
            if winning_company is None:
                winning_company = clean_text(
                    str(top_row.get("中标候选人名称") or top_row.get("候选人名称") or top_row.get("中标人名称") or "")
                ) or winning_company
                if winning_company:
                    winning_company_source = "candidate_page_first_rank"
            if winning_price is None:
                candidate_amount = normalize_price(
                    parse_float(
                        _row_value(
                            top_row,
                            "投标报价（元）",
                            "投标报价 （元）",
                            "投标报价",
                            "投标总报价（元）",
                            "投标总报价 （元）",
                            "投标总报价",
                            "成交价",
                        )
                    ),
                    _row_value(
                        top_row,
                        "投标报价（元）",
                        "投标报价 （元）",
                        "投标报价",
                        "投标总报价（元）",
                        "投标总报价 （元）",
                        "投标总报价",
                        "成交价",
                    ),
                    "中标价",
                )
                if candidate_amount is not None:
                    winning_price = candidate_amount
                    winning_price_source = "candidate_page_first_rank"

    control_price, control_price_source = choose_control_price(detail_entry, detail_pages)
    bid_participants = build_bid_participants(detail_pages)
    bid_quotes = [float(item["quote"]) for item in bid_participants if item.get("quote") is not None]

    has_notice = bool(notice_pages)
    has_open = bool(open_pages)
    has_result = bool(result_pages)
    has_control_price = control_price is not None
    has_winning_price = winning_price is not None
    has_winning_company = bool(winning_company)
    has_bid_quotes = bool(bid_quotes)
    has_required_core_fields = has_control_price and has_winning_price and has_winning_company
    can_analyze_core = has_required_core_fields
    file_complete = has_notice and has_open and has_result and has_required_core_fields

    issues: list[str] = []
    if not has_notice:
        issues.append("缺招标公告")
    if not has_open:
        issues.append("缺开标记录")
    if not has_result:
        issues.append("缺中标结果")
    if not has_control_price:
        issues.append("缺控制价")
    if not has_winning_price:
        issues.append("缺中标价")
    if not has_winning_company:
        issues.append("缺中标单位")
    if not has_bid_quotes:
        issues.append("缺全体报价")

    bid_quote_min = min(bid_quotes) if bid_quotes else None
    bid_quote_max = max(bid_quotes) if bid_quotes else None
    bid_quote_avg = sum(bid_quotes) / len(bid_quotes) if bid_quotes else None
    bid_quote_rows = sorted(
        [
            {
                "company": item.get("company") or "未识别单位",
                "quote": item.get("quote"),
                "down_rate": calc_down_rate(control_price, item.get("quote")),
            }
            for item in bid_participants
            if item.get("quote") is not None
        ],
        key=lambda row: (row.get("down_rate") is None, -(row.get("down_rate") or -10**9)),
    )
    return {
        "project_key": project.get("project_key") or project.get("project_code") or normalize_title(project.get("title") or ""),
        "project_title": project.get("title") or project.get("project_key") or "",
        "record_count": len(detail_pages),
        "notice_types_present": notice_types_present,
        "missing_notice_types": [name for name in ("招标公告", "开标记录", "中标结果") if name not in notice_types_present],
        "control_price": control_price,
        "control_price_source": control_price_source,
        "winning_price": winning_price,
        "winning_price_source": winning_price_source,
        "winning_company": winning_company,
        "winning_company_source": winning_company_source,
        "bid_quotes": bid_quotes,
        "bid_participants": bid_participants,
        "bid_quote_rows": bid_quote_rows,
        "bid_quote_count": len(bid_quotes),
        "bid_quote_min": bid_quote_min,
        "bid_quote_max": bid_quote_max,
        "bid_quote_avg": bid_quote_avg,
        "winning_down_rate": calc_down_rate(control_price, winning_price),
        "avg_down_rate": calc_down_rate(control_price, bid_quote_avg),
        "max_down_rate": calc_down_rate(control_price, bid_quote_min),
        "min_down_rate": calc_down_rate(control_price, bid_quote_max),
        "notice_record_id": str(notice_pages[0].get("url") or "") if notice_pages else None,
        "open_record_id": str(open_pages[0].get("url") or "") if open_pages else None,
        "result_record_id": str(result_pages[0].get("url") or "") if result_pages else None,
        "notice_record_url": str(notice_pages[0].get("url") or "") if notice_pages else None,
        "open_record_url": str(open_pages[0].get("url") or "") if open_pages else None,
        "result_record_url": str(result_pages[0].get("url") or "") if result_pages else None,
        "has_notice": has_notice,
        "has_open": has_open,
        "has_result": has_result,
        "has_intelligent_evaluation": bool(review_pages),
        "has_control_price": has_control_price,
        "has_winning_price": has_winning_price,
        "has_winning_company": has_winning_company,
        "has_bid_quotes": has_bid_quotes,
        "has_required_core_fields": has_required_core_fields,
        "file_complete": file_complete,
        "can_analyze_core": can_analyze_core,
        "core_ready_reason": "full_three_files" if file_complete else ("core_fields_ready" if can_analyze_core else None),
        "readiness_stage": "strict_complete" if file_complete else ("core_ready_missing_notice" if can_analyze_core else "partial_unready"),
        "primary_blocker": None if file_complete else (
            "missing_notice" if not has_notice and can_analyze_core else
            "missing_open_record" if not has_open else
            "missing_result" if not has_result else
            "missing_control_price" if not has_control_price else
            "missing_winning_price" if not has_winning_price else
            "missing_winning_company" if not has_winning_company else
            "missing_bid_quotes"
        ),
        "next_action": "none" if file_complete else (
            "recover_notice" if not has_notice and can_analyze_core else
            "recover_open_record" if not has_open else
            "recover_result" if not has_result else
            "recover_notice_or_open" if not has_control_price else
            "recover_result" if not has_winning_price or not has_winning_company else
            "recover_open_record"
        ),
        "issues": issues,
    }


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
        summary = item.get("summary") or {}
        return {
            "project_key": item.get("project_key") or "",
            "project_title": summary.get("project_title") or "",
            "record_count": summary.get("record_count", 0),
            "file_complete": bool(summary.get("file_complete")),
            "can_analyze_core": bool(summary.get("can_analyze_core")),
            "core_ready_reason": summary.get("core_ready_reason"),
            "readiness_stage": summary.get("readiness_stage"),
            "primary_blocker": summary.get("primary_blocker"),
            "next_action": summary.get("next_action"),
            "notice_types_present": summary.get("notice_types_present") or [],
            "missing_notice_types": summary.get("missing_notice_types") or [],
            "issues": summary.get("issues") or [],
            "control_price": summary.get("control_price"),
            "winning_price": summary.get("winning_price"),
            "winning_company": summary.get("winning_company"),
            "bid_quote_count": summary.get("bid_quote_count", 0),
        }

    for item in projects:
        summary = item.get("summary") or {}
        packed = pack_project(item)
        if summary.get("file_complete"):
            complete_projects.append(packed)
        else:
            incomplete_projects.append(packed)
        if summary.get("can_analyze_core") and not summary.get("file_complete"):
            core_ready_incomplete_projects.append(packed)
        if not summary.get("has_notice"):
            missing_notice_projects.append(packed)
        if not summary.get("has_open"):
            missing_open_projects.append(packed)
        if not summary.get("has_result"):
            missing_result_projects.append(packed)
        if not summary.get("has_control_price"):
            missing_control_price_projects.append(packed)
        if not summary.get("has_winning_price"):
            missing_winning_price_projects.append(packed)
        if not summary.get("has_winning_company"):
            missing_winning_company_projects.append(packed)
        if not summary.get("has_bid_quotes"):
            missing_bid_quotes_projects.append(packed)
        if not summary.get("has_required_core_fields"):
            missing_core_field_projects.append(packed)
        for issue in summary.get("issues") or []:
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


def build_customer_project(item: dict[str, Any]) -> dict[str, Any]:
    summary = item.get("summary") or {}
    quote_list = [
        {
            "单位名称": str(row.get("company") or "").strip() or "未识别单位",
            "报价": row.get("quote"),
            "下浮率": row.get("down_rate"),
        }
        for row in (summary.get("bid_quote_rows") or [])
        if isinstance(row, dict)
    ]
    return {
        "项目名称": summary.get("project_title") or item.get("project_key") or "",
        "项目编号": item.get("project_code") or "",
        "控制价档位": project_control_bucket(summary.get("control_price")),
        "当前状态": "三类文件完整" if summary.get("file_complete") else ("核心数据齐全" if summary.get("can_analyze_core") else "数据未齐"),
        "主要问题": "无" if not summary.get("issues") else "、".join(summary.get("issues") or []),
        "是否核心数据齐全": yes_no(bool(summary.get("can_analyze_core"))),
        "是否三类文件完整": yes_no(bool(summary.get("file_complete"))),
        "已有文件类型": list(summary.get("notice_types_present") or []),
        "缺失项": list(summary.get("issues") or []),
        "控制价": summary.get("control_price"),
        "中标价": summary.get("winning_price"),
        "中标单位": summary.get("winning_company") or "",
        "中标下浮率": summary.get("winning_down_rate"),
        "报价家数": summary.get("bid_quote_count", 0),
        "最高报价": summary.get("bid_quote_max"),
        "最低报价": summary.get("bid_quote_min"),
        "平均报价": summary.get("bid_quote_avg"),
        "最高下浮率": summary.get("max_down_rate"),
        "最低下浮率": summary.get("min_down_rate"),
        "平均下浮率": summary.get("avg_down_rate"),
        "各单位报价下浮率排序": quote_list,
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
            "站点": "ggzy.gov.cn",
        },
        "统计": {
            "抓取记录数": meta.get("record_count", 0),
            "归并项目数": meta.get("project_count", 0),
            "核心数据齐全项目数": meta.get("core_analyzable_project_count", 0),
            "三类文件完整项目数": meta.get("file_complete_project_count", 0),
        },
        "分析": output.get("analysis") or {},
        "项目列表": [build_customer_project(item) for item in projects],
    }


def project_control_bucket(control_price: float | None) -> str:
    if control_price is None:
        return "控制价缺失"
    if control_price < 10_000_000:
        return "小于1000万"
    if control_price < 20_000_000:
        return "1000万-2000万"
    if control_price < 50_000_000:
        return "2000万-5000万"
    if control_price < 100_000_000:
        return "5000万-1亿"
    return "1亿及以上"


def rate_stats(values: list[float | None]) -> dict[str, Any]:
    clean_values = [float(v) for v in values if v is not None]
    if not clean_values:
        return {"样本数": 0, "最低": None, "最高": None, "平均": None}
    return {
        "样本数": len(clean_values),
        "最低": min(clean_values),
        "最高": max(clean_values),
        "平均": sum(clean_values) / len(clean_values),
    }


def count_stats(values: list[int | None]) -> dict[str, Any]:
    clean_values = [int(v) for v in values if v is not None]
    if not clean_values:
        return {"样本数": 0, "最低": None, "最高": None, "平均": None}
    return {
        "样本数": len(clean_values),
        "最低": min(clean_values),
        "最高": max(clean_values),
        "平均": sum(clean_values) / len(clean_values),
    }


def price_stats(values: list[float | None]) -> dict[str, Any]:
    clean_values = [float(v) for v in values if v is not None]
    if not clean_values:
        return {"样本数": 0, "最低": None, "最高": None, "平均": None}
    return {
        "样本数": len(clean_values),
        "最低": min(clean_values),
        "最高": max(clean_values),
        "平均": sum(clean_values) / len(clean_values),
    }


def build_analysis(projects: list[dict[str, Any]]) -> dict[str, Any]:
    overall_control_prices: list[float | None] = []
    overall_winning_down_rates: list[float | None] = []
    overall_quote_down_rates: list[float | None] = []
    overall_participant_counts: list[int | None] = []
    bucket_rows: dict[str, dict[str, list[Any]]] = {}
    bucket_order = ["小于1000万", "1000万-2000万", "2000万-5000万", "5000万-1亿", "1亿及以上", "控制价缺失"]
    for bucket in bucket_order:
        bucket_rows[bucket] = {
            "control_prices": [],
            "winning_down_rates": [],
            "quote_down_rates": [],
            "participant_counts": [],
            "project_count": [],
        }

    for item in projects:
        summary = item.get("summary") or {}
        control_price = summary.get("control_price")
        bucket = project_control_bucket(control_price)
        bucket_rows.setdefault(bucket, {
            "control_prices": [],
            "winning_down_rates": [],
            "quote_down_rates": [],
            "participant_counts": [],
            "project_count": [],
        })
        bucket_rows[bucket]["project_count"].append(1)
        if control_price is not None:
            overall_control_prices.append(float(control_price))
            bucket_rows[bucket]["control_prices"].append(float(control_price))
        winning_down_rate = summary.get("winning_down_rate")
        if winning_down_rate is not None:
            overall_winning_down_rates.append(float(winning_down_rate))
            bucket_rows[bucket]["winning_down_rates"].append(float(winning_down_rate))
        quote_rows = summary.get("bid_quote_rows") or []
        quote_down_rates = [row.get("down_rate") for row in quote_rows if row.get("down_rate") is not None]
        overall_quote_down_rates.extend(float(v) for v in quote_down_rates)
        bucket_rows[bucket]["quote_down_rates"].extend(float(v) for v in quote_down_rates)
        participant_count = summary.get("bid_quote_count")
        if participant_count is not None:
            overall_participant_counts.append(int(participant_count))
            bucket_rows[bucket]["participant_counts"].append(int(participant_count))

    control_buckets: dict[str, Any] = {}
    for bucket in bucket_order:
        rows = bucket_rows.get(bucket) or {}
        control_buckets[bucket] = {
            "项目数": len(rows.get("project_count") or []),
            "控制价统计": price_stats(rows.get("control_prices") or []),
            "中标价下浮率统计": rate_stats(rows.get("winning_down_rates") or []),
            "报价下浮率统计": rate_stats(rows.get("quote_down_rates") or []),
            "参与单位数统计": count_stats(rows.get("participant_counts") or []),
        }

    return {
        "控制价档位统计": control_buckets,
        "总体统计": {
            "控制价统计": price_stats(overall_control_prices),
            "报价下浮率统计": rate_stats(overall_quote_down_rates),
            "中标价下浮率统计": rate_stats(overall_winning_down_rates),
            "参与单位数统计": count_stats(overall_participant_counts),
            "有控制价项目数": len([v for v in overall_control_prices if v is not None]),
            "有中标下浮率项目数": len([v for v in overall_winning_down_rates if v is not None]),
            "有报价明细项目数": len([item for item in projects if (item.get("summary") or {}).get("bid_quote_rows")]),
        },
    }


def write_customer_json(output_path: str, output: dict[str, Any]) -> str:
    target_path = derive_customer_json_path(output_path)
    customer_payload = build_customer_json(output)
    write_json(target_path, customer_payload)
    write_customer_json_splits(target_path, customer_payload)
    return target_path


def build_list_payload(config: CrawlConfig, page: int) -> dict[str, str]:
    payload: dict[str, str] = {
        "DEAL_CLASSIFY": config.classify,
        "DEAL_STAGE": config.stage,
        "DEAL_PROVINCE": PROVINCE_CODES.get(config.province, config.province),
        "SOURCE_TYPE": config.source_type,
        "DEAL_TIME": config.deal_time,
        "TIMEBEGIN": config.begin or date_days_ago(config.days),
        "TIMEEND": config.end or today_str(),
        "PAGENUMBER": str(page),
    }
    if config.city:
        payload["DEAL_CITY"] = config.city
    if config.keyword:
        payload["FINDTXT"] = config.keyword
    return payload


def crawl(config: CrawlConfig) -> dict[str, Any]:
    client = GGZYClient(config)
    begin = config.begin or date_days_ago(config.days)
    end = config.end or today_str()
    pages: list[dict[str, Any]] = []
    project_rows: list[dict[str, Any]] = []

    emit_progress({"stage": "search_start", "site": "ggzy"})
    first_page = client.post_list(build_list_payload(config, 1))
    total = int((first_page.get("data") or {}).get("total") or 0)
    page_total = int((first_page.get("data") or {}).get("pages") or 1)
    records = parse_list_records(first_page)
    pages.append({"page": 1, "count": len(records)})
    all_records = records[:]
    emit_progress(
        {
            "stage": "search_page_done",
            "site": "ggzy",
            "page": 1,
            "max_pages": page_total,
            "page_items": len(records),
            "collected_records": len(all_records),
            "grouped_projects": len(all_records),
        }
    )
    if config.max_pages is None or config.max_pages > 1:
        max_page = min(page_total, config.max_pages or page_total)
        for page in range(2, max_page + 1):
            if stop_requested():
                break
            emit_progress(
                {
                    "stage": "search_page_start",
                    "site": "ggzy",
                    "page": page,
                    "max_pages": page_total,
                    "collected_records": len(all_records),
                    "grouped_projects": len(all_records),
                }
            )
            resp = client.post_list(build_list_payload(config, page))
            page_records = parse_list_records(resp)
            all_records.extend(page_records)
            pages.append({"page": page, "count": len(page_records)})
            emit_progress(
                {
                    "stage": "search_page_done",
                    "site": "ggzy",
                    "page": page,
                    "max_pages": page_total,
                    "page_items": len(page_records),
                    "collected_records": len(all_records),
                    "grouped_projects": len(all_records),
                }
            )
            sleep_jitter(client.config.request_min_delay, client.config.request_max_delay)

    if config.max_projects is not None:
        all_records = all_records[: config.max_projects]

    emit_progress(
        {
            "stage": "detail_plan_ready",
            "site": "ggzy",
            "total": len(all_records),
            "grouped_projects": len(all_records),
            "core_projects": 0,
        }
    )
    for idx, record in enumerate(all_records, 1):
        if stop_requested():
            break
        a_url = BASE_URL + record["url"]
        emit_progress(
            {
                "stage": "detail_fetch_start",
                "site": "ggzy",
                "current": idx - 1,
                "total": len(all_records),
                "title": record["title"],
                "grouped_projects": len(all_records),
            }
        )
        a_html = client.get_html(a_url)
        a_data = parse_a_detail_page(a_html, a_url)
        b_urls = []
        if a_data["first_last_url"]:
            b_urls.append(a_data["first_last_url"])
        for rel in a_data["related_pages"]:
            if rel["url"] not in b_urls:
                b_urls.append(rel["url"])

        detail_pages: list[dict[str, Any]] = []
        for rel_url in b_urls:
            full_url = BASE_URL + rel_url
            b_html = client.get_html(full_url)
            page_data = parse_b_detail_page(b_html, full_url)
            detail_pages.append(dataclasses.asdict(page_data))
            sleep_jitter(client.config.request_min_delay, client.config.request_max_delay)

        row = {
            "index": idx,
            "project_key": record["project_key"],
            "title": record["title"],
            "publish_time": record["publish_time"],
            "province": record["province"],
            "city": record["city"],
            "platform": record["platform"],
            "business_type": record["business_type"],
            "information_type": record["information_type"],
            "industry": record["industry"],
            "list_url": a_url,
            "project_code": a_data.get("project_code") or record.get("raw", {}).get("tenderProjectCode") or "",
            "detail_entry": a_data,
            "detail_pages": detail_pages,
            "raw": record.get("raw") or {},
        }
        detail_haystack = "\n".join(
            [
                str(row.get("title") or ""),
                str((row.get("detail_entry") or {}).get("text") or ""),
                *[str((page.get("text") or "")) for page in detail_pages],
            ]
        )
        if not looks_like_target_construction(detail_haystack):
            continue
        if not any(page.get("title") and infer_notice_type_from_page(page) == "招标公告" and has_intelligent_evaluation(str(page.get("text") or "")) for page in detail_pages):
            continue
        project_rows.append(row)
        summary = build_project_summary(row)
        emit_progress(
            {
                "stage": "detail_fetch_done",
                "site": "ggzy",
                "current": idx,
                "total": len(all_records),
                "title": record["title"],
                "grouped_projects": len(all_records),
                "core_projects": sum(1 for item in project_rows if build_project_summary(item).get("can_analyze_core")),
            }
        )
    projects: list[dict[str, Any]] = []
    for row in project_rows:
        summary = build_project_summary(row)
        projects.append(
            {
                "project_key": row.get("project_key") or "",
                "status": {
                    "usable": bool(summary.get("can_analyze_core")),
                    "file_complete": bool(summary.get("file_complete")),
                },
                "summary": summary,
                "records": [
                    {
                        "notice_type": infer_notice_type(str(page.get("title") or "")),
                        "title": page.get("title") or "",
                        "publish_time": page.get("publish_time") or "",
                        "url": page.get("url") or "",
                        "extracted": page.get("extracted") or {},
                    }
                    for page in (row.get("detail_pages") or [])
                ],
                "project_code": row.get("project_code") or "",
                "publish_time": row.get("publish_time") or "",
                "province": row.get("province") or "",
                "city": row.get("city") or "",
                "platform": row.get("platform") or "",
                "industry": row.get("industry") or "",
                "list_url": row.get("list_url") or "",
                "detail_entry": row.get("detail_entry") or {},
                "detail_pages": row.get("detail_pages") or [],
            }
        )
    audit = build_project_audit_meta(projects)
    analysis = build_analysis(projects)
    output = {
        "meta": {
            "site": "ggzy",
            "keywords": config.keyword,
            "province": config.province,
            "industry": "工程建设",
            "publish_range": f"{begin} 至 {end}",
            "page_size": None,
            "max_pages": config.max_pages,
            "record_count": len(project_rows),
            "project_count": len(projects),
            "usable_project_count": sum(1 for item in projects if (item.get("status") or {}).get("usable")),
            "file_complete_project_count": sum(1 for item in projects if (item.get("summary") or {}).get("file_complete")),
            "core_analyzable_project_count": sum(1 for item in projects if (item.get("summary") or {}).get("can_analyze_core")),
            "audit": audit,
            "partial_saved_project_count": sum(1 for item in projects if not (item.get("summary") or {}).get("has_bid_quotes")),
        },
        "query": {
            "province": config.province,
            "province_code": PROVINCE_CODES.get(config.province, config.province),
            "city": config.city,
            "classify": config.classify,
            "stage": config.stage,
            "source_type": config.source_type,
            "deal_time": config.deal_time,
            "begin": begin,
            "end": end,
            "keyword": config.keyword,
            "total_records": total,
            "page_total": page_total,
        },
        "pages": pages,
        "analysis": analysis,
        "projects": projects,
    }
    customer_json_path = write_customer_json(config.output_json, output)
    output["meta"]["customer_json_path"] = customer_json_path
    return output


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Stable collector for ggzy.gov.cn")
    parser.add_argument("--province", default="西藏", help="省份名称，默认西藏")
    parser.add_argument("--city", default="", help="城市名称，可留空")
    parser.add_argument("--classify", default="01", help="业务分类代码，默认工程建设")
    parser.add_argument("--stage", default="0104", help="信息类型代码，默认交易结果公示")
    parser.add_argument("--source-type", default="1", help="数据来源代码")
    parser.add_argument("--deal-time", default="02", help="发布时间筛选代码，默认近一月")
    parser.add_argument("--days", type=int, default=30, help="时间范围天数，用于自动计算 TIMEBEGIN/TIMEEND")
    parser.add_argument("--begin", default="", help="开始日期 YYYY-MM-DD，优先级高于 days")
    parser.add_argument("--end", default="", help="结束日期 YYYY-MM-DD，优先级高于 days")
    parser.add_argument("--keyword", default="", help="关键字")
    parser.add_argument("--max-projects", type=int, default=None, help="最多抓取多少条项目")
    parser.add_argument("--max-pages", type=int, default=None, help="最多翻多少页")
    parser.add_argument("--request-min-delay", type=float, default=0.25, help="最小请求间隔秒数")
    parser.add_argument("--request-max-delay", type=float, default=0.9, help="最大请求间隔秒数")
    parser.add_argument("--retry-count", type=int, default=3, help="单次请求最大重试次数")
    parser.add_argument("--cooldown-seconds", type=float, default=20.0, help="code=800 时冷却秒数")
    parser.add_argument("--captcha-dir", default="tmp/ggzy_captcha", help="验证码图片保存目录")
    parser.add_argument("--output-json", default="ggzy_gov_output.json", help="输出 JSON 文件路径")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    config = CrawlConfig(
        province=args.province,
        city=args.city,
        classify=args.classify,
        stage=args.stage,
        source_type=args.source_type,
        deal_time=args.deal_time,
        days=args.days,
        begin=args.begin,
        end=args.end,
        keyword=args.keyword,
        max_projects=args.max_projects,
        max_pages=args.max_pages,
        request_min_delay=args.request_min_delay,
        request_max_delay=args.request_max_delay,
        retry_count=args.retry_count,
        cooldown_seconds=args.cooldown_seconds,
        captcha_dir=args.captcha_dir,
        output_json=args.output_json,
    )
    result = crawl(config)
    out_path = Path(config.output_json)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(
        {
            "output": str(out_path),
            "projects": len(result["projects"]),
            "records": result["query"]["total_records"],
            "pages": result["query"]["page_total"],
        },
        ensure_ascii=False,
        indent=2,
    ))


if __name__ == "__main__":
    main()
