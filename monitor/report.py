from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from .models import Opportunity, RecentSale, price_trend_label


CATEGORY_RULES = (
    ("鞋子", ("鞋", "拖鞋", "凉鞋", "靴", "SNEAKER")),
    ("衣服", ("衣", "裤", "裙", "外套", "夹克", "卫衣", "衬衫", "T恤", "短袖", "羽绒", "内衣")),
    ("化妆品", ("口红", "粉底", "面膜", "香水", "护肤", "精华", "乳液", "彩妆", "洁面", "眼影")),
    ("数码", ("手机", "耳机", "相机", "电脑", "平板", "键盘", "鼠标", "无人机", "智能手环", "充电")),
    ("箱包", ("包", "背包", "挎包", "手提包", "行李箱")),
    ("配饰", ("项链", "戒指", "手链", "手镯", "帽", "眼镜", "腕表", "手表")),
    ("潮玩", ("手办", "玩偶", "积木", "模型", "公仔", "盲盒")),
)

HEADERS = (
    "报警时间",
    "货物品类",
    "商品名称",
    "货号",
    "匹配规格",
    "拼多多价格",
    "得物同规格最近成交价",
    "得物预计收入",
    "预计利润",
    "最近成交记录",
    "购买方式",
)

QUALITY_HEADERS = (
    "报警等级",
    "7天同规格观察成交",
    "7天活跃天数",
    "最近成交距今(小时)",
    "最大成交间隔(小时)",
    "成交价区间",
    "价格波动",
    "价格趋势",
    "保守试算价",
)
HEADERS = HEADERS + QUALITY_HEADERS
COLUMN_WIDTHS = (20, 12, 42, 20, 12, 15, 20, 18, 15, 48, 48) + (
    10, 18, 14, 20, 20, 22, 14, 14, 16,
)
CURRENCY_FORMAT = '¥#,##0.00'


def classify_product(title: str) -> str:
    normalized = title.upper()
    for category, keywords in CATEGORY_RULES:
        if any(keyword.upper() in normalized for keyword in keywords):
            return category
    return "其他"


class SessionReport:
    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d_%H-%M-%S")
        path = directory / f"{timestamp}.xlsx"
        suffix = 2
        while path.exists():
            path = directory / f"{timestamp}_{suffix}.xlsx"
            suffix += 1

        self.path = path
        self._base_path = path
        self._continuation_index = 2
        self.workbook = Workbook()
        self.sheet = self.workbook.active
        self.sheet.title = "价差报警"
        self._configure_sheet(self.sheet)
        self.workbook.properties.title = "拼多多与得物价差报警记录"
        self.workbook.properties.subject = "仅记录同货号同规格且预计收入减拼多多成本达到阈值的商品"
        self.workbook.save(self.path)

    def _save(self) -> None:
        try:
            self.workbook.save(self.path)
            return
        except PermissionError:
            pass

        while True:
            continuation = self._base_path.with_name(
                f"{self._base_path.stem}_续写{self._continuation_index}.xlsx"
            )
            self._continuation_index += 1
            if not continuation.exists():
                break
        self.workbook.save(continuation)
        self.path = continuation

    @staticmethod
    def _configure_sheet(sheet: Worksheet) -> None:
        sheet.append(HEADERS)
        header_fill = PatternFill("solid", fgColor="1F4E78")
        for cell in sheet[1]:
            cell.font = Font(color="FFFFFF", bold=True)
            cell.fill = header_fill
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        sheet.row_dimensions[1].height = 32
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = f"A1:{get_column_letter(len(HEADERS))}1"
        for index, width in enumerate(COLUMN_WIDTHS, start=1):
            sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = width

    @staticmethod
    def _recent_sales_text(recent_sales: list[RecentSale]) -> str:
        if not recent_sales:
            return "暂无可见成交记录"
        return "\n".join(
            f"{sale.sold_at} | 规格 {sale.size} | 成交价 ¥{sale.price}"
            for sale in recent_sales
        )

    def append(
        self,
        opportunity: Opportunity,
        category: str,
        recent_sales: list[RecentSale],
    ) -> None:
        profile = opportunity.sales_profile
        self.sheet.append(
            (
                datetime.now().astimezone().replace(tzinfo=None),
                category,
                opportunity.title,
                opportunity.sku,
                opportunity.size,
                float(opportunity.pdd_price),
                float(opportunity.recent_sale_price) if opportunity.recent_sale_price is not None else None,
                float(opportunity.estimated_income),
                float(opportunity.spread),
                self._recent_sales_text(recent_sales),
                "拼多多搜索货号并选择上述匹配规格，付款前复核价格",
                "A",
                profile.observed_sales_7d if profile else 0,
                profile.active_days_7d if profile else 0,
                profile.latest_age_hours if profile else None,
                profile.max_gap_hours if profile else None,
                (
                    f"¥{profile.minimum_price}-¥{profile.maximum_price}"
                    if profile else ""
                ),
                f"{profile.price_range_percent}%" if profile else "",
                price_trend_label(profile.price_trend) if profile else "样本不足",
                float(opportunity.evaluation_sale_price)
                if opportunity.evaluation_sale_price is not None else None,
            )
        )
        row = self.sheet.max_row
        self.sheet.cell(row=row, column=1).number_format = "yyyy-mm-dd hh:mm:ss"
        for column in (6, 7, 8, 9):
            self.sheet.cell(row=row, column=column).number_format = CURRENCY_FORMAT
        for cell in self.sheet[row]:
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        self.sheet.cell(row=row, column=len(HEADERS)).number_format = CURRENCY_FORMAT
        self.sheet.auto_filter.ref = f"A1:{get_column_letter(len(HEADERS))}{row}"
        self._save()
