#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Build an arm380 (Koolshare 1.0 software center) port of angelineage/xray-web-tool.

Upstream target : RT-AC3100 / Merlin 386 arm_lite / Linux 2.6.36 / httpdb 1.5 API
Port target     : R6300V2 / Merlin-Koolshare 380.70_X7.9.1 / Linux 2.6.36 / 1.0 API

Everything is derived deterministically from the checked-out upstream source, so
re-running this script reproduces the same tar.gz. Native binaries are never
modified; their SHA-256 values must still match packaging/vendor-lock.json.

VERIFIED ON DEVICE (R6300V2, 380.70_X7.9.1, read-only, nothing installed)
  - uname -m=armv7l, kernel=2.6.36.4brcmarm: matches the upstream platform_test
  - all 17 bundled binaries are ARMv7 32-bit; xray 26.6.1, jq 1.6,
    websocketd 0.1.2, node-tool, isutf8 all execute on this firmware
  - /usr/bin/dbus, /usr/bin/skipd, /koolshare/scripts/base.sh, start-stop-daemon,
    iptables 1.4.14 + ipset 6.32 + xt_TPROXY are all present
  - GET /dbconf?p=ss works WITHOUT auth and declares window.db_ss as a plain
    object -> the 1.0-generation equivalent of GET /_api/ss
  - every shell script passes `sh -n` under the router's own ash
  - 256 MB RAM / 63 MB free JFFS: the 12.9 MB package fits

KNOWN LIMITATIONS (stage 2, requires on-device tuning)
  1. GET /_api/fss_data and /_api/fss_node are httpdb virtual tables with no
     dbus equivalent. The shim reads /dbconf?p=fss_* (empty) and optionally
     window.XWT_FSS_EXPORT; node/subscription lists will be empty until an
     exporter is written from node-tool's local_*.txt data.
  2. POST /_api/ is translated to /applydb.cgi?p=ss + SystemCmd=fss_api_bridge.sh.
     httpd has an "[httpd] Invalid SystemCmd!" validator; whether it accepts a
     new script name has NOT been tested. If rejected, map the bridge onto an
     existing allowed name or use apply.cgi with SystemCmd.
  3. Timing between the async SystemCmd and the UI's WebSocket log channel
     (ws://host:803/) is unverified.
  4. Fields are submitted in one p=ss batch; ssconf_basic_* / ss_acl_* may need
     separate p= groups on some 1.0 builds.
  5. 386-only CSS/JS assets were patched (httpApi.js removed, softcenter.css
     supplied), but full visual QA has not been done.

INSTALL RISK: upstream install.sh removes /koolshare/ss/*, /koolshare/scripts/ss_*,
most of /koolshare/bin/* and /koolshare/res/*, which WIPES the currently running
fancyss 4.38.3 (nodes/subscriptions/rules). Run scripts/backup-before-install.sh
first; scripts/rollback.sh restores it.
"""
import gzip
import hashlib
import io
import json
import os
import pathlib
import shutil
import tarfile

ROOT = pathlib.Path(__file__).resolve().parents[1]
TMP = ROOT.parent / "tmp"
# XWT_UPSTREAM lets a fresh clone build without reproducing this machine's
# directory layout:  XWT_UPSTREAM=/path/to/xray-web-tool python tools/port.py
UPSTREAM = pathlib.Path(os.environ.get("XWT_UPSTREAM") or (TMP / "xray-web-tool"))
SRC_UP = UPSTREAM / "src" / "shadowsocks"
SRC = ROOT / "src" / "shadowsocks"
DIST = ROOT / "dist"
# XWT_VERSION overrides the version stamped into the package name and
# maintenance.json; the in-app updater compares that value against the tag.
VERSION = (os.environ.get("XWT_VERSION")
           or json.loads((UPSTREAM / "maintenance.json").read_text("utf-8"))["version"])
# ks_tar_intall.sh derives the extracted directory name from the first
# underscore-separated token of the tarball filename -> must be "shadowsocks".
PACKAGE_NAME = "shadowsocks_arm380_r6300v2_%s.tar.gz" % VERSION


def write_text_lf(path, text, bom=False):
    """Windows pathlib.write_text() converts \\n to \\r\\n; router ash cannot parse that."""
    data = text.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    path.write_bytes(data)


def replace_once(text, old, new, label):
    count = text.count(old)
    if count != 1:
        raise SystemExit("PATCH FAILED [%s]: expected 1 match, found %d" % (label, count))
    return text.replace(old, new, 1)


def patch_install_sh(text):
    # 1. device whitelist: RT-AC3100 -> RT-AC3100 + R6300V2
    text = replace_once(text,
        '''if [ "${MAINTENANCE_MODEL}" != "RT-AC3100" ]; then
\techo '此维护包仅支持 RT-AC3100，安装已停止。'
\texit 1
fi''',
        '''case "${MAINTENANCE_MODEL}" in
\tRT-AC3100|R6300V2) ;;
\t*)
\t\techo '此维护包仅支持 RT-AC3100 / R6300V2(arm380)，安装已停止。'
\t\texit 1
\t\t;;
esac''', "device whitelist")

    # 2. platform_test: 380 has no /koolshare/bin/httpdb, but has dbus + skipd + base.sh
    text = replace_once(text,
        '''\tif [ ! -d "/koolshare" ] || [ ! -x "/koolshare/bin/httpdb" ] || [ ! -x "/usr/bin/skipd" ]; then
\t\techo_date "Xray Web Tool 需要支持软件中心的梅林固件。"''',
        '''\tif [ ! -d "/koolshare" ] || [ ! -f "/koolshare/scripts/base.sh" ] || [ ! -x "/usr/bin/skipd" ] || [ ! -x "/usr/bin/dbus" ]; then
\t\techo_date "Xray Web Tool 需要支持 Koolshare 软件中心的梅林固件（dbus + skipd + base.sh）。"''',
        "platform_test deps")

    text = replace_once(text,
        '''\t\t\techo_date "Xray Web Tool 安装包仅适用于 RT-AC3100 arm_lite，内核需为 2.6.36。"''',
        '''\t\t\techo_date "Xray Web Tool 安装包仅适用于 armv7l / 2.6.36 的 arm380(R6300V2) 或 RT-AC3100 arm_lite。"''',
        "platform_test message")

    # 3. arm380 offline installer is ks_tar_intall.sh (yes, upstream typo)
    text = replace_once(text,
        '''\tlocal softcenter_installer_pattern="ks_tar_""install\\\\.sh"
\tinstall_parent_chain_contains "${softcenter_installer_pattern}\\\\|start-stop-daemon"''',
        '''\tlocal softcenter_installer_pattern="ks_tar_""install\\\\.sh\\\\|ks_tar_""intall\\\\.sh"
\tinstall_parent_chain_contains "${softcenter_installer_pattern}\\\\|start-stop-daemon"''',
        "offline installer name")

    # 4. strings shown in software center / install log
    text = replace_once(text,
        '''\tlocal DESCR="angelineage 维护 · RT-AC3100 arm_lite · Xray 代理、实时流量与规则更新"''',
        '''\tlocal DESCR="angelineage 维护 · R6300V2 arm380 移植 · Xray 代理、实时流量与规则更新"''',
        "DESCR")
    text = replace_once(text,
        '''\techo_date "安装插件版本：${TITLE_NEW} v${MAINT_VERSION}（RT-AC3100 arm_lite）"''',
        '''\techo_date "安装插件版本：${TITLE_NEW} v${MAINT_VERSION}（R6300V2 arm380 移植版）"''',
        "install banner")

    # 5. arm380 software center uses install=4 (verified against
    #    angelineage/v2ray_bin shadowsocks/install.sh), and reads home_url from
    #    dbus -- overwrite the legacy Main_Ss_Content.asp pointer left by 4.38.3.
    text = replace_once(text,
        '''\tdbus set softcenter_module_${module}_install="4"''',
        '''\tdbus set softcenter_module_${module}_install="4"
\tdbus set softcenter_module_${module}_home_url="Module_${module}.asp"''',
        "module registration")

    # 6. make sure the shim + bridge are always executable after copy
    text = replace_once(text,
        '''\tcp -rf /tmp/shadowsocks/scripts/* /koolshare/scripts/''',
        '''\tcp -rf /tmp/shadowsocks/scripts/* /koolshare/scripts/
\tchmod +x /koolshare/scripts/fss_api_bridge.sh >/dev/null 2>&1''',
        "bridge chmod")

    # 7. the bundled curl-fancyss is linked against a newer libcurl and fails
    #    on 380.70 ("can't resolve symbol 'curl_mime_free'"). Always use the
    #    firmware curl, which supports --proxy / --socks5-hostname.
    text = replace_once(text,
        '''\tlocal CURL_PROXY_FLAG=$(curl -V|grep -Eo proxy)
\tif [ -n "${CURL_PROXY_FLAG}" ];then
\t\trm -rf /tmp/shadowsocks/bin/curl-fancyss
\t\tln -sf $(which curl) /koolshare/bin/curl-fancyss
\tfi''',
        '''\t# arm380: bundled curl-fancyss cannot resolve curl_mime_free against libcurl 7.54.1
\trm -rf /tmp/shadowsocks/bin/curl-fancyss
\tln -sf $(which curl) /koolshare/bin/curl-fancyss''',
        "curl-fancyss replacement")

    # 8. BusyBox ash on koolshare 380 has NO `command` builtin, but upstream
    #    scripts call `command -v` 41 times. Append one idempotent shim to
    #    base.sh so every script that sources it gets a working implementation.
    text = replace_once(text,
        '''\techo_date "清理旧文件"''',
        '''\t# arm380: /bin/sh has no `command` builtin; upstream uses `command -v` 41x.
\t# 8. Every arm380 compatibility shim lives in ONE file so an upgrade can
\t#    replace it wholesale. Appending blocks to base.sh meant an old body
\t#    survived forever, and stripping one block removed its neighbour.
\tif ! grep -q 'XWT_ARM380_SHIM_LOADER' /koolshare/scripts/base.sh 2>/dev/null; then
\t\t# older installs appended up to four blocks, all starting with this prefix
\t\tsed -i '/^# XWT_ARM380_/,$d' /koolshare/scripts/base.sh 2>/dev/null
\t\tprintf '\n# XWT_ARM380_SHIM_LOADER: arm380 shims, rewritten on every install\n[ -f /koolshare/scripts/xwt_arm380_shims.sh ] && . /koolshare/scripts/xwt_arm380_shims.sh\n' >> /koolshare/scripts/base.sh
\t\techo_date "arm380: base.sh 已挂载兼容函数。"
\tfi
\techo_date "清理旧文件"''',
        "command shim injection")

    # The free-space gate looks up /jffs with `df | grep -w "/jffs"`. Our
    #     tmpfs lives at /jffs/.koolshare/res/xwt_tmp, which contains "/jffs"
    #     as a word too, so grep returns TWO lines, SPACE_AVAL becomes
    #     "36328\n4036" and the numeric test aborts with "bad number" right
    #     after the old files were already deleted. Match the mount column.
    text = replace_once(text,
        '''\tSPACE_AVAL=$(df | grep -w "/jffs" | awk '{print $4}')''',
        '''\tSPACE_AVAL=$(df | awk '$6 == "/jffs" {print $4; exit}')''',
        "jffs free space lookup")

    # 9. arm380 boot hook. The legacy nat-start called ssconfig.sh with NO
    #    argument, which matches no case branch -> nothing started on reboot.
    text = replace_once(text,
        '''\tcp -rf /tmp/shadowsocks/webs/* /koolshare/webs/''',
        '''\tcp -rf /tmp/shadowsocks/webs/* /koolshare/webs/
\t# XWT_ARM380_RUN_GUARD: install-time daemon starts below honour the
\t# router mode and master switch instead of always running.
\txwt_may_run() { return 0; }
\txwt_router_mode() { return 0; }
\txwt_start_daemons() { :; }
\txwt_stop_daemons() { :; }
\txwt_publish_mode() { :; }
\t[ -f /koolshare/scripts/xwt_run_guard.sh ] && . /koolshare/scripts/xwt_run_guard.sh
\t# arm380: the 386 "_root/files/..." route does not exist here. httpd maps
\t# /Module_<name> to /koolshare/webs/<name>, so expose /tmp/files under that
\t# name; every export / log download is fetched as /Module_files/<file>.js.
\tmkdir -p /tmp/files
\tln -sfn /tmp/files /koolshare/webs/Module_files
\t# arm380: legacy nat-start called ssconfig.sh without "start" -> no autostart.
\t# ssconfig.sh also restarts dnsmasq/iptables, which re-triggers nat-start,
\t# so a bare call causes a start-storm. Always write the guarded version.
\tmkdir -p /jffs/scripts /tmp/upload /koolshare/res/xwt_tmp
\tcat > /jffs/scripts/nat-start <<'BOOTEOF'
#!/bin/sh
[ -x /usr/bin/onnatstart.sh ] && /usr/bin/onnatstart.sh
mkdir -p /tmp/upload /koolshare/res/xwt_tmp
# XWT_ARM380_DOWNLOADS: /tmp is RAM, but the /koolshare/webs symlink is JFFS
# and survives the wipe; recreate both ends on every boot.
mkdir -p /tmp/files
ln -sfn /tmp/files /koolshare/webs/Module_files
# XWT_ARM380_RUN_GUARD: WAN/NAT/DNS belong to the upstream router in AP,
# repeater and media-bridge modes. Publish the mode for diagnostics, then
# stop before any daemon or proxy is started.
xwt_may_run() { return 0; }
xwt_router_mode() { return 0; }
xwt_start_daemons() { :; }
xwt_stop_daemons() { :; }
xwt_publish_mode() { :; }
[ -f /koolshare/scripts/xwt_run_guard.sh ] && . /koolshare/scripts/xwt_run_guard.sh
xwt_publish_mode
if ! xwt_router_mode; then
	logger "[软件中心]: 非路由模式，Xray Web Tool 不启动。"
	xwt_stop_daemons
	exit 0
fi
# XWT_ARM380_TMPFS: /res is JFFS. Rewriting the status/dashboard mirrors there
# every second kept the flash in D-state (load 8+ on this 2-core box), so the
# volatile files live on tmpfs. Falls back to plain JFFS if mount is refused.
if ! mount | grep -q 'res/xwt_tmp type tmpfs'; then
\tmount -t tmpfs -o size=4m tmpfs /koolshare/res/xwt_tmp 2>/dev/null
fi
# XWT_ARM380_TMP_MIRROR: 380 httpd has no /_temp/ route; expose the upload
# cache as /res/xwt_tmp/ so the UI transport can rewrite those reads.
if xwt_may_run && ! ps w | grep -q '[x]wt_tmp_mirror.sh'; then
\tif [ -x /usr/sbin/start-stop-daemon ]; then
\t\tstart-stop-daemon -S -b -q -m -p /var/run/xwt_tmp_mirror.pid -x /bin/sh -- /koolshare/scripts/xwt_tmp_mirror.sh
\telse
\t\tsh /koolshare/scripts/xwt_tmp_mirror.sh >/dev/null 2>&1 &
\tfi
fi
# XWT_ARM380_DASH: one resident 1 Hz sampler owns the status snapshot. The
# poll path must not fork jq/curl per request -- that saturated mtdblock3.
if xwt_may_run && ! ps w | grep -q '[x]wt_dash_sampler.sh$'; then
\tif [ -x /usr/sbin/start-stop-daemon ]; then
\t\tstart-stop-daemon -S -b -q -m -p /var/run/xwt_dash_sampler.pid -x /bin/sh -- /koolshare/scripts/xwt_dash_sampler.sh
\telse
\t\tsh /koolshare/scripts/xwt_dash_sampler.sh >/dev/null 2>&1 &
\tfi
fi
# XWT_ARM380_BOOT_GUARD: one start per boot, never re-enter.
if xwt_may_run && [ ! -f /tmp/xwt_boot_started ] && ! ps w | grep -q '[s]sconfig.sh'; then
\t: > /tmp/xwt_boot_started
\tsh /koolshare/ss/ssconfig.sh start
fi
BOOTEOF
\tchmod +x /jffs/scripts/nat-start
\t# apply the tmpfs now so the running system matches the boot hook
\tif ! mount | grep -q 'res/xwt_tmp type tmpfs'; then
\t\tmount -t tmpfs -o size=4m tmpfs /koolshare/res/xwt_tmp 2>/dev/null
\tfi
\tchmod 755 /koolshare/scripts/xwt_*.sh >/dev/null 2>&1
\txwt_start_daemons
\techo_date "arm380: 已写入带防重入的开机自启钩子与 /_temp 镜像。"''',
        "boot hook")
    # arm380: the 1 Hz dashboard sampler is a standalone daemon and an install
    # replaces its script, so restart it here. Waiting for the next poll to
    # notice a stale snapshot can take 30 s.
    text = replace_once(text,
        '''	# finish''',
        '''	if xwt_may_run; then
		start-stop-daemon -K -q -p /var/run/xwt_dash_sampler.pid >/dev/null 2>&1
		for _xwt_p in $(ps w | grep '[x]wt_dash_sampler.sh' | awk '{print $1}'); do kill $_xwt_p 2>/dev/null; done
		rm -f /tmp/xwt_dash.lock
		start-stop-daemon -S -b -q -m -p /var/run/xwt_dash_sampler.pid -x /bin/sh -- /koolshare/scripts/xwt_dash_sampler.sh >/dev/null 2>&1
	else
		xwt_stop_daemons
	fi

	# finish''',
        "restart dashboard sampler")
    return text


ICON_CSS = '''<style type="text/css">
/* arm380: 386 ships /images/New_ui/{activate,deactivate}.svg for the
 * "current node" marker; 380 has no such files, so the marker rendered as an
 * invisible 26x26 box. Inline data-URI keeps it visible without new assets. */
.activate_icon,.activate_client_icon,.deactivate_icon,.deactivate_client_icon{background-repeat:no-repeat;background-position:center center;background-size:22px 22px;}
.activate_icon,.activate_client_icon{background-image:url("data:image/svg+xml;charset=utf8,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Ccircle cx='12' cy='12' r='10' fill='%2322ab39'/%3E%3Cpath d='M7 12.4l3.3 3.3L17 9.2' fill='none' stroke='%23ffffff' stroke-width='2.3' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");}
.deactivate_icon,.deactivate_client_icon{background-image:url("data:image/svg+xml;charset=utf8,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Ccircle cx='12' cy='12' r='9.3' fill='none' stroke='%2399a3ad' stroke-width='1.7'/%3E%3Cpath d='M12 6.6v5.6' stroke='%2399a3ad' stroke-width='2' stroke-linecap='round'/%3E%3Cpath d='M8.4 9.1a5.3 5.3 0 1 0 7.2 0' fill='none' stroke='%2399a3ad' stroke-width='1.7' stroke-linecap='round'/%3E%3C/svg%3E");}
.deactivate_icon:hover,.deactivate_client_icon:hover{background-image:url("data:image/svg+xml;charset=utf8,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24'%3E%3Ccircle cx='12' cy='12' r='10' fill='none' stroke='%2322ab39' stroke-width='1.8'/%3E%3Cpath d='M7 12.4l3.3 3.3L17 9.2' fill='none' stroke='%2322ab39' stroke-width='2.3' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");}
/* arm380: every #log_content1 colour rule upstream is scoped to #app[skin=...]
 * and that skin attribute is absent here, so the log textarea inherited the
 * light face style -> the loaded log was invisible on the grey panel. */
#log_content1{background:#1b1f22 !important;color:#dfe6e9 !important;border:1px solid #4b5563 !important;font:12px/1.5 Consolas,Menlo,monospace !important;}
</style>
'''



def patch_ssconfig(text):
    # arm380: the proxy needs WAN/NAT/DNS ownership, which only router mode
    # provides. `ssconfig.sh restart` is called by the web UI AND by the
    # ss_reboot cron; neither checked the master switch, so a disabled plugin
    # came back on its own. nat-start also started the arm380 daemons without
    # asking. One guard in front of the ACTION dispatch closes every path.
    guard = '''
# =========================================================================
# XWT_ARM380_RUN_GUARD
# =========================================================================
xwt_may_run() { return 0; }
xwt_router_mode() { return 0; }
xwt_start_daemons() { :; }
xwt_stop_daemons() { :; }
xwt_publish_mode() { :; }
[ -f /koolshare/scripts/xwt_run_guard.sh ] && . /koolshare/scripts/xwt_run_guard.sh
if [ -f /koolshare/scripts/xwt_run_guard.sh ]; then
\txwt_publish_mode
\tif ! xwt_router_mode; then
\t\tcase "$ACTION" in
\t\t\tstop|flush_nat) ;;
\t\t\t*)
\t\t\t\tlogger "[软件中心]: 非路由模式，Xray Web Tool 已停用。"
\t\t\t\tstop_ws
\t\t\t\tkill_process
\t\t\t\tstop_status
\t\t\t\tflush_iptables
\t\t\t\tflush_ipset
\t\t\t\tkill_cron_job
\t\t\t\tremove_ss_reboot_job
\t\t\t\tremove_ss_trigger_job
\t\t\t\txwt_stop_daemons
\t\t\t\tdbus set ss_basic_status="0" >/dev/null 2>&1
\t\t\t\texit 0
\t\t\t\t;;
\t\t\tesac
\tfi
fi

