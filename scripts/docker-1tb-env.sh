#!/bin/bash
# Source before docker:  source ".../scripts/docker-1tb-env.sh"
SOURCE="/Volumes/1tb drive"
LINK="$HOME/1tb-drive"
[[ -L "$LINK" ]] || ln -sf "$SOURCE" "$LINK"

export COLIMA_HOME="$SOURCE/homelab-runtime/colima"
export DOCKER_CONFIG="$SOURCE/homelab-runtime/docker-config"
mkdir -p "$COLIMA_HOME" "$DOCKER_CONFIG"

if [[ ! -f "$DOCKER_CONFIG/config.json" ]]; then
  cat > "$DOCKER_CONFIG/config.json" << 'JSON'
{
  "cliPluginsExtraDirs": ["/opt/homebrew/lib/docker/cli-plugins"]
}
JSON
fi

unset DOCKER_HOST
docker context use colima 2>/dev/null || true
