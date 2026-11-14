from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation


TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9][A-Za-z0-9-]{4,28})(?![A-Za-z0-9])")
PRICE_RE = re.compile(r"([0-9][0-9\s]*(?:\.[0-9]{1,2})?)")
SIZE_RE = re.compile(r"^(?:EU)?\s*(3[0-9]|4[0-9]|5[0-2])(?:\.5)?(?:码)?$", re.I)
MEASUREMENT_RE = re.compile(
    r"^\d+(?:\.\d+)?(?:ML|L|G|KG|MG|MM|CM|M|OZ|GB|TB|MB|W|V|HZ)\d*$",
    re.I,
)
DIMENSION_RE = re.compile(r"^\d+(?:X|\*)\d+(?:(?:X|\*)\d+)?$", re.I)

SKU_STOPWORDS = {
    "AIRFORCE1",
    "ANDROID15",
    "IPHONE16",
    "IPHONE17",
}

EDITION_MARKER_GROUPS = {
    "custom": ("定制", "订制", "客制", "手绘", "改造款", "DIY"),
    "collectible": ("收藏版", "收藏款", "典藏版", "典藏款", "珍藏版", "珍藏款", "收藏级"),
    "limited": ("限量版", "限量款", "限定版", "限定款", "特别版", "特殊版", "纪念版"),
    "collaboration": ("联名版", "联名款", "联名"),
    "gift_box": ("礼盒版", "礼盒款", "礼盒"),
    "player": ("球员版",),
    "replica": ("复刻版", "复刻款", "复刻"),
}

EXCLUSIVE_VARIANT_GROUPS = (
    {
        "low_top": ("低帮", "LOW TOP"),
        "mid_top": ("中帮", "MID TOP"),
        "high_top": ("高帮", "HIGH TOP"),
    },
    {
        "men": ("男款", "男子"),
        "women": ("女款", "女子"),
        "kids": ("儿童", "大童", "小童"),
    },
)

BRAND_ALIASES = {
    "NIKE": ("NIKE", "耐克"),
    "JORDAN": ("JORDAN", "乔丹"),
    "ADIDAS": ("ADIDAS", "阿迪达斯", "三叶草"),
    "CONVERSE": ("CONVERSE", "匡威"),
    "PUMA": ("PUMA", "彪马"),
    "ASICS": ("ASICS", "亚瑟士"),
    "NEW BALANCE": ("NEW BALANCE", "新百伦"),
    "LI-NING": ("LI-NING", "李宁"),
    "ANTA": ("ANTA", "安踏"),
    "FILA": ("FILA", "斐乐"),
    "VANS": ("VANS", "范斯"),
}

PRODUCT_KIND_KEYWORDS = {
    "shoes": ("鞋", "靴"),
    "bags": ("包", "背包", "双肩包", "挎包", "行李箱"),
    "clothing": ("衣", "裤", "裙", "夹克", "外套", "卫衣", "T恤", "短袖"),
    "beauty": ("香水", "口红", "粉底", "面膜", "精华", "乳液", "护肤", "彩妆"),
    "digital": ("手机", "耳机", "鼠标", "键盘", "相机", "电脑", "平板", "手表", "手环"),
}

MODEL_STOPWORDS = {
    "NIKE", "ADIDAS", "ORIGINALS", "CONVERSE", "PUMA", "ASICS", "JORDAN",
    "OFFICIAL", "SPORT", "SPORTS", "SHOES", "MEN", "WOMEN", "UNISEX",
}


def normalize_sku(value: str) -> str:
    return re.sub(r"[\s‐‑‒–—−]", lambda m: "-" if m.group(0) != " " else "", value).upper()


def extract_skus(text: str) -> list[str]:
    found: list[str] = []
    for match in TOKEN_RE.finditer(text):
        token = normalize_sku(match.group(1)).strip("-")
        letters = sum(char.isalpha() for char in token)
        digits = sum(char.isdigit() for char in token)
        is_barcode = letters == 0 and 8 <= digits <= 14
        is_measurement = bool(MEASUREMENT_RE.fullmatch(token)) or bool(DIMENSION_RE.fullmatch(token))
        if (letters < 1 and not is_barcode) or digits < 2 or is_measurement or token in SKU_STOPWORDS:
            continue
        if token not in found:
            found.append(token)
    return found


def contains_exact_sku(text: str, sku: str) -> bool:
    wanted = normalize_sku(sku).replace("-", "")
    return any(candidate.replace("-", "") == wanted for candidate in extract_skus(text))


def edition_signature(text: str) -> frozenset[str]:
    normalized = text.upper()
    return frozenset(
        group
        for group, markers in EDITION_MARKER_GROUPS.items()
        if any(marker.upper() in normalized for marker in markers)
    )


