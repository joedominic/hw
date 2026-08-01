"""Unit tests for Redis-backed LLM rate-limit reservation accounting."""
from __future__ import annotations

from django.test import SimpleTestCase

from resume_app.llm_rate_limit import RATE_LIMIT_BUCKET_TTL_SECONDS, _RateLimitReservation


class _FakePipeline:
    def __init__(self, client: "_FakeRedis") -> None:
        self.client = client
        self.ops: list[tuple] = []

    def incrby(self, key: str, amount: int) -> "_FakePipeline":
        self.ops.append(("incrby", key, amount))
        return self

    def decr(self, key: str) -> "_FakePipeline":
        self.ops.append(("decr", key))
        return self

    def expire(self, key: str, seconds: int) -> "_FakePipeline":
        self.ops.append(("expire", key, seconds))
        return self

    def execute(self) -> list:
        results = []
        for op in self.ops:
            if op[0] == "incrby":
                results.append(self.client.incrby(op[1], op[2]))
            elif op[0] == "decr":
                results.append(self.client.decr(op[1]))
            elif op[0] == "expire":
                results.append(self.client.expire(op[1], op[2]))
        self.ops.clear()
        return results


class _FakeRedis:
    """Minimal Redis stand-in that recreates keys without TTL on INCRBY/DECR."""

    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.ttls: dict[str, int | None] = {}

    def pipeline(self) -> _FakePipeline:
        return _FakePipeline(self)

    def incrby(self, key: str, amount: int) -> int:
        if key not in self.values:
            self.values[key] = 0
            self.ttls[key] = None  # recreate with no expiry (Redis behavior)
        self.values[key] += int(amount)
        return self.values[key]

    def decr(self, key: str) -> int:
        return self.incrby(key, -1)

    def expire(self, key: str, seconds: int) -> bool:
        if key not in self.values:
            return False
        self.ttls[key] = int(seconds)
        return True

    def ttl(self, key: str) -> int:
        if key not in self.values:
            return -2
        ttl = self.ttls.get(key)
        return -1 if ttl is None else ttl


class RateLimitReservationTtlTests(SimpleTestCase):
    def test_release_on_invoke_failure_reapplies_ttl(self) -> None:
        r = _FakeRedis()
        r.values["req"] = 1
        r.values["tok"] = 100
        r.ttls["req"] = None  # simulate expired-and-recreated state
        r.ttls["tok"] = None
        res = _RateLimitReservation(
            redis=r, req_key="req", tok_key="tok", estimated_tokens=100, acquired=True
        )
        res.release_on_invoke_failure()
        self.assertEqual(r.values["req"], 0)
        self.assertEqual(r.values["tok"], 0)
        self.assertEqual(r.ttls["req"], RATE_LIMIT_BUCKET_TTL_SECONDS)
        self.assertEqual(r.ttls["tok"], RATE_LIMIT_BUCKET_TTL_SECONDS)

    def test_reconcile_tokens_reapplies_ttl(self) -> None:
        r = _FakeRedis()
        r.values["tok"] = 50
        r.ttls["tok"] = None
        res = _RateLimitReservation(
            redis=r, req_key="req", tok_key="tok", estimated_tokens=50, acquired=True
        )
        res.reconcile_tokens(actual_prompt_tokens=80)
        self.assertEqual(r.values["tok"], 80)
        self.assertEqual(r.ttls["tok"], RATE_LIMIT_BUCKET_TTL_SECONDS)
