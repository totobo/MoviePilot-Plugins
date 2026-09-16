import sys, os, types, importlib.util, datetime, unittest

# ---- stub 掉 MP 运行时依赖，让纯逻辑可独立单测 ----
def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m

_stub("apscheduler")
_stub("apscheduler.triggers")
_stub("apscheduler.triggers.cron", CronTrigger=object)

class _Any:
    def __init__(self, *a, **k): pass
    def __getattr__(self, n): return _Any()
    def __call__(self, *a, **k): return _Any()

_stub("app")
_stub("app.core")
_stub("app.core.context", MediaType=_Any())
_stub("app.core.schemas", MediaType=_Any())
_stub("app.core.event", Event=_Any(), eventmanager=_stub("em"))
sys.modules["em"].register = lambda *a, **k: (lambda f: f)
_stub("app.core.module", ModuleManager=_Any())
_stub("app.db")
_stub("app.db.models")
_stub("app.db.models.subscribehistory", SubscribeHistory=_Any())
_stub("app.db.subscribe_oper", SubscribeOper=_Any())
_stub("app.log", logger=_stub("lg",
      info=print, warning=print, debug=lambda *a, **k: None, error=print))
_stub("app.plugins", _PluginBase=object)
_stub("app.schemas")
_stub("app.schemas.types", ChainEventType=_Any(), EventType=_Any(), NotificationType=_Any())
_stub("app.schemas.event",
      SubscribeCompletionCheckEventData=_Any(), SubscribeEpisodesRefreshEventData=_Any())

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("seg", os.path.join(HERE, "..", "plugins.v2", "subscribeepisodeguard", "__init__.py"))
seg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(seg)


class TestParse(unittest.TestCase):
    def test_parse_dt(self):
        self.assertEqual(seg.parse_dt("2026-09-07 13:12:10"),
                         datetime.datetime(2026, 9, 7, 13, 12, 10))
        self.assertEqual(seg.parse_dt("2026-09-07"), datetime.datetime(2026, 9, 7, 0, 0))
        self.assertIsNone(seg.parse_dt(None))
        self.assertIsNone(seg.parse_dt("bad"))

    def test_parse_date(self):
        self.assertEqual(seg.parse_date("2026-09-11"), datetime.date(2026, 9, 11))
        self.assertIsNone(seg.parse_date(""))


class TestCovered(unittest.TestCase):
    class Sub:
        note = [1, 2, "3"]
        episode_priority = {"4": 96, "x": 1}

    def test_max(self):
        self.assertEqual(seg.covered_max_episode(self.Sub()), 4)

    def test_episodes(self):
        self.assertEqual(seg.covered_episodes(self.Sub()), {1, 2, 3, 4})

    def test_empty(self):
        self.assertEqual(seg.covered_max_episode(object()), 0)
        self.assertEqual(seg.covered_episodes(object()), set())


