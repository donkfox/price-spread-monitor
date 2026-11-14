from __future__ import annotations

import tempfile
import unittest
import xml.etree.ElementTree as ET
import json
import sqlite3
import subprocess
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import URLError
from urllib.parse import unquote_plus

from openpyxl import load_workbook

from monitor.adb import Adb, AdbError, AuthenticationRequired
from monitor.app_data import AlertRecord, AppRepository
from monitor.desktop import MonitorDesktop, TaskController
from monitor.main import acquire_single_instance, best_opportunity, matching_opportunities
from monitor.matching import (
    contains_exact_sku,
    edition_signature,
    extract_skus,
    normalize_spec,
    normalize_size,
    parse_price,
    titles_compatible_without_sku,
    versions_compatible,
)
from monitor.models import Opportunity, RecentSale, SalesProfile, VerifiedProduct
from monitor.notify import (
    NtfyConfig,
    NtfyNotifier,
    PushDeerConfig,
    PushDeerNotifier,
    WeComBotConfig,
    WeComBotNotifier,
)
from monitor.platforms import Dewu, Pinduoduo, _pdd_exclusion_reason
from monitor.quality import assess_opportunity
from monitor.report import CURRENCY_FORMAT, HEADERS, SessionReport, classify_product
from monitor.storage import Storage
from monitor.ui import UiTree


class MatchingTests(unittest.TestCase):
    def test_normalizes_generic_product_specifications(self) -> None:
        self.assertEqual(normalize_spec("黑色"), "黑色")
        self.assertEqual(normalize_spec("250毫升"), "250ML")
        self.assertEqual(normalize_spec("M"), "M")
        self.assertEqual(normalize_spec("xl"), "XL")

    def test_extracts_real_model_tokens(self) -> None:
        title = 'Nike AJ1 IW6276-001 多威MT92232A MLB 3ASXCDN3N 2026新款'
        self.assertEqual(extract_skus(title), ["IW6276-001", "MT92232A", "3ASXCDN3N"])

    def test_ignores_measurements_but_keeps_product_codes_and_barcodes(self) -> None:
        title = "精华液 250ml 旅行装 500ML 型号 KZ1234 条码 6901234567890"
        self.assertEqual(extract_skus(title), ["KZ1234", "6901234567890"])

    def test_ignores_dimension_tokens(self) -> None:
        self.assertEqual(extract_skus("收纳盒 250MM0 30x40x20"), [])

    def test_special_edition_must_match_both_platforms(self) -> None:
        pdd = "【5000万人收藏该品牌】Nike P-6000 女款 BV1021-106"
        custom = "【定制球鞋】Nike P-6000 粉灰机械梦 礼盒 女款"
        self.assertEqual(edition_signature(pdd), frozenset())
        self.assertFalse(versions_compatible(pdd, custom))
        self.assertTrue(versions_compatible(pdd, "Nike P-6000 女款 白黑蓝"))
        self.assertFalse(
            versions_compatible(
                "Converse 男女同款低帮帆布鞋 M7652C",
                "Converse High Top 舒适休闲 高帮帆布鞋 男女同款",
            )
        )

    def test_first_result_can_match_without_parameter_sku(self) -> None:
        pdd = "adidas 阿迪达斯女子运动休闲双肩包 JE3224"
        first_result = "adidas 阿迪达斯 20.75L 品牌Logo双肩包 女款 黑色"
        self.assertTrue(titles_compatible_without_sku(pdd, first_result))
        self.assertFalse(
            titles_compatible_without_sku(pdd, "【定制包】adidas 卡通印花双肩包 女款")
        )
        self.assertFalse(
            titles_compatible_without_sku(pdd, "Nike 运动休闲双肩包 女款")
        )

    def test_exact_sku_does_not_accept_prefix(self) -> None:
        self.assertTrue(contains_exact_sku("主货号 MT92232A", "MT92232A"))
        self.assertTrue(contains_exact_sku("主货号 211702-100", "211702100"))
        self.assertFalse(contains_exact_sku("主货号 MT92232AB", "MT92232A"))

    def test_price_and_size(self) -> None:
        self.assertEqual(parse_price("限1件 ¥84"), Decimal("84"))
        self.assertEqual(parse_price("¥ 2 5 0.42"), Decimal("250.42"))
        self.assertEqual(normalize_size("42码"), "42")
        self.assertEqual(normalize_size("36⅔"), "36.5")
        self.assertEqual(normalize_size("37⅓"), "37")
        self.assertEqual(normalize_size("38 2/3"), "38.5")
        self.assertIsNone(normalize_size("2026"))