'''
    text = replace_once(text, "case $ACTION in\n", guard + "case $ACTION in\n",
                        "ssconfig guard")

    text = replace_once(text,
        '''\t\tapply_ss 2>&1 | tee -a "$LOG_FILE" | tee -a "/tmp/upload/ss_wan_log.txt"
\t\techo XU6J03M6 | tee -a "$LOG_FILE"''',
        '''\t\tapply_ss 2>&1 | tee -a "$LOG_FILE" | tee -a "/tmp/upload/ss_wan_log.txt"
\t\txwt_start_daemons
\t\techo XU6J03M6 | tee -a "$LOG_FILE"''',
        "ssconfig start launches daemons")

    text = replace_once(text,
        '''\telse
\t\tlogger "[软件中心]: Xray Web Tool 已停用。"
\tfi
\tunset_lock
\t;;
stop)''',
        '''\telse
\t\tlogger "[软件中心]: Xray Web Tool 已停用。"
\t\txwt_stop_daemons
\tfi
\tunset_lock
\t;;
stop)''',
        "ssconfig start disabled stops daemons")

    text = replace_once(text,
        '''stop)
\tset_lock
\tdisable_ss
\techo_date''',
        '''stop)
\tset_lock
\tdisable_ss
\txwt_stop_daemons
\techo_date''',
        "ssconfig stop stops daemons")

    text = replace_once(text,
        '''restart)
\t# start/restart by web or user
\tset_lock
\tstart_ws
\tapply_ss
\techo_date''',
        '''restart)
\t# start/restart by web or user
\tset_lock
\tif [ "$ss_basic_enable" != "1" ]; then
\t\tlogger "[软件中心]: 总开关关闭，跳过启动并停止后台守护。"
\t\txwt_stop_daemons
\t\tunset_lock
\t\texit 0
\tfi
\tstart_ws
\tapply_ss
\txwt_start_daemons
\techo_date''',
        "ssconfig restart honours master switch")

    text = replace_once(text,
        '''restart_chinadns_ng)
\tstart_chinadns_ng
\t;;''',
        '''restart_chinadns_ng)
\txwt_may_run && start_chinadns_ng
\t;;''',
        "ssconfig chinadns restart guard")
    return text


def patch_ss_conf(text):
    """arm380: httpd only serves .js/.xml out of any directory; every other
    extension is a 404 *before* authentication. On top of that the 386
    "_root/files/..." URL does not exist on 380 (the working route is
    /Module_files/<file>.js), and httpd truncates a binary .js response at the
    first non-text bytes (13 MB package -> 43100 bytes, for .js AND .xml),
    while 17 MB of plain ASCII goes through intact.

    So: publish a *.js twin for every text download, ship the binary package
    as single-line base64, and keep the /Module_files symlink alive.
    """
    helper = '''LOG_FILE=/tmp/upload/ss_log.txt

# arm380: httpd serves only .js/.xml from any directory; everything else is a
# 404 before authentication. Every download is therefore published twice: the
# real file keeps its extension for the restore path, and a hardlink named
# *.js is what the browser actually fetches. Hardlink rather than copy -- the
# package is 13 MB and /tmp lives in RAM.
publish_download_alias(){
	[ -n "$1" ] || return 0
	[ -f "$1" ] || return 0
	# 386's "_root/files/..." is a 404 on 380; httpd maps /Module_<name> to
	# /koolshare/webs/<name>. Keep that symlink alive even after a /tmp wipe.
	[ -L /koolshare/webs/Module_files ] || ln -sfn /tmp/files /koolshare/webs/Module_files 2>/dev/null
	rm -f "$1.js" "$1.b64.js"
	ln "$1" "$1.js" 2>/dev/null || cp -f "$1" "$1.js"
}
# arm380 httpd stops sending a binary .js at the first non-text bytes: the
# 13 MB package was reproducibly cut to 43100 bytes for BOTH .js and .xml,
# while 17 MB of plain ASCII went through intact. Ship the package as
# single-line base64; the UI decodes it back into the .tar.gz blob.
publish_package_download(){
	publish_download_alias /tmp/files/shadowsocks.tar.gz
	rm -f /tmp/files/shadowsocks.tar.gz.b64.js
	openssl base64 -A -in /tmp/files/shadowsocks.tar.gz -out /tmp/files/shadowsocks.tar.gz.b64.js 2>/dev/null
	if [ ! -s /tmp/files/shadowsocks.tar.gz.b64.js ]; then
		cp -f /tmp/files/shadowsocks.tar.gz.js /tmp/files/shadowsocks.tar.gz.b64.js
	fi
}'''
    text = replace_once(text, "LOG_FILE=/tmp/upload/ss_log.txt", helper,
                        "ss_conf publish helper")

    old = '''	rm -f "${target_file}" "${tmp_file}"
	if "$@" "${tmp_file}"; then
		mv -f "${tmp_file}" "${target_file}"
'''
    new = '''	rm -f "${target_file}" "${tmp_file}" "${target_file}.js"
	if "$@" "${tmp_file}"; then
		mv -f "${tmp_file}" "${target_file}"
		publish_download_alias "${target_file}"
'''
    text = replace_once(text, old, new, "ss_conf alias on export")

    old = '''    mv /tmp/files/shadowsocks.tar.gz.part /tmp/files/shadowsocks.tar.gz
    echo_date "插件打包完成，准备下载。"'''
    new = '''    mv /tmp/files/shadowsocks.tar.gz.part /tmp/files/shadowsocks.tar.gz
    publish_package_download
    echo_date "插件打包完成，准备下载。"'''
    text = replace_once(text, old, new, "ss_conf alias on package")

    old = '''    rm -f /tmp/files/shadowsocks.tar.gz
    echo_date "开始打包当前插件..."'''
    new = '''    rm -f /tmp/files/shadowsocks.tar.gz /tmp/files/shadowsocks.tar.gz.js /tmp/files/shadowsocks.tar.gz.b64.js
    echo_date "开始打包当前插件..."'''
    text = replace_once(text, old, new, "ss_conf package stale cleanup")

    old = '''    case "$act" in 1) rm -f /tmp/files/ssconf_backup.sh ;; 12) rm -f /tmp/files/ssconf_backup_v2.json ;; 2) rm -f /tmp/files/shadowsocks.tar.gz ;; esac'''
    new = '''    case "$act" in 1) rm -f /tmp/files/ssconf_backup.sh /tmp/files/ssconf_backup.sh.js ;; 12) rm -f /tmp/files/ssconf_backup_v2.json /tmp/files/ssconf_backup_v2.json.js ;; 2) rm -f /tmp/files/shadowsocks.tar.gz /tmp/files/shadowsocks.tar.gz.js /tmp/files/shadowsocks.tar.gz.b64.js ;; esac'''
    text = replace_once(text, old, new, "ss_conf stale cleanup")

    # The four log-download helpers wipe /tmp/files and recreate only the
    # legacy `files` symlink; the working /Module_files route must be rebuilt
    # too, otherwise log downloads break for the rest of the boot.
    old_link = '''\trm -rf /tmp/files
\trm -rf /koolshare/webs/files
\tmkdir -p /tmp/files
\tln -sf /tmp/files /koolshare/webs/files'''
    new_link = old_link + '''
