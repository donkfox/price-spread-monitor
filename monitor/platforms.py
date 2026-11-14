from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal

from .adb import Adb, AdbError, AuthenticationRequired, RiskDetected
from .matching import (
    contains_exact_sku,
    extract_skus,
    normalize_sku,
    normalize_spec,
    normalize_size,
    parse_price,
    spec_sort_key,
    versions_compatible,
)
from .models import (
    Bounds,
    ProductCard,
    RecentSale,
    SalesProfile,
    VerifiedProduct,
    price_trend_label,
)
from .ui import UiTree


PDD_PACKAGE = "com.xunmeng.pinduoduo"
DEWU_PACKAGE = "com.shizhuang.duapp"
PDD_COMPONENT = f"{PDD_PACKAGE}/.ui.activity.MainFrameActivity"
DEWU_COMPONENT = f"{DEWU_PACKAGE}/.modules.home.ui.SplashActivity"


def _pdd_exclusion_reason(text: str) -> str | None:
    """Return the explicit listing condition that makes an item ineligible."""
    compact = re.sub(r"\s+", "", text)
    reasons: list[str] = []
    if "隔码" in compact:
        reasons.append("隔码")
    defect_markers = ("瑕疵", "微瑕", "瑕品")
    defect_negations = ("无瑕疵", "无微瑕", "非瑕疵", "不含瑕疵", "非瑕品")
    if (
        any(marker in compact for marker in defect_markers)
        and not any(marker in compact for marker in defect_negations)
    ):
        reasons.append("瑕疵")
    grade_b_markers = ("B品", "B级品", "B级")
    grade_b_negations = ("非B品", "不是B品", "非B级", "不是B级")
    if (
        any(marker in compact.upper() for marker in grade_b_markers)
        and not any(marker in compact.upper() for marker in grade_b_negations)
    ):
        reasons.append("B品")
    return "、".join(reasons) if reasons else None


@dataclass(frozen=True)
class PddSpecOption:
    spec: str
    bounds: Bounds
    in_stock: bool
    label: str


def _price_after_yuan(nodes: list[ET.Element], tree: UiTree) -> Decimal | None:
    # Search result cards may expose the currency and amount in one label.
    for node in nodes:
        label = tree.label(node)
        if not label or not any(marker in label for marker in ("\u00a5", "\uffe5")):
            continue
        price = parse_price(label)
        if price is not None:
            return price

    for index, node in enumerate(nodes):
        if tree.text(node) not in {"¥", "￥"}:
            continue
        for following in nodes[index + 1:index + 4]:
            label = tree.text(following)
            if label and label[0].isdigit():
                return parse_price(label)
    return None


