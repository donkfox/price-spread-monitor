from __future__ import annotations

import argparse
import json
import msvcrt
import os
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from .app_data import AlertRecord, AppRepository, LedgerOrder, ORDER_STATUSES
from .models import price_trend_label
from .license_client import LicenseClient, LicenseStatus
from .purchase import PURCHASE_PLANS, PurchasePlan, purchase_url
from .storage import purge_removed_stock_watch_data


def application_root() -> Path:
    override = os.environ.get("PRICE_MONITOR_ROOT")
    if override:
        return Path(override).resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


ROOT = application_root()

COLORS = {
    "bg": "#F3F5F6",
    "surface": "#FFFFFF",
    "sidebar": "#18222A",
    "sidebar_hover": "#24323C",
    "text": "#172126",
    "muted": "#66747C",
    "line": "#DDE3E6",
    "green": "#16765F",
    "green_soft": "#E5F3EE",
    "orange": "#C66A18",
    "orange_soft": "#FFF0DF",
    "red": "#B33A3A",
    "red_soft": "#FBE8E8",
    "blue": "#2E6F9E",
    "blue_soft": "#E7F1F8",
}


def money(value: Decimal | int | str) -> str:
    try:
        return f"¥{Decimal(str(value)):,.2f}"
    except InvalidOperation:
        return "¥0.00"


def display_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value).astimezone().strftime("%m-%d %H:%M")
    except ValueError:
        return value[:16].replace("T", " ")


class TaskController:
    def __init__(self, root: Path):
        self.root = root

    def is_running(self) -> bool:
        config_path = self.root / "config.json"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
            runtime_dir = self.root / config.get("runtime_dir", "runtime")
            lock_path = runtime_dir / "monitor.lock"
            if not lock_path.is_file():
                return False
            handle = lock_path.open("r+b")
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                return True
            else:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                return False
            finally:
                handle.close()
        except (OSError, json.JSONDecodeError):
            return False

    def _run_script(self, name: str, timeout: int = 45) -> str:
        script = self.root / name
        if not script.exists():
            raise RuntimeError(f"缺少启动文件：{script}")
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            cwd=self.root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "操作失败")
        return result.stdout.strip()

    def start(self) -> str:
        return self._run_script("start-monitor.ps1")

    def stop(self) -> str:
        return self._run_script("stop-monitor.ps1")


