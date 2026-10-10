# D7065E — Fire Detection & Autonomous Response at the Edge

**Course**: D7065E Embedded Intelligence at the Edge, Luleå University of Technology
**Student**: Binay Mishra (Group 3)
**Repository**: [applied-ai-ltu/d7065e](https://github.com/applied-ai-ltu/d7065e) — code in `fire-detection-sim/`

A two-tier fire detection and response system for LTU's A-building, running against the
BuildSim building simulator (950 rooms, 3 floors). Tier 1 is an edge controller per floor
on a Raspberry Pi that detects fires from smoke, CO and temperature, releases its own
floor's fire doors within one 5 s tick, and keeps working when the server, the broker or the
LLM is down. Tier 2 is a server agent that asks an LLM for a verdict on every edge alert,
coordinates the other floors, computes evacuation routes, drives the 3D viewer and stands
the building down when readings are clear.

Measured on the project cluster (acceptance run, 3–4 Oct 2026):

| Metric | Value |
|---|---|
| Ignition → own-floor doors released (edge, Pi 400) | 14.7 s (smoke build-up + 5 s tick) |
| Isolation-forest inference, Pi 400 / amd64 | p50 17.8 / 2.3 ms, p95 33.0 / 6.4 ms |
| Edge alert → LLM verdict (Qwen2.5-14B, SGLang) | p50 2.1 s, p95 3.2 s |
| Verdict → cross-floor door release | 25 ms |
| Standby promotion after active controller loss | 30 s |
| Server takeover of a floor with no controller | 30 s (90 s after server boot) |
| Fire out → ALL CLEAR (CO decay + 60 s clear window) | ~7 min for a 45 s fire |

## Architecture

```
                 BuildSim (3D building, doors, sprinklers, viewer)  :30909
                        ▲ REST                    ▲ REST (doors, effects, cards)
                        │                         │
   ┌────────────────────┴─────────┐   ┌───────────┴─────────────────────────────┐
   │  Tier 1 — edge, one per floor │   │  Tier 2 — server (fds-server, proart)   │
   │  active: pi400-master-01      │   │  physics engine + sensor gateway        │
   │  standby: infinity-magic      │   │  alert queue → LLM verdict (LiteLLM →   │
   │  threshold rule + IF (ONNX)   │◄──┤  SGLang Qwen2.5-14B) → coordinate       │
   │  own-floor doors, alerts,     │MQ │  cross-floor doors, routes, viz,        │
   │  heartbeats, failover         │TT │  stand-down, takeover; pipeline → DB    │
   └───────────────────────────────┘   └──────────┬───────────────┬─────────────┘
                                                  │               │
                                        TimescaleDB :30543    Grafana :30300
```

**Tier 1 decision rule** (`edge/edge_controller.py`, every 5 s per room):

| Readings | Decision | Action |
|---|---|---|
| smoke ≥ 10 and CO ≥ 20 | `fire` | release all doors on this floor, alert server |
| CO sensor stale (frozen ≥ 60 s) and smoke ≥ 20 | `fire` | same: a known-faulty sensor cannot veto |
| smoke ≥ 5 and the isolation forest flags the window | `possible` | alert server (Tier 2 decides) |
| smoke ≥ 10, model quiet | `possible` | safety net |
| drops back below threshold | `clear` | tells the server the room is clear |

The deterministic rule is never vetoed by the model; the model only gates the ambiguous band.

**Tier 2 policy** (`src/fds/agent/agent.py`): the LLM returns `{is_fire, confidence}` plus
alert text; the response plan is derived deterministically. Confidence ≥ 0.7 confirms:
cross-floor release, evacuation routes, viewer effects. Otherwise the room goes on a 60 s
watch and readings decide: clear (smoke < 2, CO < 5 on live sensors) → stand-down; fire
level → escalate; else keep watching. ALL CLEAR re-locks every floor. With the LLM
unreachable the same multi-sensor rule is applied as a fallback.

**Resilience** (all exercised live, see *Acceptance* below):

| Failure | Behaviour |
|---|---|
| Active edge controller dies | standby on another node promotes after 30 s of missed heartbeats, demotes when the active returns; doors are never re-locked by a handover |
| Both controllers of a floor down | server takes over the floor after 30 s: subscribes to its readings, actuates its doors directly; hands back when a heartbeat returns |
| LLM unreachable | rule-based fallback confirms/rejects immediately; cards say "(fallback mode)" |
| MQTT broker restart | paho reconnects; edge keeps ticking meanwhile, retries the initial connect with backoff |
| Stuck sensor | value frozen ≥ 60 s is reported as stale; stand-down ignores it, detection falls back to strong smoke alone |
| Server restart | edges keep their floors; stand-down messages carry `floor_clear`/`relock` so a restarted controller still re-locks |

## Repository layout

```
fire-detection-sim/
  edge/edge_controller.py     Tier 1 controller (one process per floor and role)
  edge/detector.onnx          isolation forest for the edge (exported by retrain)
  src/fds/agent/              Tier 2 agent, prompts, LLM client
  src/fds/physics/            engine (heat/smoke/CO spread), sensor gateway with faults
  src/fds/pipeline/           MQTT → TimescaleDB consumer, schema
  src/fds/models/             detector, retrain (ClearML), spread, evacuation
  src/fds/api/control.py      control API (fire start/stop, false alarm, viz clear)
  src/fds/run_full.py         server entry point
  k8s/                        server.yaml, render_edge.py → edge.yaml (6 deployments)
  scripts/deploy.sh           build, ship and apply; `check` = pre-flight
  scripts/door_check.py       door states per floor straight from BuildSim
  scripts/db_check.py         row counts per table for a window
  scripts/grafana_provision.py  pushes grafana/*.json (built by grafana/build_dashboards.py)
  tests/                      79 offline tests (MQTT, BuildSim, ONNX, LLM, DB all faked)
  docs/figures/               screenshots used in the report
```

## Infrastructure

| Service | Where | Port |
|---|---|---|
| BuildSim | ns `ai-edge-intel-course`, proart | 30909 |
| fds-server (control API) | ns `ai-edge-intel-course`, proart | 30089 |
| Edge controllers | pi400-master-01 (active), infinity-magic (standby) | — |
| Mosquitto | ns `data-infra` | 31174 |
| TimescaleDB | ns `data-infra` | 30543 |
| LiteLLM → SGLang (Qwen2.5-14B) | ns `ai-inference` | 30400 |
| Grafana | — | 30300 (HTTPS) |
| Phoenix (LLM traces), ClearML (training) | — | 30317 / 30808 |
| Vault (secrets for the tooling on the dev machine) | ns `vault` | 30820 |

Secrets: the server pod reads `LITELLM_API_KEY` and `DB_PASSWORD` from the Kubernetes Secret
`fds-app` (created once from Vault `secret/homelab/d7065e`; not in git). Tooling on the dev
machine reads the same values through `fds.secrets.get_secrets()` (Vault, or the env vars).

## Deploy

```bash
cd fire-detection-sim
python3 -m venv venv && source venv/bin/activate
pip install -e ".[all,dev]"

scripts/deploy.sh check      # images on each node, Secret fds-app, pods — no changes
scripts/deploy.sh            # build both images, ship edge image to the Pi (arm64) and
                             # infinity-magic (amd64), import, apply, restart
scripts/deploy.sh edge       # or only the edge / server half
scripts/deploy.sh server
```

Images are node-local (`imagePullPolicy: Never`, imported with `k3s ctr images import`), so
there is no registry and no GitOps: `deploy.sh` is the deployment path.

**Start order** (after a full stop): BuildSim first, then the server and the six edge
deployments together. The edges retry door discovery and the broker connection, and the
server waits 90 s after its own start before it would take over a silent floor.

```bash
NS=ai-edge-intel-course
kubectl -n $NS scale deploy/buildsim --replicas=1 && kubectl -n $NS rollout status deploy/buildsim
kubectl -n $NS scale deploy --all --replicas=1
curl -s localhost:30089/status                      # {"running":true,...,"rooms":950}
```

Stop everything with `kubectl -n $NS scale deploy --all --replicas=0`; the edge pods end as
`Completed` (clean SIGTERM).

## Running a scenario

```bash
T1  kubectl -n $NS logs -f deploy/fds-server | grep --line-buffered -i "ALERT from\|LLM verdict\|LLM doubts\|CROSS-FLOOR\|STAND-DOWN\|ALL CLEAR\|takeover\|handback"
T2  curl -s -X POST localhost:30089/viz/clear
    curl -s -X POST localhost:30089/fire/start -H 'Content-Type: application/json' -d '{"room":"A109","level":"level0","intensity":1.0}'
    sleep 45
    curl -s -X POST localhost:30089/fire/stop  -H 'Content-Type: application/json' -d '{"room":"A109","level":"level0"}'
    python scripts/door_check.py                # closed_unlocked while the incident is open
    curl -s -X POST localhost:30089/false-alarm/trigger -H 'Content-Type: application/json' -d '{"room":"A209","level":"level1"}'
    python scripts/db_check.py 1h
```

Watch the BuildSim viewer (`:30909`, F0/F1/F2) and the Grafana *Operations* dashboard
side by side. Confirmed rooms get a fire effect and a red card; rooms the LLM doubts get an
orange "under observation" card until they stand down.

## Acceptance (3–4 October 2026)

| # | Scenario | Result |
|---|---|---|
| 1 | Both controllers of level2 down → server takeover, direct door actuation, hand-back | pass |
| 2 | LiteLLM scaled to 0 → rule-based fallback confirms, cross-floor release, cards in fallback mode | pass |
| 3 | Mosquitto restart → server reconnects in 6 s, edges in 13 s, no takeover, fire handled afterwards | pass |
| 4 | Cold start of the namespace → doors discovered, 6 heartbeats, no alerts, no takeover | pass |
| 5 | Smoke-only event → LLM rejects in 1.9 s, watch, stand-down, no door commands | pass |
| 6 | Real fire baseline (timings in the table at the top) | pass |
| 7 | Fire on level0 + concurrent false alarm on level1 → parallel verdicts, level1 never escalates | pass |
| 8 | Active controller deleted mid-incident → promote at 30 s, doors held, demote when it returns | pass |
| 9 | Data (183 M readings, 18 GB), Grafana over 3 h, retrain on the new data reproduces the model | pass |

Findings fixed during the run: smoke spreading across a courtyard (neighbour graph now uses
BuildSim's door edges plus a 30-unit wall radius), a stuck CO sensor at 5.09 holding three
floors open (stale-sensor rule), the LLM fallback doubting the origin room (fallback now
trusts the multi-sensor rule), a dead CO sensor in the fire room blocking detection
(strong-smoke rule and the faulty-sensor list in the prompt), and a start-order takeover
(90 s boot grace).

## Grafana

```bash
python grafana/build_dashboards.py        # grafana/operations.json, grafana/technical.json
python scripts/grafana_provision.py       # pushes them to folder D7065E (credentials from Vault)
```

*Operations*: controllers alive/acting, confirmed rooms, door state per floor, incident
timeline per room (Tier-2 verdicts), readings of the rooms Tier 1 flagged, Tier-1 / Tier-2
event rates, incident log. *Technical*: IF latency per node, alert→verdict, LLM inference
p50/p95, heartbeats, failover events, table sizes.

## Retraining

```bash
python -m fds.models.retrain
```

Builds training windows from the real readings (per-room 5 s windows), adds the windows the
LLM rejected and augmented false-alarm windows, fits the isolation forest (contamination 0.02),
evaluates against the LLM-labelled fires and real normal rooms, compares with the deployed
model, exports ONNX and checks sklearn/ONNX parity. Logged to ClearML. Deploy a new model
with `scripts/deploy.sh edge`.

## Tests and CI

```bash
ruff check . && ruff format --check .
python -m pytest -q --ignore=tests/test_buildsim.py   # 79 passed
```

`tests/test_buildsim.py` needs a live BuildSim. CI (`.github/workflows/ci-python.yaml`) runs
lint and the offline suite.

## Known limitations

- A server restart in the middle of an incident loses the server's incident state; the
  edges keep their floors released until a stand-down arrives. Mitigation designed (edge
  self-clear after a long clear window, server door-state recovery at boot) but not built.
- The 956 spaces the gateway publishes include 6 combined rooms/corridors that BuildSim does
  not list among its 950 rooms; they are monitored like any other room.
- LLM verdicts are not calibrated probabilities; a borderline edge-confirmed room was once
  rejected at conf 0.00 and had to be escalated on readings. A calibrated decision model for
  the verdict (with the LLM kept for text) is the natural next step.

## Design decisions

| Decision | Chosen | Rejected | Rationale |
|---|---|---|---|
| Topology | Two tiers: edge per floor + server | Server-only, edge-only | life-safety actions must not depend on the server, the LLM or the network; cross-floor coordination and explanation benefit from a server |
| Edge decision | Deterministic multi-sensor rule, IF gates the ambiguous band only | Model decides | the rule is auditable and never vetoed by a learned model |
| Edge model | Isolation forest → ONNX Runtime | Autoencoder, TFLite | 18 ms on a Pi 400, sklearn-native export, retrainable from real windows |
| Tier-2 model | Qwen2.5-14B via LiteLLM/SGLang, verdict-only contract | Full-plan generation; cloud API | plan derived deterministically; 5.2 s → 2.1 s per verdict; must work offline |
| Door ownership | Edge owns its floor, server owns cross-floor; `floor_clear`/`relock` on stand-down | Server owns all | survives failover and restarts without state transfer |
| Spread model | BuildSim door graph + 30-unit wall radius | 50-unit radius | a plain radius crossed courtyards |
| Broker / DB | Mosquitto / TimescaleDB | Kafka / InfluxDB | footprint, SQL, continuous aggregates |
| Deployment | k3s, node-local images, `deploy.sh` | ArgoCD GitOps | images never leave the cluster; GitOps added nothing and `selfHeal` conflicts with failover tests |
| Secrets | Kubernetes Secret `fds-app`; Vault for tooling | Vault token in the pod | no root token in the namespace |

## Cluster

| Node | Role | Hardware |
|---|---|---|
| proart-master-01 | control plane, server, BuildSim, GPU inference | Ryzen 9 9950X, 128 GB, RTX 5060 Ti 16 GB |
| pi400-master-01 | active edge controllers | Raspberry Pi 400, ARM aarch64, 4 GB |
| infinity-magic | standby edge controllers | amd64 worker |
