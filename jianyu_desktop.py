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
import ggzy_gov_collector as ggzy_collector
from customer_json_utils import write_customer_json_splits


SCRIPT_PATH = os.path.join(os.path.dirname(__file__), "jianyu_project_collector.py")
LOGIN_URL = "https://www.jianyu360.cn/jylab/supsearch/index.html?keywords=&selectType=title&searchGroup=1"
GGZY_URL = "https://www.ggzy.gov.cn/deal/dealList.html"


def app_state_dir() -> str:
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("TEMP") or os.getcwd()
        return os.path.join(base, "JianyuDesktop")
    return os.path.join("/tmp", "jianyu_desktop")


def default_output_path() -> str:
    home_dir = os.path.expanduser("~")
    desktop_dir = os.path.join(home_dir, "Desktop")
    if os.path.isdir(desktop_dir):
        return os.path.join(desktop_dir, "bid_sources_output.json")
    if os.path.isdir(home_dir):
        return os.path.join(home_dir, "bid_sources_output.json")
    return os.path.join(os.path.dirname(__file__), "bid_sources_output.json")


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
        self.shutting_down = False
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
        self.last_progress_site = ""
        self.log_lock = threading.Lock()
        self.run_diag_dir = ""
        self.run_log_path = ""

        self.province_var = tk.StringVar(value="西藏")
        self.days_var = tk.StringVar(value="30")
        os.makedirs(APP_STATE_DIR, exist_ok=True)
        os.makedirs(DIAGNOSTICS_DIR, exist_ok=True)
        self.cookie_var = tk.StringVar(value=DEFAULT_COOKIE_PATH)
        self.output_var = tk.StringVar(value=default_output_path())
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

        ttk.Label(top, text="项目地区").grid(row=0, column=0, sticky="w")
        self.province_entry = ttk.Entry(top, textvariable=self.province_var, width=18)
        self.province_entry.grid(row=0, column=1, sticky="we", padx=8)

        ttk.Label(top, text="最近天数").grid(row=0, column=2, sticky="w")
        self.days_entry = ttk.Entry(top, textvariable=self.days_var, width=8)
        self.days_entry.grid(row=0, column=3, sticky="w", padx=8)

        self.cookie_label = ttk.Label(top, text="剑鱼 Cookie")
        self.cookie_label.grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.cookie_entry = ttk.Entry(top, textvariable=self.cookie_var, width=72)
        self.cookie_entry.grid(row=1, column=1, columnspan=3, sticky="we", padx=8, pady=(10, 0))
        self.cookie_actions = ttk.Frame(top)
        self.cookie_actions.grid(row=1, column=4, padx=(0, 8), pady=(10, 0), sticky="e")
        ttk.Button(self.cookie_actions, text="选择", command=self._pick_cookie).pack(side="left")
        self.login_button = ttk.Button(self.cookie_actions, text="打开登录页", command=self._open_login_browser)
        self.login_button.pack(side="left", padx=(8, 0))
        self.save_login_button = ttk.Button(self.cookie_actions, text="保存登录态", command=self._save_login_state)
        self.save_login_button.pack(side="left", padx=(8, 0))

        ttk.Label(top, text="输出数据库 JSON").grid(row=2, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(top, textvariable=self.output_var, width=72).grid(row=2, column=1, columnspan=3, sticky="we", padx=8, pady=(10, 0))
        ttk.Button(top, text="选择", command=self._pick_output).grid(row=2, column=4, padx=(0, 8), pady=(10, 0))

        self.keep_browser_check = ttk.Checkbutton(
            top,
            text="剑鱼抓取时复用已保存浏览器会话（全国公共资源不需要登录态）",
            variable=self.keep_browser_session_var,
        )
        self.keep_browser_check.grid(row=3, column=0, columnspan=4, sticky="w", pady=(10, 0))

        top.columnconfigure(1, weight=1)
        top.columnconfigure(2, weight=0)
        top.columnconfigure(3, weight=0)

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
        self.root.title("投标项目抓取器 - 双源版")
        self.status_var.set("就绪：默认同时抓取剑鱼和全国公共资源，Cookie 只用于剑鱼。")

    def _pick_cookie(self) -> None:
        path = filedialog.askopenfilename(title="选择 cookie 文件", filetypes=[("Text", "*.txt"), ("All files", "*.*")])
        if path:
            self.cookie_var.set(path)

    def _pick_output(self) -> None:
        path = filedialog.asksaveasfilename(title="选择输出 JSON", defaultextension=".json", filetypes=[("JSON", "*.json")])
        if path:
            self.output_var.set(path)

    def _on_site_changed(self) -> None:
        self.root.title("投标项目抓取器 - 双源版")
        if not self.output_var.get().strip():
            self.output_var.set(default_output_path())
        self.status_var.set("就绪：默认同时抓取剑鱼和全国公共资源，Cookie 只用于剑鱼。")

    def _launch_login_context(self) -> None:
        if self.shutting_down:
            raise RuntimeError("application is closing")
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
        try:
            if not self.login_page.url or "jianyu360.cn" not in self.login_page.url:
                self.login_page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            try:
                self.login_page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
            except Exception:
                pass

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
        if self.login_context is None:
            return
        try:
            cookie_text = self._build_cookie_string()
            cookie_path = self.cookie_var.get().strip() or DEFAULT_COOKIE_PATH
            with open(cookie_path, "w", encoding="utf-8") as fp:
                fp.write(cookie_text)
        except Exception:
            pass

    def _get_request_page(self):
        if self.shutting_down:
            raise RuntimeError("application is closing")
        self._launch_login_context()
        if self.login_context is None:
            raise RuntimeError("登录浏览器尚未打开。")
        if self.worker_page is None or self.worker_page.is_closed():
            self.worker_page = self.login_context.new_page()
        try:
            current_url = self.worker_page.url or ""
        except Exception:
            current_url = ""
        if "jianyu360.cn" not in current_url:
            self.worker_page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
        return self.worker_page

    def _browser_request(self, method: str, url: str, payload, headers: dict | None, expect: str):
        if threading.get_ident() != self.main_thread_id:
            return self._run_on_main_thread_sync(self._browser_request, method, url, payload, headers, expect)
        page = self._get_request_page()
        js = """async ({ method, url, payload, headers, expect }) => {
            async function decryptJianyuPayload(data) {
                if (!data || typeof data !== 'object' || data.antiEncrypt !== 1 || !data.data || !data.secretKey) {
                    return data;
                }
                if (!window.__jianyuDecryptor) {
                    window.__jianyuDecryptor = {
                        iframe: null,
                        iframeName: '',
                        readyPromise: null,
                        pending: {},
                    };
                }
                const decryptor = window.__jianyuDecryptor;
                if (!decryptor.readyPromise) {
                    decryptor.readyPromise = new Promise((resolve, reject) => {
                        const iframe = document.createElement('iframe');
                        iframe.name = `jianyu_decrypt_${Date.now()}_${Math.random().toString(36).slice(2)}`;
                        iframe.src = '/page_decrypt/index.html';
                        iframe.style.display = 'none';
                        iframe.onload = () => resolve();
                        iframe.onerror = () => reject(new Error('解密 iframe 加载失败'));
                        document.body.appendChild(iframe);
                        decryptor.iframe = iframe;
                        decryptor.iframeName = iframe.name;

                        if (!window.__jianyuDecryptMessageListenerInstalled) {
                            window.__jianyuDecryptMessageListenerInstalled = true;
                            window.addEventListener('message', (event) => {
                                const message = event.data || {};
                                if (message.type !== 'after-decrypt' || !message.id) {
                                    return;
                                }
                                const task = window.__jianyuDecryptor && window.__jianyuDecryptor.pending[message.id];
                                if (!task) {
                                    return;
                                }
                                delete window.__jianyuDecryptor.pending[message.id];
                                if (message.plainText) {
                                    task.resolve(message);
                                } else {
                                    task.reject(new Error(message.error || '解密 iframe 未返回明文'));
                                }
                            });
                        }
                    });
                }
                await decryptor.readyPromise;
                return await new Promise((resolve, reject) => {
                    const id = `jy_${Date.now()}_${Math.random().toString(36).slice(2)}_${decryptor.iframeName}`;
                    const timer = setTimeout(() => {
                        delete decryptor.pending[id];
                        reject(new Error('解密 iframe 超时'));
                    }, 15000);
                    decryptor.pending[id] = {
                        resolve: (message) => {
                            clearTimeout(timer);
                            try {
                                resolve(JSON.parse(message.plainText));
                            } catch (error) {
                                reject(error);
                            }
                        },
                        reject: (error) => {
                            clearTimeout(timer);
                            reject(error);
                        },
                    };
                    const targetWindow = decryptor.iframe && decryptor.iframe.contentWindow;
                    if (!targetWindow) {
                        clearTimeout(timer);
                        delete decryptor.pending[id];
                        reject(new Error('解密 iframe 不可用'));
                        return;
                    }
                    targetWindow.postMessage(
                        {
                            id,
                            base64Key: data.secretKey,
                            cipherText: data.data,
                            fromOrigin: location.origin,
                            type: 'decrypt',
                        },
                        location.origin
                    );
                });
            }
            const hasJQuery = typeof window.$ === 'function' && typeof window.$.ajax === 'function';
            let result;
            if (hasJQuery) {
                const ajaxOptions = {
                    url,
                    type: method,
                    method,
                    headers: headers || {},
                    xhrFields: { withCredentials: true },
                    crossDomain: false,
                    dataType: expect === 'json' ? 'json' : 'text',
                };
                if (payload !== undefined && payload !== null) {
                    const contentType = (ajaxOptions.headers['content-type'] || ajaxOptions.headers['Content-Type'] || '').toLowerCase();
                    if (contentType.includes('application/json')) {
                        ajaxOptions.contentType = 'application/json; charset=UTF-8';
                        ajaxOptions.processData = false;
                        ajaxOptions.data = JSON.stringify(payload);
                    } else if (contentType.includes('application/x-www-form-urlencoded')) {
                        ajaxOptions.data = new URLSearchParams(payload).toString();
                    } else {
                        ajaxOptions.processData = false;
                        ajaxOptions.data = typeof payload === 'string' ? payload : JSON.stringify(payload);
                    }
                }
                result = await new Promise((resolve, reject) => {
                    ajaxOptions.success = (data) => resolve(data);
                    ajaxOptions.error = (jqXHR, textStatus, errorThrown) => {
                        reject({
                            __ajax_error__: true,
                            status: jqXHR && jqXHR.status,
                            statusText: jqXHR && jqXHR.statusText,
                            textStatus,
                            errorThrown: String(errorThrown || ''),
                            responseText: jqXHR && jqXHR.responseText ? jqXHR.responseText : '',
                        });
                    };
                    window.$.ajax(ajaxOptions);
                });
            } else {
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
                        result = JSON.parse(text);
                    } catch (error) {
                        result = { __parse_error__: String(error), __raw_text__: text };
                    }
                } else {
                    result = text;
                }
            }
            if (expect === 'json') {
                try {
                    return await decryptJianyuPayload(result);
                } catch (error) {
                    if (result && typeof result === 'object') {
                        return { ...result, __decrypt_error__: String(error) };
                    }
                    return { __decrypt_error__: String(error), __raw_text__: String(result || '') };
                }
            }
            return result;
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

    def _browser_current_page_state(self, page) -> dict:
        try:
            current_url = page.url or ""
        except Exception:
            current_url = ""
        try:
            html = page.content()
        except Exception as exc:
            html = f"<capture-error>{exc}</capture-error>"
        return {"url": current_url, "html": html}

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
        while True:
            if self.stop_flag:
                self.captcha_waiting = False
                return False
            try:
                page = self.login_page
                if page is None:
                    self._run_on_main_thread_sync(self._launch_login_context)
                    page = self.login_page
                if page is None:
                    time.sleep(3)
                    continue
                state = self._run_on_main_thread_sync(self._browser_current_page_state, page)
                current_url = str(state.get("url") or "")
                html = str(state.get("html") or "")
                if "antiVerify" not in html and "textVerify" not in html and "imgData" not in html:
                    self.captcha_waiting = False
                    self._run_on_main_thread_sync(self._capture_page_debug, page, "manual_captcha_resolved_page", target_url)
                    self._run_on_main_thread_sync(self._refresh_cookie_from_browser_if_possible)
                    if "/front/notFind" in current_url or "该页面信息不存在" in html:
                        self._run_on_main_thread_sync(
                            self.status_var.set,
                            "验证码已处理，当前详情页不存在，跳过继续...",
                        )
                    else:
                        self._run_on_main_thread_sync(self.status_var.set, "验证码已通过，继续抓取...")
                    return True
            except Exception:
                pass
            time.sleep(3)

    def _prompt_ggzy_captcha_code(self, image_path: str) -> str:
        dialog = tk.Toplevel(self.root)
        dialog.title("ggzy 验证码")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        dialog.grab_set()

        container = ttk.Frame(dialog, padding=14)
        container.pack(fill="both", expand=True)

        ttk.Label(container, text="列表接口触发验证码，请输入图片中的字符后继续。").pack(anchor="w")
        ttk.Label(container, text=f"图片路径: {image_path}").pack(anchor="w", pady=(4, 10))

        image_label = ttk.Label(container)
        image_label.pack(anchor="center", pady=(0, 10))
        photo = None
        try:
            photo = tk.PhotoImage(file=image_path)
            image_label.configure(image=photo)
            image_label.image = photo
        except Exception:
            image_label.configure(text="无法直接加载验证码图片，请打开图片路径查看。")

        code_var = tk.StringVar(value="")
        entry = ttk.Entry(container, textvariable=code_var, width=24)
        entry.pack(fill="x")
        entry.focus_set()

        result = {"code": ""}

        button_row = ttk.Frame(container)
        button_row.pack(fill="x", pady=(12, 0))

        def accept() -> None:
            result["code"] = code_var.get().strip()
            dialog.destroy()

        def cancel() -> None:
            result["code"] = ""
            dialog.destroy()

        ttk.Button(button_row, text="确定", command=accept).pack(side="right")
        ttk.Button(button_row, text="取消", command=cancel).pack(side="right", padx=(0, 8))
        dialog.protocol("WM_DELETE_WINDOW", cancel)
        dialog.wait_window()
        return result["code"]

    def _manual_ggzy_captcha_handler(self, payload: dict) -> str:
        image_path = str(payload.get("captcha_image_path") or "")
        self._run_on_main_thread_sync(self.status_var.set, "ggzy 列表接口触发验证码，请手动输入。")
        self._run_on_main_thread_sync(
            self.progress_text_var.set,
            f"ggzy 验证码待输入 | 图片: {os.path.basename(image_path) if image_path else '-'}",
        )
        self._write_diag_json("ggzy_manual_captcha_start.json", payload)
        code = self._run_on_main_thread_sync(self._prompt_ggzy_captcha_code, image_path)
        if code:
            self._run_on_main_thread_sync(self.status_var.set, "ggzy 验证码已输入，继续抓取...")
        else:
            self._run_on_main_thread_sync(self.status_var.set, "ggzy 验证码已取消")
        return code

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
        processed = 0
        try:
            while True:
                if processed >= 80:
                    break
                callback, args = self.ui_task_queue.get_nowait()
                try:
                    callback(*args)
                except Exception:
                    pass
                processed += 1
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
        message: str = "",
        url: str = "",
        seconds: float = 0.0,
        reason: str = "",
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
            prefix = "第一阶段: 准备抓取列表"
            if self.last_progress_site == "ggzy":
                prefix = "第一阶段: 准备抓取 ggzy 列表"
            self.current_progress_base = prefix
            self.progress_text_var.set(self.current_progress_base)
        elif stage == "search_page_start":
            self.current_progress_base = (
                f"第一阶段: 已搜 {max(page-1,0)}/{max_pages} 页 | 当前抓取第 {page} 页 | 已有 {self.collected_records} 条记录 | {self.grouped_projects} 个项目"
            )
            self.progress_text_var.set(self.current_progress_base)
        elif stage == "search_page_loaded":
            self.current_progress_base = (
                f"第一阶段: 第 {page}/{max_pages} 页已返回 | 本页命中 {page_items} 条 | 累计 {self.collected_records} 条 | {self.grouped_projects} 个项目"
            )
            self.progress_text_var.set(self.current_progress_base)
        elif stage == "search_page_done":
            self.current_progress_base = (
                f"第一阶段: 已搜 {page}/{max_pages} 页 | 已拿到 {self.collected_records} 条记录 | 已归并 {self.grouped_projects} 个项目"
            )
            self.progress_text_var.set(self.current_progress_base)
        elif stage == "search_source_done":
            self.current_progress_base = (
                f"第一阶段: 当前频道已结束 | 已搜 {page}/{max_pages} 页 | 累计 {self.collected_records} 条 | {self.grouped_projects} 个项目 | {message or '-'}"
            )
            self.progress_text_var.set(self.current_progress_base)
        elif stage == "search_done":
            self.current_progress_base = (
                f"第一阶段: 列表抓取完成 | 累计 {self.collected_records} 条 | {self.grouped_projects} 个项目 | 准备回补同项目文件"
            )
            self.progress_text_var.set(self.current_progress_base)
        elif stage == "backfill_start":
            self.current_progress_base = (
                f"同项目回补: 待处理 {total} 个种子项目 | 当前已有 {self.grouped_projects} 个项目"
            )
            self.progress_text_var.set(self.current_progress_base)
        elif stage == "backfill_done":
            self.current_progress_base = (
                f"同项目回补完成 | 补回 {page_items} 条记录 | 当前 {self.grouped_projects} 个项目"
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
        elif stage == "rate_limit_wait":
            reason_text = "验证码冷却" if reason == "captcha_cooldown" else "请求间隔"
            base = self.current_progress_base or "运行中"
            self.progress_text_var.set(f"{base} | 等待中: {reason_text} {self._format_eta(seconds)} | 不是卡死")
        elif stage == "request_start":
            short_url = url[:92] + "..." if len(url) > 92 else url
            base = self.current_progress_base or "运行中"
            self.progress_text_var.set(f"{base} | 请求中: {short_url or '-'}")
        elif stage == "request_done":
            short_url = url[:72] + "..." if len(url) > 72 else url
            base = self.current_progress_base or "运行中"
            self.progress_text_var.set(f"{base} | 请求完成: 用时 {self._format_eta(seconds)} | {short_url or '-'}")
        elif stage == "request_error":
            short_url = url[:72] + "..." if len(url) > 72 else url
            base = self.current_progress_base or "运行中"
            self.progress_text_var.set(f"{base} | 请求失败: {message or '-'} | {short_url or '-'}")
        elif stage == "detail_fetch_start":
            self.current_detail_title = short_title = title[:28] + "..." if len(title) > 28 else title
            self.current_detail_started_at = time.time()
            self.current_progress_base = (
                f"第二阶段: 已补 {self.progress_current}/{self.progress_total or total} 条 | 核心完整 {self.core_projects} 个 | 正在抓取 {short_title or '-'}"
            )
            self.progress_text_var.set(self.current_progress_base)
        elif stage == "detail_fetch_done":
            short_title = title[:28] + "..." if len(title) > 28 else title
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
        stale_seconds = (now - self.last_progress_at) if self.last_progress_at else 0.0
        auto_stopping = stale_seconds >= 480 and not self.captcha_waiting and not self.stop_flag
        if auto_stopping:
            self.stop_flag = True
            self.status_var.set("长时间无进展，已自动停止并保存当前结果...")
        if auto_stopping:
            text = f"{text} | 超过 {self._format_eta(stale_seconds)} 无新进度，自动停止并保存"
        elif self.current_detail_started_at > 0:
            current_elapsed = self._format_eta(now - self.current_detail_started_at)
            stale_elapsed = self._format_eta(stale_seconds) if self.last_progress_at else "0秒"
            text = f"{text} | 当前条已耗时 {current_elapsed} | 距上次进度 {stale_elapsed}"
        elif self.last_progress_at > 0:
            stale_elapsed = self._format_eta(stale_seconds)
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
        self.last_progress_site = str(payload.get("site") or "")
        self._write_run_log("__PROGRESS__" + json.dumps(payload, ensure_ascii=False) + "\n")
        self._enqueue_ui(
            self._update_progress_ui,
            str(payload.get("stage") or ""),
            int(payload.get("current") or 0),
            int(payload.get("total") or payload.get("detail_plan_total") or 0),
            str(payload.get("title") or ""),
            int(payload.get("page") or 0),
            int(payload.get("max_pages") or 0),
            int(payload.get("page_items") or 0),
            int(payload.get("collected_records") or 0),
            int(payload.get("grouped_projects") or 0),
            int(payload.get("core_projects") or 0),
            str(payload.get("message") or payload.get("error") or ""),
            str(payload.get("url") or ""),
            float(payload.get("seconds") or payload.get("elapsed_seconds") or 0.0),
            str(payload.get("reason") or ""),
        )

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
        if "sources" in payload:
            self._render_multi_source_summary(payload)
            return
        if (payload.get("meta") or {}).get("site") == "ggzy":
            self._render_ggzy_summary(payload)
            return
        lines: list[str] = []
        meta = payload.get("meta") or {}
        days_text = self.days_var.get().strip() or "-"
        lines.append(f"搜索条件: 地区={self.province_var.get().strip() or '-'} | 最近天数={days_text}")
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

    def _render_ggzy_summary(self, payload: dict) -> None:
        query = payload.get("query") or {}
        meta = payload.get("meta") or {}
        analysis = payload.get("analysis") or {}
        projects = payload.get("projects") or []
        lines: list[str] = []
        lines.append(
            f"搜索条件: 站点=ggzy.gov.cn | 地区={query.get('province') or '-'} | 关键词={query.get('keyword') or '-'} | 开始={query.get('begin') or '-'} | 结束={query.get('end') or '-'}"
        )
        lines.append(f"列表总记录数: {query.get('total_records', 0)}")
        lines.append(f"列表总页数: {query.get('page_total', 0)}")
        lines.append(f"项目总数: {meta.get('project_count', 0)}")
        lines.append(f"核心字段齐全项目数: {meta.get('core_analyzable_project_count', 0)}")
        lines.append(f"严格三文件完整项目数: {meta.get('file_complete_project_count', 0)}")
        lines.append(f"缺全体报价但已保存项目数: {meta.get('partial_saved_project_count', 0)}")
        lines.append(f"客户版JSON: {meta.get('customer_json_path') or '-'}")
        lines.append("")
        overall = analysis.get("总体统计") or {}
        quote_stats = overall.get("报价下浮率统计") or {}
        win_stats = overall.get("中标价下浮率统计") or {}
        participant_stats = overall.get("参与单位数统计") or {}
        lines.append("[整体分析]")
        lines.append(f"报价下浮率: 最低={quote_stats.get('最低')} | 最高={quote_stats.get('最高')} | 平均={quote_stats.get('平均')}")
        lines.append(f"中标下浮率: 最低={win_stats.get('最低')} | 最高={win_stats.get('最高')} | 平均={win_stats.get('平均')}")
        lines.append(f"参与单位数: 最低={participant_stats.get('最低')} | 最高={participant_stats.get('最高')} | 平均={participant_stats.get('平均')}")
        bucket_stats = analysis.get("控制价档位统计") or {}
        for bucket_name in ["小于1000万", "1000万-2000万", "2000万-5000万", "5000万-1亿", "1亿及以上", "控制价缺失"]:
            bucket = bucket_stats.get(bucket_name) or {}
            lines.append(f"{bucket_name}: 项目数 {bucket.get('项目数', 0)}")
        lines.append("")
        core_projects = [project for project in projects if (project.get("summary") or {}).get("can_analyze_core")]
        if not core_projects:
            lines.append("这个时间段内没有找到核心字段齐全的项目。")
        for index, project in enumerate(projects[:8], start=1):
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

    def _render_multi_source_summary(self, payload: dict) -> None:
        meta = payload.get("meta") or {}
        sources = payload.get("sources") or {}
        lines: list[str] = []
        lines.append(f"搜索条件: 地区={meta.get('province') or '-'} | 关键词={meta.get('keywords') or '-'} | 最近天数={meta.get('recent_days') or '-'}")
        lines.append(f"总项目数: {meta.get('project_count', 0)}")
        lines.append(f"核心字段齐全项目数: {meta.get('core_analyzable_project_count', 0)}")
        lines.append(f"三文件完整项目数: {meta.get('file_complete_project_count', 0)}")
        lines.append(f"客户版JSON: {meta.get('customer_json_path') or '-'}")
        source_summary_parts: list[str] = []
        for source_name, source_label in (("jianyu", "剑鱼"), ("ggzy", "全国公共资源")):
            source = sources.get(source_name) or {}
            source_meta = source.get("meta") or {}
            source_summary_parts.append(
                f"{source_label}: 项目{source_meta.get('project_count', 0)} / 核心{source_meta.get('core_analyzable_project_count', 0)} / 完整{source_meta.get('file_complete_project_count', 0)}"
            )
        lines.append(f"来源汇总: {' | '.join(source_summary_parts)}")
        lines.append("")
        for source_name, source_label in (("jianyu", "剑鱼"), ("ggzy", "全国公共资源")):
            source = sources.get(source_name) or {}
            source_meta = source.get("meta") or {}
            lines.append(f"[源: {source_label}]")
            lines.append(f"项目数: {source_meta.get('project_count', 0)}")
            lines.append(f"核心字段齐全: {source_meta.get('core_analyzable_project_count', 0)}")
            lines.append(f"三文件完整: {source_meta.get('file_complete_project_count', 0)}")
            lines.append(f"输出: {source_meta.get('output_json') or '-'}")
            lines.append("")
        self._set_summary("\n".join(lines).strip() or "暂无结果")

    def _derive_source_path(self, output_path: str, suffix: str, ext: str = ".json") -> str:
        base = output_path or default_output_path()
        root, current_ext = os.path.splitext(base)
        if not current_ext:
            root = base
        return f"{root}_{suffix}{ext}"

    def _derive_timestamped_path(self, output_path: str, run_id: str) -> str:
        base = output_path or default_output_path()
        root, ext = os.path.splitext(base)
        if not ext:
            root, ext = base, ".json"
        return f"{root}_{run_id}{ext}"

    @staticmethod
    def _load_json_file(path: str) -> dict | None:
        if not path or not os.path.exists(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fp:
                payload = json.load(fp)
            return payload if isinstance(payload, dict) else None
        except Exception:
            return None

    @staticmethod
    def _refresh_source_payload_counts(payload: dict | None) -> dict | None:
        if not isinstance(payload, dict):
            return payload
        projects = [item for item in payload.get("projects") or [] if isinstance(item, dict)]
        meta = dict(payload.get("meta") or {})
        meta["project_count"] = len(projects)
        meta["record_count"] = sum(len(project.get("records") or []) for project in projects)
        meta["file_complete_project_count"] = sum(1 for project in projects if (project.get("summary") or {}).get("file_complete"))
        meta["core_analyzable_project_count"] = sum(1 for project in projects if (project.get("summary") or {}).get("can_analyze_core"))
        meta["usable_project_count"] = sum(1 for project in projects if (project.get("status") or {}).get("usable"))
        payload["meta"] = meta
        payload["projects"] = projects
        return payload

    def _merge_source_payload(self, existing: dict | None, current: dict | None) -> dict | None:
        if not existing:
            return self._refresh_source_payload_counts(current)
        if not current:
            return self._refresh_source_payload_counts(existing)
        merged_projects: dict[str, dict] = {}
        for project in list(existing.get("projects") or []) + list(current.get("projects") or []):
            if not isinstance(project, dict):
                continue
            summary = project.get("summary") or {}
            key = str(project.get("project_key") or summary.get("project_title") or "").strip()
            if not key:
                continue
            merged_projects[key] = project
        merged = dict(current)
        merged["projects"] = list(merged_projects.values())
        return self._refresh_source_payload_counts(merged)

    @staticmethod
    def _write_json_file(path: str, payload: dict | None) -> None:
        if not path or payload is None:
            return
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, ensure_ascii=False, indent=2)

    def _customer_project_from_any_source(self, project: dict, source_name: str) -> dict:
        summary = project.get("summary") or {}
        quote_rows: list[dict] = []
        for row in summary.get("bid_quote_rows") or []:
            if not isinstance(row, dict):
                continue
            quote_rows.append(
                {
                    "单位名称": str(row.get("company") or "").strip() or "未识别单位",
                    "报价": row.get("quote"),
                    "下浮率": row.get("down_rate"),
                }
            )
        if not quote_rows:
            for index, quote in enumerate(summary.get("bid_quotes") or [], start=1):
                down_rate = None
                control_price = summary.get("control_price")
                try:
                    if control_price and quote:
                        down_rate = (float(control_price) - float(quote)) / float(control_price) * 100.0
                except Exception:
                    down_rate = None
                quote_rows.append({"单位名称": f"报价单位{index}", "报价": quote, "下浮率": down_rate})
        return {
            "数据来源": "剑鱼" if source_name == "jianyu" else "全国公共资源",
            "项目名称": summary.get("project_title") or project.get("project_key") or "",
            "项目编号": summary.get("bid_number") or project.get("project_code") or "",
            "控制价档位": self._control_bucket(summary.get("control_price")),
            "是否核心数据齐全": "是" if summary.get("can_analyze_core") else "否",
            "是否三类文件完整": "是" if summary.get("file_complete") else "否",
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
            "各单位报价下浮率排序": quote_rows,
            "来源链接": list(summary.get("source_urls") or []),
        }

    def _control_bucket(self, control_price) -> str:
        try:
            if control_price is None:
                return "控制价缺失"
            value = float(control_price)
        except Exception:
            return "控制价缺失"
        if value < 10_000_000:
            return "小于1000万"
        if value < 20_000_000:
            return "1000万-2000万"
        if value < 50_000_000:
            return "2000万-5000万"
        if value < 100_000_000:
            return "5000万-1亿"
        return "1亿及以上"

    def _numeric_stats(self, values: list) -> dict:
        clean_values: list[float] = []
        for value in values:
            if value is None:
                continue
            try:
                clean_values.append(float(value))
            except Exception:
                continue
        if not clean_values:
            return {"样本数": 0, "最低": None, "最高": None, "平均": None}
        return {
            "样本数": len(clean_values),
            "最低": min(clean_values),
            "最高": max(clean_values),
            "平均": sum(clean_values) / len(clean_values),
        }

    def _count_stats(self, values: list) -> dict:
        stats = self._numeric_stats(values)
        for key in ("最低", "最高"):
            if stats[key] is not None:
                stats[key] = int(stats[key])
        return stats

    def _build_combined_analysis(self, projects: list[dict]) -> dict:
        bucket_names = ["小于1000万", "1000万-2000万", "2000万-5000万", "5000万-1亿", "1亿及以上", "控制价缺失"]
        buckets = {
            name: {
                "控制价": [],
                "中标价下浮率": [],
                "报价下浮率": [],
                "参与单位数": [],
                "项目数": 0,
            }
            for name in bucket_names
        }
        all_control_prices: list = []
        all_winning_down_rates: list = []
        all_quote_down_rates: list = []
        all_participant_counts: list = []
        for project in projects:
            summary = project.get("summary") or {}
            bucket = self._control_bucket(summary.get("control_price"))
            buckets.setdefault(bucket, {"控制价": [], "中标价下浮率": [], "报价下浮率": [], "参与单位数": [], "项目数": 0})
            buckets[bucket]["项目数"] += 1
            control_price = summary.get("control_price")
            if control_price is not None:
                buckets[bucket]["控制价"].append(control_price)
                all_control_prices.append(control_price)
            winning_down_rate = summary.get("winning_down_rate")
            if winning_down_rate is not None:
                buckets[bucket]["中标价下浮率"].append(winning_down_rate)
                all_winning_down_rates.append(winning_down_rate)
            quote_down_rates = [
                row.get("down_rate")
                for row in summary.get("bid_quote_rows") or []
                if isinstance(row, dict) and row.get("down_rate") is not None
            ]
            buckets[bucket]["报价下浮率"].extend(quote_down_rates)
            all_quote_down_rates.extend(quote_down_rates)
            participant_count = summary.get("bid_quote_count")
            if participant_count is not None:
                buckets[bucket]["参与单位数"].append(participant_count)
                all_participant_counts.append(participant_count)
        return {
            "控制价档位统计": {
                name: {
                    "项目数": buckets[name]["项目数"],
                    "控制价统计": self._numeric_stats(buckets[name]["控制价"]),
                    "中标价下浮率统计": self._numeric_stats(buckets[name]["中标价下浮率"]),
                    "报价下浮率统计": self._numeric_stats(buckets[name]["报价下浮率"]),
                    "参与单位数统计": self._count_stats(buckets[name]["参与单位数"]),
                }
                for name in bucket_names
            },
            "总体统计": {
                "控制价统计": self._numeric_stats(all_control_prices),
                "报价下浮率统计": self._numeric_stats(all_quote_down_rates),
                "中标价下浮率统计": self._numeric_stats(all_winning_down_rates),
                "参与单位数统计": self._count_stats(all_participant_counts),
                "有控制价项目数": len(all_control_prices),
                "有中标下浮率项目数": len(all_winning_down_rates),
                "有报价明细项目数": sum(1 for item in projects if (item.get("summary") or {}).get("bid_quote_rows")),
            },
        }

    def _write_multi_source_output(
        self,
        output_path: str,
        *,
        keywords: str,
        province: str,
        recent_days: int,
        jianyu_payload: dict | None,
        ggzy_payload: dict | None,
        jianyu_error: str = "",
        ggzy_error: str = "",
        jianyu_output: str = "",
        ggzy_output: str = "",
    ) -> dict:
        projects: list[dict] = []
        for source_name, payload in (("jianyu", jianyu_payload), ("ggzy", ggzy_payload)):
            if not payload:
                continue
            for project in payload.get("projects") or []:
                item = dict(project)
                item["source_site"] = source_name
                projects.append(item)

        def meta_int(payload: dict | None, key: str) -> int:
            try:
                return int(((payload or {}).get("meta") or {}).get(key) or 0)
            except Exception:
                return 0

        customer_json_path = self._derive_source_path(output_path, "客户版")
        output_payload = {
            "meta": {
                "site": "multi",
                "sources": ["jianyu", "ggzy"],
                "keywords": keywords,
                "province": province,
                "recent_days": recent_days,
                "project_count": len(projects),
                "core_analyzable_project_count": meta_int(jianyu_payload, "core_analyzable_project_count") + meta_int(ggzy_payload, "core_analyzable_project_count"),
                "file_complete_project_count": meta_int(jianyu_payload, "file_complete_project_count") + meta_int(ggzy_payload, "file_complete_project_count"),
                "customer_json_path": customer_json_path,
            },
            "sources": {
                "jianyu": {
                    "ok": jianyu_payload is not None and not jianyu_error,
                    "error": jianyu_error,
                    "meta": {
                        **((jianyu_payload or {}).get("meta") or {}),
                        "output_json": jianyu_output,
                    },
                    "payload": jianyu_payload,
                },
                "ggzy": {
                    "ok": ggzy_payload is not None and not ggzy_error,
                    "error": ggzy_error,
                    "meta": {
                        **((ggzy_payload or {}).get("meta") or {}),
                        "output_json": ggzy_output,
                    },
                    "payload": ggzy_payload,
                },
            },
            "projects": projects,
        }
        with open(output_path, "w", encoding="utf-8") as fp:
            json.dump(output_payload, fp, ensure_ascii=False, indent=2)
        customer_payload = {
            "说明": "这是给客户查看的双源合并结果，只保留中文字段、状态和核心数字。",
            "抓取条件": {
                "关键词": keywords,
                "地区": province,
                "最近天数": recent_days,
            },
            "汇总": {
                "项目总数": output_payload["meta"]["project_count"],
                "核心字段齐全项目数": output_payload["meta"]["core_analyzable_project_count"],
                "三文件完整项目数": output_payload["meta"]["file_complete_project_count"],
            },
            "来源": {
                "剑鱼": {
                    "是否成功": "是" if output_payload["sources"]["jianyu"]["ok"] else "否",
                    "错误": jianyu_error,
                    "项目数": meta_int(jianyu_payload, "project_count"),
                    "核心字段齐全项目数": meta_int(jianyu_payload, "core_analyzable_project_count"),
                },
                "全国公共资源": {
                    "是否成功": "是" if output_payload["sources"]["ggzy"]["ok"] else "否",
                    "错误": ggzy_error,
                    "项目数": meta_int(ggzy_payload, "project_count"),
                    "核心字段齐全项目数": meta_int(ggzy_payload, "core_analyzable_project_count"),
                },
            },
            "分析": self._build_combined_analysis(projects),
            "项目列表": [
                self._customer_project_from_any_source(project, str(project.get("source_site") or ""))
                for project in projects
            ],
        }
        with open(customer_json_path, "w", encoding="utf-8") as fp:
            json.dump(customer_payload, fp, ensure_ascii=False, indent=2)
        split_paths = write_customer_json_splits(customer_json_path, customer_payload)
        output_payload["meta"]["customer_split_json_paths"] = split_paths
        with open(output_path, "w", encoding="utf-8") as fp:
            json.dump(output_payload, fp, ensure_ascii=False, indent=2)
        return output_payload

    @staticmethod
    def _is_expected_stop_exception(exc: Exception) -> bool:
        return str(exc) == "collection stopped"

    @staticmethod
    def _compact_jianyu_meta(payload: dict | None) -> dict:
        meta = (payload or {}).get("meta") or {}
        issue_counts = ((meta.get("audit") or {}).get("issue_counts") or {}) if isinstance(meta.get("audit"), dict) else {}
        return {
            "source_mode": meta.get("source_mode"),
            "record_count": meta.get("record_count"),
            "project_count": meta.get("project_count"),
            "core_analyzable_project_count": meta.get("core_analyzable_project_count"),
            "file_complete_project_count": meta.get("file_complete_project_count"),
            "anti_verify": meta.get("anti_verify"),
            "issue_counts": issue_counts,
            "customer_json_path": meta.get("customer_json_path"),
        }

    def run_capture(self) -> None:
        if self.collection_running:
            messagebox.showinfo("提示", "任务正在运行。")
            return
        keywords = ""
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
        database_output = self.output_var.get().strip()
        if not cookie_path or not os.path.exists(cookie_path):
            messagebox.showwarning("提示", "请选择有效的剑鱼 Cookie 文件。全国公共资源不需要 Cookie，但剑鱼需要。")
            return
        try:
            self._refresh_cookie_from_browser_if_possible()
        except Exception as exc:
            messagebox.showwarning("提示", f"无法从当前浏览器会话刷新剑鱼登录态，将继续使用现有 Cookie 文件。\n原因：{exc}")
        run_id = datetime.now().strftime("%Y%m%d-%H%M%S")
        output = self._derive_timestamped_path(database_output, run_id)
        jianyu_database = self._derive_source_path(database_output, "jianyu")
        ggzy_database = self._derive_source_path(database_output, "ggzy")
        jianyu_output = self._derive_source_path(output, "jianyu")
        ggzy_output = self._derive_source_path(output, "ggzy")
        run_mode = "双源抓取：剑鱼使用登录态，全国公共资源使用公开接口"
        self._start_run_diagnostics()
        self._write_diag_json(
            "run_context.json",
            {
                "site": "multi",
                "sources": ["jianyu", "ggzy"],
                "keywords": keywords,
                "province": province,
                "recent_days": recent_days,
                "cookie_path": cookie_path,
                "database_output": database_output,
                "output": output,
                "run_id": run_id,
                "jianyu_database": jianyu_database,
                "ggzy_database": ggzy_database,
                "jianyu_output": jianyu_output,
                "ggzy_output": ggzy_output,
                "keep_browser_session": keep_browser_session,
                "login_context_ready": self.login_context is not None,
                "mode": run_mode,
            },
        )
        self._append_log(f"[模式] {run_mode}\n")
        self._append_log(f"[输出] 本次合并={output} | 数据库={database_output} | 剑鱼库={jianyu_database} | 全国库={ggzy_database}\n")
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
            jianyu_payload = None
            ggzy_payload = None
            jianyu_error = ""
            ggzy_error = ""
            try:
                self._run_on_main_thread_sync(self.status_var.set, "运行中：正在抓取剑鱼...")
                config = collector.SearchConfig(
                    keywords=keywords,
                    province=province,
                    industry="建筑工程",
                    publish_range=collector.build_publish_range(recent_days),
                    page_size=50,
                    max_pages=20,
                    cookie_file=cookie_path,
                    output=jianyu_output,
                    report_md="",
                    fetch_details=True,
                    detail_limit=120,
                    source_mode="area_listing",
                    cache_json=jianyu_database,
                )
                try:
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
                    collector.MANUAL_CAPTCHA_HANDLER = self._manual_captcha_handler
                    try:
                        jianyu_payload = collector.run_collection(config)
                    finally:
                        collector.COOKIE_PROVIDER = previous_provider
                        collector.PROGRESS_CALLBACK = previous_callback
                        collector.STOP_REQUESTED = previous_stop
                        collector.BROWSER_REQUEST_PROVIDER = previous_browser_provider
                        collector.BROWSER_RENDERED_PROVIDER = previous_rendered_provider
                        collector.MANUAL_CAPTCHA_HANDLER = previous_manual_captcha
                    self._run_on_main_thread_sync(self._append_log, "[剑鱼完成] " + json.dumps(self._compact_jianyu_meta(jianyu_payload), ensure_ascii=False) + "\n")
                except Exception as exc:
                    jianyu_error = "" if self._is_expected_stop_exception(exc) else str(exc)
                    if os.path.exists(jianyu_output):
                        try:
                            with open(jianyu_output, "r", encoding="utf-8") as fp:
                                jianyu_payload = json.load(fp)
                        except Exception:
                            pass
                    if jianyu_error:
                        self._write_diag_json(
                            "jianyu_source_error.json",
                            {
                                "error": jianyu_error,
                                "traceback": traceback.format_exc(),
                            },
                        )
                        self._run_on_main_thread_sync(self._append_log, f"[剑鱼失败] {jianyu_error}\n{traceback.format_exc()}\n")
                    else:
                        self._run_on_main_thread_sync(self._append_log, "[剑鱼停止] 已收到停止请求，已保留当前结果。\n")
                jianyu_payload = self._merge_source_payload(self._load_json_file(jianyu_database), jianyu_payload)
                if jianyu_payload:
                    self._write_json_file(jianyu_output, jianyu_payload)
                    self._write_json_file(jianyu_database, jianyu_payload)
                    try:
                        run_customer_path = collector.write_customer_json(jianyu_output, jianyu_payload)
                        stable_customer_path = collector.write_customer_json(jianyu_database, jianyu_payload)
                        (jianyu_payload.setdefault("meta", {}))["customer_json_path"] = run_customer_path
                        self._write_json_file(jianyu_output, jianyu_payload)
                        (jianyu_payload.setdefault("meta", {}))["customer_json_path"] = stable_customer_path
                        self._write_json_file(jianyu_database, jianyu_payload)
                    except Exception:
                        pass
                if self.stop_flag:
                    self._write_multi_source_output(
                        output,
                        keywords=keywords,
                        province=province,
                        recent_days=recent_days,
                        jianyu_payload=jianyu_payload,
                        ggzy_payload=ggzy_payload,
                        jianyu_error=jianyu_error,
                        ggzy_error=ggzy_error,
                        jianyu_output=jianyu_output,
                        ggzy_output=ggzy_output,
                    )
                    self._write_multi_source_output(
                        database_output,
                        keywords=keywords,
                        province=province,
                        recent_days=recent_days,
                        jianyu_payload=jianyu_payload,
                        ggzy_payload=ggzy_payload,
                        jianyu_error=jianyu_error,
                        ggzy_error=ggzy_error,
                        jianyu_output=jianyu_database,
                        ggzy_output=ggzy_database,
                    )
                    self._enqueue_ui(self._render_summary_from_output, output)
                    self._enqueue_ui(self._finish, "已停止，已保存当前结果")
                    return

                self._run_on_main_thread_sync(self.status_var.set, "运行中：正在抓取全国公共资源...")
                previous_callback = ggzy_collector.PROGRESS_CALLBACK
                previous_stop = ggzy_collector.STOP_REQUESTED
                previous_captcha = ggzy_collector.CAPTCHA_HANDLER
                ggzy_collector.PROGRESS_CALLBACK = self._handle_progress_payload
                ggzy_collector.STOP_REQUESTED = lambda: self.stop_flag
                ggzy_collector.CAPTCHA_HANDLER = self._manual_ggzy_captcha_handler
                try:
                    ggzy_config = ggzy_collector.CrawlConfig(
                        province=province,
                        days=recent_days,
                        keyword=keywords,
                        max_pages=20,
                        output_json=ggzy_output,
                    )
                    ggzy_payload = ggzy_collector.crawl(ggzy_config)
                    with open(ggzy_output, "w", encoding="utf-8") as fp:
                        json.dump(ggzy_payload, fp, ensure_ascii=False, indent=2)
                    self._run_on_main_thread_sync(self._append_log, "[全国公共资源完成] " + json.dumps((ggzy_payload or {}).get("query") or {}, ensure_ascii=False) + "\n")
                except Exception as exc:
                    ggzy_error = "" if self._is_expected_stop_exception(exc) else str(exc)
                    if os.path.exists(ggzy_output):
                        try:
                            with open(ggzy_output, "r", encoding="utf-8") as fp:
                                ggzy_payload = json.load(fp)
                        except Exception:
                            pass
                    if ggzy_error:
                        self._write_diag_json(
                            "ggzy_source_error.json",
                            {
                                "error": ggzy_error,
                                "traceback": traceback.format_exc(),
                            },
                        )
                        self._run_on_main_thread_sync(self._append_log, f"[全国公共资源失败] {ggzy_error}\n{traceback.format_exc()}\n")
                    else:
                        self._run_on_main_thread_sync(self._append_log, "[全国公共资源停止] 已收到停止请求，已保留当前结果。\n")
                finally:
                    ggzy_collector.PROGRESS_CALLBACK = previous_callback
                    ggzy_collector.STOP_REQUESTED = previous_stop
                    ggzy_collector.CAPTCHA_HANDLER = previous_captcha
                ggzy_payload = self._merge_source_payload(self._load_json_file(ggzy_database), ggzy_payload)
                if ggzy_payload:
                    self._write_json_file(ggzy_output, ggzy_payload)
                    self._write_json_file(ggzy_database, ggzy_payload)
                    try:
                        run_customer_path = ggzy_collector.write_customer_json(ggzy_output, ggzy_payload)
                        stable_customer_path = ggzy_collector.write_customer_json(ggzy_database, ggzy_payload)
                        (ggzy_payload.setdefault("meta", {}))["customer_json_path"] = run_customer_path
                        self._write_json_file(ggzy_output, ggzy_payload)
                        (ggzy_payload.setdefault("meta", {}))["customer_json_path"] = stable_customer_path
                        self._write_json_file(ggzy_database, ggzy_payload)
                    except Exception:
                        pass

                if self.stop_flag:
                    self._write_multi_source_output(
                        output,
                        keywords=keywords,
                        province=province,
                        recent_days=recent_days,
                        jianyu_payload=jianyu_payload,
                        ggzy_payload=ggzy_payload,
                        jianyu_error=jianyu_error,
                        ggzy_error=ggzy_error,
                        jianyu_output=jianyu_output,
                        ggzy_output=ggzy_output,
                    )
                    self._write_multi_source_output(
                        database_output,
                        keywords=keywords,
                        province=province,
                        recent_days=recent_days,
                        jianyu_payload=jianyu_payload,
                        ggzy_payload=ggzy_payload,
                        jianyu_error=jianyu_error,
                        ggzy_error=ggzy_error,
                        jianyu_output=jianyu_database,
                        ggzy_output=ggzy_database,
                    )
                    self._enqueue_ui(self._render_summary_from_output, output)
                    self._enqueue_ui(self._finish, "已停止，已保存当前结果")
                    return
                self._write_multi_source_output(
                    output,
                    keywords=keywords,
                    province=province,
                    recent_days=recent_days,
                    jianyu_payload=jianyu_payload,
                    ggzy_payload=ggzy_payload,
                    jianyu_error=jianyu_error,
                    ggzy_error=ggzy_error,
                    jianyu_output=jianyu_output,
                    ggzy_output=ggzy_output,
                )
                self._write_multi_source_output(
                    database_output,
                    keywords=keywords,
                    province=province,
                    recent_days=recent_days,
                    jianyu_payload=jianyu_payload,
                    ggzy_payload=ggzy_payload,
                    jianyu_error=jianyu_error,
                    ggzy_error=ggzy_error,
                    jianyu_output=jianyu_database,
                    ggzy_output=ggzy_database,
                )
                self._enqueue_ui(self._render_summary_from_output, output)
                if jianyu_error and ggzy_error:
                    self._enqueue_ui(self._finish, "失败：两个来源都失败")
                elif jianyu_error:
                    self._enqueue_ui(self._finish, "部分完成：剑鱼失败")
                elif ggzy_error:
                    self._enqueue_ui(self._finish, "部分完成：全国公共资源失败")
                else:
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
        if self.shutting_down:
            return
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
        self.shutting_down = True
        self.stop_flag = True
        collector.STOP_REQUESTED = lambda: True
        ggzy_collector.STOP_REQUESTED = lambda: True
        self._close_login_browser()
        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    app = JianyuDesktopApp(root)
    root.protocol("WM_DELETE_WINDOW", app._on_close)
    root.mainloop()


if __name__ == "__main__":
    main()
