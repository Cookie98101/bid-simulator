# 投标报价模拟器

一个基于固定评标规则的投标报价模拟工具。

此外，仓库内还提供了一个剑鱼标讯项目采集脚本，用于批量归并同一项目的公告、开标记录和中标结果。

## 功能

- 输入控制价、平均下浮率、主流下浮区间、竞争家数和随机系数范围
- 按固定规则模拟多轮竞争报价
- 输出推荐下浮率、推荐报价和候选结果排名

评标规则：

1. 剔除一个最高价
2. 剔除一个最低价
3. 剩余报价取平均
4. 平均值乘随机系数得到基准价
5. 最接近基准价者中标

## 本地运行

默认启动桌面界面：

```bash
python bid_simulator.py
```

命令行模式：

```bash
python bid_simulator.py --cli --simulations 1000
```

## Windows 打包

项目内置 GitHub Actions，会在 `push` 到主分支后自动构建 Windows 打包产物。

生成产物位于 Actions Artifacts，文件名为：

- `bid-simulator-windows`
- `jianyu-desktop-windows`

运行方式：

- `bid_simulator.exe`
- `jianyu-desktop-windows` 产物是一个 zip，解压后运行其中的 `jianyu_desktop.exe`

## 剑鱼标讯项目采集

脚本文件：

- `jianyu_project_collector.py`

用途：

- 使用登录态 Cookie 调用剑鱼标讯搜索接口
- 支持直接从西藏分站公开列表页稳定抓取结果，不依赖 `searchList`
- 支持把西藏地市公开分页一并纳入发现源与回补源
- 支持按项目当前缺口类型做定向回补：
  - 缺 `开标记录` 时优先追开标类页面
  - 缺 `中标结果` 时优先追结果类页面
  - 缺 `招标公告` 时优先追公告类页面
- 支持 `--source-mode area_listing` 强制只走公开分站列表，不先碰 `searchList`
- 支持 `--input-urls-json` 输入同一项目的详情链接种子集合，直接走详情闭环模式
- 支持 `--input-json` 输入标准化记录种子集合，并继续做同项目回补
- 支持 `--input-dir` 直接读取本地详情页目录，离线归并同一项目文件
- 支持“混合输入包”：同一目录里同时放本地详情 HTML、链接种子 JSON、Markdown 链接清单，脚本会按同一链接自动合并去重
- 归并同一项目的多条公告
- 识别 `招标公告 / 开标记录 / 中标结果`
- 输出项目级 JSON，标记哪些项目具备 3 类核心文件
- 自动额外生成一份“客户版 JSON”，只保留中文字段、中文状态和核心数字，便于直接发给客户
- 支持从结果链接种子继续回搜同项目其他文件
- `file_complete` 现在要求同时满足：
  - 已拿到 `招标公告 + 开标记录 + 中标结果`
  - 且核心字段齐全：`控制价 + 中标价 + 中标单位 + 全体报价`

当前限制：

- 公开分站分页规则已修复，现在可以继续抓第 2 页及后续页，不会只停在第一页
- 如果剑鱼 `searchList` 二次搜索触发验证码，脚本仍可抓取你手头已有的详情链接并解析正文
- 但“自动补齐同项目的招标公告/开标记录/中标结果”这一步会被验证码拦截
- 修复分页后，公开分站列表覆盖明显增加，但近 30 天西藏房建公开列表样本里仍然几乎没有 `开标记录`
- 现在已把 `lasa / changdou / rikaze / linzhi / shannan` 这类西藏地市公开分页并入发现源
- 这条路径已在真实样本上补回 `中标候选人` 和 `开标记录`
- 也就是说，正文解析和字段抽取已经可用，真正的不稳定点仍然是“如何稳定拿齐同项目完整文件集合”
- 默认时间窗口现在按“最近 30 天”动态生成，不再写死时间戳

当前已验证的可用闭环：

- `详情链接种子模式`
- `本地详情目录模式`
- 只要输入同一项目的 `开标记录 + 中标结果` 详情链接，脚本已能稳定输出：
  - 控制价
  - 中标价
  - 中标单位
  - 全体报价
  - 最高/最低/平均/中标下浮率

说明：

- `招标公告` 仍然有价值，主要补控制价和项目信息
- 但在部分真实项目里，`开标记录` 已含控制价，因此 `开标记录 + 中标结果` 已足够形成核心分析闭环
- 当前最新实测表明：
  - `城市公开页 + 全区频道页` 联合回补后
  - 可以在真实项目上补回 `开标记录 + 中标候选人 + 中标结果`
  - 但 `招标公告` 仍可能缺失，因此不能把当前链路误认为“严格三文件全自动闭环”
  - 对批量公开样本来说，`开标记录` 仍然是最稀缺的核心文件
  - 这意味着当前最现实的目标不是“所有项目凑齐三文件”，而是优先筛出能补到 `开标记录 + 中标结果` 的项目

示例：

```bash
python jianyu_project_collector.py \
  --keywords 房建 \
  --province 西藏 \
  --industry 建筑工程 \
  --cookie-file /tmp/jianyu_cookie.txt \
  --output xizang_projects.json
```

桌面界面：

```bash
python jianyu_desktop.py
```

稳定列表流示例：

```bash
python jianyu_project_collector.py \
  --cookie-file /tmp/jianyu_cookie.txt \
  --source-mode area_listing \
  --keywords 房建 \
  --province 西藏 \
  --industry 建筑工程 \
  --discover-channels jzgc \
  --backfill-discover-channels jzgc \
  --max-pages 5 \
  --backfill-pages 20 \
  --fetch-details \
  --output xizang_projects.json \
  --report-md xizang_projects.md
```

当前更准确的使用建议：