class Pinduoduo:
    def __init__(self, adb: Adb, logger: logging.Logger):
        self.adb = adb
        self.log = logger

    @staticmethod
    def _is_bonded_warehouse(tree: UiTree) -> bool:
        # Bonded/cross-border fulfilment is excluded because delivery and
        # customs conditions are not comparable with ordinary stock.
        text = re.sub(r"\s+", "", tree.page_text()).lower()
        return any(
            marker in text
            for marker in (
                "保税仓",
                "保税区",
                "保税发货",
                "保税",
                "报税仓",
                "跨境电商",
                "跨境商品",
                "跨境发货",
                "跨境",
                "海外仓",
                "海淘",
            )
        )

    def prepare_feed(self) -> UiTree:
        return self._load_home_feed("pdd-feed", delay=5)

    @staticmethod
    def _is_home_feed(tree: UiTree) -> bool:
        return any(
            (tree.text(node) == "首页" or tree.desc(node) == "首页")
            and (bounds := tree.bounds(node)) is not None
            and bounds.top >= 1450
            for node in tree.nodes()
        )

    def _load_home_feed(
        self,
        name: str,
        delay: float = 2,
        reset_first: bool = False,
    ) -> UiTree:
        last_error: AdbError | None = None
        for attempt in range(2):
            try:
                self.adb.launch(
                    PDD_PACKAGE,
                    component=PDD_COMPONENT,
                    reset_task=reset_first or attempt > 0,
                )
                time.sleep(3)
                try:
                    self.adb.tap(90, 1550)
                except AdbError as error:
                    self.log.warning("拼多多首页按钮点击超时，继续检查当前页面：%s", error)
                tree = self.adb.dump_ui(
                    name if attempt == 0 else f"{name}-recovered",
                    delay=delay,
                )
                if self._is_home_feed(tree):
                    return tree
                last_error = AdbError("拼多多当前页面不是首页推荐流")
            except AdbError as error:
                last_error = error
            if attempt == 0:
                self.log.warning("拼多多推荐流未就绪，正在重启应用恢复：%s", last_error)
        raise last_error or AdbError("无法恢复拼多多首页推荐流")

    def parse_cards(self, tree: UiTree) -> list[ProductCard]:
        cards: list[ProductCard] = []
        for title_node in tree.find(resource_id=f"{PDD_PACKAGE}:id/tv_title"):
            container = tree.nearest_clickable(title_node)
            title_bounds = tree.bounds(title_node)
            if container is None or title_bounds is None or title_bounds.bottom < 300:
                continue
            title = tree.desc(title_node) or tree.text(title_node)
            skus = extract_skus(title)
            price = _price_after_yuan(list(container.iter()), tree)
            if not skus or price is None:
                continue
            # The card center can overlap Pinduoduo's store entry. The title
            # itself consistently opens the product detail page.
            cards.append(
                ProductCard(
                    title=title,
                    sku=skus[-1],
                    price=price,
                    bounds=title_bounds,
                )
            )
        return cards

    @staticmethod
    def _label_matches_target_variant(label: str, sku: str) -> bool:
        wanted = normalize_sku(sku)
        wanted_compact = wanted.replace("-", "")
        return any(
            (candidate := normalize_sku(value)) == wanted
            or candidate.replace("-", "") == wanted_compact
            or candidate.startswith(f"{wanted}-")
            or candidate.startswith(f"{wanted_compact}-")
            for value in extract_skus(label)
        )

    @staticmethod
    def _selector_identity_is_unambiguous(
        sku: str,
        title: str,
        selected_labels: list[str],
        selector_text: str,
    ) -> bool:
        if any(
            Pinduoduo._label_matches_target_variant(label, sku)
            for label in selected_labels
        ):
            return True
        title_skus = set(extract_skus(title))
        selector_skus = set(extract_skus(selector_text))
        wanted = normalize_sku(sku).replace("-", "")
        title_conflicts = {
            value for value in title_skus
            if normalize_sku(value).replace("-", "") != wanted
        }
        selector_conflicts = {
            value for value in selector_skus
            if normalize_sku(value).replace("-", "") != wanted
        }
        return not title_conflicts and not selector_conflicts and any(
            normalize_sku(value).replace("-", "") == wanted
            for value in title_skus
        )

    def _select_target_variant(self, tree: UiTree, sku: str) -> UiTree:
        selected_labels = [
            tree.label(node)
            for node in tree.nodes()
            if node.get("selected") == "true" and tree.label(node)
        ]
        if any(self._label_matches_target_variant(label, sku) for label in selected_labels):
            return tree

        candidates: list[tuple[str, Bounds]] = []
        seen: set[tuple[int, int, int, int]] = set()
        for node in tree.nodes():
            bounds = tree.bounds(node)
            label = tree.label(node)
            if (
                bounds is None
                or node.get("clickable") != "true"
                or node.get("enabled") != "true"
                or not 650 <= bounds.top < 1100
                or not self._label_matches_target_variant(label, sku)
            ):
                continue
            key = (bounds.left, bounds.top, bounds.right, bounds.bottom)
            if key not in seen:
                seen.add(key)
                candidates.append((label, bounds))

        if len(candidates) != 1:
            return tree
        label, bounds = candidates[0]
        self.log.info("%s：拼多多自动选择唯一匹配子款：%s", sku, label)
        self.adb.tap_bounds(bounds)
        return self.adb.dump_ui(f"pdd-selector-{sku}-variant", delay=0.7)

    @staticmethod
    def _option_spec(label: str) -> str | None:
        value = re.split(r"[¥￥]", label, maxsplit=1)[0].strip()
        for marker in Pinduoduo._out_of_stock_markers():
            value = value.replace(marker, " ")
        value = re.sub(r"\s+", " ", value).strip()
        shoe_size = re.match(
            r"^\s*((?:EU\s*)?(?:3[0-9]|4[0-9]|5[0-2])"
            r"(?:\.\d+|[⅓⅔]|\s*[12]\s*/\s*3)?\s*码?)(?![0-9.])",
            value,
            re.I,
        )
        if shoe_size and (normalized := normalize_size(shoe_size.group(1))):
            return normalized
        return normalize_spec(value)

    @staticmethod
    def _out_of_stock_markers() -> tuple[str, ...]:
        return (
            "无货",
            "售罄",
            "缺货",
            "补货",
            "抢光",
            "不可选",
            "暂不可选",
            "鍞絼",
            "鏃犺揣",
            "缂鸿揣",
            "琛ヨ揣",
            "鎶㈠厜",
        )

    @staticmethod
    def _option_in_stock(node: ET.Element, tree: UiTree) -> bool:
        label = " ".join(tree.descendant_labels(node))
        if any(marker in label for marker in Pinduoduo._out_of_stock_markers()):
            return False
        return node.get("clickable") == "true" and node.get("enabled") == "true"

    @staticmethod
    def _selected_purchase_price(tree: UiTree) -> Decimal | None:
        bottom_prices: list[Decimal] = []
        summary_prices: list[Decimal] = []
        # Some saved dumps contain legacy mojibake for the currency symbols.
        # Keep accepting it while preferring the real yen characters.
        price_markers = ("\u00a5", "\uffe5", "\u697c", "\u952c?")
        for node in tree.nodes():
            bounds = tree.bounds(node)
            label = tree.label(node)
            if bounds is None or not any(marker in label for marker in price_markers):
                continue
            price = parse_price(label)
            if price is None:
                continue
            if bounds.top >= 1450:
                bottom_prices.append(price)
            elif 500 <= bounds.top < 900:
                summary_prices.append(price)

        # The bottom bar can contain both the live purchase price and a
        # struck-through original price; the lower value is the live price.
        if bottom_prices:
            return min(bottom_prices)
        return min(summary_prices) if summary_prices else None

    @staticmethod
    def _spec_options(tree: UiTree, sku: str) -> list[tuple[str, Bounds]]:
        return [
            (option.spec, option.bounds)
            for option in Pinduoduo._spec_option_states(tree, sku)
            if option.in_stock
        ]

    @staticmethod
    def _spec_option_states(tree: UiTree, sku: str) -> list[PddSpecOption]:
        options: list[PddSpecOption] = []
        seen: set[tuple[str, int, int, int, int]] = set()
        for node in tree.nodes():
            bounds = tree.bounds(node)
            label = tree.label(node)
            if (
                bounds is None
                or not 680 <= bounds.top < 1400
                or extract_skus(label)
            ):
                continue
            descendant_text = " ".join(tree.descendant_labels(node))
            spec = Pinduoduo._option_spec(label)
            if spec is None:
                continue
            key = (spec, bounds.left, bounds.top, bounds.right, bounds.bottom)
            if key not in seen:
                seen.add(key)
                options.append(
                    PddSpecOption(
                        spec=spec,
                        bounds=bounds,
                        in_stock=Pinduoduo._option_in_stock(node, tree),
                        label=descendant_text or label,
                    )
                )
        return options

    def scan(
        self,
        pages: int,
        max_candidates: int,
        candidate_due: Callable[[ProductCard], bool] | None = None,
        on_product: Callable[[VerifiedProduct], None] | None = None,
    ) -> list[VerifiedProduct]:
        tree = self.prepare_feed()
        products: list[VerifiedProduct] = []
        seen: set[str] = set()
        attempted = 0
        for page in range(pages):
            cards = self.parse_cards(tree)
            self.log.info("拼多多第 %s 页发现 %s 个带货号候选", page + 1, len(cards))
            for queued_card in cards:
                if queued_card.sku in seen or attempted >= max_candidates:
                    continue
                card = next(
                    (current for current in self.parse_cards(tree) if current.sku == queued_card.sku),
                    None,
                )
                if card is None:
                    continue
                seen.add(card.sku)
                if reason := _pdd_exclusion_reason(card.title):
                    self.log.info("跳过 %s：商品标题标注%s", card.sku, reason)
                    continue
                if candidate_due is not None and not candidate_due(card):
                    self.log.info("跳过 %s：相同价格近期已经核验，继续浏览后续商品", card.sku)
                    continue
                attempted += 1
                self.log.info("核验拼多多 %s，列表候选价 ¥%s（不用于利润计算）", card.sku, card.price)
                try:
                    product = self.verify_card(card)
                except AdbError as error:
                    self.log.warning(
                        "跳过拼多多 %s：页面结构连续抓取失败，继续本轮后续商品：%s",
                        card.sku,
                        error,
                    )
                    tree = self.resume_feed()
                    continue
                if product:
                    products.append(product)
                    if on_product is not None:
                        on_product(product)
                        tree = self.resume_feed()
                        continue
                tree = self.resume_feed()
            if attempted >= max_candidates or page == pages - 1:
                break
            try:
                self.adb.swipe(760, 1320, 760, 560)
                tree = self.adb.dump_ui(f"pdd-feed-{page + 2}", delay=2)
            except AdbError as error:
                self.log.warning("拼多多翻页抓取失败，本轮停止翻页：%s", error)
                break
        self.log.info(
            "拼多多本轮共尝试 %s 个候选，其中 %s 个取得详情规格",
            attempted,
            len(products),
        )
        if attempted >= max_candidates:
            self.log.info("拼多多本轮已核验满 %s 个候选，开始刷新首页", max_candidates)
        else:
            self.log.info(
                "拼多多本轮可核验候选不足 %s 个（排除重复、无货或无可靠货号后为 %s 个），开始刷新首页",
                max_candidates,
                attempted,
            )
        try:
            self.refresh_feed()
        except AdbError as error:
            self.log.warning("拼多多本轮刷新失败，已保留任务并将在下一轮恢复：%s", error)
        return products

    def resume_feed(self) -> UiTree:
        # Preserve the current recommendation page so queued cards remain
        # available after Dewu verification. Only reset if Android fails to
        # restore Pinduoduo's existing task.
        try:
            self.adb.launch(PDD_PACKAGE, component=PDD_COMPONENT, reset_task=False)
            tree = self.adb.dump_ui("pdd-feed-resumed", delay=2)
            if self._is_home_feed(tree):
                return tree
        except AdbError as error:
            self.log.warning("拼多多原推荐页恢复失败，准备重新载入：首页恢复错误：%s", error)
        return self._load_home_feed(
            "pdd-feed-resumed-recovered",
            delay=2,
            reset_first=True,
        )

    def refresh_feed(self) -> None:
        self.adb.tap(90, 1550)
        time.sleep(1)
        self.adb.tap(90, 1550)
        before = self.adb.dump_ui("pdd-refresh-before", delay=2)
        if not self._is_home_feed(before):
            before = self._load_home_feed(
                "pdd-refresh-home-recovered",
                delay=2,
                reset_first=True,
            )

        top = self._return_feed_to_top(before)
        top_fingerprint = self._feed_fingerprint(top)

        for attempt in range(2):
            self.adb.swipe(450, 360, 450, 1360, 900)
            after = self.adb.dump_ui(f"pdd-refresh-after-{attempt + 1}", delay=4)
            if (
                self._is_home_feed(after)
                and self._feed_fingerprint(after) != top_fingerprint
            ):
                self.log.info("拼多多主页刷新成功，已确认推荐商品发生变化")
                return

        self.log.warning("拼多多回到顶部后下拉内容未变化，正在重新载入首页再刷新一次")
        self.adb.launch(PDD_PACKAGE, component=PDD_COMPONENT, reset_task=True)
        time.sleep(8)
        self.adb.tap(90, 1550)
        relaunched = self.adb.dump_ui("pdd-refresh-relaunched", delay=4)
        if not self._is_home_feed(relaunched):
            raise AdbError("重新载入后仍未进入拼多多首页推荐流")

        relaunched_top = self._return_feed_to_top(relaunched)
        relaunched_fingerprint = self._feed_fingerprint(relaunched_top)
        if relaunched_fingerprint != top_fingerprint:
            self.log.info("拼多多前两种刷新未更新内容，重新载入后已确认商品变化")
            return

        self.adb.swipe(450, 360, 450, 1360, 900)
        recovered = self.adb.dump_ui("pdd-refresh-relaunched-after", delay=4)
        if (
            self._is_home_feed(recovered)
            and self._feed_fingerprint(recovered) != relaunched_fingerprint
        ):
            self.log.info("拼多多重新载入后下拉刷新成功，已确认推荐商品发生变化")
            return

        raise AdbError("已回到首页顶部并重试刷新，但推荐商品仍未变化")

    def _return_feed_to_top(self, tree: UiTree, max_swipes: int = 8) -> UiTree:
        """Move a deep recommendation feed to its top before pull-to-refresh."""
        current = tree
        current_fingerprint = self._feed_fingerprint(current)
        unchanged = 0
        for attempt in range(max_swipes):
            # A shorter gesture moves towards the top without relying on one
            # long pull being enough from a deeply scrolled recommendation feed.
            self.adb.swipe(450, 650, 450, 1250, 300)
            current = self.adb.dump_ui(
                f"pdd-refresh-return-top-{attempt + 1}",
                delay=1,
            )
            if not self._is_home_feed(current):
                raise AdbError("回到顶部时离开了拼多多首页推荐流")
            fingerprint = self._feed_fingerprint(current)
            if fingerprint == current_fingerprint:
                unchanged += 1
                if unchanged >= 2:
                    self.log.info("拼多多推荐流已回到顶部，准备执行下拉刷新")
                    return current
            else:
                unchanged = 0
            current_fingerprint = fingerprint

        self.log.info("拼多多推荐流已连续向顶部回拉 %s 次，准备执行下拉刷新", max_swipes)
        return current

    @staticmethod
    def _feed_fingerprint(tree: UiTree) -> tuple[str, ...]:
        return tuple(
            tree.desc(node) or tree.text(node)
            for node in tree.find(resource_id=f"{PDD_PACKAGE}:id/tv_title")
            if tree.desc(node) or tree.text(node)
        )

    def _switch_home_category(self, tree: UiTree) -> UiTree | None:
        category_names = {"运动", "手机", "鞋包", "百货", "男装", "女装", "食品"}
        category = next(
            (
                node for node in tree.nodes()
                if tree.text(node) in category_names
                and (bounds := tree.bounds(node)) is not None
                and bounds.top < 250
            ),
            None,
        )
        if category is None:
            return None
        target = tree.nearest_clickable(category) or category
        bounds = tree.bounds(target)
        if bounds is None:
            return None
        self.adb.tap_bounds(bounds)
        category_tree = self.adb.dump_ui("pdd-refresh-category", delay=3)
        recommendation = next(
            (
                node for node in category_tree.find(text="推荐")
                if (node_bounds := category_tree.bounds(node)) is not None and node_bounds.top < 250
            ),
            None,
        )
        if recommendation is None:
            return None
        target = category_tree.nearest_clickable(recommendation) or recommendation
        target_bounds = category_tree.bounds(target)
        if target_bounds is None:
            return None
        self.adb.tap_bounds(target_bounds)
        return self.adb.dump_ui("pdd-refresh-recommendation", delay=4)

    def verify_card(self, card: ProductCard) -> VerifiedProduct | None:
        selector_open = False
        self.adb.tap_bounds(card.bounds)
        try:
            detail = self.adb.dump_ui(f"pdd-detail-{card.sku}", delay=3)
            title_nodes = detail.find(resource_id=f"{PDD_PACKAGE}:id/tv_title")
            titles = [detail.label(node) for node in title_nodes]
            title = next((value for value in titles if contains_exact_sku(value, card.sku)), "")
            if not title:
                title = next(
                    (detail.label(node) for node in detail.nodes() if contains_exact_sku(detail.label(node), card.sku)),
                    "",
                )
            if not title:
                self.log.warning(
                    "跳过 %s：点击后未进入对应商品详情（可能误入店铺），正在返回首页",
                    card.sku,
                )
                return None
            if self._is_bonded_warehouse(detail):
                self.log.warning("跳过 %s：拼多多商品为保税/跨境仓方案", card.sku)
                return None
            if reason := _pdd_exclusion_reason(detail.page_text()):
                self.log.warning("跳过 %s：拼多多详情标注%s", card.sku, reason)
                return None

            cta = next(
                (
                    node for node in detail.nodes()
                    if node.get("clickable") == "true"
                    and (bounds := detail.bounds(node)) is not None
                    and bounds.top >= 1400
                    and "¥" in detail.label(node)
                ),
                None,
            )
            if cta is None:
                self.log.warning("跳过 %s：找不到拼多多规格入口", card.sku)
                return None
            self.adb.tap_bounds(detail.bounds(cta))  # type: ignore[arg-type]
            selector_open = True
            selector = self.adb.dump_ui(f"pdd-selector-{card.sku}", delay=1.5)
            if self._is_bonded_warehouse(selector):
                self.log.warning("跳过 %s：拼多多规格页确认是保税/跨境仓方案", card.sku)
                return None
            selector = self._select_target_variant(selector, card.sku)
            if self._is_bonded_warehouse(selector):
                self.log.warning("跳过 %s：拼多多规格页确认是保税/跨境仓方案", card.sku)
                return None
            if reason := _pdd_exclusion_reason(selector.page_text()):
                self.log.warning("跳过 %s：拼多多规格页标注%s", card.sku, reason)
                return None
            selected_labels = [
                selector.label(node)
                for node in selector.nodes()
                if node.get("selected") == "true" and selector.label(node)
            ]
            if not self._selector_identity_is_unambiguous(
                card.sku, title, selected_labels, selector.page_text()
            ):
                self.log.warning("跳过 %s：拼多多规格存在其他货号或无法确认唯一型号", card.sku)
                return None

            variant = next(
                (
                    label for label in selected_labels
                    if self._label_matches_target_variant(label, card.sku)
                ),
                " / ".join(selected_labels),
            )
            resolved_sku = next(
                (
                    value
                    for label in selected_labels
                    for value in extract_skus(label)
                    if self._label_matches_target_variant(value, card.sku)
                ),
                card.sku,
            )
            option_states = self._spec_option_states(selector, card.sku)
            stock_states = {
                option.spec: option.in_stock
                for option in option_states
            }
            stock_labels = {
                option.spec: option.label
                for option in option_states
            }
            options = [
                (option.spec, option.bounds)
                for option in option_states
                if option.in_stock
            ]
            spec_prices: dict[str, Decimal] = {}
            for spec, bounds in options:
                self.adb.tap_bounds(bounds)
                selected_tree = self.adb.dump_ui(f"pdd-{card.sku}-{spec}", delay=0.5)
                selected_specs = {
                    self._option_spec(selected_tree.text(node))
                    for node in selected_tree.nodes()
                    if node.get("selected") == "true"
                    and not contains_exact_sku(selected_tree.text(node), card.sku)
                }
                if spec not in selected_specs:
                    self.log.warning("%s 规格%s：拼多多点击后未显示为已选，跳过", card.sku, spec)
                    continue
                price = self._selected_purchase_price(selected_tree)
                if price is None:
                    price = next(
                        (
                            parse_price(selected_tree.text(node))
                            for node in selected_tree.nodes()
                            if node.get("selected") == "true"
                            and self._option_spec(selected_tree.text(node)) == spec
                            and ("¥" in selected_tree.text(node) or "￥" in selected_tree.text(node))
                        ),
                        None,
                    )
                if price is not None:
                    spec_prices[spec] = price
                else:
                    self.log.warning("%s 规格%s：未读取到详情页实际购买价", card.sku, spec)
            if not spec_prices and not stock_states:
                self.log.warning("跳过 %s：没有取得任何详情规格的实际购买价", card.sku)
                return None
            if not spec_prices:
                self.log.warning("%s：没有可购买规格价格，仅记录库存状态", card.sku)
            return VerifiedProduct(
                platform="pdd",
                title=title,
                sku=resolved_sku,
                base_price=min(spec_prices.values()) if spec_prices else card.price,
                variant=variant,
                size_prices=spec_prices,
                stock_states=stock_states,
                stock_labels=stock_labels,
                source_sku=card.sku,
            )
        finally:
            if selector_open:
                self.adb.back()
                time.sleep(0.5)
            self.adb.back()
            time.sleep(1.5)


