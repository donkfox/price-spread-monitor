from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Iterator

from .models import price_trend_label

ORDER_STATUSES = ("未到货", "已到货", "已售出", "已退货")


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except InvalidOperation:
        return Decimal("0")


@dataclass(frozen=True)
class AlertRecord:
    id: int
    created_at: str
    category: str
    title: str
    sku: str
    size: str
    pdd_price: Decimal
    recent_sale_price: Decimal
    estimated_income: Decimal
    profit: Decimal
    alert_level: str = "A"
    assessment_reasons: tuple[str, ...] = ()
    evaluation_sale_price: Decimal = Decimal("0")
    observed_sales_7d: int = 0
    active_days_7d: int = 0
    latest_age_hours: float | None = None
    max_gap_hours: float | None = None
    minimum_sale_price: Decimal = Decimal("0")
    maximum_sale_price: Decimal = Decimal("0")
    price_range_percent: Decimal = Decimal("0")
    price_trend: str = "insufficient"


@dataclass(frozen=True)
class LedgerOrder:
    id: int
    created_at: str
    updated_at: str
    title: str
    sku: str
    size: str
    cost: Decimal
    income: Decimal
    shipping: Decimal
    status: str
    source: str
    alert_id: int | None
    notes: str

    @property
    def profit(self) -> Decimal:
        return self.income - self.cost - self.shipping


