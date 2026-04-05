from dataclasses import dataclass
from typing import Dict, List


@dataclass(frozen=True)
class IncidentScenario:
    task_id: str
    title: str
    difficulty: str
    root_cause: str
    correct_fix: str
    destructive_fixes: List[str]
    alert_clues: List[str]
    metric_clues: Dict[str, float]
    service_clues: Dict[str, str]
    log_clues: Dict[str, List[str]]
    services: List[str]
    expected_hypothesis_tokens: List[str]


SCENARIOS: List[IncidentScenario] = [
    IncidentScenario(
        task_id="easy",
        title="Checkout API latency spike",
        difficulty="easy",
        root_cause="Cache miss storm due to expired Redis keyspace",
        correct_fix="scale-cache-cluster",
        destructive_fixes=["restart-database-primary"],
        alert_clues=[
            "p95 latency crossed 1200ms on checkout-api",
            "cache hit ratio dropped below 35%",
        ],
        metric_clues={
            "checkout_api_p95_ms": 1285.0,
            "cache_hit_ratio": 0.31,
            "db_cpu_percent": 42.0,
            "app_cpu_percent": 49.0,
        },
        service_clues={
            "checkout-api": "healthy but waiting on cache",
            "redis-cache": "high eviction rate and miss storm",
            "orders-db": "healthy",
        },
        log_clues={
            "checkout-api": [
                "WARN cache lookup timeout for cart-session",
                "INFO fallback query to orders-db completed in 880ms",
            ],
            "redis-cache": [
                "WARN maxmemory reached, evicting hot keys",
                "INFO keyspace TTL batch expired unexpectedly",
            ],
        },
        services=["checkout-api", "redis-cache", "orders-db"],
        expected_hypothesis_tokens=["cache", "redis", "miss", "eviction"],
    ),
    IncidentScenario(
        task_id="medium",
        title="Payment API intermittent 503",
        difficulty="medium",
        root_cause="DB connection pool saturation in payments service",
        correct_fix="increase-payment-db-pool",
        destructive_fixes=["drop-payment-read-replica"],
        alert_clues=[
            "payment-api 503 rate reached 8%",
            "database connection wait exceeded 900ms",
        ],
        metric_clues={
            "payment_503_rate": 0.08,
            "payment_db_pool_utilization": 0.98,
            "payment_worker_cpu_percent": 58.0,
            "cache_hit_ratio": 0.79,
        },
        service_clues={
            "payment-api": "degraded due to DB wait",
            "payment-worker": "queue backpressure increasing",
            "payment-db": "healthy CPU, saturated connection slots",
        },
        log_clues={
            "payment-api": [
                "ERROR timeout acquiring DB connection from pool",
                "WARN retry budget exhausted for charge request",
            ],
            "payment-worker": [
                "WARN backlog rising; waiting on payment-db connections",
            ],
        },
        services=["payment-api", "payment-worker", "payment-db"],
        expected_hypothesis_tokens=["db", "pool", "connection", "saturation"],
    ),
    IncidentScenario(
        task_id="hard",
        title="Platform-wide latency and 5xx burst",
        difficulty="hard",
        root_cause="Search service thread pool exhaustion after bad rollout",
        correct_fix="rollback-search-rollout",
        destructive_fixes=["restart-platform-gateway"],
        alert_clues=[
            "global p95 latency exceeded 1600ms",
            "5xx error burst correlated with search traffic peaks",
        ],
        metric_clues={
            "gateway_5xx_rate": 0.11,
            "search_thread_pool_utilization": 0.99,
            "search_queue_depth": 782.0,
            "gateway_cpu_percent": 51.0,
        },
        service_clues={
            "platform-gateway": "stable CPU but high upstream failures",
            "search-service": "thread pool exhausted after rollout v3.14",
            "recommendation-service": "healthy",
        },
        log_clues={
            "platform-gateway": [
                "ERROR upstream timeout from search-service after 1200ms",
            ],
            "search-service": [
                "WARN rejected execution: thread pool queue capacity reached",
                "INFO rollout marker: build v3.14 deployed 12m ago",
            ],
        },
        services=["platform-gateway", "search-service", "recommendation-service"],
        expected_hypothesis_tokens=["search", "thread", "pool", "rollout"],
    ),
]


SCENARIO_BY_ID = {scenario.task_id: scenario for scenario in SCENARIOS}


def scenario_by_index(index: int) -> IncidentScenario:
    return SCENARIOS[index % len(SCENARIOS)]
