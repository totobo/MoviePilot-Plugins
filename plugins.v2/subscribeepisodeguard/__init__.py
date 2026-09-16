"""
SubscribeEpisodeGuard（订阅集数守卫）v1.0.0

问题：周更剧订阅时 TMDB 集数尚未更新全，MoviePilot 按旧总集数误判"已下完"提前
     结项（写历史+删订阅）；之后 TMDB 涨集数，已完成订阅没有复活机制，缺集无人追。

双保险：
  实时保护（仅使用官方链事件扩展口，不 monkey-patch 主程序）：
    结项急刹 SubscribeCompletionCheck：订阅结项前一票否决（cancel=True），命中
        "疑似过早完成"规则时阻止结项，订阅继续存活等 TMDB 追平。
    集数缓兵 SubscribeEpisodesRefresh(scene=precheck)：完成判定前 total_episode +1
        兜底抬高（默认关闭，与结项急刹二选一）。
  每日巡检（官方 get_service 注册的每日 cron）：
    扫近 N 天已完成的电视剧订阅历史，比对 TMDB 当前季集数：上涨且仍有缺口
    → 通知；auto_resubscribe=true 时自动重建订阅（默认关）。

Requires: MoviePilot >= v2.15.0
"""
import datetime
from typing import Any, Dict, List, Optional, Tuple

from apscheduler.triggers.cron import CronTrigger

from app.core.context import MediaType
from app.core.event import Event, eventmanager
from app.core.module import ModuleManager
from app.db.models.subscribehistory import SubscribeHistory
from app.db.subscribe_oper import SubscribeOper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import ChainEventType, EventType, NotificationType
from app.schemas.event import (SubscribeCompletionCheckEventData,
                               SubscribeEpisodesRefreshEventData)

PLUGIN_SOURCE = "SubscribeEpisodeGuard"

# 视为"不再更新"的 TMDB 状态
FINISHED_STATUSES = {"ended", "cancelled"}


# ---------------------------------------------------------------------------
# 纯判定函数（无 MP 运行时依赖，可独立单测）
# ---------------------------------------------------------------------------

def parse_dt(value: Any) -> Optional[datetime.datetime]:
    """宽容解析订阅表里的时间字符串。"""
    if isinstance(value, datetime.datetime):
        return value
    if isinstance(value, str):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.datetime.strptime(value.strip(), fmt)
            except ValueError:
                continue
    return None


def parse_date(value: Any) -> Optional[datetime.date]:
    if isinstance(value, datetime.datetime):
        return value.date()
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        try:
            return datetime.datetime.strptime(value.strip(), "%Y-%m-%d").date()
        except ValueError:
            return None
    return None


def covered_max_episode(subscribe: Any) -> int:
    """订阅事实（note / episode_priority）里已覆盖的最高集号。"""
    max_ep = 0
    for e in (getattr(subscribe, "note", None) or []):
        try:
            max_ep = max(max_ep, int(e))
        except (TypeError, ValueError):
            continue
    for k in (getattr(subscribe, "episode_priority", None) or {}):
        try:
            max_ep = max(max_ep, int(k))
        except (TypeError, ValueError):
            continue
    return max_ep


def covered_episodes(item: Any) -> set:
    """已覆盖集号集合：优先 episode_priority（下载事实），退回 note。"""
    out = set()
    eprio = getattr(item, "episode_priority", None) or {}
    if isinstance(eprio, dict):
        for k, v in eprio.items():
            try:
                if v and int(float(v)) > 0:
                    out.add(int(k))
            except (TypeError, ValueError):
                continue
    note = getattr(item, "note", None) or []
    if isinstance(note, (list, tuple, set)):
        for e in note:
            try:
                out.add(int(e))
            except (TypeError, ValueError):
                continue
    return out


