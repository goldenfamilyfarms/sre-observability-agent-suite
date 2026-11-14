"""Bounded read-only Temporal observations; never start, poll, signal or cancel work."""

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from temporalio.api.enums.v1 import NamespaceState, TaskQueueType
from temporalio.api.taskqueue.v1 import TaskQueue
from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest, DescribeTaskQueueRequest
from temporalio.client import Client
from temporalio.service import RPCError, RPCStatusCode

from .readiness import Check, failure, require, temporal_config


async def inspect_temporal(client: Client, queue: str, execution_id: str) -> list[Check]:
    checks = []
    timeout = timedelta(seconds=5)
    try:
        result = await client.workflow_service.describe_namespace(
            DescribeNamespaceRequest(namespace=client.namespace),
            timeout=timeout,
        )
        require(
            result.namespace_info.name == client.namespace
            and result.namespace_info.state == NamespaceState.NAMESPACE_STATE_REGISTERED
        )
    except Exception as error:
        return [
            failure(
                "temporal_namespace",
                "Connect to an accessible registered Temporal namespace.",
                error,
            )
        ]
    checks.append(
        Check(
            "temporal_namespace", "pass", "The configured namespace is accessible and registered."
        )
    )
    for name, kind in (
        ("temporal_workflow_pollers", TaskQueueType.TASK_QUEUE_TYPE_WORKFLOW),
        ("temporal_activity_pollers", TaskQueueType.TASK_QUEUE_TYPE_ACTIVITY),
    ):
        try:
            queue_result = await client.workflow_service.describe_task_queue(
                DescribeTaskQueueRequest(
                    namespace=client.namespace,
                    task_queue=TaskQueue(name=queue),
                    task_queue_type=kind,
                ),
                timeout=timeout,
            )
            now = datetime.now(UTC)
            require(
                any(
                    timedelta(0)
                    <= now - p.last_access_time.ToDatetime(tzinfo=UTC)
                    <= timedelta(minutes=5)
                    for p in queue_result.pollers
                )
            )
        except Exception as error:
            checks.append(
                failure(
                    name,
                    "Start the worker on this namespace/queue; recent pollers are required.",
                    error,
                )
            )
        else:
            checks.append(
                Check(
                    name,
                    "pass",
                    "A poller was observed within five minutes; current capacity is not proven.",
                )
            )
    try:
        await client.get_workflow_handle(execution_id).describe(rpc_timeout=timeout)
    except RPCError as error:
        if error.status == RPCStatusCode.NOT_FOUND:
            checks.append(
                Check(
                    "temporal_execution_id",
                    "pass",
                    "No retained workflow with this execution ID was found.",
                )
            )
        else:
            checks.append(
                failure(
                    "temporal_execution_id",
                    "Check access to the requested workflow identity.",
                    error,
                )
            )
    except Exception as error:
        checks.append(
            failure(
                "temporal_execution_id", "Check access to the requested workflow identity.", error
            )
        )
    else:
        checks.append(
            Check(
                "temporal_execution_id",
                "fail",
                "Execution ID already exists; reconcile it before choosing a new run ID.",
            )
        )
    checks.append(
        Check(
            "temporal_start_authorization",
            "unverified",
            "Describe access does not prove start authorization or future worker availability.",
        )
    )
    return checks


async def probe(env: Mapping[str, str], execution_id: str) -> list[Check]:
    try:
        address, namespace, queue, tls = temporal_config(env)
        client = await asyncio.wait_for(
            Client.connect(
                address, namespace=namespace, tls=tls, api_key=env.get("SRE_TEMPORAL_API_KEY")
            ),
            timeout=10,
        )
        return await asyncio.wait_for(inspect_temporal(client, queue, execution_id), timeout=25)
    except Exception as error:
        return [
            failure(
                "temporal_connectivity",
                "Check the Temporal service, TLS, namespace and credentials.",
                error,
            )
        ]
