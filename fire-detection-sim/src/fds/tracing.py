"""Phoenix tracing and ClearML integration.

Uses phoenix.otel.register for project-aware trace routing via gRPC.
"""

import logging
import os

logger = logging.getLogger(__name__)


def setup_phoenix(project_name="d7065e"):
    """Initialize Phoenix tracing."""
    try:
        from phoenix.otel import register

        endpoint = os.getenv("PHOENIX_COLLECTOR_ENDPOINT", "http://localhost:30317")

        provider = register(
            project_name=project_name,
            endpoint=endpoint,
            protocol="grpc",
            batch=True,
        )

        logger.info(f"Phoenix tracing enabled (project={project_name}, endpoint={endpoint})")
        return provider

    except Exception as e:
        logger.warning(f"Phoenix tracing setup failed: {e}")
        return None


def setup_clearml(project_name="d7065e", task_name="fire-detection"):
    """Initialize ClearML experiment tracking."""
    try:
        from clearml import Task

        task = Task.init(
            project_name=project_name,
            task_name=task_name,
            auto_connect_frameworks=True,
        )
        logger.info(f"ClearML task created: {project_name}/{task_name}")
        return task
    except Exception as e:
        logger.warning(f"ClearML setup failed: {e}")
        return None
