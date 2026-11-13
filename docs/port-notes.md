# 移植笔记

记录 386 → 380 移植中真正花时间的部分。所有结论都在 R6300V2 上实测过，不是推断。

## 1. 两代软件中心的协议差异

| 能力 | 386（httpdb 1.5） | 380（1.0 软件中心） |
|---|---|---|
| 读配置 | `GET /_api/ss` | `GET /dbconf?p=ss`，无需认证，声明 `window.db_ss` 为普通对象 |
| 写配置 | `POST /_api/` JSON-RPC | `POST /applydb.cgi?p=ss` |
| 执行命令 | `POST /_api/` + method | 同上，带 `SystemCmd=<脚本>`，httpd 有白名单校验 |
| 临时文件 | `/_temp/<id>` | **无此路由**，且 httpd 只放行 `.js` / `.xml` 扩展名 |
| 软件中心首页 | `/Module_Softcenter.asp` | `/Main_Soft_center.asp` |
| 模块注册 | `softcenter_module_X_install="1"` | `="4"`，另需 `_home_url` |

最容易踩的一条：**`/applydb.cgi?p=X` 只写以 `X` 为前缀的键**。
一次提交里混着 `ssconf_basic_*`、`ss_acl_*`、`ss_*` 时，只有 `p=ss` 那一组会落库，
其余静默丢弃。所以桥接层必须按键前缀分组，多个 POST，且带 `SystemCmd` 的组最后发。

## 2. 380 的 BusyBox ash 有四个坑

1. **没有 `command` 内建**（上游脚本 42 处 `command -v` 全部失败，安装第一步就退出）。
   修法：向 `/koolshare/scripts/base.sh` 幂等追加 `command()` 函数，覆盖内建语义；
   所有 source base.sh 的脚本一次全好。
2. **不展开 alias**。`alias echo_date=...` 在脚本里等于没写。修法：改成同名函数。
3. **脚本注释里不能出现单引号**。`printf "%.0f"  # awk's %d folds` 会提前闭合引号，
   报 `unexpected word (expecting ")")`。这个错不会指向真实行号，非常费时。
4. **`read` 遇 NUL 截断**。读 `/proc/<pid>/cmdline`（NUL 分隔）只能拿到第一个字段，
   必须 `tr '\000' ' '` 之后再读。

另外：`curl-fancyss` 是静态链接的老版本，在 380 的 libcurl 7.54.1 上解析
`curl_mime_free` 失败，必须换成系统 `/usr/bin/curl`。

## 3. 采样架构为什么长这样

改造前：前端每 1 秒发一次 `/_api/`，后端每次**重跑一遍** `ss_dashboard.sh`——
单次 8 秒、13 个 fork（`pidof`/`dbus`/`base64`/`sed`/`awk`/`jq`/`curl`）。
前端 XHR 8 秒超时后重发，于是同时跑着 4–8 份，显示的数据其实是 8 秒前生成的。

现在的分工：

- **主循环 1 Hz，只用 shell 内建**读 `/proc/uptime`、`/proc/stat`、`/proc/<pid>/status`，
  `usleep` 对齐整秒，只 fork `pidof`（每 5 tick 一次）和发布用的 `mv`。
- **流量采集独立成守护**。`xray /debug/vars` 单次响应要 0.2–1.4 秒（15 次采样：
  200/300/400/400/500/900/1000/1200/1400 ms），放进 1 秒循环必然拖垮节拍。
  它把计数器和取样时刻一起写盘，前端按真实间隔差分，计数器没推进就重画上一点。
- **快照写 tmpfs**。一开始写到 JFFS 的 `/koolshare/res`，每秒一次把 `mtdblock5`
  拖成 D 状态、load 冲到 8.8。改成挂在 `/koolshare/res/xwt_tmp` 的 4 MB tmpfs。
- **单写者**。bridge 和 CGI 薄壳都会在快照过期时拉起守护，冷启动会双开；
  用 `flock -n` 让输的那个在碰发布文件之前退出。
- **子进程必须 `9>&-`**。流量守护是主守护 fork 出来的，会继承 flock 的文件描述符，
  主守护被 kill 后孤儿仍占着写锁，新实例永远起不来——表现为“面板永久冻结”。

## 4. mtdblock3 归因

现象：只要开着插件面板，`mtdblock3`（squashfs 即 `/dev/root` 的块设备线程）就到 40–76%。

排查过程值得记，因为我第一次判断错了：

1. 先测“把插件后台进程全停”——仍然 48%，于是误判为固件基线。
2. 用户指出“只有开着 XWT 才升高”。改测**关掉页面**：`mtdblock3` = 0 ticks/10s。归因成立。
3. 真正原因：面板每秒一次 `/_api/` → httpd 校验 `SystemCmd` → **fork 一个 shell**。
   每次 exec 都要从 NAND 重新读约 490 KB 的 busybox（这台机只有 ~8 MB page cache），
   读出量正好对上 `/proc/diskstats` 里的 ~280 KB/s。
4. 修法不是调参数，是把这条路砍掉：轮询直接读 tmpfs 文件，每 30 次才走一次真 RPC 兜底
   （保证守护挂了还能被拉起来）。`mtdblock3` 46% → 2.7%，page cache 反而涨到 26 MB。

教训：**先做“关掉它就不高”的对照，再归因**。否则很容易把交互开销算成平台基线。

## 5. 数据层的三个 bug

1. **非法 JSON**：重写输出时漏了 `cpus` 变量，拼出 `"cpus":,`，前端直接报“状态数据读取失败”。
2. **32 位截断**：`awk printf "%d"` 在 2 GB 以上折成 `2147483647`。下载量超过 2 GB 就撞上，
   改 `%.0f`。
3. **jq 静默空输出**：`jq -r --slurpfile n f '...'` 必须配 `-n`，否则 filter 没有输入可跑，
   退出码 0、输出为空，看起来像“配置文件读不到”。

## 6. 没有做的事

- **`/koolshare/bin` 里 hysteria 4.5 MB、anytls 1.7 MB、smartdns 1.3 MB、obfs-local 0.36 MB**
  当前 vless 链路不用，但脚本都引用着，删了就没法切 hy2/anytls 节点。
  它们只占 JFFS，不占运行内存。
- **xray 的 `GOMEMLIMIT`**：能把 RSS 从 50 MB 压到 45 MB，但 GC 更频繁；
  在 800 MHz 双核上多出的 CPU 比省下的 5 MB 更亏。
- **一个包同时支持 380/386**：两代是协议级差异，运行时探测两套 API 会让两条路径都变脆。
