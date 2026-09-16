import sys, os, types, importlib.util, unittest

# ---- stub 掉 MP 运行时依赖，让纯逻辑可独立单测 ----
def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m

class _Any:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n): return _Any()
    def __call__(self, *a, **k): return _Any()

_stub("app")
_stub("app.core")
_stub("app.core.event", Event=_Any(), eventmanager=_stub("em2"))
sys.modules["em2"].register = lambda *a, **k: (lambda f: f)
_stub("app.db")
_stub("app.db.downloadhistory_oper", DownloadHistoryOper=_Any())
_stub("app.db.site_oper", SiteOper=_Any())
_stub("app.db.subscribe_oper", SubscribeOper=_Any())
_stub("app.log", logger=_stub("lg2",
      info=print, warning=print, debug=lambda *a, **k: None, error=print, warn=print))
_stub("app.plugins", _PluginBase=object)
_stub("app.schemas")
_stub("app.schemas.types", EventType=_Any(), SystemConfigKey=_Any(), MediaType=_Any())

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location(
    "sgf", os.path.join(HERE, "..", "plugins.v2", "subscribegroupfix", "__init__.py"))
sgf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sgf)


class TestParsePairs(unittest.TestCase):
    def test_simple_replace(self):
        pairs = sgf.parse_replace_pairs(["[Ff][Pp][Ss] => 帧", "HDR => HDR10"])
        self.assertIn(("[Ff][Pp][Ss]", "帧"), pairs)
        self.assertIn(("HDR", "HDR10"), pairs)

    def test_skip_complex_and_block(self):
        pairs = sgf.parse_replace_pairs([
            "# 注释",
            "屏蔽词",                       # block 无 =>
            "A => B && C <> D >> E",        # 复合词
            "X <> Y >> +1",                 # offset
        ])
        self.assertEqual(pairs, [])

    def test_skip_nonliteral_rhs(self):
        # RHS 含正则元字符不可反演
        self.assertEqual(sgf.parse_replace_pairs(["A => $1"]), [])


class TestRestore(unittest.TestCase):
    PAIRS = [("[Ff][Pp][Ss]", "帧")]

    def test_token_already_raw(self):
        # token 本来就在原始标题里：直接用
        got = sgf.restore_customization_token(
            "10Bit", "Show 10Bit WEB", "Show.10bit.WEB-GRP", self.PAIRS)
        self.assertEqual(got, "10Bit")

    def test_route1_word_table(self):
        # 60帧 -> 60FPS（[Ff][Pp][Ss] 命中 FPS，字面 60 保留）
        got = sgf.restore_customization_token(
            "60帧", "Show.S01E01.60帧.WEB-HHWEB",
            "Show.S01E01.2160p.WEB-DL.60FPS.HHWEB", self.PAIRS)
        self.assertEqual(got, "60FPS")

    def test_route2_anchor_diff_chain(self):
        # 连锁替换（Xy、Z 两条词拼成 token，词表反查不中）：锚点差分兜底
        got = sgf.restore_customization_token(
            "XyZ", "Show S01E01 XyZ WEB HHWEB",
            "Show S01E01 Weird-Original-77 WEB HHWEB",
            [("W?ei?rd", "Xy"), ("77", "Z")])
        self.assertEqual(got, "Weird-Original-77")

    def test_route2_requires_evidence(self):
        # 无任何词表证据时不臆测差分（replaced/raw 排版不同源场景）
        got = sgf.restore_customization_token(
            "60帧", "Show 60帧 WEB", "Show S01E01 Weird-Original WEB", [])
        self.assertIsNone(got)

    def test_route2_fail_returns_none(self):
        got = sgf.restore_customization_token(
            "60帧", "Show 60帧 WEB", "Show.WEB-DL-HHWEB", [])
        self.assertIsNone(got)

    def test_empty_inputs(self):
        self.assertIsNone(sgf.restore_customization_token("", "a", "b", []))
        self.assertIsNone(sgf.restore_customization_token("x", None, None, []))


class TestBuildRule(unittest.TestCase):
    def test_single_fragment(self):
        self.assertEqual(sgf.build_include_rule(["HHWEB"]), r"HHWEB")

    def test_coexist_assertion(self):
        rule = sgf.build_include_rule(["60FPS", "HHWEB"])
        self.assertEqual(rule, r"(?=.*60FPS)(?=.*HHWEB)")

    def test_escapes_metachars(self):
        rule = sgf.build_include_rule(["A+B", "C.D"])
        self.assertIn(r"A\+B", rule)
        self.assertIn(r"C\.D", rule)

    def test_gate_matches_reordered_title(self):
        # 同现断言的价值：组名前置排版也命中（上游 .+ 串接做不到）
        import re
        rule = sgf.build_include_rule(["60FPS", "HHWEB"])
        self.assertTrue(re.search(rule, "HHWEB.Show.2160p.60fps.WEB-DL", re.IGNORECASE))
        self.assertTrue(re.search(rule, "Show.60FPS.WEB.HHWEB", re.IGNORECASE))
        self.assertFalse(re.search(rule, "Show.2160p.WEB.DDP", re.IGNORECASE))

    def test_empty(self):
        self.assertIsNone(sgf.build_include_rule([]))
        self.assertIsNone(sgf.build_include_rule([None, ""]))


class TestEndToEndPipeline(unittest.TestCase):
    """还原+拼规则+自匹配闸门 全链路（复刻 download_notice 制作组分支语义）。"""

    def _pipeline(self, customization, resource_team, raw_title, replaced, pairs):
        import re
        fragments = []
        for tk in [t for t in customization.split("@") if t]:
            r = sgf.restore_customization_token(tk, replaced, raw_title, pairs)
            if r:
                fragments.append(r)
        if resource_team:
            fragments.append(resource_team)
        candidate = sgf.build_include_rule(fragments)
        if candidate and raw_title and re.search(candidate, raw_title, re.IGNORECASE):
            return candidate
        return resource_team  # 降级

    def test_full_precision(self):
        rule = self._pipeline("60帧", "HHWEB",
                              "Show.S01E01.2160p.WEB-DL.60FPS.HHWEB",
                              "Show.S01E01.2160p.WEB-DL.60帧.HHWEB",
                              [("[Ff][Pp][Ss]", "帧")])
        self.assertEqual(rule, r"(?=.*60FPS)(?=.*HHWEB)")

    def test_multi_tokens(self):
        rule = self._pipeline("60帧@10Bit", "HHWEB",
                              "Show.S01E01.10Bit.60FPS.WEB.HHWEB",
                              "Show.S01E01.10Bit.60帧.WEB.HHWEB",
                              [("[Ff][Pp][Ss]", "帧")])
        self.assertEqual(rule, r"(?=.*60FPS)(?=.*10Bit)(?=.*HHWEB)")

    def test_degrade_when_unrestorable(self):
        rule = self._pipeline("60帧", "HHWEB",
                              "Show.S01E01.2160p.WEB-DL.HHWEB",
                              "Show.S01E01.60帧.WEB", [])
        self.assertEqual(rule, "HHWEB")


if __name__ == "__main__":
    unittest.main(verbosity=2)