\tln -sfn /tmp/files /koolshare/webs/Module_files'''
    n = text.count(old_link)
    if n != 4:
        raise SystemExit("PATCH FAILED [log download symlink]: expected 4 matches, got %d" % n)
    text = text.replace(old_link, new_link)

    # Those logs are plain *.txt; give each one a *.js twin for the browser.
    for call, alias in (
        ('\tdownload_ssf\n', '\tpublish_download_alias /tmp/files/ssf_status.txt\n'),
        ('\tdownload_ssc\n', '\tpublish_download_alias /tmp/files/ssc_status.txt\n'),
        ('\tdownload_dig_log\n', '\tpublish_download_alias /tmp/files/dns_dig_result.txt\n'),
        ('\tdownload_resv_log\n', '\tpublish_download_alias /tmp/files/$(dbus get ss_basic_logname).txt\n'),
    ):
        text = replace_once(text, call, call + alias, "ss_conf log alias %s" % call.strip())
    return text


def patch_dashboard_js(text):
    """Make the 运行状态 sampler usable on a 2-core 800 MHz router.

    ss_dashboard.sh needs 0.3-13 s per run on this hardware (jq + stats API),
    while the page polls every second. The original code demanded the uptime
    delta be 0.5-5 s AND advanced `previous` on every poll, so a repeated
    sample reset the baseline to itself -> elapsed 0 -> the rate, the CPU and
    the chart were permanently "采样中…"/"统计不可用".
    """
    # 1. accept the real sampling interval (measured 6-13 s under load)
    text = replace_once(text,
        'var valid=traffic&&same&&previous.traffic_available&&elapsed>=0.5'
        '&&elapsed<=5&&wallElapsed>=0.5&&wallElapsed<=5'
        '&&elapsed>=wallElapsed*0.5&&elapsed<=wallElapsed*2&&',
        'var valid=traffic&&same&&previous.traffic_available&&elapsed>=0.3'
        '&&elapsed<=20&&wallElapsed>=0.3&&wallElapsed<=20'
        '&&elapsed>=wallElapsed*0.4&&elapsed<=wallElapsed*2.5&&',
        "dashboard sampling window")
    # 2. a repeated sample must not overwrite the baseline it is compared to;
    #    keep the last rendered numbers instead of flickering to 采样中…
    text = replace_once(text,
        '                        previous=s;\n',
        '                        if(previous&&s.uptime<=previous.uptime)'
        '{$(\'#xwt-error\').text(\'\');healthy=true;return;}\n'
        '                        previous=s;\n',
        "dashboard stale-sample guard")
    # 3. retry faster while unhealthy so the first valid sample shows up sooner
    text = replace_once(text,
        ':5000);}',
        ':2000);}',
        "dashboard retry delay")
    # 4. document.hidden is true whenever the window is merely unfocused, which
    #    froze the panel on remote/background sessions. visibilitychange already
    #    stops the sampler on a real hide, so the extra check is redundant.
    text = replace_once(text,
        "function active(){return !document.hidden && $('#tablet_11').is(':visible');}",
        "function active(){return $('#tablet_11').is(':visible');}",
        "dashboard active() without hidden")
    # 5. sampling runs every ~10 s here while the page reads every second, so the
    #    old code flashed between a real value and "采样中…" on every other tick.
    #    Once a rate has been shown, keep it until the next valid sample.
    text = replace_once(text,
        "                        $('#xwt-up').text(valid?bitsPerSecond((s.upload-previous.upload)/elapsed):traffic?'采样中…':'统计不可用');",
        "                        if(valid){$('#xwt-up').text(bitsPerSecond((s.upload-previous.upload)/elapsed));}"
        "else if(!traffic){$('#xwt-up').text('统计不可用');}",
        "dashboard upload text hold")
    text = replace_once(text,
        "                        $('#xwt-down').text(valid?bitsPerSecond((s.download-previous.download)/elapsed):traffic?'采样中…':'统计不可用');",
        "                        if(valid){$('#xwt-down').text(bitsPerSecond((s.download-previous.download)/elapsed));}"
        "else if(!traffic){$('#xwt-down').text('统计不可用');}",
        "dashboard download text hold")
    text = replace_once(text,
        "                        $('#xwt-cpu').text(!s.pid?'—':cpu===null?'采样中…':cpu.toFixed(1)+'%');",
        "                        if(cpu!==null){$('#xwt-cpu').text(cpu.toFixed(1)+'%');}"
        "else if(!s.pid){$('#xwt-cpu').text('—');}",
        "dashboard cpu text hold")
    # 6. the sampler now answers in ~50 ms, so keep a real 1 s cadence instead
    #    of firing again immediately after a slow round (that stacked requests)
    text = replace_once(text,
        'Math.max(0,1000-(Date.now()-started))',
        'Math.max(150,1000-(Date.now()-started))',
        "dashboard tick floor")
    # 7. a 12 s ceiling keeps a stalled backend from aborting the poll and
    #    re-issuing it while the old request is still running.
    text = replace_once(text,
        "dataType:'json',timeout:8000,",
        "dataType:'json',timeout:12000,",
        "dashboard xhr timeout")
    # 8. traffic now comes from a separate collector that can only refresh
    #    every 1-2 s (Xray's /debug/vars needs 0.2-1.4 s here). Rate over the
    #    real traffic interval, not over the 1 s poll, otherwise the derived
    #    speed is off by whatever the collector lagged.
    text = replace_once(text,
        "var elapsed=same?s.uptime-previous.uptime:0;",
        "var elapsed=same?(Number(s.traffic_uptime===undefined?s.uptime:s.traffic_uptime)"
        "-Number(previous.traffic_uptime===undefined?previous.uptime:previous.traffic_uptime)):0;",
        "dashboard traffic interval")
    # 9. when the counters did not move (collector still in flight) redraw the
    #    previous rate instead of punching a hole into the curve.
    text = replace_once(text,
        "root.XwtChart.add(valid?(s.upload-previous.upload)/elapsed:null,"
        "valid?(s.download-previous.download)/elapsed:null);",
        "var _ur=valid?(s.upload-previous.upload)/elapsed:null,"
        "_dr=valid?(s.download-previous.download)/elapsed:null;"
        "if(_ur!==null){lastUpRate=_ur;lastDownRate=_dr;}"
        "else if(same&&elapsed<=0&&lastUpRate!==null){_ur=lastUpRate;_dr=lastDownRate;}"
        "root.XwtChart.add(_ur,_dr);",
        "dashboard curve hold")
    text = replace_once(text,
        "var timer=null, pending=null, generation=0, previous=null, previousTime=0;",
        "var timer=null, pending=null, generation=0, previous=null, previousTime=0,"
        "lastUpRate=null, lastDownRate=null;",
        "dashboard rate cache")
    text = replace_once(text,
        "if(valid){$('#xwt-up').text(bitsPerSecond((s.upload-previous.upload)/elapsed));}",
        "if(_ur!==null){$('#xwt-up').text(bitsPerSecond(_ur));}",
        "dashboard upload rate source")
    text = replace_once(text,
        "if(valid){$('#xwt-down').text(bitsPerSecond((s.download-previous.download)/elapsed));}",
        "if(_dr!==null){$('#xwt-down').text(bitsPerSecond(_dr));}",
        "dashboard download rate source")
    return text



# arm380: rebuilt probe / download helpers for the /Module_files route.
NEW_PROBE_JS = r'''
function poll_route_file_ready(arg, meta, retry) {
	var xhr = new XMLHttpRequest();
	var url = meta.url;
	var maxRetry = typeof meta.maxRetry == "number" ? meta.maxRetry : 20;
	var retryDelay = typeof meta.retryDelay == "number" ? meta.retryDelay : 300;
	var isB64 = /\.b64\.js(\?|$)/.test(url);
	xhr.open("GET", url + (url.indexOf("?") === -1 ? "?" : "&") + "_probe=" + new Date().getTime(), true);
	xhr.responseType = isB64 ? "text" : "blob";
	xhr.onload = function() {
		// arm380: /Module_* answers 200 with an EMPTY body when the file is
		// missing, and the export is asynchronous (16 nodes take ~18 s).
		// A bare status check would declare a nonexistent file ready.
		var body = isB64 ? xhr.responseText : xhr.response;
		var size = isB64 ? (body ? body.length : 0) : (body ? body.size : 0);
		if (xhr.status >= 200 && xhr.status < 300 && size > 0) {
			// Keep the probe payload: the package is 17 MB of base64, so
			// downloading it a second time in download_blob_file is wasteful.
			route_file_cache[url] = body;
			flush_route_file_callbacks(arg, true);
			return;
		}
		if (retry >= maxRetry) {
			flush_route_file_callbacks(arg, false);
			return;
		}
		setTimeout(function() {
			poll_route_file_ready(arg, meta, retry + 1);
		}, retryDelay);
	};
	xhr.onerror = function() {
		if (retry >= maxRetry) {
			flush_route_file_callbacks(arg, false);
			return;
		}
		setTimeout(function() {
			poll_route_file_ready(arg, meta, retry + 1);
		}, retryDelay);
	};
	xhr.send();
}
'''

NEW_DOWNLOAD_JS = r'''
function save_route_blob(blob, fileName) {
	var URLApi = window.URL || window.webkitURL;
	if (!URLApi || !URLApi.createObjectURL || !blob) { return false; }
	var blobUrl = URLApi.createObjectURL(blob);
	var a = document.createElement("a");
	a.href = blobUrl;
	a.download = fileName;
	document.body.appendChild(a);
	a.click();
	document.body.removeChild(a);
	setTimeout(function() { URLApi.revokeObjectURL(blobUrl); }, 1000);
	return true;
}
function decode_b64_blob(text) {
	// arm380: a 13 MB binary cannot pass through httpd intact (it is cut at
	// 43100 bytes). ss_conf.sh publishes it as single-line base64 instead;
	// rebuild the .tar.gz bytes here.
	var raw = atob(String(text).replace(/[\r\n\t ]/g, ""));
	var arr = new Uint8Array(raw.length);
	for (var i = 0; i < raw.length; i++) { arr[i] = raw.charCodeAt(i); }
	return new Blob([arr], { type: "application/octet-stream" });
}
function download_blob_file(url, fileName, cb) {
	var isB64 = /\.b64\.js(\?|$)/.test(url);
	var cached = route_file_cache[url];
	if (cached) {
		try {
			var cachedBlob = isB64 ? decode_b64_blob(cached) : cached;
			if (save_route_blob(cachedBlob, fileName)) {
				delete route_file_cache[url];
				if (typeof cb === "function") { cb(true); }
				return;
			}
		} catch (e) {}
	}
	var xhr = new XMLHttpRequest();
	xhr.open("GET", url + (url.indexOf("?") === -1 ? "?" : "&") + "_ts=" + new Date().getTime(), true);
	xhr.responseType = isB64 ? "text" : "blob";
	xhr.onload = function() {
		var body = isB64 ? xhr.responseText : xhr.response;
		var size = isB64 ? (body ? body.length : 0) : (body ? body.size : 0);
		if (xhr.status >= 200 && xhr.status < 300 && size > 0) {
			try {
				var blob = isB64 ? decode_b64_blob(body) : body;
				if (save_route_blob(blob, fileName)) {
					delete route_file_cache[url];
					if (typeof cb === "function") { cb(true); }
					return;
				}
			} catch (e) {}
		}
		// An iframe fallback would save the base64 TEXT under a .tar.gz name;
		// report failure instead of handing the user a corrupt archive.
		if (isB64) {
			if (typeof cb === "function") { cb(false); }
			return;
		}
		trigger_iframe_download(url);
		if (typeof cb === "function") { cb(true); }
	};
	xhr.onerror = function() {
		if (isB64) {
			if (typeof cb === "function") { cb(false); }
			return;
		}
		trigger_iframe_download(url);
		if (typeof cb === "function") { cb(true); }
	};
	xhr.send();
}
'''


def patch_asp(text, shim_ver):
    # 380 firmware has no /js/httpApi.js; page never references the object
    text = replace_once(text,
        '''<script language="JavaScript" type="text/javascript" src="/js/httpApi.js"></script>\n''',
        '', "remove httpApi.js")
    # register the /_api/ shim right after jQuery, before any business script
    text = replace_once(text,
        '''<script language="JavaScript" type="text/javascript" src="/js/jquery.js"></script>\n''',
        '''<script language="JavaScript" type="text/javascript" src="/js/jquery.js"></script>\n'''
        '''<script type="text/javascript" src="/res/xwt-arm380-api.js?v=arm380-''' + shim_ver + '''"></script>\n'''
        + ICON_CSS,
        "inject api shim")
    # 1.5-generation software center lives at /Module_Softcenter.asp; on 380 the
    # page is /Main_Soft_center.asp, so the back button landed on a blank 404.
    text = replace_once(text,
        '''location.href = "/Module_Softcenter.asp";''',
        '''location.href = "/Main_Soft_center.asp";''',
        "softcenter back url")
    # Koolshare 1.0 /res/softcenter.js starts with `var $j = jQuery.noConflict();`
    # which RELEASES `$`. The upstream UI uses `$` everywhere -> every call site
    # throws "$ is not a function". Restore the alias right after that script.
    text = replace_once(text,
        '''<script language="JavaScript" type="text/javascript" src="/res/softcenter.js"></script>\n''',
        '''<script language="JavaScript" type="text/javascript" src="/res/softcenter.js"></script>\n'''
        '''<script type="text/javascript">window.$ = window.jQuery;</script>\n''',
        "restore jQuery $ alias")
    # 380: the log tab used to re-poll ss_log.sh every 1.5 s. Each poll spawns a
    # shell + jq on a 2-core router, and the user asked for a static view.
    # setTimer becomes a no-op, so the viewer loads once per tab activation.
    text = replace_once(text,
        '''        setTimer: function(fn, delay) { return setTimeout(fn, delay); },''',
        '''        /* arm380: 查看日志改为单次加载，不再每 1.5 秒自动刷新 */
        setTimer: function() { return 0; },''',
        "log viewer no auto refresh")
    text = replace_once(text,
        '''$("#ac3100_log_status").text(snapshot.truncated ? "显示最近 2000 行 / 128 KiB；每 1.5 秒刷新" : "每 1.5 秒自动刷新");''',
        '''$("#ac3100_log_status").text((snapshot.truncated ? "显示最近 2000 行 / 128 KiB" : "已加载当前日志") + "；不自动刷新，重新切换本页可更新");''',
        "log viewer status text")
    text = replace_once(text,
        '''        error: function(message) { $("#ac3100_log_status").text(message); }''',
        '''        error: function(message) { $("#ac3100_log_status").text(String(message).replace("正在重试…", "请重新切换本页")); }''',
        "log viewer error text")
    # an unfocused window must still be able to load the log once
    text = replace_once(text,
        '''isActive: function() { return E("tablet_10").style.display !== "none" && !document.hidden; },''',
        '''isActive: function() { return E("tablet_10").style.display !== "none"; },''',
        "log viewer isActive")

    # 380 httpd has no /_upload route (httpdb's upload endpoint): the restore
    # button cannot push the backup file into /tmp/upload. Replace the uploader
    # with a chunked /_api/ upload; xwt_backup_upload.sh reassembles it.
    try:
        _up_i = text.index("function upload_ss_backup() {")
        _up_j = text.index(chr(10) + "}" + chr(10), _up_i) + 3
        _up_old = text[_up_i:_up_j]
        if "/_upload" not in _up_old:
            raise ValueError("upload_ss_backup no longer posts to /_upload")
        text = text[:_up_i] + NEW_UPLOAD_JS + text[_up_j:]
    except ValueError as exc:
        raise SystemExit("PATCH FAILED [upload_ss_backup]: %s" % exc)

    # 380 httpd serves ONLY .js/.xml out of any directory; *.json / *.sh /
    # *.tar.gz are a 404 before authentication. ss_conf.sh republishes every
    # export under a *.js twin. The 386 "_root/files/..." route does not exist
    # on 380: httpd maps /Module_<name> to /koolshare/webs/<name>, and
    # install.sh links /koolshare/webs/Module_files to /tmp/files.
    # The binary package is published as single-line base64 (.b64.js) because
    # httpd truncates binary .js at 43100 bytes; the UI decodes it with atob.
    text = replace_once(text,
        '''url: "_root/files/shadowsocks.tar.gz"''',
        '''url: "/Module_files/shadowsocks.tar.gz.b64.js"''',
        "export url: package (base64)")
    text = replace_once(text,
        '''url: "_root/files/ssconf_backup.sh"''',
        '''url: "/Module_files/ssconf_backup.sh.js"''',
        "export url: legacy sh")
    text = replace_once(text,
        '''url: "_root/files/ssconf_backup_v2.json"''',
        '''url: "/Module_files/ssconf_backup_v2.json.js"''',
        "export url: json")

    # Log/status downloads use a plain <a href>; repoint them too.
    for old_href, new_href in (
        ('b.href = "_root/files/" + pkg_name + "_" + db_ss["ss_basic_version_local"] + ".tar.gz"',
         'b.href = "/Module_files/" + pkg_name + "_" + db_ss["ss_basic_version_local"] + ".tar.gz.js"'),
        ('b.href = "_root/files/ssf_status.txt"',
         'b.href = "/Module_files/ssf_status.txt.js"'),
        ('b.href = "_root/files/ssc_status.txt"',
         'b.href = "/Module_files/ssc_status.txt.js"'),
        ('b.href = "_root/files/"+ dns_log["ss_basic_logname"] +".txt"',
         'b.href = "/Module_files/"+ dns_log["ss_basic_logname"] +".txt.js"'),
        ('b.href = "_root/files/dns_dig_result.txt"',
         'b.href = "/Module_files/dns_dig_result.txt.js"'),
    ):
        text = replace_once(text, old_href, new_href, "log href %s" % old_href[:46])

    # The probe payload is cached so a 17 MB base64 package is fetched once.
    text = replace_once(text,
        'var prepared_route_files = {};',
        'var prepared_route_files = {};\nvar route_file_cache = {};',
        "route cache declaration")
    text = replace_once(text,
        '\tprepared_route_files[arg] = "pending";\n',
        '\tprepared_route_files[arg] = "pending";\n\tdelete route_file_cache[meta.url];\n',
        "route cache reset on new export")

    # Replace the probe + blob-download helpers wholesale.
    try:
        i = text.index('function poll_route_file_ready(arg, meta, retry) {')
        j = text.index('\nfunction prepare_route_file', i)
        text = text[:i] + NEW_PROBE_JS + text[j + 1:]
        i = text.index('function download_blob_file(url, fileName, cb) {')
        j = text.index('\nfunction check_node_migration_notice', i)
        text = text[:i] + NEW_DOWNLOAD_JS + text[j + 1:]
    except ValueError as exc:
        raise SystemExit("PATCH FAILED [download helpers]: %s" % exc)
    # XWT_ARM380_RUN_GUARD: httpd expands <% nvram_get(...) %> before serving
    # the ASP, so the page knows the operation mode without an extra RPC.
    # sw_mode: 1=Router, 2=Repeater, 3=Access Point, 4=Media Bridge.
    text = replace_once(text,
        '''var lan_ipaddr = '<% nvram_get("lan_ipaddr"); %>';''',
        '''var lan_ipaddr = '<% nvram_get("lan_ipaddr"); %>';
