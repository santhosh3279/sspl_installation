#!/bin/bash

# rclone installer — the binary only.
#
# This is step 1 of 3 for cloud backups:
#   1. install the rclone binary            <- this script (panel button)
#   2. connect a cloud account              <- 'sudo rclone config' over SSH
#   3. point the backup script at the remote <- panel button
#
# Step 2 is deliberately not done here. It is an OAuth flow that needs a
# browser on another machine, and on the way through it prints the account's
# long-lived refresh token to the terminal — which, run as a panel job, would
# land in the job log and in every panel user's browser. Do it over SSH.
# See Backup/Rclone_Configuration_Guide.docx (downloadable from the panel).
#
# Run it as root: the backup job runs as root, and rclone reads the config of
# whoever runs it. A remote configured as another user is invisible to the
# backup, which then silently uploads nothing.

set -e

if command -v rclone >/dev/null 2>&1; then
    echo "rclone is already installed: $(rclone version 2>/dev/null | head -1)"
    echo "Nothing to do."
    exit 0
fi

# The official installer, as the guide recommends, rather than the distro
# package: apt can carry an rclone old enough to still use Google's
# out-of-band auth flow, which Google switched off — 'rclone config' then
# cannot complete for Google Drive at all.
echo "Installing rclone from the official installer (https://rclone.org/install.sh)..."
curl -fsSL https://rclone.org/install.sh | bash

if ! command -v rclone >/dev/null 2>&1; then
    echo "ERROR: the installer finished but rclone is not on PATH." >&2
    exit 1
fi

echo ""
echo "Installed: $(rclone version 2>/dev/null | head -1)"
echo ""
echo "Next: connect a cloud account over SSH, as root:"
echo "    sudo rclone config"
echo "then return to the panel to point backups at the remote."
