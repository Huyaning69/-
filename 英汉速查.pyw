from __future__ import annotations

import ctypes
import gzip
import json
import os
import queue
import re
import tempfile
import threading
import tkinter as tk
from collections import OrderedDict
from ctypes import wintypes
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_URL = "https://dict.youdao.com/jsonapi"
MUTEX_NAME = "Local\\EnglishChineseQuickLook"
ACTIVATION_FILE = Path(tempfile.gettempdir()) / "english_chinese_quick_lookup.activate"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
CACHE_LIMIT = 256
BG = "#F4F6F8"
CARD = "#FFFFFF"
TEXT = "#18212F"
MUTED = "#697586"
BORDER = "#DCE2EA"
ACCENT = "#2F6FED"
ACCENT_ACTIVE = "#245CC8"

_cache: OrderedDict[str, dict] = OrderedDict()
_cache_lock = threading.Lock()

WH_KEYBOARD_LL = 13
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_SYSKEYDOWN = 0x0104
WM_SYSKEYUP = 0x0105
VK_CONTROL = 0x11
VK_MENU = 0x12
VK_E = 0x45
VK_M = 0x4D


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("vkCode", wintypes.DWORD),
        ("scanCode", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]

def already_running() -> bool:
    if os.name != "nt":
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return ctypes.get_last_error() == 183


def signal_existing_instance() -> None:
    if os.name == "nt":
        user32 = ctypes.WinDLL("user32")
        user32.AllowSetForegroundWindow.argtypes = [ctypes.c_uint]
        user32.AllowSetForegroundWindow(0xFFFFFFFF)
    ACTIVATION_FILE.touch()


def show_window(root: tk.Tk, entry: tk.Entry) -> None:
    root.deiconify()
    root.state("normal")
    root.lift()
    root.attributes("-topmost", True)
    root.after(1800, lambda: root.attributes("-topmost", False))

    def focus() -> None:
        if os.name == "nt":
            try:
                user32 = ctypes.WinDLL("user32", use_last_error=True)
                kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
                user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
                user32.BringWindowToTop.argtypes = [ctypes.c_void_p]
                user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
                user32.SetFocus.argtypes = [ctypes.c_void_p]
                user32.SwitchToThisWindow.argtypes = [ctypes.c_void_p, ctypes.c_bool]
                user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
                user32.GetWindowThreadProcessId.restype = wintypes.DWORD
                user32.AttachThreadInput.argtypes = [
                    wintypes.DWORD,
                    wintypes.DWORD,
                    wintypes.BOOL,
                ]
                kernel32.GetCurrentThreadId.restype = wintypes.DWORD

                hwnd = ctypes.c_void_p(root.winfo_id())
                foreground = user32.GetForegroundWindow()
                foreground_thread = user32.GetWindowThreadProcessId(foreground, None)
                current_thread = kernel32.GetCurrentThreadId()
                attached = False
                if foreground_thread and foreground_thread != current_thread:
                    attached = bool(
                        user32.AttachThreadInput(
                            current_thread,
                            foreground_thread,
                            True,
                        )
                    )

                user32.ShowWindow(hwnd, 9)
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
                user32.SetFocus(hwnd)
                user32.SwitchToThisWindow(hwnd, True)

                if attached:
                    user32.AttachThreadInput(
                        current_thread,
                        foreground_thread,
                        False,
                    )
            except Exception:
                pass
        root.focus_force()
        entry.focus_set()
        entry.selection_range(0, "end")

    root.after(10, focus)
    root.after(80, focus)


def minimize_window(root: tk.Tk) -> None:
    try:
        root.attributes("-topmost", False)
    except tk.TclError:
        pass
    root.iconify()