var xwt_sw_mode = '<% nvram_get("sw_mode"); %>';''',
        "inject sw_mode")

    text = replace_once(text,
        '''function init() {
	show_menu(menu_hook);''',
        '''function xwt_show_mode_block() {
	var names = { "2": "中继模式", "3": "AP 模式", "4": "媒体桥接模式" };
	var label = names[xwt_sw_mode] || ("非路由模式 (sw_mode=" + xwt_sw_mode + ")");
	var box = document.createElement("div");
	box.style.cssText = "position:fixed;left:0;top:0;right:0;bottom:0;z-index:99999;background:rgba(0,0,0,.72);display:flex;align-items:center;justify-content:center;";
	box.innerHTML = '<div style="max-width:560px;padding:26px 30px;background:#fff;color:#222;border-radius:8px;box-shadow:0 8px 40px rgba(0,0,0,.5);font:14px/1.9 sans-serif;">'
		+ '<div style="font-size:18px;font-weight:600;margin-bottom:12px;color:#c0392b;">Xray Web Tool 已停止运行</div>'
		+ '<div>当前设备工作在 <b>' + label + '</b>，不是路由模式。</div>'
		+ '<div>本插件需要接管 WAN、NAT 与 DNS，仅支持<b>路由模式</b>；为避免破坏网络，所有代理进程、定时任务与后台采样均已停止。</div>'
		+ '<div style="margin-top:12px;color:#666;">请在「系统管理 → 操作模式」切回路由模式后重新打开本页。</div>'
		+ '<div style="margin-top:20px;"><a href="javascript:history.back();" style="display:inline-block;padding:7px 18px;background:#2d7d46;color:#fff;border-radius:4px;text-decoration:none;">返回软件中心</a></div>'
		+ '</div>';
	document.body.appendChild(box);
}
function init() {
	show_menu(menu_hook);
	if (xwt_sw_mode && xwt_sw_mode !== "1") {
		xwt_show_mode_block();
		return;
	}''',
        "ASP mode block")

    # cosmetic: the upstream hardcodes the reviewed model in two page titles.
    text = text.replace('"RT-AC3100 Xray Web Tool"', '"R6300V2 Xray Web Tool"')
    # 380 ships /res/Softerware_center.css under a different name; our package
    # brings a copy as /res/softcenter.css so this href resolves.
    return text


API_SHIM = r'''/*
 * xwt-arm380-api.js -- compatibility shim for Koolshare 1.0 (arm380 / R6300V2)
 *
 * upstream (1.5-generation software center) talks to httpdb via /_api/:
 *   GET  /_api/<prefix>      -> {"result":[ {dbus key/value}, ... ]}
 *   POST /_api/              -> {"id":..,"method":"x.sh","params":[..],"fields":{..}}
 *                              returns {"result": <id>}
 *
 * 380.70 httpd has no httpdb and no /_api/ route. Instead it provides:
 *   GET  /dbconf?p=<prefix>  -> "var db_<prefix>=(function(){var o={}; ... })();"
 *   POST /applydb.cgi?p=<p>  -> writes every posted field into dbus, and runs
 *                              SystemCmd=<script>.sh from /koolshare/scripts/
 *
 * This shim installs a jQuery ajaxTransport for every /_api/ request so the
 * 674 KB upstream UI needs ZERO call-site changes.
 */
(function ($) {
	"use strict";
	if (!$ || !$.ajaxTransport) { return; }
	var API = "/_api/";
	/* 380's httpd has no /_temp/ route (the upload cache). The device mirrors
	 * /tmp/upload/* into /res/xwt_tmp/*, so rewrite these reads. */
	var TMP = "/_temp/";
	var TMP_MIRROR = "/res/xwt_tmp/";
	/* Tracks how long the current apply has been waiting for its log, so the
	 * progress dialog can be force-closed instead of hanging forever. */
	var logWaitKey = "", logWaitStart = 0;
	/* arm380: how many times each panel asked for data. The snapshot and the
	 * status payload are plain files under /res/xwt_tmp, so only every Nth
	 * poll is sent through /_api/ (which makes httpd fork a shell). */
	var dashPolls = 0, statusPolls = 0;

	function own(o, k) { return Object.prototype.hasOwnProperty.call(o, k); }
	function done(complete, obj) {
		complete(200, "success", { json: obj }, "application/json");
	}
	function flat(raw) {
		var obj = {};
		if (raw && typeof raw === "object") {
			for (var k in raw) {
				if (own(raw, k)) { obj[k] = String(raw[k]); }
			}
		}
		return obj;
	}

	/* This httpd serves .js without a charset and the firmware default is GBK,
	 * so plain XHR mangles the Chinese status text. Read raw bytes and decode
	 * as UTF-8 explicitly. */
	function readUtf8(url, ok, fail) {
		var x = new XMLHttpRequest();
		try {
			x.open("GET", url, true);
			x.responseType = "arraybuffer";
			x.onload = function () {
				if (x.status >= 400) { if (fail) { fail(); } return; }
				var txt = "";
				try {
					if (window.TextDecoder && x.response) {
						txt = new TextDecoder("utf-8").decode(new Uint8Array(x.response));
					} else if (!x.response) {
						txt = "";
					} else {
						txt = String(x.response);
					}
				} catch (e) { txt = ""; }
				ok(txt);
			};
			x.onerror = function () { if (fail) { fail(); } };
			x.send(null);
		} catch (e) {
			if (fail) { fail(); }
		}
	}

	/* GET: /dbconf declares window.db_<prefix> as a plain object. */
	function readDbconf(prefix, cb) {
		/* fss_data / fss_node are 1.5-generation httpdb virtual tables. On 1.0
		 * the same data lives in dbus and is exposed as `var db_<prefix>` by
		 * /dbconf. Use a synchronous XHR + global eval: the node-management
		 * tab cannot tolerate another async hop, but MUST see real node data. */
		if (/^fss_/.test(prefix)) {
			var fssObj = {};
			try {
				var x = new XMLHttpRequest();
				x.open("GET", "/dbconf?p=" + encodeURIComponent(prefix) + "&v=" + new Date().getTime(), false);
				x.send(null);
				var txt = x.responseText || "";
				if (x.status === 200 && txt.indexOf("var db_" + prefix) === 0) {
					(0, eval)(txt);
					fssObj = flat(window["db_" + prefix]);
				}
			} catch (e) { fssObj = {}; }
			var exported = (window.XWT_FSS_EXPORT && window.XWT_FSS_EXPORT[prefix]) || {};
			for (var ek in exported) { if (own(exported, ek)) { fssObj[ek] = String(exported[ek]); } }
			cb(fssObj);
			return;
		}
		var safe = String(prefix).replace(/[^A-Za-z0-9_]/g, "");
		var varName = "db_" + safe;
		var stamp = new Date().getTime() + "_" + Math.floor(Math.random() * 10000);
		$.ajax({
			type: "GET",
			url: "/dbconf?p=" + encodeURIComponent(prefix) + "&v=" + stamp,
			dataType: "script",
			cache: true,
			success: function () {
				var merged = flat(window[varName]);
				/* fss_* virtual tables have no dbus equivalent on 1.0.
				 * An optional exported file from the bridge can populate them. */
				if (/^fss_/.test(prefix)) {
					var exported = window.XWT_FSS_EXPORT && window.XWT_FSS_EXPORT[prefix];
					if (exported) {
						for (var k in exported) { if (own(exported, k)) { merged[k] = String(exported[k]); } }
					}
				}
				cb(merged);
			},
			error: function () { cb({}); }
		});
	}

	/* POST: write fields through applydb.cgi, then let fss_api_bridge.sh run method. */
	function writeRpc(options, complete) {
		var payload = {};
		/* jQuery allows `data` to be a FUNCTION evaluated just before sending
		 * (poll_text_file does exactly this). Resolve it, or every RPC from the
		 * log poller arrives with an EMPTY method. */
		var raw = options.data;
		if (typeof raw === "function") { try { raw = raw(); } catch (e) { raw = ""; } }
		try { payload = JSON.parse(raw || "{}"); } catch (e) { payload = {}; }
		var id = (payload.id === undefined || payload.id === null) ? "" : String(payload.id);
		var method = String(payload.method || "");
		/* The 1 s dashboard poll and the 6 s status poll only need a file.
		 * Routing them through /_api/ made httpd exec a shell every second,
		 * and each exec re-reads ~490 KB of busybox out of the squashfs --
		 * that is what kept mtdblock3 at 30-50% while the page was open.
		 * Read the mirrored file directly; fire the real RPC only every Nth
		 * poll so a dead daemon still gets restarted by the bridge. */
		if (method === "ss_dashboard.sh") {
			dashPolls++;
			if (dashPolls % 30 !== 0) { readDashPayload(complete); return; }
		}
		if (method === "ss_status.sh") {
			statusPolls++;
			if (statusPolls % 15 !== 0) { readStatusPayload(complete); return; }
		}
		if (method === "ss_config.sh") { logWaitStart = 0; }
		var params = payload.params || [];
		var fields = payload.fields || {};
		/* /applydb.cgi?p=X ONLY writes keys starting with X. Upstream saves
		 * nodes under fss_node_* / fss_data_*, which a single p=ss silently
		 * drops. Group by prefix and submit each group to its own p=. */
		var groups = {};
		for (var k in fields) {
			if (!own(fields, k)) { continue; }
			var grp;
			if (k.indexOf("ss") === 0) { grp = "ss"; }
			else if (k.indexOf("fss") === 0) { grp = "fss"; }
			else {
				var cut = k.indexOf("_");
				grp = cut > 0 ? k.slice(0, cut) : k;
			}
			if (!groups[grp]) { groups[grp] = {}; }
			groups[grp][k] = fields[k] == null ? "" : String(fields[k]);
		}

		/* hand the RPC target to the bridge through ss_ dbus fields, so
		 * SystemCmd stays a single plain script name. */
		var ssForm = groups["ss"] || (groups["ss"] = {});
		ssForm["ss_fss_bridge_method"] = method;
		ssForm["ss_fss_bridge_id"] = id;
		ssForm["ss_fss_bridge_params"] = (params || []).join(" ");
		ssForm["action_mode"] = " Refresh ";
		ssForm["current_page"] = "Module_shadowsocks.asp";
		if (method && method !== "dummy_script.sh" && /^[A-Za-z0-9_.-]+\.sh$/.test(method)) {
			ssForm["SystemCmd"] = "fss_api_bridge.sh";
		}

		/* The ss group carries SystemCmd -> httpd may run the script right
		 * away, so it must be submitted LAST, after every other field exists. */
		var order = [];
		for (var g in groups) { if (own(groups, g) && g !== "ss") { order.push(g); } }
		order.push("ss");
		var idx = 0;
		/* ss_log.sh must return LOG TEXT, not an id: the realtime log viewer
		 * runs FssLogViewer.decode(result) and loops forever on anything else. */
		function pollLogOutput() {
			var tries = 0;
			/* The backend never writes /tmp/upload/ss_log.operation, so its
			 * `operation` is always empty. Echo the operation the page is
			 * waiting for, otherwise get_realtime_log loops forever. */
			function stamp(snap) {
				/* get_realtime_log() bails out with {done:false} whenever
				 * snapshot.operation !== realtimeOperationId, BEFORE it looks at
				 * the text. The backend can never produce that id, so clear it
				 * and always answer with "" -> the next poll skips the check. */
				snap.operation = "";
				try { window.realtimeOperationId = ""; } catch (e) {}
				snap.view = snap.view || "plugin";
				snap.exists = true;
				snap.truncated = !!snap.truncated;
				snap.text = String(snap.text || "");
				return JSON.stringify(snap);
			}
			if (!logWaitStart) { logWaitStart = new Date().getTime(); }
			function poll() {
				readUtf8("/res/xwt_tmp/xwt_rt.js?v=" + new Date().getTime(), function (txt) {
					var body = String(txt || ""), snap;
					try { snap = JSON.parse(body); } catch (e) { snap = { text: "" }; }
						/* Safety net: an apply can take ~2 min on this hardware and
						 * the dialog must never hang forever. */
						if (logWaitStart > 0 && new Date().getTime() - logWaitStart > 150000) {
							snap.text = String(snap.text || "") + "\n[arm380] 操作超时，已停止等待。\nXU6J03M6\n";
						}
						done(complete, { result: stamp(snap) });
				}, function () {
					if (++tries < 15) { setTimeout(poll, 200); }
					else { done(complete, { result: stamp({ text: "" }) }); }
				});
			}
			setTimeout(poll, 120);
		}
		/* ss_status.sh hands its payload to httpdb via http_response -> /_resp/id.
		 * On 380 the script writes the same string into its cache file, so read
		 * that instead; without it the panel sits on "Waiting ..." forever. */
		function readStatusPayload(complete) {
			var tries = 0;
			function attempt() {
				readUtf8(TMP_MIRROR + "ss_status_front.txt.js?_=" + new Date().getTime(),
					function (txt) {
						var body = String(txt == null ? "" : txt).replace(/[\r\n]+$/, "");
						done(complete, { result: body });
					},
					function () {
						if (++tries < 4) { setTimeout(attempt, 350); }
						else { done(complete, { result: "" }); }
					});
			}
			attempt();
		}
		/* ss_dashboard.sh answers through /_resp/<id> as well; the bridge keeps a
		 * rendered copy at /res/xwt_dash.js (refreshed in the background). */
		function readDashPayload(complete) {
			readUtf8("/res/xwt_tmp/xwt_dash.js?_=" + new Date().getTime(),
				function (txt) {
					var body = String(txt == null ? "" : txt);
					done(complete, { result: body || "{}" });
				},
				function () { done(complete, { result: "{}" }); });
		}
		/* ss_update.sh check answers through http_response. The bridge parks the
		 * tag in /res/xwt_tmp/xwt_upd.js together with the request id, so a stale
		 * file from an earlier click can never be mistaken for this answer. */
		function readUpdateCheck(complete) {
			var tries = 0, want = String(id);
			function attempt() {
				readUtf8("/res/xwt_tmp/xwt_upd.js?_=" + new Date().getTime(), function (txt) {
					var body = String(txt == null ? "" : txt);
					var got = /"id"\s*:\s*"([^"]*)"/.exec(body);
					var val = /"result"\s*:\s*"([^"]*)"/.exec(body);
					if (got && val && got[1] === want) { done(complete, { result: val[1] }); return; }
					if (++tries < 22) { setTimeout(attempt, 700); }
					else { done(complete, { result: "unavailable" }); }
				}, function () {
					if (++tries < 22) { setTimeout(attempt, 700); }
					else { done(complete, { result: "unavailable" }); }
				});
			}
			attempt();
		}
		function nextGroup() {
			if (idx >= order.length) {
				if (String(method) === "ss_log.sh") { pollLogOutput(); }
				else if (String(method) === "ss_status.sh") { readStatusPayload(complete); }
				else if (String(method) === "ss_dashboard.sh") { readDashPayload(complete); }
				else if (String(method) === "ss_update.sh") { readUpdateCheck(complete); }
				else { done(complete, { result: id }); }
				return;
			}
			var key = order[idx++];
			$.ajax({
				type: "POST",
				url: "/applydb.cgi?p=" + encodeURIComponent(key),
				data: $.param(groups[key]),
				contentType: "application/x-www-form-urlencoded",
				dataType: "text",
				cache: false,
				complete: function () { nextGroup(); }
			});
		}
		nextGroup();
	}

	$.ajaxTransport("+*", function (options) {
		var url = options.url || "";
		/* Rewrite /_temp/<file> to its mirrored copy. Keeps every one of the 19
		 * upstream /_temp/ consumers (status, webtest, dns lists, logs) working
		 * without touching a single call site. */
		if (url.indexOf(TMP) === 0) {
			return {
				send: function (headers, complete) {
					var name = url.slice(TMP.length).split("?")[0].split("&")[0].split("/").pop();
					/* ss_status.txt is only written when failover is enabled; the
					 * front cache always carries the same payload on this build. */
					if (name === "ss_status.txt") { name = "ss_status_front.txt"; }
					readUtf8(TMP_MIRROR + name + ".js?_=" + new Date().getTime(),
						function (txt) { complete(200, "OK", { text: txt, html: txt }); },
						function () { complete(404, "Not Found", { text: "", html: "" }); });
				},
				abort: function (callback) { if (callback) { callback(); } }
			};
		}
		if (url.indexOf(API) !== 0) { return undefined; }
		return {
			send: function (headers, complete) {
				var tail = url.slice(API.length).split("?")[0].split("&")[0];
				if ((options.type || "GET").toUpperCase() === "POST") {
					writeRpc(options, complete);
				} else {
					readDbconf(tail, function (obj) { done(complete, { result: [obj] }); });
				}
			},
			abort: function (callback) { if (callback) { callback(); } }
		};
	});
})(window.jQuery);

