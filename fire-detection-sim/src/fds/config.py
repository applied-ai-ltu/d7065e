"""Configuration management."""

import yaml
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class BuildSimConfig:
    host: str = "192.168.68.67"
    port: int = 30909

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass
class MQTTConfig:
    host: str = "192.168.68.67"
    port: int = 1883
    topic_prefix: str = "building"


@dataclass
class DatabaseConfig:
    host: str = "192.168.68.67"
    port: int = 5432
    user: str = "postgres"
    password: str = ""
    database: str = "fire_detection"


@dataclass
class LLMConfig:
    base_url: str = "https://192.168.68.67:30400"
    model: str = "gemma4:12b"
    fallback_model: str = "phi4-mini"


@dataclass
class Config:
    buildsim: BuildSimConfig = field(default_factory=BuildSimConfig)
    mqtt: MQTTConfig = field(default_factory=MQTTConfig)
    database: DatabaseConfig = field(default_factory=DatabaseConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Config":
        path = Path(path)
        if not path.exists():
            return cls()
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
        return cls(
            buildsim=BuildSimConfig(**raw.get("buildsim", {})),
            mqtt=MQTTConfig(**raw.get("mqtt", {})),
            database=DatabaseConfig(**raw.get("database", {})),
            llm=LLMConfig(**raw.get("llm", {})),
        )
