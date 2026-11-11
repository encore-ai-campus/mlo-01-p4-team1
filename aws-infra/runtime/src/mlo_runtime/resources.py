"""Shared resource ceilings for the low-load, 48-hour service profile.

These are scheduling limits, not reserved CPU or a throughput guarantee. Real
artifact acceptance and active-group fencing remain mandatory.
"""

SPARK_WORKER_CPUS = 1.0
SPARK_WORKER_CORES = 1
SPARK_WORKER_MEMORY = "3g"
SPARK_APPLICATION_CORES = 1
SPARK_DRIVER_MEMORY = "1g"
SPARK_EXECUTOR_MEMORY = "1g"
SPARK_EXECUTOR_CORES = 1


def spark_resource_conf():
    return {
        "spark.cores.max": str(SPARK_APPLICATION_CORES),
        "spark.executor.cores": str(SPARK_EXECUTOR_CORES),
        "spark.executor.memory": SPARK_EXECUTOR_MEMORY,
        "spark.driver.memory": SPARK_DRIVER_MEMORY,
        "spark.dynamicAllocation.enabled": "false",
    }


def spark_submit_resources():
    return [
        "--driver-memory", SPARK_DRIVER_MEMORY,
        "--executor-memory", SPARK_EXECUTOR_MEMORY,
        "--executor-cores", str(SPARK_EXECUTOR_CORES),
        "--total-executor-cores", str(SPARK_APPLICATION_CORES),
        "--conf", "spark.dynamicAllocation.enabled=false",
    ]


def resource_profile(inventory):
    workers = sum(node["role"] == "spark" for node in inventory["nodes"])
    if workers != 3:
        raise ValueError("low-load profile requires all three Spark workers")
    return {
        "name": "low-load-48h-v1",
        "service_hours": 48,
        "billing_window_hours": 52,
        "worker_cpu_limit": SPARK_WORKER_CPUS,
        "worker_advertised_cores": SPARK_WORKER_CORES,
        "cluster_executor_cores": workers * SPARK_WORKER_CORES,
        "application_max_cores": SPARK_APPLICATION_CORES,
        "driver_memory": SPARK_DRIVER_MEMORY,
        "executor_memory": SPARK_EXECUTOR_MEMORY,
        "continuous_queries": ["weather-stream", "feedback-postgresql"],
        "shared_batch_queries": ["feedback-analytics-batch", "venues-catalog"],
        "batch_capacity_with_both_streams_running": 1,
        "scheduling": "start both streams first; batch apps share remaining core and must stop SparkContext on completion",
        "acceptance": "accepted artifacts must honor these ceilings; confirm executor allocation on all workers",
        "ready_for_aws": False,
    }
