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

### 8.2 httpd 只放行 `.js` / `.xml`

实测（未登录态，`.js` 返回登录页说明路由通过，`.json` 直接 404）：

| 路径 | 结果 |
|---|---|
| `/_root/files/x.js` | 200（放行）|
| `/_root/files/x.xml` | 200（放行）|
| `/_root/files/x.json` | **404** |
| `/_root/files/x.sh` | **404** |
| `/_root/files/x.tar.gz` | **404** |

扩展名白名单在**鉴权之前**生效，所以"文件确实生成了"也没用，浏览器根本取不到。
上游把导出结果放在 `_root/files/ssconf_backup_v2.json`，在 380 上必然 404。

修法：`ss_conf.sh` 生成文件后额外挂一个**硬链接** `原名.js`，
ASP 的下载 URL 指向 `.js` 那一份。浏览器用 blob 下载并以 `a[download]` 指定的名字
保存，用户拿到的仍是 `.json` / `.sh` / `.tar.gz`。
用硬链接而不是复制：打包出来 13 MB，而 `/tmp` 是内存。

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

## 7. 没有做的事

- **`/koolshare/bin` 里 hysteria 4.5 MB、anytls 1.7 MB、smartdns 1.3 MB、obfs-local 0.36 MB**
  当前 vless 链路不用，但脚本都引用着，删了就没法切 hy2/anytls 节点。
  它们只占 JFFS，不占运行内存。
- **xray 的 `GOMEMLIMIT`**：能把 RSS 从 50 MB 压到 45 MB，但 GC 更频繁；
  在 800 MHz 双核上多出的 CPU 比省下的 5 MB 更亏。
- **一个包同时支持 380/386**：两代是协议级差异，运行时探测两套 API 会让两条路径都变脆。
