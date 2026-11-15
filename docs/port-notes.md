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
2. **会展开 alias —— 而且这比"不展开"危险得多**。`ss_base.sh` 里有
   `alias echo_date='echo 【..】:'`，而 `base.sh` 在一次运行里会被 source 两次
   （`ss_conf.sh` → `ss_base.sh` → `base.sh` → 兼容层，然后
   `ss_node_common.sh` → `base.sh` → 兼容层又来一次）。第二次加载时，兼容层里的
   `echo_date() {` 被别名改写成 `echo 【..】: () {`，ash 直接报
   `syntax error: unexpected "("` 并**终止整个脚本**。
   表现是"点了没反应"，日志里只有一行语法错误。
   修法：兼容层文件第一行先 `unalias echo_date urldecode http_response command sha256sum`，
   这样它被 source 多少次都安全。
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

## 6. 更新链路：两个缺失的基础件 + 一个自己挖的坑

380 固件的基础设施比 386 老一截：**没有 `sha256sum`**（只有 `md5sum`），**CA 证书库停在 2017-09-14**。
两者都不报错，只是静默失败——所以最初看上去像"网络不通"：

- `sha256sum f | awk '{print $1}'` 返回空串，与清单里的值不等 → 判"校验失败"；
- `curl` 默认校验失败时配 `-f` 直接非零退出 → `fetch` 返回 1 → "下载失败"。

修法：`base.sh` 注入 `sha256sum()`（走 `openssl dgst -sha256`，这台机的 openssl 是 1.0.2n，够用），
并把包内的 `res/cacert.pem` 通过 `export CURL_CA_BUNDLE` 全局生效。两处都放在被所有脚本 source 的
`base.sh` 里，一次修完，不用逐个改几十处 `curl` 调用。

第三个坑是我们自己挖的。插件把快照 tmpfs 挂在 `/jffs/.koolshare/res/xwt_tmp`（`/koolshare` 是它的软链），
而 `install.sh` 用 `df | grep -w "/jffs"` 取剩余空间。`-w` 认为 `/jffs` 后面接 `/` 也算词边界，
于是**同时匹配两行**，`SPACE_AVAL` 变成 `36328\n4036`，数值比较直接 `bad number`。
更糟的是这个检查在 `rm -rf /koolshare/ss/*` **之后**，等于把插件删干净了才退出安装。

修法：按挂载点列精确匹配。

```sh
SPACE_AVAL=$(df | awk '$6 == "/jffs" {print $4; exit}')
```

教训：任何 `grep -w <挂载点>` 的判定，只要你自己往该路径下加了挂载点，就会失效。


### 兼容层的组织方式（踩了三次坑之后）

最早是把 `command()` / `sha256sum()` / CA 导出 / `http_response` 逐段追加进 `base.sh`，用标记判断是否已存在。三个版本的迭代证明这条路走不通：

1. 标记存在就跳过 ⇒ 升级后旧函数体永远留着（`http_response` 停在空实现，界面「检查更新」永远失败）
2. 改成"发现旧块就从标记处截断重写" ⇒ `sed '/^# XWT_ARM380_/,$d'` 会把相邻的块一起删掉，CA 和 `sha256sum` 就这样消失，规则更新随之失败
3. 最终方案：所有 shim 放进 `/koolshare/scripts/xwt_arm380_shims.sh`，`base.sh` 只挂一行 loader，安装时整文件覆盖。改一个函数只动一个文件，互不影响

教训：**给别人的配置追加内容时，不要用"标记 + 追加"做版本管理**，要么独立文件，要么整段替换有自己的边界标记。

## 8. 附加功能：导出 / 清空 / 打包 / 恢复

面板【附加功能】里的四个按钮在 380 上原本全部无效（点下去毫无反应），
根因有两个，都跟 380 的 httpd 有关。

### 8.1 共同的死点：兼容层被 source 两次

见上面第 2 条。`ss_conf.sh` 是这四个功能的唯一入口（导出=参数 12、清空=3、
打包=2、恢复=4），它在第二次加载兼容层时就被 ash 的语法错误打死，
所以四个功能一起失灵。兼容层加一行 `unalias` 之后全部恢复。