def _text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    value = re.sub(r"<[^>]+>", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _translation_lines(data: dict, query: str) -> list[str]:
    lines: list[str] = []
    words = (data.get("ec") or {}).get("word") or []

    for word in words:
        for translation in word.get("trs") or []:
            for item in translation.get("tr") or []:
                values = (item.get("l") or {}).get("i") or []
                if isinstance(values, str):
                    values = [values]
                for value in values:
                    line = _text(value)
                    if line and line not in lines:
                        lines.append(line)

    if lines:
        return lines

    web_items = (data.get("web_trans") or {}).get("web-translation") or []
    for web_item in web_items:
        if _text(web_item.get("key")).casefold() != query.casefold():
            continue
        for item in web_item.get("trans") or []:
            line = _text(item.get("value"))
            if line and line not in lines:
                lines.append(line)
    return lines


def has_chinese(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def is_valid_query(query: str) -> bool:
    if has_chinese(query):
        return bool(re.fullmatch(r"[\u4e00-\u9fff·]+", query))
    return bool(re.fullmatch(r"[A-Za-z][A-Za-z' -]*", query))


def _chinese_dictionary_entries(data: dict) -> list[dict]:
    entries: list[dict] = []
    seen: set[str] = set()
    records = (data.get("ce") or {}).get("word") or []

    for record in records:
        for translation in record.get("trs") or []:
            for group in translation.get("tr") or []:
                item = group.get("l") or {}
                values = item.get("i") or []
                if isinstance(values, str):
                    values = [values]

                english_parts: list[str] = []
                for value in values:
                    if isinstance(value, dict):
                        part = _text(value.get("#text"))
                    else:
                        part = _text(value)
                    if part:
                        english_parts.append(part)

                english = " ".join(english_parts).strip()
                key = english.casefold()
                if not english or key in seen:
                    continue
                seen.add(key)
                entries.append(
                    {
                        "english": english,
                        "pos": _text(item.get("pos")),
                        "explanation": _text(item.get("#tran")),
                    }
                )

    if entries:
        return entries

    fallback_records = (data.get("ce_new") or {}).get("word") or []
    for record in fallback_records:
        for translation in record.get("trs") or []:
            for group in translation.get("tr") or []:
                item = group.get("l") or {}
                values = item.get("i") or []
                if isinstance(values, str):
                    values = [values]
                for value in values:
                    for part in _text(value).split(";"):
                        english = part.strip(" :：")
                        key = english.casefold()
                        if not english or key in seen:
                            continue
                        seen.add(key)
                        entries.append({"english": english, "pos": "", "explanation": ""})
    return entries


def parse_cn_response(data: dict, query: str) -> dict | None:
    entries = _chinese_dictionary_entries(data)
    if not entries:
        return None

    phone = ""
    records = (data.get("ce") or {}).get("word") or []
    if records:
        phone = _text(records[0].get("phone"))

    source = ((data.get("ce") or {}).get("source") or {}).get("name") or "有道词典"
    return {
        "word": query,
        "direction": "zh-en",
        "query_type": "word",
        "phone": phone,
        "uk_phone": "",
        "us_phone": "",
        "translations": [],
        "entries": entries,
        "source": _text(source),
    }

def parse_response(data: dict, query: str) -> dict | None:
    lines = _translation_lines(data, query)
    if not lines:
        return None

    uk_phone = ""
    us_phone = ""
    simple_words = (data.get("simple") or {}).get("word") or []
    ec_words = (data.get("ec") or {}).get("word") or []
    records = ec_words or simple_words
    if records:
        record = records[0]
        uk_phone = _text(record.get("ukphone"))
        us_phone = _text(record.get("usphone"))

    source = ((data.get("ec") or {}).get("source") or {}).get("name") or "有道词典"
    return {
        "word": query,
        "direction": "en-zh",
        "query_type": "phrase" if " " in query else "word",
        "phone": "",
        "uk_phone": uk_phone,
        "us_phone": us_phone,
        "translations": lines,
        "entries": [],
        "source": _text(source),
    }


def lookup(query: str) -> dict | None:
    key = query.casefold()
    with _cache_lock:
        cached = _cache.get(key)
        if cached is not None:
            _cache.move_to_end(key)
            return cached

    url = f"{API_URL}?{urlencode({'q': query})}"
    request = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
        },
    )
    with urlopen(request, timeout=8) as response:
        raw = response.read()
        if response.headers.get("Content-Encoding", "").lower() == "gzip":
            raw = gzip.decompress(raw)
        data = json.loads(raw.decode("utf-8"))

    result = parse_cn_response(data, query) if has_chinese(query) else parse_response(data, query)
    if result is not None:
        with _cache_lock:
            _cache[key] = result
            _cache.move_to_end(key)
            while len(_cache) > CACHE_LIMIT:
                _cache.popitem(last=False)
    return result

class DictionaryApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.query_token = 0
        self.debounce_id: str | None = None
        self.results: queue.Queue[tuple[int, dict | None, str | None]] = queue.Queue()
        self.hotkeys: queue.Queue[str] = queue.Queue()
        self._hotkey_pressed: set[int] = set()
        self._hotkey_callback = None

        self.root.title("英汉速查")
        self.root.geometry("680x470")
        self.root.minsize(560, 390)
        self.root.configure(bg=BG)
        self._center_window()
        self._build_ui()
        self.root.after(50, self._poll_results)
        self.root.after(100, self._poll_activation)
        self.root.after(50, self._poll_hotkeys)
        self._start_hotkey_listener()
        self.entry.focus_set()

    def _center_window(self) -> None:
        self.root.update_idletasks()
        x = max(0, (self.root.winfo_screenwidth() - 680) // 2)
        y = max(0, (self.root.winfo_screenheight() - 470) // 3)
        self.root.geometry(f"680x470+{x}+{y}")

    def _build_ui(self) -> None:
        body = tk.Frame(self.root, bg=BG, padx=28, pady=24)
        body.pack(fill="both", expand=True)

        tk.Label(
            body,
            text="英汉速查",
            bg=BG,
            fg=TEXT,
            font=("Microsoft YaHei UI", 22, "bold"),
        ).pack(anchor="w")
        tk.Label(
            body,
            text="支持中文、英文单词和英文短语",
            bg=BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 10),
        ).pack(anchor="w", pady=(2, 16))

        search_row = tk.Frame(body, bg=BG)
        search_row.pack(fill="x")
        self.entry = tk.Entry(
            search_row,
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            bg="white",
            fg=TEXT,
            insertbackground=TEXT,
            font=("Microsoft YaHei UI", 14),
        )
        self.entry.pack(side="left", fill="x", expand=True, ipady=10)
        self.entry.bind("<Return>", self._search_now)
        self.entry.bind("<KeyRelease>", self._schedule_search)

        self.search_button = tk.Button(
            search_row,
            text="查询",
            command=self._search_now,
            bg=ACCENT,
            activebackground=ACCENT_ACTIVE,
            fg="white",
            activeforeground="white",
            relief="flat",
            bd=0,
            padx=24,
            pady=10,
            cursor="hand2",
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        self.search_button.pack(side="left", padx=(10, 0))

        self.status = tk.Label(
            body,
            text="请输入中文、英文单词或英文短语",
            bg=BG,
            fg=MUTED,
            font=("Microsoft YaHei UI", 9),
        )
        self.status.pack(anchor="w", pady=(8, 10))

        card = tk.Frame(
            body,
            bg=CARD,
            highlightthickness=1,
            highlightbackground=BORDER,
        )
        card.pack(fill="both", expand=True)

        self.word_label = tk.Label(
            card,
            text="",
            bg=CARD,
            fg=TEXT,
            anchor="w",
            font=("Microsoft YaHei UI", 19, "bold"),
        )
        self.word_label.pack(fill="x", padx=22, pady=(18, 0))

        self.phone_label = tk.Label(
            card,
            text="",
            bg=CARD,
            fg=MUTED,
            anchor="w",
            font=("Microsoft YaHei UI", 10),
        )
        self.phone_label.pack(fill="x", padx=22, pady=(2, 12))
        tk.Frame(card, bg=BORDER, height=1).pack(fill="x", padx=22)

        result_frame = tk.Frame(card, bg=CARD)
        result_frame.pack(fill="both", expand=True, padx=22, pady=(12, 10))
        scrollbar = tk.Scrollbar(result_frame, orient="vertical")
        scrollbar.pack(side="right", fill="y")
        self.result_text = tk.Text(
            result_frame,
            bg=CARD,
            fg=TEXT,
            bd=0,
            relief="flat",
            wrap="word",
            cursor="arrow",
            font=("Microsoft YaHei UI", 12),
            padx=0,
            pady=0,
            spacing1=3,
            spacing3=7,
            yscrollcommand=scrollbar.set,
        )
        self.result_text.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.result_text.yview)
        self.result_text.tag_configure("translation", foreground=TEXT)
        self.result_text.tag_configure("english", foreground=ACCENT, font=("Microsoft YaHei UI", 12, "bold"))
        self.result_text.tag_configure("explanation", foreground=MUTED, spacing3=10)
        self.result_text.configure(state="disabled")

        self.source_label = tk.Label(
            card,
            text="",
            bg=CARD,
            fg="#8A95A5",
            anchor="e",
            font=("Microsoft YaHei UI", 8),
        )
        self.source_label.pack(fill="x", padx=22, pady=(0, 12))

    def _start_hotkey_listener(self) -> None:
        if os.name != "nt":
            return
        threading.Thread(target=self._hotkey_loop, daemon=True).start()

    def _hotkey_loop(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        hook_proc = ctypes.WINFUNCTYPE(
            ctypes.c_ssize_t,
            ctypes.c_int,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int,
            hook_proc,
            wintypes.HINSTANCE,
            wintypes.DWORD,
        ]
        user32.SetWindowsHookExW.restype = ctypes.c_void_p
        kernel32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
        kernel32.GetModuleHandleW.restype = ctypes.c_void_p
        user32.CallNextHookEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        user32.CallNextHookEx.restype = ctypes.c_ssize_t

        def callback(n_code: int, w_param: int, l_param: int) -> int:
            if n_code == 0:
                key = ctypes.cast(
                    l_param,
                    ctypes.POINTER(KBDLLHOOKSTRUCT),
                ).contents
                ctrl_down = user32.GetAsyncKeyState(VK_CONTROL) & 0x8000
                alt_down = user32.GetAsyncKeyState(VK_MENU) & 0x8000
                if key.vkCode in (VK_E, VK_M):
                    if (
                        w_param in (WM_KEYDOWN, WM_SYSKEYDOWN)
                        and ctrl_down
                        and alt_down
                    ):
                        if key.vkCode not in self._hotkey_pressed:
                            self._hotkey_pressed.add(key.vkCode)
                            action = "show" if key.vkCode == VK_E else "minimize"
                            self.hotkeys.put(action)
                        return 1
                    if (
                        w_param in (WM_KEYUP, WM_SYSKEYUP)
                        and key.vkCode in self._hotkey_pressed
                    ):
                        self._hotkey_pressed.discard(key.vkCode)
                        return 1
            return user32.CallNextHookEx(None, n_code, w_param, l_param)

        self._hotkey_callback = hook_proc(callback)
        module = kernel32.GetModuleHandleW(None)
        hook = user32.SetWindowsHookExW(
            WH_KEYBOARD_LL,
            self._hotkey_callback,
            module,
            0,
        )
        if not hook:
            return

        message = MSG()
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))

        user32.UnhookWindowsHookEx(hook)

    def _poll_hotkeys(self) -> None:
        try:
            while True:
                event = self.hotkeys.get_nowait()
                if event == "show":
                    show_window(self.root, self.entry)
                elif event == "minimize":
                    minimize_window(self.root)
        except queue.Empty:
            pass
        finally:
            self.root.after(50, self._poll_hotkeys)
    def _schedule_search(self, _event: tk.Event | None = None) -> None:
        if self.debounce_id is not None:
            self.root.after_cancel(self.debounce_id)
        self.debounce_id = self.root.after(350, self._search_now)

    def _search_now(self, _event: tk.Event | None = None) -> str:
        if self.debounce_id is not None:
            self.root.after_cancel(self.debounce_id)
            self.debounce_id = None

        query = " ".join(self.entry.get().strip().split())
        if not query:
            self._reset_result()
            self.status.configure(text="请输入中文、英文单词或英文短语")
            return "break"
        if not is_valid_query(query):
            self._reset_result()
            self.status.configure(text="请输入中文、英文单词或英文短语")
            return "break"

        self.query_token += 1
        token = self.query_token
        self.status.configure(text=f"正在查询 “{query}”…")
        threading.Thread(target=self._lookup_worker, args=(token, query), daemon=True).start()
        return "break"

    def _lookup_worker(self, token: int, query: str) -> None:
        try:
            result = lookup(query)
            self.results.put((token, result, None))
        except Exception:
            self.results.put((token, None, "查询失败，请检查网络连接后重试"))

    def _poll_results(self) -> None:
        try:
            while True:
                token, result, error = self.results.get_nowait()
                if token != self.query_token:
                    continue
                if error:
                    self._reset_result()
                    self.status.configure(text=error)
                elif result is None:
                    self._reset_result()
                    self.status.configure(text="未找到这个英文单词")
                else:
                    self._show_result(result)
        except queue.Empty:
            pass
        finally:
            self.root.after(50, self._poll_results)

    def _poll_activation(self) -> None:
        try:
            if ACTIVATION_FILE.exists():
                ACTIVATION_FILE.unlink(missing_ok=True)
                show_window(self.root, self.entry)
        except OSError:
            pass
        self.root.after(100, self._poll_activation)
    def _show_result(self, result: dict) -> None:
        self.word_label.configure(text=result["word"])
        if result.get("direction") == "zh-en":
            phone = result.get("phone") or ""
            self.phone_label.configure(text=f"拼音：{phone}" if phone else "")
        else:
            phones = []
            if result["uk_phone"]:
                phones.append(f"英 /{result['uk_phone']}/")
            if result["us_phone"] and result["us_phone"] != result["uk_phone"]:
                phones.append(f"美 /{result['us_phone']}/")
            self.phone_label.configure(text="    ".join(phones))

        self.result_text.configure(state="normal")
        self.result_text.delete("1.0", "end")
        entries = result.get("entries") or []
        if entries:
            for index, entry in enumerate(entries, 1):
                title = f"{index}. {entry['english']}"
                if entry.get("pos"):
                    title += f"  {entry['pos']}"
                self.result_text.insert("end", title + "\n", "english")
                if entry.get("explanation"):
                    self.result_text.insert("end", "简要辨析：" + entry["explanation"] + "\n\n", "explanation")
        else:
            for line in result["translations"]:
                self.result_text.insert("end", f"• {line}\n", "translation")
        self.result_text.configure(state="disabled")
        self.result_text.yview_moveto(0)
        source_name = "短语释义来源" if result.get("query_type") == "phrase" else "释义来源"
        self.source_label.configure(text=f"{source_name}：{result['source']}")
        self.status.configure(text="查询完成")

    def _reset_result(self) -> None:
        self.word_label.configure(text="")
        self.phone_label.configure(text="")
        self.source_label.configure(text="")
        self.result_text.configure(state="normal")
        self.result_text.delete("1.0", "end")
        self.result_text.configure(state="disabled")


def main() -> None:
    if already_running():
        signal_existing_instance()
        return

    try:
        ACTIVATION_FILE.unlink(missing_ok=True)
    except OSError:
        pass

    root = tk.Tk()
    DictionaryApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
