/* Merlin 386 exposes a global Base64 helper; koolshare 380 does not.
 * The upstream UI calls Base64.encode/decode 31 times (node JSON, profiles). */
if (typeof window.Base64 === "undefined") {
	window.Base64 = {
		_keyStr: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=",
		encode: function (input) {
			var output = "", chr1, chr2, chr3, enc1, enc2, enc3, enc4, i = 0;
			input = window.Base64._utf8_encode(String(input));
			while (i < input.length) {
				chr1 = input.charCodeAt(i++);
				chr2 = input.charCodeAt(i++);
				chr3 = input.charCodeAt(i++);
				enc1 = chr1 >> 2;
				enc2 = ((chr1 & 3) << 4) | (chr2 >> 4);
				enc3 = isNaN(chr2) ? 64 : (((chr2 & 15) << 2) | (chr3 >> 6));
				enc4 = isNaN(chr3) ? 64 : (chr3 & 63);
				output += this._keyStr.charAt(enc1) + this._keyStr.charAt(enc2)
					+ this._keyStr.charAt(enc3) + this._keyStr.charAt(enc4);
			}
			return output;
		},
		decode: function (input) {
			var output = "", chr1, chr2, chr3, enc1, enc2, enc3, enc4, i = 0;
			input = String(input).replace(/[^A-Za-z0-9\+\/\=]/g, "");
			while (i < input.length) {
				enc1 = this._keyStr.indexOf(input.charAt(i++));
				enc2 = this._keyStr.indexOf(input.charAt(i++));
				enc3 = this._keyStr.indexOf(input.charAt(i++));
				enc4 = this._keyStr.indexOf(input.charAt(i++));
				chr1 = (enc1 << 2) | (enc2 >> 4);
				chr2 = ((enc2 & 15) << 4) | (enc3 >> 2);
				chr3 = ((enc3 & 3) << 6) | enc4;
				output += String.fromCharCode(chr1);
				if (enc3 !== 64) { output += String.fromCharCode(chr2); }
				if (enc4 !== 64) { output += String.fromCharCode(chr3); }
			}
			return window.Base64._utf8_decode(output);
		},
		_utf8_encode: function (s) {
			s = s.replace(/\r\n/g, "\n");
			var out = "";
			for (var n = 0; n < s.length; n++) {
				var c = s.charCodeAt(n);
				if (c < 128) { out += String.fromCharCode(c); }
				else if (c > 127 && c < 2048) {
					out += String.fromCharCode((c >> 6) | 192);
					out += String.fromCharCode((c & 63) | 128);
				} else {
					out += String.fromCharCode((c >> 12) | 224);
					out += String.fromCharCode(((c >> 6) & 63) | 128);
					out += String.fromCharCode((c & 63) | 128);
				}
			}
			return out;
		},
		_utf8_decode: function (s) {
			var out = "", i = 0, c, c2, c3;
			while (i < s.length) {
				c = s.charCodeAt(i);
				if (c < 128) { out += String.fromCharCode(c); i++; }
				else if (c > 191 && c < 224) {
					c2 = s.charCodeAt(i + 1);
					out += String.fromCharCode(((c & 31) << 6) | (c2 & 63)); i += 2;
				} else {
					c2 = s.charCodeAt(i + 1); c3 = s.charCodeAt(i + 2);
					out += String.fromCharCode(((c & 15) << 12) | ((c2 & 63) << 6) | (c3 & 63)); i += 3;
				}
			}
			return out;
		}
	};
}
'''

# 380's ash does not expand aliases in non-interactive scripts, while the
# upstream defines echo_date / urldecode as aliases. Without function
# fallbacks, ss_node_subscribe.sh silently gets an EMPTY link list.
ALIAS_SHIM = r'''# XWT_ARM380_ALIAS_SHIM_V2: 380 ash does not expand aliases in scripts.
echo_date() { echo "【$(TZ=UTC-8 date -R '+%Y%m%d %X')】: $*"; }
urldecode() { sed -e 's/+/ /g' -e 's/%\([0-9A-Fa-f][0-9A-Fa-f]\)/\\x\1/g' | xargs -0 printf '%b'; }
http_response() { printf '%s' "$1" > /tmp/upload/.xwt_response 2>/dev/null; }
'''

# 380's httpd has no /_temp/ route, but 19 upstream call sites read the upload
# cache from it (status, webtest, dns lists, subscribe log, proc status...).
# A /www/_temp symlink is impossible (/www is a read-only squashfs), so mirror
# the files into /res/xwt_tmp/ and let the JS transport rewrite the URL.
# NOTE: this httpd only serves whitelisted extensions (.js/.xml/.css/.png...);
# .txt and .json answer 404, so every mirror file is stored as "<name>.js".
RUN_GUARD = r'''
#!/bin/sh
# xwt_run_guard.sh -- arm380: one authority for "may the proxy run?".
# 380/386 nvram sw_mode: 1=Router, 2=Repeater, 3=Access Point, 4=Media Bridge.
# Only router mode owns WAN/NAT/DNS, so every start path passes through here.
XWT_KSROOT=/koolshare

xwt_router_mode(){
  local sw lp
  sw="$(nvram get sw_mode 2>/dev/null)"
  if [ -n "$sw" ]; then
    [ "$sw" = "1" ] && return 0
    return 1
  fi
  # Builds without sw_mode: AP/bridge gets its LAN address from upstream
  # DHCP, router mode uses a static LAN address.
  lp="$(nvram get lan_proto 2>/dev/null)"
  [ "$lp" != "dhcp" ]
}

xwt_switch_on(){
  [ "$(dbus get ss_basic_enable 2>/dev/null)" = "1" ]
}

xwt_may_run(){
  xwt_router_mode || return 1
  xwt_switch_on || return 1
  return 0
}

xwt_publish_mode(){
  if xwt_router_mode; then
    dbus set ss_basic_run_mode="router" >/dev/null 2>&1
  else
    dbus set ss_basic_run_mode="nonrouter" >/dev/null 2>&1
    dbus set ss_basic_status="0" >/dev/null 2>&1
  fi
}

# disable_ss() predates the arm380 port and knows nothing about these two
# resident daemons; without this they keep sampling a stopped proxy forever.
xwt_stop_daemons(){
  local p
  for p in $(ps w 2>/dev/null | grep -E '[x]wt_dash_sampler\.sh|[x]wt_tmp_mirror\.sh' | awk '{print $1}'); do
    kill "$p" 2>/dev/null
  done
  rm -f /var/run/xwt_dash_sampler.pid /var/run/xwt_tmp_mirror.pid
  rm -f /tmp/xwt_dash.lock /tmp/xwt_dash_traffic.lock
}

# Called when the master switch turns ON. UI plumbing (tmp mirror) also needs
# to come back if the router was booted while the proxy was disabled.
xwt_start_daemons(){
  xwt_may_run || return 0
  mkdir -p "$XWT_KSROOT/res/xwt_tmp" 2>/dev/null
  if ! ps w | grep -q '[x]wt_tmp_mirror.sh'; then
    if [ -x /usr/sbin/start-stop-daemon ]; then
      start-stop-daemon -S -b -q -m -p /var/run/xwt_tmp_mirror.pid \
        -x /bin/sh -- "$XWT_KSROOT/scripts/xwt_tmp_mirror.sh" >/dev/null 2>&1
    else
      /bin/sh "$XWT_KSROOT/scripts/xwt_tmp_mirror.sh" >/dev/null 2>&1 &
    fi
  fi
  if ! ps w | grep -q '[x]wt_dash_sampler.sh$'; then
    if [ -x /usr/sbin/start-stop-daemon ]; then
      start-stop-daemon -S -b -q -m -p /var/run/xwt_dash_sampler.pid \
        -x /bin/sh -- "$XWT_KSROOT/scripts/xwt_dash_sampler.sh" >/dev/null 2>&1
    else
      /bin/sh "$XWT_KSROOT/scripts/xwt_dash_sampler.sh" >/dev/null 2>&1 &
    fi
  fi
}
'''


TMP_MIRROR = r'''#!/bin/sh
# xwt_tmp_mirror.sh -- mirror /tmp/upload/* into /res/xwt_tmp/ for arm380.
# /res lives on JFFS, so xwt_tmp is mounted as tmpfs (see nat-start): rewriting
# it every second used to put the flash in permanent D-state (load 8+ on 2 cores).
# "test -nt" keeps the loop fork-free; cp only runs when a source really changed.
DST=/koolshare/res/xwt_tmp
mkdir -p "$DST" 2>/dev/null
# XWT_ARM380_RUN_GUARD: the master switch turns off EVERYTHING the plugin
# runs, this mirror included. Bridge writes its own *.js replies directly,
# so the panel stays usable without it.
xwt_may_run() { return 0; }
[ -f /koolshare/scripts/xwt_run_guard.sh ] && . /koolshare/scripts/xwt_run_guard.sh
while : ; do
	xwt_may_run || exit 0
	for f in /tmp/upload/*.txt /tmp/upload/*.json; do
		[ -f "$f" ] || continue
		b=${f##*/}
		if [ ! -f "$DST/$b.js" ] || [ "$f" -nt "$DST/$b.js" ]; then
			cp -f "$f" "$DST/$b.js" 2>/dev/null
		fi
	done
	# Nothing here needs a lock any more: the dashboard poll reads the
	# snapshot straight from tmpfs. A 10 s sweep is enough to publish the
	# upload cache, and it halves the wake-ups plus two forks per round.
	sleep 10
done
'''

SAMPLER = r'''#!/bin/sh
# XWT_ARM380_RUN_GUARD: do not keep sampling a proxy that must be off.
xwt_may_run() { return 0; }
[ -f /koolshare/scripts/xwt_run_guard.sh ] && . /koolshare/scripts/xwt_run_guard.sh
# xwt_dash_sampler.sh -- 1 Hz dashboard sampler for Merlin 380 / R6300V2.
#
# arm380 notes, all measured on the device:
#  * ss_dashboard.sh used to be re-executed for every UI poll and forked ~13
#    helpers (pidof, dbus, sed, awk, jq, curl) per sample. One sample cost 8 s
#    against a 1 s poll and an 8 s XHR timeout, so polls overlapped four to
#    eight deep: load 6+ and the mtdblock3 thread (squashfs reads for every
#    exec) burned ~76% of a core.
#  * Xray's /debug/vars itself takes 0.2-1.4 s to answer here, so traffic
#    cannot be read inside the 1 s loop without breaking the cadence. It runs
#    in its own daemon that publishes the counters with the uptime it saw;
#    the 1 s loop only reads /proc plus that file.
#  * / is squashfs on mtdblock3 and only ~8 MB of page cache is kept, so every
#    exec re-reads ~490 KB of NAND. The shell applets therefore run from a
#    tmpfs copy (see below).

export PATH=/koolshare/bin:/koolshare/scripts:/bin:/usr/bin:/sbin:/usr/sbin

KS=/koolshare
ST=/tmp/xwt_dash
JQ=$KS/bin/jq
# The UI transport reads /res/xwt_tmp/xwt_dash.js, so publish there too. Only
# when that path really is the tmpfs mount: walking it every second on JFFS is
# what used to keep the flash busy.
WRES=0
mount 2>/dev/null | grep -q 'res/xwt_tmp type tmpfs' && WRES=1

ONCE=0
TRAFFIC_DAEMON=0
case "$1" in
	--once) ONCE=1 ;;
	--traffic-daemon) TRAFFIC_DAEMON=1 ;;
esac
mkdir -p "$ST" 2>/dev/null

# arm380: run the applets from tmpfs instead of re-reading them from NAND.
if [ "$ONCE" = 0 ] && [ ! -x /tmp/xwt_bin/busybox ]; then
	mkdir -p /tmp/xwt_bin 2>/dev/null
	cp -f /bin/busybox /tmp/xwt_bin/busybox 2>/dev/null
	chmod 755 /tmp/xwt_bin/busybox 2>/dev/null
	for _a in awk nc mv sleep usleep pidof grep ps sed cat tr sh; do
		ln -sf busybox "/tmp/xwt_bin/$_a" 2>/dev/null
	done
fi
[ -x /tmp/xwt_bin/busybox ] && PATH=/tmp/xwt_bin:$PATH && export PATH

cpus=$(grep -c '^processor' /proc/cpuinfo 2>/dev/null)
case "$cpus" in ''|*[!0-9]*) cpus=1 ;; esac