class MonitorDesktop(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("价差监控中心")
        icon = ROOT / "assets" / "app.ico"
        if icon.is_file():
            self.iconbitmap(default=str(icon))
        self.geometry("1280x820")
        self.minsize(1080, 700)
        self.configure(bg=COLORS["bg"])
        self.option_add("*Font", ("Microsoft YaHei UI", 10))

        self.config_path = ROOT / "config.json"
        self.config_data = self._load_config()
        self.license_client = LicenseClient(self.config_data, ROOT)
        self.license_status = self.license_client.status(online=False)
        runtime_dir = ROOT / self.config_data.get("runtime_dir", "runtime")
        purge_removed_stock_watch_data(
            runtime_dir / "monitor.sqlite3",
            runtime_dir / "alerts.jsonl",
        )
        self.repository = AppRepository(runtime_dir / "monitor.sqlite3")
        self.log_path = runtime_dir / "monitor.log"
        self.controller = TaskController(ROOT)
        self.alert_cache: dict[int, AlertRecord] = {}
        self.order_cache: dict[int, LedgerOrder] = {}
        self.nav_buttons: dict[str, tk.Button] = {}
        self.pages: dict[str, tk.Frame] = {}
        self.current_page = "overview"
        self._busy = False
        self._checks_busy = False
        self._license_check_busy = False
        self._alerts_revision: tuple[int, int] | None = None
        self._alerts_search = ""
        self._alerts_level = "全部"
        self._orders_revisions: dict[str, tuple[int, str]] = {}
        self._overview_alerts_revision: tuple[int, int] | None = None
        self._last_log_text = ""

        self._configure_styles()
        self._build_shell()
        self._build_overview()
        self._build_alerts()
        self._build_ledger()
        self._build_setup()
        self._build_license()
        self.show_page("overview")
        self.refresh_all()
        self.after(250, self.refresh_license_online)

    def _load_config(self) -> dict:
        try:
            config = json.loads(self.config_path.read_text(encoding="utf-8-sig"))
            if config.pop("stock_watchlist", None) is not None:
                self.config_path.write_text(
                    json.dumps(config, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            return config
        except (OSError, json.JSONDecodeError):
            return {}

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "Data.Treeview",
            background=COLORS["surface"],
            fieldbackground=COLORS["surface"],
            foreground=COLORS["text"],
            rowheight=34,
            borderwidth=0,
            font=("Microsoft YaHei UI", 9),
        )
        style.configure(
            "Data.Treeview.Heading",
            background="#E9EEF0",
            foreground=COLORS["text"],
            relief="flat",
            font=("Microsoft YaHei UI", 9, "bold"),
            padding=(8, 10),
        )
        style.map("Data.Treeview", background=[("selected", "#CFE5DD")], foreground=[("selected", COLORS["text"])])
        style.configure("TCombobox", padding=6)

    def _build_shell(self) -> None:
        self.sidebar = tk.Frame(self, bg=COLORS["sidebar"], width=215)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        brand = tk.Frame(self.sidebar, bg=COLORS["sidebar"], height=108)
        brand.pack(fill="x")
        brand.pack_propagate(False)
        tk.Label(
            brand,
            text="价差监控中心",
            bg=COLORS["sidebar"],
            fg="white",
            font=("Microsoft YaHei UI", 17, "bold"),
        ).pack(anchor="w", padx=22, pady=(24, 2))
        tk.Label(
            brand,
            text="拼多多 -> 得物",
            bg=COLORS["sidebar"],
            fg="#AFC0C8",
            font=("Microsoft YaHei UI", 9),
        ).pack(anchor="w", padx=22)

        for key, label in (
            ("overview", "运行总览"),
            ("alerts", "报警中心"),
            ("ledger", "订单记账"),
            ("setup", "部署与设置"),
            ("license", "授权与续费"),
        ):
            button = tk.Button(
                self.sidebar,
                text=label,
                anchor="w",
                padx=22,
                pady=13,
                relief="flat",
                borderwidth=0,
                bg=COLORS["sidebar"],
                fg="#D6E0E4",
                activebackground=COLORS["sidebar_hover"],
                activeforeground="white",
                font=("Microsoft YaHei UI", 11),
                command=lambda page=key: self.show_page(page),
            )
            button.pack(fill="x", padx=10, pady=2)
            self.nav_buttons[key] = button

        self.sidebar_status = tk.Label(
            self.sidebar,
            text="正在读取状态",
            bg="#25343D",
            fg="#D6E0E4",
            padx=14,
            pady=12,
            anchor="w",
        )
        self.sidebar_status.pack(side="bottom", fill="x", padx=12, pady=14)

        self.main = tk.Frame(self, bg=COLORS["bg"])
        self.main.pack(side="left", fill="both", expand=True)
        self.header = tk.Frame(self.main, bg=COLORS["surface"], height=76, highlightthickness=1, highlightbackground=COLORS["line"])
        self.header.pack(fill="x")
        self.header.pack_propagate(False)
        self.page_title = tk.Label(
            self.header,
            text="运行总览",
            bg=COLORS["surface"],
            fg=COLORS["text"],
            font=("Microsoft YaHei UI", 18, "bold"),
        )
        self.page_title.pack(side="left", padx=28)

        self.stop_button = self._button(self.header, "停止监控", COLORS["red"], self.stop_monitor, width=10)
        self.stop_button.pack(side="right", padx=(8, 24), pady=18)
        self.start_button = self._button(self.header, "开启监控", COLORS["green"], self.start_monitor, width=10)
        self.start_button.pack(side="right", padx=0, pady=18)
        self.header_status = tk.Label(
            self.header,
            text="检测中",
            bg=COLORS["orange_soft"],
            fg=COLORS["orange"],
            padx=12,
            pady=7,
            font=("Microsoft YaHei UI", 9, "bold"),
        )
        self.header_status.pack(side="right", padx=14)

        self.content = tk.Frame(self.main, bg=COLORS["bg"])
        self.content.pack(fill="both", expand=True)

    def _button(
        self,
        parent,
        text: str,
        color: str,
        command,
        *,
        width: int = 12,
        fg: str = "white",
    ) -> tk.Button:
        return tk.Button(
            parent,
            text=text,
            command=command,
            width=width,
            bg=color,
            fg=fg,
            activebackground=color,
            activeforeground=fg,
            relief="flat",
            borderwidth=0,
            padx=10,
            pady=8,
            cursor="hand2",
            font=("Microsoft YaHei UI", 9, "bold"),
        )

    def _surface(self, parent, **pack_options) -> tk.Frame:
        frame = tk.Frame(
            parent,
            bg=COLORS["surface"],
            highlightthickness=1,
            highlightbackground=COLORS["line"],
        )
        if pack_options:
            frame.pack(**pack_options)
        return frame

    def _new_page(self, key: str) -> tk.Frame:
        frame = tk.Frame(self.content, bg=COLORS["bg"])
        self.pages[key] = frame
        return frame

    def _section_title(self, parent, title: str, subtitle: str = "") -> tk.Frame:
        header = tk.Frame(parent, bg=COLORS["surface"])
        tk.Label(
            header,
            text=title,
            bg=COLORS["surface"],
            fg=COLORS["text"],
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(side="left")
        if subtitle:
            tk.Label(
                header,
                text=subtitle,
                bg=COLORS["surface"],
                fg=COLORS["muted"],
                font=("Microsoft YaHei UI", 9),
            ).pack(side="left", padx=10)
        return header

    def _build_overview(self) -> None:
        page = self._new_page("overview")
        kpis = tk.Frame(page, bg=COLORS["bg"])
        kpis.pack(fill="x", padx=24, pady=(22, 14))
        self.kpi_values: dict[str, tk.Label] = {}
        for index, (key, title, color) in enumerate(
            (
                ("status", "监控状态", COLORS["green"]),
                ("runs", "今日扫描轮次", COLORS["blue"]),
                ("alerts", "今日报警", COLORS["orange"]),
                ("profit", "本月已实现利润", COLORS["green"]),
            )
        ):
            card = self._surface(kpis)
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 6, 6 if index < 3 else 0))
            kpis.grid_columnconfigure(index, weight=1)
            tk.Frame(card, bg=color, width=5).pack(side="left", fill="y")
            body = tk.Frame(card, bg=COLORS["surface"])
            body.pack(fill="both", expand=True, padx=16, pady=14)
            tk.Label(body, text=title, bg=COLORS["surface"], fg=COLORS["muted"], font=("Microsoft YaHei UI", 9)).pack(anchor="w")
            value = tk.Label(body, text="--", bg=COLORS["surface"], fg=COLORS["text"], font=("Microsoft YaHei UI", 18, "bold"))
            value.pack(anchor="w", pady=(5, 0))
            self.kpi_values[key] = value

        body = tk.PanedWindow(page, orient="horizontal", bg=COLORS["bg"], sashwidth=8, bd=0)
        body.pack(fill="both", expand=True, padx=24, pady=(0, 22))
        log_surface = self._surface(body)
        alert_surface = self._surface(body)
        body.add(log_surface, minsize=520, stretch="always")
        body.add(alert_surface, minsize=350, stretch="always")

        log_header = self._section_title(log_surface, "实时运行记录", "颜色区分正常、提醒和错误")
        log_header.pack(fill="x", padx=18, pady=(16, 8))
        self.log_text = tk.Text(
            log_surface,
            bg="#111A20",
            fg="#D9E2E6",
            insertbackground="white",
            relief="flat",
            borderwidth=0,
            wrap="word",
            padx=14,
            pady=12,
            font=("Consolas", 9),
            state="disabled",
        )
        self.log_text.pack(fill="both", expand=True, padx=14, pady=(0, 14))
        self.log_text.tag_configure("info", foreground="#BFD4DD")
        self.log_text.tag_configure("success", foreground="#5FD3A8")
        self.log_text.tag_configure("warning", foreground="#FFB45C")
        self.log_text.tag_configure("error", foreground="#FF7777")

        alert_header = self._section_title(alert_surface, "最近报警")
        alert_header.pack(fill="x", padx=18, pady=(16, 8))
        columns = ("level", "time", "sku", "size", "profit")
        self.overview_alert_tree = ttk.Treeview(alert_surface, columns=columns, show="headings", style="Data.Treeview", height=10)
        for column, label, width in (
            ("level", "等级", 52),
            ("time", "时间", 92),
            ("sku", "货号", 135),
            ("size", "规格", 75),
            ("profit", "利润", 90),
        ):
            self.overview_alert_tree.heading(column, text=label)
            self.overview_alert_tree.column(column, width=width, anchor="w")
        self.overview_alert_tree.tag_configure("level_a", foreground=COLORS["red"])
        self.overview_alert_tree.tag_configure("level_b", foreground=COLORS["orange"])
        self.overview_alert_tree.pack(fill="both", expand=True, padx=14, pady=(0, 10))
        self._button(alert_surface, "查看全部报警", COLORS["blue"], lambda: self.show_page("alerts"), width=14).pack(anchor="e", padx=14, pady=(0, 14))

    def _build_alerts(self) -> None:
        page = self._new_page("alerts")
        toolbar = tk.Frame(page, bg=COLORS["bg"])
        toolbar.pack(fill="x", padx=24, pady=(22, 12))
        self.alert_search = tk.StringVar()
        self.alert_level_filter = tk.StringVar(value="全部")
        level_filter = ttk.Combobox(
            toolbar,
            textvariable=self.alert_level_filter,
            values=("全部", "A+级优选", "A级强报警", "B+级关注", "B级观察"),
            state="readonly",
            width=11,
        )
        level_filter.pack(side="left", padx=(0, 8), ipady=6)
        level_filter.bind("<<ComboboxSelected>>", lambda _event: self.refresh_alerts())
        search = tk.Entry(toolbar, textvariable=self.alert_search, relief="flat", bd=0, highlightthickness=1, highlightbackground=COLORS["line"], font=("Microsoft YaHei UI", 10))
        search.pack(side="left", ipady=8, ipadx=8, fill="x", expand=True)
        search.bind("<Return>", lambda _event: self.refresh_alerts())
        self._button(toolbar, "搜索", COLORS["blue"], self.refresh_alerts, width=7).pack(side="left", padx=8)
        self._button(toolbar, "导出 Excel", COLORS["green"], self.export_alerts, width=9).pack(side="right")
        self._button(toolbar, "报表目录", "#54656F", self.open_report_directory, width=9).pack(side="right", padx=8)
        self._button(toolbar, "批量删除", COLORS["red"], self.delete_selected_alerts, width=8).pack(side="right")

        surface = self._surface(page, fill="both", expand=True, padx=24, pady=(0, 22))
        columns = (
            "level", "time", "category", "sku", "size", "sales",
            "cost", "sale", "income", "profit",
        )
        self.alert_tree = ttk.Treeview(
            surface,
            columns=columns,
            show="headings",
            style="Data.Treeview",
            selectmode="extended",
        )
        settings = (
            ("level", "等级", 55),
            ("time", "报警时间", 105),
            ("category", "品类", 75),
            ("sku", "货号", 145),
            ("size", "规格", 75),
            ("sales", "7天成交", 75),
            ("cost", "拼多多", 90),
            ("sale", "最近成交", 95),
            ("income", "预计收入", 95),
            ("profit", "预计利润", 95),
        )
        for column, label, width in settings:
            self.alert_tree.heading(column, text=label)
            self.alert_tree.column(column, width=width, anchor="w")
        self.alert_tree.tag_configure("level_a", foreground=COLORS["red"])
        self.alert_tree.tag_configure("level_b", foreground=COLORS["orange"])
        self.alert_tree.bind("<<TreeviewSelect>>", self._show_alert_detail)
        scroll = ttk.Scrollbar(surface, orient="vertical", command=self.alert_tree.yview)
        self.alert_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y", pady=(12, 0))
        self.alert_tree.pack(fill="both", expand=True, padx=(14, 0), pady=(14, 0))

        detail = tk.Frame(surface, bg="#EEF3F4", height=150)
        detail.pack(fill="x", padx=14, pady=14)
        detail.pack_propagate(False)
        self.alert_detail_title = tk.Label(detail, text="选择一条报警查看完整内容", bg="#EEF3F4", fg=COLORS["text"], font=("Microsoft YaHei UI", 11, "bold"), anchor="w")
        self.alert_detail_title.pack(fill="x", padx=14, pady=(12, 5))
        self.alert_detail_text = tk.Label(detail, text="", bg="#EEF3F4", fg=COLORS["muted"], anchor="w", justify="left", wraplength=760)
        self.alert_detail_text.pack(side="left", fill="both", expand=True, padx=14, pady=(0, 12))
        self.add_alert_button = self._button(
            detail, "加入记账", COLORS["orange"], self.add_selected_alert, width=10
        )
        self.add_alert_button.pack(side="right", padx=14, pady=28)
        self.add_alert_button.configure(state="disabled", cursor="arrow")

    def _build_ledger(self) -> None:
        page = self._new_page("ledger")
        toolbar = tk.Frame(page, bg=COLORS["bg"])
        toolbar.pack(fill="x", padx=24, pady=(18, 10))
        tk.Label(toolbar, text="账期", bg=COLORS["bg"], fg=COLORS["muted"]).pack(side="left")
        months = []
        now = datetime.now()
        year, month = now.year, now.month
        for _ in range(18):
            months.append(f"{year:04d}-{month:02d}")
            month -= 1
            if month == 0:
                month, year = 12, year - 1
        self.ledger_month = tk.StringVar(value=months[0])
        month_box = ttk.Combobox(toolbar, textvariable=self.ledger_month, values=months, width=10, state="readonly")
        month_box.pack(side="left", padx=8)
        month_box.bind("<<ComboboxSelected>>", lambda _event: self.refresh_ledger())
        self._button(toolbar, "手动添加", COLORS["green"], self.add_manual_order, width=10).pack(side="right")
        self._button(toolbar, "导出账本", COLORS["blue"], self.export_ledger, width=10).pack(side="right", padx=8)

        metrics = tk.Frame(page, bg=COLORS["bg"])
        metrics.pack(fill="x", padx=20, pady=(0, 12))
        self.ledger_summary_labels: dict[str, tk.Label] = {}
        self.ledger_summary_details: dict[str, tk.Label] = {}
        for column, (key, title, color, detail) in enumerate((
            ("cost", "商品总价", COLORS["text"], "不含退货"),
            ("shipping", "运费合计", COLORS["orange"], "不含退货"),
            ("revenue", "总流水", COLORS["blue"], "已售流水 ¥0.00"),
            ("profit", "总利润", COLORS["green"], "已实现 ¥0.00"),
            ("count", "订单数", COLORS["orange"], "已售 0 单"),
        )):
            card = tk.Frame(
                metrics,
                bg=COLORS["surface"],
                highlightthickness=1,
                highlightbackground=COLORS["line"],
                padx=14,
                pady=10,
            )
            card.grid(row=0, column=column, sticky="nsew", padx=4)
            metrics.grid_columnconfigure(column, weight=1, uniform="ledger-metric")
            tk.Label(card, text=title, bg=COLORS["surface"], fg=COLORS["muted"], anchor="w").pack(fill="x")
            label = tk.Label(
                card,
                text="--",
                bg=COLORS["surface"],
                fg=color,
                font=("Microsoft YaHei UI", 13, "bold"),
                anchor="w",
            )
            label.pack(fill="x", pady=(3, 1))
            detail_label = tk.Label(
                card,
                text=detail,
                bg=COLORS["surface"],
                fg=COLORS["muted"],
                font=("Microsoft YaHei UI", 8),
                anchor="w",
            )
            detail_label.pack(fill="x")
            self.ledger_summary_labels[key] = label
            self.ledger_summary_details[key] = detail_label

        surface = self._surface(page, fill="both", expand=True, padx=24, pady=(0, 12))
        columns = ("product", "size", "cost", "shipping", "income", "profit", "status", "time")
        self.ledger_tree = ttk.Treeview(
            surface, columns=columns, show="headings", style="Data.Treeview", height=6
        )
        for column, label, width in (
            ("product", "商品 / 货号", 230),
            ("size", "规格", 75),
            ("cost", "成本", 80),
            ("shipping", "运费", 70),
            ("income", "到手价", 85),
            ("profit", "利润", 85),
            ("status", "状态", 80),
            ("time", "下单时间", 105),
        ):
            self.ledger_tree.heading(column, text=label)
            self.ledger_tree.column(column, width=width, anchor="w")
        for status, foreground, background in (
            ("未到货", "#8A5A00", "#FFF7E8"),
            ("已到货", "#245D88", "#EDF5FB"),
            ("已售出", COLORS["green"], "#EAF6F1"),
            ("已退货", COLORS["red"], "#FCEEEE"),
        ):
            self.ledger_tree.tag_configure(status, foreground=foreground, background=background)
        self.ledger_tree.bind("<<TreeviewSelect>>", self._on_order_selected)
        self.ledger_tree.bind("<Double-1>", lambda _event: self.edit_selected_order())
        scroll = ttk.Scrollbar(surface, orient="vertical", command=self.ledger_tree.yview)
        self.ledger_tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.ledger_tree.pack(fill="both", expand=True, padx=(14, 0), pady=14)

        actions = self._surface(page, fill="x", padx=24, pady=(0, 18))
        self.order_selection_label = tk.Label(
            actions,
            text="未选择订单",
            bg=COLORS["surface"],
            fg=COLORS["muted"],
            width=18,
            anchor="w",
        )
        self.order_selection_label.pack(side="left", padx=14)
        tk.Label(actions, text="状态", bg=COLORS["surface"], fg=COLORS["muted"]).pack(side="left", padx=(0, 4))
        self.order_action_buttons: list[tk.Button] = []
        for status, color in (
            ("未到货", COLORS["orange"]),
            ("已到货", COLORS["blue"]),
            ("已售出", COLORS["green"]),
            ("已退货", COLORS["red"]),
        ):
            button = self._button(
                actions,
                status,
                color,
                lambda value=status: self.set_selected_status(value),
                width=6,
            )
            button.pack(side="left", padx=3, pady=10)
            button.configure(state="disabled", cursor="arrow")
            self.order_action_buttons.append(button)
        edit_button = self._button(actions, "编辑金额", "#54656F", self.edit_selected_order, width=8)
        edit_button.pack(side="right", padx=(4, 14), pady=10)
        delete_button = self._button(actions, "删除", COLORS["red"], self.delete_selected_order, width=6)
        delete_button.pack(side="right", padx=4, pady=10)
        for button in (edit_button, delete_button):
            button.configure(state="disabled", cursor="arrow")
            self.order_action_buttons.append(button)

    def _build_setup(self) -> None:
        page = self._new_page("setup")
        split = tk.PanedWindow(page, orient="horizontal", bg=COLORS["bg"], sashwidth=8, bd=0)
        split.pack(fill="both", expand=True, padx=24, pady=22)
        checks = self._surface(split)
        guide = self._surface(split)
        split.add(checks, minsize=400)
        split.add(guide, minsize=500)

        header = self._section_title(checks, "首次使用检查", "检测本机是否可以开始扫描")
        header.pack(fill="x", padx=18, pady=(16, 8))
        self.check_tree = ttk.Treeview(checks, columns=("item", "status", "detail"), show="headings", style="Data.Treeview", height=10)
        for column, label, width in (("item", "项目", 120), ("status", "状态", 70), ("detail", "说明", 260)):
            self.check_tree.heading(column, text=label)
            self.check_tree.column(column, width=width, anchor="w")
        self.check_tree.tag_configure("ok", foreground=COLORS["green"])
        self.check_tree.tag_configure("bad", foreground=COLORS["red"])
        self.check_tree.pack(fill="both", expand=True, padx=14, pady=(0, 10))
        buttons = tk.Frame(checks, bg=COLORS["surface"])
        buttons.pack(fill="x", padx=14, pady=(0, 14))
        self._button(buttons, "一键初始化", COLORS["green"], self.auto_initialize, width=11).grid(row=0, column=0, padx=(0, 8), pady=(0, 8))
        self._button(buttons, "重新检测", COLORS["blue"], self.refresh_checks, width=10).grid(row=0, column=1, pady=(0, 8))
        self._button(buttons, "打开 MuMu 官网", COLORS["orange"], lambda: webbrowser.open("https://mumu.163.com/"), width=13).grid(row=1, column=0, columnspan=2, sticky="w")

        guide_header = self._section_title(guide, "部署教程")
        guide_header.pack(fill="x", padx=18, pady=(16, 8))
        tutorial = (
            "1. 安装 MuMu 模拟器 12，并完成首次启动。\n\n"
            "2. 在 MuMu 中安装中国大陆版拼多多和得物。打开两个 App，手动登录账号；登录、验证码和风控验证必须本人完成。\n\n"
            "3. 在 MuMu 设置中开启 ADB 调试。默认连接地址为 127.0.0.1:7555。\n\n"
            "4. 点击“一键初始化”只会检查并保存本机路径，不会自动开启检测。\n\n"
            "5. 每次电脑开机后，手动打开本软件并点击“开启监控”。软件绝不会设置开机自动检测；需要停用时点击“停止监控”。\n\n"
            "6. 手机通知使用 ntfy。安卓端订阅软件配置的私密主题，并允许通知、自启动和后台运行。\n\n"
            "7. 报警会直接出现在“报警中心”。确认下单后点击“加入记账”，再根据实际进度修改为未到货、已到货、已售出或已退货。\n\n"
            "注意：软件只核价和报警，不会自动下单，也不会提交得物出价。"
        )
        text = tk.Text(guide, bg=COLORS["surface"], fg=COLORS["text"], relief="flat", wrap="word", padx=6, pady=4, spacing2=3, font=("Microsoft YaHei UI", 10))
        text.insert("1.0", tutorial)
        text.configure(state="disabled")
        text.pack(fill="both", expand=True, padx=16, pady=(0, 12))

        settings = tk.Frame(guide, bg="#EEF3F4")
        settings.pack(fill="x", padx=14, pady=(0, 14))
        tk.Label(settings, text="利润报警阈值", bg="#EEF3F4", fg=COLORS["muted"]).grid(row=0, column=0, padx=(12, 6), pady=12)
        self.threshold_var = tk.StringVar(value=str(self.config_data.get("estimated_profit_threshold_yuan", 30)))
        tk.Entry(settings, textvariable=self.threshold_var, width=8, relief="flat").grid(row=0, column=1, ipady=5)
        tk.Label(settings, text="扫描间隔（秒）", bg="#EEF3F4", fg=COLORS["muted"]).grid(row=0, column=2, padx=(18, 6))
        self.interval_var = tk.StringVar(value=str(self.config_data.get("scan", {}).get("interval_seconds", 60)))
        tk.Entry(settings, textvariable=self.interval_var, width=8, relief="flat").grid(row=0, column=3, ipady=5)
        ntfy_config = self.config_data.get("notification", {}).get("ntfy", {})
        ntfy_file = ROOT / ntfy_config.get("topic_file", "secrets/ntfy_topic.txt")
        try:
            ntfy_topic = ntfy_file.read_text(encoding="utf-8").strip()
        except OSError:
            ntfy_topic = ""
        tk.Label(settings, text="ntfy 私密主题", bg="#EEF3F4", fg=COLORS["muted"]).grid(row=1, column=0, padx=(12, 6), pady=10)
        self.ntfy_topic_var = tk.StringVar(value=ntfy_topic)
        tk.Entry(settings, textvariable=self.ntfy_topic_var, show="*", width=22, relief="flat").grid(row=1, column=1, columnspan=2, sticky="ew", ipady=5)
        self.ntfy_enabled_var = tk.BooleanVar(value=bool(ntfy_config.get("enabled", False)))
        ttk.Checkbutton(settings, text="启用", variable=self.ntfy_enabled_var).grid(row=1, column=3, padx=8)
        self._button(settings, "保存设置", COLORS["green"], self.save_settings, width=9).grid(
            row=2, column=0, columnspan=4, sticky="e", padx=12, pady=(0, 10)
        )

    def _build_license(self) -> None:
        page = self._new_page("license")
        content = tk.Frame(page, bg=COLORS["bg"])
        content.pack(fill="both", expand=True, padx=30, pady=26)

        status = self._surface(content, fill="x")
        tk.Label(
            status,
            text="软件授权",
            bg=COLORS["surface"],
            fg=COLORS["text"],
            font=("Microsoft YaHei UI", 15, "bold"),
        ).grid(row=0, column=0, sticky="w", padx=20, pady=(18, 4))
        self.license_state_label = tk.Label(
            status,
            text="正在读取",
            bg=COLORS["surface"],
            fg=COLORS["orange"],
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        self.license_state_label.grid(row=1, column=0, sticky="w", padx=20, pady=(6, 3))
        self.license_expiry_label = tk.Label(
            status,
            text="到期时间：--",
            bg=COLORS["surface"],
            fg=COLORS["muted"],
        )
        self.license_expiry_label.grid(row=2, column=0, sticky="w", padx=20, pady=(0, 18))
        tk.Label(
            status,
            text=f"设备编号：{self.license_client.device_id[:16].upper()}",
            bg=COLORS["surface"],
            fg=COLORS["muted"],
        ).grid(row=1, column=1, rowspan=2, sticky="e", padx=20)
        status.grid_columnconfigure(0, weight=1)

        activate = self._surface(content, fill="x", pady=(16, 0))
        tk.Label(
            activate,
            text="激活或续费",
            bg=COLORS["surface"],
            fg=COLORS["text"],
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(anchor="w", padx=20, pady=(17, 8))
        row = tk.Frame(activate, bg=COLORS["surface"])
        row.pack(fill="x", padx=20, pady=(0, 18))
        self.activation_code_var = tk.StringVar()
        entry = tk.Entry(
            row,
            textvariable=self.activation_code_var,
            relief="flat",
            highlightthickness=1,
            highlightbackground=COLORS["line"],
            font=("Consolas", 12),
        )
        entry.pack(side="left", fill="x", expand=True, ipady=8, ipadx=8)
        entry.bind("<Return>", lambda _event: self.activate_license())
        self.activate_license_button = self._button(
            row, "激活 / 续费", COLORS["green"], self.activate_license, width=12
        )
        self.activate_license_button.pack(side="left", padx=(10, 0))

        plans = self._surface(content, fill="x", pady=(16, 0))
        tk.Label(
            plans,
            text="可用套餐",
            bg=COLORS["surface"],
            fg=COLORS["text"],
            font=("Microsoft YaHei UI", 12, "bold"),
        ).pack(anchor="w", padx=20, pady=(17, 8))
        plan_row = tk.Frame(plans, bg=COLORS["surface"])
        plan_row.pack(fill="x", padx=14, pady=(0, 18))
        plan_colors = (COLORS["orange"], COLORS["green"], COLORS["blue"], COLORS["red"])
        for index, (plan, color) in enumerate(zip(PURCHASE_PLANS, plan_colors)):
            item = tk.Frame(
                plan_row,
                bg="#F7F9FA",
                highlightthickness=1,
                highlightbackground=COLORS["line"],
            )
            item.grid(row=0, column=index, sticky="nsew", padx=6)
            plan_row.grid_columnconfigure(index, weight=1, uniform="license-plan")
            tk.Frame(item, bg=color, height=4).pack(fill="x")
            tk.Label(
                item,
                text=plan.name,
                bg="#F7F9FA",
                fg=COLORS["text"],
                font=("Microsoft YaHei UI", 11, "bold"),
            ).pack(pady=(13, 2))
            tk.Label(
                item,
                text=plan.price_text,
                bg="#F7F9FA",
                fg=color,
                font=("Microsoft YaHei UI", 17, "bold"),
            ).pack()
            tk.Label(item, text=plan.short_name, bg="#F7F9FA", fg=COLORS["muted"]).pack(pady=(2, 7))
            self._button(
                item,
                "购买",
                color,
                lambda selected=plan: self.open_purchase(selected),
                width=9,
            ).pack(pady=(0, 13))

        self._render_license_status()

    def _payment_qr(self, url: str):
        import qrcode
        from PIL import ImageTk

        image = qrcode.make(url).convert("RGB")
        image = image.resize((220, 220))
        return ImageTk.PhotoImage(image)

    def open_purchase(self, plan: PurchasePlan) -> None:
        url = purchase_url(self.config_data, plan.key)
        dialog = tk.Toplevel(self)
        dialog.title(f"购买 {plan.name}")
        dialog.geometry("520x650")
        dialog.resizable(False, False)
        dialog.configure(bg=COLORS["bg"])
        dialog.transient(self)
        dialog.grab_set()
        dialog.update_idletasks()
        left = self.winfo_rootx() + max(0, (self.winfo_width() - 520) // 2)
        top = self.winfo_rooty() + max(0, (self.winfo_height() - 650) // 2)
        dialog.geometry(f"520x650+{left}+{top}")

        header = tk.Frame(dialog, bg=COLORS["sidebar"], height=104)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(
            header,
            text=plan.name,
            bg=COLORS["sidebar"],
            fg="white",
            font=("Microsoft YaHei UI", 17, "bold"),
        ).pack(anchor="w", padx=24, pady=(19, 2))
        tk.Label(
            header,
            text=plan.price_text,
            bg=COLORS["sidebar"],
            fg="#7EE0B8",
            font=("Microsoft YaHei UI", 18, "bold"),
        ).pack(anchor="w", padx=24)

        body = tk.Frame(dialog, bg=COLORS["surface"])
        body.pack(fill="both", expand=True, padx=22, pady=20)
        if url:
            qr_photo = self._payment_qr(url)
            qr_label = tk.Label(body, image=qr_photo, bg=COLORS["surface"])
            qr_label.image = qr_photo
            qr_label.pack(pady=(12, 5))
            tk.Label(
                body,
                text="微信或支付宝扫码进入链动小铺付款",
                bg=COLORS["surface"],
                fg=COLORS["muted"],
            ).pack(pady=(0, 10))
            self._button(
                body,
                "打开付款页",
                COLORS["blue"],
                lambda: webbrowser.open(url),
                width=14,
            ).pack()
        else:
            pending = tk.Frame(
                body,
                bg=COLORS["orange_soft"],
                height=260,
                highlightthickness=1,
                highlightbackground="#E8C79E",
            )
            pending.pack(fill="x", padx=20, pady=(20, 10))
            pending.pack_propagate(False)
            tk.Label(
                pending,
                text="付款链接待配置",
                bg=COLORS["orange_soft"],
                fg=COLORS["orange"],
                font=("Microsoft YaHei UI", 15, "bold"),
            ).pack(pady=(80, 8))
            tk.Label(
                pending,
                text="商家需要先在链动小铺创建该套餐商品",
                bg=COLORS["orange_soft"],
                fg=COLORS["muted"],
            ).pack()

        activation = tk.Frame(body, bg="#EEF3F4")
        activation.pack(side="bottom", fill="x", padx=10, pady=10)
        tk.Label(
            activation,
            text="付款后收到的激活码",
            bg="#EEF3F4",
            fg=COLORS["text"],
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(anchor="w", padx=14, pady=(12, 6))
        activation_row = tk.Frame(activation, bg="#EEF3F4")
        activation_row.pack(fill="x", padx=14, pady=(0, 13))
        code_var = tk.StringVar()
        code_entry = tk.Entry(
            activation_row,
            textvariable=code_var,
            relief="flat",
            font=("Consolas", 11),
        )
        code_entry.pack(side="left", fill="x", expand=True, ipady=7, ipadx=6)

        def activate_from_purchase() -> None:
            code = code_var.get().strip()
            if not code:
                messagebox.showinfo("激活授权", "请输入付款后收到的激活码", parent=dialog)
                return
            self.activation_code_var.set(code)
            dialog.destroy()
            self.activate_license()

        self._button(
            activation_row,
            "立即激活",
            COLORS["green"],
            activate_from_purchase,
            width=10,
        ).pack(side="left", padx=(8, 0))

    def show_page(self, key: str) -> None:
        titles = {
            "overview": "运行总览",
            "alerts": "报警中心",
            "ledger": "订单记账",
            "setup": "部署与设置",
            "license": "授权与续费",
        }
        for page in self.pages.values():
            page.pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        self.page_title.configure(text=titles[key])
        for name, button in self.nav_buttons.items():
            button.configure(bg=COLORS["sidebar_hover"] if name == key else COLORS["sidebar"], fg="white" if name == key else "#D6E0E4")
        self.current_page = key
        if key == "alerts":
            self.refresh_alerts()
        elif key == "ledger":
            self.refresh_ledger()
        elif key == "setup":
            self.refresh_checks()

    def _render_license_status(self) -> None:
        if not hasattr(self, "license_state_label"):
            return
        status = self.license_status
        color = COLORS["green"] if status.can_scan else COLORS["red"]
        if status.offline:
            color = COLORS["orange"]
        self.license_state_label.configure(text=status.message, fg=color)
        expiry = status.display_expiry
        self.license_expiry_label.configure(
            text=(
                "到期时间：永久（自用配置）"
                if status.state == "disabled"
                else f"到期时间：{expiry}"
            )
        )
        if hasattr(self, "start_button"):
            self.start_button.configure(
                state="normal" if status.can_scan else "disabled",
                cursor="hand2" if status.can_scan else "arrow",
            )

    def refresh_license_online(self) -> None:
        if self._license_check_busy or not self.license_client.enabled:
            self._render_license_status()
            return
        self._license_check_busy = True

        def worker() -> None:
            status = self.license_client.status(online=True, force_online=True)
            try:
                self.after(0, lambda: self._finish_license_refresh(status))
            except RuntimeError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _finish_license_refresh(self, status: LicenseStatus) -> None:
        self._license_check_busy = False
        self.license_status = status
        self._render_license_status()

    def activate_license(self) -> None:
        code = self.activation_code_var.get().strip()
        if not code:
            messagebox.showinfo("激活授权", "请输入链动小铺发给你的激活码", parent=self)
            return

        def operation() -> None:
            self.license_status = self.license_client.activate(code)

        self._run_async(operation, "激活或续费成功")

    def _licensed_start(self) -> None:
        self.license_status = self.license_client.require_scan(force_online=True)
        self.controller.start()

    def _run_async(self, operation, success_message: str) -> None:
        if self._busy:
            return
        self._busy = True
        self.header_status.configure(text="正在处理", bg=COLORS["orange_soft"], fg=COLORS["orange"])

        def worker() -> None:
            try:
                operation()
                self.after(0, lambda: self._operation_done(success_message, None))
            except Exception as error:
                self.after(0, lambda: self._operation_done("", str(error)))

        threading.Thread(target=worker, daemon=True).start()

    def _operation_done(self, message: str, error: str | None) -> None:
        self._busy = False
        self.license_status = self.license_client.status(online=False)
        self._render_license_status()
        self.refresh_status()
        if error:
            messagebox.showerror("操作失败", error, parent=self)
        elif message:
            self.sidebar_status.configure(text=message)

    def start_monitor(self) -> None:
        if not self.license_status.can_scan:
            self.show_page("license")
            messagebox.showwarning("授权不可用", self.license_status.message, parent=self)
            return
        self._run_async(self._licensed_start, "监控已开启，ntfy 将继续推送")

    def stop_monitor(self) -> None:
        self._run_async(self.controller.stop, "监控已停止")

    @staticmethod
    def _first_existing(paths: list[Path]) -> Path | None:
        return next((path for path in paths if path.is_file()), None)

    @staticmethod
    def _configured_path(value: str) -> Path:
        path = Path(value)
        return path if path.is_absolute() else ROOT / path

    def _write_discovered_config(self) -> None:
        config = self._load_config()
        current_adb = self._configured_path(config.get("adb_path", ""))
        current_mumu = self._configured_path(config.get("mumu_cli_path", ""))
        adb = self._first_existing(
            [
                current_adb,
                ROOT / "tools" / "platform-tools" / "adb.exe",
                Path(r"D:\workspace\tools\android\platform-tools\adb.exe"),
            ]
        )
        mumu = self._first_existing(
            [
                current_mumu,
                Path(r"C:\Program Files\Netease\MuMu\nx_main\mumu-cli.exe"),
                Path(r"D:\Program Files\Netease\MuMu\nx_main\mumu-cli.exe"),
            ]
        )
        if adb is None:
            raise RuntimeError("没有找到 ADB 工具。发布版应自带 tools\\platform-tools，请重新解压完整安装包。")
        config.update(
            {
                "adb_path": str(adb.resolve()),
                "adb_serial": config.get("adb_serial", "127.0.0.1:7555"),
                "mumu_cli_path": str(mumu.resolve()) if mumu else "",
                "mumu_vmindex": int(config.get("mumu_vmindex", 0)),
                "runtime_dir": config.get("runtime_dir", "runtime"),
                "report_dir": config.get(
                    "report_dir",
                    str((Path.home() / "Documents" / "价差报警记录").resolve()),
                ),
                "estimated_profit_threshold_yuan": config.get("estimated_profit_threshold_yuan", 30),
                "recent_sale_max_age_days": config.get("recent_sale_max_age_days", 7),
            }
        )
        quality = config.setdefault("alert_quality", {})
        quality.update(
            {
                "a_min_observed_sales_7d": int(quality.get("a_min_observed_sales_7d", 5)),
                "a_min_active_days_7d": int(quality.get("a_min_active_days_7d", 3)),
                "a_max_latest_sale_age_hours": float(quality.get("a_max_latest_sale_age_hours", 48)),
                "a_max_sale_gap_hours": float(quality.get("a_max_sale_gap_hours", 72)),
                "a_max_price_range_percent": float(quality.get("a_max_price_range_percent", 10)),
                "a_plus_min_score": float(quality.get("a_plus_min_score", 96)),
                "a_min_score": float(quality.get("a_min_score", 75)),
                "b_plus_min_score": float(quality.get("b_plus_min_score", 55)),
                "a_plus_min_profit_yuan": float(quality.get("a_plus_min_profit_yuan", 80)),
            }
        )
        config.setdefault("scan", {}).update(
            {
                "interval_seconds": int(config.get("scan", {}).get("interval_seconds", 60)),
                "pdd_pages_per_cycle": int(config.get("scan", {}).get("pdd_pages_per_cycle", 4)),
                "max_candidates_per_cycle": int(config.get("scan", {}).get("max_candidates_per_cycle", 4)),
                "same_product_cooldown_seconds": int(config.get("scan", {}).get("same_product_cooldown_seconds", 1800)),
                "unresolved_retry_seconds": int(config.get("scan", {}).get("unresolved_retry_seconds", 300)),
            }
        )
        config.setdefault("notification", {}).setdefault(
            "ntfy",
            {
                "enabled": False,
                "server": "https://ntfy.sh",
                "topic_file": "secrets/ntfy_topic.txt",
                "priority": 5,
                "timeout_seconds": 8,
                "retries": 2,
            },
        )
        self.config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.config_data = config

    def auto_initialize(self) -> None:
        def operation() -> None:
            self._write_discovered_config()

        self._run_async(operation, "初始化完成，未开启检测；需要时请手动点击开启监控")

    def refresh_status(self) -> None:
        running = self.controller.is_running()
        if running:
            self.header_status.configure(text="监控运行中", bg=COLORS["green_soft"], fg=COLORS["green"])
            self.sidebar_status.configure(text="后台监控：运行中", fg="#7EE0B8")
            if "status" in self.kpi_values:
                self.kpi_values["status"].configure(text="运行中", fg=COLORS["green"])
        else:
            self.header_status.configure(text="监控已停止", bg=COLORS["red_soft"], fg=COLORS["red"])
            self.sidebar_status.configure(text="后台监控：已停止", fg="#FF9999")
            if "status" in self.kpi_values:
                self.kpi_values["status"].configure(text="已停止", fg=COLORS["red"])

    def refresh_overview(self) -> None:
        summary = self.repository.dashboard_summary()
        self.kpi_values["runs"].configure(text=str(summary["runs_today"]))
        self.kpi_values["alerts"].configure(text=str(summary["alerts_today"]))
        self.kpi_values["profit"].configure(text=money(summary["realized_profit"]))
        revision = self.repository.alerts_revision()
        if revision != self._overview_alerts_revision:
            alerts = self.repository.alerts(limit=8)
            self._clear_tree(self.overview_alert_tree)
            for alert in alerts:
                self.overview_alert_tree.insert(
                    "",
                    "end",
                    values=(
                        f"{alert.alert_level}级",
                        display_time(alert.created_at),
                        alert.sku,
                        alert.size,
                        money(alert.profit),
                    ),
                    tags=("level_a" if alert.alert_level in {"A+", "A"} else "level_b",),
                )
            self._overview_alerts_revision = revision
        self._refresh_log()

    def _refresh_log(self) -> None:
        if not self.log_path.exists():
            return
        try:
            with self.log_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 131072))
                content = handle.read().decode("utf-8", errors="replace")
            lines = content.splitlines()[-90:]
        except OSError:
            return
        rendered = "\n".join(lines)
        if rendered == self._last_log_text:
            return
        self._last_log_text = rendered
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        for line in lines:
            tag = "info"
            if "ERROR" in line or "失败" in line:
                tag = "error"
            elif "WARNING" in line or "跳过" in line:
                tag = "warning"
            elif "发现预计利润机会" in line or "报警" in line:
                tag = "success"
            self.log_text.insert("end", line + "\n", tag)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    @staticmethod
    def _clear_tree(tree: ttk.Treeview) -> None:
        children = tree.get_children()
        if children:
            tree.delete(*children)

    def refresh_alerts(self, force: bool = True) -> None:
        search = self.alert_search.get() if hasattr(self, "alert_search") else ""
        level = (
            self.alert_level_filter.get()
            if hasattr(self, "alert_level_filter") else "全部"
        )
        revision = self.repository.alerts_revision()
        if (
            not force
            and revision == self._alerts_revision
            and search == self._alerts_search
            and level == self._alerts_level
        ):
            return
        alerts = self.repository.alerts(search)
        if level == "A+级优选":
            alerts = [alert for alert in alerts if alert.alert_level == "A+"]
        elif level == "A级强报警":
            alerts = [alert for alert in alerts if alert.alert_level == "A"]
        elif level == "B+级关注":
            alerts = [alert for alert in alerts if alert.alert_level == "B+"]
        elif level == "B级观察":
            alerts = [alert for alert in alerts if alert.alert_level == "B"]
        self.alert_cache = {alert.id: alert for alert in alerts}
        self._alerts_revision = revision
        self._alerts_search = search
        self._alerts_level = level
        self._clear_tree(self.alert_tree)
        for alert in alerts:
            self.alert_tree.insert(
                "",
                "end",
                iid=str(alert.id),
                values=(
                    f"{alert.alert_level}级", display_time(alert.created_at),
                    alert.category, alert.sku, alert.size, alert.observed_sales_7d,
                    money(alert.pdd_price), money(alert.recent_sale_price),
                    money(alert.estimated_income), money(alert.profit),
                ),
                tags=("level_a" if alert.alert_level in {"A+", "A"} else "level_b",),
            )
        self.alert_detail_title.configure(text="选择一条报警查看完整内容")
        self.alert_detail_text.configure(text="")
        self.add_alert_button.configure(
            text="加入记账", state="disabled", bg=COLORS["orange"], cursor="arrow"
        )

    def _selected_alert(self) -> AlertRecord | None:
        selection = self.alert_tree.selection()
        return self.alert_cache.get(int(selection[0])) if selection else None

    def _selected_alerts(self) -> list[AlertRecord]:
        return [
            self.alert_cache[int(item_id)]
            for item_id in self.alert_tree.selection()
            if int(item_id) in self.alert_cache
        ]

    def _show_alert_detail(self, _event=None) -> None:
        selected = self._selected_alerts()
        if len(selected) > 1:
            self.alert_detail_title.configure(text=f"已选择 {len(selected)} 条报警")
            self.alert_detail_text.configure(text="")
            self.add_alert_button.configure(
                text="加入记账", state="disabled", bg=COLORS["orange"], cursor="arrow"
            )
            return
        alert = selected[0] if selected else None
        if alert is None:
            return
        self.alert_detail_title.configure(
            text=f"{alert.alert_level}级  |  {alert.sku}  |  规格 {alert.size}  |  {alert.title or '未记录商品名称'}"
        )
        age_text = (
            f"{alert.latest_age_hours:g}小时"
            if alert.latest_age_hours is not None else "样本不足"
        )
        gap_text = (
            f"{alert.max_gap_hours:g}小时"
            if alert.max_gap_hours is not None else "样本不足"
        )
        reasons = "；".join(alert.assessment_reasons)
        if not reasons:
            reasons = (
                "旧版报警，未采集销量质量数据"
                if alert.observed_sales_7d == 0
                else "全部A级条件已满足"
            )
        self.alert_detail_text.configure(
            text=(
                f"拼多多成本 {money(alert.pdd_price)}    最近成交 {money(alert.recent_sale_price)}    "
                f"保守试算 {money(alert.evaluation_sale_price)}    得物预计收入 {money(alert.estimated_income)}    "
                f"预计利润 {money(alert.profit)}\n"
                f"7天同规格观察成交 {alert.observed_sales_7d} 次    活跃 {alert.active_days_7d} 天    "
                f"最近成交 {age_text}    最大间隔 {gap_text}\n"
                f"成交价 ¥{alert.minimum_sale_price}-¥{alert.maximum_sale_price}    "
                f"波动 {alert.price_range_percent}%    趋势 {price_trend_label(alert.price_trend)}    判断：{reasons}"
            )
        )
        self.add_alert_button.configure(
            text="加入记账", state="normal", bg=COLORS["orange"], cursor="hand2"
        )

    def delete_selected_alerts(self) -> None:
        selected = self._selected_alerts()
        if not selected:
            messagebox.showinfo("删除报警", "请先选择要删除的报警", parent=self)
            return
        if not messagebox.askyesno(
            "删除报警",
            f"确认删除选中的 {len(selected)} 条报警？\n已加入记账的订单会保留。",
            parent=self,
        ):
            return
        deleted = self.repository.delete_alerts(alert.id for alert in selected)
        self.refresh_alerts()
        self.sidebar_status.configure(text=f"已删除 {deleted} 条报警记录")

    def add_selected_alert(self) -> None:
        alert = self._selected_alert()
        if alert is None:
            messagebox.showinfo("加入记账", "请先选择一条报警方案", parent=self)
            return
        try:
            self.repository.add_alert_to_ledger(alert)
        except ValueError as error:
            messagebox.showwarning("加入记账", str(error), parent=self)
            return
        messagebox.showinfo("加入记账", f"{alert.sku} 规格 {alert.size} 已加入订单记账", parent=self)
        self.refresh_ledger()

    def export_alerts(self) -> None:
        alerts = list(self.alert_cache.values()) or self.repository.alerts()
        path = filedialog.asksaveasfilename(
            parent=self,
            title="下载报警表格",
            defaultextension=".xlsx",
            initialfile=f"报警记录_{datetime.now():%Y%m%d_%H%M}.xlsx",
            filetypes=(("Excel 工作簿", "*.xlsx"),),
        )
        if not path:
            return
        try:
            self.repository.export_alerts(Path(path), alerts)
            messagebox.showinfo("下载完成", f"报警表格已保存到：\n{path}", parent=self)
        except OSError as error:
            messagebox.showerror("下载失败", str(error), parent=self)

    def open_report_directory(self) -> None:
        directory = Path(self.config_data.get("report_dir", ROOT / "reports"))
        directory.mkdir(parents=True, exist_ok=True)
        os.startfile(directory)

    def refresh_ledger(self, selected_id: int | None = None, force: bool = True) -> None:
        if not hasattr(self, "ledger_tree"):
            return
        month = self.ledger_month.get()
        revision = self.repository.orders_revision(month)
        if not force and revision == self._orders_revisions.get(month):
            return
        orders = self.repository.orders(month)
        self._orders_revisions[month] = revision
        self.order_cache = {order.id: order for order in orders}
        self._clear_tree(self.ledger_tree)
        for order in orders:
            product = order.sku if not order.title else f"{order.sku}  |  {order.title}"
            self.ledger_tree.insert(
                "",
                "end",
                iid=str(order.id),
                values=(
                    product, order.size, money(order.cost), money(order.shipping),
                    money(order.income), money(order.profit), order.status,
                    display_time(order.created_at),
                ),
                tags=(order.status,),
            )
        summary = self.repository.month_summary(month)
        self.ledger_summary_labels["cost"].configure(text=money(summary["total_cost"]))
        self.ledger_summary_labels["shipping"].configure(text=money(summary["total_shipping"]))
        self.ledger_summary_labels["revenue"].configure(text=money(summary["total_revenue"]))
        self.ledger_summary_labels["profit"].configure(text=money(summary["expected_profit"]))
        self.ledger_summary_labels["count"].configure(text=str(summary["count"]))
        self.ledger_summary_details["revenue"].configure(
            text=f"已售流水 {money(summary['sold_revenue'])}"
        )
        self.ledger_summary_details["profit"].configure(
            text=f"已实现 {money(summary['realized_profit'])}"
        )
        self.ledger_summary_details["count"].configure(text=f"已售 {summary['sold_count']} 单")
        if selected_id is not None and str(selected_id) in self.ledger_tree.get_children():
            self.ledger_tree.selection_set(str(selected_id))
            self.ledger_tree.focus(str(selected_id))
            self.ledger_tree.see(str(selected_id))
            self._on_order_selected()
        else:
            self._set_order_actions(False)

    def _set_order_actions(self, enabled: bool) -> None:
        if not hasattr(self, "order_action_buttons"):
            return
        state = "normal" if enabled else "disabled"
        cursor = "hand2" if enabled else "arrow"
        for button in self.order_action_buttons:
            button.configure(state=state, cursor=cursor)
        if not enabled:
            self.order_selection_label.configure(text="未选择订单", fg=COLORS["muted"])

    def _on_order_selected(self, _event=None) -> None:
        order = self._selected_order()
        self._set_order_actions(order is not None)
        if order is not None:
            self.order_selection_label.configure(
                text=f"{order.sku}  {order.size}  |  {order.status}",
                fg=COLORS["text"],
            )

    def _selected_order(self) -> LedgerOrder | None:
        selection = self.ledger_tree.selection()
        return self.order_cache.get(int(selection[0])) if selection else None

    def add_manual_order(self) -> None:
        self._order_dialog(None)

    def edit_selected_order(self) -> None:
        order = self._selected_order()
        if order is None:
            messagebox.showinfo("编辑订单", "请先选择一条订单", parent=self)
            return
        self._order_dialog(order)

    def _order_dialog(self, order: LedgerOrder | None) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("编辑订单" if order else "添加订单")
        dialog.geometry("540x620")
        dialog.resizable(False, False)
        dialog.configure(bg=COLORS["surface"])
        dialog.transient(self)
        dialog.grab_set()

        tk.Label(dialog, text="编辑订单" if order else "添加自己的下单方案", bg=COLORS["surface"], fg=COLORS["text"], font=("Microsoft YaHei UI", 16, "bold")).pack(anchor="w", padx=28, pady=(24, 16))
        form = tk.Frame(dialog, bg=COLORS["surface"])
        form.pack(fill="both", expand=True, padx=28)
        variables = {
            "title": tk.StringVar(value=order.title if order else ""),
            "sku": tk.StringVar(value=order.sku if order else ""),
            "size": tk.StringVar(value=order.size if order else ""),
            "cost": tk.StringVar(value=str(order.cost) if order else ""),
            "shipping": tk.StringVar(value=str(order.shipping) if order else "0"),
            "income": tk.StringVar(value=str(order.income) if order else ""),
            "status": tk.StringVar(value=order.status if order else "未到货"),
            "notes": tk.StringVar(value=order.notes if order else ""),
        }
        for row, (key, label) in enumerate((("title", "商品名称（可选）"), ("sku", "货号"), ("size", "码数 / 规格"), ("cost", "商品成本"), ("shipping", "运费"), ("income", "到手价"), ("notes", "备注（可选）"))):
            tk.Label(form, text=label, bg=COLORS["surface"], fg=COLORS["muted"]).grid(row=row, column=0, sticky="w", pady=8)
            tk.Entry(form, textvariable=variables[key], relief="flat", highlightthickness=1, highlightbackground=COLORS["line"], width=32).grid(row=row, column=1, sticky="ew", padx=(16, 0), ipady=7)
        row = 7
        tk.Label(form, text="状态", bg=COLORS["surface"], fg=COLORS["muted"]).grid(row=row, column=0, sticky="w", pady=8)
        ttk.Combobox(form, textvariable=variables["status"], values=ORDER_STATUSES, state="readonly").grid(row=row, column=1, sticky="ew", padx=(16, 0))
        form.grid_columnconfigure(1, weight=1)
        profit_label = tk.Label(form, text="预计利润：¥0.00", bg=COLORS["green_soft"], fg=COLORS["green"], padx=12, pady=10, font=("Microsoft YaHei UI", 11, "bold"))
        profit_label.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(14, 0))

        def update_profit(*_args) -> None:
            try:
                value = (
                    Decimal(variables["income"].get())
                    - Decimal(variables["cost"].get())
                    - Decimal(variables["shipping"].get())
                )
                profit_label.configure(text=f"预计利润：{money(value)}")
            except InvalidOperation:
                profit_label.configure(text="预计利润：等待输入价格")

        variables["cost"].trace_add("write", update_profit)
        variables["shipping"].trace_add("write", update_profit)
        variables["income"].trace_add("write", update_profit)
        update_profit()

        def save() -> None:
            try:
                cost = Decimal(variables["cost"].get())
                shipping = Decimal(variables["shipping"].get())
                income = Decimal(variables["income"].get())
                if order:
                    self.repository.update_order(
                        order.id,
                        sku=variables["sku"].get(),
                        size=variables["size"].get(),
                        cost=cost,
                        income=income,
                        shipping=shipping,
                        status=variables["status"].get(),
                        title=variables["title"].get(),
                        notes=variables["notes"].get(),
                    )
                else:
                    self.repository.add_order(
                        sku=variables["sku"].get(),
                        size=variables["size"].get(),
                        cost=cost,
                        income=income,
                        shipping=shipping,
                        status=variables["status"].get(),
                        title=variables["title"].get(),
                        notes=variables["notes"].get(),
                    )
            except (InvalidOperation, ValueError) as error:
                messagebox.showerror("无法保存", "价格必须是数字" if isinstance(error, InvalidOperation) else str(error), parent=dialog)
                return
            dialog.destroy()
            self.refresh_ledger(selected_id=order.id if order else None)

        self._button(dialog, "保存订单", COLORS["green"], save, width=12).pack(anchor="e", padx=28, pady=22)

    def set_selected_status(self, status: str) -> None:
        order = self._selected_order()
        if order is None:
            messagebox.showinfo("更新状态", "请先选择一条订单", parent=self)
            return
        self.repository.update_status(order.id, status)
        self.refresh_ledger(selected_id=order.id)

    def delete_selected_order(self) -> None:
        order = self._selected_order()
        if order is None:
            messagebox.showinfo("删除订单", "请先选择一条订单", parent=self)
            return
        if not messagebox.askyesno("删除订单", f"确认删除 {order.sku} 规格 {order.size}？", parent=self):
            return
        self.repository.delete_order(order.id)
        self.refresh_ledger()

    def export_ledger(self) -> None:
        month = self.ledger_month.get()
        orders = self.repository.orders(month)
        path = filedialog.asksaveasfilename(
            parent=self,
            title="导出订单账本",
            defaultextension=".xlsx",
            initialfile=f"订单账本_{month}.xlsx",
            filetypes=(("Excel 工作簿", "*.xlsx"),),
        )
        if not path:
            return
        try:
            self.repository.export_ledger(Path(path), orders)
            messagebox.showinfo("导出完成", f"订单账本已保存到：\n{path}", parent=self)
        except OSError as error:
            messagebox.showerror("导出失败", str(error), parent=self)

    def refresh_checks(self) -> None:
        if not hasattr(self, "check_tree") or self._checks_busy:
            return
        self._checks_busy = True
        self._clear_tree(self.check_tree)
        self.check_tree.insert("", "end", values=("环境检测", "检测中", "正在读取 MuMu、ADB 和应用状态"))

        def worker() -> None:
            checks = self._collect_checks()
            try:
                self.after(0, lambda: self._render_checks(checks))
            except RuntimeError:
                pass

        threading.Thread(target=worker, daemon=True).start()

    def _collect_checks(self) -> list[tuple[str, bool, str]]:
        config = self._load_config()
        adb_path = Path(config.get("adb_path", ""))
        mumu_path = Path(config.get("mumu_cli_path", ""))
        ntfy_config = config.get("notification", {}).get("ntfy", {})
        ntfy_topic_file = ROOT / ntfy_config.get("topic_file", "secrets/ntfy_topic.txt")
        ntfy_ready = bool(ntfy_config.get("enabled")) and ntfy_topic_file.is_file()
        checks: list[tuple[str, bool, str]] = [
            ("软件配置", self.config_path.exists(), str(self.config_path)),
            ("ADB 工具", adb_path.is_file(), str(adb_path) if adb_path else "未配置"),
            ("MuMu 模拟器", mumu_path.is_file(), str(mumu_path) if mumu_path else "未配置"),
            ("ntfy 通知", ntfy_ready, "已启用并配置私密主题" if ntfy_ready else "可在右侧输入主题并启用"),
        ]
        device_ready = False
        packages: set[str] = set()
        if adb_path.is_file():
            try:
                state = subprocess.run(
                    [str(adb_path), "-s", config.get("adb_serial", "127.0.0.1:7555"), "get-state"],
                    capture_output=True,
                    text=True,
                    timeout=4,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                device_ready = state.returncode == 0 and state.stdout.strip() == "device"
                if device_ready:
                    result = subprocess.run(
                        [str(adb_path), "-s", config.get("adb_serial", "127.0.0.1:7555"), "shell", "pm", "list", "packages"],
                        capture_output=True,
                        text=True,
                        timeout=8,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                    packages = {line.removeprefix("package:").strip() for line in result.stdout.splitlines()}
            except (OSError, subprocess.TimeoutExpired):
                pass
        checks.extend(
            (
                ("MuMu ADB", device_ready, "设备在线" if device_ready else "启动 MuMu 并开启 ADB"),
                ("拼多多 App", "com.xunmeng.pinduoduo" in packages, "已安装" if "com.xunmeng.pinduoduo" in packages else "请在 MuMu 中安装"),
                ("得物 App", "com.shizhuang.duapp" in packages, "已安装" if "com.shizhuang.duapp" in packages else "请在 MuMu 中安装"),
            )
        )
        return checks

    def _render_checks(self, checks: list[tuple[str, bool, str]]) -> None:
        self._checks_busy = False
        if not self.winfo_exists():
            return
        self._clear_tree(self.check_tree)
        for item, ok, detail in checks:
            self.check_tree.insert("", "end", values=(item, "正常" if ok else "待处理", detail), tags=("ok" if ok else "bad",))

    def save_settings(self) -> None:
        try:
            threshold = Decimal(self.threshold_var.get())
            interval = int(self.interval_var.get())
            if threshold < 0 or interval < 10:
                raise ValueError
        except (InvalidOperation, ValueError):
            messagebox.showerror("设置无效", "利润阈值不能小于 0，扫描间隔不能少于 10 秒", parent=self)
            return
        config = self._load_config()
        config["estimated_profit_threshold_yuan"] = float(threshold)
        config.pop("stock_watchlist", None)
        config.setdefault("scan", {})["interval_seconds"] = interval
        ntfy = config.setdefault("notification", {}).setdefault("ntfy", {})
        ntfy.setdefault("server", "https://ntfy.sh")
        ntfy.setdefault("topic_file", "secrets/ntfy_topic.txt")
        ntfy.setdefault("priority", 5)
        ntfy.setdefault("timeout_seconds", 8)
        ntfy.setdefault("retries", 2)
        ntfy["enabled"] = bool(self.ntfy_enabled_var.get())
        topic = self.ntfy_topic_var.get().strip()
        topic_path = ROOT / ntfy["topic_file"]
        if ntfy["enabled"] and not topic and not topic_path.is_file():
            messagebox.showerror("设置无效", "启用 ntfy 前必须输入私密主题", parent=self)
            return
        try:
            if topic:
                topic_path.parent.mkdir(parents=True, exist_ok=True)
                topic_path.write_text(topic + "\n", encoding="utf-8")
            self.config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        except OSError as error:
            messagebox.showerror("保存失败", str(error), parent=self)
            return
        self.config_data = config
        self.license_client = LicenseClient(config, ROOT)
        self.license_status = self.license_client.status(online=False)
        self._render_license_status()
        messagebox.showinfo("设置已保存", "重新启动监控后生效", parent=self)

    def refresh_all(self) -> None:
        try:
            self.refresh_status()
            if self.current_page == "overview":
                self.refresh_overview()
            elif self.current_page == "alerts":
                self.refresh_alerts(force=False)
            elif self.current_page == "ledger":
                self.refresh_ledger(force=False)
        except (OSError, sqlite3.Error):
            pass
        self._render_license_status()
        self.after(5000, self.refresh_all)


def main() -> int:
    parser = argparse.ArgumentParser(description="价差监控中心桌面软件")
    parser.add_argument(
        "--page",
        choices=("overview", "alerts", "ledger", "setup", "license"),
        default="overview",
    )
    args = parser.parse_args()
    app = MonitorDesktop()
    app.show_page(args.page)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
