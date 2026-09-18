"""Fetch secrets from HashiCorp Vault."""

import logging
import os

import hvac

logger = logging.getLogger(__name__)


def get_secrets(path="homelab/d7065e", mount="secret"):
    """Fetch secrets from Vault. Falls back to env vars if Vault is unavailable."""
    vault_addr = os.getenv("VAULT_ADDR", "https://localhost:30820")
    vault_token = os.getenv("VAULT_TOKEN", "")

    if not vault_token:
        # Try reading from file
        try:
            with open(os.path.expanduser("~/.vault-token")) as f:
                vault_token = f.read().strip()
        except FileNotFoundError:
            pass

    if not vault_token:
        logger.warning("No Vault token found, falling back to env vars")
        return {
            "litellm_api_key": os.getenv("LITELLM_API_KEY", ""),
            "db_password": os.getenv("DB_PASSWORD", ""),
        }

    try:
        client = hvac.Client(url=vault_addr, token=vault_token, verify=False)
        result = client.secrets.kv.v2.read_secret_version(path=path, mount_point=mount)
        secrets = result["data"]["data"]
        logger.info(f"Secrets loaded from Vault ({path})")
        return secrets
    except Exception as e:
        logger.warning(f"Vault unavailable ({e}), falling back to env vars")
        return {
            "litellm_api_key": os.getenv("LITELLM_API_KEY", ""),
            "db_password": os.getenv("DB_PASSWORD", ""),
        }
