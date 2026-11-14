from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from .models import Opportunity, VerifiedProduct


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def purge_removed_stock_watch_data(database: Path, alerts_jsonl: Path) -> None:
    """Remove data left by the retired stock-watch feature."""
    if database.is_file():
        connection = sqlite3.connect(database)
        try:
            connection.executescript(
                """
                DROP TABLE IF EXISTS stock_watch_states;
                DROP TABLE IF EXISTS restock_alerts;
                """
            )
            connection.commit()
        finally:
            connection.close()

    if not alerts_jsonl.is_file():
        return
    kept_lines: list[str] = []
    changed = False
    for line in alerts_jsonl.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            kept_lines.append(line)
            continue
        is_legacy_restock = (
            isinstance(payload, dict)
            and "expected_profit" in payload
            and "reference" not in payload
            and "alert_level" not in payload
        )
        if is_legacy_restock:
            changed = True
        else:
            kept_lines.append(line)
    if changed:
        content = "\n".join(kept_lines)
        alerts_jsonl.write_text(content + ("\n" if content else ""), encoding="utf-8")


class Storage:
    def __init__(self, database: Path, alerts_jsonl: Path):
        database.parent.mkdir(parents=True, exist_ok=True)
        self.database = database
        self.alerts_jsonl = alerts_jsonl
        purge_removed_stock_watch_data(database, alerts_jsonl)
        self.connection = sqlite3.connect(database)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, observed_at TEXT NOT NULL,
                sku TEXT NOT NULL, pdd_title TEXT NOT NULL, pdd_price TEXT NOT NULL,
                pdd_sizes TEXT NOT NULL, dewu_title TEXT NOT NULL DEFAULT '',
                dewu_price TEXT, dewu_sizes TEXT NOT NULL DEFAULT '{}',
                best_spread TEXT, status TEXT NOT NULL, reason TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY, created_at TEXT NOT NULL, dedupe_key TEXT UNIQUE NOT NULL,
                sku TEXT NOT NULL, size TEXT NOT NULL, pdd_price TEXT NOT NULL,
                dewu_price TEXT NOT NULL, spread TEXT NOT NULL, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS candidate_checks (
                sku TEXT PRIMARY KEY, list_price TEXT NOT NULL, checked_at TEXT NOT NULL
            );
            """
        )
        self.connection.commit()

    def candidate_due(self, sku: str, list_price: Decimal, cooldown_seconds: int) -> bool:
        row = self.connection.execute(
            "SELECT list_price, checked_at FROM candidate_checks WHERE sku=?", (sku,)
        ).fetchone()
        current_time = datetime.now(timezone.utc).astimezone()
        due = row is None or row[0] != str(list_price)
        if row is not None and not due:
            checked_at = datetime.fromisoformat(row[1])
            due = (current_time - checked_at).total_seconds() >= cooldown_seconds
        if not due:
            return False
        self.connection.execute(
            """INSERT INTO candidate_checks(sku, list_price, checked_at) VALUES (?, ?, ?)
               ON CONFLICT(sku) DO UPDATE SET list_price=excluded.list_price, checked_at=excluded.checked_at""",
            (sku, str(list_price), current_time.isoformat(timespec="seconds")),
        )
        self.connection.commit()
        return True

    def retry_candidate_after(self, sku: str, cooldown_seconds: int, retry_seconds: int) -> None:
        checked_at = datetime.now(timezone.utc).astimezone() - timedelta(
            seconds=max(0, cooldown_seconds - retry_seconds)
        )
        self.connection.execute(
            "UPDATE candidate_checks SET checked_at=? WHERE sku=?",
            (checked_at.isoformat(timespec="seconds"), sku),
        )
        self.connection.commit()

    def start_run(self) -> int:
        cursor = self.connection.execute(
            "INSERT INTO runs(started_at, status) VALUES (?, 'running')", (now_iso(),)
        )
        self.connection.commit()
        return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str, detail: str = "") -> None:
        self.connection.execute(
            "UPDATE runs SET finished_at=?, status=?, detail=? WHERE id=?",
            (now_iso(), status, detail, run_id),
        )
        self.connection.commit()

    def observation(
        self,
        run_id: int,
        pdd: VerifiedProduct,
        dewu: VerifiedProduct | None,
        status: str,
        reason: str,
        best_spread: Decimal | None = None,
    ) -> None:
        self.connection.execute(
            """INSERT INTO observations(
                run_id, observed_at, sku, pdd_title, pdd_price, pdd_sizes,
                dewu_title, dewu_price, dewu_sizes, best_spread, status, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                now_iso(),
                pdd.sku,
                pdd.title,
                str(pdd.base_price),
                json.dumps({k: str(v) for k, v in pdd.size_prices.items()}, ensure_ascii=False),
                dewu.title if dewu else "",
                str(dewu.base_price) if dewu else None,
                json.dumps({k: str(v) for k, v in (dewu.size_prices if dewu else {}).items()}, ensure_ascii=False),
                str(best_spread) if best_spread is not None else None,
                status,
                reason,
            ),
        )
        self.connection.commit()

    def record_alert(
        self,
        opportunity: Opportunity,
        category: str,
        alert_level: str = "A",
        assessment_reasons: tuple[str, ...] = (),
    ) -> dict | None:
        profile = opportunity.sales_profile
        payload = {
            "created_at": now_iso(),
            "sku": opportunity.sku,
            "title": opportunity.title,
            "size": opportunity.size,
            "pdd_price": str(opportunity.pdd_price),
            "recent_sale_price": str(opportunity.recent_sale_price),
            "evaluation_sale_price": str(
                opportunity.evaluation_sale_price or opportunity.recent_sale_price
            ),
            "estimated_income": str(opportunity.estimated_income),
            "estimated_profit": str(opportunity.spread),
            "dewu_price": str(opportunity.estimated_income),
            "spread": str(opportunity.spread),
            "category": category,
            "alert_level": alert_level,
            "assessment_reasons": list(assessment_reasons),
            "observed_sales_7d": profile.observed_sales_7d if profile else 0,
            "active_days_7d": profile.active_days_7d if profile else 0,
            "latest_age_hours": profile.latest_age_hours if profile else None,
            "max_gap_hours": profile.max_gap_hours if profile else None,
            "minimum_sale_price": str(profile.minimum_price) if profile else "",
            "maximum_sale_price": str(profile.maximum_price) if profile else "",
            "median_sale_price": str(profile.median_price) if profile else "",
            "price_range_percent": str(profile.price_range_percent) if profile else "",
            "price_trend": profile.price_trend if profile else "insufficient",
            "buy": "拼多多",
            "reference": "得物",
            "purchase_method": f"在拼多多搜索货号 {opportunity.sku}，选择规格 {opportunity.size}，付款前复核价格",
            "reference_method": (
                f"在得物“我-出售-我要出售”搜索货号 {opportunity.sku}，"
                f"选择规格 {opportunity.size}，按最近成交价 ¥{opportunity.recent_sale_price} 试算，"
                f"预计收入为 ¥{opportunity.estimated_income}；未提交出价"
            ),
        }
        key = (
            f"{alert_level}|{opportunity.sku}|{opportunity.size}|{opportunity.pdd_price}|"
            f"{opportunity.recent_sale_price}|{opportunity.estimated_income}"
        )
        try:
            self.connection.execute(
                "INSERT INTO alerts(created_at,dedupe_key,sku,size,pdd_price,dewu_price,spread,payload) VALUES(?,?,?,?,?,?,?,?)",
                (
                    payload["created_at"], key, opportunity.sku, opportunity.size,
                    str(opportunity.pdd_price), str(opportunity.dewu_price),
                    str(opportunity.spread), json.dumps(payload, ensure_ascii=False),
                ),
            )
            self.connection.commit()
        except sqlite3.IntegrityError:
            return None
        self.alerts_jsonl.parent.mkdir(parents=True, exist_ok=True)
        with self.alerts_jsonl.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")
        return payload
