"""Trusted, configurable allowances for the functional-validation stage.

These govern research effort, not sandbox or user-data permissions. Model tool
arguments cannot change them. Values are frozen for each broker process.
"""
from dataclasses import dataclass, fields
import os
from .config import DEEPSEEK_MODEL_ID


@dataclass(frozen=True)
class ResearchCapacity:
    researches: int = 1
    queued: int = 6

    def __post_init__(self):
        if type(self.researches) is not int or not 1 <= self.researches <= 2:
            raise ValueError('research concurrency must be 1 or 2')
        if type(self.queued) is not int or not 0 <= self.queued <= 32:
            raise ValueError('research queue capacity must be between 0 and 32')

    @classmethod
    def from_environment(cls, env=None):
        env = os.environ if env is None else env
        return cls(int(env.get('VOXQUARRY_WENQUAN_RESEARCH_CONCURRENCY', '1')),
                   int(env.get('VOXQUARRY_WENQUAN_QUEUED_RESEARCHES', '6')))


@dataclass(frozen=True)
class ResearchLimits:
    model_calls: int = 160
    tool_calls: int = 1000
    search_calls: int = 50
    fetch_calls: int = 100
    total_tokens: int = 200_000_000
    active_seconds: int = 3600
    model_budget_cny: int = 100
    deepseek_output_tokens: int = 131072
    deepseek_context_tokens: int = 1_000_000
    research_context_tokens: int = 200_000
    model_timeout_seconds: int = 1200
    model_request_bytes: int = 16_000_000
    tool_parallelism: int = 4

    def __post_init__(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or value <= 0:
                raise ValueError("research allowance must be a positive integer: " + field.name)
        if self.deepseek_output_tokens > 393216 or self.deepseek_context_tokens > 1_000_000:
            raise ValueError("research allowance exceeds the configured provider capability")
        if self.research_context_tokens > 1_000_000:
            raise ValueError("research working context exceeds provider capacity")
        if self.active_seconds > 7200 or self.model_timeout_seconds > 1800:
            raise ValueError("research timeout exceeds the runtime protocol capacity")
        if self.tool_parallelism > 8:
            raise ValueError("research tool parallelism exceeds 8")

    @classmethod
    def from_environment(cls, env=None):
        env = os.environ if env is None else env
        return cls(**{field.name: int(env["VOXQUARRY_WENQUAN_" + field.name.upper()])
                      for field in fields(cls) if "VOXQUARRY_WENQUAN_" + field.name.upper() in env})

    def runtime(self, model):
        deepseek = model == DEEPSEEK_MODEL_ID
        return {"context_window": self.deepseek_context_tokens if deepseek else 200000,
                "research_context_tokens": min(self.research_context_tokens, self.deepseek_context_tokens if deepseek else 200000),
                "max_output_tokens": self.deepseek_output_tokens if deepseek else 16384,
                "model_timeout_seconds": self.model_timeout_seconds}