class AdbTests(unittest.TestCase):
    @patch("monitor.adb.time.sleep")
    @patch("monitor.adb.subprocess.run")
    def test_launch_timeout_is_treated_as_pending_ui_check(
        self,
        run: MagicMock,
        _sleep: MagicMock,
    ) -> None:
        run.side_effect = [
            subprocess.TimeoutExpired(["adb.exe", "shell", "am", "start"], 10),
            subprocess.TimeoutExpired(["adb.exe", "shell", "am", "start"], 10),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            adb = Adb(Path("adb.exe"), "127.0.0.1:7555", Path(temp_dir))
            with patch.object(adb, "_reconnect_after_timeout") as recover:
                adb.launch(
                    "com.xunmeng.pinduoduo",
                    component="com.xunmeng.pinduoduo/.ui.activity.MainFrameActivity",
                )

        self.assertEqual(run.call_count, 2)
        recover.assert_called_once_with()
        self.assertIn("am", run.call_args_list[0].args[0])
        self.assertNotIn("monkey", run.call_args_list[0].args[0])

    @patch("monitor.adb.time.sleep")
    @patch("monitor.adb.subprocess.run")
    def test_timeout_recovery_restarts_adb_server(
        self,
        run: MagicMock,
        _sleep: MagicMock,
    ) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        with tempfile.TemporaryDirectory() as temp_dir:
            adb = Adb(Path("adb.exe"), "127.0.0.1:7555", Path(temp_dir))
            adb._reconnect_after_timeout()

        commands = [call.args[0] for call in run.call_args_list]
        self.assertEqual(commands[0], ["adb.exe", "kill-server"])
        self.assertEqual(commands[1], ["adb.exe", "start-server"])
        self.assertEqual(commands[2], ["adb.exe", "connect", "127.0.0.1:7555"])

    @patch("monitor.adb.time.sleep")
    @patch("monitor.adb.subprocess.run")
    def test_transient_closed_connection_reconnects_and_retries(
        self,
        run: MagicMock,
        _sleep: MagicMock,
    ) -> None:
        run.side_effect = [
            subprocess.CompletedProcess([], 1, "", "error: closed"),
            subprocess.CompletedProcess([], 0, "connected", ""),
            subprocess.CompletedProcess([], 0, "device", ""),
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            adb = Adb(Path("adb.exe"), "127.0.0.1:7555", Path(temp_dir))
            result = adb.run("get-state")

        self.assertEqual(result.stdout, "device")
        self.assertEqual(run.call_count, 3)
        self.assertEqual(
            run.call_args_list[1].args[0],
            ["adb.exe", "connect", "127.0.0.1:7555"],
        )

    @patch("monitor.adb.time.sleep")
    def test_idle_state_failure_uses_fast_dump_helper(self, _sleep: MagicMock) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            runtime = project / "runtime"
            helper = project / "tools" / "fast-ui-dump" / "fast-ui-dump.jar"
            helper.parent.mkdir(parents=True)
            helper.write_bytes(b"helper jar" * 20)
            adb = Adb(Path("adb.exe"), "127.0.0.1:7555", runtime)

            def fake_run(*args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
                if args[:3] == ("shell", "uiautomator", "dump"):
                    return subprocess.CompletedProcess(
                        [], 139, "", "ERROR: could not get idle state."
                    )
                if args and args[0] == "pull":
                    Path(args[2]).write_text(
                        '<hierarchy><node text="ok" bounds="[0,0][1,1]"/></hierarchy>',
                        encoding="utf-8",
                    )
                return subprocess.CompletedProcess([], 0, "OK (1 test)", "")

            adb.run = MagicMock(side_effect=fake_run)
            tree = adb.dump_ui("idle-fallback")

        self.assertIn("ok", tree.page_text())
        adb.run.assert_any_call(
            "push", str(helper), Adb.FAST_DUMP_REMOTE_JAR, timeout=20
        )
        adb.run.assert_any_call(
            "shell", "uiautomator", "runtest", Adb.FAST_DUMP_REMOTE_JAR,
            "-c", "com.pricemonitor.FastDump", timeout=15, check=False,
        )

    @patch("monitor.adb.time.sleep")
    def test_fast_dump_retries_when_runner_has_not_created_xml(
        self,
        sleep: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            project = Path(temp_dir)
            runtime = project / "runtime"
            helper = project / "tools" / "fast-ui-dump" / "fast-ui-dump.jar"
            helper.parent.mkdir(parents=True)
            helper.write_bytes(b"helper jar" * 20)
            adb = Adb(Path("adb.exe"), "127.0.0.1:7555", runtime)
            pulls = 0

            def fake_run(*args: str, **_kwargs: object) -> subprocess.CompletedProcess[str]:
                nonlocal pulls
                if args and args[0] == "pull":
                    pulls += 1
                    if pulls < 3:
                        raise AdbError("remote XML does not exist yet")
                    Path(args[2]).write_text(
                        '<hierarchy><node text="recovered" bounds="[0,0][1,1]"/></hierarchy>',
                        encoding="utf-8",
                    )
                return subprocess.CompletedProcess([], 0, "OK (1 test)", "")

            adb.run = MagicMock(side_effect=fake_run)
            tree = adb._dump_ui_without_idle_wait(runtime / "delayed.xml")

        self.assertEqual(pulls, 3)
        self.assertIn("recovered", tree.page_text())
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(
            sum(1 for call in adb.run.call_args_list if call.args[0] == "push"),
            2,
        )


class ParserTests(unittest.TestCase):
    def test_pdd_excludes_explicit_defects_and_skipped_sizes(self) -> None:
        self.assertEqual(_pdd_exclusion_reason("Nike 瑕疵品 DZ2538-100"), "瑕疵")
        self.assertEqual(_pdd_exclusion_reason("Adidas 微 瑕 隔 码 特价"), "隔码、瑕疵")
        self.assertEqual(_pdd_exclusion_reason("Nike B级品 隔码特价"), "隔码、B品")
        self.assertEqual(_pdd_exclusion_reason("商品无瑕疵，原盒发货"), None)
        self.assertIsNone(_pdd_exclusion_reason("卖家明确说明非B品，原盒发货"))
        self.assertIsNone(_pdd_exclusion_reason("Nike 正品现货 DZ2538-100"))

    def test_pdd_rejects_bonded_and_cross_border_fulfilment(self) -> None:
        bonded = UiTree(ET.fromstring(
            '<hierarchy><node text="保税仓发货，跨境商品" /></hierarchy>'
        ))
        ordinary = UiTree(ET.fromstring(
            '<hierarchy><node text="国内现货，正常发货" /></hierarchy>'
        ))
        self.assertTrue(Pinduoduo._is_bonded_warehouse(bonded))
        self.assertFalse(Pinduoduo._is_bonded_warehouse(ordinary))

    def test_dewu_startup_dialogs_are_detected(self) -> None:
        consent_xml = """<hierarchy><node text="同意并继续" clickable="true" bounds="[1,1][10,10]" /></hierarchy>"""
        consent_tree = UiTree(ET.fromstring(consent_xml))
        self.assertEqual(
            consent_tree.text(Dewu._startup_dialog_target(consent_tree)),
            "同意并继续",
        )

        update_xml = """<hierarchy>
          <node text="新版本抢先体验" bounds="[1,1][10,10]" />
          <node resource-id="com.shizhuang.duapp:id/ivClose" clickable="true" bounds="[20,1][30,10]" />
        </hierarchy>"""
        update_tree = UiTree(ET.fromstring(update_xml))
        self.assertEqual(
            Dewu._startup_dialog_target(update_tree).get("resource-id"),
            "com.shizhuang.duapp:id/ivClose",
        )

    def test_dewu_login_expiry_is_not_reported_as_empty_results(self) -> None:
        login_xml = """<hierarchy>
          <node text="手机号登录" bounds="[1,1][10,10]" />
          <node text="验证并登录" bounds="[1,20][10,30]" />
        </hierarchy>"""
        with self.assertRaises(AuthenticationRequired):
            Dewu._require_login(UiTree(ET.fromstring(login_xml)))

    def test_pdd_accepts_unique_detail_sku_when_selector_only_has_size(self) -> None:
        self.assertTrue(
            Pinduoduo._selector_identity_is_unambiguous(
                "HF0013-900", "Nike Dunk HF0013-900", ["40.5"], "已选：40.5"
            )
        )
        self.assertFalse(
            Pinduoduo._selector_identity_is_unambiguous(
                "HF0013-900",
                "Nike Dunk HF0013-900",
                ["40.5"],
                "白色 HF0013-900 黑色 HF0013-100 已选：40.5",
            )
        )
        self.assertTrue(
            Pinduoduo._selector_identity_is_unambiguous(
                "HF0013-900", "Nike Dunk HF0013-900", [], "40.5 无货 41 无货"
            )
        )

    def test_pdd_accepts_target_sku_without_hyphen_in_selector(self) -> None:
        self.assertTrue(
            Pinduoduo._label_matches_target_variant("IM5067010", "IM5067-010")
        )

    def test_pdd_accepts_unique_sku_subvariant(self) -> None:
        self.assertTrue(
            Pinduoduo._label_matches_target_variant(
                "P9200TD-AJ（白/自由紫） ¥202", "P9200TD"
            )
        )
        self.assertFalse(
            Pinduoduo._label_matches_target_variant(
                "P9200TDF-LI（粉红/暖白） ¥204", "P9200TD"
            )
        )

    def test_pdd_selects_only_unique_matching_subvariant(self) -> None:
        selector_xml = """<hierarchy>
          <node text="P9200TD-AJ（白/自由紫）  ¥202" clickable="true" enabled="true" selected="false" bounds="[18,813][403,859]"/>
          <node text="P9200TDF-LI（粉红/暖白）  ¥204" clickable="true" enabled="true" selected="false" bounds="[418,813][752,859]"/>
        </hierarchy>"""
        selected_xml = """<hierarchy>
          <node text="P9200TD-AJ（白/自由紫）  ¥202" clickable="true" enabled="true" selected="true" bounds="[18,813][403,859]"/>
        </hierarchy>"""
        adb = MagicMock()
        selected_tree = UiTree(ET.fromstring(selected_xml))
        adb.dump_ui.return_value = selected_tree
        parser = Pinduoduo(adb, MagicMock())

        result = parser._select_target_variant(
            UiTree(ET.fromstring(selector_xml)), "P9200TD"
        )

        self.assertIs(result, selected_tree)
        adb.tap_bounds.assert_called_once()

    def test_pdd_card_uses_full_content_description(self) -> None:
        xml = """<hierarchy><node clickable="true" bounds="[1,100][300,500]">
          <node text="多威征途2" content-desc="多威征途2跑鞋MT92232A" resource-id="com.xunmeng.pinduoduo:id/tv_title" clickable="false" bounds="[10,300][290,330]" />
          <node text="¥" bounds="[10,350][20,370]"/><node text="84" bounds="[25,350][60,370]"/>
        </node></hierarchy>"""
        tree = UiTree(ET.fromstring(xml))
        parser = Pinduoduo.__new__(Pinduoduo)
        cards = parser.parse_cards(tree)
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].sku, "MT92232A")
        self.assertEqual(cards[0].price, Decimal("84"))
        self.assertEqual(cards[0].bounds.left, 10)
        self.assertEqual(cards[0].bounds.top, 300)
        self.assertEqual(cards[0].bounds.right, 290)
        self.assertEqual(cards[0].bounds.bottom, 330)

    def test_pdd_search_card_accepts_combined_currency_label(self) -> None:
        xml = """<hierarchy><node clickable="true" bounds="[1,100][300,500]">
          <node text="NIKE卫衣" content-desc="NIKE卫衣 DQ5794-010" resource-id="com.xunmeng.pinduoduo:id/tv_title" clickable="false" bounds="[10,300][290,330]" />
          <node content-desc="¥131.87本店已拼1.9万件" bounds="[10,350][290,370]"/>
        </node></hierarchy>"""
        tree = UiTree(ET.fromstring(xml))
        parser = Pinduoduo.__new__(Pinduoduo)

        cards = parser.parse_cards(tree)

        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].sku, "DQ5794-010")
        self.assertEqual(cards[0].price, Decimal("131.87"))

    def test_pdd_spec_option_states_keep_out_of_stock_sizes(self) -> None:
        xml = """<hierarchy>
          <node text="42 无货" clickable="true" enabled="true" bounds="[18,813][160,859]"/>
          <node text="43 ¥199" clickable="true" enabled="true" bounds="[180,813][320,859]"/>
        </hierarchy>"""
        tree = UiTree(ET.fromstring(xml))

        states = {
            option.spec: option.in_stock
            for option in Pinduoduo._spec_option_states(tree, "DZ2538-100")
        }

        self.assertEqual(states, {"42": False, "43": True})
        self.assertEqual(Pinduoduo._spec_options(tree, "DZ2538-100")[0][0], "43")

    def test_dewu_size_price_cells_are_paired_by_left_edge(self) -> None:
        xml = """<hierarchy>
          <node text="40" resource-id="com.shizhuang.duapp:id/tvName" bounds="[10,10][100,40]"/>
          <node text="¥160" resource-id="com.shizhuang.duapp:id/tvPrice" bounds="[10,40][100,70]"/>
          <node text="41" resource-id="com.shizhuang.duapp:id/tvName" bounds="[110,10][200,40]"/>
          <node text="¥170" resource-id="com.shizhuang.duapp:id/tvPrice" bounds="[110,40][200,70]"/>
          <node text="42" resource-id="com.shizhuang.duapp:id/tvName" bounds="[10,100][100,130]"/>
          <node text="¥180" resource-id="com.shizhuang.duapp:id/tvPrice" bounds="[10,130][100,160]"/>
        </hierarchy>"""
        tree = UiTree(ET.fromstring(xml))
        self.assertEqual(
            Dewu._parse_size_prices(tree),
            {"40": Decimal("160"), "41": Decimal("170"), "42": Decimal("180")},
        )

    def test_dewu_sell_quote_parses_accessibility_cells(self) -> None:
        xml = """<hierarchy>
          <node text="FILA 隐藏搜索结果中的旧商品标题" bounds="[0,459][560,485]"/>
          <node text="Nike Dunk Premium Light Orewood Brown 男款低帮板鞋" bounds="[24,528][876,555]"/>
          <node text="DZ2538-100" bounds="[24,567][124,592]"/>
          <node text="货号确认" bounds="[137,568][205,591]"/>
          <node text="最低出售价" bounds="[30,768][120,793]"/>
          <node content-desc="38.5, ¥1599" bounds="[237,1097][444,1304]"/>
          <node content-desc="39, ¥--" bounds="[456,1097][663,1304]"/>
          <node content-desc="40, ¥1089" bounds="[675,1097][882,1304]"/>
        </hierarchy>"""
        tree = UiTree(ET.fromstring(xml))
        self.assertEqual(Dewu._quote_actual_sku(tree), "DZ2538-100")
        self.assertEqual(Dewu._quote_title(tree), "Nike Dunk Premium Light Orewood Brown 男款低帮板鞋")
        self.assertEqual(
            Dewu._parse_size_prices(tree),
            {"38.5": Decimal("1599"), "40": Decimal("1089")},
        )

    def test_dewu_sell_search_card_is_detected(self) -> None:
        xml = """<hierarchy><node clickable="true" bounds="[0,187][900,316]"
          content-desc="Nike Dunk Premium 男款低帮板鞋, 40 即将缺货, ¥1048, 7日均价, 去出价">
          <node text="Nike Dunk Premium 男款低帮板鞋" bounds="[123,208][837,234]"/>
        </node></hierarchy>"""
        tree = UiTree(ET.fromstring(xml))
        parser = Dewu.__new__(Dewu)
        cards = parser._result_cards(tree)
        self.assertEqual(len(cards), 1)
        self.assertEqual(parser._card_title(tree, cards[0]), "Nike Dunk Premium 男款低帮板鞋")

    def test_dewu_new_profile_exposes_seller_mode_switch(self) -> None:
        xml = """<hierarchy>
          <node resource-id="com.shizhuang.duapp:id/llTabSwitch"
                text="" clickable="true" bounds="[99,920][205,945]">
            <node text="切换商家版" bounds="[120,920][205,945]"/>
          </node>
        </hierarchy>"""
        tree = UiTree(ET.fromstring(xml))
        switch = Dewu._seller_mode_switch(tree)
        self.assertIsNotNone(switch)
        self.assertEqual(tree.bounds(switch).left, 99)  # type: ignore[union-attr]

    def test_dewu_seller_center_sale_button_is_detected(self) -> None:
        xml = """<hierarchy>
          <node text="我要出售" resource-id="com.shizhuang.duapp:id/tvBtnSale"
                clickable="true" bounds="[42,408][858,459]"/>
        </hierarchy>"""
        tree = UiTree(ET.fromstring(xml))
        button = Dewu._sell_center_button(tree)
        self.assertIsNotNone(button)
        self.assertEqual(tree.text(button), "我要出售")  # type: ignore[arg-type]

    def test_dewu_sell_search_placeholder_is_detected_by_label(self) -> None:
        xml = """<hierarchy>
          <node content-desc="请输入商品名称/货号/条形码" bounds="[144,64][450,93]"/>
        </hierarchy>"""
        tree = UiTree(ET.fromstring(xml))
        self.assertEqual(len(Dewu._sell_search_fields(tree)), 1)

    def test_dewu_offer_action_reports_selected_spec_count(self) -> None:
        xml = """<hierarchy>
          <node clickable="true" content-desc="去出价（3）" bounds="[165,1519][882,1585]">
            <node text="去出价（3）" bounds="[465,1538][582,1567]" />
          </node>
        </hierarchy>"""
        tree = UiTree(ET.fromstring(xml))
        button, count = Dewu._offer_action(tree)
        self.assertEqual(count, 3)
        self.assertEqual(tree.desc(button), "去出价（3）")

    def test_dewu_sell_confirmation_extracts_sku_title_and_button(self) -> None:
        xml = """<hierarchy>
          <node text="请确认实物是否一致" bounds="[75,509][825,542]"/>
          <node text="adidas originals SUPERSTAR J 金标经典款 低帮 儿童板鞋 白黑 男女同款"
                bounds="[30,878][870,938]"/>
          <node text="该商品易发错货，请确认货号/条形码是否一致" bounds="[30,950][870,980]"/>
          <node text="主货号：FU7712鞋" bounds="[54,1025][767,1055]"/>
          <node clickable="true" content-desc="是这款" bounds="[457,1521][882,1586]">
            <node text="是这款" bounds="[634,1537][706,1570]"/>
          </node>
        </hierarchy>"""
        tree = UiTree(ET.fromstring(xml))
        confirmation = Dewu._sell_confirmation(tree)
        self.assertIsNotNone(confirmation)
        button, sku, title = confirmation
        self.assertEqual(sku, "FU7712")
        self.assertEqual(title, "adidas originals SUPERSTAR J 金标经典款 低帮 儿童板鞋 白黑 男女同款")
        self.assertEqual(tree.desc(button), "是这款")

    def test_dewu_accurate_search_cards_are_detected(self) -> None:
        xml = """<hierarchy><node clickable="true" bounds="[0,194][372,486]">
          <node text="adidas Superstar" resource-id="com.shizhuang.duapp:id/tvAccurateProductName" bounds="[15,403][357,430]" />
          <node text="¥ 5 2 6" resource-id="com.shizhuang.duapp:id/tvAccurateProductPrice" bounds="[15,439][57,466]" />
        </node></hierarchy>"""
        tree = UiTree(ET.fromstring(xml))
        parser = Dewu.__new__(Dewu)
        cards = parser._result_cards(tree)
        self.assertEqual(len(cards), 1)
        self.assertEqual(tree.bounds(cards[0]).left, 0)

    def test_dewu_product_tab_cards_are_detected(self) -> None:
        xml = """<hierarchy><node clickable="true" bounds="[0,232][300,569]">
          <node text="adidas Superstar" resource-id="com.shizhuang.duapp:id/search_title" bounds="[36,440][264,486]" />
          <node text="¥ 5 2 6" resource-id="com.shizhuang.duapp:id/search_price" bounds="[36,508][77,535]" />
        </node></hierarchy>"""
        tree = UiTree(ET.fromstring(xml))
        parser = Dewu.__new__(Dewu)
        self.assertEqual(len(parser._result_cards(tree)), 1)

    def test_dewu_result_cards_follow_visual_order(self) -> None:
        xml = """<hierarchy>
          <node clickable="true" bounds="[300,500][600,800]">
            <node text="第二格" resource-id="com.shizhuang.duapp:id/search_title" bounds="[320,700][580,740]" />
          </node>
          <node clickable="true" bounds="[0,200][300,500]">
            <node text="第一格" resource-id="com.shizhuang.duapp:id/search_title" bounds="[20,400][280,440]" />
          </node>
        </hierarchy>"""
        tree = UiTree(ET.fromstring(xml))
        parser = Dewu.__new__(Dewu)
        cards = parser._result_cards(tree)
        self.assertEqual(parser._card_title(tree, cards[0]), "第一格")

    def test_recent_sales_only_include_today(self) -> None:
        xml = """<hierarchy>
          <node text="白色 42.5" resource-id="com.shizhuang.duapp:id/tvRecentSize" bounds="[10,100][100,130]"/>
          <node text="¥419" resource-id="com.shizhuang.duapp:id/tvRecentPrice" bounds="[110,100][180,130]"/>
          <node text="5分钟前" resource-id="com.shizhuang.duapp:id/tvRecentDate" bounds="[200,100][280,130]"/>
          <node text="白色 41" resource-id="com.shizhuang.duapp:id/tvRecentSize" bounds="[10,150][100,180]"/>
          <node text="¥399" resource-id="com.shizhuang.duapp:id/tvRecentPrice" bounds="[110,150][180,180]"/>
          <node text="4天前" resource-id="com.shizhuang.duapp:id/tvRecentDate" bounds="[200,150][280,180]"/>
        </hierarchy>"""
        sales, reached_old = Dewu._parse_recent_sales_page(UiTree(ET.fromstring(xml)))
        self.assertEqual(len(sales), 1)
        self.assertEqual(sales[0].size, "42.5")
        self.assertEqual(sales[0].price, Decimal("419"))
        self.assertTrue(reached_old)

        all_sales, _ = Dewu._parse_recent_sales_page(
            UiTree(ET.fromstring(xml)), today_only=False
        )
        self.assertEqual([sale.size for sale in all_sales], ["42.5", "41"])

    def test_recent_sale_older_than_seven_days_is_always_rejected(self) -> None:
        now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone(timedelta(hours=8)))
        seven_days = RecentSale(size="39", price=Decimal("500"), sold_at="7天前")
        eight_days = RecentSale(size="39", price=Decimal("500"), sold_at="8天前")
        unknown_date = RecentSale(size="39", price=Decimal("500"), sold_at="很久以前")

        self.assertEqual(Dewu._relative_sale_age("7天前", now), timedelta(days=7))
        self.assertEqual(Dewu._relative_sale_age("2026-07-28", now), timedelta(days=7))
        self.assertTrue(Dewu._recent_sale_is_fresh(seven_days, now))
        self.assertFalse(Dewu._recent_sale_is_fresh(eight_days, now))
        self.assertFalse(Dewu._recent_sale_is_fresh(unknown_date, now))

    def test_estimated_income_is_read_from_expected_income_row(self) -> None:
        xml = """<hierarchy>
          <node text="预计最高收入" bounds="[30,823][132,847]"/>
          <node text="¥ 526.98 /件" bounds="[778,823][870,847]"/>
          <node text="最高收入¥333.70" bounds="[488,888][773,911]"/>
        </hierarchy>"""
        self.assertEqual(
            Dewu._estimated_income(UiTree(ET.fromstring(xml))),
            Decimal("526.98"),
        )

    def test_estimated_income_accepts_negative_and_zero_values(self) -> None:
        negative_xml = """<hierarchy>
          <node text="预计最高收入" bounds="[30,662][132,686]"/>
          <node text="¥ -34.95 /件" bounds="[781,662][870,686]"/>
        </hierarchy>"""
        zero_xml = """<hierarchy>
          <node text="预计最高收入" bounds="[30,823][132,847]"/>
          <node text="¥ 0 /件" bounds="[820,823][870,847]"/>
        </hierarchy>"""
        self.assertEqual(
            Dewu._estimated_income(UiTree(ET.fromstring(negative_xml))),
            Decimal("-34.95"),
        )
        self.assertEqual(
            Dewu._estimated_income(UiTree(ET.fromstring(zero_xml))),
            Decimal("0"),
        )

    def test_estimated_income_reads_split_accessibility_row(self) -> None:
        xml = """<hierarchy>
          <node content-desc="预计最高收入, ¥,  , 477.39,  , /件" bounds="[15,461][885,521]">
            <node text="预计最高收入" bounds="[30,485][132,509]"/>
            <node text="¥" bounds="[746,479][757,509]"/>
            <node text="477.39" bounds="[759,479][830,509]"/>
            <node text="/件" bounds="[832,479][849,507]"/>
          </node>
        </hierarchy>"""
        self.assertEqual(
            Dewu._estimated_income(UiTree(ET.fromstring(xml))),
            Decimal("477.39"),
        )

    def test_fractional_sale_price_is_entered_as_whole_yuan(self) -> None:
        form = UiTree(ET.fromstring(
            '<hierarchy><node class="android.widget.EditText" text="" '
            'bounds="[62,380][725,449]"/></hierarchy>'
        ))
        calculated = UiTree(ET.fromstring(
            '<hierarchy><node class="android.widget.EditText" text="502" '
            'bounds="[62,380][725,449]"/></hierarchy>'
        ))
        adb = MagicMock()
        adb.dump_ui.side_effect = [calculated, calculated]
        parser = Dewu(adb, MagicMock())

        result = parser._enter_and_validate_price(
            form, "DZ1847-115", "36", Decimal("502.5")
        )

        self.assertIs(result, calculated)
        adb.type_ascii.assert_called_once_with("502")

    def test_pdd_reads_detail_specifications_and_bottom_purchase_price(self) -> None:
        xml = """<hierarchy>
          <node text="黑色 14179171" clickable="true" enabled="true" bounds="[20,700][280,780]"/>
          <node text="M" clickable="true" enabled="true" bounds="[20,820][130,900]"/>
          <node text="XL" clickable="true" enabled="true" bounds="[150,820][270,900]"/>
          <node text="¥199" clickable="true" enabled="true" bounds="[500,1510][870,1590]"/>
        </hierarchy>"""
        tree = UiTree(ET.fromstring(xml))
        self.assertEqual(
            [spec for spec, _ in Pinduoduo._spec_options(tree, "14179171")],
            ["M", "XL"],
        )
        self.assertEqual(Pinduoduo._selected_purchase_price(tree), Decimal("199"))

    def test_pdd_reads_top_selected_purchase_price(self) -> None:
        xml = """<hierarchy>
          <node text="&#xA5; 699" bounds="[162,582][232,616]"/>
          <node text="已选择:36 392290-09" bounds="[162,627][594,655]"/>
        </hierarchy>"""
        self.assertEqual(
            Pinduoduo._selected_purchase_price(UiTree(ET.fromstring(xml))),
            Decimal("699"),
        )

    def test_pdd_prefers_live_bottom_price_over_original_price(self) -> None:
        xml = """<hierarchy>
          <node text="现在买，仅 &#xA5;793" bounds="[295,1538][514,1573]"/>
          <node text="&#xA5;1299" bounds="[514,1536][605,1574]"/>
        </hierarchy>"""
        self.assertEqual(
            Pinduoduo._selected_purchase_price(UiTree(ET.fromstring(xml))),
            Decimal("793"),
        )

    def test_pdd_reads_specification_when_price_is_in_option_label(self) -> None:
        xml = """<hierarchy>
          <node text="JR5410" clickable="true" selected="true" enabled="true" bounds="[18,704][121,750]"/>
          <node text="36.5  ¥499.12" clickable="true" selected="false" enabled="true" bounds="[18,814][168,860]"/>
          <node text="37  ¥499.13" clickable="true" selected="false" enabled="true" bounds="[183,814][314,860]"/>
        </hierarchy>"""
        tree = UiTree(ET.fromstring(xml))
        self.assertEqual(
            [spec for spec, _ in Pinduoduo._spec_options(tree, "JR5410")],
            ["36.5", "37"],
        )

    def test_pdd_normalizes_shoe_size_status_and_fraction_suffixes(self) -> None:
        xml = """<hierarchy>
          <node text="38⅔支持鉴定" clickable="true" enabled="true"
                bounds="[18,814][168,860]"/>
          <node text="40支持鉴定" clickable="true" enabled="true"
                bounds="[183,814][314,860]"/>
          <node text="40.5最后1件" clickable="true" enabled="true"
                bounds="[329,814][490,860]"/>
        </hierarchy>"""
        tree = UiTree(ET.fromstring(xml))

        self.assertEqual(
            [spec for spec, _ in Pinduoduo._spec_options(tree, "KI4505")],
            ["38.5", "40", "40.5"],
        )

    def test_pdd_resume_feed_preserves_current_recommendation_page(self) -> None:
        home = UiTree(ET.fromstring(
            """<hierarchy><node text="首页" bounds="[0,1500][120,1600]"/></hierarchy>"""
        ))
        adb = MagicMock()
        adb.dump_ui.return_value = home
        parser = Pinduoduo(adb, MagicMock())
        with patch.object(parser, "_load_home_feed") as load:
            result = parser.resume_feed()

        self.assertIs(result, home)
        adb.launch.assert_called_once_with(
            "com.xunmeng.pinduoduo",
            component="com.xunmeng.pinduoduo/.ui.activity.MainFrameActivity",
            reset_task=False,
        )
        load.assert_not_called()

    def test_pdd_refresh_returns_to_top_before_pull_to_refresh(self) -> None:
        def feed(title: str) -> UiTree:
            return UiTree(ET.fromstring(
                f"""<hierarchy>
                  <node text="首页" bounds="[0,1500][120,1600]"/>
                  <node resource-id="com.xunmeng.pinduoduo:id/tv_title"
                        text="{title}" bounds="[20,400][850,500]"/>
                </hierarchy>"""
            ))

        adb = MagicMock()
        adb.dump_ui.side_effect = [
            feed("深处商品A"),
            feed("深处商品B"),
            feed("顶部商品"),
            feed("顶部商品"),
            feed("顶部商品"),
            feed("刷新商品"),
        ]
        parser = Pinduoduo(adb, MagicMock())

        with patch("monitor.platforms.time.sleep"):
            parser.refresh_feed()

        swipes = [call.args for call in adb.swipe.call_args_list]
        self.assertEqual(swipes[:4], [(450, 650, 450, 1250, 300)] * 4)
        self.assertEqual(swipes[4], (450, 360, 450, 1360, 900))
        adb.launch.assert_not_called()

    def test_pdd_refresh_uses_only_two_top_checks_when_already_at_top(self) -> None:
        def feed(title: str) -> UiTree:
            return UiTree(ET.fromstring(
                f"""<hierarchy>
                  <node text="首页" bounds="[0,1500][120,1600]"/>
                  <node resource-id="com.xunmeng.pinduoduo:id/tv_title"
                        text="{title}" bounds="[20,400][850,500]"/>
                </hierarchy>"""
            ))

        adb = MagicMock()
        adb.dump_ui.side_effect = [
            feed("顶部商品"),
            feed("顶部商品"),
            feed("顶部商品"),
            feed("刷新商品"),
        ]
        parser = Pinduoduo(adb, MagicMock())

        with patch("monitor.platforms.time.sleep"):
            parser.refresh_feed()

        self.assertEqual(
            [call.args for call in adb.swipe.call_args_list],
            [
                (450, 650, 450, 1250, 300),
                (450, 650, 450, 1250, 300),
                (450, 360, 450, 1360, 900),
            ],
        )
        adb.launch.assert_not_called()

    def test_dewu_finds_size_after_one_scroll_into_middle_of_list(self) -> None:
        initial = UiTree(ET.fromstring(
            """<hierarchy>
              <node clickable="true" content-desc="40.5, ¥528"
                    bounds="[18,1494][225,1600]"/>
            </hierarchy>"""
        ))
        after_scroll = UiTree(ET.fromstring(
            """<hierarchy>
              <node clickable="true" content-desc="40.5, ¥528"
                    bounds="[18,719][225,764]"/>
            </hierarchy>"""
        ))
        adb = MagicMock()
        adb.dump_ui.return_value = after_scroll
        parser = Dewu(adb, MagicMock())

        tree, cell = parser._visible_size_cell(initial, "IU7239-001", "40.5")

        self.assertIs(tree, after_scroll)
        self.assertIsNotNone(cell)
        adb.swipe.assert_called_once_with(760, 1400, 760, 850, 350)


