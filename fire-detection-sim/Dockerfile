FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc && rm -rf /var/lib/apt/lists/*

# Copy project files
COPY pyproject.toml .
COPY src/ src/
COPY configs/config.yaml.example configs/config.yaml

# Install Python dependencies
RUN pip install --no-cache-dir -e ".[all]"

# Train initial model
RUN python -m fds.models.trainer

# Expose control API port
EXPOSE 8089

# Environment variables (override at runtime)
ENV VAULT_ADDR="https://localhost:30820"
ENV PYTHONWARNINGS="ignore:Unverified HTTPS request"

CMD ["python", "-m", "fds.run_full"]
