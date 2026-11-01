#!/bin/bash
# Run a Dispatcharr management command with the environment the Dispatcharr service itself
# has: its .env file and the Environment= lines of its systemd unit (the database password
# lives there). Without them, manage.py cannot reach the database.
#
#   bash /root/dispatcharr-shell.sh shell < /root/some-script.py
set -euo pipefail
cd /opt/dispatcharr
set -a
[ -f .env ] && . ./.env
set +a
while IFS= read -r line; do
  [ -n "$line" ] && export "$line"
done < <(systemctl show dispatcharr -p Environment --value | tr ' ' '\n')
# The Proxmox community script makes .venv, Dispatcharr's own debian_install.sh makes env
PY=.venv/bin/python
[ -x "$PY" ] || PY=env/bin/python
exec "$PY" manage.py "$@"