def versions_compatible(source_title: str, reference_title: str) -> bool:
    if edition_signature(source_title) != edition_signature(reference_title):
        return False
    source = source_title.upper()
    reference = reference_title.upper()
    for variants in EXCLUSIVE_VARIANT_GROUPS:
        source_values = {
            name for name, markers in variants.items()
            if any(marker in source for marker in markers)
        }
        reference_values = {
            name for name, markers in variants.items()
            if any(marker in reference for marker in markers)
        }
        if source_values and reference_values and source_values.isdisjoint(reference_values):
            return False
    return True


def _brands(text: str) -> set[str]:
    normalized = text.upper()
    return {
        brand
        for brand, aliases in BRAND_ALIASES.items()
        if any(alias.upper() in normalized for alias in aliases)
    }


def _product_kinds(text: str) -> set[str]:
    normalized = text.upper()
    return {
        kind
        for kind, keywords in PRODUCT_KIND_KEYWORDS.items()
        if any(keyword.upper() in normalized for keyword in keywords)
    }


def _model_terms(text: str) -> set[str]:
    normalized = re.sub(r"[‐‑‒–—−]", "-", text.upper())
    terms = set(re.findall(r"[A-Z][A-Z0-9-]{2,}", normalized))
    terms.update(re.findall(r"[A-Z]{3,}", normalized))
    return {term.strip("-") for term in terms if term.strip("-") not in MODEL_STOPWORDS}


def titles_compatible_without_sku(source_title: str, reference_title: str) -> bool:
    if not source_title or not reference_title or not versions_compatible(source_title, reference_title):
        return False
    source_brands = _brands(source_title)
    reference_brands = _brands(reference_title)
    if source_brands and reference_brands and source_brands.isdisjoint(reference_brands):
        return False
    source_kinds = _product_kinds(source_title)
    reference_kinds = _product_kinds(reference_title)
    if source_kinds and reference_kinds and source_kinds.isdisjoint(reference_kinds):
        return False
    if _model_terms(source_title) & _model_terms(reference_title):
        return True
    return bool(source_brands & reference_brands and source_kinds & reference_kinds)


def parse_price(text: str) -> Decimal | None:
    value = text.replace(",", "")
    symbol_positions = [position for symbol in ("¥", "￥") if (position := value.find(symbol)) >= 0]
    if symbol_positions:
        value = value[min(symbol_positions) + 1:]
    match = PRICE_RE.search(value)
    if not match:
        return None
    try:
        return Decimal(match.group(1).replace(" ", ""))
    except InvalidOperation:
        return None


def normalize_size(text: str) -> str | None:
    value = text.strip().upper().replace("码", "").replace("EU", "").strip()
    # Adidas uses one-third sizing while many Chinese listings round the same
    # size run to integers and half sizes (for example 36⅔ is shown as 36.5).
    value = re.sub(r"\s*2\s*/\s*3", "⅔", value)
    value = re.sub(r"\s*1\s*/\s*3", "⅓", value)
    value = value.replace("⅔", ".5").replace("⅓", "")
    if not SIZE_RE.match(value):
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    if number == number.to_integral_value():
        return str(int(number))
    return format(number.normalize(), "f")


SPEC_STOPWORDS = {
    "请选择", "请选择规格", "选择规格", "添加规格", "查看已选规格", "全选",
    "颜色", "颜色分类", "尺码", "鞋码", "规格", "容量", "净含量",
    "减少数量", "增加数量", "关闭", "商品主图", "免费服务",
}


def normalize_spec(text: str) -> str | None:
    size = normalize_size(text)
    if size:
        return size
    value = text.strip().upper()
    value = re.sub(r"^(?:已选|规格|颜色|容量|净含量|尺码|鞋码)\s*[：:]\s*", "", value)
    value = (
        value.replace("毫升", "ML")
        .replace("升", "L")
        .replace("千克", "KG")
        .replace("公斤", "KG")
        .replace("克", "G")
    )
    value = re.sub(r"[\s/|\\：:]+", "", value)
    value = value.strip("，,；;")
    if not value or value in SPEC_STOPWORDS or "¥" in value or "￥" in value:
        return None
    if any(word in value for word in ("提交订单", "现在买", "单独购买", "支付", "售罄", "无货")):
        return None
    if len(value) > 48 or not re.search(r"[A-Z0-9\u4e00-\u9fff]", value):
        return None
    return value


def spec_sort_key(value: str) -> tuple[int, Decimal | str]:
    try:
        return (0, Decimal(value))
    except InvalidOperation:
        return (1, value)