### 8.2 下载链路：扩展名、路径前缀、二进制截断，三层坑

只修扩展名是不够的。带**有效登录会话**重新实测（早先那张表是未登录态，
`.js` 的 200 其实是登录页而不是文件）：

| 路径 | 结果 |
|---|---|
| `/_root/files/ssconf_backup_v2.json.js` | **404**（386 专用前缀，380 根本没有） |
| `/files/ssconf_backup_v2.json.js` | **404** |
| `/Module_files/ssconf_backup_v2.json.js` | **200**，与源文件 MD5 完全一致 |
| `/Module_files/ssconf_backup_v2.json` | **404**（扩展名白名单） |
| `/Module_files/<不存在的文件>.js` | **200 但 0 字节** |

380 的 httpd 把 `/Module_<name>` 映射到 `/koolshare/webs/<name>`（前缀保留）。
`install.sh` 与开机钩子 `nat-start` 都建 `/koolshare/webs/Module_files -> /tmp/files`；
四个日志下载函数会 `rm -rf /tmp/files`，所以 `publish_download_alias()` 里再兜底重建一次。

**第一层 · 扩展名**：白名单在鉴权之前生效，只放行 `.js` / `.xml`。
`ss_conf.sh` 生成文件后挂一个**硬链接** `原名.js`（13 MB 包在内存里，不复制），
浏览器用 blob 下载、`a[download]` 存成原名。

**第二层 · 路径**：上游所有 `_root/files/...` URL 全部改成 `/Module_files/...js`，
含三个导出和五个日志/状态下载。漏掉这一层，后端文件生成得再正确，前端轮询
60 次全是 404，最后报「导出文件准备失败，请重试」——这正是"导出配置依然不可用"的根因。

**第三层 · 二进制截断**：httpd 遇到二进制内容会提前停止发送。同一个 13 MB
`shadowsocks.tar.gz.js`，改成 `.js` 或 `.xml` 都稳定只发 **43100 字节**；
而 17 MB 纯 ASCII 文本能完整传完，与大小、扩展名、tmpfs/JFFS、单个特殊字节都无关。
打包插件因此由 `openssl base64 -A` 生成**单行纯文本** `.b64.js`（约 17.4 MB），
前端 `atob` 解码成 `Uint8Array` 再存回 `.tar.gz`，往返 MD5 实测一致。

另外 `/Module_*` 下不存在的文件也返回 200 空响应，所以 `poll_route_file_ready()`
不能只看 status，必须校验 body 非空；探测结果按 URL 缓存给 `download_blob_file()`
复用，17 MB 只下一次。

### 8.3 没有 `/_upload`：恢复配置要自己传文件

上游的"恢复配置"用 httpdb 的 `POST /_upload` 把备份文件推进 `/tmp/upload`。
380 上 `/_upload` 与 `/upload` 都是 404，只有固件自己的 `/upload.cgi`（存 nvram 用）。

修法：前端改成用 FileReader 读文件 → base64 → **每片一个独立 dbus 键**
（`ss_backup_c0`、`ss_backup_c1`…，每片 3000 字符）分片 POST 上去，
最后发一次 commit；`xwt_backup_upload.sh` 按序号拼接、base64 解码，
写回 `/tmp/upload/ssconf_backup.json`，然后 `ss_conf.sh 4` 照常恢复。

每片用独立键而不是同一个键追加，是为了避开竞态：
慢请求不会覆盖还没被取走的上一片。dbus 单值上限实测约 16 KB（`dbus get` 读回会截断，
`dbus export` 才是完整的），3000 字符/片远在安全范围内。

## 9. 采样器的"存活检查"不能只看进程名

采样器有一个常驻主进程和一个 `--traffic-daemon` 采集子进程。
早先的存活检查是 `ps w | grep -q '[x]wt_dash_sampler.sh'`，
采集子进程的命令行同样包含这个串 —— 于是主进程死掉后，
采集进程冒充"采样器还活着"，桥接认为一切正常，永远不重启主进程，
面板就停在最后一份快照上（实测落后 33 分钟）。