class SpreadTests(unittest.TestCase):
    def test_estimated_income_profit_example(self) -> None:
        pdd = VerifiedProduct(
            "pdd", "shoe", "EXAMPLE-1", Decimal("420"),
            size_prices={"39": Decimal("420")},
        )
        dewu = VerifiedProduct(
            "dewu", "shoe", "EXAMPLE-1", Decimal("600"),
            size_prices={"39": Decimal("600")},
            recent_sale_prices={"39": Decimal("600")},
            estimated_incomes={"39": Decimal("480")},
        )
        opportunity = best_opportunity(pdd, dewu, require_numbered_size=True)
        self.assertIsNotNone(opportunity)
        self.assertEqual(opportunity.recent_sale_price, Decimal("600"))
        self.assertEqual(opportunity.estimated_income, Decimal("480"))
        self.assertEqual(opportunity.spread, Decimal("60"))

    def test_best_spread_requires_same_size(self) -> None:
        pdd = VerifiedProduct("pdd", "shoe", "MT92232A", Decimal("84"), size_prices={"40": Decimal("84"), "41": Decimal("84")})
        dewu = VerifiedProduct(
            "dewu", "shoe", "MT92232A", Decimal("160"),
            size_prices={"39": Decimal("284"), "40": Decimal("160"), "41": Decimal("170")},
            recent_sale_prices={"40": Decimal("180"), "41": Decimal("190")},
            estimated_incomes={"40": Decimal("150"), "41": Decimal("170")},
        )
        best = best_opportunity(pdd, dewu)
        self.assertIsNotNone(best)
        self.assertEqual(best.size, "41")
        self.assertEqual(best.spread, Decimal("86"))
        self.assertEqual(best.recent_sale_price, Decimal("190"))

        all_matches = matching_opportunities(pdd, dewu)
        self.assertEqual([item.size for item in all_matches], ["40", "41"])
        self.assertEqual([item.spread for item in all_matches], [Decimal("66"), Decimal("86")])

    def test_shoes_never_compare_unsized_surface_prices(self) -> None:
        pdd = VerifiedProduct(
            "pdd", "shoe", "ABC123", Decimal("100"),
            size_prices={"ONE": Decimal("100")},
        )
        dewu = VerifiedProduct(
            "dewu", "shoe", "ABC123", Decimal("300"),
            size_prices={"ONE": Decimal("300")},
            recent_sale_prices={"ONE": Decimal("300")},
            estimated_incomes={"ONE": Decimal("260")},
        )
        self.assertIsNone(best_opportunity(pdd, dewu, require_numbered_size=True))


