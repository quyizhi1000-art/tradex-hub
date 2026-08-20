from __future__ import annotations

from astock_signals.smart_router import SmartRouter


def test_route_validated_falls_back_before_recording_success() -> None:
    router = SmartRouter()
    router.register("example", "bad", lambda: {"value": "bad"}, priority=1)
    router.register("example", "good", lambda: {"value": "7"}, priority=2)

    def validate(payload, provider):
        if provider == "bad":
            raise ValueError("payload violates the canonical contract")
        return int(payload["value"])

    value, provider = router.route_validated("example", validate)

    assert (value, provider) == (7, "good")
    health = {item["source"]: item for item in router.get_health_report()}
    assert health["example:bad"]["fail_count"] == 1
    assert health["example:bad"]["success_rate"] == 0.0
    assert health["example:good"]["success_rate"] == 100.0

