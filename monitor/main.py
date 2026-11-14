from __future__ import annotations

import argparse
import json
import logging
import msvcrt
import os
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

from .adb import Adb, AdbError, AuthenticationRequired, RiskDetected
from .models import Opportunity, VerifiedProduct
from .notify import (
    NtfyNotifier,
    PushDeerNotifier,
    WeComBotNotifier,
    windows_toast,
)
from .platforms import Dewu, Pinduoduo
from .report import SessionReport, classify_product
from .storage import Storage
from .matching import normalize_size, spec_sort_key
from .quality import assess_opportunity
from .license_client import LicenseClient, LicenseClientError


CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_PRICE_MONITOR_ROOT = os.environ.get("PRICE_MONITOR_ROOT")
if _PRICE_MONITOR_ROOT:
    ROOT = Path(_PRICE_MONITOR_ROOT).resolve()
elif getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parents[1]


def load_config(path: Path) -> dict:
    with path.open("r", encoding="utf-8-sig") as handle:
        config = json.load(handle)
    if config.pop("stock_watchlist", None) is not None:
        path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return config


def configure_logging(runtime_dir: Path) -> logging.Logger:
    runtime_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("monitor")
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    file_handler = logging.FileHandler(runtime_dir / "monitor.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.handlers[:] = [stream, file_handler]
    return logger


def acquire_single_instance(runtime_dir: Path):
    lock_path = runtime_dir / "monitor.lock"
    handle = lock_path.open("a+b")
    if lock_path.stat().st_size == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def matching_opportunities(
    pdd: VerifiedProduct,
    dewu: VerifiedProduct,
    require_numbered_size: bool = False,
) -> list[Opportunity]:
    opportunities = [
        Opportunity(
            sku=pdd.sku,
            title=pdd.title,
            size=size,
            pdd_price=pdd.size_prices[size],
            dewu_price=dewu.estimated_incomes[size],
            recent_sale_price=dewu.recent_sale_prices[size],
            evaluation_sale_price=dewu.evaluation_sale_prices.get(
                size, dewu.recent_sale_prices[size]
            ),
            sales_profile=dewu.sales_profiles.get(size),
        )
        for size in pdd.size_prices.keys() & dewu.estimated_incomes.keys() & dewu.recent_sale_prices.keys()
        if not require_numbered_size or normalize_size(size) is not None
    ]
    return sorted(
        opportunities,
        key=lambda item: spec_sort_key(item.size),
    )


def best_opportunity(
    pdd: VerifiedProduct,
    dewu: VerifiedProduct,
    require_numbered_size: bool = False,
) -> Opportunity | None:
    return max(
        matching_opportunities(pdd, dewu, require_numbered_size),
        key=lambda item: item.spread,
        default=None,
    )


def ensure_device(config: dict, adb: Adb, logger: logging.Logger) -> None:
    try:
        adb.connect()
        return
    except Exception:
        cli = config.get("mumu_cli_path")
        if not cli:
            raise
    logger.info("MuMu 未就绪，正在自动启动模拟器")
    subprocess.run(
        [cli, "control", "--vmindex", str(config.get("mumu_vmindex", 0)), "launch"],
        capture_output=True,
        timeout=30,
        check=False,
        creationflags=CREATE_NO_WINDOW,
    )
    last_error: Exception | None = None
    for _ in range(30):
        time.sleep(3)
        try:
            adb.connect()
            logger.info("MuMu 已启动并连接 ADB")
            return
        except Exception as error:
            last_error = error
    raise RuntimeError("MuMu 启动后 90 秒内 ADB 仍未就绪") from last_error


def run_cycle(
    config: dict,
    adb: Adb,
    storage: Storage,
    report: SessionReport,
    logger: logging.Logger,
    notifiers: list[PushDeerNotifier | NtfyNotifier | WeComBotNotifier] | None = None,
) -> None:
    run_id = storage.start_run()
    try:
        ensure_device(config, adb, logger)
        pdd = Pinduoduo(adb, logger)
        dewu = Dewu(adb, logger)
        threshold = Decimal(str(config.get("estimated_profit_threshold_yuan", 30)))
        quality_settings = config.get("alert_quality", {})
        def evaluate_product(product: VerifiedProduct) -> None:
            if not product.size_prices:
                storage.observation(
                    run_id,
                    product,
                    None,
                    "rejected",
                    "拼多多当前没有可购买规格价格",
                )
                return
            logger.info("%s：拼多多核验完成，立即切换得物核验", product.sku)
            try:
                reference = dewu.verify(
                    product.sku,
                    product.title,
                    target_sizes=set(product.size_prices),
                    recent_sale_max_age_days=int(config.get("recent_sale_max_age_days", 7)),
                )
            except (AuthenticationRequired, RiskDetected):
                raise
            except (AdbError, RuntimeError) as error:
                logger.warning(
                    "%s：本次得物核验遇到临时页面故障，跳过当前商品并稍后重试：%s",
                    product.sku,
                    error,
                )
                storage.observation(run_id, product, None, "deferred", str(error))
                storage.retry_candidate_after(
                    product.source_sku or product.sku,
                    int(config["scan"]["same_product_cooldown_seconds"]),
                    int(config["scan"]["unresolved_retry_seconds"]),
                )
                return
            if reference is None:
                logger.info("%s：得物没有找到货号及版本一致的商品", product.sku)
                storage.observation(run_id, product, None, "rejected", "得物货号或版本未匹配")
                storage.retry_candidate_after(
                    product.source_sku or product.sku,
                    int(config["scan"]["same_product_cooldown_seconds"]),
                    int(config["scan"]["unresolved_retry_seconds"]),
                )
                return
            category = classify_product(product.title)
            opportunities = matching_opportunities(
                product,
                reference,
                require_numbered_size=category == "鞋子",
            )
            if not opportunities:
                reason = (
                    "鞋类货号已匹配，但没有形成同鞋码的有效最近成交及预计收入"
                    if category == "鞋子"
                    else "没有取得同规格最近成交及预计收入"
                )
                logger.info("%s：%s", product.sku, reason)
                storage.observation(run_id, product, reference, "rejected", reason)
                return
            qualifying = [
                opportunity for opportunity in opportunities
                if opportunity.spread >= threshold
            ]
            for opportunity in opportunities:
                if opportunity.spread >= threshold:
                    continue
                logger.info(
                    "%s 规格%s：拼多多 ¥%s，最近成交 ¥%s，预计收入 ¥%s，预计利润 ¥%s，不报警",
                    opportunity.sku, opportunity.size, opportunity.pdd_price,
                    opportunity.recent_sale_price, opportunity.estimated_income, opportunity.spread,
                )
            if not qualifying:
                best = max(opportunities, key=lambda item: item.spread)
                storage.observation(
                    run_id, product, reference, "below_threshold",
                    f"全部匹配规格预计利润均低于 {threshold}", best.spread,
                )
                return
            assessed = [
                (opportunity, assess_opportunity(opportunity, quality_settings))
                for opportunity in qualifying
            ]
            strong_count = sum(assessment.level in {"A+", "A"} for _item, assessment in assessed)
            watch_count = len(assessed) - strong_count
            storage.observation(
                run_id, product, reference, "alert" if strong_count else "watch",
                f"强报警 {strong_count} 个，关注 {watch_count} 个",
                max(opportunity.spread for opportunity in qualifying),
            )
            for opportunity, assessment in assessed:
                payload = storage.record_alert(
                    opportunity,
                    category,
                    assessment.level,
                    assessment.reasons,
                )
                if not payload:
                    continue
                if assessment.level not in {"A+", "A"}:
                    logger.info(
                        "%s级关注 %s 规格%s：预计利润 ¥%s；%s",
                        assessment.level,
                        opportunity.sku,
                        opportunity.size,
                        opportunity.spread,
                        "；".join(assessment.reasons),
                    )
                    for notifier in notifiers or []:
                        notifier.send_opportunity(opportunity, category, assessment.level)
                    continue
                previous_report_path = report.path
                report.append(opportunity, category, reference.recent_sales)
                if report.path != previous_report_path:
                    logger.warning(
                        "原报警 Excel 正在使用，已自动切换到续写文件：%s",
                        report.path,
                    )
                message = (
                    f"{assessment.level}级 {opportunity.sku} 规格{opportunity.size}：拼多多 ¥{opportunity.pdd_price}，"
                    f"最近成交 ¥{opportunity.recent_sale_price}，保守试算 ¥{opportunity.evaluation_sale_price}，"
                    f"预计收入 ¥{opportunity.estimated_income}，"
                    f"预计利润 ¥{opportunity.spread}"
                )
                logger.warning("发现预计利润机会：%s", message)
                windows_toast("拼多多 -> 得物预计利润机会", message)
                for notifier in notifiers or []:
                    notifier.send_opportunity(opportunity, category, assessment.level)

        products = pdd.scan(
            pages=int(config["scan"]["pdd_pages_per_cycle"]),
            max_candidates=int(config["scan"]["max_candidates_per_cycle"]),
            candidate_due=lambda card: storage.candidate_due(
                card.sku,
                card.price,
                int(config["scan"]["same_product_cooldown_seconds"]),
            ),
            on_product=evaluate_product,
        )
        logger.info(
            "本轮有 %s 个推荐流商品取得详情规格并完成后续核验",
            len(products),
        )
        storage.finish_run(
            run_id,
            "completed",
            f"核验 {len(products)} 个推荐流商品",
        )
    except AuthenticationRequired as error:
        storage.finish_run(run_id, "authentication_required", str(error))
        logger.error("平台登录已失效，监控已暂停：%s", error)
        windows_toast("价差监控已暂停", str(error))
        raise
    except RiskDetected as error:
        storage.finish_run(run_id, "risk_control", str(error))
        logger.error("检测到平台风控，已停止本轮：%s", error)
        windows_toast("价差监控已暂停", str(error))
        raise
    except Exception as error:
        storage.finish_run(run_id, "failed", repr(error))
        logger.exception("本轮扫描失败")
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="拼多多与得物严格货号价差监控")
    parser.add_argument("--config", type=Path, default=ROOT / "config.json")
    parser.add_argument("--once", action="store_true", help="只执行一轮")
    args = parser.parse_args()
    config = load_config(args.config)
    runtime_dir = ROOT / config.get("runtime_dir", "runtime")
    logger = configure_logging(runtime_dir)
    license_client = LicenseClient(config, ROOT)
    try:
        license_status = license_client.require_scan(force_online=True)
    except LicenseClientError as error:
        logger.error("授权校验未通过，监控未启动：%s", error)
        windows_toast("价差监控授权不可用", str(error))
        return 4
    logger.info("软件授权：%s", license_status.message)
    instance_lock = acquire_single_instance(runtime_dir)
    if instance_lock is None:
        logger.info("监控器已经在运行，本次启动退出")
        return 3
    report = SessionReport(Path(config["report_dir"]))
    logger.info("本次启动的报警 Excel：%s", report.path)
    adb = Adb(
        Path(config["adb_path"]),
        config.get("adb_serial", "127.0.0.1:7555"),
        runtime_dir,
    )
    storage = Storage(runtime_dir / "monitor.sqlite3", runtime_dir / "alerts.jsonl")
    notifiers: list[
        PushDeerNotifier | NtfyNotifier | WeComBotNotifier
    ] = []
    wecom_bot = WeComBotNotifier.from_config(config, ROOT, logger)
    pushdeer = PushDeerNotifier.from_config(config, ROOT, logger)
    ntfy = NtfyNotifier.from_config(config, ROOT, logger)
    if wecom_bot is not None:
        notifiers.append(wecom_bot)
    if pushdeer is not None:
        notifiers.append(pushdeer)
    if ntfy is not None:
        notifiers.append(ntfy)
    logger.info("企业微信群机器人通知：%s", "已启用" if wecom_bot is not None else "未启用")
    logger.info("PushDeer 安卓通知：%s", "已启用" if pushdeer is not None else "未启用")
    logger.info("ntfy 安卓高优先级通知：%s", "已启用" if ntfy is not None else "未启用")
    interval = int(config["scan"]["interval_seconds"])
    while True:
        next_delay = interval
        try:
            license_client.require_scan()
            run_cycle(config, adb, storage, report, logger, notifiers)
        except LicenseClientError as error:
            logger.error("授权校验未通过，监控已停止：%s", error)
            windows_toast("价差监控授权不可用", str(error))
            return 4
        except (AuthenticationRequired, RiskDetected):
            return 2
        except Exception:
            if args.once:
                return 1
            next_delay = min(interval, 10)
        if args.once:
            return 0
        logger.info("等待 %s 秒后开始下一轮", next_delay)
        time.sleep(next_delay)


if __name__ == "__main__":
    raise SystemExit(main())