class AlertQualityTests(unittest.TestCase):
    def test_stable_five_sale_profile_is_grade_a_and_uses_conservative_price(self) -> None:
        now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone(timedelta(hours=8)))
        sales = [
            RecentSale("42", Decimal("500"), "2026-08-06"),
            RecentSale("42", Decimal("498"), "2026-08-05"),
            RecentSale("42", Decimal("497"), "2026-08-04"),
            RecentSale("42", Decimal("496"), "2026-08-04", "buyer-2"),
            RecentSale("42", Decimal("495"), "2026-08-04", "buyer-3"),
        ]
        profile = Dewu._sales_profile(
            sales, "42", now, current_ask=Decimal("490")
        )
        self.assertIsNotNone(profile)
        self.assertEqual(profile.observed_sales_7d, 5)
        self.assertEqual(profile.active_days_7d, 3)
        self.assertEqual(profile.max_gap_hours, 24)
        self.assertEqual(profile.price_trend, "stable")
        self.assertEqual(profile.conservative_price, Decimal("490"))

        opportunity = Opportunity(
            "TEST-A", "Stable shoe", "42", Decimal("400"), Decimal("460"),
            recent_sale_price=Decimal("500"),
            evaluation_sale_price=Decimal("490"),
            sales_profile=profile,
        )
        self.assertEqual(assess_opportunity(opportunity).level, "A")

    def test_quality_levels_expand_to_a_plus_and_b_plus(self) -> None:
        profile = SalesProfile(
            observed_sales_7d=5,
            active_days_7d=4,
            latest_age_hours=8,
            max_gap_hours=24,
            latest_price=Decimal("520"),
            median_price=Decimal("520"),
            minimum_price=Decimal("515"),
            maximum_price=Decimal("525"),
            conservative_price=Decimal("510"),
            price_range_percent=Decimal("1.9"),
            price_trend="stable",
        )
        premium = Opportunity(
            "TEST-A+", "Premium shoe", "42", Decimal("400"), Decimal("500"),
            recent_sale_price=Decimal("520"), evaluation_sale_price=Decimal("500"),
            sales_profile=profile,
        )
        self.assertEqual(assess_opportunity(premium).level, "A+")

        watch_profile = SalesProfile(
            **{**profile.__dict__, "observed_sales_7d": 4, "active_days_7d": 2, "max_gap_hours": 80}
        )
        watch = Opportunity(
            "TEST-B+", "Watch shoe", "42", Decimal("400"), Decimal("450"),
            recent_sale_price=Decimal("470"), evaluation_sale_price=Decimal("450"),
            sales_profile=watch_profile,
        )
        self.assertEqual(assess_opportunity(watch).level, "B+")

    def test_falling_or_sparse_sales_are_grade_b_with_reasons(self) -> None:
        now = datetime(2026, 8, 6, 12, 0, tzinfo=timezone(timedelta(hours=8)))
        calculated = Dewu._sales_profile(
            [
                RecentSale("42", Decimal("470"), "2026-08-06"),
                RecentSale("42", Decimal("480"), "2026-08-05"),
                RecentSale("42", Decimal("500"), "2026-08-04"),
            ],
            "42",
            now,
        )
        self.assertIsNotNone(calculated)
        self.assertEqual(calculated.price_trend, "falling")

        profile = SalesProfile(
            observed_sales_7d=3,
            active_days_7d=2,
            latest_age_hours=12,
            max_gap_hours=96,
            latest_price=Decimal("470"),
            median_price=Decimal("480"),
            minimum_price=Decimal("470"),
            maximum_price=Decimal("500"),
            conservative_price=Decimal("470"),
            price_range_percent=Decimal("6.3"),
            price_trend="falling",
        )
        opportunity = Opportunity(
            "TEST-B", "Falling shoe", "42", Decimal("400"), Decimal("450"),
            recent_sale_price=Decimal("470"),
            evaluation_sale_price=Decimal("470"),
            sales_profile=profile,
        )
        assessment = assess_opportunity(opportunity)
        self.assertEqual(assessment.level, "B")
        self.assertTrue(any("3次" in reason for reason in assessment.reasons))
        self.assertTrue(any("连续明显下降" in reason for reason in assessment.reasons))