# ------------------------------------------------------------------ traffic --
# Sums every captured outbound counter and stamps the sample with the uptime
# it was taken at, so the UI can compute a rate over the real interval even
# when this loop takes longer than a second.
collect_traffic() {
	_tl=""
	[ -r "$ST/tags" ] && IFS= read -r _tl < "$ST/tags"
	[ -n "$_tl" ] || return 1
	_res=$(printf 'GET /debug/vars HTTP/1.0\r\n\r\n' | nc -w 3 127.0.0.1 11111 2>/dev/null |
		awk -v allow=" $_tl " '
			{
				s = $0
				while (match(s, /"[^"]+":\{"[^{}]*\}/)) {
					blk = substr(s, RSTART, RLENGTH)
					tag = blk; sub(/:.*/, "", tag); gsub(/"/, "", tag)
					if (index(allow, " " tag " ") > 0) {
						if (match(blk, /"uplink":[0-9]+/)) {
							t = substr(blk, RSTART, RLENGTH); sub(/.*:/, "", t); up += t
						}
						if (match(blk, /"downlink":[0-9]+/)) {
							t = substr(blk, RSTART, RLENGTH); sub(/.*:/, "", t); dn += t
						}
						found = 1
					}
					s = substr(s, RSTART + RLENGTH)
				}
			}
			# %.0f, not %d: these counters pass 2^31 within a day of streaming
			# and the integer format folded them into 2147483647.
			END { printf "%.0f %.0f %d", up + 0, dn + 0, found + 0 }')
	set -- $_res
	case "${1:-}" in ''|*[!0-9]*) return 1 ;; esac
	read -r _t _ < /proc/uptime
	printf '%s %s %s %s\n' "${_t:-0}" "$1" "${2:-0}" "${3:-0}" > "$ST/traffic.$$" &&
		mv -f "$ST/traffic.$$" "$ST/traffic"
	return 0
}

if [ "$TRAFFIC_DAEMON" = 1 ]; then
	exec 8>/tmp/xwt_dash_traffic.lock
	flock -n 8 || exit 0
	_td_tick=0
	while :; do
		collect_traffic >/dev/null 2>&1
		# The collector is a child of the sampler. If the sampler dies the
		# collector is reparented to init and keeps running -- and then every
		# "ps | grep xwt_dash_sampler.sh" liveness check sees a live process
		# and never restarts the real sampler, so the panel freezes on a stale
		# snapshot. Exit instead and let whoever notices restart the pair.
		_td_tick=$(( (_td_tick + 1) % 20 ))
		if [ "$_td_tick" = 0 ]; then
			ps w | grep -q '[x]wt_dash_sampler.sh$' || exit 0
		fi
		# One round already costs 0.5-1.4 s inside Xray's expvar handler, so a
		# longer nap only trims the wasted wake-ups: the rate stays exact
		# because the UI differences the stamped samples, not the poll count.
		usleep 500000
	done
fi

# ---------------------------------------------------------------- metadata --
# Rebuild the node/config derived values. Runs on node switch or Xray restart
# only (a handful of times per session), never on the 1 s path.
refresh_meta() {
	_n="$1"
	node="$_n"
	name_json='""'
	kind_json='""'
	net_json='""'
	tags_json='[]'
	taglist=""
	[ -z "$_n" ] && return 1
	dbus get "fss_node_$_n" 2>/dev/null | base64 -d > "$ST/node.json" 2>/dev/null
	[ -s "$ST/node.json" ] || printf '{}' > "$ST/node.json"
	_cfg="$KS/ss/xray.json"
	[ -r "$_cfg" ] || _cfg=/dev/null
	# The programme lives in its own variable: this ash mis-parses a multi-line
	# single-quoted string inside $( ) once it contains parentheses.
	_jqprog='
		($n[0] // {}) as $node | ($c[0] // {}) as $cfg |
		[($cfg.outbounds // [])[] |
			select(.protocol != "freedom" and .protocol != "blackhole"
				and .protocol != "dns" and .protocol != "loopback") |
			.tag | select(type == "string")] as $tags |
		(if (($node.xray_prot // "") | length) > 0 then $node.xray_prot
		 else (($cfg.outbounds[0].protocol) // "") end) as $kind |
		(if (($node.xray_network // "") | length) > 0 then $node.xray_network
		 else (($cfg.outbounds[0].streamSettings.network) // "") end) as $net |
		[(($node.name // "") | tojson), ($kind | tojson), ($net | tojson),
			($tags | tojson), ($tags | join(" "))] | .[]
	'
	_meta=$("$JQ" -rn --slurpfile n "$ST/node.json" --slurpfile c "$_cfg" "$_jqprog" 2>/dev/null)
	[ -n "$_meta" ] || return 1
	{
		IFS= read -r name_json
		IFS= read -r kind_json
		IFS= read -r net_json
		IFS= read -r tags_json
		IFS= read -r taglist
	} <<EOF
$_meta
EOF
	case "$name_json" in ''|null) name_json='""' ;; esac
	case "$kind_json" in ''|null) kind_json='""' ;; esac
	case "$net_json" in ''|null) net_json='""' ;; esac
	case "$tags_json" in ''|null) tags_json='[]' ;; esac
	printf '%s\n' "$taglist" > "$ST/tags.$$" && mv -f "$ST/tags.$$" "$ST/tags"
	rm -f "$ST/node.json"
	return 0
}

# ------------------------------------------------------------------ sample --
# Emits one JSON document on stdout. Everything except the traffic counters is
# read from /proc; set()/read/printf are builtins, so a tick only forks for
# pidof (every 5th tick), the mv below and the usleep.
sample() {
	read -r _upf _idle < /proc/uptime
	_up=${_upf%.*}
	case "$_up" in ''|*[!0-9]*) _up=0; _upf=0 ;; esac

	read -r _cpu < /proc/stat
	set -- $_cpu
	shift
	_total=0
	for _v in "$@"; do _total=$((_total + _v)); done

	# pidof only every 5th tick. /proc/<pid>/cmdline is NUL separated, but
	# ash's builtin read drops the NULs and returns the whole argv, so the
	# check needs no tr helper.
	if [ "$_tick" = 0 ] || [ -z "$pid" ] || [ ! -r "/proc/$pid/stat" ]; then
		pid=""
		for _c in $(pidof xray 2>/dev/null); do
			read -r _cl < "/proc/$_c/cmdline" 2>/dev/null
			case "$_cl" in
				*xray-test*) continue ;;
				*'/koolshare/ss/xray.json'*) pid="$_c"; break ;;
			esac
		done
	fi

	_rss=0
	_ticks=0
	_start=0
	if [ -n "$pid" ] && [ -r "/proc/$pid/stat" ]; then
		while read -r _k _v _r; do
			case "$_k" in VmRSS:) _rss=$_v; break ;; esac
		done < "/proc/$pid/status"
		read -r _stat < "/proc/$pid/stat"
		_rest=${_stat##*) }
		set -- $_rest
		if [ $# -ge 20 ]; then
			_ticks=$(( $12 + $13 ))
			_start=$20
		fi
	fi
	case "$_rss" in ''|*[!0-9]*) _rss=0 ;; esac

	# Traffic comes from the daemon's snapshot, together with the moment it
	# was read; the UI differences both values so the rate stays correct.
	_t_up=0
	_t_dn=0
	_t_avail=0
	_t_stamp=0
	if [ -r "$ST/traffic" ]; then
		{ read -r _t_stamp _t_up _t_dn _t_avail; } < "$ST/traffic"
	fi
	case "$_t_up" in ''|*[!0-9]*) _t_up=0; _t_avail=0 ;; esac
	case "$_t_dn" in ''|*[!0-9]*) _t_dn=0; _t_avail=0 ;; esac
	case "$_t_stamp" in ''|*[!0-9.]*) _t_stamp=0 ;; esac
	# a sample older than 20 s means the collector is gone
	[ $((_up - ${_t_stamp%%.*})) -gt 20 ] && _t_avail=0
	if [ "$_t_avail" = 1 ]; then _avail_json=true; else _avail_json=false; fi

	# Latency probe: keep it out of the sample path and throttle the launcher
	# so an unreachable node cannot spawn one shell per second.
	_ping=""
	if [ -r /tmp/xwt_dashboard_ping ]; then
		read -r _pn _pt _pr < /tmp/xwt_dashboard_ping
		case "$_pt" in ''|*[!0-9]*) _pt=0 ;; esac
		if [ "$_pn" = "$node" ] && [ "$_up" -ge "$_pt" ] && [ $((_up - _pt)) -le 90 ]; then
			_ping=$_pr
		fi
	fi
	if [ -n "$node" ] && [ $((_up - last_probe)) -ge 60 ]; then
		last_probe=$_up
		sh "$KS/scripts/ss_dashboard_ping.sh" "$node" >/dev/null 2>&1 &
	fi

	printf '{"pid":"%s","node":"%s","name":%s,"ping":"%s","kind":%s,"network":%s,' \
		"$pid" "$node" "$name_json" "$_ping" "$kind_json" "$net_json"
	printf '"rss_kb":%s,"cpu_ticks":%s,"start_ticks":%s,"total_ticks":%s,' \
		"$_rss" "$_ticks" "$_start" "$_total"
	printf '"cpus":%s,"uptime":%s,"traffic_uptime":%s,"tags":%s,"traffic_available":%s,' \
		"$cpus" "$_upf" "$_t_stamp" "$tags_json" "$_avail_json"
	printf '"upload":%s,"download":%s}\n' "$_t_up" "$_t_dn"
}

# -------------------------------------------------------------------- loop --
if [ "$ONCE" = 1 ]; then
	# The poll path falls back here when the daemon is not running yet. Take
	# the same lock as the daemon: whoever loses reads what is already there.
	exec 9>/tmp/xwt_dash.lock
	if flock -n 9; then
		_n=$(dbus get fss_node_current 2>/dev/null)
		case "$_n" in ''|*[!0-9]*) _n="" ;; esac
		refresh_meta "$_n" >/dev/null 2>&1
		collect_traffic >/dev/null 2>&1
		_tick=0
		sample
		exit 0
	fi
	[ -r "$KS/res/xwt_tmp/xwt_dash.js" ] && cat "$KS/res/xwt_tmp/xwt_dash.js" && exit 0
	[ -r "$ST/latest" ] && cat "$ST/latest" && exit 0
	exit 1
fi

# Single writer: the bridge and the CGI shim both start the daemon when they
# see a stale snapshot, so two copies can race on a cold boot.
exec 9>/tmp/xwt_dash.lock
flock -n 9 || exit 0

# Traffic collector, restarted if it ever dies.
start_traffic_daemon() {
	# A snapshot younger than 3 s means a collector is already working; this
	# avoids a second copy when two ticks race into the launcher.
	if [ -r "$ST/traffic" ]; then
		{ read -r _tt_stamp _tt_rest; } < "$ST/traffic"
		case "$_tt_stamp" in ''|*[!0-9.]*) _tt_stamp=0 ;; esac
		read -r _tt_now _ < /proc/uptime
		[ $(( ${_tt_now%%.*} - ${_tt_stamp%%.*} )) -lt 3 ] && return 0
	fi
	if ! ps w | grep -q '[x]wt_dash_sampler.sh --traffic-daemon'; then
		# 9>&- keeps the child from inheriting the writer lock: an orphaned
		# collector would otherwise hold it and block every future daemon.
		sh "$0" --traffic-daemon >/dev/null 2>&1 9>&- &
	fi
}
start_traffic_daemon

_loop_node=""
_tick=0
_meta_check=0
while :; do
	# Node / config watch: every 5th tick, plus immediately on Xray restart.
	if [ "$_tick" = 0 ]; then
		_n=$(dbus get fss_node_current 2>/dev/null)
		case "$_n" in ''|*[!0-9]*) _n="" ;; esac
		if [ "$_n" != "$_loop_node" ]; then
			_loop_node="$_n"
			refresh_meta "$_n" >/dev/null 2>&1
			meta_pid=""
		fi
	fi
	if [ -n "$pid" ] && [ "$pid" != "$meta_pid" ]; then
		meta_pid="$pid"
		[ -n "$_loop_node" ] && refresh_meta "$_loop_node" >/dev/null 2>&1
	fi
	# Re-arm the collector every ~30 s if it stopped publishing.
	_meta_check=$(( (_meta_check + 1) % 30 ))
	[ "$_meta_check" = 0 ] && start_traffic_daemon
	# ~30 s cadence: quitting here costs nothing and covers a live mode
	# switch without a reboot. The launchers are guarded too, so nothing
	# respawns us until router mode + master switch are both back.
	[ "$_meta_check" = 0 ] && { xwt_may_run || exit 0; }

	# Publish once per tick. Write-then-rename keeps readers from catching a
	# truncated document.
	if [ "$WRES" = 1 ]; then
		sample > "$KS/res/xwt_tmp/xwt_dash.js.$$" &&
			mv -f "$KS/res/xwt_tmp/xwt_dash.js.$$" "$KS/res/xwt_tmp/xwt_dash.js"
	else
		sample > "$ST/latest.$$" && mv -f "$ST/latest.$$" "$ST/latest"
	fi
	_tick=$(( (_tick + 1) % 5 ))
	# Align to the next whole second instead of sleeping a flat second: the
	# sample itself costs a few hundred ms, which would stretch the cadence.
	read -r _t _ < /proc/uptime
	_f=${_t#*.}
	case "$_f" in ''|*[!0-9]*) _f=0 ;; esac
	_us=$(( (100 - _f) * 10000 ))
	# Less than 300 ms left in this second: wait for the next one instead of
	# squeezing a second tick into the same second.
	[ "$_us" -lt 300000 ] && _us=$((_us + 1000000))
	usleep "$_us"
done
'''


DASH_SHIM = r'''#!/bin/sh
# XWT_ARM380_RUN_GUARD: never cold-start the sampler when it may not run.
xwt_may_run() { return 0; }
[ -f /koolshare/scripts/xwt_run_guard.sh ] && . /koolshare/scripts/xwt_run_guard.sh
# ss_dashboard.sh -- arm380 thin shim over the resident 1 Hz sampler.
#
# The original script rebuilt the whole document on every UI poll: pidof,
# two dbus reads, sed/awk over /proc, jq over the node and Xray config, and a
# curl round trip for the traffic counters. Measured 8 s per run on this
# board, against a 1 s poll and an 8 s XHR timeout, so polls overlapped four
# to eight deep: load 6+ and the mtdblock3 kernel thread (squashfs reads for
# every exec) sitting at ~76% of a core.
#
# xwt_dash_sampler.sh now does that work once a second in a single resident
# shell and publishes /tmp/xwt_dash/latest atomically. This file only reads
# the snapshot and hands it to httpd, so a poll costs ~50 ms and one fork.
# If the sampler is not running yet it is asked for one sample and started,
# so the first poll of a cold boot still gets a valid document.

REQUEST_ID="$1"
export PATH=/koolshare/bin:/koolshare/scripts:/bin:/usr/bin:/sbin:/usr/sbin
ST=/tmp/xwt_dash
KS=/koolshare

snap=""
# The sampler publishes straight into the tmpfs copy httpd serves; the /tmp
# copy is only the fallback for a boot where the mount was refused.
[ -r "$KS/res/xwt_tmp/xwt_dash.js" ] && IFS= read -r snap < "$KS/res/xwt_tmp/xwt_dash.js"
[ -n "$snap" ] || { [ -r "$ST/latest" ] && IFS= read -r snap < "$ST/latest"; }

fresh=0
case "$snap" in
	*'"uptime":'*)
		_u=${snap#*'"uptime":'}
		_u=${_u%%,*}
		_u=${_u%%.*}
		case "$_u" in ''|*[!0-9]*) _u=0 ;; esac
		read -r _nowf _idle < /proc/uptime
		_now=${_nowf%%.*}
		_d=$((_now - _u))
		[ "$_d" -ge 0 ] && [ "$_d" -le 2 ] && fresh=1
		;;
esac

