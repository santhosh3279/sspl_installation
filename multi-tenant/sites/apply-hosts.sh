#!/bin/bash
# Re-generate the compose file so every container resolves every tenant
# hostname, and apply it. new-site.sh and drop-site.sh run this themselves;
# run it by hand only if one of them warned that it failed.
#
# Restarts backend, workers and scheduler for a few seconds (all tenants).
set -euo pipefail
source "$(cd "$(dirname "$0")/.." && pwd)/lib/common.sh"
take_lock
apply_tenant_hosts
ok "Tenant hostnames applied: $(list_sites | grep -vE '^[0-9.]+$' | tr '\n' ' ')"