class RemainingSpreadTests(unittest.TestCase):
    def test_shoes_reject_color_specifications(self) -> None:
        pdd = VerifiedProduct(
            "pdd", "shoe", "ABC123", Decimal("100"),
            size_prices={"黑色": Decimal("100")},
        )
        dewu = VerifiedProduct(
            "dewu", "shoe", "ABC123", Decimal("300"),
            size_prices={"黑色": Decimal("300")},
            recent_sale_prices={"黑色": Decimal("300")},
            estimated_incomes={"黑色": Decimal("260")},
        )
        self.assertIsNone(best_opportunity(pdd, dewu, require_numbered_size=True))

    def test_negative_income_is_valid_but_has_negative_profit(self) -> None:
        pdd = VerifiedProduct(
            "pdd", "shoe", "ABC123", Decimal("100"),
            size_prices={"39": Decimal("100")},
        )
        dewu = VerifiedProduct(
            "dewu", "shoe", "ABC123", Decimal("300"),
            recent_sale_prices={"39": Decimal("5")},
            estimated_incomes={"39": Decimal("-34.95")},
        )
        opportunity = best_opportunity(pdd, dewu, require_numbered_size=True)
        self.assertIsNotNone(opportunity)
        self.assertEqual(opportunity.estimated_income, Decimal("-34.95"))
        self.assertEqual(opportunity.spread, Decimal("-134.95"))

    def test_different_shoe_sizes_are_not_compared(self) -> None:
        pdd = VerifiedProduct(
            "pdd", "shoe", "ABC123", Decimal("100"),
            size_prices={"42": Decimal("100")},
        )
        dewu = VerifiedProduct(
            "dewu", "shoe", "ABC123", Decimal("300"),
            size_prices={"42.5": Decimal("300")},
            recent_sale_prices={"42.5": Decimal("300")},
            estimated_incomes={"42.5": Decimal("260")},
        )
        self.assertIsNone(best_opportunity(pdd, dewu, require_numbered_size=True))


