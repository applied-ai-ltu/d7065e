# D7065E — Fire Detection & Autonomous Response System

**Course**: D7065E Embedded Intelligence at the Edge, LTU Sweden  
**Student**: Binay Mishra, Group 3  
**Repository**: [applied-ai-ltu/d7065e](https://github.com/applied-ai-ltu/d7065e)

## Overview

An autonomous fire detection and response system for the A-Building at LTU, built on the BuildSim building simulation platform. The system monitors 950 rooms across 3 floors using simulated smoke, CO, and temperature sensors, detects fire events using an isolation forest anomaly detector, and generates response plans using an LLM agent.

### Key Capabilities

- **Full building monitoring**: 950 rooms, 3 floors, 2,868 sensors
- **Physics-based simulation**: ground truth with heat propagation, smoke diffusion, cross-floor spread via stairwells
- **Realistic sensor degradation**: noise, measurement lag, drift, stuck/offline sensors
- **Multi-sensor fusion**: smoke + CO + temperature correlation distinguishes real fires from false alarms
- **LLM-powered agent**: assesses situations, generates response plans, verifies safety constraints
- **Graceful degradation**: rule-based fallback when LLM is unavailable
- **Edge deployment**: ONNX model runs on Raspberry Pi for local inference
- **Real-time monitoring**: Grafana dashboards with per-floor filtering
- **MLOps pipeline**: ClearML for training tracking, Phoenix for LLM observability

## Architecture

## Infrastructure

| Service | Port | Description |
|---------|------|-------------|
| BuildSim | 30909 | 3D building simulation |
| Mosquitto | 31174 | MQTT broker |
| TimescaleDB | 30543 | Time-series database |
| LiteLLM | 30400 | LLM routing (self-hosted + cloud) |
| Ollama | 31434 | Local LLM inference |
| Grafana | 30300 | Monitoring dashboards |
| Phoenix | 30600 | LLM observability |
| ClearML | 30808 | ML experiment tracking |
| Vault | 30820 | Secrets management |
| Control API | 8089 | Fire simulation control |

## Quick Start

### Prerequisites

- K3s cluster with GPU node
- BuildSim deployed
- Mosquitto, TimescaleDB, LiteLLM, Vault running
- Python 3.11+

### Setup

```bash
cd fire-detection-sim

# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -e ".[all,dev]"

# Copy and configure
cp configs/config.yaml.example configs/config.yaml
cp .env.example .env
# Edit config.yaml and .env with your values

# Set environment
export VAULT_ADDR="https://your-vault:30820"
export PYTHONWARNINGS="ignore:Unverified HTTPS request"

# Initialize database
python3 -c "
import asyncio
from fds.config import Config
from fds.pipeline.schema import init_database
from fds.secrets import get_secrets
config = Config.from_yaml('configs/config.yaml')
secrets = get_secrets()
asyncio.run(init_database(config.database.host, config.database.port,
    config.database.user, secrets['db_password'], config.database.database))
"

# Train the detector
python -m fds.models.trainer
```

### Run

```bash
# Start the full system (all 3 floors, 950 rooms)
python -m fds.run_full

# In another terminal — trigger a fire
curl -X POST http://localhost:8089/fire/start \
  -H "Content-Type: application/json" \
  -d '{"room": "A109"}'

# Stop the fire
curl -X POST http://localhost:8089/fire/stop \
  -H "Content-Type: application/json" \
  -d '{"room": "A109"}'
```

### Monitor

- **Grafana**: `https://your-host:30300` — operations + technical dashboards
- **BuildSim**: `http://your-host:30909` — 3D building viewer
- **Control API**: `http://localhost:8089/docs` — Swagger UI
- **Phoenix**: `https://your-host:30600` — LLM traces
- **ClearML**: `https://your-host:30808` — training experiments

## Project Structure

## Tests

```bash
# Run all unit tests (38 passing)
pytest tests/ -v --ignore=tests/test_buildsim.py

# Run integration tests (requires live BuildSim)
pytest tests/test_buildsim.py -v
pytest tests/test_graceful_degradation.py -v
```

## Edge Deployment (Raspberry Pi)

```bash
# On ProArt: export ONNX model
python3 -c "
from fds.models.detector import FireDetector
detector = FireDetector()
detector.load('models/detector.pkl')
detector.export_onnx('models/detector.onnx')
"

# Copy to Pi
scp models/detector.onnx pi:/home/user/
scp src/fds/models/edge_detector.py pi:/home/user/

# On Pi: install and test
pip install onnxruntime numpy
python3 edge_detector.py
```

## Design Decisions

| Decision | Chosen | Rejected | Rationale |
|----------|--------|----------|-----------|
| Message broker | MQTT (Mosquitto) | Kafka | Industry standard for building sensors, 5MB vs 1-2GB footprint |
| Time-series DB | TimescaleDB | InfluxDB | Continuous aggregates, SQL compatibility, single tool for storage + computation |
| Feature computation | Continuous aggregates | Apache Flink | No additional operational complexity at simulation scale |
| Implementation | Python | Rust | 5-week timeline, all components in one language |
| LLM deployment | Self-hosted (LiteLLM) | Cloud-only (Groq) | Fire safety system must work without internet |
| Anomaly detection | Isolation forest | Autoencoder | Simple, interpretable, sufficient for multi-sensor fusion |
| Architecture | Separate physics + gateway | Merged sensors | Matches reference, proper separation of concerns |
| Edge inference | ONNX Runtime | TensorFlow Lite | Cross-platform, scikit-learn native export |

## Homelab Cluster

| Node | Role | Hardware |
|------|------|----------|
| ProArt | Control plane + GPU workloads | Ryzen 9 9950X, 128GB RAM, RTX 5060 Ti 16GB |
| Pi 400 | Edge inference | ARM aarch64, 4GB RAM |
| Jetson Orin Nano | Embedding model | ARM, GPU |
| Infinity Magic | Worker | Additional compute |

## Docker (Portable Deployment)

A Dockerfile is provided for running the system outside the homelab K3s cluster. See `fire-detection-sim/Dockerfile`.

```bash
docker build -t fire-detection-sim fire-detection-sim/
docker run -e VAULT_ADDR=... -e DB_PASSWORD=... fire-detection-sim
```

> **Note**: BuildSim, Mosquitto, TimescaleDB, and LiteLLM must be reachable from the container.

## License

Course project — LTU D7065E Embedded Intelligence at the Edge.
