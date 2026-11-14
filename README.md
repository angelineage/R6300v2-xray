# R6300v2-xray

把 [Xray Web Tool](https://github.com/angelineage/xray-web-tool) 移植到 **Netgear R6300V2**
（梅林 380.70_X7.9.1 / Koolshare 1.0 软件中心 / BCM4708 armv7l / 内核 2.6.36.4brcmarm）。

上游面向梅林 **386**（arm_lite），前端通过 httpdb 的 `/_api/` JSON-RPC 取数；380 上**没有 httpdb**，
软件中心是 1.0 代，只有 `/dbconf?p=` 读、`/applydb.cgi?p=` 写、`SystemCmd` 执行。
这个仓库补的就是这层落差，并顺带把状态面板从“每次请求重跑一遍 8 秒脚本”改成常驻 1 Hz 采样。

| 项目 | 值 |
|---|---|
| 版本 | 0.1.15 |
| 安装包 | `shadowsocks_arm380_r6300v2_0.1.15.tar.gz`（177 文件 / 13.0 MB） |
| SHA-256 | `8430c7f1a47d96aa3bee6f198bdaa78ea26a1c9d55060a4b05641b5cdcf76fda` |
| 实测机型 | R6300V2（380.70_X7.9.1）；其他 arm380 机型未验证 |
| 许可 | GPL-3.0（与上游一致，二进制原样取自上游发行） |

## 功能状态

在 R6300V2 上实测通过：安装/卸载、节点订阅导入、vless + xhttp 出网（302/200 验证）、
规则与 DNS（chinadns-ng）、开机自启（重启后 xray + chinadns-ng + iptables 链自动恢复）、
运行状态面板（1 秒刷新）、日志页、节点切换、**规则更新**与**插件自更新**（见下）。

【附加功能】四个按钮也逐个在设备上跑通：导出配置（JSON，16 个节点 57 KB）、
清空配置（清理 178 项普通配置 + 16 个节点）、打包插件（13 MB / 192 个文件）、
恢复配置（分片上传后还原，节点与配置逐项一致）。
订阅模块用真实订阅地址验证：保存订阅配置 → 同步 → 解析出 24 个 vless 节点 → 写入 dbus。

> 这一批修复已进入源码（`tools/port.py`），**尚未发布新版本**；版本号与 Release 等你确认后再动。

## 安装

### 1. 先备份（必做）

上游 `install.sh` 会 `rm -rf /koolshare/ss/*`、`/koolshare/scripts/ss_*`、大部分
`/koolshare/bin/*` 与 `/koolshare/res/*`，等于清空现有 fancyss 的节点、订阅和规则。
在设备上执行：

```sh
sh scripts/backup-before-install.sh
# 产出 /jffs/fancyss380-backup-<时间戳>.tar.gz，先把它拷到本地
```

回滚：`sh scripts/rollback.sh /jffs/fancyss380-backup-<时间戳>.tar.gz`

### 2. 安装

方式一：软件中心 → 离线安装 → 上传 `shadowsocks_arm380_r6300v2_0.1.8.tar.gz`。

方式二（SSH）：

```sh
scp shadowsocks_arm380_r6300v2_0.1.8.tar.gz admin@192.168.50.1:/tmp/
ssh admin@192.168.50.1
dbus set soft_name="shadowsocks_arm380_r6300v2_0.1.8.tar.gz"
sh /koolshare/scripts/ks_tar_intall.sh
```

包名不能改：`ks_tar_intall.sh` 读 `soft_name`，按第一个 `_` 之前的部分推导解压目录名，
必须是 `shadowsocks`。

### 3. 装完

- 入口：软件中心 → **Xray Web Tool** → 打开（`home_url=Module_shadowsocks.asp`）
- 首次打开若样式错乱或数据为空，`Ctrl+F5` 强刷（资源 URL 带内容哈希版本号）
- 旧 fancyss 的节点不会迁移，需要在节点页重新导入订阅

## 面板采样架构

| 组件 | 作用 |
|---|---|
| `scripts/xwt_dash_sampler.sh` | 常驻 1 Hz 采样器，单写者（flock）。读 `/proc` 内建操作，把快照原子替换到 `/koolshare/res/xwt_tmp/xwt_dash.js`（tmpfs） |
| `xwt_dash_sampler.sh --traffic-daemon` | 独立采集 xray `/debug/vars` 计数器。该端点本身要 0.2–1.4 秒，塞进 1 Hz 循环必然拖垮节拍，所以拆开，连取样时刻一起落盘 |
| `scripts/ss_dashboard.sh` | 薄壳：读快照 → 判新鲜度 → `nc` 回 `/_resp/<id>`；冷启动时才 `--once` 兜底 |
| `scripts/fss_api_bridge.sh` | `SystemCmd` 入口，把 `/_api/` 请求翻成 dbus 读写或文件读取 |
| `scripts/xwt_tmp_mirror.sh` | 380 httpd 没有 `/_temp/` 路由且只放行 `.js`/`.xml`，把 `/tmp/upload` 的 txt/json 镜像成 `/res/xwt_tmp/*.js` |
| `res/xwt-arm380-api.js` | 前端 `$.ajaxTransport` 兼容层：GET→`/dbconf?p=`，POST→`/applydb.cgi?p=`（按键前缀分组），`/_temp/`→`/res/xwt_tmp/*.js` |

关键结论：**面板每秒轮询一次 `/_api/`，httpd 就要 fork 一个 shell**，只为拿一份 290 字节的 JSON。
改成直读 tmpfs 文件、每 30 次才走一次真 RPC 兜底后，`mtdblock3`（squashfs 块线程）从 46% 掉到 2.7%。

## 自动更新

两个通道都在 R6300V2 上实测跑通。

**规则更新**（`scripts/ss_rule_update.sh`）

- 定时：`0 4 * * *`（在界面上改成你想要的小时数即可）
- 源：`raw.githubusercontent.com/angelineage/xray-web-tool/main/src/shadowsocks/ss/rules`
- 流程：先拉 `rules.json.js` 清单 → 逐文件比对 SHA-256 → 下载 → 校验 SHA-256 → 校验格式与条数（gfwlist/chnlist 域名数、chnroute 网段数）→ 全量通过才替换，中途失败整批回滚
- 只替换勾选的项（gfwlist / chnlist / chnroute），替换成功后重启插件生效

**插件自更新**（`scripts/ss_update.sh`）

- 查本仓库 Releases 的 `latest`
- 资产名固定 `shadowsocks_arm380_r6300v2_<版本>.tar.gz`，下载后校验 `SHA256SUMS.txt`
- 解包后校验 `maintenance.json`：`version` 必须等于标签值、`target.model` 必须是 `R6300V2`，否则拒绝安装（防止把 386 的包或别的机型的包灌进来）
- 直连 GitHub 失败时自动改走本机 `socks5 127.0.0.1:23456` 续传重试
- 升级保留节点、当前节点、排序与订阅：节点存在 dbus 的 `fss_node_*` 里，安装脚本只替换 `/koolshare/ss` 下的程序与规则

380 固件有两个坑让这两条链路本来是死的，已在本仓库修掉：**没有 `sha256sum`**（只有 `md5sum`，改用 `openssl dgst -sha256` 实现），**CA 证书库停在 2017 年**（包里带 `res/cacert.pem`，`base.sh` 导出 `CURL_CA_BUNDLE`）。

## 实测数据

30 秒窗口，面板开启，同一台 R6300V2：

| 指标 | 改造前 | 现在 |
|---|---|---|
| load average | 5.7 | **1.5** |
| mtdblock3（块设备线程） | 46% | **2.7%** |
| 状态刷新周期 | 8 秒（且并发 4–8 份） | **1 秒**（单实例） |
| 采样器 CPU | 1.9% | 1.8% |
| 流量采集 CPU | 0.9% | 0.7% |
| page cache | 8 MB | 26 MB |

## 从源码构建

```sh
git clone https://github.com/angelineage/xray-web-tool.git ../tmp/xray-web-tool
python3 tools/port.py
```

或指向上游检出目录：

```sh
XWT_UPSTREAM=/path/to/xray-web-tool python3 tools/port.py
```

产物在 `dist/`。构建是确定性的（`gzip mtime` 固定为 0），SHA-256 应与 Release 里的一致；
不一致说明上游源码或原生二进制变了。原生二进制从不修改，其 SHA-256 需与上游
`packaging/vendor-lock.json` 匹配。

## 已知限制

1. 上游自带的定制 websocketd 0.1.2 在 380 上报 `InvalidProtocolOption`，实时日志走 HTTP 快照回退，功能不受影响。
2. 仅 R6300V2 实测。`platform_test` 白名单是 `RT-AC3100|R6300V2`，其他 arm380 机型需自行加 id 并自测。
3. 1.0 软件中心没有 `fss_*` 虚拟表，节点/订阅列表由本地文件导出后镜像，细节行为与上游不完全相同。
4. **不支持 386**：API 兼容层是针对 1.0 软件中心写的，386 请直接装上游发行版。
5. “国外连接 / 国内连接”探测默认 90 秒一轮（可改回更短，代价是 CPU）。
6. 自更新要下 13 MB 包，本机直连 GitHub 常解析超时，日志里会先出现一条 `curl: (6)` 再自动走代理续传，属正常回退；全程约 2 分钟。
7. 380 的 httpd 只放行 `.js` / `.xml`，导出/打包的文件靠一个 `.js` 硬链接才能被浏览器取到；用户看到的文件名仍是 `.json` / `.tar.gz`。
8. 「恢复配置」不走 `/_upload`（380 没有这个路由），改为 base64 分片经 `/_api/` 上传，单片 3000 字符。

## 目录结构

```
tools/port.py                    确定性构建脚本，所有补丁都在这里
scripts/backup-before-install.sh 装前备份（设备上执行）
scripts/rollback.sh              回滚
docs/port-notes.md               API 映射表、踩过的坑、性能归因过程
```

## 致谢

- 上游：[angelineage/xray-web-tool](https://github.com/angelineage/xray-web-tool)
- 更上游：[hq450/fancyss](https://github.com/hq450/fancyss)（3.0 分支）