修法两处：
- 存活检查收紧为 `[x]wt_dash_sampler.sh$`（只有主进程命令行以路径结尾）；
- 采集进程每 20 轮检查一次主进程是否还在，不在就退出（它本来是子进程，
  主进程没了它会被 init 收养并继续跑，这正是问题所在）。

实测：杀掉主进程 → 采集进程 ~20 秒后自行退出 → 桥接下一次轮询发现快照过期 →
自动拉起主进程与采集进程 → 快照恢复 1 秒新鲜。

## 10. 只在路由模式运行；总开关关闭 = 全部停

### 10.1 问题

切到 AP 模式（`sw_mode=3`，LAN 由上级 DHCP 分配）后，插件页面仍能被打开。
更要紧的是几条启动路径根本不看模式或开关：

- `ssconfig.sh restart`（Web 端开关、`ss_reboot` 定时重启都走它）**不检查
  `ss_basic_enable`**，一个已关闭的插件会被定时任务或界面调用重新拉起来；
- 380 移植新增的 `xwt_dash_sampler.sh` / `xwt_tmp_mirror.sh` 由 `nat-start`
  **无条件**拉起，而 `disable_ss()` 是上游代码，完全不认识这两个进程；
- 桥接 `fss_api_bridge.sh` 与 `ss_dashboard.sh` 有「快照过期就重新拉起采样器」
  的自愈逻辑，停掉也会被复活。

### 10.2 统一守卫

新增 `scripts/xwt_run_guard.sh` 作为唯一判定源：

- `xwt_router_mode()`：`sw_mode=1` 才是路由模式；固件不提供该键时用
  `lan_proto != dhcp` 兜底（AP/桥接由上级 DHCP 分配 LAN 地址）；
- `xwt_switch_on()`：`ss_basic_enable=1`；`xwt_may_run()`：两者同时成立；
- `xwt_start_daemons()` / `xwt_stop_daemons()`：管两个 380 常驻守护；
- `xwt_publish_mode()`：把 `router` / `nonrouter` 写进 dbus 供诊断。

所有入口都过守卫：`ssconfig.sh` 的 start/restart/stop/start_nat/
restart_chinadns_ng、`nat-start`、install.sh 安装收尾、采样器主循环（30 s
一次）、tmp mirror 主循环（10 s 一次）、桥接与 `ss_dashboard.sh` 的自愈拉起。
非路由模式下 `restart` 会先执行一遍轻量清理（停 ws / 代理进程 / 状态守护、
刷插件专用 iptables 与 cron、`xwt_stop_daemons`）再 `exit 0`。
`flush_iptables` 只删带 `SHADOWSOCKS|3333` 标记的规则，不碰固件自己的链。

### 10.3 界面拒绝打开

ASP 里的 `<% nvram_get("sw_mode"); %>` 由 httpd 在发页面前展开，实测得到
`var xwt_sw_mode = '3';`。`init()` 开头判断非 `1` 就显示全屏遮罩并 `return`：
不加载数据、不启动状态轮询，控件也不会绑定事件。

### 10.4 实测（R6300V2，AP 模式 192.168.10.146）

- 判定：sw_mode 1→放行，2/3/4→拒绝；sw_mode=1 且开关 1→放行，开关 0→拒绝。
- `ssconfig.sh restart` 被模式守卫拦下：进程 0→0、cron 0→0、
  iptables nat 规则 11→11（无误伤），syslog 出现「非路由模式，Xray Web Tool 已停用。」
- 模拟路由模式 + 开关 0：被开关守卫拦下，syslog「总开关关闭，跳过启动并停止后台守护。」
- `xwt_stop_daemons` 能杀掉名字匹配的守护进程；设备 7 个文件与构建产物 MD5 全一致；
  `sh -n` 与 ASP 内联 JS 检查通过。

### 10.5 取舍

开关关闭时连 `xwt_tmp_mirror.sh` 也停（对应「关闭全部相关功能」）。
面板仍可打开、配置仍可导出：RPC 回传由桥接直接写 `/res/xwt_tmp/*.js`，
导出走 `/Module_files/`，都不依赖这个镜像；但状态页 `ss_status_front.txt.js`
的实时刷新会停。若希望关掉代理后仍保留实时日志镜像，把 `TMP_MIRROR` 的守卫
从 `xwt_may_run` 换回 `xwt_router_mode` 即可。

