#!/usr/bin/env bash
# Build both images on the server node, ship the edge image to each edge node's containerd
# (arm64 for the Pi, amd64 for the standby node), import the server image locally, apply.
# Nodes are found by their fds-role label (edge-active | edge-standby | server); override
# with PI=<host> STANDBY=<host> if ssh needs a different name.
#   scripts/deploy.sh            # everything
#   scripts/deploy.sh edge       # edge image + edge deployments only
#   scripts/deploy.sh server     # server image + server deployment only
#   scripts/deploy.sh check      # pre-flight: images on nodes, secret, pods (no changes)
set -euo pipefail
cd "$(dirname "$0")/.."

NS=ai-edge-intel-course
DOCKER=${DOCKER:-"sudo docker"}
node_by_role() { kubectl get nodes -l "fds-role=$1" -o jsonpath='{.items[0].metadata.name}'; }
PI=${PI:-$(node_by_role edge-active)}
STANDBY=${STANDBY:-$(node_by_role edge-standby)}
[ -n "$PI" ] && [ -n "$STANDBY" ] || { echo 'label the nodes first: kubectl label node <name> fds-role=edge-active|edge-standby|server'; exit 1; }
WHAT=${1:-all}

ensure_builder() {
  if ! $DOCKER buildx inspect fds >/dev/null 2>&1; then
    $DOCKER buildx create --name fds --driver docker-container --bootstrap >/dev/null
  fi
  $DOCKER buildx use fds
}

ship_edge() {  # $1 = platform, $2 = host
  local plat=$1 host=$2 tar
  tar="/tmp/fds-edge-${plat//\//-}.tar"
  echo "== edge image for $plat -> $host"
  $DOCKER buildx build --platform "linux/$plat" -f Dockerfile.edge -t fds-edge:latest \
    -o "type=docker,dest=$tar" .
  scp -q "$tar" "$host:/tmp/fds-edge.tar"
  ssh "$host" 'sudo k3s ctr images import /tmp/fds-edge.tar && rm /tmp/fds-edge.tar'
}

require_app_secret() {  # fds-server reads LITELLM_API_KEY / DB_PASSWORD from Secret fds-app
  kubectl -n $NS get secret fds-app >/dev/null 2>&1 && return
  echo "Secret fds-app is missing. Create it from Vault secret/homelab/d7065e, then re-run:"
  echo "  kubectl -n $NS create secret generic fds-app \\"
  echo "    --from-literal=LITELLM_API_KEY=\"\$(vault kv get -field=litellm_api_key secret/homelab/d7065e)\" \\"
  echo "    --from-literal=DB_PASSWORD=\"\$(vault kv get -field=db_password secret/homelab/d7065e)\""
  exit 1
}

if [[ $WHAT == check ]]; then
  ok=0
  for h in "$PI" "$STANDBY"; do
    if ssh -o ConnectTimeout=5 "$h" 'sudo k3s ctr images ls -q' 2>/dev/null | grep -q '^docker.io/library/fds-edge:latest$'; then
      echo "ok       fds-edge image on $h"
    else
      echo "MISSING  fds-edge image on $h  -> scripts/deploy.sh edge"; ok=1
    fi
  done
  if sudo k3s ctr images ls -q | grep -q '^docker.io/library/fds-server:latest$'; then
    echo "ok       fds-server image on $(hostname)"
  else
    echo "MISSING  fds-server image on $(hostname)  -> scripts/deploy.sh server"; ok=1
  fi
  if kubectl -n $NS get secret fds-app >/dev/null 2>&1; then
    echo "ok       secret fds-app"
  else
    echo "MISSING  secret fds-app (see require_app_secret in this script)"; ok=1
  fi
  kubectl -n $NS get secret fds-vault >/dev/null 2>&1 && { echo "WARNING  legacy secret fds-vault still exists; delete it"; ok=1; }
  echo "-- pods"; kubectl -n $NS get pods -o wide
  exit $ok
fi

if [[ $WHAT == all || $WHAT == edge ]]; then
  test -f edge/detector.onnx || { echo "edge/detector.onnx missing — run python -m fds.models.retrain"; exit 1; }
  ensure_builder
  ship_edge arm64 "$PI"
  ship_edge amd64 "$STANDBY"
  python3 k8s/render_edge.py
  kubectl apply -f k8s/edge.yaml
  kubectl -n $NS rollout restart deploy -l app=fds-edge
fi

if [[ $WHAT == all || $WHAT == server ]]; then
  echo "== server image (amd64, local)"
  $DOCKER build -f Dockerfile.server -t fds-server:latest .
  $DOCKER save fds-server:latest | sudo k3s ctr images import -
  require_app_secret
  kubectl apply -f k8s/server.yaml
  kubectl -n $NS rollout restart deploy fds-server
fi

kubectl -n $NS get pods -o wide
