"""Fake production systems the runbooks act on: deploys, replicas, caches, feature flags.

Every action has a plan step (what would change, no side effects) and an apply step. Apply is
idempotent: applying the same resolved arguments twice changes nothing the second time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SERVICES = ("web-app", "auth", "invoices-api", "payments-api", "checkout", "search",
            "notifications", "webhooks", "reporting")

BASELINE_ERROR_RATE = 0.002
READ_ONLY = {"deploy.status", "health.check"}


class ActionError(ValueError):
    pass


@dataclass
class Plan:
    action: str
    args: dict[str, Any]  # resolved arguments; apply uses these, not the template
    changes: bool
    summary: str


@dataclass
class FakeSystems:
    versions: dict[str, list[str]] = field(default_factory=dict)  # deploy history, last = live
    replicas: dict[str, int] = field(default_factory=dict)
    cache_keys: dict[str, int] = field(default_factory=dict)
    flags: dict[str, bool] = field(default_factory=dict)
    error_rate: dict[str, float] = field(default_factory=dict)

    @classmethod
    def default(cls) -> FakeSystems:
        s = cls()
        for i, svc in enumerate(SERVICES):
            s.versions[svc] = ["v2026.6.1", "v2026.6.2"]
            s.replicas[svc] = 4 + (i % 3) * 2
            s.cache_keys[svc] = 12_000 + i * 1_500
            s.error_rate[svc] = BASELINE_ERROR_RATE
        s.flags = {"new-checkout-flow": False, "batched-email-sends": False,
                   "search-v2-ranking": False, "pdf-renderer-v3": False,
                   "smart-retry-webhooks": False}
        return s

    def _service(self, args: dict[str, Any]) -> str:
        svc = str(args.get("service", "")).strip()
        if svc not in self.versions:
            raise ActionError(f"unknown service {svc!r}")
        return svc

    # deploy events from the bus land here too, so rollbacks see real history
    def record_deploy(self, service: str, version: str) -> None:
        hist = self.versions.setdefault(service, [])
        if not hist or hist[-1] != version:
            hist.append(version)

    def plan(self, action: str, args: dict[str, Any]) -> Plan:
        fn = getattr(self, "_plan_" + action.replace(".", "_"), None)
        if fn is None:
            raise ActionError(f"unknown action {action!r}")
        return fn(dict(args))

    def apply(self, plan: Plan) -> str:
        if plan.action in READ_ONLY:
            return plan.summary
        if not plan.changes:
            return f"no change: {plan.summary}"
        fn = getattr(self, "_apply_" + plan.action.replace(".", "_"))
        return fn(plan.args)

    # read-only checks ---------------------------------------------------------
    def _plan_deploy_status(self, args):
        svc = self._service(args)
        hist = self.versions[svc]
        prev = hist[-2] if len(hist) > 1 else "none"
        return Plan("deploy.status", {"service": svc}, False,
                    f"{svc} runs {hist[-1]} (previous {prev})")

    def _plan_health_check(self, args):
        svc = self._service(args)
        rate = self.error_rate.get(svc, BASELINE_ERROR_RATE)
        verdict = "healthy" if rate <= BASELINE_ERROR_RATE * 2 else "unhealthy"
        return Plan("health.check", {"service": svc}, False,
                    f"{svc} error rate {rate * 100:.1f}%, {verdict}")

    # rollback -----------------------------------------------------------------
    def _plan_deploy_rollback(self, args):
        svc = self._service(args)
        hist = self.versions[svc]
        target = args.get("to") or ""
        if not target or target == "previous":
            if len(hist) < 2:
                raise ActionError(f"{svc} has no previous version to roll back to")
            target = hist[-2]
        if target not in hist:
            raise ActionError(f"{svc} never ran {target}")
        changes = hist[-1] != target
        summary = (f"roll back {svc} from {hist[-1]} to {target}" if changes
                   else f"{svc} already runs {target}")
        return Plan("deploy.rollback", {"service": svc, "to": target}, changes, summary)

    def _apply_deploy_rollback(self, args):
        svc, target = args["service"], args["to"]
        current = self.versions[svc][-1]
        self.versions[svc].append(target)
        self.error_rate[svc] = BASELINE_ERROR_RATE
        return f"rolled back {svc} from {current} to {target}"

    # scale --------------------------------------------------------------------
    def _plan_service_scale(self, args):
        svc = self._service(args)
        now = self.replicas[svc]
        want = str(args.get("replicas", "x2")).strip()
        try:
            target = now * int(want[1:]) if want.startswith("x") else int(want)
        except ValueError as exc:
            raise ActionError(f"replicas must be a number or x<factor>, got {want!r}") from exc
        if not 1 <= target <= 200:
            raise ActionError(f"replicas {target} out of range 1 to 200")
        changes = target != now
        summary = (f"scale {svc} from {now} to {target} replicas" if changes
                   else f"{svc} already has {target} replicas")
        return Plan("service.scale", {"service": svc, "replicas": str(target)}, changes, summary)

    def _apply_service_scale(self, args):
        svc, target = args["service"], int(args["replicas"])
        before = self.replicas[svc]
        self.replicas[svc] = target
        self.error_rate[svc] = BASELINE_ERROR_RATE
        return f"scaled {svc} from {before} to {target} replicas"

    # cache --------------------------------------------------------------------
    def _plan_cache_flush(self, args):
        svc = self._service(args)
        n = self.cache_keys.get(svc, 0)
        return Plan("cache.flush", {"service": svc}, n > 0,
                    f"flush {n} keys from the {svc} cache" if n else f"{svc} cache is empty")

    def _apply_cache_flush(self, args):
        svc = args["service"]
        n = self.cache_keys[svc]
        self.cache_keys[svc] = 0
        self.error_rate[svc] = BASELINE_ERROR_RATE
        return f"flushed {n} keys from the {svc} cache"

    # flags --------------------------------------------------------------------
    def _plan_flag_set(self, args):
        flag = str(args.get("flag", "")).strip()
        if flag not in self.flags:
            raise ActionError(f"unknown flag {flag!r}")
        raw = str(args.get("enabled", "false")).lower()
        if raw not in ("true", "false", "on", "off"):
            raise ActionError(f"enabled must be true or false, got {raw!r}")
        enabled = raw in ("true", "on")
        changes = self.flags[flag] != enabled
        state = "on" if enabled else "off"
        summary = f"turn {flag} {state}" if changes else f"{flag} is already {state}"
        return Plan("flag.set", {"flag": flag, "enabled": str(enabled).lower(),
                                 "service": args.get("service", "")}, changes, summary)

    def _apply_flag_set(self, args):
        enabled = args["enabled"] == "true"
        self.flags[args["flag"]] = enabled
        if args.get("service") in self.error_rate:
            self.error_rate[args["service"]] = BASELINE_ERROR_RATE
        return f"turned {args['flag']} {'on' if enabled else 'off'}"
