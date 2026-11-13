#!/bin/sh
# rollback.sh -- restore the pre-port fancyss 4.38.3 after a failed experiment.
#
# USAGE (SSH on the router):
#   sh /tmp/rollback.sh /jffs/fancyss380-backup-YYYYMMDD-HHMMSS.tar.gz
#
# This stops the ported plugin first, restores every /koolshare subtree from
# the backup, re-imports the dbus exports and restarts the original service.
# It deliberately does NOT delete the ported Module_shadowsocks.asp until the
# very end, and it never touches nvram or the firmware.

set -e
BK="$1"
[ -n "$BK" ] || { echo "usage: sh rollback.sh <backup.tar.gz>"; exit 1; }
[ -f "$BK" ] || { echo "backup not found: $BK"; exit 1; }

echo "== stopping ported plugin / websocketd =="
sh /koolshare/ss/ssconfig.sh stop >/dev/null 2>&1 || true
killall websocketd >/dev/null 2>&1 || true
killall xray >/dev/null 2>&1 || true
killall smartdns >/dev/null 2>&1 || true
killall chinadns-ng >/dev/null 2>&1 || true

STAGE="/tmp/xwt380-rollback-$$"
rm -rf "$STAGE"; mkdir -p "$STAGE"
tar -xzf "$BK" -C "$STAGE"
SRC=$(find "$STAGE" -maxdepth 1 -type d -name 'fancyss380-backup-*' | head -1)
[ -n "$SRC" ] || { echo "bad backup layout"; exit 1; }

echo "== restoring files =="
for d in ss scripts webs res bin configs; do
    if [ -d "$SRC/$d" ]; then
        rm -rf "/koolshare/$d"
        cp -a "$SRC/$d" "/koolshare/"
    fi
done

echo "== restoring dbus =="
for f in "$SRC"/dbus_*.export; do
    [ -f "$f" ] || continue
    # exports look like: export ss_basic_enable='1';
    # shellcheck disable=SC1090
    . "$f" >/dev/null 2>&1 || true
    awk -F"'" '/^export /{k=$1; sub(/^export /,"",k); sub(/=.$/,"",k);
                printf "dbus set %s=%s\n", k, $2}' "$f" | sh >/dev/null 2>&1 || true
done

echo "== clearing software-center registration of the ported module =="
dbus remove softcenter_module_shadowsocks_install   >/dev/null 2>&1 || true
dbus remove softcenter_module_shadowsocks_version   >/dev/null 2>&1 || true
dbus remove softcenter_module_shadowsocks_title     >/dev/null 2>&1 || true
dbus remove softcenter_module_shadowsocks_description >/dev/null 2>&1 || true
rm -f /koolshare/webs/Module_shadowsocks.asp
rm -f /koolshare/res/xwt-arm380-api.js
rm -f /koolshare/scripts/fss_api_bridge.sh
rm -f /koolshare/bin/curl-fancyss

rm -rf "$STAGE"
echo "== restarting original fancyss =="
if [ -f /koolshare/ss/ssconfig.sh ]; then
    sh /koolshare/ss/ssconfig.sh restart >/dev/null 2>&1 || true
fi
echo "------------------------------------------------------------"
echo "ROLLBACK DONE. Verify with: cat /koolshare/ss/version ; dbus get ss_basic_enable"
echo "If the web UI still caches the old page, reboot the router once."
echo "------------------------------------------------------------"