if [ "$fresh" = 0 ]; then
	# Cold cache: one synchronous sample, then make sure the daemon survives.
	snap=$(sh "$KS/scripts/xwt_dash_sampler.sh" --once 2>/dev/null)
	if xwt_may_run && ! ps w | grep -q '[x]wt_dash_sampler.sh$'; then
		sh "$KS/scripts/xwt_dash_sampler.sh" >/dev/null 2>&1 &
	fi
fi

[ -n "$snap" ] || exit 1
if [ "$REQUEST_ID" = read ]; then printf '%s\n' "$snap"; exit 0; fi
case "$REQUEST_ID" in ""|*[!0-9]*) exit 2 ;; esac

# httpd collects the async answer on /_resp/<id>. nc reaches it in ~20 ms;
# curl needed seconds for the same local POST on this board.
body="FSSLOG1:$(printf '%s' "$snap" | base64 | tr -d '\r\n')"
printf 'POST /_resp/%s HTTP/1.0\r\nHost: 127.0.0.1\r\nContent-Type: text/plain\r\nContent-Length: %s\r\n\r\n%s' \
	"$REQUEST_ID" "${#body}" "$body" | nc 127.0.0.1 80 >/dev/null 2>&1
exit 0
'''


BRIDGE = r'''#!/bin/sh
# fss_api_bridge.sh -- arm380 backend for xwt-arm380-api.js
# Invoked by httpd as SystemCmd=fss_api_bridge.sh after /applydb.cgi writes the
# RPC payload into dbus. Never takes arguments on the command line.
KSROOT=/koolshare
# XWT_ARM380_RUN_GUARD: stale-snapshot self-healing must not revive a
# stopped proxy. Default allows, the real guard overrides when present.
xwt_may_run() { return 0; }
xwt_publish_mode() { :; }
[ -f "$KSROOT/scripts/xwt_run_guard.sh" ] && . "$KSROOT/scripts/xwt_run_guard.sh"
xwt_publish_mode
[ -f "$KSROOT/scripts/base.sh" ] && . "$KSROOT/scripts/base.sh"
# xwt_tmp is a tmpfs mount point (empty after every boot)
mkdir -p "$KSROOT/res/xwt_tmp" 2>/dev/null
# httpd spawns SystemCmd with a minimal PATH; every helper script here relies
# on /koolshare/bin (jq, base64, curl), so publish it once for all branches.
PATH="$KSROOT/bin:$KSROOT/scripts:/bin:/usr/bin:/sbin:/usr/sbin"
export PATH

METHOD=$(dbus get ss_fss_bridge_method 2>/dev/null)
PARAMS=$(dbus get ss_fss_bridge_params 2>/dev/null)
REQID=$(dbus get ss_fss_bridge_id 2>/dev/null)
dbus remove ss_fss_bridge_method  >/dev/null 2>&1
dbus remove ss_fss_bridge_params >/dev/null 2>&1
dbus remove ss_fss_bridge_id     >/dev/null 2>&1

echo "$(date +%s) id=${REQID} method=${METHOD} params=${PARAMS}" >> /tmp/xwt_bridge.log
if [ -f /tmp/xwt_bridge.log ] && [ "$(wc -l < /tmp/xwt_bridge.log)" -gt 200 ]; then
	tail -100 /tmp/xwt_bridge.log > /tmp/xwt_bridge.tmp
	mv -f /tmp/xwt_bridge.tmp /tmp/xwt_bridge.log
fi

case "$METHOD" in
	""|dummy_script.sh) exit 0 ;;
esac
printf '%s' "$METHOD" | grep -Eq '^[A-Za-z0-9_.-]+\.sh$' || exit 1
SCRIPT="$KSROOT/scripts/$METHOD"
[ -f "$SCRIPT" ] || exit 1

# The UI sends exactly one param per call, but ss_update.sh and
# ss_rule_update.sh were written for httpdb, which passed the request id as $1
# and the action as $2. Without the id they fall through to "exit 2", so the
# buttons did nothing. Restore that contract here.
if [ "$METHOD" = "ss_update.sh" ] && [ "$PARAMS" = "check" ]; then
	# check_release answers through http_response, which now lands in a file.
	rm -f /tmp/upload/.xwt_response
	/bin/sh "$SCRIPT" "$REQID" check >/dev/null 2>&1
	_upd=$(cat /tmp/upload/.xwt_response 2>/dev/null)
	case "$_upd" in v[0-9]*) ;; *) _upd=unavailable ;; esac
	printf '{"result":"%s","id":"%s"}' "$_upd" "$REQID" > "$KSROOT/res/xwt_tmp/xwt_upd.js.$$" &&
		mv -f "$KSROOT/res/xwt_tmp/xwt_upd.js.$$" "$KSROOT/res/xwt_tmp/xwt_upd.js"
	rm -f /tmp/upload/.xwt_response
	exit 0
fi
case "$METHOD" in
	ss_update.sh|ss_rule_update.sh) PARAMS="$REQID $PARAMS" ;;
esac

# A new apply must not close the progress dialog on the PREVIOUS run's
# terminator: drop the old log the moment the apply is dispatched.
if [ "$METHOD" = "ss_config.sh" ] && [ "$PARAMS" = "start" ]; then
	: > /tmp/upload/ss_log.txt
fi

# ss_log.sh: 1.5-generation httpdb returned the script's stdout as the RPC
# result. 380 has no httpdb and /_temp/ is NOT served by its httpd, so publish
# the log tail into /res (which httpd does serve) for the shim to poll.
if [ "$METHOD" = "ss_log.sh" ]; then
	# While an apply is running the log still holds the PREVIOUS run's
	# XU6J03M6 terminator; reporting it would close the dialog instantly.
	if ps w | grep -q '[s]sconfig.sh'; then
		printf '%s' '{"operation":"","view":"plugin","exists":true,"truncated":false,"text":""}' \
			> "$KSROOT/res/xwt_tmp/xwt_rt.js"
	else
		/bin/sh "$SCRIPT" $PARAMS 2>/dev/null \
			| "$KSROOT/bin/jq" -c '{operation:(.operation // ""),view:(.view // "plugin"),exists:(.exists // true),truncated:(.truncated // false),text:((.text // "") | .[-8192:])}' 2>/dev/null \
			> "$KSROOT/res/xwt_tmp/xwt_rt.js"
	fi
	exit 0
fi

# ss_status.sh: the page polls every second, but one run costs 5-30 s here
# because it probes two latency endpoints, and the poll started a fresh copy
# the moment the previous one finished. Three parallel copies were normal,
# each forking ss_status_curl.sh -- that is what kept the mtdblock3 kernel
# thread at 20-35% of a core. Throttle to one run per 90 s and let the poll
# read the cached payload in between; the panel's own cache age is 120 s.
if [ "$METHOD" = "ss_status.sh" ]; then
	_st_last=0
	[ -r /tmp/xwt_ss_status_last ] && IFS= read -r _st_last < /tmp/xwt_ss_status_last
	case "$_st_last" in ''|*[!0-9]*) _st_last=0 ;; esac
	read -r _st_upf _st_i < /proc/uptime
	_st_now=${_st_upf%%.*}
	if [ $((_st_now - _st_last)) -ge 90 ]; then
		if ! ps w | grep -q '[s]s_status.sh'; then
			printf '%s\n' "$_st_now" > /tmp/xwt_ss_status_last
			PATH="$KSROOT/bin:/bin:/usr/bin:/sbin:/usr/sbin" \
				/bin/sh "$SCRIPT" $PARAMS >/dev/null 2>&1 &
		fi
	fi
	exit 0
fi

# ss_dashboard.sh: xwt_dash_sampler.sh owns the snapshot and republishes it in
# /res/xwt_tmp/xwt_dash.js once a second, so a poll must NOT spawn work of its
# own. It only checks that the daemon is alive and kicking; the whole check is
# shell builtins, so the request path costs no fork at all.
if [ "$METHOD" = "ss_dashboard.sh" ]; then
	_dash_up=""
	[ -r "$KSROOT/res/xwt_tmp/xwt_dash.js" ] && IFS= read -r _dash_up < "$KSROOT/res/xwt_tmp/xwt_dash.js"
	[ -n "$_dash_up" ] || { [ -r /tmp/xwt_dash/latest ] && IFS= read -r _dash_up < /tmp/xwt_dash/latest; }
	case "$_dash_up" in
		*'"uptime":'*)
			_dash_up=${_dash_up#*'"uptime":'}
			_dash_up=${_dash_up%%,*}
			_dash_up=${_dash_up%%.*}
			;;
		*) _dash_up=0 ;;
	esac
	read -r _nowf _idle < /proc/uptime
	_dash_age=$(( ${_nowf%%.*} - _dash_up ))
	if [ "$_dash_age" -lt 0 ] || [ "$_dash_age" -gt 5 ]; then
		if xwt_may_run && ! ps w | grep -q '[x]wt_dash_sampler.sh$'; then
			if [ -x /usr/sbin/start-stop-daemon ]; then
				start-stop-daemon -S -b -q -m -p /var/run/xwt_dash_sampler.pid \
					-x /bin/sh -- "$KSROOT/scripts/xwt_dash_sampler.sh" 2>/dev/null
			else
				/bin/sh "$KSROOT/scripts/xwt_dash_sampler.sh" >/dev/null 2>&1 &
			fi
		fi
	fi
	exit 0
fi

# xwt_backup_upload.sh: 380 httpd has no /_upload route, so the page uploads
# a backup file as base64 chunks and this script reassembles it. Run it in the
# foreground -- the chunk order matters and a background run could interleave
# with the next request.
if [ "$METHOD" = "xwt_backup_upload.sh" ]; then
	/bin/sh "$SCRIPT" $PARAMS >/dev/null 2>&1
	exit 0
fi

# xwt_backup_upload.sh: 380 httpd has no /_upload route, so the page uploads
# a backup file as base64 chunks and this script reassembles it. Run it in the
# foreground -- the chunk order matters and a background run could interleave
# with the next request.
if [ "$METHOD" = "xwt_backup_upload.sh" ]; then
	/bin/sh "$SCRIPT" $PARAMS >/dev/null 2>&1
	exit 0
fi

# PARAMS is a space-joined list. Values produced by variable expansion are NOT
# re-parsed as shell syntax; disable globbing so URL-ish params stay literal.
set -f
PATH="$KSROOT/bin:$KSROOT/scripts:/bin:/usr/bin:/sbin:/usr/sbin" \
	/bin/sh "$SCRIPT" $PARAMS >/dev/null 2>&1 &
exit 0
'''


_SHIM_HEAD = r'''#!/bin/sh
# XWT_ARM380_SHIMS -- every arm380 compatibility shim in one file.
# base.sh sources this through a single loader line, so an upgrade replaces the
# whole set instead of appending yet another block.

# XWT_SHIMS_ALIAS_RESET: this ash DOES expand aliases inside scripts, and
# ss_base.sh carries `alias echo_date=...`. base.sh gets sourced more than once
# per run (ss_conf.sh -> ss_base.sh -> base.sh -> shims, then
# ss_node_common.sh -> base.sh -> shims again), so on the SECOND load the line
# `echo_date() {` was rewritten by the alias into `echo 【..】: () {` and ash
# aborted the whole script with 'syntax error: unexpected "("'. That silently
# killed every ss_conf.sh action (export / clear / package). Dropping the
# aliases first makes this file safe to source any number of times.
unalias echo_date urldecode http_response command sha256sum 2>/dev/null

# 1. ash on koolshare 380 has no `command` builtin (upstream calls it 41x).
command() {
	if [ "$1" = "-v" ]; then
		shift
		which "$@" 2>/dev/null
	else
		which "$1" >/dev/null 2>&1 || return 127
		"$@"
	fi
}

# 2. no sha256sum on this firmware; openssl always is present.
sha256sum() {
	_rc=0
	for _f in "$@"; do
		case "$_f" in -*) continue ;; esac
		_h=$(openssl dgst -sha256 "$_f" 2>/dev/null | awk '{print $NF}')
		if [ -n "$_h" ]; then echo "$_h  $_f"; else _rc=1; fi
	done
	return $_rc
}

# 3. the firmware CA store dates from 2017-09-14; prefer the bundled bundle.
if [ -f /koolshare/res/cacert.pem ]; then
	CURL_CA_BUNDLE=/koolshare/res/cacert.pem
	export CURL_CA_BUNDLE
fi