class AppRepository:
    def __init__(self, database: Path):
        self.database = database
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                    status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS alerts (
                    id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, dedupe_key TEXT UNIQUE NOT NULL,
                    sku TEXT NOT NULL, size TEXT NOT NULL, pdd_price TEXT NOT NULL,
                    dewu_price TEXT NOT NULL, spread TEXT NOT NULL, payload TEXT NOT NULL
                );
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ledger_orders (
                    id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    sku TEXT NOT NULL,
                    size TEXT NOT NULL,
                    cost TEXT NOT NULL,
                    income TEXT NOT NULL,
                    shipping TEXT NOT NULL DEFAULT '0',
                    status TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '手动添加',
                    alert_id INTEGER,
                    notes TEXT NOT NULL DEFAULT ''
                )
                """
            )
            columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(ledger_orders)")
            }
            if "shipping" not in columns:
                connection.execute(
                    "ALTER TABLE ledger_orders ADD COLUMN shipping TEXT NOT NULL DEFAULT '0'"
                )
            if self._alert_id_is_unique(connection):
                self._remove_alert_id_unique_constraint(connection)
            connection.execute("DROP TABLE IF EXISTS ledger_alert_claims")

    @staticmethod
    def _alert_id_is_unique(connection: sqlite3.Connection) -> bool:
        for index in connection.execute("PRAGMA index_list(ledger_orders)").fetchall():
            if not index["unique"]:
                continue
            index_name = str(index["name"]).replace('"', '""')
            columns = connection.execute(
                f'PRAGMA index_info("{index_name}")'
            ).fetchall()
            if [column["name"] for column in columns] == ["alert_id"]:
                return True
        return False

    @staticmethod
    def _remove_alert_id_unique_constraint(connection: sqlite3.Connection) -> None:
        connection.execute("DROP TABLE IF EXISTS ledger_orders_migrated")
        connection.execute(
            """
            CREATE TABLE ledger_orders_migrated (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                title TEXT NOT NULL DEFAULT '',
                sku TEXT NOT NULL,
                size TEXT NOT NULL,
                cost TEXT NOT NULL,
                income TEXT NOT NULL,
                shipping TEXT NOT NULL DEFAULT '0',
                status TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT '手动添加',
                alert_id INTEGER,
                notes TEXT NOT NULL DEFAULT ''
            )
            """
        )
        connection.execute(
            """
            INSERT INTO ledger_orders_migrated(
                id, created_at, updated_at, title, sku, size, cost, income, shipping,
                status, source, alert_id, notes
            )
            SELECT
                id, created_at, updated_at, title, sku, size, cost, income, shipping,
                status, source, alert_id, notes
            FROM ledger_orders
            """
        )
        connection.execute("DROP TABLE ledger_orders")
        connection.execute("ALTER TABLE ledger_orders_migrated RENAME TO ledger_orders")

    @staticmethod
    def _alert_from_row(row: sqlite3.Row) -> AlertRecord:
        payload = json.loads(row["payload"] or "{}")
        latest_age = payload.get("latest_age_hours")
        max_gap = payload.get("max_gap_hours")
        return AlertRecord(
            id=int(row["id"]),
            created_at=row["created_at"],
            category=str(payload.get("category", "其他")),
            title=str(payload.get("title", "")),
            sku=row["sku"],
            size=row["size"],
            pdd_price=_decimal(row["pdd_price"]),
            recent_sale_price=_decimal(payload.get("recent_sale_price")),
            estimated_income=_decimal(row["dewu_price"]),
            profit=_decimal(row["spread"]),
            alert_level=str(payload.get("alert_level", "A")),
            assessment_reasons=tuple(payload.get("assessment_reasons") or ()),
            evaluation_sale_price=_decimal(
                payload.get("evaluation_sale_price") or payload.get("recent_sale_price")
            ),
            observed_sales_7d=int(payload.get("observed_sales_7d") or 0),
            active_days_7d=int(payload.get("active_days_7d") or 0),
            latest_age_hours=float(latest_age) if latest_age is not None else None,
            max_gap_hours=float(max_gap) if max_gap is not None else None,
            minimum_sale_price=_decimal(payload.get("minimum_sale_price")),
            maximum_sale_price=_decimal(payload.get("maximum_sale_price")),
            price_range_percent=_decimal(payload.get("price_range_percent")),
            price_trend=str(payload.get("price_trend", "insufficient")),
        )

    def alerts(self, search: str = "", limit: int = 500) -> list[AlertRecord]:
        if not self.database.exists():
            return []
        query = "SELECT id, created_at, sku, size, pdd_price, dewu_price, spread, payload FROM alerts"
        params: list[object] = []
        if search.strip():
            query += " WHERE sku LIKE ? OR size LIKE ? OR payload LIKE ?"
            term = f"%{search.strip()}%"
            params.extend((term, term, term))
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        try:
            with self._connect() as connection:
                rows = connection.execute(query, params).fetchall()
        except sqlite3.OperationalError:
            return []
        return [self._alert_from_row(row) for row in rows]

    def alerts_revision(self) -> tuple[int, int]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*), COALESCE(MAX(id), 0) FROM alerts"
            ).fetchone()
        return int(row[0]), int(row[1])

    def delete_alerts(self, alert_ids: Iterable[int]) -> int:
        unique_ids = tuple(dict.fromkeys(int(alert_id) for alert_id in alert_ids))
        if not unique_ids:
            return 0
        placeholders = ",".join("?" for _ in unique_ids)
        with self._connect() as connection:
            cursor = connection.execute(
                f"DELETE FROM alerts WHERE id IN ({placeholders})",
                unique_ids,
            )
        return max(cursor.rowcount, 0)

    def dashboard_summary(self) -> dict[str, int | Decimal]:
        today = datetime.now().astimezone().date().isoformat()
        with self._connect() as connection:
            runs = connection.execute(
                "SELECT COUNT(*) FROM runs WHERE started_at LIKE ?", (f"{today}%",)
            ).fetchone()[0]
            alert_rows = connection.execute(
                "SELECT payload FROM alerts WHERE created_at LIKE ?", (f"{today}%",)
            ).fetchall()
            alerts = sum(
                1
                for row in alert_rows
                if json.loads(row["payload"] or "{}").get("alert_level", "A") in {"A+", "A"}
            )
        month = datetime.now().astimezone().strftime("%Y-%m")
        ledger = self.month_summary(month)
        return {
            "runs_today": int(runs),
            "alerts_today": int(alerts),
            "orders_month": int(ledger["count"]),
            "realized_profit": ledger["realized_profit"],
        }

    @staticmethod
    def _order_from_row(row: sqlite3.Row) -> LedgerOrder:
        return LedgerOrder(
            id=int(row["id"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            title=row["title"],
            sku=row["sku"],
            size=row["size"],
            cost=_decimal(row["cost"]),
            income=_decimal(row["income"]),
            shipping=_decimal(row["shipping"]),
            status=row["status"],
            source=row["source"],
            alert_id=int(row["alert_id"]) if row["alert_id"] is not None else None,
            notes=row["notes"],
        )

    def orders(self, month: str | None = None) -> list[LedgerOrder]:
        query = "SELECT * FROM ledger_orders"
        params: tuple[object, ...] = ()
        if month:
            query += " WHERE created_at LIKE ?"
            params = (f"{month}%",)
        query += " ORDER BY id DESC"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._order_from_row(row) for row in rows]

    def orders_revision(self, month: str) -> tuple[int, str]:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*), COALESCE(MAX(updated_at), '')
                FROM ledger_orders
                WHERE created_at LIKE ?
                """,
                (f"{month}%",),
            ).fetchone()
        return int(row[0]), str(row[1])

    def add_order(
        self,
        *,
        sku: str,
        size: str,
        cost: Decimal,
        income: Decimal,
        shipping: Decimal = Decimal("0"),
        title: str = "",
        status: str = "未到货",
        source: str = "手动添加",
        alert_id: int | None = None,
        notes: str = "",
        created_at: str | None = None,
    ) -> int:
        if status not in ORDER_STATUSES:
            raise ValueError(f"不支持的订单状态：{status}")
        if not sku.strip() or not size.strip():
            raise ValueError("货号和规格不能为空")
        timestamp = created_at or datetime.now().astimezone().isoformat(timespec="seconds")
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO ledger_orders(
                    created_at, updated_at, title, sku, size, cost, income, shipping,
                    status, source, alert_id, notes
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    timestamp,
                    timestamp,
                    title.strip(),
                    sku.strip(),
                    size.strip(),
                    str(cost),
                    str(income),
                    str(shipping),
                    status,
                    source,
                    alert_id,
                    notes.strip(),
                ),
            )
            return int(cursor.lastrowid)

    def add_alert_to_ledger(self, alert: AlertRecord) -> int:
        return self.add_order(
            sku=alert.sku,
            size=alert.size,
            cost=alert.pdd_price,
            income=alert.estimated_income,
            title=alert.title,
            status="未到货",
            source="报警方案",
            alert_id=alert.id,
        )

    def update_order(
        self,
        order_id: int,
        *,
        sku: str,
        size: str,
        cost: Decimal,
        income: Decimal,
        shipping: Decimal,
        status: str,
        title: str = "",
        notes: str = "",
    ) -> None:
        if status not in ORDER_STATUSES:
            raise ValueError(f"不支持的订单状态：{status}")
        if not sku.strip() or not size.strip():
            raise ValueError("货号和规格不能为空")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE ledger_orders
                SET updated_at=?, title=?, sku=?, size=?, cost=?, income=?, shipping=?, status=?, notes=?
                WHERE id=?
                """,
                (
                    datetime.now().astimezone().isoformat(timespec="seconds"),
                    title.strip(),
                    sku.strip(),
                    size.strip(),
                    str(cost),
                    str(income),
                    str(shipping),
                    status,
                    notes.strip(),
                    order_id,
                ),
            )

    def update_status(self, order_id: int, status: str) -> None:
        if status not in ORDER_STATUSES:
            raise ValueError(f"不支持的订单状态：{status}")
        with self._connect() as connection:
            connection.execute(
                "UPDATE ledger_orders SET status=?, updated_at=? WHERE id=?",
                (status, datetime.now().astimezone().isoformat(timespec="seconds"), order_id),
            )

    def delete_order(self, order_id: int) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM ledger_orders WHERE id=?", (order_id,))

    def month_summary(self, month: str) -> dict[str, int | Decimal]:
        orders = self.orders(month)
        active = [order for order in orders if order.status != "已退货"]
        sold = [order for order in active if order.status == "已售出"]
        return {
            "count": len(orders),
            "active_count": len(active),
            "sold_count": len(sold),
            "total_cost": sum((order.cost for order in active), Decimal("0")),
            "total_shipping": sum((order.shipping for order in active), Decimal("0")),
            "total_revenue": sum((order.income for order in active), Decimal("0")),
            "sold_revenue": sum((order.income for order in sold), Decimal("0")),
            "expected_profit": sum((order.profit for order in active), Decimal("0")),
            "realized_profit": sum((order.profit for order in sold), Decimal("0")),
        }

    @staticmethod
    def _style_sheet(sheet, widths: tuple[int, ...]) -> None:
        from openpyxl.styles import Alignment, Font, PatternFill

        fill = PatternFill("solid", fgColor="176B5B")
        for cell in sheet[1]:
            cell.fill = fill
            cell.font = Font(color="FFFFFF", bold=True)
            cell.alignment = Alignment(horizontal="center", vertical="center")
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = sheet.dimensions
        for index, width in enumerate(widths, start=1):
            sheet.column_dimensions[sheet.cell(1, index).column_letter].width = width

    def export_alerts(self, path: Path, alerts: Iterable[AlertRecord]) -> None:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "报警方案"
        sheet.append((
            "报警时间", "等级", "品类", "商品名称", "货号", "规格", "拼多多成本",
            "最近成交", "保守试算价", "预计收入", "预计利润", "7天观察成交",
            "活跃天数", "最近成交距今(小时)", "最大成交间隔(小时)", "成交价区间",
            "价格趋势", "分级原因",
        ))
        for alert in alerts:
            sheet.append(
                (
                    alert.created_at,
                    alert.alert_level,
                    alert.category,
                    alert.title,
                    alert.sku,
                    alert.size,
                    float(alert.pdd_price),
                    float(alert.recent_sale_price),
                    float(alert.evaluation_sale_price),
                    float(alert.estimated_income),
                    float(alert.profit),
                    alert.observed_sales_7d,
                    alert.active_days_7d,
                    alert.latest_age_hours,
                    alert.max_gap_hours,
                    f"¥{alert.minimum_sale_price}-¥{alert.maximum_sale_price}",
                    price_trend_label(alert.price_trend),
                    "；".join(alert.assessment_reasons),
                )
            )
        self._style_sheet(
            sheet,
            (22, 8, 12, 40, 18, 10, 14, 14, 14, 14, 14, 14, 12, 18, 18, 20, 12, 42),
        )
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
            for index in (7, 8, 9, 10, 11):
                row[index - 1].number_format = '¥#,##0.00'
        workbook.save(path)

    def export_ledger(self, path: Path, orders: Iterable[LedgerOrder]) -> None:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment

        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "订单记账"
        sheet.append(("创建时间", "商品名称", "货号", "规格", "成本", "运费", "到手价", "利润", "状态", "来源", "备注"))
        for order in orders:
            sheet.append(
                (
                    order.created_at,
                    order.title,
                    order.sku,
                    order.size,
                    float(order.cost),
                    float(order.shipping),
                    float(order.income),
                    float(order.profit),
                    order.status,
                    order.source,
                    order.notes,
                )
            )
        self._style_sheet(sheet, (22, 42, 20, 12, 14, 12, 14, 14, 12, 14, 36))
        for row in sheet.iter_rows(min_row=2):
            for cell in row:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
            for index in (5, 6, 7, 8):
                row[index - 1].number_format = '¥#,##0.00'
        workbook.save(path)
