# MoviePilot-Plugins (totobo)

MoviePilot 第三方插件仓库：自研插件 + 对上游插件的二开修复版。
兼容 MoviePilot v2 插件市场机制：在 MP「设定 → 自定义插件仓库」添加本仓库地址即可：

```
https://github.com/totobo/MoviePilot-Plugins
```

## 插件列表

### SubscribeEpisodeGuard — 订阅集数守卫（自研，v1.0.0）
解决：**周更剧刚开播时 TMDB 季集数不全，订阅按旧集数下完即被误判"完成"结项，
之后 TMDB 涨集数但已完成订阅无复活机制，缺集永远漏追。**

- B 主防线：挂官方链事件 `SubscribeCompletionCheck`（结项前一票否决）与
  `SubscribeEpisodesRefresh`（precheck +1 兜底），命中"疑似过早完成"规则时阻止结项，
  订阅继续存活等 TMDB 追平；
- C 保险丝：每日 09:30 巡检近 N 天已结项电视剧订阅，比对 TMDB 当前集数，
  集数上涨仍有缺口 → 通知，可选自动重新订阅；
- 默认**观察模式**（只记日志不干预），确认判定无误后在配置里切生效模式；
- 保守判定：仅电视剧、非手动集数、宽限期内、TMDB 未完结、最新集在播窗口内才拦截；
- 要求 MoviePilot >= v2.15.0；回滚 = 界面禁用插件，零数据改动。

### SubscribeGroupFix — 订阅规则自动填充·修复版（二开 thsrite/SubscribeGroup v2.8.7）
上游功能（下载后固化订阅条件 / 按二级分类预填规则），修复两处：

1. **死规则修复**（上游 issue：[thsrite/MoviePilot-Plugins#364](https://github.com/thsrite/MoviePilot-Plugins/issues/364)）：
   上游把识别词**替换后**的占位符文本（如 `60FPS→60帧`）拼进 include 正则，
   而订阅过滤匹配的是种子**原始标题**（只有 `60FPS`），规则永不命中，订阅静默停摆。
   本修复对候选正则先用原始种子标题自匹配校验，不通过则降级为仅制作组名并告警。
2. **多季固化**：固化去重键从 `类型:tmdbid` 改为 `类型:tmdbid:季号`，多季剧第二季可正常填充。

> 与原版 `SubscribeGroup` 配置前缀、类名、目录均不同，可共存，但建议**卸载原版后再装本修复版**，避免双插件同时固化互相覆盖。

## 目录结构（MP 市场标准）

```
├── package.v2.json                  # v2 插件索引
├── plugins.v2/
│   ├── subscribegroupfix/           # SubscribeGroupFix
│   │   └── __init__.py
│   └── subscribeepisodeguard/       # SubscribeEpisodeGuard
│       └── __init__.py
└── icons/
```

## 开发 / 发布

- 目录名 = 插件类名小写（MP 加载器要求）
- 索引 `package.v2.json` 中 key 为插件 ID（类名）
- 版本号变更写进对应插件的 `history`
- SubscribeEpisodeGuard 附单元测试：
  `python3 tests/test_guard.py`（stub MP 依赖，本地直接可跑）

## License

- `subscribegroupfix` 衍生自 [thsrite/MoviePilot-Plugins](https://github.com/thsrite/MoviePilot-Plugins)（GPL-3.0），沿用 GPL-3.0；
- `subscribeepisodeguard` 同为 GPL-3.0。
