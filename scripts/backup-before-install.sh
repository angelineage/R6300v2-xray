#!/bin/sh
# backup-before-install.sh -- run ON THE R6300V2 before installing the arm380 port.
#
# WHY: the upstream install.sh deletes /koolshare/ss/*, /koolshare/scripts/ss_*,
#      most of /koolshare/bin/* and /koolshare/res/*, i.e. it WILL wipe the
#      currently running fancyss 4.38.3 (nodes, subscriptions, ACL, rules).
#
# USAGE (from an SSH session on the router):
#   sh /tmp/backup-before-install.sh
# It prints the resulting archive path; copy that path before doing anything else.
#
# The archive is written to /jffs (survives reboot) and /tmp is used only as a
# staging area. Nothing in /koolshare is modified by this script.

set -e
TS=$(date +%Y%m%d-%H%M%S)
NAME="fancyss380-backup-$TS"
STAGE="/tmp/$NAME"
OUT="/jffs/$NAME.tar.gz"

echo "creating $STAGE ..."
rm -rf "$STAGE"
mkdir -p "$STAGE"
for d in ss scripts webs res bin configs; do
    if [ -d "/koolshare/$d" ]; then
        cp -a "/koolshare/$d" "$STAGE/"
    fi
done

# dbus holds the live configuration (ss_*, softcenter_module_*).
dbus export ss          > "$STAGE/dbus_ss.export"          2>/dev/null || true
dbus export ssconf      > "$STAGE/dbus_ssconf.export"      2>/dev/null || true
dbus export softcenter  > "$STAGE/dbus_softcenter.export"  2>/dev/null || true
cru l                   > "$STAGE/crontab.txt"             2>/dev/null || true
nvram get productid     > "$STAGE/productid.txt"           2>/dev/null || true

tar -czf "$OUT" -C /tmp "$NAME"
rm -rf "$STAGE"
echo "------------------------------------------------------------"
echo "BACKUP OK : $OUT"
echo "SIZE      : $(wc -c < "$OUT") bytes"
echo "RESTORE   : sh /tmp/rollback.sh $OUT"
echo "------------------------------------------------------------"
echo "Also record the current proxy state before installing:"
echo "  dbus get ss_basic_enable ; dbus get ss_basic_server"