def is_premature_completion(*,
                            media_type: str,
                            manual_total_episode: bool,
                            subscribe_created: Optional[datetime.datetime],
                            tmdb_status: Optional[str],
                            latest_episode_air_date: Optional[datetime.date],
                            now: Optional[datetime.datetime] = None,
                            grace_days: int = 14,
                            recent_ep_days: int = 21) -> Tuple[bool, str]:
    """
    是否疑似"过早完成"。全部条件满足才 True；任一不满足保守放行结项。
    media_type 与 Subscribe.type 存值一致，电视剧为 '电视剧'。
    """
    now = now or datetime.datetime.now()
    if media_type != "电视剧":
        return False, "非电视剧订阅"
    if manual_total_episode:
        return False, "用户手动指定总集数，不干预"
    if not subscribe_created:
        return False, "订阅创建时间缺失，保守放行"
    age_days = (now - subscribe_created).days
    if age_days > grace_days:
        return False, f"订阅已创建 {age_days} 天，超宽限期 {grace_days} 天"
    if (tmdb_status or "").strip().lower() in FINISHED_STATUSES:
        return False, f"TMDB 已标记完结（{tmdb_status}）"
    if latest_episode_air_date is None:
        return False, "无最新集首播日期，保守放行"
    gap = (now.date() - latest_episode_air_date).days
    if gap > recent_ep_days:
        return False, f"最新集已播 {gap} 天（>{recent_ep_days}），不像仍在更新"
    return True, (f"订阅创建 {age_days} 天内、TMDB 未完结、最新集 {gap} 天前刚播，"
                  f"当前总集数可能未更新全")


def should_raise_total(*, current_total: int, covered_max: int) -> bool:
    """已覆盖最高集号追平 TMDB 总集数 → 处于完成临界点。"""
    return current_total > 0 and covered_max >= current_total


# ---------------------------------------------------------------------------
# 插件主体
# ---------------------------------------------------------------------------