class Dewu:
    def __init__(self, adb: Adb, logger: logging.Logger):
        self.adb = adb
        self.log = logger

    @staticmethod
    def _startup_dialog_target(tree: UiTree) -> ET.Element | None:
        consent = tree.find(text="同意并继续")
        if consent:
            return tree.nearest_clickable(consent[0]) or consent[0]
        if tree.find(text="新版本抢先体验"):
            close = tree.find(resource_id=f"{DEWU_PACKAGE}:id/ivClose")
            if close:
                return tree.nearest_clickable(close[0]) or close[0]
        return None

    @staticmethod
    def _login_required(tree: UiTree) -> bool:
        page = tree.page_text()
        return (
            "手机号登录" in page and "验证并登录" in page
        ) or (
            "换绑新手机号" in page and "找回账号" in page
        )

    @classmethod
    def _require_login(cls, tree: UiTree) -> None:
        if cls._login_required(tree):
            raise AuthenticationRequired(
                "得物登录已失效，请打开 MuMu 登录得物后重新开启价差检测"
            )

    def _dismiss_startup_dialogs(self, tree: UiTree, sku: str) -> tuple[UiTree, bool]:
        dismissed = False
        for attempt in range(3):
            target = self._startup_dialog_target(tree)
            if target is None:
                break
            bounds = tree.bounds(target)
            if bounds is None:
                break
            label = tree.text(target) or target.get("resource-id", "启动提示")
            self.log.info("得物启动提示已处理：%s", label)
            self.adb.tap_bounds(bounds)
            dismissed = True
            tree = self.adb.dump_ui(f"dewu-startup-{sku}-{attempt + 1}", delay=2)
        return tree, dismissed

    def _search(self, sku: str) -> UiTree:
        for attempt in range(2):
            try:
                return self._search_once(sku)
            except (AdbError, AuthenticationRequired, RiskDetected):
                raise
            except RuntimeError as error:
                if attempt == 1:
                    raise
                self.log.warning("得物出售页导航失败，正在从首页重试一次：%s", error)
        raise RuntimeError("得物出售页导航失败")

    @staticmethod
    def _sell_search_fields(tree: UiTree) -> list[ET.Element]:
        return [
            node
            for node in tree.nodes()
            if "请输入商品名称/货号/条形码" in tree.label(node)
        ]

    @staticmethod
    def _sell_center_button(tree: UiTree) -> ET.Element | None:
        nodes = tree.find(resource_id=f"{DEWU_PACKAGE}:id/tvBtnSale")
        if nodes:
            return tree.nearest_clickable(nodes[0]) or nodes[0]
        return next(
            (
                tree.nearest_clickable(node) or node
                for node in tree.nodes()
                if tree.label(node) == "我要出售"
            ),
            None,
        )

    @staticmethod
    def _seller_mode_switch(tree: UiTree) -> ET.Element | None:
        nodes = tree.find(resource_id=f"{DEWU_PACKAGE}:id/llTabSwitch")
        if nodes:
            return tree.nearest_clickable(nodes[0]) or nodes[0]
        return next(
            (
                tree.nearest_clickable(node) or node
                for node in tree.nodes()
                if tree.label(node) == "切换商家版"
            ),
            None,
        )

    @staticmethod
    def _switch_confirmation(tree: UiTree) -> ET.Element | None:
        return next(
            (
                tree.nearest_clickable(node) or node
                for node in tree.nodes()
                if tree.label(node) == "确认切换"
            ),
            None,
        )

    def _open_sell_search(self, profile: UiTree, sku: str) -> UiTree:
        """Enter the current seller-center search page across Dewu app versions."""
        search_fields = self._sell_search_fields(profile)
        if search_fields:
            return profile

        # If the app remembers seller mode, the "I" tab opens the seller
        # center directly and exposes "我要出售" on this first page.
        sell_button = self._sell_center_button(profile)
        if sell_button is not None:
            sell_bounds = profile.bounds(sell_button)
            if sell_bounds is None:
                raise RuntimeError("得物“我要出售”按钮没有可点击区域")
            self.adb.tap_bounds(sell_bounds)
            return self.adb.dump_ui(f"dewu-sell-search-{sku}", delay=3)

        # New buyer profile: switch to seller center before looking for the
        # sell button. The old sellerRootView now opens order management.
        switch = self._seller_mode_switch(profile)
        if switch is not None:
            switch_bounds = profile.bounds(switch)
            if switch_bounds is None:
                raise RuntimeError("得物“切换商家版”入口没有可点击区域")
            self.adb.tap_bounds(switch_bounds)
            switched = self.adb.dump_ui(f"dewu-seller-switch-{sku}", delay=1.5)
            confirmation = self._switch_confirmation(switched)
            if confirmation is not None:
                confirmation_bounds = switched.bounds(confirmation)
                if confirmation_bounds is None:
                    raise RuntimeError("得物“确认切换”按钮没有可点击区域")
                self.adb.tap_bounds(confirmation_bounds)
                switched = self.adb.dump_ui(
                    f"dewu-seller-center-{sku}", delay=3
                )
            sell_button = self._sell_center_button(switched)
            if sell_button is not None:
                sell_bounds = switched.bounds(sell_button)
                if sell_bounds is None:
                    raise RuntimeError("得物“我要出售”按钮没有可点击区域")
                self.adb.tap_bounds(sell_bounds)
                return self.adb.dump_ui(f"dewu-sell-search-{sku}", delay=3)
            if self._sell_search_fields(switched):
                return switched
            raise RuntimeError("得物商家中心没有找到“我要出售”入口")

        # Compatibility path for older app versions that exposed the seller
        # card directly on the buyer profile.
        sellers = profile.find(resource_id=f"{DEWU_PACKAGE}:id/sellerRootView")
        if sellers:
            seller = profile.nearest_clickable(sellers[0]) or sellers[0]
            seller_bounds = profile.bounds(seller)
            if seller_bounds is None:
                raise RuntimeError("得物“出售”入口没有可点击区域")
            self.adb.tap_bounds(seller_bounds)
            legacy = self.adb.dump_ui(f"dewu-sell-legacy-{sku}", delay=3)
            search_fields = self._sell_search_fields(legacy)
            if not search_fields:
                sell_button = self._sell_center_button(legacy)
                if sell_button is not None:
                    sell_bounds = legacy.bounds(sell_button)
                    if sell_bounds is not None:
                        self.adb.tap_bounds(sell_bounds)
                        legacy = self.adb.dump_ui(
                            f"dewu-sell-search-{sku}", delay=3
                        )
            if self._sell_search_fields(legacy):
                return legacy
            raise RuntimeError("得物旧版出售入口打开的是订单管理页")

        raise RuntimeError("得物“我”页面没有找到出售入口")

    def _search_once(self, sku: str) -> UiTree:
        self.adb.launch(DEWU_PACKAGE, component=DEWU_COMPONENT, reset_task=True)
        startup = self.adb.dump_ui(f"dewu-start-{sku}", delay=3)
        self._require_login(startup)
        startup, _ = self._dismiss_startup_dialogs(startup, sku)

        me_nodes = startup.find(resource_id=f"{DEWU_PACKAGE}:id/tvUser")
        if not me_nodes:
            raise RuntimeError("得物首页没有找到“我”入口")
        me = startup.nearest_clickable(me_nodes[0]) or me_nodes[0]
        me_bounds = startup.bounds(me)
        if me_bounds is None:
            raise RuntimeError("得物“我”入口没有可点击区域")
        self.adb.tap_bounds(me_bounds)

        profile = self.adb.dump_ui(f"dewu-me-{sku}", delay=2)
        self._require_login(profile)
        search = self._open_sell_search(profile, sku)
        self._require_login(search)
        search_fields = self._sell_search_fields(search)
        if not search_fields:
            raise RuntimeError("未进入得物“我要出售”商品搜索页")

        search_target = search.nearest_clickable(search_fields[0]) or search_fields[0]
        search_bounds = search.bounds(search_target)
        if search_bounds is None:
            raise RuntimeError("得物出售搜索框没有可点击区域")
        self.adb.tap_bounds(search_bounds)
        time.sleep(0.5)
        self.adb.clear_focused_text()
        self.adb.type_ascii(sku)
        self.adb.shell("input", "keyevent", "66")
        results = self.adb.dump_ui(f"dewu-sell-results-{sku}", delay=3)
        self._require_login(results)
        return results

    def _result_cards(self, tree: UiTree) -> list[ET.Element]:
        cards: list[ET.Element] = []
        seen_bounds: set[tuple[int, int, int, int]] = set()
        for node in tree.nodes():
            candidate: ET.Element | None = None
            description = tree.desc(node)
            if node.get("clickable") == "true" and "去出价" in description and "7日均价" in description:
                candidate = node
            elif description == "product_item" and node.get("clickable") == "true":
                candidate = node
            elif node.get("resource-id") in {
                f"{DEWU_PACKAGE}:id/tvAccurateProductName",
                f"{DEWU_PACKAGE}:id/search_title",
            }:
                candidate = tree.nearest_clickable(node)
            if candidate is None:
                continue
            bounds = tree.bounds(candidate)
            if not bounds or bounds.bottom <= 160:
                continue
            key = (bounds.left, bounds.top, bounds.right, bounds.bottom)
            if key not in seen_bounds:
                seen_bounds.add(key)
                cards.append(candidate)
        cards.sort(
            key=lambda candidate: (
                tree.bounds(candidate).top,  # type: ignore[union-attr]
                tree.bounds(candidate).left,  # type: ignore[union-attr]
            )
        )
        return cards

    @staticmethod
    def _card_title(tree: UiTree, card: ET.Element) -> str:
        title_ids = {
            f"{DEWU_PACKAGE}:id/tvAccurateProductName",
            f"{DEWU_PACKAGE}:id/search_title",
        }
        return next(
            (
                tree.text(node)
                for node in card.iter()
                if node.get("resource-id") in title_ids and tree.text(node)
            ),
            next(
                (tree.text(node) for node in card.iter() if len(tree.text(node)) > 12 and "¥" not in tree.text(node)),
                "",
            ),
        )

    def verify(
        self,
        sku: str,
        source_title: str = "",
        max_results: int = 3,
        target_sizes: set[str] | None = None,
        recent_sale_max_age_days: int = 7,
    ) -> VerifiedProduct | None:
        results = self._search(sku)
        cards = self._result_cards(results)[:max_results]
        self.log.info("得物“我要出售”搜索 %s 返回 %s 个当前可见商品", sku, len(cards))
        for index, card in enumerate(cards):
            bounds = results.bounds(card)
            if not bounds:
                continue
            result_title = self._card_title(results, card)
            self.adb.tap_bounds(bounds)
            try:
                quote = self.adb.dump_ui(f"dewu-sell-quote-{sku}-{index}", delay=3)
                confirmation = self._sell_confirmation(quote)
                if confirmation is not None:
                    button, confirmation_sku, confirmation_title = confirmation
                    if not confirmation_sku or not contains_exact_sku(confirmation_sku, sku):
                        self.log.warning(
                            "%s：得物“是这款”确认页货号 %s 与目标货号不一致",
                            sku, confirmation_sku or "未读取到",
                        )
                        continue
                    if source_title and confirmation_title and not versions_compatible(
                        source_title, confirmation_title
                    ):
                        self.log.warning(
                            "%s：得物“是这款”确认页版本与拼多多不一致：%s",
                            sku, confirmation_title.replace("\n", " "),
                        )
                        continue
                    button_bounds = quote.bounds(button)
                    if button_bounds is None:
                        self.log.warning("%s：得物“是这款”按钮没有可点击区域", sku)
                        continue
                    self.log.info("%s：得物二次确认货号一致，点击“是这款”继续核价", sku)
                    self.adb.tap_bounds(button_bounds)
                    quote = self.adb.dump_ui(
                        f"dewu-sell-quote-{sku}-{index}-confirmed", delay=2
                    )
                if "切换可查看预计收入" in quote.page_text():
                    self.adb.tap(450, 180)
                    quote = self.adb.dump_ui(f"dewu-sell-quote-{sku}-{index}-ready", delay=0.7)
                product = self._verify_sell_quote(
                    quote,
                    sku,
                    source_title=source_title,
                    result_title=result_title,
                    target_sizes=target_sizes,
                    recent_sale_max_age_days=recent_sale_max_age_days,
                )
                if product:
                    return product
            finally:
                self.adb.back()
                time.sleep(1)
        return None

    @staticmethod
    def _sell_confirmation(
        tree: UiTree,
    ) -> tuple[ET.Element, str, str] | None:
        button_node = next(
            (
                node for node in tree.nodes()
                if tree.text(node) == "是这款" or tree.desc(node) == "是这款"
            ),
            None,
        )
        if button_node is None:
            return None
        button = tree.nearest_clickable(button_node) or button_node

        sku = ""
        sku_top = 1600
        for node in tree.nodes():
            label = tree.label(node)
            if "主货号" not in label:
                continue
            candidates = extract_skus(label)
            if candidates:
                sku = candidates[0]
                bounds = tree.bounds(node)
                if bounds:
                    sku_top = bounds.top
                break

        titles: list[tuple[int, str]] = []
        ignored_text = ("请确认", "易发错货", "主货号", "扫码确认", "是这款")
        for node in tree.nodes():
            label = tree.text(node)
            bounds = tree.bounds(node)
            if bounds is None or bounds.top >= sku_top or len(label) <= 18:
                continue
            if any(word in label for word in ignored_text):
                continue
            titles.append((bounds.top, label))
        title = max(titles, default=(0, ""))[1]
        return button, sku, title

    @staticmethod
    def _quote_actual_sku(tree: UiTree) -> str:
        confirmations = [
            node for node in tree.nodes()
            if tree.text(node) == "货号确认" or tree.desc(node) == "货号确认"
        ]
        if not confirmations:
            return ""
        confirmation_bounds = tree.bounds(confirmations[0])
        if confirmation_bounds is None:
            return ""
        for node in tree.nodes():
            bounds = tree.bounds(node)
            if bounds is None or bounds.left >= confirmation_bounds.left:
                continue
            if abs(bounds.top - confirmation_bounds.top) > 35:
                continue
            skus = extract_skus(tree.label(node))
            if skus:
                return skus[0]
        return ""

    @staticmethod
    def _quote_title(tree: UiTree) -> str:
        confirmation = next(
            (
                node for node in tree.nodes()
                if tree.text(node) == "货号确认" or tree.desc(node) == "货号确认"
            ),
            None,
        )
        confirmation_bounds = tree.bounds(confirmation) if confirmation is not None else None
        if confirmation_bounds is None:
            return ""
        candidates: list[tuple[int, str]] = []
        for node in tree.nodes():
            bounds = tree.bounds(node)
            label = tree.text(node)
            if bounds is None or not confirmation_bounds.top - 100 <= bounds.top < confirmation_bounds.top:
                continue
            if len(label) > 18 and "¥" not in label and not extract_skus(label):
                candidates.append((bounds.top, label))
        return max(candidates, default=(0, ""))[1]

    def _verify_sell_quote(
        self,
        quote: UiTree,
        sku: str,
        source_title: str = "",
        result_title: str = "",
        target_sizes: set[str] | None = None,
        recent_sale_max_age_days: int = 7,
    ) -> VerifiedProduct | None:
        if "最低出售价" not in quote.page_text():
            self.log.warning("%s：得物未进入最低出售价页面", sku)
            return None
        title = self._quote_title(quote) or result_title
        reference_title = title or result_title
        for candidate_title in (result_title, title):
            if source_title and candidate_title and not versions_compatible(source_title, candidate_title):
                self.log.warning("得物候选版本与拼多多不一致：%s", candidate_title.replace("\n", " "))
                return None
        actual_sku = self._quote_actual_sku(quote)
        if not actual_sku:
            self.log.warning("%s：得物出售页没有读取到货号", sku)
            return None
        if not contains_exact_sku(actual_sku, sku):
            self.log.warning("%s：得物出售页货号 %s 与目标货号不一致", sku, actual_sku)
            return None

        size_prices = self._parse_size_prices(quote)
        if not size_prices:
            self.log.warning("%s：得物出售页没有可用的规格最低出售价", sku)
            return None
        wanted_sizes = set(size_prices)
        if target_sizes is not None:
            wanted_sizes &= target_sizes
        recent_sales = self._read_recent_sales(
            quote,
            sku,
            wanted_sizes,
            max_age_days=recent_sale_max_age_days,
        )
        recent_sale_prices: dict[str, Decimal] = {}
        evaluation_sale_prices: dict[str, Decimal] = {}
        sales_profiles: dict[str, SalesProfile] = {}
        now = datetime.now().astimezone()
        for size in sorted(wanted_sizes, key=spec_sort_key):
            profile = self._sales_profile(
                recent_sales,
                size,
                now,
                recent_sale_max_age_days,
                current_ask=size_prices.get(size),
            )
            if profile is None:
                self.log.info(
                    "%s 规格%s：没有取得可确认在 %s 天内的同规格成交，作废",
                    sku, size, recent_sale_max_age_days,
                )
                continue
            sales_profiles[size] = profile
            recent_sale_prices[size] = profile.latest_price
            evaluation_sale_prices[size] = profile.conservative_price
            self.log.info(
                "%s 规格%s：7天观察成交 %s 次，活跃 %s 天，最近 %.1f 小时，"
                "价格 ¥%s-¥%s，趋势%s，保守试算价 ¥%s",
                sku, size, profile.observed_sales_7d, profile.active_days_7d,
                profile.latest_age_hours or 0, profile.minimum_price,
                profile.maximum_price, price_trend_label(profile.price_trend),
                profile.conservative_price,
            )
        eligible_sizes = set(size_prices) & set(recent_sale_prices)
        if target_sizes is not None:
            eligible_sizes &= target_sizes
        estimated_incomes = self._estimate_incomes(
            quote,
            sku,
            {
                size: evaluation_sale_prices[size]
                for size in sorted(eligible_sizes, key=spec_sort_key)
            },
        )
        return VerifiedProduct(
            platform="dewu",
            title=reference_title,
            sku=sku,
            base_price=min(size_prices.values()),
            variant=actual_sku,
            size_prices=size_prices,
            recent_sales=recent_sales,
            recent_sale_prices={
                size: recent_sale_prices[size]
                for size in estimated_incomes
            },
            evaluation_sale_prices={
                size: evaluation_sale_prices[size]
                for size in estimated_incomes
            },
            sales_profiles={
                size: sales_profiles[size]
                for size in estimated_incomes
            },
            estimated_incomes=estimated_incomes,
        )

    @staticmethod
    def _size_cell(tree: UiTree, target_size: str) -> ET.Element | None:
        for node in tree.nodes():
            if node.get("clickable") != "true":
                continue
            description = tree.desc(node)
            match = re.fullmatch(r"\s*(.+?)\s*[,，]\s*[¥￥]\s*(--|\d+(?:\.\d+)?)\s*", description)
            if match and normalize_spec(match.group(1)) == target_size:
                return node
        return None

    def _visible_size_cell(
        self,
        tree: UiTree,
        sku: str,
        target_size: str,
    ) -> tuple[UiTree, ET.Element | None]:
        current = tree
        reset_done = False
        for attempt in range(12):
            cell = self._size_cell(current, target_size)
            bounds = current.bounds(cell) if cell is not None else None
            # The quote list begins below the top toolbar. A target is usable
            # anywhere in the list viewport; requiring top >= 820 skips sizes
            # that become visible in the middle of the list after one swipe.
            if bounds is not None and bounds.top >= 120 and bounds.bottom <= 1495:
                return current, cell

            if attempt == 6 and not reset_done:
                for _ in range(4):
                    self.adb.swipe(760, 900, 760, 1400, 350)
                reset_done = True
            else:
                self.adb.swipe(760, 1400, 760, 850, 350)
            current = self.adb.dump_ui(
                f"dewu-spec-{sku}-{target_size}-scroll-{attempt + 1}", delay=0.5
            )
        return current, None

    @staticmethod
    def _label_target(tree: UiTree, label: str) -> ET.Element | None:
        node = next(
            (
                candidate for candidate in tree.nodes()
                if tree.text(candidate) == label or tree.desc(candidate) == label
            ),
            None,
        )
        return tree.nearest_clickable(node) if node is not None else None

    @staticmethod
    def _offer_action(tree: UiTree) -> tuple[ET.Element | None, int]:
        for node in tree.nodes():
            for label in (tree.text(node), tree.desc(node)):
                match = re.fullmatch(r"去出价\s*[（(]\s*(\d+)\s*[）)]", label)
                if match:
                    return tree.nearest_clickable(node) or node, int(match.group(1))
                if label == "去出价":
                    return tree.nearest_clickable(node) or node, 1
        return None, 0

    def _tap_spec_cell(self, bounds: Bounds) -> None:
        x, y = bounds.center
        # The fixed bottom action bar starts around y=1519 and can cover the
        # centre of the last visible specification row.
        safe_bottom = min(bounds.bottom - 8, 1510)
        if safe_bottom > bounds.top + 8:
            y = (bounds.top + safe_bottom) // 2
        self.adb.tap(x, y)

    def _deselect_size(self, quote: UiTree, sku: str, size: str) -> UiTree:
        quote, selected_cell = self._visible_size_cell(quote, sku, size)
        selected_bounds = quote.bounds(selected_cell) if selected_cell is not None else None
        if selected_bounds is None:
            self.log.warning("%s 规格%s：返回后未找到已选规格，无法自动取消选择", sku, size)
            return quote
        self._tap_spec_cell(selected_bounds)
        return self.adb.dump_ui(f"dewu-income-{sku}-{size}-deselected", delay=0.5)

    def _restore_quote_and_deselect(self, sku: str, size: str) -> UiTree | None:
        quote = self._return_to_quote(sku, size)
        return self._deselect_size(quote, sku, size) if quote is not None else None

    def _dismiss_income_interstitials(self, tree: UiTree, sku: str, size: str) -> UiTree:
        for step in range(5):
            target: ET.Element | None = None
            for label in ("下一步", "知道了"):
                target = self._label_target(tree, label)
                if target is not None:
                    break
            if target is None and "请再次确认" in tree.page_text():
                target = self._label_target(tree, "确认出售")
            if target is None:
                break
            bounds = tree.bounds(target)
            if bounds is None:
                break
            self.adb.tap_bounds(bounds)
            tree = self.adb.dump_ui(
                f"dewu-income-{sku}-{size}-interstitial-{step + 1}", delay=0.7
            )
        return tree

    @staticmethod
    def _estimated_income(tree: UiTree) -> Decimal | None:
        def signed_price(label: str) -> Decimal | None:
            price = parse_price(label)
            if price is None:
                return None
            symbol_index = max(label.find("¥"), label.find("￥"))
            tail = label[symbol_index + 1:] if symbol_index >= 0 else label
            if re.search(r"[-−－][^0-9]{0,6}\d", tail):
                return -price
            return price

        label_node = next(
            (node for node in tree.nodes() if tree.text(node) == "预计最高收入"),
            None,
        )
        label_bounds = tree.bounds(label_node) if label_node is not None else None
        if label_bounds is None:
            return None

        # Newer Dewu layouts expose the whole income row on the parent while
        # splitting the currency, amount and unit into separate child nodes.
        current = label_node
        while current is not None:
            label = tree.label(current)
            if "预计最高收入" in label and "/件" in label and ("¥" in label or "￥" in label):
                price = signed_price(label)
                if price is not None:
                    return price
            current = tree.parent.get(current)

        candidates: list[tuple[int, Decimal]] = []
        for node in tree.nodes():
            bounds = tree.bounds(node)
            label = tree.label(node)
            if bounds is None or "/件" not in label or "¥" not in label:
                continue
            if abs(bounds.top - label_bounds.top) > 12:
                continue
            price = signed_price(label)
            if price is not None:
                candidates.append((bounds.left, price))
        if candidates:
            return max(candidates, key=lambda item: item[0])[1]

        row_nodes = [
            node
            for node in tree.nodes()
            if (bounds := tree.bounds(node)) is not None
            and bounds.left > label_bounds.right
            and abs(bounds.top - label_bounds.top) <= 12
        ]
        row_label = " ".join(tree.label(node) for node in row_nodes if tree.label(node))
        if "/件" in row_label and ("¥" in row_label or "￥" in row_label):
            return signed_price(row_label)
        return None

    @staticmethod
    def _price_input(tree: UiTree) -> ET.Element | None:
        inputs: list[tuple[int, ET.Element]] = []
        for node in tree.nodes():
            if node.get("class") != "android.widget.EditText":
                continue
            bounds = tree.bounds(node)
            if bounds is not None:
                inputs.append((bounds.right - bounds.left, node))
        return max(inputs, default=(0, None), key=lambda item: item[0])[1]

    @classmethod
    def _entered_price(cls, tree: UiTree) -> Decimal | None:
        node = cls._price_input(tree)
        return parse_price(tree.text(node)) if node is not None else None

    def _slow_type_price(
        self,
        tree: UiTree,
        sku: str,
        size: str,
        value: str,
    ) -> UiTree:
        current = tree
        input_node = self._price_input(current)
        bounds = current.bounds(input_node) if input_node is not None else None
        if bounds is None:
            return current
        self.adb.tap_bounds(bounds)
        self.adb.clear_focused_text(count=16)
        current = self.adb.dump_ui(f"dewu-income-{sku}-{size}-cleared", delay=0.4)

        expected = ""
        for index, char in enumerate(value):
            input_node = self._price_input(current)
            bounds = current.bounds(input_node) if input_node is not None else None
            if bounds is None:
                break
            self.adb.tap_bounds(bounds)
            self.adb.shell("input", "keyevent", "123")
            keycode = "56" if char == "." else str(7 + int(char))
            self.adb.shell("input", "keyevent", keycode)
            expected += char
            current = self.adb.dump_ui(
                f"dewu-income-{sku}-{size}-typed-{index + 1}", delay=0.25
            )
            actual_text = current.text(self._price_input(current)) if self._price_input(current) is not None else ""
            if actual_text != expected:
                break
        return current

    def _enter_and_validate_price(
        self,
        form: UiTree,
        sku: str,
        size: str,
        recent_sale_price: Decimal,
    ) -> UiTree | None:
        trial_price = recent_sale_price.to_integral_value(rounding=ROUND_DOWN)
        value = format(trial_price, "f")
        input_node = self._price_input(form)
        bounds = form.bounds(input_node) if input_node is not None else None
        if bounds is None:
            return None

        self.adb.tap_bounds(bounds)
        self.adb.clear_focused_text(count=16)
        self.adb.type_ascii(value)
        calculated = self.adb.dump_ui(f"dewu-income-{sku}-{size}-calculated", delay=1.0)
        if self._entered_price(calculated) != trial_price:
            self.log.warning(
                "%s 规格%s：快速输入 ¥%s 后输入框值为 %s，改用逐位输入重试",
                sku, size, trial_price, self._entered_price(calculated),
            )
            calculated = self._slow_type_price(calculated, sku, size, value)
        if self._entered_price(calculated) != trial_price:
            self.log.warning(
                "%s 规格%s：出价输入校验失败，目标 ¥%s，实际 %s",
                sku, size, trial_price, self._entered_price(calculated),
            )
            return None
        self.adb.shell("input", "keyevent", "66")
        return self.adb.dump_ui(f"dewu-income-{sku}-{size}-validated", delay=1.0)

    def _return_to_quote(self, sku: str, size: str) -> UiTree | None:
        self.adb.back()
        tree = self.adb.dump_ui(f"dewu-income-{sku}-{size}-back", delay=0.8)
        if "最低出售价" not in tree.page_text():
            self.adb.back()
            tree = self.adb.dump_ui(f"dewu-income-{sku}-{size}-back-2", delay=0.8)
        return tree if "最低出售价" in tree.page_text() else None

    def _estimate_one_income(
        self,
        quote: UiTree,
        sku: str,
        size: str,
        recent_sale_price: Decimal,
    ) -> tuple[Decimal | None, UiTree | None]:
        quote, cell = self._visible_size_cell(quote, sku, size)
        cell_bounds = quote.bounds(cell) if cell is not None else None
        if cell_bounds is None:
            self.log.warning("%s 规格%s：滚动规格列表后仍未找到可点击项", sku, size)
            return None, quote
        self._tap_spec_cell(cell_bounds)
        selected = self.adb.dump_ui(f"dewu-income-{sku}-{size}-selected", delay=0.5)
        go_quote, selected_count = self._offer_action(selected)
        if selected_count == 0:
            # Some long lists settle a few pixels after the first dump, which
            # can make the fixed footer receive the tap. Retry at a safe point.
            selected = self.adb.dump_ui(
                f"dewu-income-{sku}-{size}-selected-settled", delay=0.8
            )
            go_quote, selected_count = self._offer_action(selected)
        if selected_count == 0:
            selected, retry_cell = self._visible_size_cell(selected, sku, size)
            retry_bounds = selected.bounds(retry_cell) if retry_cell is not None else None
            if retry_bounds is not None:
                self._tap_spec_cell(retry_bounds)
                selected = self.adb.dump_ui(
                    f"dewu-income-{sku}-{size}-selected-retry", delay=0.7
                )
                go_quote, selected_count = self._offer_action(selected)

        if selected_count != 1:
            if selected_count > 0:
                self._deselect_size(selected, sku, size)
            self.log.warning(
                "%s 规格%s：选择后已选规格数为 %s，未进入单规格出价",
                sku,
                size,
                selected_count,
            )
            return None, None if selected_count > 1 else selected
        go_bounds = selected.bounds(go_quote) if go_quote is not None else None
        if go_bounds is None:
            self.log.warning("%s 规格%s：选择后没有出现单规格“去出价”", sku, size)
            return None, self._deselect_size(selected, sku, size)
        self.adb.tap_bounds(go_bounds)
        form = self.adb.dump_ui(f"dewu-income-{sku}-{size}-form", delay=1.5)
        form = self._dismiss_income_interstitials(form, sku, size)
        if "现货出售" not in form.page_text():
            self.log.warning("%s 规格%s：未进入现货出售试算页", sku, size)
            return None, self._restore_quote_and_deselect(sku, size)

        price_input = self._price_input(form)
        input_bounds = form.bounds(price_input) if price_input is not None else None
        if input_bounds is None:
            self.log.warning("%s 规格%s：现货出售页没有找到出售价输入框", sku, size)
            return None, self._restore_quote_and_deselect(sku, size)

        calculated = self._enter_and_validate_price(
            form, sku, size, recent_sale_price
        )
        if calculated is None:
            return None, self._restore_quote_and_deselect(sku, size)
        income = self._estimated_income(calculated)
        if income is None:
            self.log.warning(
                "%s 规格%s：按最近成交价 ¥%s 试算后未读取到预计收入",
                sku, size, recent_sale_price,
            )
        quote_after = self._restore_quote_and_deselect(sku, size)
        return income, quote_after

    def _estimate_incomes(
        self,
        quote: UiTree,
        sku: str,
        evaluation_sale_prices: dict[str, Decimal],
    ) -> dict[str, Decimal]:
        estimates: dict[str, Decimal] = {}
        current_quote: UiTree | None = quote
        for size, sale_price in evaluation_sale_prices.items():
            if current_quote is None:
                break
            income, current_quote = self._estimate_one_income(
                current_quote, sku, size, sale_price
            )
            if income is not None:
                estimates[size] = income
                self.log.info(
                    "%s 规格%s：保守试算价 ¥%s，得物预计收入 ¥%s",
                    sku, size, sale_price, income,
                )
        return estimates

    def _read_recent_sales(
        self,
        quote: UiTree,
        sku: str,
        target_sizes: set[str] | None = None,
        max_age_days: int = 7,
    ) -> list[RecentSale]:
        if target_sizes is not None and not target_sizes:
            return []
        recent_node = next((node for node in quote.nodes() if quote.text(node) == "最近出售"), None)
        if recent_node is None:
            return []
        container = quote.nearest_clickable(recent_node) or recent_node
        container_bounds = quote.bounds(container)
        if container_bounds is None:
            return []

        self.adb.tap_bounds(container_bounds)
        sales_tree = self.adb.dump_ui(f"dewu-sell-recent-{sku}", delay=1.5)
        if not sales_tree.find(resource_id=f"{DEWU_PACKAGE}:id/tvPurchaseCount"):
            if not sales_tree.find(resource_id=f"{DEWU_PACKAGE}:id/tvRecentDate"):
                self.adb.back()
                return []

        sales: list[RecentSale] = []
        seen: set[tuple[str, str, Decimal, str]] = set()
        try:
            for page in range(5):
                page_sales, _ = self._parse_recent_sales_page(sales_tree, today_only=False)
                added = 0
                for sale in page_sales:
                    key = (sale.sold_at, sale.size, sale.price, sale.buyer)
                    if key not in seen:
                        seen.add(key)
                        sales.append(sale)
                        added += 1
                if not page_sales or (page > 0 and added == 0):
                    break
                page_ages = [
                    self._relative_sale_age(sale.sold_at, datetime.now().astimezone())
                    for sale in page_sales
                ]
                if (
                    target_sizes
                    and target_sizes <= {sale.size for sale in sales}
                    and page_ages
                    and all(
                        age is not None and age > timedelta(days=max_age_days)
                        for age in page_ages
                    )
                ):
                    break
                self.adb.swipe(760, 1450, 760, 900, 400)
                sales_tree = self.adb.dump_ui(f"dewu-sell-recent-{sku}-{page + 2}", delay=0.8)
        finally:
            close = sales_tree.find(resource_id=f"{DEWU_PACKAGE}:id/closeIcon")
            close_bounds = sales_tree.bounds(close[0]) if close else None
            if close_bounds:
                self.adb.tap_bounds(close_bounds)
            else:
                self.adb.back()
            time.sleep(0.5)
        return sales

    @staticmethod
    def _parse_recent_sales_page(
        tree: UiTree,
        today_only: bool = True,
    ) -> tuple[list[RecentSale], bool]:
        now = datetime.now().astimezone()
        rows: list[RecentSale] = []
        reached_previous_day = False
        nodes = tree.nodes()
        for date_node in nodes:
            if date_node.get("resource-id") != f"{DEWU_PACKAGE}:id/tvRecentDate":
                continue
            date_bounds = tree.bounds(date_node)
            sold_at = tree.text(date_node)
            if date_bounds is None or not sold_at:
                continue
            is_today = Dewu._relative_date_is_today(sold_at, now)
            if not is_today:
                reached_previous_day = True
                if today_only:
                    continue
            row_nodes = [
                node for node in nodes
                if (bounds := tree.bounds(node)) is not None
                and abs(bounds.top - date_bounds.top) <= 5
            ]
            size_text = next(
                (tree.text(node) for node in row_nodes if node.get("resource-id") == f"{DEWU_PACKAGE}:id/tvRecentSize"),
                "",
            )
            price_text = next(
                (tree.text(node) for node in row_nodes if node.get("resource-id") == f"{DEWU_PACKAGE}:id/tvRecentPrice"),
                "",
            )
            buyer = next(
                (tree.text(node) for node in row_nodes if node.get("resource-id") == f"{DEWU_PACKAGE}:id/tvRecentName"),
                "",
            )
            parts = size_text.split()
            size = next(
                (normalized for part in reversed(parts) if (normalized := normalize_size(part))),
                None,
            )
            if size is None and parts:
                tail = normalize_spec(parts[-1])
                if tail and (
                    re.fullmatch(r"(?:X{0,3}[SLM]|\d+(?:\.\d+)?(?:ML|L|G|KG|GB|TB))", tail)
                    or len(parts) == 1
                ):
                    size = tail
            if size is None:
                size = normalize_spec(size_text)
            price = parse_price(price_text)
            if size and price is not None:
                rows.append(RecentSale(size=size, price=price, sold_at=sold_at, buyer=buyer))
        return rows, reached_previous_day

    @staticmethod
    def _relative_date_is_today(label: str, now: datetime) -> bool:
        if "刚刚" in label or "分钟前" in label or label.startswith("今天"):
            return True
        hours = re.search(r"(\d+)小时前", label)
        if hours:
            return (now - timedelta(hours=int(hours.group(1)))).date() == now.date()
        return False

    @staticmethod
    def _relative_sale_age(label: str, now: datetime) -> timedelta | None:
        value = label.strip()
        if "刚刚" in value:
            return timedelta(0)
        minutes = re.search(r"(\d+)分钟前", value)
        if minutes:
            return timedelta(minutes=int(minutes.group(1)))
        hours = re.search(r"(\d+)小时前", value)
        if hours:
            return timedelta(hours=int(hours.group(1)))
        if value.startswith("今天"):
            return timedelta(0)
        if "前天" in value:
            return timedelta(days=2)
        if "昨天" in value:
            return timedelta(days=1)
        days = re.search(r"(\d+)天前", value)
        if days:
            return timedelta(days=int(days.group(1)))
        weeks = re.search(r"(\d+)周前", value)
        if weeks:
            return timedelta(days=7 * int(weeks.group(1)))
        if "一周前" in value:
            return timedelta(days=7)
        months = re.search(r"(\d+)个月前", value)
        if months:
            return timedelta(days=30 * int(months.group(1)))

        normalized = value.replace("年", "-").replace("月", "-").replace("日", "")
        normalized = normalized.replace("/", "-").replace(".", "-")
        parsed: datetime | None = None
        for date_format in ("%Y-%m-%d", "%m-%d"):
            try:
                parsed = datetime.strptime(normalized, date_format).replace(tzinfo=now.tzinfo)
                if date_format == "%m-%d":
                    parsed = parsed.replace(year=now.year)
                    if parsed.date() > now.date():
                        parsed = parsed.replace(year=now.year - 1)
                break
            except ValueError:
                continue
        if parsed is None:
            return None
        return timedelta(days=max((now.date() - parsed.date()).days, 0))

    @classmethod
    def _recent_sale_is_fresh(
        cls,
        sale: RecentSale,
        now: datetime,
        max_age_days: int = 7,
    ) -> bool:
        age = cls._relative_sale_age(sale.sold_at, now)
        return age is not None and age <= timedelta(days=max_age_days)

    @staticmethod
    def _median_price(prices: list[Decimal]) -> Decimal:
        ordered = sorted(prices)
        middle = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / Decimal("2")

    @classmethod
    def _sales_profile(
        cls,
        sales: list[RecentSale],
        size: str,
        now: datetime,
        max_age_days: int = 7,
        current_ask: Decimal | None = None,
    ) -> SalesProfile | None:
        samples: list[tuple[RecentSale, timedelta]] = []
        for sale in sales:
            if sale.size != size:
                continue
            age = cls._relative_sale_age(sale.sold_at, now)
            if age is not None and age <= timedelta(days=max_age_days):
                samples.append((sale, age))
        if not samples:
            return None

        samples.sort(key=lambda item: item[1])
        latest_sale, latest_age = samples[0]
        prices = [sale.price for sale, _age in samples]
        median_price = cls._median_price(prices)
        minimum_price = min(prices)
        maximum_price = max(prices)
        active_days = {
            (now - age).date()
            for _sale, age in samples
        }
        ordered_ages = sorted(age.total_seconds() / 3600 for _sale, age in samples)
        gaps = [
            newer - older
            for older, newer in zip(ordered_ages, ordered_ages[1:])
        ]
        max_gap_hours = max(gaps) if gaps else None

        # The recent-sales page is collected newest first. Reverse the sampled
        # sequence to detect a sustained decline from older to newer trades.
        chronological_prices = [sale.price for sale, _age in reversed(samples)]
        recent_three = chronological_prices[-3:]
        falling = (
            len(recent_three) == 3
            and all(newer < older for older, newer in zip(recent_three, recent_three[1:]))
            and (recent_three[0] - recent_three[-1]) / recent_three[0] >= Decimal("0.03")
        )
        trend = "falling" if falling else ("stable" if len(prices) >= 3 else "insufficient")
        conservative_candidates = [latest_sale.price, median_price]
        if current_ask is not None:
            conservative_candidates.append(current_ask)
        price_range_percent = (
            (maximum_price - minimum_price) / median_price * Decimal("100")
            if median_price > 0
            else Decimal("0")
        )
        return SalesProfile(
            observed_sales_7d=len(samples),
            active_days_7d=len(active_days),
            latest_age_hours=round(latest_age.total_seconds() / 3600, 1),
            max_gap_hours=round(max_gap_hours, 1) if max_gap_hours is not None else None,
            latest_price=latest_sale.price,
            median_price=median_price,
            minimum_price=minimum_price,
            maximum_price=maximum_price,
            conservative_price=min(conservative_candidates),
            price_range_percent=price_range_percent.quantize(Decimal("0.1")),
            price_trend=trend,
        )

    @staticmethod
    def _parse_size_prices(tree: UiTree) -> dict[str, Decimal]:
        result: dict[str, Decimal] = {}
        for node in tree.nodes():
            description = tree.desc(node)
            match = re.fullmatch(r"\s*(.+?)\s*[,，]\s*[¥￥]\s*(--|\d+(?:\.\d+)?)\s*", description)
            if not match or match.group(2) == "--":
                continue
            size = normalize_spec(match.group(1))
            price = parse_price(f"¥{match.group(2)}")
            if size and price is not None:
                result[size] = price
        if result:
            return result

        sizes: list[tuple[int, int, str]] = []
        prices: list[tuple[int, int, Decimal]] = []
        for node in tree.nodes():
            bounds = tree.bounds(node)
            if not bounds:
                continue
            if node.get("resource-id") == f"{DEWU_PACKAGE}:id/tvName":
                size = normalize_spec(tree.text(node))
                if size:
                    sizes.append((bounds.left, bounds.top, size))
            elif node.get("resource-id") == f"{DEWU_PACKAGE}:id/tvPrice":
                price = parse_price(tree.text(node))
                if price is not None and "--" not in tree.text(node):
                    prices.append((bounds.left, bounds.top, price))
        for left, top, size in sizes:
            cell_prices = [
                (price_top - top, price)
                for price_left, price_top, price in prices
                if price_left == left and 0 < price_top - top < 80
            ]
            if cell_prices:
                result[size] = min(cell_prices, key=lambda item: item[0])[1]
        return result