class NotificationTests(unittest.TestCase):
    def test_pushdeer_includes_alert_level_and_specification(self) -> None:
        logger = MagicMock()
        notifier = PushDeerNotifier(
            PushDeerConfig("PDU_TEST", timeout_seconds=1, retries=1),
            logger,
        )
        response = MagicMock()
        response.read.return_value = b'{"error":0,"content":{"result":["success"]}}'
        response.__enter__.return_value = response
        opportunity = Opportunity(
            sku="EXAMPLE-1",
            title="Example shoe",
            size="38 2/3",
            pdd_price=Decimal("420"),
            dewu_price=Decimal("480"),
            recent_sale_price=Decimal("600"),
        )
        with patch("monitor.notify.urlopen", return_value=response) as send:
            self.assertTrue(notifier.send_opportunity(opportunity, "鞋子", "B"))
        body = unquote_plus(send.call_args.args[0].data.decode("utf-8"))
        self.assertIn("B级", body)
        self.assertIn("38 2/3", body)

    def test_wecom_bot_sends_markdown_opportunity(self) -> None:
        logger = MagicMock()
        notifier = WeComBotNotifier(
            WeComBotConfig(
                "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key",
                timeout_seconds=1,
                retries=1,
            ),
            logger,
        )
        response = MagicMock()
        response.read.return_value = b'{"errcode":0,"errmsg":"ok"}'
        response.__enter__.return_value = response
        opportunity = Opportunity(
            sku="EXAMPLE-1",
            title="Example shoe",
            size="39",
            pdd_price=Decimal("420"),
            dewu_price=Decimal("480"),
            recent_sale_price=Decimal("600"),
        )
        with patch("monitor.notify.urlopen", return_value=response) as send:
            self.assertTrue(notifier.send_opportunity(opportunity, "鞋子"))
        request = send.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, notifier.config.webhook_url)
        self.assertEqual(body["msgtype"], "markdown")
        self.assertIn("EXAMPLE-1", body["markdown"]["content"])

    def test_wecom_bot_failure_does_not_raise(self) -> None:
        logger = MagicMock()
        notifier = WeComBotNotifier(
            WeComBotConfig(
                "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key",
                timeout_seconds=1,
                retries=1,
            ),
            logger,
        )
        with patch("monitor.notify.urlopen", side_effect=URLError("offline")):
            self.assertFalse(notifier.send("test", "message"))

    def test_ntfy_sends_high_priority_notification(self) -> None:
        logger = MagicMock()
        notifier = NtfyNotifier(
            NtfyConfig("https://ntfy.sh", "private_topic_1234", priority=5, retries=1),
            logger,
        )
        response = MagicMock()
        response.read.return_value = b'{"id":"abc123","event":"message"}'
        response.__enter__.return_value = response
        with patch("monitor.notify.urlopen", return_value=response) as send:
            self.assertTrue(notifier.send("test title", "test message"))
        request = send.call_args.args[0]
        body = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, "https://ntfy.sh")
        self.assertEqual(body["topic"], "private_topic_1234")
        self.assertEqual(body["priority"], 5)

    def test_ntfy_direct_ip_keeps_tls_hostname_and_bypasses_urlopen(self) -> None:
        logger = MagicMock()
        notifier = NtfyNotifier(
            NtfyConfig(
                "https://ntfy.sh",
                "private_topic_1234",
                priority=5,
                retries=1,
                direct_ip="159.203.148.75",
            ),
            logger,
        )
        response = MagicMock(status=200)
        response.read.return_value = b'{"id":"abc123","event":"message"}'
        connection = MagicMock()
        connection.getresponse.return_value = response

        with (
            patch("monitor.notify._DirectHTTPSConnection", return_value=connection) as direct,
            patch("monitor.notify.urlopen") as send,
        ):
            self.assertTrue(notifier.send("test title", "test message"))

        direct.assert_called_once_with(
            "ntfy.sh",
            "159.203.148.75",
            443,
            timeout=8.0,
        )
        send.assert_not_called()
        request = connection.request.call_args
        self.assertEqual(request.args[:2], ("POST", "/"))
        body = json.loads(request.kwargs["body"].decode("utf-8"))
        self.assertEqual(body["topic"], "private_topic_1234")
        self.assertEqual(request.kwargs["headers"]["Host"], "ntfy.sh")

    def test_pushdeer_sends_qualified_specification(self) -> None:
        logger = MagicMock()
        notifier = PushDeerNotifier(
            PushDeerConfig("PDU_TEST", timeout_seconds=1, retries=1),
            logger,
        )
        response = MagicMock()
        response.read.return_value = b'{"error":0,"content":{"result":["success"]}}'
        response.__enter__.return_value = response
        opportunity = Opportunity(
            sku="EXAMPLE-1",
            title="Example shoe",
            size="39",
            pdd_price=Decimal("420"),
            dewu_price=Decimal("480"),
            recent_sale_price=Decimal("600"),
        )
        with patch("monitor.notify.urlopen", return_value=response) as send:
            self.assertTrue(notifier.send_opportunity(opportunity, "鞋子"))
        request = send.call_args.args[0]
        body = request.data.decode("utf-8")
        self.assertEqual(request.full_url, "https://api2.pushdeer.com/message/push")
        self.assertIn("PDU_TEST", body)
        self.assertIn("EXAMPLE-1", body)

    def test_pushdeer_failure_does_not_raise(self) -> None:
        logger = MagicMock()
        notifier = PushDeerNotifier(
            PushDeerConfig("PDU_TEST", timeout_seconds=1, retries=1),
            logger,
        )
        with patch("monitor.notify.urlopen", side_effect=URLError("offline")):
            self.assertFalse(notifier.send("test", "message"))

    def test_pushdeer_rejects_success_response_without_bound_devices(self) -> None:
        logger = MagicMock()
        notifier = PushDeerNotifier(
            PushDeerConfig("PDU_TEST", timeout_seconds=1, retries=1),
            logger,
        )
        response = MagicMock()
        response.read.return_value = b'{"code":0,"content":{"result":[]}}'
        response.__enter__.return_value = response
        with patch("monitor.notify.urlopen", return_value=response):
            self.assertFalse(notifier.send("test", "message"))