---

## 11. v0.1.16 发布（2026-11-15）

包含 0.1.15 之后三批已验证修复：`2c9ac8e` 附加功能与订阅、`911644d` 下载链路、
`1d929de` 运行模式守卫。构建用 `XWT_VERSION=0.1.16 python tools/port.py`，
产物 13,038,354 字节 / 179 文件，SHA-256
`f9289680beda05ff5427d40eb534292a97c035d8eb2a627f1fd744c835ea2af2`。

### 发布前完整重建 + 设备重装验证（AP 模式 192.168.10.146）

- 备份用 `dbus listall`（**不是** `dbus export`）：该固件的 `dbus export <key>`
  实测返回空、无参数调用还会段错误；`listall` 全量 235 行可用于 `dbus import`。
- 安装退出码 0，日志 `Xray Web Tool v0.1.16 安装成功`；`compat_revision`、
  `fss_ac3100_version`、`softcenter_module_shadowsocks_version` 均为 0.1.16。
- **dbus 全量差分**：安装前后键集合完全相同，7 个节点值、order、current、
  next_id、schema 全无损，仅两个版本字段由 0.1.15 → 0.1.16。
- 守卫：xray / chinadns / xwt / websocketd 进程全 0、cron 0、iptables nat 11 条未变；
  页面注入 `var xwt_sw_mode = '3';`，遮罩函数存在，`_root/files` 零残留。
- 下载闭环：导出 JSON 43,507 B（HTTP MD5 = 源文件 MD5）、打包 13,061,713 B →
  base64 17,415,620 B → 完整下载 1.36 s → 解码 MD5 一致、`tar tzf` 可读、
  日志 `.txt.js` MD5 一致。
- 包内 51 个 `.sh` 的 `bash -n`、设备 ash `sh -n` 7 个脚本、ASP 内联 JS
  `node --check` 全通过。

### 两个测试侧的环境坑（不是产品缺陷）

1. **343 字节响应 = 未登录跳转页**（`<script>top.location.href='/Main_Login.asp';`）。
   一次登录偶发失败会让后续所有 `/Module_files/` 下载都变成 343 B；重新登录后
   同一 URL 立刻返回正确内容。判定下载成功必须校验 body 大小。
2. **设备时钟停在 2015-08-01** ⇒ 任何 CA 都报 `certificate is not yet valid`，
   `curl -k` 却 200。临时 `date -s` 校正后 GitHub API 200、
   `ss_update.sh check` 经 `http_response` 回传 `v0.1.16`。测完已恢复原时钟。
   另外 `ss_conf.sh 6`（日志下载）会清空 `/tmp/files`，所以导出与日志的校验要分开做。

### 端到端

Release `v0.1.16`（id `445940723`）三个附件均 201；从
`releases/download/v0.1.16/` 拉回的 `SHA256SUMS.txt`、`build-manifest.json` 与
本地逐字节一致，安装包 SHA-256 三方一致。首次拉包曾遇到 CDN 偶发截断
（12,378,496 B），重下即完整 —— 自更新会用 `SHA256SUMS.txt` 拒绝残包。

### 本版未在 AP 模式重测

代理出网、1 Hz 采样、规则更新全链路、订阅同步、清空 / 恢复配置。这些在
0.1.15 及之前的路由模式已实测通过，本版未改动相关代码；AP 模式下插件按设计
不运行，无法产生代理流量。

## 7. 没有做的事

- **`/koolshare/bin` 里 hysteria 4.5 MB、anytls 1.7 MB、smartdns 1.3 MB、obfs-local 0.36 MB**
  当前 vless 链路不用，但脚本都引用着，删了就没法切 hy2/anytls 节点。
  它们只占 JFFS，不占运行内存。
- **xray 的 `GOMEMLIMIT`**：能把 RSS 从 50 MB 压到 45 MB，但 GC 更频繁；
  在 800 MHz 双核上多出的 CPU 比省下的 5 MB 更亏。
- **一个包同时支持 380/386**：两代是协议级差异，运行时探测两套 API 会让两条路径都变脆。