class SubscribeEpisodeGuard(_PluginBase):
    # 插件名称
    plugin_name = "订阅集数守卫"
    # 插件描述
    plugin_desc = "防止周更剧因 TMDB 集数滞后被订阅提前结项：否决过早完成 + 每日巡检已结项订阅，告警/自动复活。"
    # 插件图标
    plugin_icon = "subscribe.png"
    # 插件版本
    plugin_version = "1.0.1"
    # 插件作者
    plugin_author = "totobo"
    # 作者主页
    author_url = "https://github.com/totobo/MoviePilot-Plugins"
    # 插件配置项ID前缀
    plugin_config_prefix = "subscribeepisodeguard_"
    # 加载顺序
    plugin_order = 99
    # 加载类型
    autostart = True

    # 运行参数（__init__ 中给默认值，__update_config 刷新）
    _enabled = True
    _mode = "observe"
    _grace_days = 14
    _recent_ep_days = 21
    _enable_completion_veto = True
    _enable_refresh_raise = False
    _sweep_enable = True
    _sweep_days = 30
    _auto_resubscribe = False
    _notify_user = ""
    _exclude_keywords = ""

    _tmdb_module = None
    _last_manual_report = ""

    def init_plugin(self, config: dict = None):
        if config:
            self.__update_config(config)

    def __update_config(self, config: Dict[str, Any]):
        """更新配置：官方 '已保存' 事件模式，配置变化即生效。"""
        self._enabled = config.get("enabled") is not False
        self._mode = config.get("enabled_mode") or "observe"
        try:
            self._grace_days = max(1, int(config.get("grace_days") or 14))
            self._recent_ep_days = max(1, int(config.get("recent_ep_days") or 21))
            self._sweep_days = max(1, int(config.get("sweep_days") or 30))
        except (TypeError, ValueError):
            pass
        # 实时保护方式：veto 结项急刹 / raise 集数缓兵 / both / none（默认 veto）
        intervention = (config.get("intervention") or "veto").strip().lower()
        if intervention not in ("veto", "raise", "both", "none"):
            intervention = "veto"
        self._enable_completion_veto = intervention in ("veto", "both")
        self._enable_refresh_raise = intervention in ("raise", "both")
        self._sweep_enable = config.get("sweep_enable") is not False
        self._auto_resubscribe = config.get("auto_resubscribe") is True
        self._notify_user = config.get("notify_user") or ""
        self._exclude_keywords = config.get("exclude_keywords") or ""
        logger.info(f"订阅集数守卫配置更新：enable={self._enabled} mode={self._mode} "
                    f"grace={self._grace_days}d veto={self._enable_completion_veto} "
                    f"raise={self._enable_refresh_raise} sweep={self._sweep_enable}/{self._sweep_days}d "
                    f"auto_resub={self._auto_resubscribe}")

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self):
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        """注册公共定时服务：每日 09:30 巡检已结项订阅。"""
        if self._enabled and self._sweep_enable:
            return [
                {
                    "id": "SubscribeEpisodeGuardSweep",
                    "name": "订阅集数守卫：已结项订阅巡检",
                    "trigger": CronTrigger.from_crontab("30 9 * * *"),
                    "func": self._sweep_job,
                    "kwargs": {}
                }
            ]
        return []

    # ------------------------------------------------------------------
    # TMDB 工具
    # ------------------------------------------------------------------

    def _tmdb(self):
        """惰性获取 TMDB 模块（官方 ModuleManager）。"""
        if self._tmdb_module is None:
            try:
                self._tmdb_module = ModuleManager().get_module("TheMovieDbModule")
            except Exception as e:
                logger.warning(f"订阅集数守卫：TMDB 模块获取失败：{e}")
        return self._tmdb_module

    def _season_detail(self, tmdbid: Optional[int], season: Optional[int]) -> Optional[dict]:
        if not tmdbid or season is None:
            return None
        tmdb = self._tmdb()
        if not tmdb:
            return None
        try:
            info = tmdb.tmdb_info(tmdbid=tmdbid, mtype=MediaType.TV, season=season)
            return info if isinstance(info, dict) else None
        except Exception as e:
            logger.debug(f"订阅集数守卫：TMDB 季详情失败 tmdb={tmdbid} S{season}: {e}")
            return None

    def _series_status(self, tmdbid: Optional[int]) -> Optional[str]:
        if not tmdbid:
            return None
        tmdb = self._tmdb()
        if not tmdb:
            return None
        try:
            info = tmdb.tmdb_info(tmdbid=tmdbid, mtype=MediaType.TV)
            return (info or {}).get("status")
        except Exception:
            return None

    @staticmethod
    def _latest_air_date(season_detail: Optional[dict]) -> Optional[datetime.date]:
        dates = [parse_date(ep.get("air_date")) for ep in ((season_detail or {}).get("episodes") or [])
                 if isinstance(ep, dict)]
        dates = [d for d in dates if d]
        return max(dates) if dates else None

    def _is_excluded(self, name: Optional[str]) -> bool:
        if not name or not self._exclude_keywords:
            return False
        return any(k.strip() and k.strip() in name for k in self._exclude_keywords.splitlines())

    # ------------------------------------------------------------------
    # 实时保护·结项急刹：否决过早完成（主挂载点）
    # ------------------------------------------------------------------

    @eventmanager.register(ChainEventType.SubscribeCompletionCheck)
    def on_completion_check(self, event: Event):
        if not self._enabled or not self._enable_completion_veto:
            return
        try:
            if not event or not event.event_data:
                return
            data: SubscribeCompletionCheckEventData = event.event_data
            subscribe = data.subscribe
            mediainfo = data.mediainfo
            if subscribe is None:
                return
            if self._is_excluded(getattr(subscribe, "name", None)):
                return
            season = getattr(subscribe, "season", None)
            tmdbid = getattr(subscribe, "tmdbid", None)
            detail = self._season_detail(tmdbid, season)
            tmdb_total = len((detail or {}).get("episodes") or [])
            if tmdb_total > (getattr(subscribe, "total_episode", 0) or 0):
                # TMDB 已涨集且主程序本轮会自行跟踪总集数，无需否决
                logger.debug(f"[守卫] 《{getattr(subscribe, 'name', '?')}》TMDB 现 {tmdb_total} 集"
                             f">订阅 {getattr(subscribe, 'total_episode', 0)} 集，交主程序跟踪")
                return
            premature, reason = is_premature_completion(
                media_type=str(getattr(subscribe, "type", "") or ""),
                manual_total_episode=bool(getattr(subscribe, "manual_total_episode", False)),
                subscribe_created=parse_dt(getattr(subscribe, "date", None)),
                tmdb_status=self._series_status(tmdbid)
                            or (getattr(mediainfo, "status", None) if mediainfo else None),
                latest_episode_air_date=self._latest_air_date(detail),
                grace_days=self._grace_days,
                recent_ep_days=self._recent_ep_days,
            )
            if not premature:
                logger.debug(f"[守卫] 《{getattr(subscribe, 'name', '?')}》完成放行：{reason}")
                return
            if self._mode == "observe":
                logger.info(f"[守卫·观察] 《{getattr(subscribe, 'name', '?')}》"
                            f"(id={getattr(subscribe, 'id', '?')}) 疑似过早完成（观察模式未否决）：{reason}")
                return
            data.cancel = True
            data.source = PLUGIN_SOURCE
            data.reason = reason
            logger.info(f"[守卫] 已否决《{getattr(subscribe, 'name', '?')}》"
                        f"(id={getattr(subscribe, 'id', '?')}) 的提前结项：{reason}")
            try:
                self.post_message(
                    mtype=NotificationType.Subscribe,
                    title=f"《{getattr(subscribe, 'name', '?')}》订阅结项被守卫拦截",
                    text=f"{reason}\n订阅将继续跟踪，TMDB 集数更新后自动补齐缺集。",
                    username=self._notify_user or getattr(subscribe, "username", None))
            except Exception:
                pass
        except Exception as e:
            # 守卫异常绝不影响主流程
            logger.warning(f"[守卫] SubscribeCompletionCheck 处理异常（已忽略）：{e}")

    # ------------------------------------------------------------------
    # 实时保护·集数缓兵：precheck +1 抬高（兜底挂载点，默认关）
    # ------------------------------------------------------------------

    @eventmanager.register(ChainEventType.SubscribeEpisodesRefresh)
    def on_episodes_refresh(self, event: Event):
        if not (self._enabled and self._enable_refresh_raise and self._mode != "observe"):
            return
        try:
            data: SubscribeEpisodesRefreshEventData = event.event_data
            if not data or getattr(data, "scene", None) != "precheck":
                return
            sid = getattr(data, "subscribe_id", None)
            if not sid:
                return
            subscribe = SubscribeOper().get(sid)
            if subscribe is None or self._is_excluded(subscribe.name):
                return
            if bool(getattr(subscribe, "manual_total_episode", False)):
                return
            if not should_raise_total(current_total=data.current_total_episode,
                                      covered_max=covered_max_episode(subscribe)):
                return
            created = parse_dt(getattr(subscribe, "date", None))
            if not created or (datetime.datetime.now() - created).days > self._grace_days:
                return
            data.updated = True
            data.total_episode = data.current_total_episode + 1
            data.source = PLUGIN_SOURCE
            logger.info(f"[守卫] precheck 抬高《{subscribe.name}》总集数 "
                        f"{data.current_total_episode} -> {data.total_episode}（宽限防误完成）")
        except Exception as e:
            logger.warning(f"[守卫] SubscribeEpisodesRefresh 处理异常（已忽略）：{e}")

    # ------------------------------------------------------------------
    # 每日巡检已结项订阅
    # ------------------------------------------------------------------

    def _sweep_job(self):
        try:
            findings = self.sweep_once(now=datetime.datetime.now())
        except Exception as e:
            logger.warning(f"[守卫·巡检] 异常（已忽略）：{e}")
            return
        if not findings:
            logger.debug("[守卫·巡检] 近期待查订阅无集数增长")
            return
        for f in findings:
            logger.info(f"[守卫·巡检] 《{f['name']}》结项时 {f['old_total']} 集 → "
                        f"TMDB 现 {f['new_total']} 集，缺 {f['missing']}")
        lines = [f"《{f['name']}》({f['year']}) S{f['season']}：结项时 {f['old_total']} 集 → "
                 f"现 {f['new_total']} 集，缺 {len(f['missing'])} 集"
                 f"（{'、'.join('E%02d' % e for e in f['missing'][:8])}"
                 f"{'…' if len(f['missing']) > 8 else ''}）" for f in findings]
        tail = "\n\n已自动重新订阅。" if self._auto_resubscribe else "\n\n可在 MP 重新订阅补齐，或开启自动复活。"
        try:
            self.post_message(
                mtype=NotificationType.Plugin,
                title=f"订阅守卫巡检：{len(findings)} 个订阅疑似提前结项",
                text="\n".join(lines) + tail,
                username=self._notify_user or None)
        except Exception as e:
            logger.warning(f"[守卫·巡检] 通知发送失败：{e}")
        if self._auto_resubscribe:
            for f in findings:
                self._resubscribe(f)

    def sweep_once(self, now: Optional[datetime.datetime] = None,
                   max_items: int = 200) -> List[dict]:
        """扫描近 _sweep_days 天完成的电视剧订阅历史，返回疑似提前结项清单。"""
        now = now or datetime.datetime.now()
        findings = []
        cutoff = now - datetime.timedelta(days=self._sweep_days)
        for item in self._list_finished_tv(count=max_items):
            try:
                if str(item.type or "") != "电视剧" or bool(item.manual_total_episode):
                    continue
                if self._is_excluded(item.name):
                    continue
                fin_time = parse_dt(item.date)
                if not fin_time or fin_time < cutoff:
                    continue
                detail = self._season_detail(item.tmdbid, item.season)
                if not detail:
                    continue
                new_total = len(detail.get("episodes") or [])
                old_total = item.total_episode or 0
                if new_total <= old_total:
                    continue
                missing = [e for e in range(1, new_total + 1) if e not in covered_episodes(item)]
                if not missing:
                    continue
                findings.append({
                    "hist_id": item.id, "name": item.name, "year": item.year,
                    "season": item.season, "tmdbid": item.tmdbid,
                    "old_total": old_total, "new_total": new_total,
                    "missing": missing, "username": item.username,
                })
            except Exception as e:
                logger.debug(f"[守卫·巡检] 单条处理失败 {getattr(item, 'name', '?')}: {e}")
        return findings

    @staticmethod
    def _list_finished_tv(count: int) -> List[Any]:
        """读订阅历史（模型公开查询接口，db_query 装饰器自动建会话）。"""
        try:
            return list(SubscribeHistory.list_by_type(mtype="电视剧", page=1, count=count) or [])
        except Exception as e:
            logger.warning(f"[守卫·巡检] 订阅历史读取失败：{e}")
            return []

    def _resubscribe(self, f: dict):
        try:
            oper = SubscribeOper()
            try:
                existing = oper.list_by_tmdbid(tmdbid=f["tmdbid"], season=f["season"])
                if existing:
                    logger.info(f"[守卫·巡检] 《{f['name']}》已存在活跃订阅"
                                f"(id={existing[0].id})，跳过复活")
                    return
            except Exception:
                pass
            tmdb = self._tmdb()
            if not tmdb:
                logger.warning(f"[守卫·巡检] 无 TMDB 模块，跳过复活《{f['name']}》")
                return
            mediainfo = tmdb.recognize_media(mtype=MediaType.TV, tmdbid=f["tmdbid"])
            if not mediainfo:
                logger.warning(f"[守卫·巡检] TMDB 识别失败，跳过复活《{f['name']}》")
                return
            season = f["season"]
            # 对齐历史订阅口径：本季已下载集写入 note，避免重复下载
            subscribeid, msg = oper.add(
                mediainfo=mediainfo,
                season=season,
                username=f.get("username") or "admin",
                note=f"{PLUGIN_SOURCE} 自动复活",
            )
            logger.info(f"[守卫·巡检] 《{f['name']}》已复活订阅 id={subscribeid} msg={msg}")
        except Exception as e:
            logger.warning(f"[守卫·巡检] 复活《{f['name']}》失败：{e}")

    # ------------------------------------------------------------------
    # 配置面板 / 命令
    # ------------------------------------------------------------------

    def get_page(self) -> List[dict]:
        """插件详情页：展示最近一次巡检结果。"""
        if not self._last_manual_report:
            return []
        return [
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {'cols': 12},
                        'content': [
                            {
                                'component': 'VAlert',
                                'props': {'type': 'info', 'variant': 'tonal'},
                                'text': self._last_manual_report
                            }
                        ]
                    }
                ]
            }
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {
                                        'model': 'enabled', 'label': '启用插件'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {
                                        'model': 'sweep_enable', 'label': '每日巡检（09:30）'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 4},
                                'content': [
                                    {'component': 'VSwitch', 'props': {
                                        'model': 'auto_resubscribe',
                                        'label': '巡检缺口自动重新订阅',
                                        'hint': '默认关，仅通知',
                                        'persistent-hint': True}}
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {'component': 'VSelect', 'props': {
                                        'model': 'enabled_mode',
                                        'label': '运行模式',
                                        'items': [
                                            {'title': '观察模式（只记日志不干预）', 'value': 'observe'},
                                            {'title': '生效模式（实际否决/抬高）', 'value': 'active'},
                                        ],
                                        'hint': '建议先观察 3 天，看日志确认判定正确后切生效',
                                        'persistent-hint': True}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 6},
                                'content': [
                                    {'component': 'VSelect', 'props': {
                                        'model': 'intervention',
                                        'label': '实时保护方式',
                                        'items': [
                                            {'title': '结项急刹（推荐：结项前一票否决）', 'value': 'veto'},
                                            {'title': '集数缓兵（完成判定前 +1 拖住）', 'value': 'raise'},
                                            {'title': '双保险（急刹+缓兵）', 'value': 'both'},
                                            {'title': '关闭实时保护（仅每日巡检）', 'value': 'none'},
                                        ],
                                        'persistent-hint': True}}
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {'component': 'VTextField', 'props': {
                                        'model': 'grace_days', 'label': '宽限期（天）',
                                        'type': 'number'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {'component': 'VTextField', 'props': {
                                        'model': 'recent_ep_days', 'label': '在播窗口（天）',
                                        'type': 'number'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {'component': 'VTextField', 'props': {
                                        'model': 'sweep_days', 'label': '巡检回溯（天）',
                                        'type': 'number'}}
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {'cols': 12, 'md': 3},
                                'content': [
                                    {'component': 'VTextField', 'props': {
                                        'model': 'notify_user', 'label': '通知用户',
                                        'placeholder': '留空=订阅所属用户'}}
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {'component': 'VTextarea', 'props': {
                                        'model': 'exclude_keywords',
                                        'label': '排除关键词（每行一个剧名）'}}
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {'cols': 12},
                                'content': [
                                    {'component': 'VAlert', 'props': {
                                        'type': 'info', 'variant': 'text',
                                        'text': '原理：周更剧刚开播时 TMDB 季集数常不全，订阅按旧集数下完即误结项。'
                                                '实时保护：在结项前识别疑似过早完成并阻止，订阅继续追更；'
                                                '每日巡检：回扫已结项订阅，集数上涨仍有缺口的通知/自动复活。要求 MP >= v2.15.0。'
                                                '立即巡检命令：/guardrun'}}
                                ]
                            }
                        ]
                    },
                ]
            }
        ], {
            "enabled": True,
            "enabled_mode": "observe",
            "intervention": "veto",
            "grace_days": 14,
            "recent_ep_days": 21,
            "sweep_enable": True,
            "sweep_days": 30,
            "auto_resubscribe": False,
            "notify_user": "",
            "exclude_keywords": "",
        }

    def get_command(self) -> List[Dict[str, Any]]:
        return [
            {
                "cmd": "/guardrun",
                "event": EventType.PluginAction,
                "desc": "订阅集数守卫：立即巡检一次",
                "category": "管理",
                "data": {"action": "subscribe_episode_guard_run"}
            }
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        return []

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Event):
        if not self._enabled or not event or not event.event_data:
            return
        try:
            event_data = event.event_data
            if event_data.get("action") != "subscribe_episode_guard_run":
                return
            channel = event_data.get("channel")
            username = event_data.get("username") or self._notify_user or None
            findings = self.sweep_once()
            if not findings:
                self.put_message(channel=channel, title="订阅守卫",
                                 text="手动巡检完成：未发现提前结项的订阅。",
                                 username=username)
                return
            text = "\n".join(
                f"《{f['name']}》S{f['season']}：{f['old_total']} → {f['new_total']} 集，"
                f"缺 {len(f['missing'])} 集" for f in findings)
            self.put_message(channel=channel, title=f"订阅守卫巡检：{len(findings)} 个疑似提前结项",
                             text=text, username=username)
            if self._auto_resubscribe:
                for f in findings:
                    self._resubscribe(f)
        except Exception as e:
            logger.warning(f"[守卫] 命令处理异常：{e}")

    # 面板按钮可调用的公开方法
    def guard_run_now(self):
        self._sweep_job()
        return {"status": "ok"}

    def put_message(self, channel=None, title=None, text=None, username=None):
        """统一出口：有渠道回渠道，否则系统消息+站内通知。"""
        try:
            if channel:
                self.post_message(channel=channel, mtype=NotificationType.Plugin,
                                  title=title, text=text, username=username)
            else:
                self.post_message(mtype=NotificationType.Plugin,
                                  title=title, text=text, username=username)
        except Exception as e:
            logger.warning(f"[守卫] 消息发送失败：{e}")