class StorageTests(unittest.TestCase):
    def test_unchanged_candidate_is_suppressed_during_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = Storage(root / "monitor.sqlite3", root / "alerts.jsonl")
            self.assertTrue(storage.candidate_due("KK4474", Decimal("449"), 600))
            self.assertFalse(storage.candidate_due("KK4474", Decimal("449"), 600))
            storage.retry_candidate_after("KK4474", 600, 0)
            self.assertTrue(storage.candidate_due("KK4474", Decimal("449"), 600))
            self.assertTrue(storage.candidate_due("KK4474", Decimal("448"), 600))
            storage.connection.close()

    def test_b_grade_observation_persists_quality_metrics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            storage = Storage(root / "monitor.sqlite3", root / "alerts.jsonl")
            profile = SalesProfile(
                observed_sales_7d=2,
                active_days_7d=1,
                latest_age_hours=20,
                max_gap_hours=80,
                latest_price=Decimal("500"),
                median_price=Decimal("495"),
                minimum_price=Decimal("490"),
                maximum_price=Decimal("500"),
                conservative_price=Decimal("480"),
                price_range_percent=Decimal("2.0"),
                price_trend="insufficient",
            )
            opportunity = Opportunity(
                "OBS-1", "Observed shoe", "42", Decimal("400"), Decimal("450"),
                recent_sale_price=Decimal("500"),
                evaluation_sale_price=Decimal("480"),
                sales_profile=profile,
            )
            storage.record_alert(
                opportunity, "鞋子", "B", ("7天同规格仅观察到2次成交",)
            )
            storage.connection.close()

            alert = AppRepository(root / "monitor.sqlite3").alerts()[0]
            self.assertEqual(alert.alert_level, "B")
            self.assertEqual(alert.observed_sales_7d, 2)
            self.assertEqual(alert.evaluation_sale_price, Decimal("480"))
            self.assertIn("2次", alert.assessment_reasons[0])

    def test_retired_stock_watch_data_is_removed_without_deleting_price_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "monitor.sqlite3"
            alerts_jsonl = root / "alerts.jsonl"
            connection = sqlite3.connect(database)
            connection.executescript(
                """
                CREATE TABLE stock_watch_states(sku TEXT);
                CREATE TABLE restock_alerts(sku TEXT);
                """
            )
            connection.close()
            price_alert = {"alert_level": "A", "reference": "得物", "sku": "KEEP-1"}
            retired_alert = {"expected_profit": "58", "sku": "REMOVE-1"}
            alerts_jsonl.write_text(
                json.dumps(price_alert, ensure_ascii=False)
                + "\n"
                + json.dumps(retired_alert, ensure_ascii=False)
                + "\n",
                encoding="utf-8",
            )

            storage = Storage(database, alerts_jsonl)
            tables = {
                row[0]
                for row in storage.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            storage.connection.close()

            self.assertNotIn("stock_watch_states", tables)
            self.assertNotIn("restock_alerts", tables)
            self.assertEqual(
                [json.loads(line) for line in alerts_jsonl.read_text(encoding="utf-8").splitlines()],
                [price_alert],
            )


class DesktopDataTests(unittest.TestCase):
    def test_task_controller_reads_monitor_lock_without_starting_powershell(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            runtime = root / "runtime"
            runtime.mkdir()
            (root / "config.json").write_text(
                json.dumps({"runtime_dir": "runtime"}), encoding="utf-8"
            )
            controller = TaskController(root)
            self.assertFalse(controller.is_running())
            lock = acquire_single_instance(runtime)
            self.assertIsNotNone(lock)
            try:
                self.assertTrue(controller.is_running())
            finally:
                lock.close()
            self.assertFalse(controller.is_running())

    def test_multiple_alerts_can_be_deleted_without_deleting_ledger_orders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "monitor.sqlite3"
            repository = AppRepository(database)
            connection = sqlite3.connect(database)
            for alert_id in (1, 2, 3):
                connection.execute(
                    """
                    INSERT INTO alerts(
                        id, created_at, dedupe_key, sku, size,
                        pdd_price, dewu_price, spread, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        alert_id,
                        "2026-08-04T12:00:00+08:00",
                        f"alert-{alert_id}",
                        f"SKU-{alert_id}",
                        "39",
                        "400",
                        "450",
                        "50",
                        json.dumps({"title": f"Product {alert_id}"}),
                    ),
                )
            connection.commit()
            connection.close()

            first_alert = next(alert for alert in repository.alerts() if alert.id == 1)
            repository.add_alert_to_ledger(first_alert)
            alert_revision = repository.alerts_revision()
            order_revision = repository.orders_revision("2026-08")
            self.assertEqual(repository.delete_alerts([1, 3, 3]), 2)
            self.assertNotEqual(repository.alerts_revision(), alert_revision)
            self.assertEqual(repository.orders_revision("2026-08"), order_revision)
            self.assertEqual([alert.id for alert in repository.alerts()], [2])
            self.assertEqual(len(repository.orders("2026-08")), 1)

    def test_alert_can_be_added_to_ledger_and_realized_profit_is_monthly(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = AppRepository(Path(temp_dir) / "monitor.sqlite3")
            alert = AlertRecord(
                id=7,
                created_at="2026-08-04T12:00:00+08:00",
                category="鞋子",
                title="Nike test",
                sku="TEST-001",
                size="39",
                pdd_price=Decimal("420"),
                recent_sale_price=Decimal("600"),
                estimated_income=Decimal("480"),
                profit=Decimal("60"),
            )
            order_id = repository.add_alert_to_ledger(alert)
            self.assertEqual(repository.month_summary("2026-08")["realized_profit"], Decimal("0"))

            repository.update_status(order_id, "已售出")
            summary = repository.month_summary("2026-08")
            self.assertEqual(summary["realized_profit"], Decimal("60"))
            self.assertEqual(summary["expected_profit"], Decimal("60"))

            second_order_id = repository.add_alert_to_ledger(alert)
            self.assertNotEqual(second_order_id, order_id)
            self.assertEqual(len(repository.orders("2026-08")), 2)

            repository.delete_order(order_id)
            third_order_id = repository.add_alert_to_ledger(alert)
            self.assertNotEqual(third_order_id, second_order_id)
            self.assertEqual(len(repository.orders("2026-08")), 2)

    def test_legacy_unique_schema_is_migrated_to_allow_duplicate_alerts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            database = Path(temp_dir) / "monitor.sqlite3"
            connection = sqlite3.connect(database)
            connection.execute(
                """
                CREATE TABLE ledger_orders (
                    id INTEGER PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    sku TEXT NOT NULL,
                    size TEXT NOT NULL,
                    cost TEXT NOT NULL,
                    income TEXT NOT NULL,
                    status TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '手动添加',
                    alert_id INTEGER UNIQUE,
                    notes TEXT NOT NULL DEFAULT ''
                )
                """
            )
            connection.commit()
            connection.close()

            repository = AppRepository(database)
            alert = AlertRecord(
                id=11,
                created_at="2026-08-04T12:00:00+08:00",
                category="鞋子",
                title="Legacy test",
                sku="LEGACY-001",
                size="40",
                pdd_price=Decimal("400"),
                recent_sale_price=Decimal("560"),
                estimated_income=Decimal("450"),
                profit=Decimal("50"),
            )
            first_order_id = repository.add_alert_to_ledger(alert)
            second_order_id = repository.add_alert_to_ledger(alert)
            self.assertNotEqual(first_order_id, second_order_id)
            self.assertEqual(len(repository.orders("2026-08")), 2)
            self.assertEqual(repository.orders("2026-08")[0].shipping, Decimal("0"))

    def test_shipping_is_deducted_and_month_totals_follow_order_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            repository = AppRepository(Path(temp_dir) / "monitor.sqlite3")
            sold_id = repository.add_order(
                sku="SOLD-001",
                size="39",
                cost=Decimal("100"),
                shipping=Decimal("10"),
                income=Decimal("160"),
                created_at="2026-08-04T12:00:00+08:00",
            )
            repository.update_status(sold_id, "已售出")
            repository.add_order(
                sku="ARRIVED-001",
                size="40",
                cost=Decimal("200"),
                shipping=Decimal("20"),
                income=Decimal("300"),
                status="已到货",
                created_at="2026-08-04T13:00:00+08:00",
            )
            repository.add_order(
                sku="RETURNED-001",
                size="41",
                cost=Decimal("999"),
                shipping=Decimal("99"),
                income=Decimal("1200"),
                status="已退货",
                created_at="2026-08-04T14:00:00+08:00",
            )

            summary = repository.month_summary("2026-08")
            self.assertEqual(summary["total_cost"], Decimal("300"))
            self.assertEqual(summary["total_shipping"], Decimal("30"))
            self.assertEqual(summary["total_revenue"], Decimal("460"))
            self.assertEqual(summary["sold_revenue"], Decimal("160"))
            self.assertEqual(summary["expected_profit"], Decimal("130"))
            self.assertEqual(summary["realized_profit"], Decimal("50"))

    def test_ledger_return_excludes_profit_and_exports_workbook(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            repository = AppRepository(directory / "monitor.sqlite3")
            order_id = repository.add_order(
                sku="OWN-001",
                size="黑色",
                cost=Decimal("100"),
                shipping=Decimal("10"),
                income=Decimal("160"),
                created_at="2026-08-04T12:00:00+08:00",
            )
            repository.update_status(order_id, "已退货")
            summary = repository.month_summary("2026-08")
            self.assertEqual(summary["expected_profit"], Decimal("0"))

            path = directory / "ledger.xlsx"
            repository.export_ledger(path, repository.orders("2026-08"))
            workbook = load_workbook(path)
            self.assertEqual(workbook.active["C2"].value, "OWN-001")
            self.assertEqual(workbook.active["F2"].value, 10)
            self.assertEqual(workbook.active["I2"].value, "已退货")


class ReportTests(unittest.TestCase):
    def test_category_and_alert_report(self) -> None:
        self.assertEqual(classify_product("Nike 男款跑鞋"), "鞋子")
        self.assertEqual(classify_product("蓝牙降噪耳机"), "数码")
        with tempfile.TemporaryDirectory() as directory:
            report = SessionReport(Path(directory))
            self.assertEqual(report.path.suffix, ".xlsx")
            workbook = load_workbook(report.path)
            sheet = workbook["价差报警"]
            self.assertEqual(sheet.max_row, 1)
            self.assertEqual(tuple(cell.value for cell in sheet[1]), HEADERS)
            self.assertEqual(sheet.freeze_panes, "A2")
            workbook.close()

            opportunity = Opportunity(
                sku="FD9883-101", title="Nike 男款拖鞋", size="42.5",
                pdd_price=Decimal("219"), dewu_price=Decimal("319"),
                recent_sale_price=Decimal("419"),
            )
            report.append(
                opportunity,
                "鞋子",
                [RecentSale(size="42.5", price=Decimal("435"), sold_at="15分钟前")],
            )
            workbook = load_workbook(report.path)
            sheet = workbook["价差报警"]
            self.assertEqual(sheet.max_row, 2)
            self.assertEqual(sheet["B2"].value, "鞋子")
            self.assertEqual(sheet["D2"].value, "FD9883-101")
            self.assertEqual(sheet["F2"].value, 219)
            self.assertEqual(sheet["G2"].value, 419)
            self.assertEqual(sheet["H2"].value, 319)
            self.assertEqual(sheet["I2"].value, 100)
            self.assertIn("规格 42.5 | 成交价 ¥435", sheet["J2"].value)
            self.assertEqual(sheet["I2"].number_format, CURRENCY_FORMAT)
            workbook.close()

    def test_locked_report_rolls_over_to_continuation_workbook(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report = SessionReport(Path(directory))
            locked_path = report.path
            real_save = report.workbook.save

            def fail_only_for_locked_path(path: Path) -> None:
                if Path(path) == locked_path:
                    raise PermissionError("workbook is open")
                real_save(path)

            report.workbook.save = fail_only_for_locked_path
            opportunity = Opportunity(
                sku="DZ2538-100",
                title="Nike Dunk Low",
                size="42",
                pdd_price=Decimal("343.2"),
                dewu_price=Decimal("870"),
            )
            report.append(opportunity, "鞋子", [])

            self.assertNotEqual(report.path, locked_path)
            self.assertTrue(report.path.name.endswith("_续写2.xlsx"))
            workbook = load_workbook(report.path, read_only=True)
            self.assertEqual(workbook["价差报警"]["D2"].value, "DZ2538-100")
            workbook.close()


if __name__ == "__main__":
    unittest.main()
