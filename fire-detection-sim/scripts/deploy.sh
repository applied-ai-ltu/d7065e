#!/usr/bin/env bash
# Build both images on ProArt, ship the edge image to each edge node's containerd
# (arm64 for the Pi, amd64 for infinity-magic), import the server image locally, apply.
#   scripts/deploy.sh            # everything
#   scripts/deploy.sh edge       # edge image + edge deployments only
#   scripts/deploy.sh server     # server image + server deployment only
set -euo pipefail
cd "$(dirname "$0")/.."

NS=ai-edge-intel-course
DOCKER=${DOCKER:-"sudo docker"}
PI=${PI:-pi400-master-01}
STANDBY=${STANDBY:-infinity-magic}
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
  kubectl -n $NS get secret fds-vault >/dev/null 2>&1 || \
    kubectl -n $NS create secret generic fds-vault --from-literal=token="$(tr -d '\n' < "$HOME/.vault-token")"
  kubectl apply -f k8s/server.yaml
  kubectl -n $NS rollout restart deploy fds-server
fi

kubectl -n $NS get pods -o wide