# 4. httpdb's /_resp/<id> does not exist on 380 -- park the payload in a file.
#    (echo_date / urldecode come from the alias shim appended below.)
'''

# Reuse the alias shim verbatim (its sed/printf escapes are easy to get wrong)
# minus its own marker line, which only matters when it is appended directly.
SHIMS = _SHIM_HEAD + ALIAS_SHIM.split("\n", 1)[1]



BACKUP_UPLOAD = r"""#!/bin/sh
# arm380: 380 httpd has no /_upload endpoint -- httpdb's upload route does not
# exist here, so the page cannot push a backup file into /tmp/upload the way
# upstream expects. The page therefore sends the file as base64 chunks through
# the normal /_api/ path, and every chunk lands in its OWN dbus key
# (ss_backup_c0, ss_backup_c1, ...) so a slow request can never overwrite a
# chunk that has not been picked up yet. This script concatenates the parts in
# order, decodes them and drops the result where ss_conf.sh looks for it.
. /koolshare/scripts/base.sh

MODE="$1"
NAME="$2"

case "${MODE}" in
	commit) ;;
	*) exit 1 ;;
esac
case "${NAME}" in
	ssconf_backup.json|ssconf_backup.sh) ;;
	*) exit 1 ;;
esac

TOTAL=$(dbus get ss_backup_total 2>/dev/null)
case "${TOTAL}" in
	''|*[!0-9]*) exit 1 ;;
esac
[ "${TOTAL}" -gt 0 ] || exit 1

B64=/tmp/xwt_backup.b64
: > "${B64}"
i=0
while [ "${i}" -lt "${TOTAL}" ]; do
	part=$(dbus get "ss_backup_c${i}" 2>/dev/null)
	if [ -z "${part}" ]; then
		rm -f "${B64}"
		exit 1
	fi
	printf '%s' "${part}" >> "${B64}"
	dbus remove "ss_backup_c${i}" >/dev/null 2>&1
	i=$((i + 1))
done
dbus remove ss_backup_total >/dev/null 2>&1

mkdir -p /tmp/upload
if /koolshare/bin/base64 -d "${B64}" > "/tmp/upload/${NAME}" 2>/dev/null; then
	rm -f "${B64}"
	echo "backup uploaded: ${NAME} $(wc -c < "/tmp/upload/${NAME}") bytes"
	exit 0
fi
rm -f "${B64}" "/tmp/upload/${NAME}"
exit 1
"""






NEW_UPLOAD_JS = r"""function upload_ss_backup() {
	db_ss["ss_basic_action"] = "9";
	var input = $('#ss_file')[0];
	var file = (input && input.files) ? input.files[0] : null;
	if (!file) {
		alert('请先选择备份文件。');
		return false;
	}
	var parts = String(file.name || '').split('.');
	var filelast = (parts[parts.length - 1] || '').toLowerCase();
	if (filelast != "sh" && filelast != "json") {
		alert('备份文件格式不正确！');
		return false;
	}
	E('ss_file_info').style.display = "none";
	var target = (filelast == 'sh') ? 'ssconf_backup.sh' : 'ssconf_backup.json';
	/* arm380: this httpd has no /_upload route, so the file is uploaded as
	 * base64 chunks through the normal /_api/ path. Every chunk goes into its
	 * own dbus key, so a slow request can never overwrite another chunk. */
	var reader = new FileReader();
	reader.onload = function (evt) {
		var raw = String(evt.target.result || '');
		var cut = raw.indexOf(',');
		var b64 = (cut >= 0) ? raw.slice(cut + 1) : raw;
		var CHUNK = 3000;
		var total = Math.max(1, Math.ceil(b64.length / CHUNK));
		var idx = 0;
		function sendChunk() {
			if (idx >= total) { return commitUpload(); }
			var field = {};
			field['ss_backup_c' + idx] = b64.substr(idx * CHUNK, CHUNK);
			var id = parseInt(Math.random() * 100000000);
			$.ajax({
				type: 'POST',
				url: '/_api/',
				cache: false,
				data: JSON.stringify({id: id, method: 'dummy_script.sh', params: [], fields: field}),
				dataType: 'json',
				success: function () { idx++; sendChunk(); },
				error: function () { alert('备份文件上传失败，请重试。'); }
			});
		}
		function commitUpload() {
			var id = parseInt(Math.random() * 100000000);
			$.ajax({
				type: 'POST',
				url: '/_api/',
				cache: false,
				data: JSON.stringify({id: id, method: 'xwt_backup_upload.sh',
					params: ['commit', target], fields: {ss_backup_total: String(total)}}),
				dataType: 'json',
				success: function (response) {
					if (response && String(response.result) == String(id)) {
						E('ss_file_info').style.display = "block";
						restore_ss_conf();
					} else {
						alert('备份文件上传失败，请重试。');
					}
				},
				error: function () { alert('备份文件上传失败，请重试。'); }
			});
		}
		sendChunk();
	};
	reader.readAsDataURL(file);
	return false;
}"""


NEW_UPLOAD_JS = r"""function upload_ss_backup() {
	db_ss["ss_basic_action"] = "9";
	var input = $('#ss_file')[0];
	var file = (input && input.files) ? input.files[0] : null;
	if (!file) {
		alert('请先选择备份文件。');
		return false;
	}
	var parts = String(file.name || '').split('.');
	var filelast = (parts[parts.length - 1] || '').toLowerCase();
	if (filelast != "sh" && filelast != "json") {
		alert('备份文件格式不正确！');
		return false;
	}
	E('ss_file_info').style.display = "none";
	var target = (filelast == 'sh') ? 'ssconf_backup.sh' : 'ssconf_backup.json';
	/* arm380: this httpd has no /_upload route, so the file is uploaded as
	 * base64 chunks through the normal /_api/ path. Every chunk goes into its
	 * own dbus key, so a slow request can never overwrite another chunk. */
	var reader = new FileReader();
	reader.onload = function (evt) {
		var raw = String(evt.target.result || '');
		var cut = raw.indexOf(',');
		var b64 = (cut >= 0) ? raw.slice(cut + 1) : raw;
		var CHUNK = 3000;
		var total = Math.max(1, Math.ceil(b64.length / CHUNK));
		var idx = 0;
		function sendChunk() {
			if (idx >= total) { return commitUpload(); }
			var field = {};
			field['ss_backup_c' + idx] = b64.substr(idx * CHUNK, CHUNK);
			var id = parseInt(Math.random() * 100000000);
			$.ajax({
				type: 'POST',
				url: '/_api/',
				cache: false,
				data: JSON.stringify({id: id, method: 'dummy_script.sh', params: [], fields: field}),
				dataType: 'json',
				success: function () { idx++; sendChunk(); },
				error: function () { alert('备份文件上传失败，请重试。'); }
			});
		}
		function commitUpload() {
			var id = parseInt(Math.random() * 100000000);
			$.ajax({
				type: 'POST',
				url: '/_api/',
				cache: false,
				data: JSON.stringify({id: id, method: 'xwt_backup_upload.sh',
					params: ['commit', target], fields: {ss_backup_total: String(total)}}),
				dataType: 'json',
				success: function (response) {
					if (response && String(response.result) == String(id)) {
						E('ss_file_info').style.display = "block";
						restore_ss_conf();
					} else {
						alert('备份文件上传失败，请重试。');
					}
				},
				error: function () { alert('备份文件上传失败，请重试。'); }
			});
		}
		sendChunk();
	};
	reader.readAsDataURL(file);
	return false;
}"""


def main():
    if not SRC_UP.is_dir():
        raise SystemExit("upstream source missing: %s" % SRC_UP)
    if SRC.exists():
        shutil.rmtree(SRC)
    SRC.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(SRC_UP, SRC)

    # --- CA bundle ---------------------------------------------------------
    # /etc/ssl/certs/ca-certificates.crt on 380.70 is dated 2017-09-14 and no
    # longer validates raw.githubusercontent.com or api.github.com. base.sh
    # exports CURL_CA_BUNDLE pointing here once the file exists.
    ca_src = ROOT / "tools" / "assets" / "cacert.pem"
    if not ca_src.is_file():
        raise SystemExit("missing CA bundle: %s" % ca_src)
    shutil.copyfile(ca_src, SRC / "res" / "cacert.pem")

    # --- install.sh ---
    p = SRC / "install.sh"
    write_text_lf(p, patch_install_sh(p.read_text("utf-8")))

    # --- self-update: point at this port's releases ---------------------
    # Upstream looks for xray_web_tool_arm_lite_ac3100_<v>.tar.gz in the 386
    # repository, so on 380 the asset lookup could never match.
    p = SRC / "scripts" / "ss_update.sh"
    t = p.read_text("utf-8")
    t = replace_once(t, "REPO=https://api.github.com/repos/angelineage/xray-web-tool",
                     "REPO=https://api.github.com/repos/angelineage/R6300v2-xray",
                     "update api repo")
    t = replace_once(t, 'NAME="xray_web_tool_arm_lite_ac3100_${NEW}.tar.gz"',
                     'NAME="shadowsocks_arm380_r6300v2_${NEW}.tar.gz"',
                     "update asset name")
    if t.count("https://github.com/angelineage/xray-web-tool/releases/download/*") != 2:
        raise SystemExit("PATCH FAILED [update url prefix]: expected 2 matches")
    t = t.replace("https://github.com/angelineage/xray-web-tool/releases/download/*",
                  "https://github.com/angelineage/R6300v2-xray/releases/download/*")
    write_text_lf(p, t)

    # --- new frontend shim first: its content hash versions the <script> tag,
    #     otherwise browsers keep serving a cached copy across builds ---
    write_text_lf(SRC / "res" / "xwt-arm380-api.js", API_SHIM)
    shim_ver = hashlib.sha256(API_SHIM.encode("utf-8")).hexdigest()[:8]

    # --- Module_shadowsocks.asp (keep the original UTF-8 BOM) ---
    p = SRC / "webs" / "Module_shadowsocks.asp"
    write_text_lf(p, patch_asp(p.read_text("utf-8-sig"), shim_ver), bom=True)

    css380 = TMP / "_softcenter_380.css"
    css = css380.read_text("utf-8") if css380.exists() else "/* arm380 software center stylesheet */\n"
    write_text_lf(SRC / "res" / "softcenter.css", css)

    # --- new backend bridge + ash alias shim ---
    write_text_lf(SRC / "scripts" / "fss_api_bridge.sh", BRIDGE)
    write_text_lf(SRC / "scripts" / "xwt_arm380_alias_shim.sh", ALIAS_SHIM)
    write_text_lf(SRC / "scripts" / "xwt_arm380_shims.sh", SHIMS)
    write_text_lf(SRC / "scripts" / "xwt_tmp_mirror.sh", TMP_MIRROR)
    write_text_lf(SRC / "scripts" / "xwt_dash_sampler.sh", SAMPLER)
    write_text_lf(SRC / "scripts" / "ss_dashboard.sh", DASH_SHIM)
    write_text_lf(SRC / "scripts" / "xwt_backup_upload.sh", BACKUP_UPLOAD)
    write_text_lf(SRC / "scripts" / "xwt_run_guard.sh", RUN_GUARD)

    # --- ss_conf.sh: export / clear / package -----------------------------
    # Two 380 facts break it: (a) the shims file is sourced twice and the
    # echo_date alias aborts the run, (b) httpd refuses *.json/*.sh/*.tar.gz,
    # so each export needs a *.js twin to be downloadable.
    p = SRC / "scripts" / "ss_conf.sh"
    write_text_lf(p, patch_ss_conf(p.read_text("utf-8")))

    # --- ssconfig.sh: router-only + master-switch guard ---
    p = SRC / "ss" / "ssconfig.sh"
    write_text_lf(p, patch_ssconfig(p.read_text("utf-8")))

    # --- model strings: online update must NOT pull the upstream 386 package
    #     onto this port (its target.model is RT-AC3100, so requiring R6300V2
    #     makes ss_update.sh reject it safely).
    p = SRC / "scripts" / "ss_update.sh"
    t = p.read_text("utf-8")
    if '.target.model=="RT-AC3100"' in t:
        write_text_lf(p, t.replace('.target.model=="RT-AC3100"', '.target.model=="R6300V2"'))
    p = SRC / "scripts" / "ss_proc_status.sh"
    t = p.read_text("utf-8")
    if "RT-AC3100 arm_lite" in t:
        write_text_lf(p, t.replace("RT-AC3100 arm_lite", "R6300V2 arm380"))
    # --- dashboard sampler tuning (see patch_dashboard_js) ---
    p = SRC / "res" / "xwt-dashboard.js"
    write_text_lf(p, patch_dashboard_js(p.read_text("utf-8")))
    # the ASP pins ?v=dashboard-...: refresh it from the content hash, otherwise
    # browsers keep running the cached (broken) sampler.
    dash_ver = hashlib.sha256(p.read_bytes()).hexdigest()[:8]
    # --- latency probe cadence -----------------------------------------
    # The panel fetches six external sites through the proxy. At 5 s that is
    # 1.2 TLS handshakes per second for a number nobody watches that closely;
    # 15 s keeps it useful at a third of the CPU.
    p = SRC / "res" / "xwt-latency.js"
    t = p.read_text("utf-8")
    if "setTimeout(cycle,5000)" in t:
        write_text_lf(p, t.replace("setTimeout(cycle,5000)", "setTimeout(cycle,15000)"))
    lat_ver = hashlib.sha256(p.read_bytes()).hexdigest()[:8]
    _asp = SRC / "webs" / "Module_shadowsocks.asp"
    t2 = _asp.read_text("utf-8-sig").replace("xwt-latency.js?v=8",
                                            "xwt-latency.js?v=" + lat_ver)
    write_text_lf(_asp, t2, bom=True)
    asp = SRC / "webs" / "Module_shadowsocks.asp"
    t = asp.read_text("utf-8-sig")
    t2 = t.replace("?v=dashboard-20261113-3", "?v=dash-" + dash_ver)
    if t2 != t:
        write_text_lf(asp, t2, bom=True)

    # --- res/ss-menu.js model string ---
    p = SRC / "res" / "ss-menu.js"
    t = p.read_text("utf-8")
    if "ASUS RT-AC3100 arm_lite" in t:
        write_text_lf(p, t.replace("ASUS RT-AC3100 arm_lite", "Netgear R6300V2 arm380"))

    # --- maintenance metadata ---
    maint = json.loads((UPSTREAM / "maintenance.json").read_text("utf-8"))
    maint["target"] = {"model": "R6300V2", "arch": "arm", "edition": "lite",
                       "kernel": "2.6.36", "software_center": "koolshare-1.0"}
    maint["port"] = {"from": "RT-AC3100 / Merlin 386 arm_lite",
                     "to": "R6300V2 / Merlin-Koolshare 380.70_X7.9.1",
                     "api_shim": "res/xwt-arm380-api.js",
                     "status": "experimental - UI data layer needs on-device tuning"}
    # The updater reads this back to decide "already current" vs "upgrade".
    maint["version"] = VERSION
    write_text_lf(SRC / "ss" / "maintenance.json",
                  json.dumps(maint, ensure_ascii=False, indent=2) + "\n")
    write_text_lf(SRC / "ss" / "compat_revision", "r6300v2-arm380-" + VERSION + "\n")

    # --- build files dict ---
    files = {"shadowsocks/" + f.relative_to(SRC).as_posix(): f.read_bytes()
             for f in SRC.rglob("*") if f.is_file()}
    files["shadowsocks/res/ac3100-version.js"] = (
        "window.FANCYSS_AC3100=" + json.dumps(maint, ensure_ascii=False) + ";\n").encode("utf-8")
    files["shadowsocks/LICENSE"] = (UPSTREAM / "LICENSE").read_bytes()
    files["shadowsocks/SOURCE.txt"] = (
        "Upstream: https://github.com/angelineage/xray-web-tool\n"
        "Port: R6300V2 arm380 (Koolshare 1.0 software center)\n"
        "Version: %s\n" % VERSION).encode("utf-8")

    modes = json.loads((UPSTREAM / "packaging" / "file-modes.json").read_text("utf-8"))
    lock = json.loads((UPSTREAM / "packaging" / "vendor-lock.json").read_text("utf-8"))
    for name, expected in lock.items():
        rel = name.split("/", 1)[1] if name.startswith("shadowsocks/") else name
        got = hashlib.sha256((SRC / rel).read_bytes()).hexdigest()
        if got != expected:
            raise SystemExit("native binary changed: %s" % name)

    # defaults snapshot, same idea as upstream build.py
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as t:
        for name, content in sorted(files.items()):
            if name.startswith("shadowsocks/configs/") or name in (
                    "shadowsocks/LICENSE", "shadowsocks/SOURCE.txt"):
                info = tarfile.TarInfo(name)
                info.size = len(content)
                info.mode = 0o644
                t.addfile(info, io.BytesIO(content))
    files["shadowsocks/res/ac3100-package-defaults.tar"] = buf.getvalue()
    files["shadowsocks/res/ac3100-package-files.txt"] = (
        "\n".join(sorted(n[len("shadowsocks/"):] for n in files)) + "\n").encode("utf-8")

    DIST.mkdir(parents=True, exist_ok=True)
    package = DIST / PACKAGE_NAME
    with package.open("wb") as raw, gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as ar:
            dirs = sorted({str(pathlib.PurePosixPath(n).parent) for n in files} | {"shadowsocks"})
            for d in dirs:
                if d == ".":
                    continue
                i = tarfile.TarInfo(d)
                i.type = tarfile.DIRTYPE
                i.mode = 0o755
                ar.addfile(i)
            for name, data in sorted(files.items()):
                i = tarfile.TarInfo(name)
                i.size = len(data)
                i.mode = modes.get(name, 0o755 if name.endswith(".sh") else 0o644)
                ar.addfile(i, io.BytesIO(data))

    with tarfile.open(package) as ar:
        for name, data in files.items():
            if ar.extractfile(name).read() != data:
                raise SystemExit("archive verify failed: " + name)
    sha = hashlib.sha256(package.read_bytes()).hexdigest()
    write_text_lf(DIST / "SHA256SUMS.txt", sha + "  " + PACKAGE_NAME + "\n")
    write_text_lf(DIST / "build-manifest.json", json.dumps({
        "version": VERSION, "target": maint["target"], "sha256": sha,
        "files": {n: hashlib.sha256(d).hexdigest() for n, d in sorted(files.items())}
    }, ensure_ascii=False, indent=2) + "\n")
    print("PACKAGE " + str(package))
    print("SHA256  " + sha)
    print("SIZE    %d bytes, %d files" % (package.stat().st_size, len(files)))


if __name__ == "__main__":
    main()
