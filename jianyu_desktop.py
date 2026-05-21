import os
import json
import sys
import queue
import threading
import traceback
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from datetime import datetime
from playwright.sync_api import sync_playwright
import jianyu_project_collector as collector


SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "jianyu_project_collector.py")
LOGIN_URL = "https://www.jianyu360.cn/jylab/supsearch/index.html?keywords=&selectType=title&searchGroup=1"


def app_state_dir() -> str:
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or os.getcwd()
        return os.path.join(base, "JianyuDesktop")
    return os.path.join("/tmp", "jianyu_desktop")


APP_STATE_DIR = app_state_dir()
LOGIN_PROFILE_DIR = os.path.join(APP_STATE_DIR, "login_profile")
DEFAULT_COOKIE_PATH = os.path.join(APP_STATE_DIR, "jianyu_cookie.txt")
DIAGNOSTICS_DIR = os.path.join(APP_STATE_DIR, "diagnostics")


def playwright_channel_candidates() -> list[str | None]:
    if sys.platform.startswith("win"):
        return ["msedge", "chrome", None]
    if sys.platform == "darwin":
        return ["chrome", "msedge", None]
    return ["chrome", "msedge", None]


class JianyuDesktopApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("投标项目抓取器")
        self.root.geometry("1080x760")
        self.main_thread_id = threading.get_ident()
        self.ui_task_queue: queue.Queue = queue.Queue()
        self.collection_running = False
        self.worker_thread = None
        self.stop_flag = False
        self.playwright = None
        self.login_context = None
        self.login_page = None
        self.worker_page = None
        self.keep_browser_session_var = tk.BooleanVar(value=True)
        self.run_started_at = 0.0
        self.progress_total = 0
        self.progress_current = 0
        self.search_max_pages = 0
        self.search_page = 0
        self.collected_records = 0
        self.grouped_projects = 0
        self.core_projects = 0
        self.last_progress_at = 0.0
        self.current_detail_title = ""
        self.current_detail_started_at = 0.0
        self.current_progress_base = "暂无进度"
        self.last_status_note = ""
        self.captcha_waiting = False
        self.log_lock = threading.Lock()
        self.run_diag_dir = ""
        self.run_log_path = ""

        self.province_var = tk.StringVar(value="西藏")
        self.keywords_var = tk.StringVar(value="房建")
        self.days_var = tk.StringVar(value="30")
        os.makedirs(APP_STATE_DIR, exist_ok=True)
        os.makedirs(DIAGNOSTICS_DIR, exist_ok=True)
        self.cookie_var = tk.StringVar(value=DEFAULT_COOKIE_PATH)
        self.output_var = tk.StringVar(value=os.path.join(os.path.dirname(__file__), "jianyu_range_output.json"))
        self.report_var = tk.StringVar(value="")
        self.status_var = tk.StringVar(value="就绪")
        self.progress_text_var = tk.StringVar(value="暂无进度")

        self._build_ui()
        self.root.after(50, self._drain_ui_tasks)

    def _build_ui(self) -> None:
        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")

        top = ttk.Frame(self.root, padding=14)
        top.pack(fill="x")

        ttk.Label(top, text="项目关键词").grid(row=0, column=0, sticky="w")
        ttk.Entry(top, textvariable=self.keywords_var, width=36).grid(row=0, column=1, sticky="we", padx=8)

        ttk.Label(top, text="项目地区").grid(row=0, column=2, sticky="w")
        ttk.Entry(top, textvariable=self.province_var, width=18).grid(row=0, column=3, sticky="we", padx=8)

        ttk.Label(top, text="最近天数").grid(row=0, column=4, sticky="w")
        ttk.Entry(top, textvariable=self.days_var, width=8).grid(row=0, column=5, sticky="we", padx=8)

        ttk.Label(top, text="Cookie 文件").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(top, textvariable=self.cookie_var, width=72).grid(row=1, column=1, columnspan=4, sticky="we", padx=8, pady=(10, 0))
        cookie_actions = ttk.Frame(top)
        cookie_actions.grid(row=1, column=5, padx=(0, 8), pady=(10, 0), sticky="e")
        ttk.Button(cookie_actions, text="选择", command=self._pick_cookie).pack(side="left")
        ttk.Button(cookie_actions, text="打开登录页", command=self._open_login_browser).pack(side="left", padx=(8, 0))
        ttk.Button(cookie_actions, text="保存登录态", command=self._save_login_state).pack(side="left", padx=(8, 0))

        ttk.Label(top, text="输出 JSON").grid(row=2, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(top, textvariable=self.output_var, width=72).grid(row=2, column=1, columnspan=4, sticky="we", padx=8, pady=(10, 0))
        ttk.Button(top, text="选择", command=self._pick_output).grid(row=2, column=5, padx=(0, 8), pady=(10, 0))

        ttk.Label(top, text="输出 MD(可留空)").grid(row=3, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(top, textvariable=self.report_var, width=72).grid(row=3, column=1, columnspan=4, sticky="we", padx=8, pady=(10, 0))
        ttk.Button(top, text="选择", command=self._pick_report).grid(row=3, column=5, padx=(0, 8), pady=(10, 0))

        ttk.Checkbutton(
            top,
            text="抓取时复用当前登录浏览器会话",
            variable=self.keep_browser_session_var,
        ).grid(row=4, column=1, columnspan=3, sticky="w", pady=(10, 0))

        top.columnconfigure(1, weight=1)
        top.columnconfigure(3, weight=1)

        actions = ttk.Frame(self.root, padding=(14, 0, 14, 8))
        actions.pack(fill="x")
        self.run_button = ttk.Button(actions, text="开始抓取", command=self.run_capture)
        self.run_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="停止", command=self.stop_capture, state="disabled")
        self.stop_button.pack(side="left", padx=8)

        self.progress = ttk.Progressbar(actions, mode="determinate", length=220, maximum=100)
        self.progress.pack(side="right")

        info = ttk.Frame(self.root, padding=(14, 0, 14, 8))
        info.pack(fill="x")
        ttk.Label(info, textvariable=self.status_var).pack(anchor="w")
        ttk.Label(info, textvariable=self.progress_text_var).pack(anchor="w", pady=(4, 0))

        body = ttk.PanedWindow(self.root, orient=tk.VERTICAL)
        body.pack(fill="both", expand=True, padx=14, pady=8)

        log_frame = ttk.Labelframe(body, text="运行日志", padding=8)
        self.log = tk.Text(log_frame, height=18, wrap="word")
        self.log.pack(fill="both", expand=True)
        body.add(log_frame, weight=2)

        summary_frame = ttk.Labelframe(body, text="结果摘要", padding=8)
        self.summary = tk.Text(summary_frame, height=12, wrap="word")
        self.summary.pack(fill="both", expand=True)
        self.summary.insert("1.0", "暂无结果")
        self.summary.configure(state="disabled")
        body.add(summary_frame, weight=1)

    def _pick_cookie(self) -> None:
        path = filedialog.askopenfilename(title="选择 cookie 文件", filetypes=[("Text", "*.txt"), ("All files", "*.*")])
        if path:
            self.cookie_var.set(path)

    def _pick_output(self) -> None:
        path = filedialog.asksaveasfilename(title="选择输出 JSON", defaultextension=".json", filetypes=[("JSON", "*.json")])
        if path:
            self.output_var.set(path)

    def _pick_report(self) -> None:
        path = filedialog.asksaveasfilename(title="选择输出 MD", defaultextension=".md", filetypes=[("Markdown", "*.md")])
        if path:
            self.report_var.set(path)

    def _launch_login_context(self) -> None:
        if self.login_context is not None:
            if self.login_page is not None:
                try:
                    self.login_page.bring_to_front()
                except Exception:
                    pass
            return
        os.makedirs(LOGIN_PROFILE_DIR, exist_ok=True)
        self.playwright = sync_playwright().start()
        launch_options = {"headless": False}
        browser_type = self.playwright.chromium
        last_error = None
        for channel in playwright_channel_candidates():
            try:
                kwargs = dict(launch_options)
                if channel:
                    kwargs["channel"] = channel
                self.login_context = browser_type.launch_persistent_context(
                    LOGIN_PROFILE_DIR,
                    **kwargs,
                )
                break
            except Exception as exc:
                last_error = exc
                self.login_context = None
        if self.login_context is None:
            raise RuntimeError(f"无法启动浏览器上下文：{last_error}")
        pages = self.login_context.pages
        self.login_page = pages[0] if pages else self.login_context.new_page()
        self.login_page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)

    def _open_login_browser(self) -> None:
        try:
            self._launch_login_context()
            self.status_var.set("登录页已打开，请在浏览器里手动登录并完成验证码。")
            messagebox.showinfo("登录提示", "浏览器已打开。请先在网页里登录剑鱼并完成验证码，然后回到这里点“保存登录态”。")
        except Exception as exc:
            messagebox.showerror("打开失败", f"无法启动登录浏览器：{exc}")

    def _build_cookie_string(self) -> str:
        if self.login_context is None:
            raise RuntimeError("登录浏览器尚未打开。")
        cookies = self.login_context.cookies()
        if not cookies:
            raise RuntimeError("当前浏览器上下文没有可保存的 Cookie。")
        pairs: list[str] = []
        seen: set[str] = set()
        for cookie in cookies:
            name = str(cookie.get("name") or "").strip()
            value = str(cookie.get("value") or "")
            domain = str(cookie.get("domain") or "")
            if not name or "jianyu360.cn" not in domain:
                continue
            if name in seen:
                continue
            seen.add(name)
            pairs.append(f"{name}={value}")
        if not pairs:
            raise RuntimeError("没有提取到 jianyu360.cn 的 Cookie，请确认已登录成功。")
        return "; ".join(pairs)

    def _save_login_state(self) -> None:
        try:
            self._launch_login_context()
            cookie_text = self._build_cookie_string()
            cookie_path = self.cookie_var.get().strip() or DEFAULT_COOKIE_PATH
            with open(cookie_path, "w", encoding="utf-8") as fp:
                fp.write(cookie_text)
            storage_path = f"{cookie_path}.json"
            self.login_context.storage_state(path=storage_path)
            self.cookie_var.set(cookie_path)
            self.status_var.set(f"登录态已保存：{cookie_path}")
            messagebox.showinfo("保存成功", f"登录态已保存。\nCookie: {cookie_path}\nStorage State: {storage_path}")
        except Exception as exc:
            messagebox.showerror("保存失败", f"无法保存登录态：{exc}")

    def _refresh_cookie_from_browser_if_possible(self) -> None:
        if not self.keep_browser_session_var.get():
            return
        if self.login_context is None:
            return
        cookie_text = self._build_cookie_string()
        cookie_path = self.cookie_var.get().strip() or DEFAULT_COOKIE_PATH
        with open(cookie_path, "w", encoding="utf-8") as fp:
            fp.write(cookie_text)

    def _get_request_page(self):
        self._launch_login_context()
        if self.login_context is None:
            raise RuntimeError("登录浏览器尚未打开。")
        if self.worker_page is None or self.worker_page.is_closed():
            self.worker_page = self.login_context.new_page()
        return self.worker_page

    def _browser_request(self, method: str, url: str, payload, headers: dict | None, expect: str):
        if threading.get_ident() != self.main_thread_id:
            return self._run_on_main_thread_sync(self._browser_request, method, url, payload, headers, expect)
        page = self._get_request_page()
        js = """async ({ method, url, payload, headers, expect }) => {
            const options = { method, headers: headers || {}, credentials: 'include' };
            if (payload !== undefined && payload !== null) {
                const contentType = (options.headers['content-type'] || options.headers['Content-Type'] || '').toLowerCase();
                if (contentType.includes('application/json')) {
                    options.body = JSON.stringify(payload);
                } else if (contentType.includes('application/x-www-form-urlencoded')) {
                    options.body = new URLSearchParams(payload).toString();
                } else {
                    options.body = typeof payload === 'string' ? payload : JSON.stringify(payload);
                }
            }
            const response = await fetch(url, options);
            const text = await response.text();
            if (expect === 'json') {
                try {
                    return JSON.parse(text);
                } catch (error) {
                    return { __parse_error__: String(error), __raw_text__: text };
                }
            }
            return text;
        }"""
        try:
            result = page.evaluate(js, {"method": method, "url": url, "payload": payload, "headers": headers or {}, "expect": expect})
            if isinstance(result, dict) and (result.get("__parse_error__") or result.get("antiVerify") is not None or result.get("textVerify") or result.get("imgData")):
                self._write_diag_json(
                    "browser_request_last.json",
                    {
                        "method": method,
                        "url": url,
                        "expect": expect,
                        "headers": headers or {},
                        "payload": payload,
                        "result": result,
                    },
                )
            return result
        except Exception as exc:
            self._write_diag_json(
                "browser_request_error.json",
                {
                    "method": method,
                    "url": url,
                    "expect": expect,
                    "headers": headers or {},
                    "payload": payload,
                    "error": str(exc),
                    "traceback": traceback.format_exc(),
                },
            )
            self._capture_page_debug(page, "browser_request_error_page", url)
            raise

    def _browser_page_content(self, page, target_url: str) -> str:
        page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
        return page.content()

    def _browser_rendered_payload(self, url: str) -> dict:
        if threading.get_ident() != self.main_thread_id:
            return self._run_on_main_thread_sync(self._browser_rendered_payload, url)
        page = self._get_request_page()
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
        if "antiVerify" in text or "textVerify" in text or "imgData" in text:
            self._capture_page_debug(page, "rendered_payload_captcha", url)
        return {"text": text, "tables": table_payload or []}

    def _bring_login_browser_to_front(self, target_url: str | None = None) -> None:
        self._launch_login_context()
        page = self.login_page
        if page is None:
            return
        try:
            if target_url:
                page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        try:
            page.bring_to_front()
        except Exception:
            pass

    def _manual_captcha_handler(self, payload: dict) -> bool:
        self.captcha_waiting = True
        target_url = str(payload.get("url") or LOGIN_URL)
        self._write_diag_json("manual_captcha_start.json", payload)
        self._run_on_main_thread_sync(self.status_var.set, "等待人工处理验证码...")
        self._run_on_main_thread_sync(
            self.progress_text_var.set,
            f"检测到验证码，请在浏览器窗口完成验证。类型={payload.get('scope') or '-'}",
        )
        self._run_on_main_thread_sync(self._bring_login_browser_to_front, target_url)
        deadline = time.time() + 600
        while time.time() < deadline:
            if self.stop_flag:
                self.captcha_waiting = False
                return False
            try:
                page = self._get_request_page()
                html = self._run_on_main_thread_sync(self._browser_page_content, page, target_url)
                if "antiVerify" not in html and "textVerify" not in html and "imgData" not in html:
                    self.captcha_waiting = False
                    self._capture_page_debug(page, "manual_captcha_resolved_page", target_url)
                    self._run_on_main_thread_sync(self.status_var.set, "验证码已通过，继续抓取...")
                    return True
            except Exception:
                pass
            time.sleep(3)
        self.captcha_waiting = False
        try:
            page = self._get_request_page()
            self._capture_page_debug(page, "manual_captcha_timeout_page", target_url)
        except Exception:
            pass
        self._run_on_main_thread_sync(self.status_var.set, "等待人工验证码超时")
        return False

    def _preflight_search_access(self, config: collector.SearchConfig) -> tuple[bool, dict]:
        previous_provider = collector.COOKIE_PROVIDER
        previous_browser_provider = collector.BROWSER_REQUEST_PROVIDER
        try:
            reuse_live_session = self.keep_browser_session_var.get() and self.login_context is not None
            collector.COOKIE_PROVIDER = self._build_cookie_string if reuse_live_session else None
            collector.BROWSER_REQUEST_PROVIDER = self._browser_request if reuse_live_session else None
            result = collector.probe_search_access("", config)
            return bool(result.get("ok")), result
        finally:
            collector.COOKIE_PROVIDER = previous_provider
            collector.BROWSER_REQUEST_PROVIDER = previous_browser_provider

    def _append_log(self, text: str) -> None:
        self.log.insert("end", text)
        self.log.see("end")
        self._write_run_log(text)

    def _start_run_diagnostics(self) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        self.run_diag_dir = os.path.join(DIAGNOSTICS_DIR, stamp)
        os.makedirs(self.run_diag_dir, exist_ok=True)
        self.run_log_path = os.path.join(self.run_diag_dir, "run.log")
        self._write_run_log(f"[diag] start {stamp}\n")

    def _write_run_log(self, text: str) -> None:
        if not self.run_log_path:
            return
        with self.log_lock:
            with open(self.run_log_path, "a", encoding="utf-8") as fp:
                fp.write(text)

    def _write_diag_json(self, name: str, payload: dict) -> None:
        if not self.run_diag_dir:
            return
        path = os.path.join(self.run_diag_dir, name)
        with self.log_lock:
            with open(path, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)

    def _capture_page_debug(self, page, prefix: str, url: str = "") -> None:
        if not self.run_diag_dir:
            return
        safe_prefix = prefix.replace("/", "_")
        try:
            html = page.content()
        except Exception as exc:
            html = f"<capture-error>{exc}</capture-error>"
        try:
            current_url = page.url
        except Exception:
            current_url = url
        meta = {
            "prefix": prefix,
            "requested_url": url,
            "current_url": current_url,
            "captured_at": datetime.now().isoformat(),
            "contains_antiVerify": "antiVerify" in html,
            "contains_textVerify": "textVerify" in html,
            "contains_imgData": "imgData" in html,
            "html_excerpt": html[:4000],
        }
        self._write_diag_json(f"{safe_prefix}.json", meta)
        with self.log_lock:
            with open(os.path.join(self.run_diag_dir, f"{safe_prefix}.html"), "w", encoding="utf-8") as fp:
                fp.write(html)

    def _enqueue_ui(self, callback, *args) -> None:
        self.ui_task_queue.put((callback, args))

    def _run_on_main_thread_sync(self, callback, *args):
        if threading.get_ident() == self.main_thread_id:
            return callback(*args)
        result_queue: queue.Queue = queue.Queue(maxsize=1)

        def wrapped() -> None:
            try:
                result_queue.put((True, callback(*args)))
            except Exception as exc:
                result_queue.put((False, exc))

        self._enqueue_ui(wrapped)
        ok, value = result_queue.get()
        if ok:
            return value
        raise value

    def _drain_ui_tasks(self) -> None:
        try:
            while True:
                callback, args = self.ui_task_queue.get_nowait()
                try:
                    callback(*args)
                except Exception:
                    pass
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(50, self._drain_ui_tasks)

    def _emit_log(self, text: str) -> None:
        self._enqueue_ui(self._append_log, text)

    def _format_eta(self, seconds: float) -> str:
        seconds = max(0, int(seconds))
        mins, secs = divmod(seconds, 60)
        hours, mins = divmod(mins, 60)
        if hours:
            return f"{hours}小时{mins}分{secs}秒"
        if mins:
            return f"{mins}分{secs}秒"
        return f"{secs}秒"

    def _update_progress_ui(
        self,
        stage: str,
        current: int = 0,
        total: int = 0,
        title: str = "",
        page: int = 0,
        max_pages: int = 0,
        page_items: int = 0,
        collected_records: int = 0,
        grouped_projects: int = 0,
        core_projects: int = 0,
    ) -> None:
        self.last_progress_at = time.time()
        elapsed = max(0.0, time.time() - self.run_started_at) if self.run_started_at else 0.0
        progress_value = 0.0
        eta_text = "-"
        if max_pages:
            self.search_max_pages = max_pages
        if page:
            self.search_page = page
        if collected_records:
            self.collected_records = collected_records
        if grouped_projects:
            self.grouped_projects = grouped_projects
        self.core_projects = core_projects or self.core_projects
        if total > 0:
            self.progress_total = total
            self.progress_current = min(current, total)
            if stage.startswith("detail_"):
                progress_value = self.progress_current / total * 100.0
            elif self.search_max_pages > 0:
                progress_value = self.search_page / self.search_max_pages * 35.0
            if self.progress_current >= 2:
                eta = elapsed / self.progress_current * (total - self.progress_current)
                eta_text = self._format_eta(eta)
        elif self.search_max_pages > 0:
            progress_value = self.search_page / self.search_max_pages * 35.0
        self.progress["value"] = progress_value

        if stage == "search_start":
            self.current_progress_base = "第一阶段: 准备抓取列表"
            self.progress_text_var.set(self.current_progress_base)
        elif stage == "search_page_start":
            self.current_progress_base = (
                f"第一阶段: 已搜 {max(page-1,0)}/{max_pages} 页 | 当前抓取第 {page} 页 | 已有 {self.collected_records} 条记录 | {self.grouped_projects} 个项目"
            )
            self.progress_text_var.set(self.current_progress_base)
        elif stage == "search_page_loaded":
            self.current_progress_base = (
                f"第一阶段: 第 {page}/{max_pages} 页已返回 | 本页原始 {page_items} 条 | 过滤后累计 {self.collected_records} 条 | {self.grouped_projects} 个项目"
            )
            self.progress_text_var.set(self.current_progress_base)
        elif stage == "search_page_done":
            self.current_progress_base = (
                f"第一阶段: 已搜 {page}/{max_pages} 页 | 已拿到 {self.collected_records} 条记录 | 已归并 {self.grouped_projects} 个项目"
            )
            self.progress_text_var.set(self.current_progress_base)
        elif stage == "detail_plan_ready":
            self.progress["value"] = 35.0
            self.current_progress_base = (
                f"第二阶段: 待补详情 {total} 条 | 当前 {self.grouped_projects} 个项目 | 已有核心完整 {self.core_projects} 个"
            )
            self.progress_text_var.set(self.current_progress_base)
        elif stage == "captcha_detected":
            self.current_progress_base = "第一阶段: 检测到验证码风控，不是无结果，正在尝试自动通过"
            self.progress_text_var.set(self.current_progress_base)
        elif stage == "captcha_auto_attempt_start":
            self.current_progress_base = "第一阶段: 正在自动处理验证码"
            self.progress_text_var.set(self.current_progress_base)
        elif stage == "captcha_auto_attempt_success":
            self.current_progress_base = "第一阶段: 验证码已自动通过，继续抓取"
            self.progress_text_var.set(self.current_progress_base)
        elif stage == "captcha_auto_attempt_failed":
            self.current_progress_base = "第一阶段: 验证码自动处理失败，需要更新登录态或人工处理"
            self.progress_text_var.set(self.current_progress_base)
        elif stage == "captcha_manual_required":
            self.current_progress_base = "已暂停: 等待你在浏览器窗口里手动完成验证码"
            self.progress_text_var.set(self.current_progress_base)
            self.status_var.set("等待人工处理验证码...")
        elif stage == "captcha_manual_resolved":
            self.current_progress_base = "验证码已人工通过，继续抓取"
            self.progress_text_var.set(self.current_progress_base)
            self.status_var.set("验证码已通过，继续抓取...")
        elif stage == "detail_fetch_start":
            self.current_detail_title = short_title = title[:28] + "..." if len(title) > 28 else title
            self.current_detail_started_at = time.time()
            self.current_progress_base = (
                f"第二阶段: 已补 {self.progress_current}/{self.progress_total or total} 条 | 核心完整 {self.core_projects} 个 | 正在抓取 {short_title or '-'}"
            )
            self.progress_text_var.set(self.current_progress_base)
        elif stage in {"detail_fetch_start", "detail_fetch_done"}:
            short_title = title[:28] + "..." if len(title) > 28 else title
            if stage == "detail_fetch_done":
                self.current_detail_title = ""
                self.current_detail_started_at = 0.0
                self.current_progress_base = (
                    f"第二阶段: 已补 {self.progress_current}/{self.progress_total or total} 条 | 核心完整 {self.core_projects} 个 | 预估剩余 {eta_text} | {short_title or '-'}"
                )
                self.progress_text_var.set(self.current_progress_base)
        else:
            self.current_progress_base = f"阶段: {stage}"
            self.progress_text_var.set(self.current_progress_base)

    def _tick_runtime_feedback(self) -> None:
        if not self.collection_running:
            return
        now = time.time()
        text = self.current_progress_base or "运行中"
        if self.current_detail_started_at > 0:
            current_elapsed = self._format_eta(now - self.current_detail_started_at)
            stale_elapsed = self._format_eta(now - self.last_progress_at) if self.last_progress_at else "0秒"
            text = f"{text} | 当前条已耗时 {current_elapsed} | 距上次进度 {stale_elapsed}"
        elif self.last_progress_at > 0:
            stale_elapsed = self._format_eta(now - self.last_progress_at)
            text = f"{text} | 距上次进度 {stale_elapsed}"
        self.progress_text_var.set(text)
        self.root.after(1000, self._tick_runtime_feedback)

    def _handle_progress_line(self, line: str) -> bool:
        prefix = "__PROGRESS__"
        if not line.startswith(prefix):
            return False
        try:
            payload = json.loads(line[len(prefix):])
        except Exception:
            return False
        self._update_progress_ui(
            stage=str(payload.get("stage") or ""),
            current=int(payload.get("current") or 0),
            total=int(payload.get("total") or payload.get("detail_plan_total") or 0),
            title=str(payload.get("title") or ""),
            page=int(payload.get("page") or 0),
            max_pages=int(payload.get("max_pages") or 0),
            page_items=int(payload.get("page_items") or 0),
            collected_records=int(payload.get("collected_records") or 0),
            grouped_projects=int(payload.get("grouped_projects") or 0),
            core_projects=int(payload.get("core_projects") or 0),
        )
        return True

    def _handle_progress_payload(self, payload: dict) -> None:
        self._write_run_log("__PROGRESS__" + json.dumps(payload, ensure_ascii=False) + "\n")
        self.root.after(0, self._update_progress_ui,
                        str(payload.get("stage") or ""),
                        int(payload.get("current") or 0),
                        int(payload.get("total") or payload.get("detail_plan_total") or 0),
                        str(payload.get("title") or ""),
                        int(payload.get("page") or 0),
                        int(payload.get("max_pages") or 0),
                        int(payload.get("page_items") or 0),
                        int(payload.get("collected_records") or 0),
                        int(payload.get("grouped_projects") or 0),
                        int(payload.get("core_projects") or 0))

    def _set_summary(self, text: str) -> None:
        self.summary.configure(state="normal")
        self.summary.delete("1.0", "end")
        self.summary.insert("1.0", text)
        self.summary.configure(state="disabled")

    def _render_summary_from_output(self, output_path: str) -> None:
        if not output_path or not os.path.exists(output_path):
            self._set_summary("未找到输出文件。")
            return
        try:
            payload = json.loads(open(output_path, "r", encoding="utf-8").read())
        except Exception as exc:
            self._set_summary(f"结果读取失败：{exc}")
            return
        lines: list[str] = []
        meta = payload.get("meta") or {}
        days_text = self.days_var.get().strip() or "-"
        lines.append(f"搜索条件: 地区={self.province_var.get().strip() or '-'} | 关键词={self.keywords_var.get().strip() or '-'} | 最近天数={days_text}")
        lines.append(f"抓取记录数: {meta.get('record_count', 0)}")
        lines.append(f"项目总数: {meta.get('project_count', 0)}")
        lines.append(f"核心字段齐全项目数: {meta.get('core_analyzable_project_count', 0)}")
        lines.append(f"严格三文件完整项目数: {meta.get('file_complete_project_count', 0)}")
        lines.append(f"客户版JSON: {meta.get('customer_json_path') or '-'}")
        lines.append("")
        core_projects = [project for project in (payload.get("projects") or []) if (project.get("summary") or {}).get("can_analyze_core")]
        if not core_projects:
            lines.append("这个时间段内没有找到核心字段齐全的项目。")
        for index, project in enumerate(core_projects, start=1):
            summary = project.get("summary") or {}
            avg_down_rate = summary.get("avg_down_rate")
            winning_down_rate = summary.get("winning_down_rate")
            lines.append(f"[项目 {index}] {summary.get('project_title') or project.get('project_key') or '-'}")
            lines.append(f"文件类型: {', '.join(summary.get('notice_types_present') or []) or '-'}")
            lines.append(f"控制价: {summary.get('control_price') if summary.get('control_price') is not None else '-'}")
            lines.append(f"中标价: {summary.get('winning_price') if summary.get('winning_price') is not None else '-'}")
            lines.append(f"中标单位: {summary.get('winning_company') or '-'}")
            lines.append(f"报价家数: {summary.get('bid_quote_count', 0)}")
            lines.append(f"平均下浮率: {f'{avg_down_rate:.4f}%' if avg_down_rate is not None else '-'}")
            lines.append(f"中标下浮率: {f'{winning_down_rate:.4f}%' if winning_down_rate is not None else '-'}")
            lines.append(f"缺失项: {', '.join(summary.get('issues') or []) or '无'}")
            lines.append("")
        self._set_summary("\n".join(lines).strip() or "暂无结果")

    def run_capture(self) -> None:
        if self.collection_running:
            messagebox.showinfo("提示", "任务正在运行。")
            return
        keywords = self.keywords_var.get().strip()
        if not keywords:
            messagebox.showwarning("提示", "请输入项目关键词。")
            return
        days_text = self.days_var.get().strip()
        try:
            recent_days = int(days_text)
        except ValueError:
            messagebox.showwarning("提示", "最近天数必须是整数。")
            return
        if recent_days <= 0:
            messagebox.showwarning("提示", "最近天数必须大于 0。")
            return
        province = self.province_var.get().strip() or "西藏"
        keep_browser_session = self.keep_browser_session_var.get()
        cookie_path = self.cookie_var.get().strip()
        output = self.output_var.get().strip()
        report = self.report_var.get().strip()
        if keep_browser_session and self.login_context is None:
            messagebox.showwarning("提示", "当前已勾选“复用当前登录浏览器会话”，请先点“打开登录页”并在浏览器里完成登录/验证码。")
            return
        if not cookie_path or not os.path.exists(cookie_path):
            messagebox.showwarning("提示", "请选择有效的 cookie 文件。")
            return
        try:
            self._refresh_cookie_from_browser_if_possible()
        except Exception as exc:
            messagebox.showwarning("提示", f"无法从当前浏览器会话刷新登录态，将继续使用现有 Cookie 文件。\n原因：{exc}")
        cmd = [
            sys.executable,
            SCRIPT_PATH,
            "--keywords", keywords,
            "--province", province,
            "--recent-days", str(recent_days),
            "--cookie-file", cookie_path,
            "--output", output,
            "--fetch-details",
            "--detail-limit", "120",
            "--captcha-auto-attempts", "3",
        ]
        if report:
            cmd.extend(["--report-md", report])
        run_mode = "同一浏览器会话抓取" if keep_browser_session and self.login_context is not None else "Cookie 文件抓取"
        self._start_run_diagnostics()
        self._write_diag_json(
            "run_context.json",
            {
                "keywords": keywords,
                "province": province,
                "recent_days": recent_days,
                "cookie_path": cookie_path,
                "output": output,
                "report": report,
                "keep_browser_session": keep_browser_session,
                "login_context_ready": self.login_context is not None,
                "mode": run_mode,
            },
        )
        self._append_log(f"[模式] {run_mode}\n")
        self._append_log(f"$ {' '.join(cmd)}\n")
        self.run_started_at = time.time()
        self.progress_total = 0
        self.progress_current = 0
        self.search_max_pages = 0
        self.search_page = 0
        self.collected_records = 0
        self.grouped_projects = 0
        self.core_projects = 0
        self.last_progress_at = time.time()
        self.current_detail_title = ""
        self.current_detail_started_at = 0.0
        self.current_progress_base = "阶段: 启动中"
        self.status_var.set("运行中...")
        self.progress["value"] = 0
        self.progress_text_var.set(self.current_progress_base)
        self.run_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.stop_flag = False
        self.collection_running = True
        self.root.after(1000, self._tick_runtime_feedback)

        def worker() -> None:
            try:
                config = collector.SearchConfig(
                    keywords=keywords,
                    province=province,
                    industry="建筑工程",
                    publish_range=collector.build_publish_range(recent_days),
                    page_size=50,
                    max_pages=20,
                    cookie_file=cookie_path,
                    output=output,
                    report_md=report,
                    fetch_details=True,
                    detail_limit=120,
                    captcha_auto_attempts=3,
                )
                if keep_browser_session and self.login_context is not None:
                    preflight_ok, preflight = self._run_on_main_thread_sync(self._preflight_search_access, config)
                    self._run_on_main_thread_sync(self._append_log, "[预检] " + json.dumps(preflight, ensure_ascii=False) + "\n")
                    if not preflight_ok:
                        if preflight.get("has_captcha"):
                            resumed = self._manual_captcha_handler(
                                {
                                    "scope": "preflight_search",
                                    "url": LOGIN_URL,
                                    "message": "预检触发验证码，等待人工处理后重新预检。",
                                }
                            )
                            if resumed:
                                preflight_ok, preflight = self._run_on_main_thread_sync(self._preflight_search_access, config)
                                self._run_on_main_thread_sync(self._append_log, "[预检重试] " + json.dumps(preflight, ensure_ascii=False) + "\n")
                                if not preflight_ok:
                                    self._run_on_main_thread_sync(self.status_var.set, "预检未通过：人工验证后仍未放行")
                                    self._run_on_main_thread_sync(
                                        self.progress_text_var.set,
                                        "你已完成一次人工验证，但搜索接口仍未放行，当前任务未进入正式抓取。",
                                    )
                                    self._run_on_main_thread_sync(self._finish, "预检未通过")
                                    return
                            else:
                                self._run_on_main_thread_sync(self.status_var.set, "预检未通过：未完成人工验证码")
                                self._run_on_main_thread_sync(
                                    self.progress_text_var.set,
                                    "预检检测到验证码，当前已停止。请在浏览器完成验证后重新开始。",
                                )
                                self._run_on_main_thread_sync(self._finish, "预检未通过")
                                return
                        self._run_on_main_thread_sync(self._finish, "预检失败：搜索页不可用")
                        return
                previous_provider = collector.COOKIE_PROVIDER
                previous_callback = collector.PROGRESS_CALLBACK
                previous_stop = collector.STOP_REQUESTED
                previous_browser_provider = collector.BROWSER_REQUEST_PROVIDER
                previous_rendered_provider = collector.BROWSER_RENDERED_PROVIDER
                previous_manual_captcha = collector.MANUAL_CAPTCHA_HANDLER
                reuse_live_session = keep_browser_session and self.login_context is not None
                collector.COOKIE_PROVIDER = self._build_cookie_string if reuse_live_session else None
                collector.PROGRESS_CALLBACK = self._handle_progress_payload
                collector.STOP_REQUESTED = lambda: self.stop_flag
                collector.BROWSER_REQUEST_PROVIDER = self._browser_request if reuse_live_session else None
                collector.BROWSER_RENDERED_PROVIDER = self._browser_rendered_payload if reuse_live_session else None
                collector.MANUAL_CAPTCHA_HANDLER = self._manual_captcha_handler if reuse_live_session else None
                try:
                    output_payload = collector.run_collection(config)
                finally:
                    collector.COOKIE_PROVIDER = previous_provider
                    collector.PROGRESS_CALLBACK = previous_callback
                    collector.STOP_REQUESTED = previous_stop
                    collector.BROWSER_REQUEST_PROVIDER = previous_browser_provider
                    collector.BROWSER_RENDERED_PROVIDER = previous_rendered_provider
                    collector.MANUAL_CAPTCHA_HANDLER = previous_manual_captcha
                if self.stop_flag:
                    self._enqueue_ui(self._finish, "已停止")
                else:
                    self._enqueue_ui(self._render_summary_from_output, output)
                    self._enqueue_ui(self._append_log, json.dumps(output_payload.get("meta") or {}, ensure_ascii=False, indent=2) + "\n")
                    self._enqueue_ui(self._finish, "完成")
            except Exception as exc:
                self._enqueue_ui(self._finish, f"失败：{exc}")

        self.worker_thread = threading.Thread(target=worker, daemon=True)
        self.worker_thread.start()

    def stop_capture(self) -> None:
        self.stop_flag = True
        self._append_log("\n[停止请求已发送]\n")
        self.status_var.set("正在停止...")

    def _finish(self, status: str) -> None:
        self.collection_running = False
        self.worker_thread = None
        elapsed = max(0.0, time.time() - self.run_started_at) if self.run_started_at else 0.0
        self._write_diag_json(
            "final_status.json",
            {
                "status": status,
                "elapsed_seconds": elapsed,
                "progress_total": self.progress_total,
                "progress_current": self.progress_current,
                "search_page": self.search_page,
                "search_max_pages": self.search_max_pages,
                "collected_records": self.collected_records,
                "grouped_projects": self.grouped_projects,
                "core_projects": self.core_projects,
                "last_progress_at": self.last_progress_at,
                "current_progress_base": self.current_progress_base,
            },
        )
        self.progress["value"] = 100 if status == "完成" else self.progress["value"]
        self.run_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.status_var.set(status)
        if status == "完成":
            self.progress_text_var.set(f"已完成，用时 {self._format_eta(elapsed)}")
        elif status == "已停止":
            self.progress_text_var.set("任务已停止")
        elif status == "预检未通过":
            pass
        else:
            self.progress_text_var.set(status)

    def _close_login_browser(self) -> None:
        if self.login_context is not None:
            try:
                self.login_context.close()
            except Exception:
                pass
        if self.playwright is not None:
            try:
                self.playwright.stop()
            except Exception:
                pass
        self.login_context = None
        self.login_page = None
        self.worker_page = None
        self.playwright = None

    def _on_close(self) -> None:
        self.stop_flag = True
        self._close_login_browser()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = JianyuDesktopApp(root)
    root.protocol("WM_DELETE_WINDOW", app._on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