class TestPremature(unittest.TestCase):
    NOW = datetime.datetime(2026, 9, 7, 12, 0, 0)

    def _call(self, **kw):
        base = dict(media_type="电视剧", manual_total_episode=False,
                    subscribe_created=datetime.datetime(2026, 9, 5),
                    tmdb_status="Returning Series",
                    latest_episode_air_date=datetime.date(2026, 9, 6),
                    now=self.NOW)
        base.update(kw)
        return seg.is_premature_completion(**base)

    def test_hit(self):
        hit, reason, judge = self._call()
        self.assertTrue(hit)
        self.assertIn("未完结", reason)
        self.assertEqual(judge["anchor_src"], "订阅创建")
        self.assertEqual(judge["age_days"], 2)
        self.assertEqual(judge["gap_days"], 1)

    def test_movie_skip(self):
        self.assertFalse(self._call(media_type="电影")[0])

    def test_manual_skip(self):
        self.assertFalse(self._call(manual_total_episode=True)[0])

    def test_grace_expired(self):
        created = self.NOW - datetime.timedelta(days=15)
        self.assertFalse(self._call(subscribe_created=created)[0])

    def test_ended(self):
        self.assertFalse(self._call(tmdb_status="Ended")[0])

    def test_no_air_date(self):
        self.assertFalse(self._call(latest_episode_air_date=None)[0])

    def test_stale_air(self):
        stale = datetime.date(2026, 8, 1)  # 37 天前
        self.assertFalse(self._call(latest_episode_air_date=stale)[0])

    def test_presubscribe_late_premiere(self):
        """提前 2 个月订阅未开播剧，开播 5 天后误结项：首播日锚点须命中。"""
        created = self.NOW - datetime.timedelta(days=65)   # 订阅很老
        first_air = datetime.date(2026, 9, 2)              # 5 天前才开播
        hit, reason, judge = self._call(subscribe_created=created,
                                        series_first_air_date=first_air,
                                        latest_episode_air_date=datetime.date(2026, 9, 6))
        self.assertTrue(hit)
        self.assertIn("剧集首播", reason)
        self.assertEqual(judge["anchor_src"], "剧集首播")

    def test_presubscribe_old_series_still_expired(self):
        """订阅老且剧首播也老（无首播日时退回订阅创建锚点）：仍超宽限放行。"""
        created = self.NOW - datetime.timedelta(days=65)
        hit, reason, judge = self._call(subscribe_created=created)  # 不传首播日
        self.assertFalse(hit)
        self.assertIn("订阅创建", reason)

    def test_first_air_earlier_than_subscribe(self):
        """老剧新订阅：首播日早于创建，锚点取创建时间，正常保护。"""
        hit, reason, judge = self._call(series_first_air_date=datetime.date(2026, 1, 1))
        self.assertTrue(hit)
        self.assertIn("订阅创建", reason)


class TestPruneLogs(unittest.TestCase):
    NOW = datetime.datetime(2026, 9, 16, 10, 0, 0)

    def _entry(self, days_ago: int):
        t = self.NOW - datetime.timedelta(days=days_ago)
        return {"time": t.strftime("%Y-%m-%d %H:%M:%S"), "action": "放行结项"}

    def test_time_window(self):
        logs = [self._entry(d) for d in (0, 30, 89, 91, 200)]
        kept = seg.prune_guard_logs(logs, now=self.NOW)
        self.assertEqual(len(kept), 3)  # 0/30/89 天内保留
        self.assertEqual(kept[0]["time"], self._entry(0)["time"])  # 原顺序尾部语义

    def test_count_cap(self):
        logs = [self._entry(0) for _ in range(600)]
        kept = seg.prune_guard_logs(logs, now=self.NOW, keep_max=500)
        self.assertEqual(len(kept), 500)
        self.assertEqual(kept, logs[-500:])  # 保新丢旧

    def test_bad_entries_dropped(self):
        logs = [{"no_time": 1}, "not-a-dict", None, self._entry(0)]
        kept = seg.prune_guard_logs(logs, now=self.NOW)
        self.assertEqual(len(kept), 1)

    def test_empty(self):
        self.assertEqual(seg.prune_guard_logs([], now=self.NOW), [])
        self.assertEqual(seg.prune_guard_logs(None, now=self.NOW), [])


class TestNoiseReasons(unittest.TestCase):
    def test_noise_prefixes_match_judge_reasons(self):
        """NOISE_PASS_REASONS 必须与判定函数实际文案前缀一致（防改文案悄悄漏噪声）。"""
        now = datetime.datetime(2026, 9, 7, 12, 0)
        _, r1, _ = seg.is_premature_completion(
            media_type="电影", manual_total_episode=False,
            subscribe_created=now, tmdb_status=None,
            latest_episode_air_date=None, now=now)
        _, r2, _ = seg.is_premature_completion(
            media_type="电视剧", manual_total_episode=True,
            subscribe_created=now, tmdb_status=None,
            latest_episode_air_date=None, now=now)
        self.assertTrue(r1.startswith(seg.NOISE_PASS_REASONS))
        self.assertTrue(r2.startswith(seg.NOISE_PASS_REASONS))


class TestRaise(unittest.TestCase):
    def test_at_boundary(self):
        self.assertTrue(seg.should_raise_total(current_total=4, covered_max=4))

    def test_below(self):
        self.assertFalse(seg.should_raise_total(current_total=20, covered_max=4))

    def test_zero_total(self):
        self.assertFalse(seg.should_raise_total(current_total=0, covered_max=0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