- 想稳定抓近 30 天公开项目列表，用 `--source-mode area_listing`
- 想真正拿到控制价 + 中标价 + 全体报价闭环，优先提供同一项目的详情链接种子，再让脚本解析正文
- 如果你已经把同一项目的详情页保存到本地目录，优先用 `--input-dir`，这条路径不依赖实时搜索接口
- 如果你手头资料不整齐，优先整理成“混合输入包目录”再跑 `--input-dir`
- 想追求完全自动补齐同项目三文件，目前仍会受 `searchList` 验证码影响，不能当作稳定生产链路
- 主输出仍然是程序内部完整 JSON；同时会自动生成一个同目录的 `xxx_客户版.json`

详情链接种子闭环示例：

```bash
python jianyu_project_collector.py \
  --input-urls-json /Users/gejian/building/tmp/jindong_seed_urls.json \
  --cookie-file /tmp/jianyu_cookie.txt \
  --fetch-details \
  --output jindong_seed_output.json \
  --report-md jindong_seed_output.md
```

本地详情目录闭环示例：

```bash
python jianyu_project_collector.py \
  --input-dir /Users/gejian/building/tmp/jindong_bundle \
  --output jindong_input_dir.json \
  --report-md jindong_input_dir.md
```

混合输入包闭环示例：

```bash
python jianyu_project_collector.py \
  --input-dir /Users/gejian/building/tmp/mixed_bundle_test \
  --output mixed_bundle_test_output.json \
  --report-md mixed_bundle_test_output.md
```

混合输入包目录里可以同时放：

- 同项目详情页 HTML，例如 `开标记录.html`、`中标结果.html`
- `seed_urls.json` 这类链接种子文件
- `.md` 链接清单

脚本会做两件事：

- 按同一详情链接自动去重，不会因为重复来源把记录拆成多份
- 如果同一链接既有简版 URL 种子又有完整 HTML 正文，会保留更丰富的 HTML 解析结果

适用场景：

- 同一项目的 `招标公告 / 开标记录 / 中标结果` 页面已经由人工或别的程序保存到本地
- 不希望再依赖 `searchList` 或验证码
- 同一个项目的材料来源不统一，有的只有链接，有的已经保存成 HTML，需要统一归并

推荐的种子 JSON 结构：

```json
[
  {
    "url": "https://xizang.jianyu360.cn/jybx/xxxx.html",
    "title": "项目中标结果公告",
    "notice_type": "中标结果",
    "project_name": "某项目",
    "bid_number": "S1407..."
  },
  {
    "url": "https://xizang.jianyu360.cn/jybx/yyyy.html",
    "title": "项目开标记录",
    "notice_type": "开标记录",
    "project_name": "某项目"
  },
  {
    "url": "https://xizang.jianyu360.cn/jybx/zzzz.html",
    "title": "项目招标公告",
    "notice_type": "招标公告",
    "project_name": "某项目",
    "bid_number": "S1407..."
  }
]
```

如果希望严格只认 `招标公告 + 开标记录 + 中标结果` 三文件完整项目，可以加：

```bash
--strict-three-files
```

补充说明：

- 不加 `--strict-three-files` 时，只要 `开标记录 + 中标结果` 已能推出控制价、中标价、全体报价，脚本会标记为 `核心可分析`
- 加了 `--strict-three-files` 后，必须同时存在 `招标公告 + 开标记录 + 中标结果` 才会记为可分析
- 输出里的 `backfill_direct_matches / backfill_coarse_candidates / backfill_detail_verified` 用来观察公开列表回补效果
- 输出里的 `targeted_backfill_projects / targeted_backfill_missing_open_projects / targeted_backfill_recovered_*` 用来观察“按缺口定向回补”效果
- 输出里的 `followup_seed_generated / followup_seed_original_url_generated / followup_seed_related_link_generated / followup_verified_kept / followup_filtered_out` 用来观察“详情页追踪种子”效果
- 输出里的 `file_complete` 代表严格完整项目
- 输出里的 `can_analyze_core` 代表即使没拿到招标公告，也已经能算控制价、中标价、全体报价
- 输出里的 `meta.audit` 会直接给出：
  - `complete_projects`
  - `core_ready_incomplete_projects`
  - `missing_notice_projects`
  - `missing_open_projects`
  - `missing_result_projects`
  - `missing_core_field_projects`
  - `issue_counts`
- `report-md` 生成的 Markdown 里也会新增“完整性审计”章节，便于直接筛完整项目

当前关于详情页追踪种子的更准确结论：

- 脚本现在会从详情页里提取两类追踪线索：
  - `original_url`
  - `related_links`
- 其中 `related_links` 不再直接并入项目，而是先抓详情再做“同项目校验”
- 同项目校验主要基于：
  - 项目名重叠度
  - 招标编号匹配
- 这避免了把右侧“西藏热门招标”误并入当前项目
- 但真实样本 `金东边境检查站业务技术用房建设项目` 的验证结果表明：
  - 清理假原文链接后，共生成追踪种子 `10` 个
  - 其中 `original_url` 0 个、`related_link` 10 个
  - 最终新增有效记录 `0` 个
  - `related_link` 候选被过滤 `10` 个
- 说明这条追踪链路现在已经“可控”，并且当前这批真实页面里并没有暴露出可自动补齐缺失文件的有效外链

也可以直接设置环境变量：

```bash
export JY_COOKIE='你的cookie'
python jianyu_project_collector.py --output xizang_projects.json
```

验证码探测：

```bash
python jianyu_project_collector.py \
  --probe-search-captcha \
  --cookie-file /tmp/jianyu_cookie.txt \
  --captcha-image-out /tmp/jianyu_search_captcha.png
```
